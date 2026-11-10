from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Ensure local package imports work with both `pytest` and `python -m pytest`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.routes import create_app
from app.core.frame_scanner import FrameScanner
from app.core.settings import AppSettings
from app.services.worker_client import WorkerRequestError


class FakeWorkerClient:
    def __init__(self):
        self.scanner = FrameScanner()
        self.workers: dict[str, dict[str, Any]] = {}
        self.render_requests: list[dict[str, Any]] = []
        self.job_drain_requests: list[dict[str, Any]] = []

    def add_worker(
        self,
        base_url: str,
        *,
        worker_id: str | None,
        name: str,
        valid_paths: bool = True,
        auto_complete: bool = False,
        reported_blender_version: str = "4.2-mock",
        validate_paths_response: Any = None,
        render_device_preference: str = "GPU",
        effective_use_gpu: bool = True,
        persistent_session_enabled: bool = False,
        persistent_session_max_queue_depth: int | None = None,
    ) -> None:
        self.workers[base_url] = {
            "workerId": worker_id,
            "name": name,
            "online": True,
            "validPaths": valid_paths,
            "autoComplete": auto_complete,
            "status": "idle",
            "currentJobId": None,
            "currentBlockId": None,
            "currentFrame": None,
            "currentAssignmentId": None,
            "progress": 0.0,
            "lastJob": None,
            "error": None,
            "blenderVersion": reported_blender_version,
            "gpu": "RTX Mock",
            "renderDevicePreference": render_device_preference,
            "effectiveUseGpu": effective_use_gpu,
            "validatePathsResponse": validate_paths_response,
            "statusFailuresRemaining": 0,
            "rejectBusyOnce": False,
            "persistentSessionEnabled": persistent_session_enabled,
            "persistentSessionMaxQueueDepth": persistent_session_max_queue_depth,
            "sessionState": None,
            "sessionQueueDepth": 0,
            "sessionDrainRequested": False,
            "queuedAssignments": [],
            "activePayload": None,
        }

    def set_online(self, base_url: str, online: bool) -> None:
        self.workers[base_url]["online"] = online
        if not online:
            self.workers[base_url]["status"] = "offline"

    def set_valid_paths(self, base_url: str, valid: bool) -> None:
        self.workers[base_url]["validPaths"] = valid

    def complete_current_frame(self, base_url: str, payload: dict[str, Any]) -> None:
        frame_path = Path(
            self.scanner.resolve_output_path(
                {"outputPattern": payload["outputPattern"], "format": payload.get("format", "PNG")},
                int(payload["startFrame"]),
            )
        )
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        frame_path.write_text("rendered", encoding="utf-8")
        worker = self.workers[base_url]
        drain_requested = bool(worker.get("sessionDrainRequested"))
        worker["status"] = "idle"
        worker["currentJobId"] = None
        worker["currentBlockId"] = None
        worker["currentFrame"] = None
        worker["currentAssignmentId"] = None
        worker["progress"] = 100.0
        worker["error"] = None
        worker["lastJob"] = {
            "assignmentId": payload.get("assignmentId"),
            "jobId": payload["jobId"],
            "blockId": payload["blockId"],
            "startFrame": payload["startFrame"],
            "endFrame": payload["endFrame"],
            "result": "done",
            "outputVerified": True,
            "outputPaths": [str(frame_path)],
        }
        worker["activePayload"] = None
        if worker.get("persistentSessionEnabled"):
            queued_assignments = list(worker.get("queuedAssignments") or [])
            if queued_assignments:
                next_payload = dict(queued_assignments.pop(0))
                worker["queuedAssignments"] = queued_assignments
                worker["status"] = "draining" if drain_requested else "rendering"
                worker["sessionState"] = "draining" if drain_requested else ("queued" if queued_assignments else "rendering")
                worker["sessionQueueDepth"] = len(queued_assignments)
                worker["currentJobId"] = next_payload["jobId"]
                worker["currentBlockId"] = next_payload["blockId"]
                worker["currentFrame"] = next_payload["startFrame"]
                worker["currentAssignmentId"] = next_payload.get("assignmentId")
                worker["activePayload"] = next_payload
                worker["progress"] = 0.0
            else:
                if drain_requested:
                    worker["status"] = "idle"
                    worker["sessionState"] = None
                    worker["sessionQueueDepth"] = 0
                    worker["sessionDrainRequested"] = False
                    worker["currentJobId"] = None
                else:
                    worker["status"] = "session_open"
                    worker["sessionState"] = "session_open"
                    worker["sessionQueueDepth"] = 0
                    worker["sessionDrainRequested"] = False
                    worker["currentJobId"] = payload["jobId"]
        else:
            worker["sessionState"] = None
            worker["sessionQueueDepth"] = 0
            worker["sessionDrainRequested"] = False
            worker["queuedAssignments"] = []

    def health(self, base_url: str) -> dict[str, Any]:
        worker = self._worker(base_url)
        return {"status": "ok", "workerId": worker["workerId"], "name": worker["name"]}

    def status(self, base_url: str) -> dict[str, Any]:
        worker = self._worker(base_url)
        if int(worker.get("statusFailuresRemaining") or 0) > 0:
            worker["statusFailuresRemaining"] = int(worker["statusFailuresRemaining"]) - 1
            raise RuntimeError(f"Transient status failure for {base_url}")
        return {
            "status": worker["status"],
            "workerId": worker["workerId"],
            "name": worker["name"],
            "blenderFound": True,
            "blenderVersion": worker["blenderVersion"],
            "gpu": worker["gpu"],
            "renderDevicePreference": worker["renderDevicePreference"],
            "effectiveUseGpu": worker["effectiveUseGpu"],
            "currentJobId": worker["currentJobId"],
            "currentBlockId": worker["currentBlockId"],
            "currentFrame": worker["currentFrame"],
            "assignmentId": worker["currentAssignmentId"],
            "currentAssignmentId": worker["currentAssignmentId"],
            "progress": worker["progress"],
            "lastJob": worker["lastJob"],
            "error": worker.get("error"),
            "persistentSessionEnabled": worker.get("persistentSessionEnabled"),
            "sessionState": worker.get("sessionState"),
            "sessionQueueDepth": worker.get("sessionQueueDepth"),
            "sessionDrainRequested": worker.get("sessionDrainRequested"),
            "queuedAssignments": worker.get("queuedAssignments"),
        }

    def capabilities(self, base_url: str) -> dict[str, Any]:
        worker = self._worker(base_url)
        return {
            "blenderFound": True,
            "blenderVersion": worker["blenderVersion"],
            "gpu": worker["gpu"],
            "renderDevicePreference": worker["renderDevicePreference"],
        }

    def validate_paths(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(base_url)
        valid = worker["validPaths"]
        response = {
            "valid": valid,
            "blendAccessible": valid,
            "outputAccessible": valid,
            "blendFile": payload["blendFile"],
            "outputFolder": payload["outputFolder"],
        }
        overrides = worker.get("validatePathsResponse")
        if callable(overrides):
            overrides = overrides(payload, worker)
        if isinstance(overrides, dict):
            response.update(overrides)
        return response

    def render_block(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(base_url)
        if worker.get("rejectBusyOnce"):
            worker["rejectBusyOnce"] = False
            worker["status"] = "busy"
            worker["currentJobId"] = worker.get("currentJobId") or "job_busy"
            worker["currentBlockId"] = worker.get("currentBlockId") or "frame_busy"
            worker["currentFrame"] = worker.get("currentFrame") if worker.get("currentFrame") is not None else -1
            worker["currentAssignmentId"] = worker.get("currentAssignmentId") or "lease_busy"
            raise WorkerRequestError(
                409,
                "Worker ja esta renderizando.",
                response_json={
                    "accepted": False,
                    "status": "busy",
                    "currentJobId": worker.get("currentJobId"),
                    "currentBlockId": worker.get("currentBlockId"),
                    "assignmentId": worker.get("currentAssignmentId"),
                    "message": "Worker ja esta renderizando.",
                },
            )
        self.render_requests.append({"baseUrl": base_url, **dict(payload)})
        if (
            worker.get("persistentSessionEnabled")
            and worker.get("currentJobId") == payload["jobId"]
            and worker.get("status") in {"rendering", "queued", "session_open", "draining"}
        ):
            if worker.get("currentFrame") is not None:
                queued_payload = dict(payload)
                queued_assignments = list(worker.get("queuedAssignments") or [])
                max_queue_depth = worker.get("persistentSessionMaxQueueDepth")
                if max_queue_depth is not None and len(queued_assignments) >= int(max_queue_depth):
                    raise WorkerRequestError(
                        409,
                        "Worker ja esta renderizando.",
                        response_json={
                            "accepted": False,
                            "ok": False,
                            "status": "rendering",
                            "error": "session_queue_full",
                            "message": "Worker ja esta renderizando.",
                            "jobId": payload["jobId"],
                            "currentJobId": worker.get("currentJobId"),
                            "currentBlockId": worker.get("currentBlockId"),
                            "assignmentId": worker.get("currentAssignmentId"),
                            "currentAssignmentId": worker.get("currentAssignmentId"),
                            "currentFrame": worker.get("currentFrame"),
                            "queueDepth": len(queued_assignments),
                            "sessionState": "queued",
                            "drainRequested": worker.get("sessionDrainRequested"),
                            "persistentSessionEnabled": True,
                        },
                    )
                queued_assignments.append(queued_payload)
                worker["queuedAssignments"] = queued_assignments
                worker["status"] = "queued"
                worker["sessionState"] = "queued"
                worker["sessionQueueDepth"] = len(queued_assignments)
                return {
                    "accepted": True,
                    "status": "queued",
                    "jobId": payload["jobId"],
                    "currentJobId": worker.get("currentJobId"),
                    "currentBlockId": worker.get("currentBlockId"),
                    "currentAssignmentId": worker.get("currentAssignmentId"),
                    "currentFrame": worker.get("currentFrame"),
                    "queueDepth": len(queued_assignments),
                    "sessionState": "queued",
                    "drainRequested": worker.get("sessionDrainRequested"),
                    "persistentSessionEnabled": True,
                }
        worker["status"] = "rendering"
        worker["currentJobId"] = payload["jobId"]
        worker["currentBlockId"] = payload["blockId"]
        worker["currentFrame"] = payload["startFrame"]
        worker["currentAssignmentId"] = payload.get("assignmentId")
        worker["progress"] = 0.0
        worker["error"] = None
        worker["activePayload"] = dict(payload)
        if worker.get("persistentSessionEnabled"):
            worker["sessionState"] = "rendering"
            worker["sessionQueueDepth"] = len(worker.get("queuedAssignments") or [])
        if worker["autoComplete"]:
            self.complete_current_frame(base_url, payload)
        return {
            "accepted": True,
            "status": "rendering",
            "jobId": payload["jobId"],
            "blockId": payload["blockId"],
            "currentJobId": worker.get("currentJobId"),
            "currentBlockId": worker.get("currentBlockId"),
            "assignmentId": payload.get("assignmentId"),
            "currentAssignmentId": worker.get("currentAssignmentId"),
            "currentFrame": worker.get("currentFrame"),
            "queueDepth": len(worker.get("queuedAssignments") or []),
            "sessionState": worker.get("sessionState"),
            "drainRequested": worker.get("sessionDrainRequested"),
            "persistentSessionEnabled": worker.get("persistentSessionEnabled"),
        }

    def cancel(self, base_url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        worker = self._worker(base_url)
        assignment_id = worker.get("currentAssignmentId")
        current_job_id = worker.get("currentJobId")
        current_block_id = worker.get("currentBlockId")
        current_frame = worker.get("currentFrame")
        cancelled_assignment_ids = [
            value
            for value in [
                assignment_id,
                *[
                    item.get("assignmentId")
                    for item in list(worker.get("queuedAssignments") or [])
                    if isinstance(item, dict)
                ],
            ]
            if value
        ]
        worker["status"] = "idle"
        worker["currentJobId"] = None
        worker["currentBlockId"] = None
        worker["currentFrame"] = None
        worker["currentAssignmentId"] = None
        worker["progress"] = 0.0
        worker["error"] = None
        worker["sessionState"] = None
        worker["sessionQueueDepth"] = 0
        worker["sessionDrainRequested"] = False
        worker["queuedAssignments"] = []
        worker["activePayload"] = None
        if assignment_id or current_job_id:
            worker["lastJob"] = {
                "assignmentId": assignment_id,
                "jobId": current_job_id,
                "blockId": current_block_id,
                "startFrame": current_frame,
                "endFrame": current_frame,
                "result": "cancelled",
                "outputVerified": False,
                "outputPaths": [],
            }
        return {
            "accepted": True,
            "terminatedSession": bool(worker.get("persistentSessionEnabled")),
            "cancelledAssignmentIds": cancelled_assignment_ids,
        }

    def job_drain(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(base_url)
        self.job_drain_requests.append({"baseUrl": base_url, **dict(payload)})
        if not worker.get("persistentSessionEnabled") or worker.get("currentJobId") != payload.get("jobId"):
            return {
                "accepted": False,
                "jobId": payload.get("jobId"),
                "sessionState": worker.get("sessionState"),
                "queueDepth": worker.get("sessionQueueDepth"),
                "drainRequested": False,
            }
        worker["sessionDrainRequested"] = True
        if worker.get("currentFrame") is None and not worker.get("queuedAssignments"):
            worker["status"] = "idle"
            worker["currentJobId"] = None
            worker["sessionState"] = None
            worker["sessionQueueDepth"] = 0
            return {
                "accepted": True,
                "jobId": payload.get("jobId"),
                "sessionState": "draining",
                "queueDepth": 0,
                "drainRequested": True,
            }
        worker["status"] = "draining"
        worker["sessionState"] = "draining"
        return {
            "accepted": True,
            "jobId": payload.get("jobId"),
            "sessionState": "draining",
            "queueDepth": len(worker.get("queuedAssignments") or []),
            "drainRequested": True,
        }

    def logs(self, base_url: str) -> str:
        self._worker(base_url)
        return ""

    def get_render_device(self, base_url: str) -> dict[str, Any]:
        worker = self._worker(base_url)
        return {"device": worker["renderDevicePreference"]}

    def set_render_device(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        worker = self._worker(base_url)
        worker["renderDevicePreference"] = payload.get("device", worker["renderDevicePreference"])
        return {"accepted": True}

    def _worker(self, base_url: str) -> dict[str, Any]:
        worker = self.workers[base_url]
        if not worker["online"]:
            raise RuntimeError(f"Worker at {base_url} is offline.")
        return worker


@pytest.fixture()
def coordinator_app(tmp_path: Path):
    project_root = tmp_path / "coordinator_project"
    settings = AppSettings(
        project_root=project_root,
        coordinator_id="coord_test_01",
        host="127.0.0.1",
        port=8010,
        database_path=project_root / "app" / "data" / "coordinator.db",
        logs_dir=project_root / "app" / "data" / "logs",
        general_log_name="coordinator.log",
        scheduler_interval_seconds=0.1,
        lease_duration_seconds=1,
        worker_request_timeout_seconds=0.1,
        worker_status_grace_seconds=5,
        max_frame_attempts=3,
        discovery_timeout_seconds=0.1,
        discovery_ports=(9000,),
        coordinator_discovery_enabled=False,
        coordinator_discovery_port=18011,
        coordinator_discovery_namespace="blender_render_farm_test",
        coordinator_discovery_broadcast_interval_seconds=0.25,
        coordinator_discovery_probe_timeout_seconds=0.1,
        log_tail_lines=50,
        auto_start_scheduler=False,
        auto_unblock_blocked_jobs=True,
    )
    fake_worker_client = FakeWorkerClient()
    app = create_app(settings=settings, worker_client=fake_worker_client)

    with TestClient(app) as client:
        yield app, client, fake_worker_client, tmp_path
