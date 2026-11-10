from __future__ import annotations

import json
import logging
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from app.config import WorkerConfig
from app.coordinator_discovery import (
    CoordinatorDiscoveryService,
    build_candidate_api_urls,
    build_discovery_probe,
    parse_coordinator_announcement,
)
from app.status_manager import WorkerStatusManager


class FakeDiscoverySocket:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        self.sent_messages.append((data, address))


class CoordinatorHttpServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, CoordinatorRequestHandler)
        self.health_status = 200
        self.health_payload: dict[str, object] = {"ok": True, "service": "coordinator"}
        self.registration_status = 201
        self.registration_payload: dict[str, object] = {"ok": True}
        self.worker_registrations: list[dict[str, object]] = []


class CoordinatorRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_error(404)
            return

        payload = json.dumps(self.server.health_payload).encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(self.server.health_status)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/workers":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        payload = json.loads(body) if body else {}
        self.server.worker_registrations.append(payload)  # type: ignore[attr-defined]

        response_body = json.dumps(self.server.registration_payload).encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(self.server.registration_status)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class CoordinatorDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.logger = logging.getLogger(f"tests.coordinator_discovery.{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())
        self.config = WorkerConfig(
            coordinatorDiscoveryEnabled=True,
            coordinatorDiscoveryPort=8011,
        )
        self.status_manager = WorkerStatusManager(self.config)
        self.status_manager.set_ready()
        self.service = CoordinatorDiscoveryService(
            config=self.config,
            status_manager=self.status_manager,
            logger=self.logger,
        )

    def test_build_candidate_api_urls_falls_back_to_addresses(self) -> None:
        urls = build_candidate_api_urls(
            {
                "apiUrls": [],
                "addresses": ["192.168.0.10", "192.168.0.11"],
                "apiPort": 8010,
            }
        )
        self.assertEqual(
            urls,
            [
                "http://192.168.0.10:8010",
                "http://192.168.0.11:8010",
            ],
        )

    def test_send_probe_uses_required_payload(self) -> None:
        fake_socket = FakeDiscoverySocket()
        self.service._socket = fake_socket

        self.service._send_discovery_probe()

        self.assertEqual(len(fake_socket.sent_messages), 1)
        raw_payload, address = fake_socket.sent_messages[0]
        self.assertEqual(address, ("255.255.255.255", 8011))
        self.assertEqual(json.loads(raw_payload.decode("utf-8")), build_discovery_probe())

    def test_announcement_registers_worker_with_coordinator(self) -> None:
        server, server_thread = self._start_http_server()
        self.addCleanup(self._stop_http_server, server, server_thread)
        api_url = f"http://127.0.0.1:{server.server_address[1]}"

        announcement = parse_coordinator_announcement(
            {
                "protocol": "blender-render-farm-coordinator",
                "version": 1,
                "messageType": "announce",
                "coordinatorId": "coord_local_01",
                "namespace": "blender_render_farm",
                "discoveryPort": 8011,
                "apiPort": server.server_address[1],
                "startedAt": "2026-05-25T12:00:00Z",
                "hostname": "COORD-01",
                "addresses": ["127.0.0.1"],
                "apiUrls": [api_url],
                "enabled": True,
                "status": "running",
            }
        )
        self.assertIsNotNone(announcement)

        self.service._handle_announcement(announcement)  # type: ignore[arg-type]
        self.service._maybe_monitor_coordinator(time.monotonic() + 10)

        self.assertEqual(len(server.worker_registrations), 1)
        registration = server.worker_registrations[0]
        self.assertEqual(registration["ip"], self.status_manager.ip)
        self.assertEqual(registration["port"], self.status_manager.port)
        self.assertEqual(registration["name"], self.status_manager.machine_name)
        self.assertEqual(registration["enabled"], self.status_manager.allow_jobs)
        self.assertEqual(
            registration["renderDevicePreference"],
            self.status_manager.render_device_preference,
        )

        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(snapshot["coordinatorDiscoveryStatus"], "registered")
        self.assertTrue(snapshot["coordinatorConnected"])
        self.assertEqual(snapshot["coordinatorId"], "coord_local_01")
        self.assertEqual(snapshot["registeredCoordinatorId"], "coord_local_01")
        self.assertEqual(snapshot["coordinatorApiUrl"], api_url)

    def test_health_failure_clears_binding_and_returns_to_discovery(self) -> None:
        server, server_thread = self._start_http_server()
        self.addCleanup(self._stop_http_server, server, server_thread)
        api_url = f"http://127.0.0.1:{server.server_address[1]}"

        announcement = parse_coordinator_announcement(
            {
                "protocol": "blender-render-farm-coordinator",
                "version": 1,
                "messageType": "announce",
                "coordinatorId": "coord_local_01",
                "namespace": "blender_render_farm",
                "discoveryPort": 8011,
                "apiPort": server.server_address[1],
                "startedAt": "2026-05-25T12:00:00Z",
                "hostname": "COORD-01",
                "addresses": ["127.0.0.1"],
                "apiUrls": [api_url],
                "enabled": True,
                "status": "running",
            }
        )
        self.service._handle_announcement(announcement)  # type: ignore[arg-type]
        self.service._maybe_monitor_coordinator(time.monotonic() + 10)

        server.health_status = 503
        self.service._maybe_monitor_coordinator(time.monotonic() + 20)

        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(snapshot["coordinatorDiscoveryStatus"], "discovering")
        self.assertFalse(snapshot["coordinatorConnected"])
        self.assertIsNone(snapshot["coordinatorId"])
        self.assertIsNone(snapshot["coordinatorApiUrl"])

    def test_health_syncs_coordinator_language(self) -> None:
        server, server_thread = self._start_http_server()
        self.addCleanup(self._stop_http_server, server, server_thread)
        server.health_payload = {"ok": True, "service": "coordinator", "language": "es"}
        api_url = f"http://127.0.0.1:{server.server_address[1]}"

        self.assertTrue(self.service._validate_coordinator_health(api_url))
        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(snapshot["language"], "es")
        self.assertEqual(self.config.language, "es")

    def test_successful_health_probe_updates_last_seen(self) -> None:
        from unittest.mock import patch
        server, server_thread = self._start_http_server()
        self.addCleanup(self._stop_http_server, server, server_thread)
        api_url = f"http://127.0.0.1:{server.server_address[1]}"
        announcement = parse_coordinator_announcement({
            "protocol": "blender-render-farm-coordinator", "version": 1,
            "messageType": "announce", "coordinatorId": "health_qa",
            "namespace": "blender_render_farm", "discoveryPort": 8011,
            "apiPort": server.server_address[1], "hostname": "HEALTH-QA",
            "addresses": ["127.0.0.1"], "apiUrls": [api_url], "enabled": True,
        })
        self.service._handle_announcement(announcement)
        self.service._maybe_monitor_coordinator(time.monotonic() + 10)
        with patch("app.coordinator_discovery.utc_now_iso", return_value="2030-01-01T00:00:00+00:00"):
            self.service._maybe_monitor_coordinator(time.monotonic() + 20)
        self.assertEqual(self.status_manager.status_snapshot()["coordinatorLastSeenAt"], "2030-01-01T00:00:00+00:00")

    def test_bind_failure_reports_same_host_port_conflict_hint(self) -> None:
        bind_error = PermissionError(
            13,
            "An attempt was made to access a socket in a way forbidden by its access permissions",
            None,
            10013,
            None,
        )

        def fail_bind() -> None:
            raise bind_error

        probe_calls = 0

        def fake_probe() -> None:
            nonlocal probe_calls
            probe_calls += 1
            self.service._stop_event.set()

        self.service._create_discovery_socket = fail_bind  # type: ignore[method-assign]
        self.service._probe_for_announcements = fake_probe  # type: ignore[method-assign]

        self.service._run_loop()

        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(probe_calls, 1)
        self.assertEqual(snapshot["coordinatorDiscoveryStatus"], "discovering")
        self.assertIn("could not bind UDP socket on port 8011", snapshot["coordinatorError"])
        self.assertIn("same Windows machine", snapshot["coordinatorError"])
        self.assertIn("UDP 8011 exclusively", snapshot["coordinatorError"])
        self.assertIn("Falling back to active probe mode", snapshot["coordinatorError"])

    def test_active_probe_targets_include_broadcast_and_localhost(self) -> None:
        targets = self.service._build_probe_targets()

        self.assertEqual(targets[0], "255.255.255.255")
        self.assertIn("127.0.0.1", targets)

    def test_render_status_preserves_registered_coordinator_when_job_has_no_metadata(self) -> None:
        self.status_manager.set_coordinator_registered(
            coordinator_id="coord_local_01",
            api_url="http://192.168.0.10:8010",
            hostname="COORD-01",
            last_seen_at="2026-05-25T12:00:00Z",
            last_registered_at="2026-05-25T12:00:01Z",
        )
        self.status_manager.begin_render_block(
            {
                "jobId": "job_test",
                "blockId": "frame_000045",
                "blendFile": r"X:\Projetos\Cena01\cena.blend",
                "outputPattern": r"X:\Projetos\Cena01\renders\frame_####",
                "startFrame": 45,
                "endFrame": 45,
                "format": "PNG",
                "useGpu": True,
                "requestedUseGpu": True,
                "renderDevicePreference": "GPU",
                "assignmentId": "lease_000045",
                "coordinatorId": None,
                "submittedByMasterId": None,
                "submittedByMasterName": None,
            }
        )

        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(snapshot["status"], "rendering")
        self.assertEqual(snapshot["coordinatorId"], "coord_local_01")
        self.assertEqual(snapshot["registeredCoordinatorId"], "coord_local_01")
        self.assertIsNone(snapshot["jobCoordinatorId"])

    def _start_http_server(self) -> tuple[CoordinatorHttpServer, Thread]:
        server = CoordinatorHttpServer(("127.0.0.1", 0))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _stop_http_server(self, server: CoordinatorHttpServer, thread: Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
