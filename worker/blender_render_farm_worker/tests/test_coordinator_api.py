from __future__ import annotations

import json
import logging
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from app.api import create_app
from app.blender_runner import BlenderInfo, BlenderSelectionResult


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveWorkerHarness:
    def __init__(self, project_root: Path, port: int) -> None:
        self.project_root = project_root
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.app = create_app(project_root)
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def configure_blender_command(self, command: list[str]) -> None:
        runner = self.app.state.blender_runner
        blender_info = BlenderInfo(found=True, path="dummy_blender.exe", version="5.2.0")

        def fake_detect_blender() -> BlenderInfo:
            runner._blender_info = blender_info
            self.app.state.status_manager.set_blender_info(
                True,
                blender_info.path,
                blender_info.version,
            )
            return blender_info

        runner.detect_blender = fake_detect_blender  # type: ignore[method-assign]
        runner.ensure_blender_available = lambda: blender_info  # type: ignore[method-assign]
        runner.select_blender_for_blend_file = lambda _blend_file, **_kwargs: BlenderSelectionResult(  # type: ignore[method-assign]
            compatible=True,
            blend_version_detected="5.2",
            required_blender_version="5.2",
            selected_blender_version="5.2.0",
            selected_blender_path=blender_info.path,
            compatibility_reason=(
                f"Selected Blender {blender_info.version} at {blender_info.path} "
                "for blend version 5.2 (exact major/minor match)."
            ),
        )
        runner._build_command = lambda *_args, **_kwargs: command  # type: ignore[method-assign]

    def start(self) -> None:
        config = uvicorn.Config(
            app=self.app,
            host="127.0.0.1",
            port=self.port,
            log_config=None,
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                try:
                    self.request("GET", "/health")
                    return
                except Exception:
                    time.sleep(0.05)
                    continue
            time.sleep(0.05)
        raise RuntimeError("Worker test server did not start in time")

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=15)
        logger = getattr(self.app.state, "logger", None)
        if logger is not None:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        expect_error: bool = False,
    ) -> tuple[int, dict[str, object]]:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body)
        except urllib.error.HTTPError as exc:
            if not expect_error:
                raise
            body = exc.read().decode("utf-8")
            return exc.code, json.loads(body)


class CoordinatorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.port = find_free_port()
        self._write_config()
        self.blend_file = self.project_root / "scene.blend"
        self.output_dir = self.project_root / "renders"
        self.blend_file.write_bytes(b"BLENDER17-01v0502REND")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.harness = LiveWorkerHarness(self.project_root, self.port)
        self.addCleanup(self.harness.stop)

    def test_accepts_old_render_block_payload_and_cleans_state(self) -> None:
        frame_1 = self.output_dir / "frame_0001.png"
        self.harness.configure_blender_command(
            self._python_command(
                f"from pathlib import Path; frame = Path(r'{frame_1}'); "
                "import sys,time;"
                "print('Fra:1'); sys.stdout.flush();"
                "frame.parent.mkdir(parents=True, exist_ok=True);"
                "frame.write_text('rendered-1', encoding='utf-8');"
                "time.sleep(0.3)"
            )
        )
        self.harness.start()

        status_code, payload = self.harness.request(
            "POST",
            "/render-block",
            self._render_payload(start_frame=1, end_frame=1),
        )
        self.assertEqual(status_code, 202)
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["status"], "rendering")
        self.assertEqual(payload["blendVersionDetected"], "5.2")
        self.assertEqual(payload["selectedBlenderVersion"], "5.2.0")
        self.assertEqual(payload["selectedBlenderPath"], "dummy_blender.exe")

        idle_status = self._wait_for_status("idle")
        self.assertIsNone(idle_status["currentJob"])
        self.assertIsNone(idle_status["currentBlock"])
        self.assertIsNone(idle_status["jobId"])
        self.assertEqual(idle_status["progress"], 0.0)

    def test_new_payload_exposes_coordinator_metadata_and_busy_409(self) -> None:
        frame_45 = self.output_dir / "frame_0045.png"
        self.harness.configure_blender_command(
            self._python_command(
                f"from pathlib import Path; frame = Path(r'{frame_45}'); "
                "import sys,time;"
                "print('Fra:45'); sys.stdout.flush();"
                "frame.parent.mkdir(parents=True, exist_ok=True);"
                "frame.write_text('rendered-45', encoding='utf-8');"
                "time.sleep(2.0)"
            )
        )
        self.harness.start()

        status_code, accepted = self.harness.request(
            "POST",
            "/render-block",
            self._render_payload(
                start_frame=45,
                end_frame=45,
                assignment_id="lease_000045",
                coordinator_id="coord_local_01",
                master_id="master_a",
                master_name="MASTER-A",
            ),
        )
        self.assertEqual(status_code, 202)
        self.assertEqual(accepted["assignmentId"], "lease_000045")
        self.assertEqual(accepted["coordinatorId"], "coord_local_01")

        rendering_status = self._wait_for_status("rendering")
        self.assertEqual(rendering_status["assignmentId"], "lease_000045")
        self.assertEqual(rendering_status["coordinatorId"], "coord_local_01")
        self.assertEqual(rendering_status["submittedByMasterId"], "master_a")
        self.assertEqual(rendering_status["submittedByMasterName"], "MASTER-A")

        busy_status_code, busy_payload = self.harness.request(
            "POST",
            "/render-block",
            self._render_payload(start_frame=46, end_frame=46),
            expect_error=True,
        )
        self.assertEqual(busy_status_code, 409)
        self.assertFalse(busy_payload["accepted"])
        self.assertFalse(busy_payload["ok"])
        self.assertEqual(busy_payload["status"], "busy")
        self.assertEqual(busy_payload["message"], "Worker ja esta renderizando.")
        self.assertEqual(busy_payload["currentJobId"], "job_45_45")
        self.assertEqual(busy_payload["currentBlockId"], "block_45_45")
        self.assertEqual(busy_payload["assignmentId"], "lease_000045")

        self._wait_for_status("idle")

    def test_cancel_honors_assignment_id_match(self) -> None:
        self.harness.configure_blender_command(
            self._python_command(
                "import sys,time;"
                "print('Fra:100'); sys.stdout.flush();"
                "time.sleep(10)"
            )
        )
        self.harness.start()

        self.harness.request(
            "POST",
            "/render-block",
            self._render_payload(
                start_frame=100,
                end_frame=100,
                assignment_id="lease_001000",
                coordinator_id="coord_local_01",
            ),
        )
        self._wait_for_status("rendering")

        wrong_cancel_status, wrong_cancel_payload = self.harness.request(
            "POST",
            "/cancel",
            {"assignmentId": "lease_wrong"},
        )
        self.assertEqual(wrong_cancel_status, 200)
        self.assertFalse(wrong_cancel_payload["cancelled"])
        self.assertEqual(
            wrong_cancel_payload["message"],
            "active render does not match requested assignmentId",
        )
        still_rendering = self._wait_for_status("rendering")
        self.assertEqual(still_rendering["assignmentId"], "lease_001000")

        cancel_status, cancel_payload = self.harness.request(
            "POST",
            "/cancel",
            {"assignmentId": "lease_001000"},
        )
        self.assertEqual(cancel_status, 200)
        self.assertTrue(cancel_payload["cancelled"])
        self.assertEqual(cancel_payload["assignmentId"], "lease_001000")

        idle_status = self._wait_for_status("idle")
        self.assertIsNone(idle_status["currentJob"])
        self.assertIsNone(idle_status["currentBlock"])
        self.assertIsNone(idle_status["assignmentId"])
        self.assertEqual(idle_status["progress"], 0.0)

    def test_status_during_render_exposes_master_progress_contract(self) -> None:
        frame_12 = self.output_dir / "master_frame_0012.png"
        self.harness.configure_blender_command(
            self._python_command(
                f"from pathlib import Path; frame = Path(r'{frame_12}'); "
                "import sys,time;"
                "print('Fra:12'); sys.stdout.flush();"
                "print('Sample 64/256'); sys.stdout.flush();"
                "time.sleep(0.4);"
                "print('Sample 128/256'); sys.stdout.flush();"
                "time.sleep(0.4);"
                "frame.parent.mkdir(parents=True, exist_ok=True);"
                "frame.write_text('rendered-12', encoding='utf-8');"
                "print('Saved: master_frame_0012.png'); sys.stdout.flush();"
                "time.sleep(0.3)"
            )
        )
        self.harness.start()

        payload = self._render_payload(start_frame=12, end_frame=12)
        payload["outputPattern"] = str(self.output_dir / "master_frame_####.png")
        status_code, accepted = self.harness.request("POST", "/render-block", payload)
        self.assertEqual(status_code, 202)
        self.assertTrue(accepted["accepted"])

        rendering_status = self._wait_for_status_payload(
            lambda item: (
                item["status"] == "rendering"
                and item["jobId"] == "job_12_12"
                and item["blockId"] == "block_12_12"
                and item["currentFrame"] == 12
                and float(item["progress"]) > 0.0
            )
        )

        for field in [
            "machineName",
            "status",
            "jobId",
            "currentJob",
            "blockId",
            "currentBlock",
            "currentFrame",
            "progress",
            "lastLog",
            "error",
            "blenderFound",
            "blenderVersion",
            "gpu",
            "renderDevicePreference",
            "effectiveUseGpu",
        ]:
            self.assertIn(field, rendering_status)

        self.assertEqual(rendering_status["jobId"], "job_12_12")
        self.assertEqual(rendering_status["currentJob"], "job_12_12")
        self.assertEqual(rendering_status["blockId"], "block_12_12")
        self.assertEqual(rendering_status["currentBlock"], "block_12_12")
        self.assertEqual(rendering_status["currentFrame"], 12)
        self.assertGreater(float(rendering_status["progress"]), 0.0)
        self.assertTrue(rendering_status["blenderFound"])
        self.assertEqual(rendering_status["blenderVersion"], "5.2.0")
        self.assertEqual(rendering_status["renderDevicePreference"], "GPU")
        self.assertTrue(rendering_status["effectiveUseGpu"])
        self.assertIn("Sample", str(rendering_status["lastLog"]))

        idle_status = self._wait_for_status("idle")
        self.assertEqual(idle_status["status"], "idle")
        self.assertIsNone(idle_status["currentFrame"])
        self.assertEqual(idle_status["progress"], 0.0)
        self.assertIn("successfully", str(idle_status["lastLog"]).lower())

    def _write_config(self) -> None:
        config = {
            "port": self.port,
            "host": "127.0.0.1",
            "workerName": "TEST-WORKER",
            "blenderPath": "",
            "coordinatorDiscoveryEnabled": False,
            "coordinatorDiscoveryPort": 8011,
            "renderDevicePreference": "GPU",
            "showRenderDeviceWindow": False,
            "allowJobs": True,
            "authToken": "",
            "logLevel": "INFO",
        }
        self.project_root.mkdir(parents=True, exist_ok=True)
        (self.project_root / "config.json").write_text(
            json.dumps(config, indent=2),
            encoding="utf-8",
        )

    def _render_payload(
        self,
        *,
        start_frame: int,
        end_frame: int,
        assignment_id: str | None = None,
        coordinator_id: str | None = None,
        master_id: str | None = None,
        master_name: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "jobId": f"job_{start_frame}_{end_frame}",
            "blockId": f"block_{start_frame}_{end_frame}",
            "blendFile": str(self.blend_file),
            "outputPattern": str(self.output_dir / "frame_####"),
            "startFrame": start_frame,
            "endFrame": end_frame,
            "format": "PNG",
            "useGpu": True,
        }
        if assignment_id is not None:
            payload["assignmentId"] = assignment_id
        if coordinator_id is not None:
            payload["coordinatorId"] = coordinator_id
        if master_id is not None:
            payload["submittedByMasterId"] = master_id
        if master_name is not None:
            payload["submittedByMasterName"] = master_name
        return payload

    def _python_command(self, script: str) -> list[str]:
        return [sys.executable, "-u", "-c", script]

    def _wait_for_status(self, expected_status: str, timeout: float = 15.0) -> dict[str, object]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, payload = self.harness.request("GET", "/status")
            if payload["status"] == expected_status:
                return payload
            time.sleep(0.05)
        self.fail(f"Timed out waiting for worker status {expected_status}")

    def _wait_for_status_payload(
        self,
        predicate,
        timeout: float = 15.0,
    ) -> dict[str, object]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, payload = self.harness.request("GET", "/status")
            if predicate(payload):
                return payload
            time.sleep(0.05)
        self.fail("Timed out waiting for expected /status payload")


if __name__ == "__main__":
    unittest.main()
