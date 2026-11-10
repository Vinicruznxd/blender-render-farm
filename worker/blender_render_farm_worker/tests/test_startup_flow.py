from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import create_app
from app.blender_runner import BlenderInfo


class StartupFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)

    def _run_startup(self, app) -> None:
        async def run_events() -> None:
            for handler in app.router.on_startup:
                result = handler()
                if asyncio.iscoroutine(result):
                    await result

        asyncio.run(run_events())

    def _run_shutdown(self, app) -> None:
        async def run_events() -> None:
            for handler in app.router.on_shutdown:
                result = handler()
                if asyncio.iscoroutine(result):
                    await result

        asyncio.run(run_events())

    def _close_app_logger(self, app) -> None:
        logger = app.state.logger
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    def test_startup_failure_before_ready_marks_error_and_raises(self) -> None:
        app = create_app(self.project_root)
        self.addCleanup(self._close_app_logger, app)

        with patch("app.api.configure_windows_error_mode", side_effect=RuntimeError("startup boom")):
            with self.assertRaises(RuntimeError):
                self._run_startup(app)

        snapshot = app.state.status_manager.status_snapshot()
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["apiServerState"], "error")
        self.assertIn("startup boom", str(snapshot["startupError"]))
        self.assertIn("startup boom", str(snapshot["apiServerMessage"]))
        self.assertIn("startup boom", str(snapshot["error"]))

    def test_startup_becomes_ready_before_background_blender_detection_finishes(self) -> None:
        app = create_app(self.project_root)
        self.addCleanup(self._close_app_logger, app)
        app.state.capabilities_service.refresh = lambda: None  # type: ignore[method-assign]
        app.state.coordinator_discovery_service.start = lambda: None  # type: ignore[method-assign]
        app.state.coordinator_discovery_service.stop = lambda: None  # type: ignore[method-assign]

        detect_started = threading.Event()
        allow_detect_finish = threading.Event()

        def fake_detect_blender() -> BlenderInfo:
            detect_started.set()
            allow_detect_finish.wait(timeout=2)
            info = BlenderInfo(found=True, path="dummy_blender.exe", version="5.0.0")
            app.state.blender_runner._blender_info = info
            app.state.status_manager.set_blender_info(True, info.path, info.version)
            return info

        app.state.blender_runner.detect_blender = fake_detect_blender  # type: ignore[method-assign]

        started_at = time.monotonic()
        self._run_startup(app)
        elapsed = time.monotonic() - started_at

        snapshot = app.state.status_manager.status_snapshot()
        self.assertLess(elapsed, 0.5)
        self.assertEqual(snapshot["status"], "idle")
        self.assertEqual(snapshot["apiServerState"], "running")
        self.assertTrue(detect_started.wait(timeout=1))
        self.assertIsNotNone(app.state.background_startup_thread)

        allow_detect_finish.set()
        background_thread = app.state.background_startup_thread
        if background_thread is not None:
            background_thread.join(timeout=2)

        self._run_shutdown(app)


if __name__ == "__main__":
    unittest.main()
