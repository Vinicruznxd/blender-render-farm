from __future__ import annotations

import logging
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from app.blender_runner import BlenderRunner
from app.capabilities import CapabilitiesService
from app.config import WorkerConfig
from app.status_manager import WorkerStatusManager


class SubprocessDecodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.logger = logging.getLogger("tests.subprocess_decoding")
        cls.logger.addHandler(logging.NullHandler())

    def _create_runner(self) -> BlenderRunner:
        config = WorkerConfig()
        status_manager = WorkerStatusManager(config)
        return BlenderRunner(
            project_root=Path.cwd(),
            config=config,
            status_manager=status_manager,
            logger=self.logger,
        )

    def test_capabilities_command_tolerates_non_utf8_output(self) -> None:
        service = CapabilitiesService(WorkerConfig(), self.logger)
        completed = subprocess.CompletedProcess(
            args=["powershell"],
            returncode=0,
            stdout=b"GPU-\x8f-NAME\r\n",
            stderr=b"",
        )

        with patch("app.capabilities.subprocess.run", return_value=completed):
            result = service._run_command(["powershell"])

        self.assertIn("GPU", result)
        self.assertTrue(result.strip())

    def test_read_blender_version_tolerates_non_utf8_output(self) -> None:
        runner = self._create_runner()
        completed = subprocess.CompletedProcess(
            args=["blender", "--version"],
            returncode=0,
            stdout=b"\x8fBlender 5.1.2\r\n",
            stderr=b"",
        )

        with patch("app.blender_runner.subprocess.run", return_value=completed):
            version = runner._read_blender_version(Path(r"C:\Blender\blender.exe"))

        self.assertEqual(version, "5.1.2")

    def test_headless_probe_tolerates_non_utf8_output(self) -> None:
        runner = self._create_runner()
        completed = subprocess.CompletedProcess(
            args=["blender", "-b"],
            returncode=0,
            stdout=b"\x8f\r\nBLENDER_HEADLESS_STARTUP_OK\r\n",
            stderr=b"",
        )

        with patch("app.blender_runner.subprocess.run", return_value=completed):
            startup_ok, startup_error = runner._probe_blender_headless_startup(
                Path(r"C:\Blender\blender.exe")
            )

        self.assertTrue(startup_ok)
        self.assertEqual(startup_error, "")


if __name__ == "__main__":
    unittest.main()
