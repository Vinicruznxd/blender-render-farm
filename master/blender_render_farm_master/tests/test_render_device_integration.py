from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import json
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import zstandard
except ImportError:  # pragma: no cover - dependency installed in normal test runs
    zstandard = None

from PySide6.QtWidgets import QApplication, QProgressBar

from app.core.blend_version import read_blend_version_metadata
from app.core.coordinator_manager import CoordinatorManager
from app.core.job_manager import JobManager, JobValidationError, WorkerPathValidationError
from app.core.models import RenderBlock, RenderJob
from app.core.settings import AppSettings
from app.services.logger import AppLogger
from app.services.coordinator_discovery import CoordinatorAnnouncement, CoordinatorDiscoveryService
from app.services.persistence import PersistenceService
from app.services.coordinator_client import CoordinatorClient, CoordinatorClientError
from app.services.worker_client import WorkerClient
from app.ui.main_window import MainWindow
from main import build_runtime_manager
from mock_worker import MockWorkerHandler, MockWorkerState


class MockWorkerServer:
    def __init__(
        self,
        render_device_preference: str,
        *,
        name: str = "TEST-WORKER",
        seconds_per_frame: float = 0.4,
    ) -> None:
        self.state = MockWorkerState(
            name=name,
            host="127.0.0.1",
            port=0,
            blender_version="4.3",
            gpu="Test GPU",
            seconds_per_frame=seconds_per_frame,
            render_device_preference=render_device_preference,
        )

        class Handler(MockWorkerHandler):
            pass

        Handler.state = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.state.port = int(self.server.server_address[1])
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.state.cancel()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class LegacyWorkerHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/status":
            return self._send_json(
                {
                    "machineName": "LEGACY-WORKER",
                    "ip": "127.0.0.1",
                    "port": self.server.server_address[1],
                    "status": "idle",
                    "blenderFound": True,
                    "blenderVersion": "4.2",
                    "gpu": "Legacy GPU",
                    "currentJob": None,
                    "currentBlock": None,
                    "lastLog": "",
                    "error": None,
                }
            )
        if self.path == "/capabilities":
            return self._send_json(
                {
                    "machineName": "LEGACY-WORKER",
                    "blenderFound": True,
                    "blenderVersion": "4.2",
                    "gpu": "Legacy GPU",
                }
            )
        if self.path == "/health":
            return self._send_json({"ok": True, "machineName": "LEGACY-WORKER", "status": "idle"})
        if self.path == "/logs":
            return self._send_json({"logs": []})
        self._send_json({"ok": False}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/validate-paths":
            return self._send_json({"ok": True, "blendAccessible": True, "outputAccessible": True})
        if self.path == "/render-block":
            return self._send_json({"ok": True, "accepted": True})
        if self.path == "/cancel":
            return self._send_json({"ok": True})
        self._send_json({"ok": False}, status=404)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SimpleValidationWorkerHandler(BaseHTTPRequestHandler):
    worker_name = "VALIDATION-WORKER"
    validation_payload = {
        "blendFileExists": True,
        "outputFolderExists": True,
        "canWriteOutput": True,
        "errors": [],
    }

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/status":
            return self._send_json(
                {
                    "machineName": self.worker_name,
                    "ip": "127.0.0.1",
                    "port": self.server.server_address[1],
                    "status": "idle",
                    "blenderFound": True,
                    "blenderVersion": "4.3",
                    "gpu": "Validation GPU",
                    "currentJob": None,
                    "currentBlock": None,
                    "lastLog": "",
                    "error": None,
                }
            )
        if self.path == "/capabilities":
            return self._send_json(
                {
                    "machineName": self.worker_name,
                    "blenderFound": True,
                    "blenderVersion": "4.3",
                    "gpu": "Validation GPU",
                }
            )
        if self.path == "/health":
            return self._send_json({"ok": True, "machineName": self.worker_name, "status": "idle"})
        if self.path == "/logs":
            return self._send_json({"logs": []})
        self._send_json({"ok": False}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        if self.path == "/validate-paths":
            return self._send_json(self.validation_payload)
        if self.path == "/render-block":
            return self._send_json({"ok": True, "accepted": True})
        if self.path == "/cancel":
            return self._send_json({"ok": True})
        self._send_json({"ok": False}, status=404)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SuccessfulValidationHandler(SimpleValidationWorkerHandler):
    worker_name = "OK-WORKER"
    validation_payload = {
        "blendFileExists": True,
        "outputFolderExists": True,
        "canWriteOutput": True,
        "errors": [],
    }


class RejectingValidationHandler(SimpleValidationWorkerHandler):
    worker_name = "REJECT-WORKER"
    validation_payload = {
        "blendFileExists": False,
        "outputFolderExists": True,
        "canWriteOutput": False,
        "errors": [
            "Blend file not found: X:\\Projetos\\Cena01\\cena.blend",
            "Cannot write to output folder: X:\\Projetos\\Cena01\\renders",
        ],
    }


class MockCoordinatorState:
    def __init__(self) -> None:
        self.heartbeats: list[dict] = []
        self.jobs: list[dict] = []
        self.queue: list[dict] = []
        self.active_job: dict | None = None
        self.workers: list[dict] = [
            {
                "id": "coord-worker-1",
                "name": "COORD-WORKER-01",
                "ip": "127.0.0.1",
                "port": 9000,
                "enabled": True,
                "status": "idle",
                "blenderFound": True,
                "blenderVersion": "4.3",
                "gpu": "Coord GPU",
                "lastLog": "",
            }
        ]

    def create_job(self, payload: dict) -> dict:
        job_id = f"job_{len(self.jobs) + 1:03d}"
        job = {
            "jobId": job_id,
            "jobName": payload.get("jobName", job_id),
            "blendFile": payload.get("blendFile"),
            "outputFolder": payload.get("outputFolder"),
            "startFrame": int(payload.get("startFrame", 1)),
            "endFrame": int(payload.get("endFrame", 1)),
            "blockSize": int(payload.get("blockSize", 1)),
            "status": "queued",
            "queuePosition": len(self.queue) + (0 if self.active_job else 1),
            "createdAt": "2026-05-25T12:00:00",
            "submittedByMasterId": payload.get("submittedByMasterId"),
            "submittedByMasterName": payload.get("submittedByMasterName"),
            "submittedByUser": payload.get("submittedByUser"),
            "overallPercent": 0.0,
            "completedFramesCount": 0,
            "totalFrames": max(0, int(payload.get("endFrame", 1)) - int(payload.get("startFrame", 1)) + 1),
            "autoAddWorkers": payload.get("autoAddWorkers", True),
            "redistributeOfflineBlocks": payload.get("redistributeOfflineBlocks", True),
            "ignoreRenderedFrames": payload.get("ignoreRenderedFrames", True),
            "requiredBlenderVersion": payload.get("requiredBlenderVersion"),
            "preferredBlenderMajor": payload.get("preferredBlenderMajor"),
            "versionRoutingPolicy": payload.get("versionRoutingPolicy"),
        }
        self.jobs.append(job)
        self.queue.append(job)
        return {"ok": True, "jobId": job_id}

    def pause_job(self, job_id: str) -> dict:
        if self.active_job and self.active_job.get("jobId") == job_id:
            self.active_job["status"] = "paused"
        for job in self.queue:
            if job.get("jobId") == job_id:
                job["status"] = "paused"
        return {"ok": True}

    def resume_job(self, job_id: str) -> dict:
        if self.active_job and self.active_job.get("jobId") == job_id:
            self.active_job["status"] = "running"
        for job in self.queue:
            if job.get("jobId") == job_id:
                job["status"] = "queued"
        return {"ok": True}

    def cancel_job(self, job_id: str) -> dict:
        if self.active_job and self.active_job.get("jobId") == job_id:
            self.active_job["status"] = "cancelled"
        self.queue = [job for job in self.queue if job.get("jobId") != job_id]
        self.jobs = [
            {**job, "status": "cancelled"} if job.get("jobId") == job_id else job
            for job in self.jobs
        ]
        return {"ok": True}

    def add_worker(self, payload: dict) -> dict:
        worker = {
            "id": f"coord-worker-{len(self.workers) + 1}",
            "name": payload.get("name"),
            "ip": payload.get("ip"),
            "port": int(payload.get("port", 9000)),
            "enabled": bool(payload.get("enabled", True)),
            "status": "idle",
            "blenderFound": True,
            "blenderVersion": "4.3",
            "gpu": "Coord GPU",
            "lastLog": "",
        }
        self.workers.append(worker)
        return worker

    def discover_workers(self) -> dict:
        existing = next((item for item in self.workers if item["id"] == "coord-worker-2"), None)
        if existing is None:
            self.workers.append(
                {
                    "id": "coord-worker-2",
                    "name": "DISCOVERED-WORKER",
                    "ip": "127.0.0.1",
                    "port": 9001,
                    "enabled": True,
                    "status": "idle",
                    "blenderFound": True,
                    "blenderVersion": "4.3",
                    "gpu": "Coord GPU 2",
                    "lastLog": "",
                }
            )
            added_count = 1
        else:
            added_count = 0
        return {
            "discoveredCount": 1,
            "addedCount": added_count,
            "updatedCount": 0,
            "scannedCount": 2,
            "workers": [
                {
                    "name": "DISCOVERED-WORKER",
                    "ip": "127.0.0.1",
                    "port": 9001,
                    "endpoint": "127.0.0.1:9001",
                }
            ],
        }

    def test_worker(self, worker_id: str) -> dict:
        worker = next(item for item in self.workers if item["id"] == worker_id)
        return {
            "status": {
                "machineName": worker["name"],
                "status": worker["status"],
                "blenderFound": True,
                "renderDevicePreference": "GPU",
                "effectiveUseGpu": True,
            },
            "validate": {
                "blendFileExists": True,
                "outputFolderExists": True,
                "canWriteOutput": True,
                "errors": [],
            },
        }

    def state_payload(self) -> dict:
        active_job = self.active_job
        if active_job is None and self.queue:
            active_job = None
        return {
            "coordinator": {
                "coordinatorId": "coord-local-01",
                "status": "running",
                "startedAt": "2026-05-25T12:00:00",
            },
            "masters": list(self.heartbeats),
            "workers": self.workers,
            "activeJob": active_job,
            "queue": self.queue,
            "jobs": self.jobs,
            "metrics": {
                "totalQueuedJobs": len(self.queue),
                "machinesOnline": sum(1 for item in self.workers if item.get("status") != "offline"),
                "machinesRendering": sum(1 for item in self.workers if item.get("status") == "rendering"),
            },
            "logs": {
                "general": "Coordinator mock online",
                "workers": {
                    item["id"]: item.get("lastLog", "")
                    for item in self.workers
                },
            },
        }


class MockCoordinatorHandler(BaseHTTPRequestHandler):
    state = MockCoordinatorState()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            return self._send_json({"ok": True, "status": "running"})
        if self.path == "/state":
            return self._send_json(self.state.state_payload())
        if self.path == "/jobs":
            return self._send_json({"jobs": self.state.jobs})
        if self.path == "/workers":
            return self._send_json({"workers": self.state.workers})
        if self.path == "/masters":
            return self._send_json({"masters": self.state.heartbeats})
        if self.path == "/logs":
            return self._send_json(self.state.state_payload()["logs"])
        self._send_json({"ok": False}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json()
        if self.path == "/masters/heartbeat":
            self.state.heartbeats.append(payload)
            return self._send_json({"ok": True})
        if self.path == "/jobs":
            return self._send_json(self.state.create_job(payload), status=201)
        if self.path.endswith("/pause"):
            return self._send_json(self.state.pause_job(self.path.split("/")[-2]))
        if self.path.endswith("/resume"):
            return self._send_json(self.state.resume_job(self.path.split("/")[-2]))
        if self.path.endswith("/cancel"):
            return self._send_json(self.state.cancel_job(self.path.split("/")[-2]))
        if self.path == "/workers":
            return self._send_json(self.state.add_worker(payload), status=201)
        if self.path == "/workers/discover":
            return self._send_json(self.state.discover_workers())
        if self.path.endswith("/test"):
            return self._send_json(self.state.test_worker(self.path.split("/")[-2]))
        self._send_json({"ok": False}, status=404)

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path.startswith("/workers/"):
            worker_id = self.path.split("/")[-1]
            self.state.workers = [item for item in self.state.workers if item["id"] != worker_id]
            return self._send_json({"ok": True})
        self._send_json({"ok": False}, status=404)

    def log_message(self, format: str, *args) -> None:
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LegacyStrictCoordinatorHandler(MockCoordinatorHandler):
    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json()
        if self.path == "/jobs" and any(
            key in payload for key in ("requiredBlenderVersion", "preferredBlenderMajor", "versionRoutingPolicy")
        ):
            return self._send_json({"ok": False, "message": "Unknown field in payload"}, status=400)
        if self.path == "/masters/heartbeat":
            self.state.heartbeats.append(payload)
            return self._send_json({"ok": True})
        if self.path == "/jobs":
            return self._send_json(self.state.create_job(payload), status=201)
        if self.path.endswith("/pause"):
            return self._send_json(self.state.pause_job(self.path.split("/")[-2]))
        if self.path.endswith("/resume"):
            return self._send_json(self.state.resume_job(self.path.split("/")[-2]))
        if self.path.endswith("/cancel"):
            return self._send_json(self.state.cancel_job(self.path.split("/")[-2]))
        if self.path == "/workers":
            return self._send_json(self.state.add_worker(payload), status=201)
        if self.path == "/workers/discover":
            return self._send_json(self.state.discover_workers())
        if self.path.endswith("/test"):
            return self._send_json(self.state.test_worker(self.path.split("/")[-2]))
        self._send_json({"ok": False}, status=404)


class StrictIdentityCoordinatorHandler(MockCoordinatorHandler):
    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json()
        if self.path == "/jobs":
            for field in ("submittedByMasterId", "submittedByMasterName", "submittedByUser"):
                if not payload.get(field):
                    return self._send_json({"detail": f"Field '{field}' is required."}, status=400)
            return self._send_json(self.state.create_job(payload), status=201)
        return super().do_POST()


class AlwaysRejectJobCoordinatorHandler(MockCoordinatorHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/jobs":
            return self._send_json({"detail": "Field 'submittedByMasterId' is required."}, status=400)
        return super().do_POST()


class StaticCoordinatorDiscoveryService:
    def __init__(self, announcements: list[CoordinatorAnnouncement]) -> None:
        self.announcements = announcements
        self.probe_calls = 0
        self.listen_port = 8011
        self.udp_port = 8011
        self.listen_error = ""

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def recent_announcements(self) -> list[CoordinatorAnnouncement]:
        return list(self.announcements)

    def probe(self, *, force: bool = False, wait_seconds: float | None = None) -> list[CoordinatorAnnouncement]:
        self.probe_calls += 1
        return list(self.announcements)

class RenderDeviceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.persistence = PersistenceService(
            base_dir=self.base_dir,
            config_path=self.base_dir / "config.json",
        )
        self.logger = AppLogger(self.persistence.logs_dir)

    def tearDown(self) -> None:
        self.logger.close()
        self.temp_dir.cleanup()

    def _build_job_manager(self, port: int) -> JobManager:
        return self._build_job_manager_for_workers(
            [
                {
                    "id": "test-worker",
                    "name": "TEST-WORKER",
                    "ip": "127.0.0.1",
                    "port": port,
                }
            ]
        )

    def _build_job_manager_for_workers(
        self,
        workers: list[dict[str, object]],
        default_port: int | None = None,
    ) -> JobManager:
        resolved_default_port = int(default_port if default_port is not None else (workers[0]["port"] if workers else 9000))
        settings = AppSettings(
            default_worker_port=resolved_default_port,
            worker_poll_interval_ms=1000,
            worker_timeout_seconds=5,
            default_block_size=2,
            max_block_retries=2,
            workers=[
                {
                    "id": str(worker["id"]),
                    "name": str(worker["name"]),
                    "ip": str(worker["ip"]),
                    "port": int(worker["port"]),
                    "enabled": True,
                    "selected": True,
                }
                for worker in workers
            ],
        )
        return JobManager(
            settings=settings,
            persistence=self.persistence,
            logger=self.logger,
            worker_client=WorkerClient(timeout_seconds=2.0),
        )

    def _start_http_server(self, handler_class: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, Thread]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _stop_http_server(self, server: ThreadingHTTPServer, thread: Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def _start_coordinator_server(self) -> tuple[ThreadingHTTPServer, Thread]:
        MockCoordinatorHandler.state = MockCoordinatorState()
        return self._start_http_server(MockCoordinatorHandler)

    def _start_legacy_strict_coordinator_server(self) -> tuple[ThreadingHTTPServer, Thread]:
        LegacyStrictCoordinatorHandler.state = MockCoordinatorState()
        return self._start_http_server(LegacyStrictCoordinatorHandler)

    def _build_coordinator_manager(
        self,
        port: int | None,
        *,
        coordinator_url: str | None = None,
        discovery_service=None,
        master_id: str = "master_test_01",
        master_name: str = "MASTER-TEST-01",
    ) -> CoordinatorManager:
        resolved_url = coordinator_url
        if resolved_url is None and port is not None:
            resolved_url = f"http://127.0.0.1:{port}"
        settings = AppSettings(
            mode="coordinator",
            coordinator_url=resolved_url or "",
            master_id=master_id,
            master_name=master_name,
            default_worker_port=9000,
            worker_poll_interval_ms=1000,
            worker_timeout_seconds=5,
            default_block_size=2,
            max_block_retries=2,
            workers=[],
        )
        return CoordinatorManager(
            settings=settings,
            persistence=self.persistence,
            logger=self.logger,
            client=CoordinatorClient(settings.coordinator_url, timeout_seconds=2.0)
            if settings.coordinator_url
            else None,
            discovery_service=discovery_service,
        )

    def _prepare_paths(self, version_code: int = 403) -> tuple[Path, Path]:
        blend_path = self.base_dir / "shared" / "scene.blend"
        output_dir = self.base_dir / "renders"
        blend_path.parent.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_blend_file(blend_path, version_code=version_code)
        return blend_path, output_dir

    def _write_blend_file(
        self,
        path: Path,
        *,
        version_code: int = 502,
        modern_header: bool = False,
        zstd_compressed: bool = False,
    ) -> None:
        header = (
            f"BLENDER17-01v{version_code:04d}".encode("ascii")
            if modern_header
            else f"BLENDER-v{version_code:03d}".encode("ascii")
        )
        payload = header + b"\x00" * 32
        if zstd_compressed:
            if zstandard is None:  # pragma: no cover - guarded by tests
                raise RuntimeError("zstandard dependency is not installed")
            payload = zstandard.ZstdCompressor().compress(payload)
        path.write_bytes(payload)

    def _announcement_for_port(
        self,
        port: int,
        *,
        coordinator_id: str = "coord-local-01",
        hostname: str = "COORDINATOR-01",
    ) -> CoordinatorAnnouncement:
        return CoordinatorAnnouncement(
            coordinator_id=coordinator_id,
            namespace="blender_render_farm",
            discovery_port=8011,
            api_port=port,
            started_at="2026-05-25T12:00:00",
            hostname=hostname,
            addresses=["127.0.0.1"],
            api_urls=[f"http://127.0.0.1:{port}"],
            enabled=True,
            status="running",
            received_at=time.time(),
            source_ip="127.0.0.1",
        )

    def _free_udp_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _wait_for_job_completion(self, manager: JobManager, timeout_seconds: float = 8.0) -> dict:
        deadline = time.time() + timeout_seconds
        last_snapshot = manager.get_snapshot()
        while time.time() < deadline:
            last_snapshot = manager.tick()
            job = last_snapshot.get("job")
            if job and job.get("status") == "completed":
                return last_snapshot
            time.sleep(0.05)
        self.fail("Timed out waiting for the job to complete.")

    def test_cpu_worker_keeps_cpu_when_job_sends_legacy_use_gpu_true(self) -> None:
        server = MockWorkerServer(render_device_preference="CPU")
        server.start()
        try:
            manager = self._build_job_manager(server.port)
            snapshot = manager.tick()
            worker = snapshot["workers"][0]
            self.assertEqual(worker["renderDevicePreference"], "CPU")
            self.assertFalse(worker["effectiveUseGpu"])

            blend_path, output_dir = self._prepare_paths()
            manager.start_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=2,
                output_folder=str(output_dir),
                block_size=2,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
                legacy_use_gpu=True,
            )

            self.assertIsNotNone(server.state.current_job)
            self.assertTrue(server.state.current_job["useGpu"])
            self.assertEqual(server.state.render_device_preference, "CPU")
            self.assertFalse(server.state.effective_use_gpu)
        finally:
            server.stop()

    def test_gpu_worker_keeps_gpu_when_job_sends_legacy_use_gpu_false(self) -> None:
        server = MockWorkerServer(render_device_preference="GPU")
        server.start()
        try:
            manager = self._build_job_manager(server.port)
            snapshot = manager.tick()
            worker = snapshot["workers"][0]
            self.assertEqual(worker["renderDevicePreference"], "GPU")
            self.assertTrue(worker["effectiveUseGpu"])

            blend_path, output_dir = self._prepare_paths()
            manager.start_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=2,
                output_folder=str(output_dir),
                block_size=2,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
                legacy_use_gpu=False,
            )

            self.assertIsNotNone(server.state.current_job)
            self.assertFalse(server.state.current_job["useGpu"])
            self.assertEqual(server.state.render_device_preference, "GPU")
            self.assertTrue(server.state.effective_use_gpu)
        finally:
            server.stop()

    def test_remote_render_device_switch_updates_master_state(self) -> None:
        server = MockWorkerServer(render_device_preference="CPU")
        server.start()
        try:
            manager = self._build_job_manager(server.port)
            manager.tick()

            result = manager.update_worker_render_device("test-worker", "GPU")
            snapshot = manager.get_snapshot()
            worker = snapshot["workers"][0]

            self.assertEqual(result["renderDevice"]["renderDevicePreference"], "GPU")
            self.assertTrue(result["renderDevice"]["effectiveUseGpu"])
            self.assertEqual(worker["renderDevicePreference"], "GPU")
            self.assertTrue(worker["effectiveUseGpu"])
        finally:
            server.stop()

    def test_legacy_worker_without_render_device_fields_still_polls(self) -> None:
        server, thread = self._start_http_server(LegacyWorkerHandler)
        try:
            manager = self._build_job_manager(int(server.server_address[1]))
            snapshot = manager.tick()
            worker = snapshot["workers"][0]

            self.assertEqual(worker["name"], "LEGACY-WORKER")
            self.assertTrue(worker["blenderFound"])
            self.assertIsNone(worker["renderDevicePreference"])
            self.assertIsNone(worker["effectiveUseGpu"])
        finally:
            self._stop_http_server(server, thread)

    def test_legacy_validation_payload_still_starts_job(self) -> None:
        server, thread = self._start_http_server(LegacyWorkerHandler)
        try:
            manager = self._build_job_manager(int(server.server_address[1]))
            manager.tick()
            blend_path, output_dir = self._prepare_paths(version_code=402)

            manager.start_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=2,
                output_folder=str(output_dir),
                block_size=2,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            worker = manager.get_snapshot()["workers"][0]
            self.assertTrue(worker["blendFileExists"])
            self.assertTrue(worker["outputFolderExists"])
            self.assertTrue(worker["canWriteOutput"])
        finally:
            self._stop_http_server(server, thread)

    def test_validation_response_with_empty_errors_is_ok(self) -> None:
        server, thread = self._start_http_server(SuccessfulValidationHandler)
        try:
            manager = self._build_job_manager(int(server.server_address[1]))
            manager.tick()
            blend_path, output_dir = self._prepare_paths()

            manager.start_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=2,
                output_folder=str(output_dir),
                block_size=2,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            worker = manager.get_snapshot()["workers"][0]
            self.assertTrue(worker["blendFileExists"])
            self.assertTrue(worker["outputFolderExists"])
            self.assertTrue(worker["canWriteOutput"])
            self.assertEqual(worker["lastValidationErrors"], [])
        finally:
            self._stop_http_server(server, thread)

    def test_some_workers_fail_but_job_starts_with_valid_workers(self) -> None:
        valid_server, valid_thread = self._start_http_server(SuccessfulValidationHandler)
        failing_server, failing_thread = self._start_http_server(RejectingValidationHandler)
        try:
            manager = self._build_job_manager_for_workers(
                [
                    {
                        "id": "valid-worker",
                        "name": "OK-WORKER",
                        "ip": "127.0.0.1",
                        "port": int(valid_server.server_address[1]),
                    },
                    {
                        "id": "invalid-worker",
                        "name": "REJECT-WORKER",
                        "ip": "127.0.0.1",
                        "port": int(failing_server.server_address[1]),
                    },
                ]
            )
            manager.tick()
            blend_path, output_dir = self._prepare_paths()

            warnings = manager.start_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=2,
                output_folder=str(output_dir),
                block_size=2,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            self.assertIsNotNone(manager.current_job)
            self.assertEqual(manager.current_job.selectedWorkerIds, ["valid-worker"])

            warning_text = "\n".join(warnings)
            self.assertIn("REJECT-WORKER", warning_text)
            self.assertIn("blendFileExists: false", warning_text)
            self.assertIn("canWriteOutput: false", warning_text)
            self.assertIn("Blend file not found", warning_text)
            self.assertIn("workers falharam na validacao", warning_text.lower())

            general_log = self.logger.general_text()
            self.assertIn("REJECT-WORKER", general_log)
            self.assertIn("Blend file not found", general_log)
        finally:
            self._stop_http_server(valid_server, valid_thread)
            self._stop_http_server(failing_server, failing_thread)

    def test_detects_blend_version_and_rejects_older_worker_version(self) -> None:
        class CompatibleValidationHandler(SimpleValidationWorkerHandler):
            worker_name = "BLENDER-5-WORKER"

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/status":
                    return self._send_json(
                        {
                            "machineName": self.worker_name,
                            "ip": "127.0.0.1",
                            "port": self.server.server_address[1],
                            "status": "idle",
                            "blenderFound": True,
                            "blenderVersion": "5.2",
                            "gpu": "Validation GPU",
                            "currentJob": None,
                            "currentBlock": None,
                            "lastLog": "",
                            "error": None,
                        }
                    )
                if self.path == "/capabilities":
                    return self._send_json(
                        {
                            "machineName": self.worker_name,
                            "blenderFound": True,
                            "blenderVersion": "5.2",
                            "gpu": "Validation GPU",
                        }
                    )
                super().do_GET()

        valid_server, valid_thread = self._start_http_server(CompatibleValidationHandler)
        old_server, old_thread = self._start_http_server(SuccessfulValidationHandler)
        try:
            manager = self._build_job_manager_for_workers(
                [
                    {
                        "id": "preferred-worker",
                        "name": "BLENDER-5-WORKER",
                        "ip": "127.0.0.1",
                        "port": int(valid_server.server_address[1]),
                    },
                    {
                        "id": "old-worker",
                        "name": "OK-WORKER",
                        "ip": "127.0.0.1",
                        "port": int(old_server.server_address[1]),
                    },
                ]
            )
            manager.tick()
            blend_path, output_dir = self._prepare_paths(version_code=502)

            warnings = manager.start_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=2,
                output_folder=str(output_dir),
                block_size=2,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            self.assertIsNotNone(manager.current_job)
            self.assertEqual(manager.current_job.requiredBlenderVersion, "5.2")
            self.assertEqual(manager.current_job.preferredBlenderMajor, 5)
            self.assertEqual(manager.current_job.versionRoutingPolicy, "prefer_same_major_min_version")
            self.assertEqual(manager.current_job.selectedWorkerIds, ["preferred-worker"])

            warning_text = "\n".join(warnings)
            self.assertNotIn("worker 4.3, arquivo 5.2", warning_text)
        finally:
            self._stop_http_server(valid_server, valid_thread)
            self._stop_http_server(old_server, old_thread)

    def test_preflight_requests_version_choice_when_only_older_same_major_and_higher_major_exist(self) -> None:
        class OlderSameMajorHandler(SimpleValidationWorkerHandler):
            worker_name = "OLDER-4X-WORKER"

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/status":
                    return self._send_json(
                        {
                            "machineName": self.worker_name,
                            "ip": "127.0.0.1",
                            "port": self.server.server_address[1],
                            "status": "idle",
                            "blenderFound": True,
                            "blenderVersion": "4.3",
                            "gpu": "Validation GPU",
                            "currentJob": None,
                            "currentBlock": None,
                            "lastLog": "",
                            "error": None,
                        }
                    )
                if self.path == "/capabilities":
                    return self._send_json(
                        {
                            "machineName": self.worker_name,
                            "blenderFound": True,
                            "blenderVersion": "4.3",
                            "gpu": "Validation GPU",
                        }
                    )
                super().do_GET()

            def do_POST(self) -> None:  # noqa: N802
                if self.path == "/validate-paths":
                    return self._send_json(
                        {
                            "valid": False,
                            "blendAccessible": True,
                            "outputAccessible": True,
                            "reason": "Only older Blender 4.x is available on this worker.",
                            "selectedBlenderVersion": "4.3",
                            "selectedVersionBucket": "same_major_older",
                            "sameMajorOlderAvailable": True,
                            "bestSameMajorOlderVersion": "4.3",
                            "higherMajorAvailable": False,
                            "requiredBlenderVersion": "4.5",
                            "preferredBlenderMajor": 4,
                            "versionRoutingPolicy": "prefer_same_major_min_version",
                            "blendVersionDetected": "4.5",
                        }
                    )
                super().do_POST()

        class HigherMajorHandler(SimpleValidationWorkerHandler):
            worker_name = "HIGHER-5X-WORKER"

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/status":
                    return self._send_json(
                        {
                            "machineName": self.worker_name,
                            "ip": "127.0.0.1",
                            "port": self.server.server_address[1],
                            "status": "idle",
                            "blenderFound": True,
                            "blenderVersion": "5.1",
                            "gpu": "Validation GPU",
                            "currentJob": None,
                            "currentBlock": None,
                            "lastLog": "",
                            "error": None,
                        }
                    )
                if self.path == "/capabilities":
                    return self._send_json(
                        {
                            "machineName": self.worker_name,
                            "blenderFound": True,
                            "blenderVersion": "5.1",
                            "gpu": "Validation GPU",
                        }
                    )
                super().do_GET()

            def do_POST(self) -> None:  # noqa: N802
                if self.path == "/validate-paths":
                    return self._send_json(
                        {
                            "valid": True,
                            "blendAccessible": True,
                            "outputAccessible": True,
                            "selectedBlenderVersion": "5.1",
                            "selectedVersionBucket": "higher_major",
                            "sameMajorOlderAvailable": False,
                            "higherMajorAvailable": True,
                            "bestHigherMajorVersion": "5.1",
                            "requiredBlenderVersion": "4.5",
                            "preferredBlenderMajor": 4,
                            "versionRoutingPolicy": "prefer_same_major_min_version",
                            "blendVersionDetected": "4.5",
                        }
                    )
                super().do_POST()

        older_server, older_thread = self._start_http_server(OlderSameMajorHandler)
        higher_server, higher_thread = self._start_http_server(HigherMajorHandler)
        try:
            manager = self._build_job_manager_for_workers(
                [
                    {
                        "id": "older-worker",
                        "name": "OLDER-4X-WORKER",
                        "ip": "127.0.0.1",
                        "port": int(older_server.server_address[1]),
                    },
                    {
                        "id": "higher-worker",
                        "name": "HIGHER-5X-WORKER",
                        "ip": "127.0.0.1",
                        "port": int(higher_server.server_address[1]),
                    },
                ]
            )
            manager.tick()
            blend_path, output_dir = self._prepare_paths(version_code=405)

            preflight = manager.preflight_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=2,
                output_folder=str(output_dir),
                block_size=2,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            self.assertTrue(preflight["needsVersionChoice"])
            self.assertEqual(preflight["sameMajorOlderVersions"], ["4.3"])
            self.assertEqual(preflight["higherMajorVersions"], ["5.1"])
        finally:
            self._stop_http_server(older_server, older_thread)
            self._stop_http_server(higher_server, higher_thread)

    @unittest.skipIf(zstandard is None, "zstandard dependency not installed")
    def test_detects_modern_zstd_blend_version_metadata(self) -> None:
        blend_path, _ = self._prepare_paths(version_code=500)
        self._write_blend_file(
            blend_path,
            version_code=500,
            modern_header=True,
            zstd_compressed=True,
        )

        metadata = read_blend_version_metadata(blend_path)

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.required_blender_version, "5.0")
        self.assertEqual(metadata.preferred_blender_major, 5)

    def test_direct_mode_accepts_blender_4_5_worker_for_modern_5_0_file(self) -> None:
        class BridgeValidationHandler(SimpleValidationWorkerHandler):
            worker_name = "BLENDER-45-WORKER"

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/status":
                    return self._send_json(
                        {
                            "machineName": self.worker_name,
                            "ip": "127.0.0.1",
                            "port": self.server.server_address[1],
                            "status": "idle",
                            "blenderFound": True,
                            "blenderVersion": "4.5",
                            "gpu": "Validation GPU",
                            "currentJob": None,
                            "currentBlock": None,
                            "lastLog": "",
                            "error": None,
                        }
                    )
                if self.path == "/capabilities":
                    return self._send_json(
                        {
                            "machineName": self.worker_name,
                            "blenderFound": True,
                            "blenderVersion": "4.5",
                            "gpu": "Validation GPU",
                        }
                    )
                super().do_GET()

        bridge_server, bridge_thread = self._start_http_server(BridgeValidationHandler)
        try:
            manager = self._build_job_manager_for_workers(
                [
                    {
                        "id": "bridge-worker",
                        "name": "BLENDER-45-WORKER",
                        "ip": "127.0.0.1",
                        "port": int(bridge_server.server_address[1]),
                    }
                ]
            )
            manager.tick()
            blend_path, output_dir = self._prepare_paths(version_code=500)
            self._write_blend_file(blend_path, version_code=500, modern_header=True)

            warnings = manager.start_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=2,
                output_folder=str(output_dir),
                block_size=2,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            self.assertIsNotNone(manager.current_job)
            self.assertEqual(manager.current_job.selectedWorkerIds, ["bridge-worker"])
            self.assertFalse(any("Use Blender 5.0 ou superior" in warning for warning in warnings))
        finally:
            self._stop_http_server(bridge_server, bridge_thread)

    def test_all_workers_fail_raise_detailed_validation_error(self) -> None:
        class RejectingValidationHandlerTwo(RejectingValidationHandler):
            worker_name = "REJECT-WORKER-02"
            validation_payload = {
                "blendFileExists": True,
                "outputFolderExists": False,
                "canWriteOutput": False,
                "errors": [
                    "Could not create output folder: X:\\Projetos\\Cena01\\renders",
                ],
            }

        first_server, first_thread = self._start_http_server(RejectingValidationHandler)
        second_server, second_thread = self._start_http_server(RejectingValidationHandlerTwo)
        try:
            manager = self._build_job_manager_for_workers(
                [
                    {
                        "id": "reject-worker-1",
                        "name": "REJECT-WORKER",
                        "ip": "127.0.0.1",
                        "port": int(first_server.server_address[1]),
                    },
                    {
                        "id": "reject-worker-2",
                        "name": "REJECT-WORKER-02",
                        "ip": "127.0.0.1",
                        "port": int(second_server.server_address[1]),
                    },
                ]
            )
            manager.tick()
            blend_path, output_dir = self._prepare_paths()

            with self.assertRaises(WorkerPathValidationError) as context:
                manager.start_job(
                    blend_file=str(blend_path),
                    start_frame=1,
                    end_frame=2,
                    output_folder=str(output_dir),
                    block_size=2,
                    ignore_rendered_frames=True,
                    auto_add_workers=True,
                    redistribute_offline_blocks=True,
                )

            message = context.exception.detailed_text()
            self.assertIn("Nenhum worker selecionado conseguiu validar os caminhos.", message)
            self.assertIn("REJECT-WORKER", message)
            self.assertIn("REJECT-WORKER-02", message)
            self.assertIn("blendFileExists: false", message)
            self.assertIn("outputFolderExists: false", message)
            self.assertIn("Could not create output folder", message)
            self.assertEqual(len(context.exception.results), 2)

            general_log = self.logger.general_text()
            self.assertIn("Validacao de paths falhou", general_log)
            self.assertIn("REJECT-WORKER-02", general_log)
        finally:
            self._stop_http_server(first_server, first_thread)
            self._stop_http_server(second_server, second_thread)

    def test_mapped_drive_hint_is_generated_for_drive_letter_paths(self) -> None:
        manager = self._build_job_manager(9000)
        worker = manager.worker_manager.list_workers()[0]
        hint = manager._mapped_drive_hint(  # noqa: SLF001
            ["X:\\Projetos\\Cena01\\cena.blend"],
            [
                manager._apply_validation_payload(  # noqa: SLF001
                    worker,
                    {
                        "blendFileExists": False,
                        "outputFolderExists": True,
                        "canWriteOutput": False,
                        "errors": ["Blend file not found: X:\\Projetos\\Cena01\\cena.blend"],
                    },
                )
            ],
        )
        self.assertIsNotNone(hint)
        self.assertIn("caminho UNC", hint)

    def test_fast_worker_receives_more_frames_with_frame_by_frame_dispatch(self) -> None:
        slow_server = MockWorkerServer(
            render_device_preference="GPU",
            name="SLOW-WORKER",
            seconds_per_frame=0.25,
        )
        fast_server = MockWorkerServer(
            render_device_preference="GPU",
            name="FAST-WORKER",
            seconds_per_frame=0.05,
        )
        slow_server.start()
        fast_server.start()
        try:
            manager = self._build_job_manager_for_workers(
                [
                    {
                        "id": "slow-worker",
                        "name": "SLOW-WORKER",
                        "ip": "127.0.0.1",
                        "port": slow_server.port,
                    },
                    {
                        "id": "fast-worker",
                        "name": "FAST-WORKER",
                        "ip": "127.0.0.1",
                        "port": fast_server.port,
                    },
                ]
            )
            manager.frame_scanner.stability_seconds = 0.0
            manager._scan_interval_seconds = 0.0  # noqa: SLF001
            manager.tick()

            blend_path, output_dir = self._prepare_paths()
            manager.start_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=6,
                output_folder=str(output_dir),
                block_size=10,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            self._wait_for_job_completion(manager, timeout_seconds=10.0)

            self.assertIsNotNone(manager.current_job)
            done_blocks = [block for block in manager.current_job.blocks if block.status == "done"]
            self.assertTrue(done_blocks)
            self.assertTrue(all(block.startFrame == block.endFrame for block in done_blocks))

            frames_by_worker: dict[str, int] = {}
            for block in done_blocks:
                if block.workerId:
                    frames_by_worker[block.workerId] = frames_by_worker.get(block.workerId, 0) + 1

            self.assertGreater(frames_by_worker.get("fast-worker", 0), frames_by_worker.get("slow-worker", 0))
        finally:
            slow_server.stop()
            fast_server.stop()

    def test_mark_block_done_releases_worker_state(self) -> None:
        manager = self._build_job_manager(9000)
        worker = manager.worker_manager.list_workers()[0]
        job = RenderJob(
            jobId="job_test_release",
            blendFile=str(self.base_dir / "shared" / "scene.blend"),
            outputFolder=str(self.base_dir / "renders"),
            outputPattern=str(self.base_dir / "renders" / "frame_####"),
            startFrame=1,
            endFrame=1,
            blockSize=1,
            status="running",
        )
        block = RenderBlock(
            blockId="block_0001_0001_0001",
            startFrame=1,
            endFrame=1,
            status="rendering",
            workerId=worker.id,
        )
        job.blocks = [block]
        manager.current_job = job

        worker.status = "rendering"
        worker.currentJobId = job.jobId
        worker.currentBlockId = block.blockId
        worker.currentFrame = 1
        worker.progress = 75.0

        manager._mark_block_done(block)  # noqa: SLF001

        self.assertEqual(block.status, "done")
        self.assertEqual(worker.status, "idle")
        self.assertIsNone(worker.currentJobId)
        self.assertIsNone(worker.currentBlockId)
        self.assertIsNone(worker.currentFrame)
        self.assertEqual(worker.progress, 0.0)

    def test_progress_is_estimated_from_current_frame_when_worker_reports_zero(self) -> None:
        manager = self._build_job_manager(9000)
        worker = manager.worker_manager.list_workers()[0]
        job = RenderJob(
            jobId="job_test_progress",
            blendFile=str(self.base_dir / "shared" / "scene.blend"),
            outputFolder=str(self.base_dir / "renders"),
            outputPattern=str(self.base_dir / "renders" / "frame_####"),
            startFrame=10,
            endFrame=13,
            blockSize=4,
            status="running",
        )
        block = RenderBlock(
            blockId="block_0002_0010_0013",
            startFrame=10,
            endFrame=13,
            status="assigned",
            workerId=worker.id,
        )
        job.blocks = [block]
        manager.current_job = job

        worker.status = "rendering"
        worker.currentJobId = job.jobId
        worker.currentBlockId = block.blockId
        worker.currentFrame = 11
        worker.progress = 0.0

        manager._reconcile_active_blocks()  # noqa: SLF001

        self.assertEqual(block.status, "rendering")
        self.assertGreater(worker.progress, 0.0)
        self.assertLess(worker.progress, 100.0)

    def test_discover_workers_adds_and_persists_new_worker(self) -> None:
        server = MockWorkerServer(
            render_device_preference="GPU",
            name="DISCOVERY-WORKER",
        )
        server.start()
        try:
            manager = self._build_job_manager_for_workers([], default_port=server.port)
            result = manager.discover_workers(hosts=["127.0.0.1"], ports=[server.port])

            self.assertEqual(result["discoveredCount"], 1)
            self.assertEqual(result["addedCount"], 1)
            self.assertEqual(result["updatedCount"], 0)

            snapshot = manager.get_snapshot()
            self.assertEqual(len(snapshot["workers"]), 1)
            self.assertEqual(snapshot["workers"][0]["name"], "DISCOVERY-WORKER")
            self.assertEqual(snapshot["workers"][0]["ip"], "127.0.0.1")
            self.assertEqual(snapshot["workers"][0]["port"], server.port)

            reloaded_settings = self.persistence.load_settings()
            self.assertEqual(len(reloaded_settings.workers), 1)
            self.assertEqual(reloaded_settings.workers[0]["name"], "DISCOVERY-WORKER")
        finally:
            server.stop()

    def test_discover_workers_updates_existing_worker_without_duplicate(self) -> None:
        server = MockWorkerServer(
            render_device_preference="GPU",
            name="DISCOVERY-WORKER",
        )
        server.start()
        try:
            manager = self._build_job_manager_for_workers(
                [
                    {
                        "id": "saved-worker",
                        "name": "OLD-NAME",
                        "ip": "127.0.0.1",
                        "port": server.port,
                    }
                ],
                default_port=server.port,
            )
            result = manager.discover_workers(hosts=["127.0.0.1"], ports=[server.port])

            self.assertEqual(result["discoveredCount"], 1)
            self.assertEqual(result["addedCount"], 0)
            self.assertEqual(result["updatedCount"], 1)

            snapshot = manager.get_snapshot()
            self.assertEqual(len(snapshot["workers"]), 1)
            self.assertEqual(snapshot["workers"][0]["id"], "saved-worker")
            self.assertEqual(snapshot["workers"][0]["name"], "DISCOVERY-WORKER")

            reloaded_settings = self.persistence.load_settings()
            self.assertEqual(len(reloaded_settings.workers), 1)
            self.assertEqual(reloaded_settings.workers[0]["id"], "saved-worker")
            self.assertEqual(reloaded_settings.workers[0]["name"], "DISCOVERY-WORKER")
        finally:
            server.stop()

    def test_coordinator_tick_sends_master_heartbeat_and_polls_state(self) -> None:
        server, thread = self._start_coordinator_server()
        try:
            manager = self._build_coordinator_manager(int(server.server_address[1]))
            snapshot = manager.tick()

            self.assertEqual(len(MockCoordinatorHandler.state.heartbeats), 1)
            heartbeat = MockCoordinatorHandler.state.heartbeats[0]
            self.assertEqual(heartbeat["masterId"], "master_test_01")
            self.assertEqual(heartbeat["protocolVersion"], 2)
            self.assertTrue(heartbeat["buildId"])
            self.assertEqual(snapshot["workers"][0]["name"], "COORD-WORKER-01")
            self.assertEqual(snapshot["metrics"]["machines_online"], 1)
        finally:
            self._stop_http_server(server, thread)

    def test_coordinator_start_job_submits_to_global_queue(self) -> None:
        server, thread = self._start_coordinator_server()
        try:
            manager = self._build_coordinator_manager(int(server.server_address[1]))
            warnings = manager.start_job(
                blend_file="X:\\Projetos\\Cena01\\cena.blend",
                start_frame=1,
                end_frame=10,
                output_folder="X:\\Projetos\\Cena01\\renders",
                block_size=5,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            self.assertEqual(len(MockCoordinatorHandler.state.jobs), 1)
            submitted = MockCoordinatorHandler.state.jobs[0]
            self.assertEqual(submitted["submittedByMasterId"], "master_test_01")
            self.assertEqual(submitted["submittedByMasterName"], "MASTER-TEST-01")
            self.assertEqual(submitted["blendFile"], "X:\\Projetos\\Cena01\\cena.blend")
            self.assertIn("fila global", warnings[0].lower())
        finally:
            self._stop_http_server(server, thread)

    def test_coordinator_start_job_generates_master_identity_when_config_is_blank(self) -> None:
        StrictIdentityCoordinatorHandler.state = MockCoordinatorState()
        server, thread = self._start_http_server(StrictIdentityCoordinatorHandler)
        try:
            manager = self._build_coordinator_manager(
                int(server.server_address[1]),
                master_id="",
                master_name="",
            )
            warnings = manager.start_job(
                blend_file="X:\\Projetos\\Cena01\\cena.blend",
                start_frame=1,
                end_frame=2,
                output_folder="X:\\Projetos\\Cena01\\renders",
                block_size=1,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            self.assertEqual(len(StrictIdentityCoordinatorHandler.state.jobs), 1)
            submitted = StrictIdentityCoordinatorHandler.state.jobs[0]
            self.assertTrue(submitted["submittedByMasterId"])
            self.assertTrue(submitted["submittedByMasterName"])
            self.assertTrue(submitted["submittedByUser"])
            self.assertIn("fila global", warnings[0].lower())
        finally:
            self._stop_http_server(server, thread)

    def test_coordinator_client_surfaces_response_detail_on_bad_request(self) -> None:
        AlwaysRejectJobCoordinatorHandler.state = MockCoordinatorState()
        server, thread = self._start_http_server(AlwaysRejectJobCoordinatorHandler)
        try:
            client = CoordinatorClient(f"http://127.0.0.1:{server.server_address[1]}", timeout_seconds=2.0)
            with self.assertRaises(CoordinatorClientError) as context:
                client.create_job({"jobName": "Teste"})
            self.assertIn("submittedByMasterId", str(context.exception))
            self.assertIn("Coordinator detail", str(context.exception))
        finally:
            self._stop_http_server(server, thread)

    def test_coordinator_start_job_sends_optional_blend_version_metadata(self) -> None:
        server, thread = self._start_coordinator_server()
        try:
            manager = self._build_coordinator_manager(int(server.server_address[1]))
            blend_path, output_dir = self._prepare_paths(version_code=502)

            warnings = manager.start_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=10,
                output_folder=str(output_dir),
                block_size=5,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            submitted = MockCoordinatorHandler.state.jobs[0]
            self.assertEqual(submitted["requiredBlenderVersion"], "5.2")
            self.assertEqual(submitted["preferredBlenderMajor"], 5)
            self.assertEqual(submitted["versionRoutingPolicy"], "prefer_same_major_min_version")
            self.assertIn("Blender do arquivo 5.2", "\n".join(warnings))
        finally:
            self._stop_http_server(server, thread)

    def test_coordinator_start_job_falls_back_when_old_schema_rejects_version_metadata(self) -> None:
        server, thread = self._start_legacy_strict_coordinator_server()
        try:
            manager = self._build_coordinator_manager(int(server.server_address[1]))
            blend_path, output_dir = self._prepare_paths(version_code=502)

            warnings = manager.start_job(
                blend_file=str(blend_path),
                start_frame=1,
                end_frame=10,
                output_folder=str(output_dir),
                block_size=5,
                ignore_rendered_frames=True,
                auto_add_workers=True,
                redistribute_offline_blocks=True,
            )

            submitted = LegacyStrictCoordinatorHandler.state.jobs[0]
            self.assertIsNone(submitted["requiredBlenderVersion"])
            self.assertIsNone(submitted["preferredBlenderMajor"])
            self.assertIsNone(submitted["versionRoutingPolicy"])
            self.assertIn("nao aceitou os metadados opcionais", "\n".join(warnings).lower())
        finally:
            self._stop_http_server(server, thread)

    def test_coordinator_discover_workers_uses_central_endpoint(self) -> None:
        server, thread = self._start_coordinator_server()
        try:
            manager = self._build_coordinator_manager(int(server.server_address[1]))
            result = manager.discover_workers()

            self.assertEqual(result["discoveredCount"], 1)
            snapshot = manager.get_snapshot()
            self.assertEqual(len(snapshot["workers"]), 2)
            self.assertEqual(snapshot["workers"][1]["name"], "DISCOVERED-WORKER")
        finally:
            self._stop_http_server(server, thread)

    def test_coordinator_discovery_service_receives_passive_announcement(self) -> None:
        listen_port = self._free_udp_port()
        service = CoordinatorDiscoveryService(
            udp_port=listen_port,
            listen_port=listen_port,
            probe_timeout_seconds=0.1,
            announcement_ttl_seconds=1.0,
            broadcast_hosts=["127.0.0.1"],
        )
        service.start()
        try:
            payload = {
                "protocol": "blender-render-farm-coordinator",
                "version": 1,
                "messageType": "announce",
                "coordinatorId": "coord-passive-01",
                "namespace": "blender_render_farm",
                "discoveryPort": listen_port,
                "apiPort": 8010,
                "startedAt": "2026-05-25T12:00:00",
                "hostname": "COORD-PASSIVE-01",
                "addresses": ["127.0.0.1"],
                "apiUrls": ["http://127.0.0.1:8010"],
                "enabled": True,
                "status": "running",
            }
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
                probe_socket.sendto(json.dumps(payload).encode("utf-8"), ("127.0.0.1", listen_port))

            deadline = time.time() + 1.0
            announcements: list[CoordinatorAnnouncement] = []
            while time.time() < deadline:
                announcements = service.recent_announcements()
                if announcements:
                    break
                time.sleep(0.05)

            self.assertEqual(len(announcements), 1)
            self.assertEqual(announcements[0].coordinator_id, "coord-passive-01")
            self.assertEqual(announcements[0].api_urls, ["http://127.0.0.1:8010"])
        finally:
            service.stop()

    def test_coordinator_tick_discovers_without_manual_url(self) -> None:
        server, thread = self._start_coordinator_server()
        try:
            discovery = StaticCoordinatorDiscoveryService(
                [self._announcement_for_port(int(server.server_address[1]))]
            )
            manager = self._build_coordinator_manager(
                None,
                coordinator_url="",
                discovery_service=discovery,
            )
            snapshot = manager.tick()

            self.assertEqual(len(MockCoordinatorHandler.state.heartbeats), 1)
            self.assertEqual(snapshot["coordinatorDiscovery"]["status"], "connected")
            self.assertEqual(snapshot["coordinator"]["apiUrl"], f"http://127.0.0.1:{int(server.server_address[1])}")
            self.assertEqual(snapshot["workers"][0]["name"], "COORD-WORKER-01")
        finally:
            self._stop_http_server(server, thread)

    def test_coordinator_conflict_blocks_automatic_selection(self) -> None:
        discovery = StaticCoordinatorDiscoveryService(
            [
                self._announcement_for_port(8010, coordinator_id="coord-a", hostname="COORD-A"),
                self._announcement_for_port(8011, coordinator_id="coord-b", hostname="COORD-B"),
            ]
        )
        manager = self._build_coordinator_manager(
            None,
            coordinator_url="",
            discovery_service=discovery,
        )

        snapshot = manager.tick()

        self.assertIsNone(manager.client)
        self.assertEqual(snapshot["coordinatorDiscovery"]["status"], "conflict")
        self.assertIn("Conflito de Coordinator", snapshot["coordinatorDiscovery"]["message"])
        self.assertEqual(snapshot["workers"], [])

    def test_coordinator_failure_clears_active_connection_and_returns_to_discovery(self) -> None:
        server, thread = self._start_coordinator_server()
        discovery = StaticCoordinatorDiscoveryService(
            [self._announcement_for_port(int(server.server_address[1]))]
        )
        try:
            manager = self._build_coordinator_manager(
                None,
                coordinator_url="",
                discovery_service=discovery,
            )
            first_snapshot = manager.tick()
            self.assertEqual(first_snapshot["coordinatorDiscovery"]["status"], "connected")

            self._stop_http_server(server, thread)
            discovery.announcements = []

            second_snapshot = manager.tick()

            self.assertIsNone(manager.client)
            self.assertEqual(second_snapshot["coordinatorDiscovery"]["status"], "discovering")
            self.assertEqual(second_snapshot["workers"], [])
            self.assertIn("Coordinator indisponivel", second_snapshot["logs"]["general"])
        finally:
            if thread.is_alive():
                self._stop_http_server(server, thread)

    def test_build_runtime_manager_falls_back_to_direct_mode(self) -> None:
        settings = AppSettings(
            mode="direct",
            coordinator_url="http://127.0.0.1:9100",
            master_id="master_direct",
            master_name="MASTER-DIRECT",
            default_worker_port=9000,
            workers=[],
        )
        manager = build_runtime_manager(settings, self.persistence, self.logger)
        self.assertIsInstance(manager, JobManager)

    def test_build_runtime_manager_uses_coordinator_mode_without_manual_url(self) -> None:
        settings = AppSettings(
            mode="coordinator",
            coordinator_url="",
            master_id="master_coord",
            master_name="MASTER-COORD",
            default_worker_port=9000,
            workers=[],
        )
        manager = build_runtime_manager(settings, self.persistence, self.logger)
        self.assertIsInstance(manager, CoordinatorManager)
        manager.close()

    def test_main_window_does_not_duplicate_workers_when_same_coordinator_snapshot_repeats(self) -> None:
        class StaticCoordinatorController:
            mode = "coordinator"
            supports_local_jobs = False
            supports_local_job_files = False
            supports_worker_selection = False
            supports_worker_device_update = False
            supports_rescan = False

            def __init__(self, snapshot: dict) -> None:
                self._snapshot = snapshot

            def get_snapshot(self) -> dict:
                return self._snapshot

            def tick(self) -> dict:
                return self._snapshot

            def discover_workers(self) -> dict:
                return {"snapshot": self._snapshot, "workers": []}

            def update_job_options(self, auto_add_workers: bool, redistribute_offline_blocks: bool) -> None:
                return

        snapshot = {
            "workers": [
                {
                    "id": "coord-worker-1",
                    "name": "COORD-WORKER-01",
                    "ip": "127.0.0.1",
                    "port": 9000,
                    "enabled": True,
                    "selected": True,
                    "status": "idle",
                    "blenderFound": True,
                    "blenderVersion": "4.3",
                    "gpu": "Coord GPU",
                    "renderDevicePreference": "GPU",
                    "effectiveUseGpu": True,
                    "blendAccessible": True,
                    "outputAccessible": True,
                    "progress": 0.0,
                    "lastLog": "",
                    "error": None,
                }
            ],
            "job": None,
            "queue": [],
            "metrics": {
                "job_name": "",
                "blend_file": "",
                "total_frames": 0,
                "completed_frames": 0,
                "pending_frames": 0,
                "rendering_frames": 0,
                "error_frames": 0,
                "overall_percent": 0.0,
                "elapsed_seconds": 0.0,
                "eta_seconds": None,
                "machines_online": 1,
                "machines_rendering": 0,
            },
            "logs": {"general": "Coordinator mock online", "workers": {}},
        }
        controller = StaticCoordinatorController(snapshot)
        window = MainWindow(
            base_dir=self.base_dir,
            job_manager=controller,
            persistence=self.persistence,
            logger=self.logger,
            default_block_size=2,
            default_worker_port=9000,
            poll_interval_ms=5000,
        )
        try:
            window._apply_snapshot(snapshot)
            window._apply_snapshot(snapshot)
            self.assertEqual(window.workers_table.rowCount(), 1)
        finally:
            window.close()

    def test_main_window_displays_detected_blend_version_and_policy(self) -> None:
        class StaticController:
            mode = "direct"
            supports_local_jobs = True
            supports_local_job_files = True
            supports_worker_selection = True
            supports_worker_device_update = True
            supports_rescan = True

            def get_snapshot(self) -> dict:
                return {
                    "workers": [],
                    "job": None,
                    "metrics": {
                        "job_name": "",
                        "blend_file": "",
                        "total_frames": 0,
                        "completed_frames": 0,
                        "pending_frames": 0,
                        "rendering_frames": 0,
                        "error_frames": 0,
                        "overall_percent": 0.0,
                        "elapsed_seconds": 0.0,
                        "eta_seconds": None,
                        "machines_online": 0,
                        "machines_rendering": 0,
                    },
                    "logs": {"general": "", "workers": {}},
                }

            def tick(self) -> dict:
                return self.get_snapshot()

            def update_job_options(self, auto_add_workers: bool, redistribute_offline_blocks: bool) -> None:
                return

        blend_path, _ = self._prepare_paths(version_code=502)
        metadata = read_blend_version_metadata(blend_path)
        self.assertIsNotNone(metadata)
        controller = StaticController()
        window = MainWindow(
            base_dir=self.base_dir,
            job_manager=controller,
            persistence=self.persistence,
            logger=self.logger,
            default_block_size=2,
            default_worker_port=9000,
            poll_interval_ms=5000,
        )
        try:
            window.blend_edit.setText(str(blend_path))
            self.qt_app.processEvents()
            self.assertEqual(window.detected_blend_version_label.text(), "5.2")
            self.assertEqual(window.detected_blend_policy_label.text(), "Prefer 5.x >= 5.2")
        finally:
            window.close()

    def test_main_window_exposes_credit_link_in_header(self) -> None:
        class StaticController:
            mode = "direct"
            supports_local_jobs = True
            supports_local_job_files = True
            supports_worker_selection = True
            supports_worker_device_update = True
            supports_rescan = True

            def get_snapshot(self) -> dict:
                return {
                    "workers": [],
                    "job": None,
                    "metrics": {
                        "job_name": "",
                        "blend_file": "",
                        "total_frames": 0,
                        "completed_frames": 0,
                        "pending_frames": 0,
                        "rendering_frames": 0,
                        "error_frames": 0,
                        "overall_percent": 0.0,
                        "elapsed_seconds": 0.0,
                        "eta_seconds": None,
                        "machines_online": 0,
                        "machines_rendering": 0,
                    },
                    "logs": {"general": "", "workers": {}},
                }

            def tick(self) -> dict:
                return self.get_snapshot()

            def update_job_options(self, auto_add_workers: bool, redistribute_offline_blocks: bool) -> None:
                return

        window = MainWindow(
            base_dir=self.base_dir,
            job_manager=StaticController(),
            persistence=self.persistence,
            logger=self.logger,
            default_block_size=2,
            default_worker_port=9000,
            poll_interval_ms=5000,
        )
        try:
            self.assertTrue(window.header_credit_label.openExternalLinks())
            self.assertIn("Vinicius D'Arcadia Cruz", window.header_credit_label.text())
            self.assertIn("https://novaxd.tech/", window.header_credit_label.text())
        finally:
            window.close()

    def test_main_window_moves_workers_between_parallel_job_tabs(self) -> None:
        class StaticController:
            mode = "coordinator"
            supports_local_jobs = False
            supports_local_job_files = False
            supports_worker_selection = False
            supports_worker_device_update = False
            supports_rescan = False

            def __init__(self, snapshot: dict) -> None:
                self._snapshot = snapshot
                self.settings = AppSettings(
                    mode="coordinator",
                    coordinator_url="",
                    master_id="master_test_01",
                    master_name="MASTER-TEST-01",
                    default_worker_port=9000,
                    worker_poll_interval_ms=1000,
                    worker_timeout_seconds=5,
                    default_block_size=2,
                    max_block_retries=2,
                    workers=[],
                )

            def get_snapshot(self) -> dict:
                return self._snapshot

            def tick(self) -> dict:
                return self._snapshot

            def update_job_options(self, auto_add_workers: bool, redistribute_offline_blocks: bool) -> None:
                return

        def build_job(
            job_id: str,
            name: str,
            status: str,
            start_frame: int,
            end_frame: int,
            *,
            worker_id: str | None = None,
            queue_position: int = 0,
        ) -> dict:
            blocks: list[dict] = []
            if worker_id:
                blocks.append(
                    {
                        "blockId": f"{job_id}_block",
                        "workerId": worker_id,
                        "startFrame": start_frame,
                        "endFrame": end_frame,
                        "status": "rendering" if status == "running" else "assigned",
                    }
                )
            return {
                "jobId": job_id,
                "jobName": name,
                "blendFile": str(self.base_dir / "shared" / f"{job_id}.blend"),
                "startFrame": start_frame,
                "endFrame": end_frame,
                "status": status,
                "queuePosition": queue_position,
                "blocks": blocks,
                "totalFrames": max(0, end_frame - start_frame + 1),
                "completedFramesCount": 0,
                "overallPercent": 0.0,
                "createdAt": "2026-06-05T10:00:00",
                "submittedByMasterName": "MASTER-TEST-01",
                "submittedByUser": "tester",
            }

        def tab_index_for_job(window: MainWindow, job_id: str | None) -> int:
            for index in range(window.worker_job_tabs.count()):
                if window.worker_job_tabs.tabData(index) == job_id:
                    return index
            raise AssertionError(f"Tab for job {job_id!r} was not found")

        job_a = build_job("job_a", "Job A", "running", 1, 20, worker_id="worker_a", queue_position=0)
        job_b = build_job("job_b", "Job B", "running", 101, 120, worker_id="worker_b", queue_position=1)
        job_c = build_job("job_c", "Job C", "queued", 201, 220, queue_position=2)
        job_a["completedFramesCount"] = 2
        job_a["pending_frames"] = 17
        job_a["rendering_frames"] = 1
        job_a["error_frames"] = 0
        job_a["overallPercent"] = 10.0
        job_b["completedFramesCount"] = 11
        job_b["pending_frames"] = 8
        job_b["rendering_frames"] = 1
        job_b["error_frames"] = 1
        job_b["overallPercent"] = 55.0

        first_snapshot = {
            "workers": [
                {
                    "id": "worker_a",
                    "name": "WORKER-A",
                    "ip": "127.0.0.1",
                    "port": 9001,
                    "enabled": True,
                    "selected": True,
                    "status": "rendering",
                    "blenderFound": True,
                    "blenderVersion": "4.3",
                    "gpu": "GPU-A",
                    "renderDevicePreference": "GPU",
                    "effectiveUseGpu": True,
                    "blendAccessible": True,
                    "outputAccessible": True,
                    "currentJobId": "job_a",
                    "currentBlockId": "job_a_block",
                    "currentFrame": 5,
                    "progress": 0.4,
                    "lastLog": "",
                    "error": None,
                },
                {
                    "id": "worker_b",
                    "name": "WORKER-B",
                    "ip": "127.0.0.1",
                    "port": 9002,
                    "enabled": True,
                    "selected": True,
                    "status": "rendering",
                    "blenderFound": True,
                    "blenderVersion": "4.3",
                    "gpu": "GPU-B",
                    "renderDevicePreference": "GPU",
                    "effectiveUseGpu": True,
                    "blendAccessible": True,
                    "outputAccessible": True,
                    "currentJobId": "job_b",
                    "currentBlockId": "job_b_block",
                    "currentFrame": 110,
                    "progress": 0.6,
                    "lastLog": "",
                    "error": None,
                },
                {
                    "id": "worker_idle",
                    "name": "WORKER-IDLE",
                    "ip": "127.0.0.1",
                    "port": 9003,
                    "enabled": True,
                    "selected": True,
                    "status": "idle",
                    "blenderFound": True,
                    "blenderVersion": "4.3",
                    "gpu": "GPU-IDLE",
                    "renderDevicePreference": "CPU",
                    "effectiveUseGpu": False,
                    "blendAccessible": True,
                    "outputAccessible": True,
                    "currentJobId": None,
                    "currentBlockId": None,
                    "currentFrame": None,
                    "progress": 0.0,
                    "lastLog": "",
                    "error": None,
                },
            ],
            "job": job_a,
            "jobs": [job_a, job_b, job_c],
            "queue": [job_c],
            "metrics": {
                "job_name": "Job A",
                "blend_file": str(self.base_dir / "shared" / "job_a.blend"),
                "total_frames": 20,
                "completed_frames": 0,
                "pending_frames": 18,
                "rendering_frames": 2,
                "error_frames": 0,
                "overall_percent": 10.0,
                "elapsed_seconds": 12.0,
                "eta_seconds": 108.0,
                "machines_online": 3,
                "machines_rendering": 2,
            },
            "logs": {"general": "Parallel jobs", "workers": {}},
        }

        second_job_b = build_job("job_b", "Job B", "running", 101, 120, worker_id="worker_b", queue_position=0)
        second_job_b["completedFramesCount"] = 6
        second_job_b["pending_frames"] = 12
        second_job_b["rendering_frames"] = 2
        second_job_b["error_frames"] = 0
        second_job_b["overallPercent"] = 30.0
        second_job_b["blocks"].append(
            {
                "blockId": "job_b_block_2",
                "workerId": "worker_a",
                "startFrame": 111,
                "endFrame": 120,
                "status": "rendering",
            }
        )
        second_snapshot = {
            "workers": [
                {
                    **first_snapshot["workers"][0],
                    "currentJobId": "job_b",
                    "currentBlockId": "job_b_block_2",
                    "currentFrame": 111,
                    "progress": 0.2,
                },
                first_snapshot["workers"][1],
                first_snapshot["workers"][2],
            ],
            "job": second_job_b,
            "jobs": [second_job_b, job_c],
            "queue": [job_c],
            "metrics": {
                "job_name": "Job B",
                "blend_file": str(self.base_dir / "shared" / "job_b.blend"),
                "total_frames": 20,
                "completed_frames": 6,
                "pending_frames": 12,
                "rendering_frames": 2,
                "error_frames": 0,
                "overall_percent": 30.0,
                "elapsed_seconds": 24.0,
                "eta_seconds": 56.0,
                "machines_online": 3,
                "machines_rendering": 2,
            },
            "logs": {"general": "Parallel jobs after rebalance", "workers": {}},
        }

        controller = StaticController(first_snapshot)
        window = MainWindow(
            base_dir=self.base_dir,
            job_manager=controller,
            persistence=self.persistence,
            logger=self.logger,
            default_block_size=2,
            default_worker_port=9000,
            poll_interval_ms=5000,
        )
        try:
            window._apply_snapshot(first_snapshot)
            self.assertEqual(window.worker_job_tabs.count(), 3)
            self.assertEqual(window.queue_table.rowCount(), 3)

            job_b_index = tab_index_for_job(window, "job_b")
            window.worker_job_tabs.setCurrentIndex(job_b_index)
            self.qt_app.processEvents()
            self.assertEqual(window.job_card.value_label.text(), "Job B")
            self.assertEqual(window.general_progress_bar.value(), 55)
            self.assertEqual(window.progress_detail_label.text(), "11 / 20 frames")
            self.assertEqual(window.workers_table.rowCount(), 1)
            self.assertEqual(window.workers_table.item(0, 1).text(), "WORKER-B")

            job_a_index = tab_index_for_job(window, "job_a")
            window.worker_job_tabs.setCurrentIndex(job_a_index)
            self.qt_app.processEvents()
            self.assertEqual(window.job_card.value_label.text(), "Job A")
            self.assertEqual(window.general_progress_bar.value(), 10)
            self.assertEqual(window.progress_detail_label.text(), "2 / 20 frames")
            self.assertEqual(window.workers_table.rowCount(), 1)
            self.assertEqual(window.workers_table.item(0, 1).text(), "WORKER-A")

            window._apply_snapshot(second_snapshot)
            self.assertEqual(window.worker_job_tabs.count(), 2)
            self.assertEqual(window.queue_table.rowCount(), 2)
            self.assertEqual(window.worker_job_tabs.tabData(window.worker_job_tabs.currentIndex()), "job_b")
            self.assertEqual(window.job_card.value_label.text(), "Job B")
            self.assertEqual(window.general_progress_bar.value(), 30)
            self.assertEqual(window.progress_detail_label.text(), "6 / 20 frames")
            self.assertEqual(window.workers_table.rowCount(), 2)
            visible_names = {window.workers_table.item(row, 1).text() for row in range(window.workers_table.rowCount())}
            self.assertEqual(visible_names, {"WORKER-A", "WORKER-B"})
        finally:
            window.close()

    def test_main_window_normalizes_fractional_progress_values_for_bars(self) -> None:
        class StaticController:
            mode = "coordinator"
            supports_local_jobs = False
            supports_local_job_files = False
            supports_worker_selection = False
            supports_worker_device_update = False
            supports_rescan = False

            def __init__(self, snapshot: dict) -> None:
                self._snapshot = snapshot
                self.settings = AppSettings(
                    mode="coordinator",
                    coordinator_url="",
                    master_id="master_test_01",
                    master_name="MASTER-TEST-01",
                    default_worker_port=9000,
                    worker_poll_interval_ms=1000,
                    worker_timeout_seconds=5,
                    default_block_size=2,
                    max_block_retries=2,
                    workers=[],
                )

            def get_snapshot(self) -> dict:
                return self._snapshot

            def tick(self) -> dict:
                return self._snapshot

            def update_job_options(self, auto_add_workers: bool, redistribute_offline_blocks: bool) -> None:
                return

        snapshot = {
            "workers": [
                {
                    "id": "worker_fractional",
                    "name": "WORKER-01",
                    "ip": "127.0.0.1",
                    "port": 9000,
                    "enabled": True,
                    "selected": True,
                    "status": "rendering",
                    "blenderFound": True,
                    "blenderVersion": "4.3",
                    "gpu": "Test GPU",
                    "renderDevicePreference": "GPU",
                    "effectiveUseGpu": True,
                    "blendAccessible": True,
                    "outputAccessible": True,
                    "progress": 0.75,
                    "currentFrame": 15,
                    "lastLog": "",
                    "error": None,
                }
            ],
            "job": {
                "jobId": "job_fractional",
                "jobName": "job_fractional",
                "blendFile": str(self.base_dir / "shared" / "scene.blend"),
                "startFrame": 10,
                "endFrame": 20,
                "status": "running",
                "blocks": [
                    {
                        "blockId": "block_fractional",
                        "startFrame": 10,
                        "endFrame": 20,
                    }
                ],
            },
            "queue": [],
            "metrics": {
                "job_name": "job_fractional",
                "blend_file": str(self.base_dir / "shared" / "scene.blend"),
                "total_frames": 20,
                "completed_frames": 10,
                "pending_frames": 5,
                "rendering_frames": 5,
                "error_frames": 0,
                "overall_percent": 0.5,
                "elapsed_seconds": 15.0,
                "eta_seconds": 15.0,
                "machines_online": 1,
                "machines_rendering": 1,
            },
            "logs": {"general": "Fractional progress snapshot", "workers": {}},
        }

        snapshot["workers"][0]["currentBlockId"] = "block_fractional"

        controller = StaticController(snapshot)
        window = MainWindow(
            base_dir=self.base_dir,
            job_manager=controller,
            persistence=self.persistence,
            logger=self.logger,
            default_block_size=2,
            default_worker_port=9000,
            poll_interval_ms=5000,
        )
        try:
            window._apply_snapshot(snapshot)
            self.assertEqual(window.general_progress_bar.value(), 50)
            progress_widget = window.workers_table.cellWidget(0, 12)
            self.assertIsInstance(progress_widget, QProgressBar)
            self.assertEqual(progress_widget.value(), 75)
        finally:
            window.close()

    def test_main_window_estimates_progress_when_worker_omits_current_block_id(self) -> None:
        class StaticController:
            mode = "coordinator"
            supports_local_jobs = False
            supports_local_job_files = False
            supports_worker_selection = False
            supports_worker_device_update = False
            supports_rescan = False

            def __init__(self, snapshot: dict) -> None:
                self._snapshot = snapshot
                self.settings = AppSettings(
                    mode="coordinator",
                    coordinator_url="",
                    master_id="master_test_01",
                    master_name="MASTER-TEST-01",
                    default_worker_port=9000,
                    worker_poll_interval_ms=1000,
                    worker_timeout_seconds=5,
                    default_block_size=2,
                    max_block_retries=2,
                    workers=[],
                )

            def get_snapshot(self) -> dict:
                return self._snapshot

            def tick(self) -> dict:
                return self._snapshot

            def update_job_options(self, auto_add_workers: bool, redistribute_offline_blocks: bool) -> None:
                return

        snapshot = {
            "workers": [
                {
                    "id": "worker_missing_block",
                    "name": "WORKER-MISSING-BLOCK",
                    "ip": "127.0.0.1",
                    "port": 9000,
                    "enabled": True,
                    "selected": True,
                    "status": "rendering",
                    "blenderFound": True,
                    "blenderVersion": "4.3",
                    "gpu": "Test GPU",
                    "renderDevicePreference": "GPU",
                    "effectiveUseGpu": True,
                    "blendAccessible": True,
                    "outputAccessible": True,
                    "progress": 0.0,
                    "currentFrame": None,
                    "lastLog": "",
                    "error": None,
                }
            ],
            "job": {
                "jobId": "job_missing_block",
                "jobName": "job_missing_block",
                "blendFile": str(self.base_dir / "shared" / "scene.blend"),
                "startFrame": 1,
                "endFrame": 10,
                "status": "running",
                "blocks": [
                    {
                        "blockId": "block_assigned",
                        "workerId": "worker_missing_block",
                        "startFrame": 1,
                        "endFrame": 1,
                        "status": "assigned",
                    }
                ],
            },
            "queue": [],
            "metrics": {
                "job_name": "job_missing_block",
                "blend_file": str(self.base_dir / "shared" / "scene.blend"),
                "total_frames": 10,
                "completed_frames": 0,
                "pending_frames": 9,
                "rendering_frames": 1,
                "error_frames": 0,
                "overall_percent": 0.0,
                "elapsed_seconds": 2.0,
                "eta_seconds": 18.0,
                "machines_online": 1,
                "machines_rendering": 1,
            },
            "logs": {"general": "Missing block id snapshot", "workers": {}},
        }

        controller = StaticController(snapshot)
        window = MainWindow(
            base_dir=self.base_dir,
            job_manager=controller,
            persistence=self.persistence,
            logger=self.logger,
            default_block_size=2,
            default_worker_port=9000,
            poll_interval_ms=5000,
        )
        try:
            window._apply_snapshot(snapshot)
            progress_widget = window.workers_table.cellWidget(0, 12)
            self.assertIsInstance(progress_widget, QProgressBar)
            self.assertEqual(progress_widget.value(), 50)
            self.assertGreater(window.general_progress_bar.value(), 0)
        finally:
            window.close()

    def test_coordinator_manager_normalizes_snake_case_state_aliases(self) -> None:
        manager = self._build_coordinator_manager(None)
        try:
            payload = {
                "workers": [
                    {
                        "worker_id": "worker_snake",
                        "machine_name": "WORKER-SNAKE",
                        "ip": "127.0.0.1",
                        "port": 9000,
                        "status": "rendering",
                        "current_job_id": "job_snake",
                        "current_block_id": "frame_000010",
                        "current_frame": 10,
                        "progress": 0.5,
                        "last_log": "rendering frame 10",
                        "error": None,
                        "blender_found": True,
                        "blender_version": "4.3",
                        "gpu": "Snake GPU",
                        "render_device_preference": "GPU",
                        "effective_use_gpu": True,
                    }
                ],
                "job": {
                    "job_id": "job_snake",
                    "job_name": "job_snake",
                    "blend_file": str(self.base_dir / "shared" / "snake.blend"),
                    "start_frame": 1,
                    "end_frame": 10,
                    "status": "running",
                    "completed_frame_count": 1,
                    "overall_percent": 0.0,
                    "blocks": [
                        {
                            "block_id": "frame_000010",
                            "worker_id": "worker_snake",
                            "status": "leased",
                            "start_frame": 10,
                            "end_frame": 10,
                        }
                    ],
                },
                "queue": [],
                "metrics": {
                    "total_frames": 10,
                    "completed_frames": 1,
                    "pending_frames": 8,
                    "rendering_frames": 1,
                    "error_frames": 0,
                    "overall_percent": 0.0,
                    "machines_online": 1,
                    "machines_rendering": 1,
                },
                "logs": {"general": "snake_case snapshot"},
            }

            snapshot = manager._normalize_state(payload)  # noqa: SLF001
            worker = snapshot["workers"][0]
            job = snapshot["job"]

            self.assertEqual(worker["id"], "worker_snake")
            self.assertEqual(worker["name"], "WORKER-SNAKE")
            self.assertEqual(worker["currentJobId"], "job_snake")
            self.assertEqual(worker["currentBlockId"], "frame_000010")
            self.assertEqual(worker["currentFrame"], 10)
            self.assertEqual(worker["progress"], 50.0)
            self.assertEqual(worker["lastLog"], "rendering frame 10")
            self.assertEqual(worker["renderDevicePreference"], "GPU")
            self.assertTrue(worker["effectiveUseGpu"])

            self.assertIsNotNone(job)
            self.assertEqual(job["jobId"], "job_snake")
            self.assertEqual(job["jobName"], "job_snake")
            self.assertEqual(job["blendFile"], str(self.base_dir / "shared" / "snake.blend"))
            self.assertEqual(job["startFrame"], 1)
            self.assertEqual(job["endFrame"], 10)
            self.assertEqual(job["blocks"][0]["blockId"], "frame_000010")
            self.assertEqual(job["blocks"][0]["workerId"], "worker_snake")
            self.assertEqual(job["blocks"][0]["status"], "assigned")
            self.assertEqual(snapshot["metrics"]["overall_percent"], 10.0)
        finally:
            manager.close()
