from __future__ import annotations

import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import WorkerConfig
from app.status_manager import WorkerStatusManager
import worker


class FakeRenderDeviceWindow:
    def __init__(self) -> None:
        self.run_called = False
        self.show_startup_error_called = False
        self.startup_error_message: str | None = None

    def run(self, *, on_close=None) -> bool:
        self.run_called = True
        if on_close is not None:
            on_close()
        return True

    def show_startup_error(self, message: str) -> bool:
        self.show_startup_error_called = True
        self.startup_error_message = message
        return True


class FakeServer:
    def __init__(self, *, should_start: bool) -> None:
        self.started = False
        self.should_exit = False
        self._should_start = should_start

    def run(self) -> None:
        if self._should_start:
            self.started = True
            while not self.should_exit:
                time.sleep(0.01)


class WorkerStartupTests(unittest.TestCase):
    def _make_app(self, *, show_window: bool = True):
        config = WorkerConfig(showRenderDeviceWindow=show_window, host="127.0.0.1", port=9000)
        status_manager = WorkerStatusManager(config)
        logger = logging.getLogger(f"tests.worker_startup.{id(self)}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        window = FakeRenderDeviceWindow()
        state = SimpleNamespace(
            config=config,
            status_manager=status_manager,
            logger=logger,
            render_device_window=window,
        )
        return SimpleNamespace(state=state), status_manager, window

    def test_main_shows_startup_error_instead_of_opening_normal_window_when_server_never_starts(self) -> None:
        fake_app, status_manager, window = self._make_app()
        fake_server = FakeServer(should_start=False)

        with patch("worker.build_server", return_value=fake_server):
            worker.main(application=fake_app)

        snapshot = status_manager.status_snapshot()
        self.assertFalse(window.run_called)
        self.assertTrue(window.show_startup_error_called)
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["apiServerState"], "error")
        self.assertIn("failed to start", str(window.startup_error_message).lower())

    def test_main_opens_normal_window_only_after_server_reports_started(self) -> None:
        fake_app, _status_manager, window = self._make_app()
        fake_server = FakeServer(should_start=True)

        with patch("worker.build_server", return_value=fake_server):
            worker.main(application=fake_app)

        self.assertTrue(window.run_called)
        self.assertFalse(window.show_startup_error_called)


if __name__ == "__main__":
    unittest.main()
