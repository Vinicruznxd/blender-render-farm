from __future__ import annotations

import json
import secrets
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .logger import get_job_log_path
from .models import RenderBlockRequest
from .subprocess_utils import decode_subprocess_output

SESSION_READY_TIMEOUT_SECONDS = 5.0
SESSION_SHUTDOWN_TIMEOUT_SECONDS = 10.0
SESSION_ACK_POLL_INTERVAL_SECONDS = 0.25
PERSISTENT_SESSION_FAILURE_COOLDOWN_SECONDS = 300.0


def _normalize_path(value: str) -> str:
    try:
        return str(Path(value).resolve(strict=False)).casefold()
    except OSError:
        return str(Path(value)).casefold()


def _capture_output_fingerprints(output_paths: list[str]) -> dict[str, tuple[int, int] | None]:
    result: dict[str, tuple[int, int] | None] = {}
    for raw_path in output_paths:
        path = Path(raw_path)
        try:
            stat = path.stat()
            result[raw_path] = (int(stat.st_size), int(stat.st_mtime_ns)) if path.is_file() else None
        except OSError:
            result[raw_path] = None
    return result


def _outputs_changed_since(
    output_paths: list[str], baselines: dict[str, tuple[int, int] | None]
) -> bool:
    for raw_path in output_paths:
        path = Path(raw_path)
        try:
            stat = path.stat()
        except OSError:
            return False
        current = (int(stat.st_size), int(stat.st_mtime_ns))
        if not path.is_file() or current[0] <= 0 or baselines.get(raw_path) == current:
            return False
    return True


@dataclass(frozen=True, slots=True)
class SessionCompatibilityKey:
    job_id: str
    blend_file: str
    blend_file_size: int | None
    blend_file_mtime_ns: int | None
    selected_blender_path: str
    selected_blender_version: str
    render_device_preference: str
    effective_use_gpu: bool
    output_pattern: str
    image_format: str
    required_blender_version: str
    preferred_blender_major: int | None
    version_routing_policy: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SessionCompatibilityKey":
        blend_file = str(payload["blendFile"])
        try:
            blend_stat = Path(blend_file).stat()
            blend_file_size: int | None = int(blend_stat.st_size)
            blend_file_mtime_ns: int | None = int(blend_stat.st_mtime_ns)
        except OSError:
            blend_file_size = None
            blend_file_mtime_ns = None
        return cls(
            job_id=str(payload["jobId"]).strip(),
            blend_file=_normalize_path(blend_file),
            blend_file_size=blend_file_size,
            blend_file_mtime_ns=blend_file_mtime_ns,
            selected_blender_path=_normalize_path(str(payload.get("selectedBlenderPath") or "")),
            selected_blender_version=str(payload.get("selectedBlenderVersion") or "").strip(),
            render_device_preference=str(payload["renderDevicePreference"]).strip().upper(),
            effective_use_gpu=bool(payload["useGpu"]),
            output_pattern=_normalize_path(str(payload["outputPattern"])),
            image_format=str(payload["format"]).strip().upper(),
            required_blender_version=str(payload.get("requiredBlenderVersion") or "").strip(),
            preferred_blender_major=(
                int(payload["preferredBlenderMajor"])
                if payload.get("preferredBlenderMajor") is not None
                else None
            ),
            version_routing_policy=str(payload.get("versionRoutingPolicy") or "").strip(),
        )


@dataclass(slots=True)
class QueuedRenderRequest:
    request: RenderBlockRequest
    status_payload: dict[str, Any]
    compatibility_key: SessionCompatibilityKey
    selected_blender_path: str
    selected_blender_version: str
    blend_version_detected: str
    render_device_preference: str
    effective_use_gpu: bool

    def assignment_summary(self) -> dict[str, Any]:
        return {
            "jobId": self.request.jobId,
            "blockId": self.request.blockId,
            "assignmentId": self.request.assignmentId,
            "startFrame": self.request.startFrame,
            "endFrame": self.request.endFrame,
        }

    def convert_to_cpu(self) -> None:
        self.render_device_preference = "CPU"
        self.effective_use_gpu = False
        self.status_payload["useGpu"] = False
        self.status_payload["renderDevicePreference"] = "CPU"
        self.compatibility_key = SessionCompatibilityKey.from_payload(self.status_payload)


