from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import WorkerConfig
from .capabilities import detect_local_ip
from .runtime_metadata import load_runtime_metadata
from renderfarm_shared.i18n import normalize_language


def detect_runtime_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class WorkerStatusManager:
    def __init__(self, config: WorkerConfig, runtime_root=None) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._logs: deque[str] = deque(maxlen=500)
        runtime_path = runtime_root if runtime_root is not None else None
        if runtime_path is None:
            runtime_path = detect_runtime_root()
        self.runtime_metadata = load_runtime_metadata(runtime_path)

        self.machine_name = config.resolved_worker_name
        bind_host = str(config.host or "").strip()
        self.ip = detect_local_ip() if bind_host in {"", "0.0.0.0", "::"} else bind_host
        self.port = config.port
        self.worker_id = self._make_worker_id(self.ip, self.port)
        self.allow_jobs = config.allowJobs
        self.status = "starting"
        self._base_status = "starting"
        self._cancelling = False
        self.startup_error: str | None = None
        self.blender_found = False
        self.blender_path = ""
        self.blender_version = ""
        self.gpu = "unknown"
        self.render_device_preference = config.renderDevicePreference
        self.ui_language = normalize_language(config.language)
        self.api_server_state = "starting"
        self.api_server_message = "Waiting for API startup"
        self.coordinator_discovery_enabled = config.coordinatorDiscoveryEnabled
        self.coordinator_discovery_status = (
            "discovering" if config.coordinatorDiscoveryEnabled else "disabled"
        )
        self.coordinator_connected = False
        self.coordinator_id: str | None = None
        self.coordinator_api_url: str | None = None
        self.coordinator_hostname: str | None = None
        self.coordinator_last_seen_at: str | None = None
        self.coordinator_last_registered_at: str | None = None
        self.coordinator_error: str | None = None
        self.current_job: dict[str, Any] | None = None
        self.session_info: dict[str, Any] | None = None
        self.last_job: dict[str, Any] | None = None
        self.last_log = ""
        self.error: str | None = None
        self.persistent_session_available = bool(config.persistentSessionEnabled)
        self.persistent_session_disabled_reason: str | None = None
        self.persistent_session_retry_at: str | None = None
        self._render_completion_callback: Callable[[dict[str, object]], None] | None = None

    def set_ready(self) -> None:
        with self._lock:
            self._base_status = "disabled" if not self.allow_jobs else "idle"
            self._refresh_status_locked()

    def set_ui_language(self, language: str | None) -> str:
        normalized = normalize_language(language)
        with self._lock:
            self.ui_language = normalized
            self.config.language = normalized
        return normalized

    def mark_startup_ready(self, message: str | None = None) -> None:
        with self._lock:
            previous_startup_error = self.startup_error
            self._base_status = "disabled" if not self.allow_jobs else "idle"
            self.startup_error = None
            self.api_server_state = "running"
            self.api_server_message = message or "API server is ready"
            if previous_startup_error and self.error == previous_startup_error:
                self.error = None
            self._refresh_status_locked()

    def mark_startup_error(self, message: str) -> None:
        cleaned = message.strip() or "Worker startup failed"
        with self._lock:
            self.startup_error = cleaned
            self._base_status = "error"
            self.api_server_state = "error"
            self.api_server_message = cleaned
            self.error = cleaned
            self.status = "error"
            self.last_log = cleaned
            self._logs.append(cleaned)

    def mark_api_server_starting(self, message: str | None = None) -> None:
        with self._lock:
            self.api_server_state = "starting"
            self.api_server_message = message or "Waiting for API startup"

    def mark_api_server_running(self, message: str | None = None) -> None:
        with self._lock:
            previous_startup_error = self.startup_error
            self.api_server_state = "running"
            self.api_server_message = message or "API server is ready"
            if previous_startup_error and self.error == previous_startup_error:
                self.error = None
            self.startup_error = None

    def mark_api_server_stopped(self, message: str | None = None) -> None:
        cleaned = (message or "API server stopped").strip()
        with self._lock:
            self.api_server_state = "stopped"
            self.api_server_message = cleaned
            if self.current_job is None and self._base_status not in {"error", "starting"}:
                self._base_status = "error"
            if self.current_job is None and self.session_info is None:
                self.error = cleaned
            self._refresh_status_locked()
            self.last_log = cleaned
            self._logs.append(cleaned)

    def start_validation(self) -> str | None:
        with self._lock:
            if self._base_status in {"idle", "disabled"} and self.current_job is None and self.session_info is None:
                previous_status = self._base_status
                self._base_status = "validating"
                self._refresh_status_locked()
                return previous_status
        return None

    def finish_validation(self, previous_status: str | None) -> None:
        if previous_status is None:
            return
        with self._lock:
            if self._base_status == "validating":
                self._base_status = previous_status
                self._refresh_status_locked()

    def set_blender_info(self, found: bool, path: str, version: str) -> None:
        with self._lock:
            self.blender_found = found
            self.blender_path = path
            self.blender_version = version

    def set_gpu_name(self, gpu_name: str | None) -> None:
        cleaned = str(gpu_name or "").strip()
        with self._lock:
            self.gpu = cleaned or "unknown"

    def set_render_device_preference(self, render_device_preference: str) -> None:
        with self._lock:
            self.render_device_preference = render_device_preference

    def override_active_render_device(
        self,
        render_device_preference: str,
        *,
        use_gpu: bool,
        selected_blender_version: str | None = None,
        selected_blender_path: str | None = None,
    ) -> None:
        with self._lock:
            if not self.current_job:
                return
            self.current_job["renderDevicePreference"] = render_device_preference
            self.current_job["useGpu"] = use_gpu
            if selected_blender_version is not None:
                self.current_job["selectedBlenderVersion"] = selected_blender_version
            if selected_blender_path is not None:
                self.current_job["selectedBlenderPath"] = selected_blender_path

    def set_coordinator_disabled(self) -> None:
        with self._lock:
            self.coordinator_discovery_enabled = False
            self.coordinator_discovery_status = "disabled"
            self.coordinator_connected = False
            self.coordinator_id = None
            self.coordinator_api_url = None
            self.coordinator_hostname = None
            self.coordinator_last_seen_at = None
            self.coordinator_last_registered_at = None
            self.coordinator_error = None

    def set_coordinator_discovering(self, message: str | None = None) -> None:
        with self._lock:
            self.coordinator_discovery_enabled = True
            self.coordinator_discovery_status = "discovering"
            self.coordinator_connected = False
            self.coordinator_id = None
            self.coordinator_api_url = None
            self.coordinator_hostname = None
            self.coordinator_last_seen_at = None
            self.coordinator_last_registered_at = None
            self.coordinator_error = message

    def set_coordinator_connecting(
        self,
        *,
        coordinator_id: str,
        api_url: str,
        hostname: str,
        last_seen_at: str,
    ) -> None:
        with self._lock:
            self.coordinator_discovery_enabled = True
            self.coordinator_discovery_status = "connecting"
            self.coordinator_connected = False
            self.coordinator_id = coordinator_id
            self.coordinator_api_url = api_url
            self.coordinator_hostname = hostname
            self.coordinator_last_seen_at = last_seen_at
            self.coordinator_error = None

    def set_coordinator_registered(
        self,
        *,
        coordinator_id: str,
        api_url: str,
        hostname: str,
        last_seen_at: str,
        last_registered_at: str,
    ) -> None:
        with self._lock:
            self.coordinator_discovery_enabled = True
            self.coordinator_discovery_status = "registered"
            self.coordinator_connected = True
            self.coordinator_id = coordinator_id
            self.coordinator_api_url = api_url
            self.coordinator_hostname = hostname
            self.coordinator_last_seen_at = last_seen_at
            self.coordinator_last_registered_at = last_registered_at
            self.coordinator_error = None

    def set_coordinator_seen(
        self,
        *,
        coordinator_id: str,
        api_url: str,
        hostname: str,
        last_seen_at: str,
    ) -> None:
        with self._lock:
            self.coordinator_discovery_enabled = True
            self.coordinator_id = coordinator_id
            self.coordinator_api_url = api_url
            self.coordinator_hostname = hostname
            self.coordinator_last_seen_at = last_seen_at
            if self.coordinator_connected:
                self.coordinator_discovery_status = "registered"
            else:
                self.coordinator_discovery_status = "connecting"

    def set_coordinator_error(self, message: str) -> None:
        with self._lock:
            self.coordinator_discovery_enabled = True
            self.coordinator_discovery_status = "error"
            self.coordinator_connected = False
            self.coordinator_error = message

    def set_render_completion_callback(
        self,
        callback: Callable[[dict[str, object]], None] | None,
    ) -> None:
        with self._lock:
            self._render_completion_callback = callback

    def get_worker_registration_payload(self) -> dict[str, object]:
        with self._lock:
            return {
                "workerId": self.worker_id,
                "ip": self.ip,
                "port": self.port,
                "name": self.machine_name,
                "enabled": self.allow_jobs,
                "renderDevicePreference": self.render_device_preference,
                "language": self.ui_language,
                "apiUrl": f"http://{self.ip}:{self.port}",
            }

    def append_log(self, line: str) -> None:
        cleaned = line.rstrip()
        if not cleaned:
            return
        with self._lock:
            self.last_log = cleaned
            self._logs.append(cleaned)

    def set_error(self, message: str | None, *, status: str | None = None) -> None:
        with self._lock:
            self.error = message
            if status is not None:
                self._base_status = status
            self._refresh_status_locked()

    def clear_error(self) -> None:
        with self._lock:
            self.error = None
            self._refresh_status_locked()

    def update_active_render_metadata(self, **changes: Any) -> None:
        with self._lock:
            if not self.current_job:
                return
            for key, value in changes.items():
                if value is None:
                    continue
                if key == "recoveryActions":
                    existing = list(self.current_job.get("recoveryActions") or [])
                    raw_values = value if isinstance(value, list) else [value]
                    for item in raw_values:
                        cleaned = str(item or "").strip()
                        if cleaned and cleaned not in existing:
                            existing.append(cleaned)
                    self.current_job["recoveryActions"] = existing
                    continue
                self.current_job[key] = value

    def add_active_render_recovery_action(self, action: str) -> None:
        cleaned = str(action or "").strip()
        if not cleaned:
            return
        self.update_active_render_metadata(recoveryActions=[cleaned])

    def update_last_job_delivery(
        self,
        status: str,
        *,
        detail: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if not self.last_job:
                return
            report: dict[str, Any] = {
                "status": str(status or "unknown").strip() or "unknown",
                "reportedAt": utc_now_iso(),
            }
            cleaned_detail = str(detail or "").strip()
            if cleaned_detail:
                report["detail"] = cleaned_detail
            if isinstance(response, dict) and response:
                report["response"] = dict(response)
            self.last_job["coordinatorReport"] = report

    def set_persistent_session_availability(
        self,
        available: bool,
        *,
        reason: str | None = None,
        retry_at: str | None = None,
    ) -> None:
        with self._lock:
            self.persistent_session_available = bool(available)
            if available:
                self.persistent_session_disabled_reason = None
                self.persistent_session_retry_at = None
            else:
                self.persistent_session_disabled_reason = (reason or "").strip() or None
                self.persistent_session_retry_at = retry_at
            self._refresh_status_locked()

    def open_persistent_session(
        self,
        payload: dict[str, Any],
        *,
        state: str = "session_open",
        queue_depth: int = 0,
        drain_requested: bool = False,
        queued_assignments: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._lock:
            opened_at = utc_now_iso()
            self.session_info = {
                "jobId": payload["jobId"],
                "blendFile": payload["blendFile"],
                "blendVersionDetected": payload.get("blendVersionDetected"),
                "requiredBlenderVersion": payload.get("requiredBlenderVersion"),
                "preferredBlenderMajor": payload.get("preferredBlenderMajor"),
                "versionRoutingPolicy": payload.get("versionRoutingPolicy"),
                "outputPattern": payload["outputPattern"],
                "format": payload["format"],
                "useGpu": payload["useGpu"],
                "requestedUseGpu": payload.get("requestedUseGpu"),
                "renderDevicePreference": payload["renderDevicePreference"],
                "selectedBlenderVersion": payload.get("selectedBlenderVersion"),
                "selectedBlenderPath": payload.get("selectedBlenderPath"),
                "state": state,
                "queueDepth": max(0, int(queue_depth)),
                "drainRequested": bool(drain_requested),
                "queuedAssignments": list(queued_assignments or []),
                "openedAt": opened_at,
                "lastActivityAt": opened_at,
            }
            self._refresh_status_locked()

    def update_persistent_session(
        self,
        *,
        state: str | None = None,
        queue_depth: int | None = None,
        drain_requested: bool | None = None,
        queued_assignments: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._lock:
            if self.session_info is None:
                return
            if state is not None:
                self.session_info["state"] = state
            if queue_depth is not None:
                self.session_info["queueDepth"] = max(0, int(queue_depth))
            if drain_requested is not None:
                self.session_info["drainRequested"] = bool(drain_requested)
            if queued_assignments is not None:
                self.session_info["queuedAssignments"] = list(queued_assignments)
            self.session_info["lastActivityAt"] = utc_now_iso()
            self._refresh_status_locked()

    def close_persistent_session(self, *, clear_error: bool = False) -> None:
        with self._lock:
            self.session_info = None
            if clear_error:
                self.error = None
            self._refresh_status_locked()

    def get_persistent_session_summary(self) -> dict[str, Any]:
        with self._lock:
            if not self.session_info:
                return {
                    "active": False,
                    "jobId": None,
                    "state": None,
                    "queueDepth": 0,
                    "drainRequested": False,
                    "queuedAssignments": [],
                }
            return {
                "active": True,
                "jobId": self.session_info.get("jobId"),
                "state": self.session_info.get("state"),
                "queueDepth": int(self.session_info.get("queueDepth") or 0),
                "drainRequested": bool(self.session_info.get("drainRequested")),
                "queuedAssignments": list(self.session_info.get("queuedAssignments") or []),
            }

    def try_begin_render_block(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            if self.current_job is not None:
                return False
            self.error = None
            self._cancelling = False
            self.current_job = self._build_current_job_payload(payload)
            log_parts = [
                f"Render started for {payload['jobId']}/{payload['blockId']} "
                f"frames {payload['startFrame']}-{payload['endFrame']}"
            ]
            if payload.get("assignmentId"):
                log_parts.append(f"assignmentId={payload['assignmentId']}")
            if payload.get("coordinatorId"):
                log_parts.append(f"coordinatorId={payload['coordinatorId']}")
            if payload.get("submittedByMasterName") or payload.get("submittedByMasterId"):
                submitted_by = payload.get("submittedByMasterName") or payload.get(
                    "submittedByMasterId"
                )
                log_parts.append(f"submittedBy={submitted_by}")
            self.last_log = " | ".join(log_parts)
            self._logs.append(self.last_log)
            self._refresh_status_locked()
            return True

    def begin_render_block(self, payload: dict[str, Any]) -> None:
        if not self.try_begin_render_block(payload):
            raise RuntimeError("render slot is already reserved")

    def update_render_progress(
        self,
        frame: int,
        *,
        frame_completion: float | None = None,
    ) -> None:
        with self._lock:
            if not self.current_job:
                return

            start_frame = int(self.current_job["startFrame"])
            end_frame = int(self.current_job["endFrame"])
            if frame < start_frame or frame > end_frame:
                return

            total_frames = end_frame - start_frame + 1
            current_frame = int(self.current_job.get("currentFrame") or start_frame)
            if frame < current_frame:
                return

            completed_frames = int(self.current_job.get("completedFrames") or 0)
            current_frame_completion = float(
                self.current_job.get("currentFrameCompletion") or 0.0
            )

            if frame > current_frame:
                completed_frames = max(completed_frames, frame - start_frame)
                current_frame = frame
                current_frame_completion = 0.0

            if frame_completion is not None:
                clamped_completion = max(0.0, min(float(frame_completion), 1.0))
                current_frame_completion = max(current_frame_completion, clamped_completion)

            total_completed = min(total_frames, completed_frames + current_frame_completion)
            progress = (total_completed / total_frames) * 100
            self.current_job["completedFrames"] = completed_frames
            self.current_job["currentFrame"] = current_frame
            self.current_job["currentFrameCompletion"] = round(current_frame_completion, 4)
            self.current_job["progress"] = round(max(0.0, min(progress, 100.0)), 2)

    def get_current_frame_number(self) -> int | None:
        with self._lock:
            if not self.current_job:
                return None
            current_frame = self.current_job.get("currentFrame")
            if current_frame is None:
                return None
            return int(current_frame)

    def get_current_job_elapsed_seconds(self) -> float | None:
        with self._lock:
            if not self.current_job:
                return None
            started_at_monotonic = self.current_job.get("startedAtMonotonic")
            if started_at_monotonic is None:
                return None
            return max(0.0, time.monotonic() - float(started_at_monotonic))

    def mark_cancelling(self) -> None:
        with self._lock:
            if self.current_job:
                self._cancelling = True
                self.last_log = (
                    f"Cancelling render {self.current_job['jobId']}/{self.current_job['blockId']}"
                )
                self._logs.append(self.last_log)
                self._refresh_status_locked()

    def finish_render_success(
        self,
        *,
        exit_code: int,
        elapsed_seconds: int,
        output_paths: list[str] | None = None,
        output_verified: bool | None = None,
        message: str | None = None,
    ) -> None:
        self._finish_render(
            result="done",
            exit_code=exit_code,
            elapsed_seconds=elapsed_seconds,
            error=None,
            output_paths=output_paths,
            output_verified=output_verified,
            message=message or f"Render finished successfully with exitCode={exit_code}",
        )

    def finish_render_error(
        self,
        *,
        exit_code: int,
        elapsed_seconds: int,
        error: str,
        output_paths: list[str] | None = None,
        output_verified: bool | None = None,
        message: str | None = None,
        failure_category: str | None = None,
        failure_code: str | None = None,
        failure_details: str | None = None,
    ) -> None:
        self._finish_render(
            result="error",
            exit_code=exit_code,
            elapsed_seconds=elapsed_seconds,
            error=error,
            output_paths=output_paths,
            output_verified=output_verified,
            message=message or f"Render failed: {error}",
            failure_category=failure_category,
            failure_code=failure_code,
            failure_details=failure_details,
        )

    def finish_render_cancelled(
        self,
        *,
        exit_code: int,
        elapsed_seconds: int,
        output_paths: list[str] | None = None,
        output_verified: bool | None = None,
        message: str | None = None,
        failure_category: str | None = None,
        failure_code: str | None = None,
        failure_details: str | None = None,
    ) -> None:
        self._finish_render(
            result="cancelled",
            exit_code=exit_code,
            elapsed_seconds=elapsed_seconds,
            error=None,
            output_paths=output_paths,
            output_verified=output_verified,
            message=message or "Render cancelled",
            failure_category=failure_category,
            failure_code=failure_code,
            failure_details=failure_details,
        )

    def has_active_render(
        self,
        *,
        job_id: str | None = None,
        block_id: str | None = None,
    ) -> bool:
        with self._lock:
            if not self.current_job:
                return False
            if job_id is not None and self.current_job["jobId"] != job_id:
                return False
            if block_id is not None and self.current_job["blockId"] != block_id:
                return False
            return True

    def is_busy(self) -> bool:
        with self._lock:
            return self.current_job is not None

    def get_active_render_summary(self) -> dict[str, object]:
        with self._lock:
            if not self.current_job:
                return {
                    "jobId": None,
                    "blockId": None,
                    "assignmentId": None,
                    "coordinatorId": None,
                    "submittedByMasterId": None,
                    "submittedByMasterName": None,
                }

            return {
                "jobId": self.current_job["jobId"],
                "blockId": self.current_job["blockId"],
                "assignmentId": self.current_job.get("assignmentId"),
                "coordinatorId": self.current_job.get("coordinatorId"),
                "submittedByMasterId": self.current_job.get("submittedByMasterId"),
                "submittedByMasterName": self.current_job.get("submittedByMasterName"),
            }

    def get_current_identifiers(self) -> tuple[str | None, str | None]:
        with self._lock:
            if not self.current_job:
                return None, None
            return self.current_job["jobId"], self.current_job["blockId"]

    def get_logs(self, limit: int = 200) -> list[str]:
        with self._lock:
            return list(self._logs)[-limit:]

    def health_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "ok": self.status != "error" and self.api_server_state != "error",
                "workerId": self.worker_id,
                "machineName": self.machine_name,
                "status": self.status,
                "startupError": self.startup_error,
                "apiServerState": self.api_server_state,
                "apiServerMessage": self.api_server_message,
                "appVersion": self.runtime_metadata["appVersion"],
                "protocolVersion": self.runtime_metadata["protocolVersion"],
                "buildId": self.runtime_metadata["buildId"],
                "buildTimestampUtc": self.runtime_metadata["buildTimestampUtc"],
            }

    def status_snapshot(self) -> dict[str, object]:
        with self._lock:
            session_info = dict(self.session_info) if self.session_info else None
            current_or_session_job_id = None
            if self.current_job:
                current_or_session_job_id = self.current_job["jobId"]
            elif session_info:
                current_or_session_job_id = session_info.get("jobId")
            current_block_id = self.current_job["blockId"] if self.current_job else None
            session_state = str(session_info.get("state") or "") if session_info else None
            session_queue_depth = int(session_info.get("queueDepth") or 0) if session_info else 0
            session_drain_requested = bool(session_info.get("drainRequested")) if session_info else False
            queued_assignments = list(session_info.get("queuedAssignments") or []) if session_info else []
            persistent_session_effective_enabled = bool(
                self.config.persistentSessionEnabled and self.persistent_session_available
            )
            payload: dict[str, object] = {
                "workerId": self.worker_id,
                "machineName": self.machine_name,
                "ip": self.ip,
                "port": self.port,
                "status": self.status,
                "startupError": self.startup_error,
                "apiServerState": self.api_server_state,
                "apiServerMessage": self.api_server_message,
                "appVersion": self.runtime_metadata["appVersion"],
                "protocolVersion": self.runtime_metadata["protocolVersion"],
                "buildId": self.runtime_metadata["buildId"],
                "buildTimestampUtc": self.runtime_metadata["buildTimestampUtc"],
                "allowJobs": self.allow_jobs,
                "blenderFound": self.blender_found,
                "blenderPath": self.blender_path,
                "blenderVersion": self.blender_version,
                "gpu": self.gpu,
                "renderDevicePreference": self.render_device_preference,
                "language": self.ui_language,
                "effectiveUseGpu": self.render_device_preference == "GPU",
                "gpuBackend": self.config.gpuBackend,
                "gpuDeviceIds": list(self.config.gpuDeviceIds),
                "gpuEnableAll": bool(self.config.gpuEnableAll),
                "gpuIncludeCpu": bool(self.config.gpuIncludeCpu),
                "coordinatorDiscoveryEnabled": self.coordinator_discovery_enabled,
                "coordinatorDiscoveryStatus": self.coordinator_discovery_status,
                "coordinatorConnected": self.coordinator_connected,
                "coordinatorId": self.coordinator_id,
                "registeredCoordinatorId": self.coordinator_id,
                "coordinatorApiUrl": self.coordinator_api_url,
                "coordinatorHostname": self.coordinator_hostname,
                "coordinatorLastSeenAt": self.coordinator_last_seen_at,
                "coordinatorLastRegisteredAt": self.coordinator_last_registered_at,
                "coordinatorError": self.coordinator_error,
                "lastLog": self.last_log,
                "error": self.error,
                "persistentSessionConfigured": bool(self.config.persistentSessionEnabled),
                "persistentSessionEnabled": persistent_session_effective_enabled,
                "persistentSessionTemporarilyDisabled": bool(
                    self.config.persistentSessionEnabled and not self.persistent_session_available
                ),
                "persistentSessionDisabledReason": self.persistent_session_disabled_reason,
                "persistentSessionRetryAt": self.persistent_session_retry_at,
                "sessionState": session_state,
                "sessionJobId": current_or_session_job_id,
                "sessionQueueDepth": session_queue_depth,
                "sessionDrainRequested": session_drain_requested,
                "queuedAssignments": queued_assignments,
                "currentJobId": current_or_session_job_id,
                "currentBlockId": current_block_id,
            }

            if self.current_job:
                elapsed_seconds = int(time.monotonic() - self.current_job["startedAtMonotonic"])
                payload.update(
                    {
                        "currentJob": current_or_session_job_id,
                        "currentBlock": current_block_id,
                        "jobId": current_or_session_job_id,
                        "blockId": current_block_id,
                        "blendFile": self.current_job["blendFile"],
                        "blendVersionDetected": self.current_job.get("blendVersionDetected"),
                        "requiredBlenderVersion": self.current_job.get("requiredBlenderVersion"),
                        "preferredBlenderMajor": self.current_job.get("preferredBlenderMajor"),
                        "versionRoutingPolicy": self.current_job.get("versionRoutingPolicy"),
                        "outputPattern": self.current_job["outputPattern"],
                        "startFrame": self.current_job["startFrame"],
                        "endFrame": self.current_job["endFrame"],
                        "currentFrame": self.current_job["currentFrame"],
                        "progress": self.current_job["progress"],
                        "elapsedSeconds": elapsed_seconds,
                        "format": self.current_job["format"],
                        "useGpu": self.current_job["useGpu"],
                        "requestedUseGpu": self.current_job["requestedUseGpu"],
                        "renderDevicePreference": self.current_job["renderDevicePreference"],
                        "effectiveUseGpu": self.current_job["useGpu"],
                        "selectedBlenderVersion": self.current_job.get("selectedBlenderVersion"),
                        "selectedBlenderPath": self.current_job.get("selectedBlenderPath"),
                        "assignmentId": self.current_job.get("assignmentId"),
                        "currentAssignmentId": self.current_job.get("assignmentId"),
                        "coordinatorId": self.current_job.get("coordinatorId")
                        or self.coordinator_id,
                        "jobCoordinatorId": self.current_job.get("coordinatorId"),
                        "submittedByMasterId": self.current_job.get("submittedByMasterId"),
                        "submittedByMasterName": self.current_job.get("submittedByMasterName"),
                    }
                )
            else:
                session_blend_file = session_info.get("blendFile") if session_info else None
                payload.update(
                    {
                        "currentJob": current_or_session_job_id,
                        "currentBlock": None,
                        "jobId": current_or_session_job_id,
                        "blockId": None,
                        "blendFile": session_blend_file,
                        "blendVersionDetected": session_info.get("blendVersionDetected") if session_info else None,
                        "requiredBlenderVersion": session_info.get("requiredBlenderVersion") if session_info else None,
                        "preferredBlenderMajor": session_info.get("preferredBlenderMajor") if session_info else None,
                        "versionRoutingPolicy": session_info.get("versionRoutingPolicy") if session_info else None,
                        "outputPattern": session_info.get("outputPattern") if session_info else None,
                        "startFrame": None,
                        "endFrame": None,
                        "currentFrame": None,
                        "progress": 0.0,
                        "elapsedSeconds": 0,
                        "format": session_info.get("format") if session_info else None,
                        "useGpu": session_info.get("useGpu") if session_info else None,
                        "requestedUseGpu": session_info.get("requestedUseGpu") if session_info else None,
                        "selectedBlenderVersion": session_info.get("selectedBlenderVersion") if session_info else None,
                        "selectedBlenderPath": session_info.get("selectedBlenderPath") if session_info else None,
                        "assignmentId": None,
                        "currentAssignmentId": None,
                        "jobCoordinatorId": None,
                        "submittedByMasterId": None,
                        "submittedByMasterName": None,
                    }
                )
                if self.last_job:
                    payload["lastJob"] = dict(self.last_job)
            return payload

    def build_render_report_payload(self) -> dict[str, object]:
        with self._lock:
            return self._build_render_report_payload_locked()

    def _finish_render(
        self,
        *,
        result: str,
        exit_code: int,
        elapsed_seconds: int,
        error: str | None,
        output_paths: list[str] | None,
        output_verified: bool | None,
        message: str,
        failure_category: str | None = None,
        failure_code: str | None = None,
        failure_details: str | None = None,
    ) -> None:
        callback = None
        callback_payload = None
        with self._lock:
            current_job = self.current_job
            if current_job:
                if result == "done":
                    current_job["completedFrames"] = (
                        int(current_job["endFrame"]) - int(current_job["startFrame"]) + 1
                    )
                    current_job["currentFrame"] = current_job["endFrame"]
                    current_job["currentFrameCompletion"] = 1.0
                    current_job["progress"] = 100.0
                self.last_job = {
                    "jobId": current_job["jobId"],
                    "blockId": current_job["blockId"],
                    "startFrame": current_job["startFrame"],
                    "endFrame": current_job["endFrame"],
                    "blendVersionDetected": current_job.get("blendVersionDetected"),
                    "requiredBlenderVersion": current_job.get("requiredBlenderVersion"),
                    "preferredBlenderMajor": current_job.get("preferredBlenderMajor"),
                    "versionRoutingPolicy": current_job.get("versionRoutingPolicy"),
                    "result": result,
                    "exitCode": exit_code,
                    "elapsedSeconds": elapsed_seconds,
                    "useGpu": current_job["useGpu"],
                    "requestedUseGpu": current_job["requestedUseGpu"],
                    "renderDevicePreference": current_job["renderDevicePreference"],
                    "selectedBlenderVersion": current_job.get("selectedBlenderVersion"),
                    "selectedBlenderPath": current_job.get("selectedBlenderPath"),
                    "assignmentId": current_job.get("assignmentId"),
                    "coordinatorId": current_job.get("coordinatorId"),
                    "submittedByMasterId": current_job.get("submittedByMasterId"),
                    "submittedByMasterName": current_job.get("submittedByMasterName"),
                    "error": error,
                    "message": message,
                    "finishedAt": utc_now_iso(),
                    "outputPaths": list(output_paths or []),
                    "outputVerified": output_verified,
                    "executionMode": current_job.get("executionMode"),
                    "recoveryActions": list(current_job.get("recoveryActions") or []),
                    "failureCategory": failure_category or current_job.get("failureCategory"),
                    "failureCode": failure_code or current_job.get("failureCode"),
                    "failureDetails": failure_details or current_job.get("failureDetails"),
                    "gpuRetryAttempted": bool(current_job.get("gpuRetryAttempted")),
                    "gpuFailureExitCode": current_job.get("gpuFailureExitCode"),
                    "persistentSessionFallbackReason": current_job.get("persistentSessionFallbackReason"),
                    "persistentSessionTemporarilyDisabled": bool(
                        self.config.persistentSessionEnabled and not self.persistent_session_available
                    ),
                    "persistentSessionDisabledReason": self.persistent_session_disabled_reason,
                    "persistentSessionRetryAt": self.persistent_session_retry_at,
                }
                callback = self._render_completion_callback

            self.current_job = None
            self._cancelling = False
            self.error = error
            self.last_log = message
            self._logs.append(message)
            self._refresh_status_locked()
            if callback and self.last_job:
                callback_payload = self._build_render_report_payload_locked(last_job=self.last_job)
        if callback and callback_payload:
            try:
                callback(dict(callback_payload))
            except Exception as exc:
                self.update_last_job_delivery(
                    "failed",
                    detail=f"Render completion callback crashed: {exc}",
                )

    def _refresh_status_locked(self) -> None:
        if self.current_job is not None:
            if self._cancelling:
                self.status = "cancelling"
                return
            if self.session_info and bool(self.session_info.get("drainRequested")):
                self.status = "draining"
                return
            self.status = "rendering"
            return

        if self.session_info is not None:
            session_state = str(self.session_info.get("state") or "session_open").strip().lower()
            if session_state in {"queued", "session_open", "draining", "closing", "error"}:
                self.status = session_state
                return
            self.status = "session_open"
            return

        if self._base_status in {"starting", "validating", "error", "disabled"}:
            self.status = self._base_status
            return

        self.status = "disabled" if not self.allow_jobs else "idle"

    @staticmethod
    def _build_current_job_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "jobId": payload["jobId"],
            "blockId": payload["blockId"],
            "blendFile": payload["blendFile"],
            "blendVersionDetected": payload.get("blendVersionDetected"),
            "requiredBlenderVersion": payload.get("requiredBlenderVersion"),
            "preferredBlenderMajor": payload.get("preferredBlenderMajor"),
            "versionRoutingPolicy": payload.get("versionRoutingPolicy"),
            "outputPattern": payload["outputPattern"],
            "startFrame": payload["startFrame"],
            "endFrame": payload["endFrame"],
            "format": payload["format"],
            "useGpu": payload["useGpu"],
            "requestedUseGpu": payload["requestedUseGpu"],
            "renderDevicePreference": payload["renderDevicePreference"],
            "selectedBlenderVersion": payload.get("selectedBlenderVersion"),
            "selectedBlenderPath": payload.get("selectedBlenderPath"),
            "assignmentId": payload.get("assignmentId"),
            "coordinatorId": payload.get("coordinatorId"),
            "submittedByMasterId": payload.get("submittedByMasterId"),
            "submittedByMasterName": payload.get("submittedByMasterName"),
            "executionMode": payload.get("executionMode") or "direct",
            "recoveryActions": list(payload.get("recoveryActions") or []),
            "failureCategory": payload.get("failureCategory"),
            "failureCode": payload.get("failureCode"),
            "failureDetails": payload.get("failureDetails"),
            "gpuRetryAttempted": bool(payload.get("gpuRetryAttempted")),
            "gpuFailureExitCode": payload.get("gpuFailureExitCode"),
            "persistentSessionFallbackReason": payload.get("persistentSessionFallbackReason"),
            "currentFrame": payload["startFrame"],
            "completedFrames": 0,
            "currentFrameCompletion": 0.0,
            "progress": 0.0,
            "startedAtMonotonic": time.monotonic(),
        }

    def _build_render_report_payload_locked(
        self,
        *,
        last_job: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        session_info = dict(self.session_info) if self.session_info else None
        current_or_session_job_id = None
        if self.current_job:
            current_or_session_job_id = self.current_job["jobId"]
        elif session_info:
            current_or_session_job_id = session_info.get("jobId")
        current_block_id = self.current_job["blockId"] if self.current_job else None
        current_frame = self.current_job["currentFrame"] if self.current_job else None
        current_assignment_id = self.current_job.get("assignmentId") if self.current_job else None
        progress = float(self.current_job.get("progress") or 0.0) if self.current_job else 0.0
        session_state = str(session_info.get("state") or "") if session_info else None
        session_queue_depth = int(session_info.get("queueDepth") or 0) if session_info else 0
        session_drain_requested = bool(session_info.get("drainRequested")) if session_info else False
        queued_assignments = list(session_info.get("queuedAssignments") or []) if session_info else []
        persistent_session_effective_enabled = bool(
            self.config.persistentSessionEnabled and self.persistent_session_available
        )
        return {
            "workerId": self.worker_id,
            "machineName": self.machine_name,
            "ip": self.ip,
            "port": self.port,
            "status": self.status,
            "currentJobId": current_or_session_job_id,
            "currentBlockId": current_block_id,
            "currentFrame": current_frame,
            "currentAssignmentId": current_assignment_id,
            "progress": progress,
            "lastLog": self.last_log,
            "error": self.error,
            "coordinatorId": self.coordinator_id,
            "coordinatorApiUrl": self.coordinator_api_url,
            "persistentSessionEnabled": persistent_session_effective_enabled,
            "persistentSessionTemporarilyDisabled": bool(
                self.config.persistentSessionEnabled and not self.persistent_session_available
            ),
            "persistentSessionDisabledReason": self.persistent_session_disabled_reason,
            "persistentSessionRetryAt": self.persistent_session_retry_at,
            "sessionState": session_state,
            "sessionQueueDepth": session_queue_depth,
            "sessionDrainRequested": session_drain_requested,
            "queuedAssignments": queued_assignments,
            "reportedAt": utc_now_iso(),
            "lastJob": dict(last_job or self.last_job or {}),
        }

    @staticmethod
    def _make_worker_id(host: str, port: int) -> str:
        sanitized_host = str(host or "127.0.0.1").replace(".", "_").replace(":", "_")
        return f"worker_{sanitized_host}_{int(port)}"
