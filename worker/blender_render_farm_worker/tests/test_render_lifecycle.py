from __future__ import annotations

from contextlib import redirect_stdout
from contextlib import contextmanager
import io
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.blender_runner import BlenderRunner, BlenderSelectionResult, build_output_path
from app.config import ConfigManager
from app.models import RenderBlockRequest
from app.status_manager import WorkerStatusManager


class RenderLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.logger = logging.getLogger(f"tests.render_lifecycle.{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())

        self.config_manager = ConfigManager(self.project_root / "config.json")
        self.config = self.config_manager.config
        self.status_manager = WorkerStatusManager(self.config)
        self.status_manager.set_ready()
        self.runner = BlenderRunner(
            project_root=self.project_root,
            config=self.config,
            config_manager=self.config_manager,
            status_manager=self.status_manager,
            logger=self.logger,
        )

    def test_render_enters_rendering_and_clears_state_on_success(self) -> None:
        request = self._make_request(start_frame=1, end_frame=2)
        frame_1 = build_output_path(request.outputPattern, 1, request.format)
        frame_2 = build_output_path(request.outputPattern, 2, request.format)
        command = self._python_command(
            f"from pathlib import Path; "
            f"frame_1 = Path(r'{frame_1}'); "
            f"frame_2 = Path(r'{frame_2}'); "
            "import sys,time;"
            "print('Fra:1'); sys.stdout.flush();"
            "print('Sample 64/128'); sys.stdout.flush();"
            "frame_1.parent.mkdir(parents=True, exist_ok=True);"
            "frame_1.write_text('rendered-1', encoding='utf-8');"
            "print('Saved: frame_0001.png'); sys.stdout.flush();"
            "time.sleep(0.8);"
            "print('Fra:2'); sys.stdout.flush();"
            "print('Sample 32/128'); sys.stdout.flush();"
            "frame_2.parent.mkdir(parents=True, exist_ok=True);"
            "frame_2.write_text('rendered-2', encoding='utf-8');"
            "print('Saved: frame_0002.png'); sys.stdout.flush();"
            "time.sleep(0.4)"
        )

        with self._patch_successful_blender(command):
            accepted, error = self.runner.start_render(request)
            self.assertTrue(accepted)
            self.assertIsNone(error)

            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "rendering")
            rendering_snapshot = self.status_manager.status_snapshot()
            self.assertEqual(rendering_snapshot["jobId"], request.jobId)
            self.assertEqual(rendering_snapshot["blockId"], request.blockId)
            self.assertEqual(rendering_snapshot["currentJob"], request.jobId)
            self.assertEqual(rendering_snapshot["currentBlock"], request.blockId)

            self._wait_until(
                lambda: (
                    self.status_manager.status_snapshot()["currentFrame"] == 1
                    and float(self.status_manager.status_snapshot()["progress"]) > 0.0
                )
            )
            progress_snapshot = self.status_manager.status_snapshot()
            self.assertEqual(progress_snapshot["status"], "rendering")
            self.assertEqual(progress_snapshot["currentFrame"], 1)
            self.assertGreater(float(progress_snapshot["progress"]), 0.0)
            self.assertLessEqual(float(progress_snapshot["progress"]), 50.0)

            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "idle")
            self._wait_for_runner_thread()

        final_snapshot = self.status_manager.status_snapshot()
        self.assertEqual(final_snapshot["status"], "idle")
        self.assertIsNone(final_snapshot["currentJob"])
        self.assertIsNone(final_snapshot["currentBlock"])
        self.assertIsNone(final_snapshot["jobId"])
        self.assertIsNone(final_snapshot["blockId"])
        self.assertIsNone(final_snapshot["currentFrame"])
        self.assertEqual(final_snapshot["progress"], 0.0)
        self.assertEqual(final_snapshot["elapsedSeconds"], 0)
        self.assertIsNone(final_snapshot["error"])
        self.assertIn("successfully", str(final_snapshot["lastLog"]).lower())
        self.assertEqual(final_snapshot["lastJob"]["result"], "done")
        self.assertTrue(final_snapshot["lastJob"]["outputVerified"])

    def test_single_frame_block_cleans_state_correctly(self) -> None:
        request = self._make_request(start_frame=5, end_frame=5)
        frame_5 = build_output_path(request.outputPattern, 5, request.format)
        command = self._python_command(
            f"from pathlib import Path; frame_5 = Path(r'{frame_5}'); "
            "import sys,time;"
            "print('Fra:5'); sys.stdout.flush();"
            "frame_5.parent.mkdir(parents=True, exist_ok=True);"
            "frame_5.write_text('rendered-5', encoding='utf-8');"
            "time.sleep(0.2)"
        )

        with self._patch_successful_blender(command):
            accepted, _ = self.runner.start_render(request)
            self.assertTrue(accepted)
            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "idle")
            self._wait_for_runner_thread()

        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(snapshot["status"], "idle")
        self.assertIsNone(snapshot["currentFrame"])
        self.assertEqual(snapshot["progress"], 0.0)
        self.assertEqual(snapshot["lastJob"]["startFrame"], 5)
        self.assertEqual(snapshot["lastJob"]["endFrame"], 5)
        self.assertEqual(snapshot["lastJob"]["result"], "done")
        self.assertTrue(snapshot["lastJob"]["outputVerified"])

    def test_single_frame_progress_updates_from_blender_samples(self) -> None:
        request = self._make_request(start_frame=7, end_frame=7)
        frame_7 = build_output_path(request.outputPattern, 7, request.format)
        command = self._python_command(
            f"from pathlib import Path; frame_7 = Path(r'{frame_7}'); "
            "import sys,time;"
            "print('Fra:7'); sys.stdout.flush();"
            "print('Sample 16/64'); sys.stdout.flush();"
            "time.sleep(0.2);"
            "print('Sample 32/64'); sys.stdout.flush();"
            "time.sleep(0.2);"
            "frame_7.parent.mkdir(parents=True, exist_ok=True);"
            "frame_7.write_text('rendered-7', encoding='utf-8');"
            "print('Saved: frame_0007.png'); sys.stdout.flush();"
            "time.sleep(0.2)"
        )

        with self._patch_successful_blender(command):
            accepted, error = self.runner.start_render(request)
            self.assertTrue(accepted)
            self.assertIsNone(error)

            self._wait_until(
                lambda: (
                    self.status_manager.status_snapshot()["status"] == "rendering"
                    and self.status_manager.status_snapshot()["currentFrame"] == 7
                    and float(self.status_manager.status_snapshot()["progress"]) >= 25.0
                )
            )
            rendering_snapshot = self.status_manager.status_snapshot()
            self.assertEqual(rendering_snapshot["currentFrame"], 7)
            self.assertGreaterEqual(float(rendering_snapshot["progress"]), 25.0)
            self.assertLess(float(rendering_snapshot["progress"]), 100.0)

            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "idle")
            self._wait_for_runner_thread()

        final_snapshot = self.status_manager.status_snapshot()
        self.assertEqual(final_snapshot["status"], "idle")
        self.assertIsNone(final_snapshot["currentFrame"])
        self.assertEqual(final_snapshot["progress"], 0.0)
        self.assertIn("successfully", str(final_snapshot["lastLog"]).lower())

    def test_single_frame_progress_uses_remaining_estimate_for_coarse_tile_updates(self) -> None:
        request = self._make_request(start_frame=8, end_frame=8)
        frame_8 = build_output_path(request.outputPattern, 8, request.format)
        command = self._python_command(
            f"from pathlib import Path; frame_8 = Path(r'{frame_8}'); "
            "import sys,time;"
            "print('Fra:8'); sys.stdout.flush();"
            "time.sleep(0.2);"
            "print('Remaining: 00:10.00 | Rendered 1/2 Tiles'); sys.stdout.flush();"
            "time.sleep(0.2);"
            "frame_8.parent.mkdir(parents=True, exist_ok=True);"
            "frame_8.write_text('rendered-8', encoding='utf-8');"
            "print('Saved: frame_0008.png'); sys.stdout.flush();"
            "time.sleep(0.2)"
        )

        with self._patch_successful_blender(command):
            accepted, error = self.runner.start_render(request)
            self.assertTrue(accepted)
            self.assertIsNone(error)

            self._wait_until(
                lambda: (
                    self.status_manager.status_snapshot()["status"] == "rendering"
                    and self.status_manager.status_snapshot()["currentFrame"] == 8
                    and float(self.status_manager.status_snapshot()["progress"]) > 0.0
                )
            )
            rendering_snapshot = self.status_manager.status_snapshot()
            self.assertGreater(float(rendering_snapshot["progress"]), 0.0)
            self.assertLess(float(rendering_snapshot["progress"]), 50.0)

            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "idle")
            self._wait_for_runner_thread()

    def test_unexpected_exception_does_not_leave_worker_stuck_rendering(self) -> None:
        request = self._make_request(start_frame=1, end_frame=1)

        with patch.object(
            self.runner,
            "select_blender_for_blend_file",
            return_value=BlenderSelectionResult(
                compatible=True,
                blend_version_detected="5.0",
                selected_blender_version="5.0.0",
                selected_blender_path="dummy_blender.exe",
                compatibility_reason="Selected Blender 5.0.0 at dummy_blender.exe for blend version 5.0 (exact major/minor match).",
            ),
        ), patch.object(
            self.runner,
            "_build_command",
            return_value=["dummy_blender.exe"],
        ), patch(
            "app.blender_runner.subprocess.Popen",
            side_effect=RuntimeError("simulated popen failure"),
        ):
            accepted, error = self.runner.start_render(request)
            self.assertTrue(accepted)
            self.assertIsNone(error)
            self._wait_for_runner_thread()

        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(snapshot["status"], "idle")
        self.assertIsNone(snapshot["currentJob"])
        self.assertIsNone(snapshot["currentBlock"])
        self.assertIsNone(snapshot["currentFrame"])
        self.assertEqual(snapshot["progress"], 0.0)
        self.assertIn("simulated popen failure", str(snapshot["error"]))
        self.assertEqual(snapshot["lastJob"]["result"], "error")

    def test_cancel_render_cleans_state_and_does_not_stay_rendering(self) -> None:
        request = self._make_request(start_frame=10, end_frame=12)
        command = self._python_command(
            "import sys,time;"
            "print('Fra:10'); sys.stdout.flush();"
            "time.sleep(30)"
        )

        with self._patch_successful_blender(command):
            accepted, _ = self.runner.start_render(request)
            self.assertTrue(accepted)
            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "rendering")

            response = self.runner.cancel_render()
            self.assertTrue(response["cancelled"])
            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "idle")
            self._wait_for_runner_thread()

        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(snapshot["status"], "idle")
        self.assertIsNone(snapshot["currentJob"])
        self.assertIsNone(snapshot["currentBlock"])
        self.assertIsNone(snapshot["currentFrame"])
        self.assertEqual(snapshot["progress"], 0.0)
        self.assertIsNone(snapshot["error"])
        self.assertIn("cancel", str(snapshot["lastLog"]).lower())
        self.assertEqual(snapshot["lastJob"]["result"], "cancelled")

    def test_render_uses_selected_blender_for_each_blend_file(self) -> None:
        request = self._make_request(start_frame=20, end_frame=20)
        frame_20 = build_output_path(request.outputPattern, 20, request.format)
        command = self._python_command(
            f"from pathlib import Path; frame_20 = Path(r'{frame_20}'); "
            "print('Fra:20'); "
            "frame_20.parent.mkdir(parents=True, exist_ok=True); "
            "frame_20.write_text('rendered-20', encoding='utf-8')"
        )
        captured: dict[str, str] = {}

        def capture_command(blender_path: str, *_args, **_kwargs) -> list[str]:
            captured["blender_path"] = blender_path
            return command

        with patch.object(
            self.runner,
            "select_blender_for_blend_file",
            return_value=BlenderSelectionResult(
                compatible=True,
                blend_version_detected="5.2",
                selected_blender_version="5.2.1",
                selected_blender_path=r"C:\Blender52\blender.exe",
                compatibility_reason="Selected Blender 5.2.1 at C:\\Blender52\\blender.exe for blend version 5.2 (exact major/minor match).",
            ),
        ), patch.object(
            self.runner,
            "_build_command",
            side_effect=capture_command,
        ):
            accepted, error = self.runner.start_render(request)
            self.assertTrue(accepted)
            self.assertIsNone(error)
            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "idle")
            self._wait_for_runner_thread()

        self.assertEqual(captured["blender_path"], r"C:\Blender52\blender.exe")
        self.assertEqual(
            self.status_manager.status_snapshot()["lastJob"]["selectedBlenderVersion"],
            "5.2.1",
        )
        self.assertTrue(self.status_manager.status_snapshot()["lastJob"]["outputVerified"])

    def test_render_reports_error_when_output_file_never_appears(self) -> None:
        request = self._make_request(start_frame=30, end_frame=30)
        command = self._python_command("print('Fra:30')")

        with self._patch_successful_blender(command), patch.object(
            self.runner,
            "_wait_for_output_paths",
            return_value=False,
        ):
            accepted, error = self.runner.start_render(request)
            self.assertTrue(accepted)
            self.assertIsNone(error)
            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "idle")
            self._wait_for_runner_thread()

        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(snapshot["status"], "idle")
        self.assertEqual(snapshot["lastJob"]["result"], "error")
        self.assertFalse(snapshot["lastJob"]["outputVerified"])
        self.assertIn("output file was not found", str(snapshot["lastJob"]["message"]).lower())

    def test_render_tolerates_non_utf8_blender_output(self) -> None:
        request = self._make_request(start_frame=40, end_frame=40)
        frame_40 = build_output_path(request.outputPattern, 40, request.format)
        command = self._python_command(
            f"from pathlib import Path; frame_40 = Path(r'{frame_40}'); "
            "import sys,time;"
            "sys.stdout.buffer.write(b'Fra:40\\n\\x8f\\n'); sys.stdout.flush();"
            "frame_40.parent.mkdir(parents=True, exist_ok=True);"
            "frame_40.write_text('rendered-40', encoding='utf-8');"
            "time.sleep(0.2)"
        )

        with self._patch_successful_blender(command):
            accepted, error = self.runner.start_render(request)
            self.assertTrue(accepted)
            self.assertIsNone(error)
            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "idle")
            self._wait_for_runner_thread()

        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(snapshot["status"], "idle")
        self.assertIsNone(snapshot["error"])
        self.assertEqual(snapshot["lastJob"]["result"], "done")
        self.assertTrue(snapshot["lastJob"]["outputVerified"])

    def test_gpu_failure_retries_once_on_cpu_and_persists_preference(self) -> None:
        self.config.persistCpuFallback = True
        request = self._make_request(start_frame=50, end_frame=50)
        frame_50 = build_output_path(request.outputPattern, 50, request.format)
        gpu_command = self._python_command(
            "import sys; print('GPU failed'); sys.stdout.flush(); sys.exit(1)"
        )
        cpu_command = self._python_command(
            f"from pathlib import Path; frame_50 = Path(r'{frame_50}'); "
            "import sys,time;"
            "print('Fra:50'); sys.stdout.flush();"
            "frame_50.parent.mkdir(parents=True, exist_ok=True);"
            "frame_50.write_text('rendered-50', encoding='utf-8');"
            "time.sleep(0.2)"
        )

        def build_command(_blender_path: str, _request: RenderBlockRequest, *, render_device_preference: str) -> list[str]:
            return gpu_command if render_device_preference == "GPU" else cpu_command

        with patch.object(
            self.runner,
            "select_blender_for_blend_file",
            return_value=BlenderSelectionResult(
                compatible=True,
                blend_version_detected="5.0",
                selected_blender_version="5.0.0",
                selected_blender_path="dummy_blender.exe",
                compatibility_reason="Selected Blender 5.0.0 at dummy_blender.exe for blend version 5.0 (exact major/minor match).",
            ),
        ), patch.object(
            self.runner,
            "_build_command",
            side_effect=build_command,
        ):
            accepted, error = self.runner.start_render(request)
            self.assertTrue(accepted)
            self.assertIsNone(error)
            self._wait_until(lambda: self.status_manager.status_snapshot()["status"] == "idle")
            self._wait_for_runner_thread()

        snapshot = self.status_manager.status_snapshot()
        self.assertEqual(snapshot["status"], "idle")
        self.assertEqual(snapshot["renderDevicePreference"], "CPU")
        self.assertFalse(snapshot["effectiveUseGpu"])
        self.assertEqual(snapshot["lastJob"]["result"], "done")
        self.assertEqual(snapshot["lastJob"]["renderDevicePreference"], "CPU")
        self.assertFalse(snapshot["lastJob"]["useGpu"])
        self.assertIn("retrying on cpu", str(snapshot["lastJob"]["message"]).lower())
        self.assertEqual(self.config_manager.config.renderDevicePreference, "CPU")

    def test_device_override_expression_uses_blender_cycles_preferences_as_base(self) -> None:
        class FakeDevice:
            def __init__(self, name: str, device_type: str, use: bool, device_id: str) -> None:
                self.name = name
                self.type = device_type
                self.use = use
                self.id = device_id

        class FakeCyclesPreferences:
            def __init__(self) -> None:
                self.compute_device_type = "OPTIX"
                self.devices = [
                    FakeDevice("NVIDIA RTX 4090", "OPTIX", False, "GPU-0"),
                    FakeDevice("NVIDIA RTX 4080", "OPTIX", False, "GPU-1"),
                    FakeDevice("NVIDIA RTX 4090", "CUDA", True, "GPU-0-CUDA"),
                    FakeDevice("CPU", "CPU", True, "CPU-0"),
                ]
                self.calls: list[str] = []

            def refresh_devices(self) -> None:
                self.calls.append("refresh_devices")

            def get_devices(self) -> None:
                self.calls.append("get_devices")

        class FakeScene:
            def __init__(self, device: str) -> None:
                self.cycles = types.SimpleNamespace(device=device)

        fake_cycles_preferences = FakeCyclesPreferences()
        fake_bpy = types.ModuleType("bpy")
        fake_bpy.context = types.SimpleNamespace(
            preferences=types.SimpleNamespace(
                addons={
                    "cycles": types.SimpleNamespace(preferences=fake_cycles_preferences),
                }
            )
        )
        fake_bpy.data = types.SimpleNamespace(
            scenes=[FakeScene("CPU"), FakeScene("CPU")]
        )

        output = io.StringIO()
        expression = self.runner._device_override_expression("GPU")

        with patch.dict(sys.modules, {"bpy": fake_bpy}):
            with redirect_stdout(output):
                exec(expression, {})

        self.assertEqual(fake_cycles_preferences.calls, ["refresh_devices", "get_devices"])
        self.assertTrue(all(scene.cycles.device == "GPU" for scene in fake_bpy.data.scenes))
        self.assertTrue(fake_cycles_preferences.devices[0].use)
        self.assertTrue(fake_cycles_preferences.devices[1].use)
        self.assertFalse(fake_cycles_preferences.devices[2].use)
        self.assertFalse(fake_cycles_preferences.devices[3].use)
        rendered_output = output.getvalue()
        self.assertIn("compute_device_type=OPTIX", rendered_output)
        self.assertIn("NVIDIA RTX 4090", rendered_output)
        self.assertIn("NVIDIA RTX 4080", rendered_output)
        self.assertNotIn("warning", rendered_output.lower())

    def test_build_output_path_preserves_existing_extension(self) -> None:
        self.assertEqual(
            build_output_path(
                str(self.project_root / "renders" / "frame_####.exr"),
                1,
                "PNG",
            ),
            self.project_root / "renders" / "frame_0001.exr",
        )
        self.assertEqual(
            build_output_path(
                str(self.project_root / "renders" / "frame.png"),
                2,
                "OPEN_EXR",
            ),
            self.project_root / "renders" / "frame0002.png",
        )

    def test_build_command_uses_output_pattern_verbatim(self) -> None:
        request = self._make_request(start_frame=9, end_frame=10).model_copy(
            update={
                "outputPattern": str(self.project_root / "renders" / "shot_####.png"),
            }
        )

        command = self.runner._build_command(
            "dummy_blender.exe",
            request,
            render_device_preference="GPU",
        )

        output_index = command.index("-o")
        self.assertEqual(command[output_index + 1], request.outputPattern)

    def _make_request(self, *, start_frame: int, end_frame: int) -> RenderBlockRequest:
        return RenderBlockRequest(
            jobId=f"job_{start_frame}_{end_frame}",
            blockId=f"block_{start_frame}_{end_frame}",
            blendFile=str(self.project_root / "scene.blend"),
            outputPattern=str(self.project_root / "renders" / "frame_####"),
            startFrame=start_frame,
            endFrame=end_frame,
            format="PNG",
            useGpu=True,
        )

    def _python_command(self, script: str) -> list[str]:
        return [sys.executable, "-u", "-c", script]

    @contextmanager
    def _patch_successful_blender(self, command: list[str]):
        with patch.object(
            self.runner,
            "select_blender_for_blend_file",
            return_value=BlenderSelectionResult(
                compatible=True,
                blend_version_detected="5.0",
                selected_blender_version="5.0.0",
                selected_blender_path="dummy_blender.exe",
                compatibility_reason="Selected Blender 5.0.0 at dummy_blender.exe for blend version 5.0 (exact major/minor match).",
            ),
        ), patch.object(
            self.runner,
            "_build_command",
            return_value=command,
        ):
            yield

    def _wait_for_runner_thread(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            thread = self.runner._thread
            if thread is None:
                return
            thread.join(timeout=0.1)
            if not thread.is_alive():
                return
        self.fail("Render thread did not finish in time")

    def _wait_until(self, predicate, timeout: float = 10.0, interval: float = 0.05) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(interval)
        self.fail("Timed out waiting for expected worker state")


if __name__ == "__main__":
    unittest.main()