@dataclass(slots=True)
class SessionProcessHandle:
    key: SessionCompatibilityKey
    process: subprocess.Popen[bytes]
    connection: Any
    stdout_thread: threading.Thread
    session_config_path: Path


class PersistentSessionManager:
    def __init__(
        self,
        *,
        project_root: Path,
        config,
        status_manager,
        logger,
        configure_windows_error_mode: Callable[[], None],
        build_session_command: Callable[[str, str, Path], list[str]],
        expected_output_paths: Callable[[RenderBlockRequest], list[str]],
        wait_for_output_paths: Callable[..., bool],
        terminate_process_tree: Callable[[subprocess.Popen[bytes]], None],
        persist_render_device_preference: Callable[[str], None],
        run_direct_render_fallback: Callable[[QueuedRenderRequest, str], None],
        cancel_direct_render: Callable[..., dict[str, object]],
        update_progress_from_line: Callable[[str], None],
    ) -> None:
        self.project_root = project_root
        self.config = config
        self.status_manager = status_manager
        self.logger = logger
        self._configure_windows_error_mode = configure_windows_error_mode
        self._build_session_command = build_session_command
        self._expected_output_paths = expected_output_paths
        self._wait_for_output_paths = wait_for_output_paths
        self._terminate_process_tree = terminate_process_tree
        self._persist_render_device_preference = persist_render_device_preference
        self._run_direct_render_fallback = run_direct_render_fallback
        self._cancel_direct_render = cancel_direct_render
        self._update_progress_from_line = update_progress_from_line

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._queue: deque[QueuedRenderRequest] = deque()
        self._active_request: QueuedRenderRequest | None = None
        self._session_key: SessionCompatibilityKey | None = None
        self._session_process: SessionProcessHandle | None = None
        self._thread: threading.Thread | None = None
        self._shutdown_requested = False
        self._drain_requested = False
        self._cancel_requested = False
        self._cancelled_assignment_ids: list[str] = []
        self._active_job_log = None
        self._last_activity_monotonic = time.monotonic()
        self._fallback_mode = False
        self._persistent_disabled_until_monotonic = 0.0
        self._persistent_disabled_reason: str | None = None

    def has_logical_session(self) -> bool:
        with self._lock:
            return self._session_key is not None

    def persistent_mode_available(self) -> bool:
        if not bool(self.config.persistentSessionEnabled):
            return False
        with self._condition:
            return self._persistent_mode_available_locked()

    def _persistent_mode_available_locked(self) -> bool:
        if self._persistent_disabled_until_monotonic <= 0.0:
            return True
        if time.monotonic() < self._persistent_disabled_until_monotonic:
            return False
        self._persistent_disabled_until_monotonic = 0.0
        self._persistent_disabled_reason = None
        self.status_manager.set_persistent_session_availability(True)
        self.logger.info("Persistent Blender sessions re-enabled after cooldown")
        self.status_manager.append_log("Persistent Blender sessions re-enabled after cooldown")
        return True

    def _disable_persistent_mode_locked(self, reason: str) -> None:
        retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=PERSISTENT_SESSION_FAILURE_COOLDOWN_SECONDS)
        ).replace(microsecond=0).isoformat()
        self._persistent_disabled_until_monotonic = (
            time.monotonic() + PERSISTENT_SESSION_FAILURE_COOLDOWN_SECONDS
        )
        self._persistent_disabled_reason = reason
        self._fallback_mode = True
        self.status_manager.set_persistent_session_availability(
            False,
            reason=reason,
            retry_at=retry_at,
        )
        self.logger.warning(
            "Persistent Blender sessions temporarily disabled until %s: %s",
            retry_at,
            reason,
        )
        self.status_manager.append_log(
            f"Persistent Blender sessions temporarily disabled until {retry_at}: {reason}"
        )

    def queue_render(self, queued_request: QueuedRenderRequest) -> tuple[bool, str | None]:
        with self._condition:
            if self._shutdown_requested:
                return False, "worker_shutting_down"
            if not self._persistent_mode_available_locked():
                return False, "persistent_temporarily_disabled"

            self.status_manager.clear_error()
            if self._session_key is None:
                self._session_key = queued_request.compatibility_key
                self._drain_requested = False
                self.status_manager.open_persistent_session(
                    queued_request.status_payload,
                    state="queued",
                    queue_depth=0,
                    drain_requested=False,
                    queued_assignments=[],
                )
            elif self._session_key != queued_request.compatibility_key:
                return False, "active_session_incompatible"

            if len(self._queue) >= int(self.config.persistentSessionMaxQueueDepth):
                return False, "session_queue_full"

            self._queue.append(queued_request)
            self._last_activity_monotonic = time.monotonic()
            self.status_manager.update_persistent_session(
                state="queued" if self._active_request is not None or self._queue else "session_open",
                queue_depth=len(self._queue),
                drain_requested=self._drain_requested,
                queued_assignments=self._queued_assignments_locked(),
            )
            self._ensure_thread_locked()
            self._condition.notify_all()
            return True, None

    def cancel_session(
        self,
        *,
        requested_job_id: str | None = None,
        requested_assignment_id: str | None = None,
    ) -> dict[str, Any]:
        fallback_mode = False
        direct_render_active = False
        with self._condition:
            logical_job_id = self._session_key.job_id if self._session_key else None
            active_assignment_id = self._active_request.request.assignmentId if self._active_request else None
            if logical_job_id is None:
                return {
                    "cancelled": False,
                    "message": "no active render",
                }

            if requested_job_id and requested_job_id != logical_job_id:
                return {
                    "cancelled": False,
                    "message": "active render does not match requested jobId",
                    "currentJobId": logical_job_id,
                    "currentBlockId": self._active_request.request.blockId if self._active_request else None,
                    "assignmentId": active_assignment_id,
                }

            if requested_assignment_id and active_assignment_id and requested_assignment_id != active_assignment_id:
                return {
                    "cancelled": False,
                    "message": "active render does not match requested assignmentId",
                    "currentJobId": logical_job_id,
                    "currentBlockId": self._active_request.request.blockId if self._active_request else None,
                    "assignmentId": active_assignment_id,
                }

            queued_assignment_ids = [
                str(item.request.assignmentId)
                for item in self._queue
                if item.request.assignmentId
            ]
            cancelled_assignment_ids = [
                assignment_id
                for assignment_id in [active_assignment_id, *queued_assignment_ids]
                if assignment_id
            ]
            previous_block_id = self._active_request.request.blockId if self._active_request else None
            self._cancel_requested = True
            self._drain_requested = True
            self._cancelled_assignment_ids = list(cancelled_assignment_ids)
            self._queue.clear()
            if self._fallback_mode:
                if self._active_request is None:
                    self._reset_logical_session_locked(clear_error=True)
                    self._fallback_mode = False
                    self._cancel_requested = False
                    self._cancelled_assignment_ids = []
                    return {
                        "cancelled": True,
                        "cleanupPending": False,
                        "previousJobId": logical_job_id,
                        "previousBlockId": previous_block_id,
                        "assignmentId": active_assignment_id,
                        "terminatedSession": True,
                        "cancelledAssignmentIds": cancelled_assignment_ids,
                    }
                fallback_mode = True
                direct_render_active = self.status_manager.has_active_render(
                    job_id=logical_job_id,
                    block_id=self._active_request.request.blockId if self._active_request else None,
                )
            else:
                self.status_manager.update_persistent_session(
                    state="closing",
                    queue_depth=0,
                    drain_requested=True,
                    queued_assignments=[],
                )
            self.status_manager.mark_cancelling()
            process = self._session_process.process if self._session_process else None
            thread = self._thread
            self._condition.notify_all()

        if fallback_mode:
            cleanup_pending = False
            if direct_render_active:
                response = self._cancel_direct_render(
                    requested_job_id=requested_job_id,
                    requested_assignment_id=requested_assignment_id,
                )
                cleanup_pending = bool(response.get("cleanupPending"))
            if not cleanup_pending:
                with self._condition:
                    self._reset_logical_session_locked(clear_error=True)
                    self._fallback_mode = False
                    self._cancel_requested = False
                    self._cancelled_assignment_ids = []
                    self._condition.notify_all()
            return {
                "cancelled": True,
                "cleanupPending": cleanup_pending,
                "previousJobId": logical_job_id,
                "previousBlockId": previous_block_id,
                "assignmentId": active_assignment_id,
                "terminatedSession": True,
                "cancelledAssignmentIds": cancelled_assignment_ids,
            }

        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=SESSION_SHUTDOWN_TIMEOUT_SECONDS)

        cleanup_pending = thread is not None and thread.is_alive()
        return {
            "cancelled": True,
            "cleanupPending": cleanup_pending,
            "previousJobId": logical_job_id,
            "previousBlockId": previous_block_id,
            "assignmentId": active_assignment_id,
            "terminatedSession": True,
            "cancelledAssignmentIds": cancelled_assignment_ids,
        }

    def request_job_drain(self, *, job_id: str, coordinator_id: str | None = None) -> dict[str, Any]:
        del coordinator_id
        with self._condition:
            if self._session_key is None or self._session_key.job_id != job_id:
                return {
                    "accepted": False,
                    "jobId": job_id,
                    "sessionState": None,
                    "queueDepth": 0,
                    "drainRequested": False,
                    "message": "no matching persistent session",
                }
            self._drain_requested = True
            self.status_manager.update_persistent_session(
                state="draining" if self._active_request is None else "rendering",
                queue_depth=len(self._queue),
                drain_requested=True,
                queued_assignments=self._queued_assignments_locked(),
            )
            self._condition.notify_all()
            return {
                "accepted": True,
                "jobId": job_id,
                "sessionState": "draining",
                "queueDepth": len(self._queue),
                "drainRequested": True,
                "message": "persistent session marked for draining",
            }

    def shutdown(self) -> None:
        fallback_mode = False
        with self._condition:
            self._shutdown_requested = True
            self._drain_requested = True
            self._cancel_requested = True
            self._queue.clear()
            if self._fallback_mode:
                fallback_mode = True
            else:
                self.status_manager.update_persistent_session(
                    state="closing",
                    queue_depth=0,
                    drain_requested=True,
                    queued_assignments=[],
                )
            process = self._session_process.process if self._session_process else None
            thread = self._thread
            self._condition.notify_all()

        if fallback_mode:
            self._cancel_direct_render()
        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=SESSION_SHUTDOWN_TIMEOUT_SECONDS)

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name="persistent-blender-session",
            daemon=True,
        )
        self._thread.start()

    def _run_loop(self) -> None:
        while True:
            next_request: QueuedRenderRequest | None = None
            close_for_idle = False
            shutdown_requested = False

            with self._condition:
                while True:
                    if self._shutdown_requested:
                        shutdown_requested = True
                        break

                    if self._active_request is None and self._queue:
                        next_request = self._queue.popleft()
                        self._active_request = next_request
                        self.status_manager.update_persistent_session(
                            state="queued" if self._queue else "session_open",
                            queue_depth=len(self._queue),
                            drain_requested=self._drain_requested,
                            queued_assignments=self._queued_assignments_locked(),
                        )
                        break

                    if self._session_process is not None and self._active_request is None:
                        if self._drain_requested:
                            close_for_idle = True
                            break
                        idle_timeout_seconds = float(self.config.persistentSessionIdleTimeoutSeconds)
                        idle_elapsed = time.monotonic() - self._last_activity_monotonic
                        wait_seconds = max(0.0, idle_timeout_seconds - idle_elapsed)
                        if wait_seconds <= 0.0:
                            close_for_idle = True
                            break
                        self._condition.wait(timeout=wait_seconds)
                        continue

                    if self._active_request is None and not self._queue and self._session_process is None:
                        with self._lock:
                            self._thread = None
                        return

                    self._condition.wait(timeout=SESSION_ACK_POLL_INTERVAL_SECONDS)

            if shutdown_requested:
                self._close_session_process("worker shutdown", graceful=False)
                with self._condition:
                    self._reset_logical_session_locked(clear_error=False)
                    self._thread = None
                return

            if close_for_idle:
                reason = "job drain acknowledged" if self._drain_requested else "idle timeout"
                self.logger.info("Closing persistent Blender session: %s", reason)
                self.status_manager.append_log(f"Closing persistent Blender session: {reason}")
                self.status_manager.update_persistent_session(
                    state="closing",
                    queue_depth=0,
                    drain_requested=self._drain_requested,
                    queued_assignments=[],
                )
                self._close_session_process(reason, graceful=True)
                with self._condition:
                    self._reset_logical_session_locked(clear_error=True)
                    self._condition.notify_all()
                continue

            if next_request is None:
                continue

            self._process_request(next_request)

            with self._condition:
                if self._active_request is next_request:
                    self._active_request = None
                if self._fallback_mode and not self._queue and not self._cancel_requested:
                    self._reset_logical_session_locked(clear_error=False)
                elif self._session_key is not None:
                    next_state = "draining" if self._drain_requested else ("queued" if self._queue else "session_open")
                    self.status_manager.update_persistent_session(
                        state=next_state,
                        queue_depth=len(self._queue),
                        drain_requested=self._drain_requested,
                        queued_assignments=self._queued_assignments_locked(),
                    )
                self._condition.notify_all()

    def _process_request(self, queued_request: QueuedRenderRequest) -> None:
        start_time = time.monotonic()
        completion_recorded = False
        attempted_cpu_fallback = False
        session = None
        try:
            if self._fallback_mode:
                reason = self._persistent_disabled_reason or "Persistent Blender session is temporarily disabled"
                self._run_direct_render_fallback(queued_request, reason)
                completion_recorded = True
                return

            session = self._ensure_session_process(queued_request)
            if not self.status_manager.try_begin_render_block(queued_request.status_payload):
                raise RuntimeError("persistent session render slot is already reserved")

            self.status_manager.update_persistent_session(
                state="draining" if self._drain_requested else "rendering",
                queue_depth=len(self._queue),
                drain_requested=self._drain_requested,
                queued_assignments=self._queued_assignments_locked(),
            )
            self._last_activity_monotonic = time.monotonic()

            job_log_path = get_job_log_path(
                self.project_root,
                queued_request.request.jobId,
                queued_request.request.blockId,
            )
            output_paths = self._expected_output_paths(queued_request.request)
            output_baselines = _capture_output_fingerprints(output_paths)
            with job_log_path.open("a", encoding="utf-8", buffering=1) as job_log:
                self._active_job_log = job_log
                job_log.write("Mode: persistent-session\n")
                job_log.write(
                    "Blender session: "
                    f"selectedBlenderVersion={queued_request.selected_blender_version}; "
                    f"selectedBlenderPath={queued_request.selected_blender_path}; "
                    f"blendVersionDetected={queued_request.blend_version_detected}; "
                    f"renderDevicePreference={queued_request.render_device_preference}\n"
                )

                response = self._send_render_command(session, queued_request)
                elapsed_seconds = int(time.monotonic() - start_time)
                cancelled = self._cancel_requested
                output_verified = (
                    self._verify_output_paths(output_paths, output_baselines)
                    if response.get("ok") and not cancelled
                    else None
                )

                if not cancelled and not response.get("ok") and queued_request.render_device_preference == "GPU":
                    attempted_cpu_fallback = True
                    self.status_manager.update_active_render_metadata(
                        gpuRetryAttempted=True,
                    )
                    self.status_manager.add_active_render_recovery_action(
                        "Persistent GPU render failed, so the worker restarted the Blender session on CPU automatically."
                    )
                    self.logger.warning(
                        "Persistent GPU render failed job=%s block=%s; restarting session on CPU",
                        queued_request.request.jobId,
                        queued_request.request.blockId,
                    )
                    self.status_manager.append_log("Persistent GPU render failed; restarting session on CPU")
                    job_log.write("Persistent GPU render failed; restarting session on CPU\n")
                    self._restart_session_on_cpu(queued_request)
                    response = self._send_render_command(self._ensure_session_process(queued_request), queued_request)
                    elapsed_seconds = int(time.monotonic() - start_time)
                    cancelled = self._cancel_requested
                    output_verified = (
                        self._verify_output_paths(output_paths, output_baselines)
                        if response.get("ok") and not cancelled
                        else None
                    )

                if cancelled:
                    self.status_manager.finish_render_cancelled(
                        exit_code=-1,
                        elapsed_seconds=elapsed_seconds,
                        output_paths=output_paths,
                        output_verified=output_verified,
                        message="Render cancelled while using persistent Blender session",
                        failure_category="cancelled",
                        failure_code="render_cancelled",
                    )
                elif response.get("ok") and output_verified:
                    self.status_manager.update_render_progress(
                        queued_request.request.endFrame,
                        frame_completion=1.0,
                    )
                    message = "Render finished successfully in persistent Blender session"
                    if attempted_cpu_fallback:
                        message += " after retrying on CPU"
                        if self.config.persistCpuFallback:
                            self._persist_render_device_preference("CPU")
                    self.status_manager.finish_render_success(
                        exit_code=0,
                        elapsed_seconds=elapsed_seconds,
                        output_paths=output_paths,
                        output_verified=output_verified,
                        message=message,
                    )
                elif response.get("ok"):
                    self.status_manager.finish_render_error(
                        exit_code=0,
                        elapsed_seconds=elapsed_seconds,
                        error="Expected output files were not found after persistent render completion",
                        output_paths=output_paths,
                        output_verified=False,
                        message="Persistent render finished but the expected output file was not found",
                        failure_category="output",
                        failure_code="output_not_found",
                        failure_details="Expected output files were not found after persistent render completion",
                    )
                else:
                    response_error = str(
                        response.get("error") or "Persistent Blender session render failed"
                    )
                    self.status_manager.finish_render_error(
                        exit_code=-1,
                        elapsed_seconds=elapsed_seconds,
                        error=response_error,
                        output_paths=output_paths,
                        output_verified=output_verified,
                        message=str(response.get("message") or response.get("error") or "Persistent render failed"),
                        failure_category="persistent_session",
                        failure_code="persistent_render_failed",
                        failure_details=response_error,
                    )
                completion_recorded = True
        except Exception as exc:
            elapsed_seconds = int(time.monotonic() - start_time)
            self.logger.exception("Persistent Blender session failed")
            self._close_session_process(f"persistent session error: {exc}", graceful=False)
            if not self._cancel_requested and not self._shutdown_requested:
                with self._condition:
                    self._disable_persistent_mode_locked(str(exc))
                    self.status_manager.update_persistent_session(
                        state="queued" if self._queue else "session_open",
                        queue_depth=len(self._queue),
                        drain_requested=self._drain_requested,
                        queued_assignments=self._queued_assignments_locked(),
                    )
                    self._condition.notify_all()
                try:
                    self._run_direct_render_fallback(queued_request, str(exc))
                    completion_recorded = True
                    return
                except Exception as fallback_exc:
                    elapsed_seconds = int(time.monotonic() - start_time)
                    self.logger.exception(
                        "Direct render fallback after persistent session failure also failed"
                    )
                    exc = fallback_exc
            self.status_manager.finish_render_error(
                exit_code=-1,
                elapsed_seconds=elapsed_seconds,
                error=str(exc),
                output_paths=self._expected_output_paths(queued_request.request),
                output_verified=None,
                message=f"Persistent session error: {exc}",
                failure_category="persistent_session",
                failure_code="persistent_session_exception",
                failure_details=str(exc),
            )
            completion_recorded = True
            self.status_manager.update_persistent_session(
                state="error",
                queue_depth=len(self._queue),
                drain_requested=self._drain_requested,
                queued_assignments=self._queued_assignments_locked(),
            )
        finally:
            self._active_job_log = None
            self._last_activity_monotonic = time.monotonic()
            if not completion_recorded:
                elapsed_seconds = int(time.monotonic() - start_time)
                self.status_manager.finish_render_error(
                    exit_code=-1,
                    elapsed_seconds=elapsed_seconds,
                    error="Persistent render ended without final cleanup",
                    output_paths=self._expected_output_paths(queued_request.request),
                    output_verified=None,
                    message="Persistent render ended unexpectedly and state was reset",
                    failure_category="worker_runtime",
                    failure_code="final_state_cleanup_missing",
                    failure_details="Persistent render ended without final cleanup",
                )

            if self._cancel_requested:
                self._close_session_process("session cancelled", graceful=False)
                with self._condition:
                    self._reset_logical_session_locked(clear_error=True)
                    self._cancel_requested = False
                    self._cancelled_assignment_ids = []

    def _restart_session_on_cpu(self, active_request: QueuedRenderRequest) -> None:
        with self._condition:
            active_request.convert_to_cpu()
            for queued_request in self._queue:
                queued_request.convert_to_cpu()
            self._session_key = active_request.compatibility_key
            self.status_manager.update_persistent_session(
                state="queued",
                queue_depth=len(self._queue),
                drain_requested=self._drain_requested,
                queued_assignments=self._queued_assignments_locked(),
            )
        self._close_session_process("restart on CPU", graceful=False)

    def _ensure_session_process(self, queued_request: QueuedRenderRequest) -> SessionProcessHandle:
        with self._condition:
            if self._session_process is not None and self._session_process.process.poll() is None:
                return self._session_process

        auth_key = secrets.token_bytes(32)
        session_directory = Path(
            tempfile.mkdtemp(
                prefix="persistent_blender_session_",
                dir=str(self.project_root),
            )
        )
        session_config_path = session_directory / "session.json"
        pipe_address = rf"\\.\pipe\blender_render_farm_{uuid4().hex}"
        session_config = {
            "pipeAddress": pipe_address,
            "authKeyHex": auth_key.hex(),
            "jobId": queued_request.request.jobId,
            "blendFile": queued_request.request.blendFile,
            "outputPattern": queued_request.request.outputPattern,
            "format": queued_request.request.format,
            "renderDevicePreference": queued_request.render_device_preference,
            "gpuBackend": self.config.gpuBackend,
            "gpuDeviceIds": list(self.config.gpuDeviceIds),
            "gpuEnableAll": bool(self.config.gpuEnableAll),
            "gpuIncludeCpu": bool(self.config.gpuIncludeCpu),
        }
        session_config_path.write_text(json.dumps(session_config, indent=2), encoding="utf-8")

        listener = Listener(pipe_address, family="AF_PIPE", authkey=auth_key)
        process = None
        try:
            command = self._build_session_command(
                queued_request.selected_blender_path,
                queued_request.request.blendFile,
                session_config_path,
            )
            self._configure_windows_error_mode()
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=False,
                bufsize=0,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
            )
            stdout_thread = threading.Thread(
                target=self._stream_session_output,
                args=(process,),
                name=f"persistent-session-stdout-{queued_request.request.jobId}",
                daemon=True,
            )
            stdout_thread.start()

            connection = self._accept_connection(listener, timeout=float(self.config.persistentSessionOpenTimeoutSeconds))
            if not connection.poll(SESSION_READY_TIMEOUT_SECONDS):
                raise TimeoutError("Persistent Blender session did not send a ready handshake in time")
            ready_message = connection.recv()
            if not isinstance(ready_message, dict) or ready_message.get("type") != "ready":
                raise RuntimeError(f"Unexpected persistent session handshake: {ready_message!r}")

            handle = SessionProcessHandle(
                key=queued_request.compatibility_key,
                process=process,
                connection=connection,
                stdout_thread=stdout_thread,
                session_config_path=session_config_path,
            )
            with self._condition:
                self._session_process = handle
                self._last_activity_monotonic = time.monotonic()
                self.status_manager.update_persistent_session(
                    state="session_open",
                    queue_depth=len(self._queue),
                    drain_requested=self._drain_requested,
                    queued_assignments=self._queued_assignments_locked(),
                )
            self.logger.info(
                "Opened persistent Blender session job=%s blender=%s device=%s",
                queued_request.request.jobId,
                queued_request.selected_blender_path,
                queued_request.render_device_preference,
            )
            self.status_manager.append_log(
                f"Persistent Blender session opened for job {queued_request.request.jobId}"
            )
            return handle
        except Exception:
            if process is not None and process.poll() is None:
                self._terminate_process_tree(process)
            raise
        finally:
            try:
                listener.close()
            except Exception:
                pass

    def _verify_output_paths(
        self,
        output_paths: list[str],
        baselines: dict[str, tuple[int, int] | None],
    ) -> bool:
        try:
            return bool(self._wait_for_output_paths(output_paths, baselines=baselines))
        except TypeError:
            # Compatibility for injected test/legacy callbacks that still
            # accept only the path list.
            return bool(self._wait_for_output_paths(output_paths)) and _outputs_changed_since(
                output_paths, baselines
            )

    def _accept_connection(self, listener: Listener, *, timeout: float) -> Any:
        result: dict[str, Any] = {}

        def accept_connection() -> None:
            try:
                result["connection"] = listener.accept()
            except Exception as exc:  # pragma: no cover - listener errors are environment-specific
                result["error"] = exc

        thread = threading.Thread(target=accept_connection, name="persistent-session-listener", daemon=True)
        thread.start()
        thread.join(timeout=max(timeout, 0.1))
        if thread.is_alive():
            raise TimeoutError("Timed out waiting for the persistent Blender session to connect to the control pipe")
        if "error" in result:
            raise result["error"]
        return result["connection"]

    def _stream_session_output(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            return
        with process.stdout:
            for raw_line in process.stdout:
                line = decode_subprocess_output(raw_line).rstrip("\r\n")
                if not line:
                    continue
                self.status_manager.append_log(line)
                self._update_progress_from_line(line)
                self.logger.info("[PersistentBlender] %s", line)
                if self._active_job_log is not None:
                    try:
                        self._active_job_log.write(f"{line}\n")
                    except Exception:
                        pass

    def _send_render_command(
        self,
        session: SessionProcessHandle,
        queued_request: QueuedRenderRequest,
    ) -> dict[str, Any]:
        command = {
            "type": "render",
            "jobId": queued_request.request.jobId,
            "blockId": queued_request.request.blockId,
            "assignmentId": queued_request.request.assignmentId,
            "startFrame": queued_request.request.startFrame,
            "endFrame": queued_request.request.endFrame,
            "outputPattern": queued_request.request.outputPattern,
            "format": queued_request.request.format,
            "expectedOutputPaths": self._expected_output_paths(queued_request.request),
            "renderDevicePreference": queued_request.render_device_preference,
        }
        session.connection.send(command)
        while True:
            if session.connection.poll(SESSION_ACK_POLL_INTERVAL_SECONDS):
                response = session.connection.recv()
                if not isinstance(response, dict):
                    raise RuntimeError(f"Invalid persistent render response: {response!r}")
                return response
            if session.process.poll() is not None:
                raise RuntimeError("Persistent Blender session process exited while waiting for a render response")
            if self._cancel_requested:
                return {
                    "ok": False,
                    "message": "render cancelled",
                    "error": "render cancelled",
                }

    def _close_session_process(self, reason: str, *, graceful: bool) -> None:
        with self._condition:
            session = self._session_process
            self._session_process = None
        if session is None:
            return

        process = session.process
        connection = session.connection
        try:
            if graceful and process.poll() is None:
                try:
                    connection.send({"type": "shutdown", "reason": reason})
                    if connection.poll(SESSION_READY_TIMEOUT_SECONDS):
                        connection.recv()
                except Exception:
                    graceful = False
            if process.poll() is None:
                if graceful:
                    try:
                        process.wait(timeout=SESSION_SHUTDOWN_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        graceful = False
                if not graceful:
                    self._terminate_process_tree(process)
        finally:
            try:
                connection.close()
            except Exception:
                pass
            try:
                if session.session_config_path.exists():
                    session.session_config_path.unlink()
            except OSError:
                pass
            try:
                session.session_config_path.parent.rmdir()
            except OSError:
                pass

    def _queued_assignments_locked(self) -> list[dict[str, Any]]:
        return [item.assignment_summary() for item in self._queue]

    def _reset_logical_session_locked(self, *, clear_error: bool) -> None:
        self._queue.clear()
        self._active_request = None
        self._session_key = None
        self._fallback_mode = False
        self._drain_requested = False
        self.status_manager.close_persistent_session(clear_error=clear_error)
