from __future__ import annotations

import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.blender_runner import BlenderRunner, BlenderSelectionResult, build_output_path
from app.config import ConfigManager
from app.models import RenderBlockRequest
from app.status_manager import WorkerStatusManager


class PersistentSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.logger = logging.getLogger(f"tests.persistent_session.{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())

        self.config_manager = ConfigManager(self.project_root / "config.json")
        self.config = self.config_manager.config
        self.config.persistentSessionEnabled = True
        self.config.persistentSessionIdleTimeoutSeconds = 60
        self.config.persistentSessionMaxQueueDepth = 2
        self.config.persistentSessionOpenTimeoutSeconds = 15
        self.status_manager = WorkerStatusManager(self.config)
        self.status_manager.set_ready()
        self.runner = BlenderRunner(
            project_root=self.project_root,
            config=self.config,
            config_manager=self.config_manager,
            status_manager=self.status_manager,
            logger=self.logger,
        )
        self.session_start_marker = self.project_root / "session_starts.txt"
        self.helper_script = self.project_root / "fake_persistent_blender.py"
        self.helper_script.write_text(
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "import time",
                    "from multiprocessing.connection import Client",
                    "from pathlib import Path",
                    "",
                    "session_config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))",
                    "marker_path = Path(sys.argv[2])",
                    "marker_path.parent.mkdir(parents=True, exist_ok=True)",
                    "with marker_path.open('a', encoding='utf-8') as marker_file:",
                    "    marker_file.write('start\\n')",
                    "pipe_address = session_config['pipeAddress']",
                    "auth_key = bytes.fromhex(session_config['authKeyHex'])",
                    "with Client(pipe_address, family='AF_PIPE', authkey=auth_key) as connection:",
                    "    connection.send({'type': 'ready'})",
                    "    while True:",
                    "        message = connection.recv()",
                    "        message_type = str(message.get('type') or '').strip().lower()",
                    "        if message_type == 'shutdown':",
                    "            connection.send({'type': 'shutdown_ack', 'ok': True})",
                    "            break",
                    "        if message_type != 'render':",
                    "            connection.send({'type': 'render_result', 'ok': False, 'message': 'unsupported'})",
                    "            continue",
                    "        start_frame = int(message['startFrame'])",
                    "        output_path = Path(message['expectedOutputPaths'][0])",
                    "        print(f'Fra:{start_frame}', flush=True)",
                    "        time.sleep(0.4 if start_frame == 1 else 0.1)",
                    "        output_path.parent.mkdir(parents=True, exist_ok=True)",
                    "        output_path.write_text(f'rendered-{start_frame}', encoding='utf-8')",
                    "        connection.send({'type': 'render_result', 'ok': True, 'message': 'done', 'outputPaths': message['expectedOutputPaths']})",
                ]
            ),
            encoding="utf-8",
        )
        self.runner._persistent_session_manager._build_session_command = (  # type: ignore[attr-defined]
            lambda _blender_path, _blend_file, session_config_path: [
                sys.executable,
                "-u",
                str(self.helper_script),
                str(session_config_path),
                str(self.session_start_marker),
            ]
        )

    def test_persistent_session_reuses_single_background_process_and_drains_explicitly(self) -> None:
        request_a = self._make_request(block_id="block_0001", start_frame=1, end_frame=1)
        request_b = self._make_request(block_id="block_0002", start_frame=2, end_frame=2)

        with patch.object(
            self.runner,
            "select_blender_for_blend_file",
            return_value=BlenderSelectionResult(
                compatible=True,
                blend_version_detected="5.2",
                selected_blender_version="5.2.0",
                selected_blender_path="dummy_blender.exe",
                compatibility_reason="Selected Blender 5.2.0 at dummy_blender.exe for blend version 5.2.",
            ),
        ):
            accepted_a, error_a = self.runner.start_render(request_a)
            self.assertTrue(accepted_a)
            self.assertIsNone(error_a)
            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "rendering")

            accepted_b, error_b = self.runner.start_render(request_b)
            self.assertTrue(accepted_b)
            self.assertIsNone(error_b)

            self._wait_until(
                lambda: self.status_manager.status_snapshot()["status"] == "session_open"
            )

            snapshot = self.status_manager.status_snapshot()
            self.assertEqual(snapshot["status"], "session_open")
            self.assertEqual(snapshot["currentJob"], "job_persistent")
            self.assertEqual(snapshot["sessionQueueDepth"], 0)
            self.assertEqual(snapshot["sessionState"], "session_open")
            self.assertTrue(build_output_path(request_a.outputPattern, 1, request_a.format).is_file())
            self.assertTrue(build_output_path(request_b.outputPattern, 2, request_b.format).is_file())
            self.assertEqual(
                self.session_start_marker.read_text(encoding="utf-8").splitlines(),
                ["start"],
            )

            drain_response = self.runner.request_job_drain(job_id="job_persistent", coordinator_id="coord_local_01")
            self.assertTrue(drain_response["accepted"])
            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "idle")

        final_snapshot = self.status_manager.status_snapshot()
        self.assertEqual(final_snapshot["status"], "idle")
        self.assertIsNone(final_snapshot["currentJob"])
        self.assertIsNone(final_snapshot["sessionState"])

    def test_build_persistent_session_command_uses_meipass_bootstrap_when_bundle_layout_is_used(self) -> None:
        bundled_root = self.project_root / "_internal"
        bootstrap_script = bundled_root / "app" / "blender_session_bootstrap.py"
        bootstrap_script.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_script.write_text("# bundled bootstrap\n", encoding="utf-8")

        with patch.object(sys, "_MEIPASS", str(bundled_root), create=True):
            command = self.runner._build_persistent_session_command(
                blender_path="dummy_blender.exe",
                blend_file="C:\\scene.blend",
                session_config_path=self.project_root / "session.json",
            )

        self.assertEqual(command[0], "dummy_blender.exe")
        self.assertEqual(command[4], str(bootstrap_script))

    def test_persistent_session_startup_failure_falls_back_to_direct_render_and_cools_down(self) -> None:
        self.config.persistentSessionOpenTimeoutSeconds = 1
        failing_session_script = self.project_root / "fake_persistent_blender_failure.py"
        failing_session_script.write_text(
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "import time",
                    "from pathlib import Path",
                    "",
                    "session_config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))",
                    "marker_path = Path(sys.argv[2])",
                    "marker_path.parent.mkdir(parents=True, exist_ok=True)",
                    "with marker_path.open('a', encoding='utf-8') as marker_file:",
                    "    marker_file.write(f\"{session_config['jobId']}\\n\")",
                    "print('[WorkerSession] Simulating persistent session startup hang', flush=True)",
                    "time.sleep(2.0)",
                    "sys.exit(1)",
                ]
            ),
            encoding="utf-8",
        )
        direct_helper_script = self.project_root / "fake_direct_blender.py"
        direct_helper_script.write_text(
            "\n".join(
                [
                    "import sys",
                    "from pathlib import Path",
                    "",
                    "output_path = Path(sys.argv[1])",
                    "frame = sys.argv[2]",
                    "output_path.parent.mkdir(parents=True, exist_ok=True)",
                    "print(f'Fra:{frame}', flush=True)",
                    "output_path.write_text(f'rendered-{frame}', encoding='utf-8')",
                ]
            ),
            encoding="utf-8",
        )

        self.runner._persistent_session_manager._build_session_command = (  # type: ignore[attr-defined]
            lambda _blender_path, _blend_file, session_config_path: [
                sys.executable,
                "-u",
                str(failing_session_script),
                str(session_config_path),
                str(self.session_start_marker),
            ]
        )
        self.runner._build_command = lambda _blender_path, request, render_device_preference: [  # type: ignore[method-assign]
            sys.executable,
            "-u",
            str(direct_helper_script),
            str(build_output_path(request.outputPattern, request.startFrame, request.format)),
            str(request.startFrame),
            render_device_preference,
        ]

        request_a = self._make_request(block_id="block_0101", start_frame=101, end_frame=101)
        request_b = self._make_request(block_id="block_0102", start_frame=102, end_frame=102)
        request_c = self._make_request(block_id="block_0103", start_frame=103, end_frame=103)

        with patch.object(
            self.runner,
            "select_blender_for_blend_file",
            return_value=BlenderSelectionResult(
                compatible=True,
                blend_version_detected="5.2",
                selected_blender_version="5.2.0",
                selected_blender_path=sys.executable,
                compatibility_reason=f"Selected Blender test interpreter at {sys.executable}.",
            ),
        ):
            accepted_a, error_a = self.runner.start_render(request_a)
            accepted_b, error_b = self.runner.start_render(request_b)
            self.assertTrue(accepted_a)
            self.assertIsNone(error_a)
            self.assertTrue(accepted_b)
            self.assertIsNone(error_b)

            self._wait_until(
                lambda: build_output_path(request_a.outputPattern, 101, request_a.format).is_file()
                and build_output_path(request_b.outputPattern, 102, request_b.format).is_file()
                and self.status_manager.status_snapshot()["status"] == "idle"
            )

            snapshot = self.status_manager.status_snapshot()
            self.assertFalse(snapshot["persistentSessionEnabled"])
            self.assertTrue(snapshot["persistentSessionTemporarilyDisabled"])
            self.assertIn(
                "Timed out waiting for the persistent Blender session to connect to the control pipe",
                str(snapshot["persistentSessionDisabledReason"]),
            )
            self.assertEqual(
                self.session_start_marker.read_text(encoding="utf-8").splitlines(),
                ["job_persistent"],
            )

            accepted_c, error_c = self.runner.start_render(request_c)
            self.assertTrue(accepted_c)
            self.assertIsNone(error_c)
            self._wait_until(
                lambda: build_output_path(request_c.outputPattern, 103, request_c.format).is_file()
                and self.status_manager.status_snapshot()["status"] == "idle"
            )

        self.assertEqual(
            self.session_start_marker.read_text(encoding="utf-8").splitlines(),
            ["job_persistent"],
        )

    def _make_request(self, *, block_id: str, start_frame: int, end_frame: int) -> RenderBlockRequest:
        blend_file = self.project_root / "scene.blend"
        blend_file.write_bytes(b"BLENDER17-01v0502REND")
        return RenderBlockRequest(
            jobId="job_persistent",
            blockId=block_id,
            blendFile=str(blend_file),
            outputPattern=str(self.project_root / "renders" / "frame_####"),
            startFrame=start_frame,
            endFrame=end_frame,
            format="PNG",
            useGpu=True,
            assignmentId=f"lease_{start_frame:06d}",
            coordinatorId="coord_local_01",
        )

    def _wait_until(self, predicate, *, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("Timed out waiting for persistent worker condition")


if __name__ == "__main__":
    unittest.main()
