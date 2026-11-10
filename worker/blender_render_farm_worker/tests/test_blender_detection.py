from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import zstandard
except ImportError:  # pragma: no cover - dependency installed in normal test runs
    zstandard = None

from app.blender_runner import (
    BlenderInstallation,
    BlenderRunner,
    discover_blender_executables,
    extract_blender_executable,
    iter_windows_blender_search_roots,
    normalize_blender_candidate,
    parse_blender_version,
    read_blend_file_version,
)
from app.config import WorkerConfig
from app.status_manager import WorkerStatusManager


class BlenderDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.logger = logging.getLogger("tests.blender_detection")
        cls.logger.addHandler(logging.NullHandler())

    def _create_runner(self, config: WorkerConfig | None = None) -> BlenderRunner:
        worker_config = config or WorkerConfig()
        status_manager = WorkerStatusManager(worker_config)
        return BlenderRunner(
            project_root=Path.cwd(),
            config=worker_config,
            status_manager=status_manager,
            logger=self.logger,
        )

    def _write_blend_file(self, path: Path, version_code: str | None) -> None:
        if version_code is None:
            path.write_bytes(b"NOT_A_VALID_BLEND")
            return
        path.write_bytes(b"BLENDER-v" + version_code.encode("ascii"))

    def _write_modern_blend_file(self, path: Path, version_code: str) -> None:
        path.write_bytes(b"BLENDER17-01v" + version_code.encode("ascii") + b"REND")

    def _write_zstd_blend_file(self, path: Path, version_code: str, *, modern_header: bool = False) -> None:
        if zstandard is None:  # pragma: no cover - guarded by test skip
            raise RuntimeError("zstandard dependency is not installed")
        if modern_header:
            payload = b"BLENDER17-01v" + version_code.encode("ascii") + b"REND"
        else:
            payload = b"BLENDER-v" + version_code.encode("ascii")
        compressor = zstandard.ZstdCompressor()
        path.write_bytes(compressor.compress(payload))

    def test_extract_blender_executable_from_registry_style_command(self) -> None:
        raw_value = r'"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" "%1"'
        extracted = extract_blender_executable(raw_value)
        self.assertEqual(
            extracted,
            r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe",
        )

    def test_normalize_blender_candidate_from_install_directory(self) -> None:
        normalized = normalize_blender_candidate(
            r"C:\Program Files\Blender Foundation\Blender 5.0"
        )
        self.assertIsNotNone(normalized)
        self.assertEqual(
            str(normalized),
            r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe",
        )

    def test_iter_windows_blender_search_roots_includes_local_appdata(self) -> None:
        roots = iter_windows_blender_search_roots(
            {
                "PROGRAMFILES": r"C:\Program Files",
                "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
                "LOCALAPPDATA": r"C:\Users\Render\AppData\Local",
            }
        )
        root_strings = {str(root) for root in roots}
        self.assertIn(r"C:\Program Files\Blender Foundation", root_strings)
        self.assertIn(r"C:\Program Files (x86)\Blender Foundation", root_strings)
        self.assertIn(
            r"C:\Users\Render\AppData\Local\Programs\Blender Foundation",
            root_strings,
        )
        self.assertIn(
            r"C:\Users\Render\AppData\Local\Programs\Blender Foundation\Blender",
            root_strings,
        )

    def test_discover_blender_executables_finds_per_user_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Programs" / "Blender Foundation"
            exe_path = root / "Blender 5.0" / "blender.exe"
            exe_path.parent.mkdir(parents=True, exist_ok=True)
            exe_path.write_bytes(b"")

            discovered = discover_blender_executables(root)

        self.assertIn(exe_path, discovered)

    def test_detect_blender_finds_per_user_install_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Programs" / "Blender Foundation"
            exe_path = root / "Blender 5.0" / "blender.exe"
            exe_path.parent.mkdir(parents=True, exist_ok=True)
            exe_path.write_bytes(b"")

            runner = self._create_runner()
            with patch("app.blender_runner.COMMON_BLENDER_PATHS", []), patch(
                "app.blender_runner.iter_windows_blender_search_roots",
                return_value=[root],
            ), patch.object(
                runner,
                "_collect_environment_candidates",
                return_value=[],
            ), patch.object(
                runner,
                "_collect_path_candidates",
                return_value=[],
            ), patch.object(
                runner,
                "_collect_registry_candidates",
                return_value=[],
            ), patch.object(
                runner,
                "_read_blender_version",
                return_value="5.0",
            ), patch.object(
                runner,
                "_probe_blender_headless_startup",
                return_value=(True, ""),
            ):
                info = runner.detect_blender()

        self.assertTrue(info.found)
        self.assertEqual(info.path, str(exe_path))
        self.assertEqual(info.version, "5.0")

    def test_candidate_paths_are_deduplicated(self) -> None:
        configured_path = r"C:\Blender\blender.exe"
        config = WorkerConfig(blenderPath=configured_path)
        runner = self._create_runner(config)
        duplicate_path = Path(configured_path)

        with patch("app.blender_runner.COMMON_BLENDER_PATHS", [configured_path]), patch.object(
            runner,
            "_collect_environment_candidates",
            return_value=[duplicate_path],
        ), patch.object(
            runner,
            "_collect_path_candidates",
            return_value=[duplicate_path],
        ), patch.object(
            runner,
            "_collect_registry_candidates",
            return_value=[duplicate_path],
        ), patch.object(
            runner,
            "_collect_common_install_candidates",
            return_value=[duplicate_path],
        ):
            candidates = runner._candidate_paths()

        self.assertEqual(candidates, [duplicate_path])

    def test_read_blend_file_version_detects_4_5(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_45.blend"
            self._write_blend_file(blend_file, "405")

            version = read_blend_file_version(blend_file)

        self.assertEqual(version, "4.5")

    def test_read_blend_file_version_detects_modern_5_0_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_50_modern.blend"
            self._write_modern_blend_file(blend_file, "0500")

            version = read_blend_file_version(blend_file)

        self.assertEqual(version, "5.0")

    def test_read_blend_file_version_accepts_future_modern_header_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_future_modern.blend"
            blend_file.write_bytes(b"BLENDER18-02v0510REND")

            version = read_blend_file_version(blend_file)

        self.assertEqual(version, "5.10")

    @unittest.skipIf(zstandard is None, "zstandard dependency not installed")
    def test_read_blend_file_version_detects_zstd_compressed_5_2(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_52_compressed.blend"
            self._write_zstd_blend_file(blend_file, "0502", modern_header=True)

            version = read_blend_file_version(blend_file)

        self.assertEqual(version, "5.2")

    @unittest.skipIf(zstandard is None, "zstandard dependency not installed")
    def test_read_blend_file_version_detects_zstd_compressed_modern_5_0_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_50_compressed_modern.blend"
            payload = b"BLENDER17-01v0500REND"
            compressor = zstandard.ZstdCompressor()
            blend_file.write_bytes(compressor.compress(payload))

            version = read_blend_file_version(blend_file)

        self.assertEqual(version, "5.0")

    def test_select_blender_prefers_exact_match_for_5_2_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_52.blend"
            self._write_blend_file(blend_file, "502")
            runner = self._create_runner()
            inventory = [
                BlenderInstallation(
                    path=r"C:\Blender50\blender.exe",
                    version="5.0.3",
                    parsed_version=parse_blender_version("5.0.3"),
                    priority=0,
                ),
                BlenderInstallation(
                    path=r"C:\Blender52\blender.exe",
                    version="5.2.1",
                    parsed_version=parse_blender_version("5.2.1"),
                    priority=1,
                ),
                BlenderInstallation(
                    path=r"C:\Blender60\blender.exe",
                    version="6.0.0",
                    parsed_version=parse_blender_version("6.0.0"),
                    priority=2,
                ),
            ]

            with patch.object(runner, "discover_blender_inventory", return_value=inventory):
                selection = runner.select_blender_for_blend_file(blend_file)

        self.assertTrue(selection.compatible)
        self.assertEqual(selection.blend_version_detected, "5.2")
        self.assertEqual(selection.selected_blender_version, "5.2.1")
        self.assertEqual(selection.selected_blender_path, r"C:\Blender52\blender.exe")
        self.assertIn("exact major/minor match", selection.compatibility_reason)

    def test_select_blender_rejects_installations_that_fail_headless_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_52.blend"
            self._write_modern_blend_file(blend_file, "0502")
            runner = self._create_runner()
            inventory = [
                BlenderInstallation(
                    path=r"C:\BrokenBlender52\blender.exe",
                    version="5.2.1",
                    parsed_version=parse_blender_version("5.2.1"),
                    priority=0,
                    startup_ok=False,
                    startup_error="The application was unable to start correctly (0xc000012d).",
                )
            ]

            with patch.object(runner, "discover_blender_inventory", return_value=inventory):
                selection = runner.select_blender_for_blend_file(blend_file)

        self.assertFalse(selection.compatible)
        self.assertIn("none could start correctly in headless mode", selection.compatibility_reason)

    def test_select_blender_uses_same_major_fallback_before_new_major(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_52.blend"
            self._write_modern_blend_file(blend_file, "0502")
            runner = self._create_runner()
            inventory = [
                BlenderInstallation(
                    path=r"C:\Blender53\blender.exe",
                    version="5.3.0",
                    parsed_version=parse_blender_version("5.3.0"),
                    priority=0,
                ),
                BlenderInstallation(
                    path=r"C:\Blender60\blender.exe",
                    version="6.0.0",
                    parsed_version=parse_blender_version("6.0.0"),
                    priority=1,
                ),
            ]

            with patch.object(runner, "discover_blender_inventory", return_value=inventory):
                selection = runner.select_blender_for_blend_file(blend_file)

        self.assertTrue(selection.compatible)
        self.assertEqual(selection.selected_blender_version, "5.3.0")
        self.assertEqual(selection.selected_blender_path, r"C:\Blender53\blender.exe")
        self.assertIn("same major fallback", selection.compatibility_reason)

    def test_select_blender_rejects_older_major_for_modern_5_x_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_50_modern.blend"
            self._write_modern_blend_file(blend_file, "0500")
            runner = self._create_runner()
            inventory = [
                BlenderInstallation(
                    path=r"C:\Blender45\blender.exe",
                    version="4.5.3",
                    parsed_version=parse_blender_version("4.5.3"),
                    priority=0,
                )
            ]

            with patch.object(runner, "discover_blender_inventory", return_value=inventory):
                selection = runner.select_blender_for_blend_file(blend_file)

        self.assertFalse(selection.compatible)
        self.assertEqual(selection.blend_version_detected, "5.0")
        self.assertEqual(selection.selected_blender_version, "")
        self.assertIn("No compatible Blender installation found", selection.compatibility_reason)

    def test_select_blender_uses_newer_major_fallback_for_4_5_when_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_45.blend"
            self._write_blend_file(blend_file, "405")
            runner = self._create_runner()
            inventory = [
                BlenderInstallation(
                    path=r"C:\Blender50\blender.exe",
                    version="5.0.0",
                    parsed_version=parse_blender_version("5.0.0"),
                    priority=0,
                )
            ]

            with patch.object(runner, "discover_blender_inventory", return_value=inventory):
                selection = runner.select_blender_for_blend_file(blend_file)

        self.assertTrue(selection.compatible)
        self.assertEqual(selection.blend_version_detected, "4.5")
        self.assertEqual(selection.selected_blender_version, "5.0.0")
        self.assertIn("newer major fallback", selection.compatibility_reason)

    def test_select_blender_prefers_same_major_older_when_policy_allows_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_45.blend"
            self._write_blend_file(blend_file, "405")
            runner = self._create_runner()
            inventory = [
                BlenderInstallation(
                    path=r"C:\Blender43\blender.exe",
                    version="4.3.0",
                    parsed_version=parse_blender_version("4.3.0"),
                    priority=0,
                ),
                BlenderInstallation(
                    path=r"C:\Blender51\blender.exe",
                    version="5.1.0",
                    parsed_version=parse_blender_version("5.1.0"),
                    priority=1,
                ),
            ]

            with patch.object(runner, "discover_blender_inventory", return_value=inventory):
                selection = runner.select_blender_for_blend_file(
                    blend_file,
                    version_routing_policy="prefer_same_major_any_version",
                )

        self.assertTrue(selection.compatible)
        self.assertEqual(selection.selected_blender_version, "4.3.0")
        self.assertEqual(selection.selected_version_bucket, "same_major_older")
        self.assertIn("same major fallback", selection.compatibility_reason)

    def test_select_blender_prefers_higher_major_when_same_major_is_older_and_policy_requires_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_45.blend"
            self._write_blend_file(blend_file, "405")
            runner = self._create_runner()
            inventory = [
                BlenderInstallation(
                    path=r"C:\Blender43\blender.exe",
                    version="4.3.0",
                    parsed_version=parse_blender_version("4.3.0"),
                    priority=0,
                ),
                BlenderInstallation(
                    path=r"C:\Blender51\blender.exe",
                    version="5.1.0",
                    parsed_version=parse_blender_version("5.1.0"),
                    priority=1,
                ),
            ]

            with patch.object(runner, "discover_blender_inventory", return_value=inventory):
                selection = runner.select_blender_for_blend_file(
                    blend_file,
                    version_routing_policy="prefer_higher_major_when_same_major_older",
                )

        self.assertTrue(selection.compatible)
        self.assertEqual(selection.selected_blender_version, "5.1.0")
        self.assertEqual(selection.selected_version_bucket, "higher_major")
        self.assertIn("newer major fallback", selection.compatibility_reason)

    def test_select_blender_rejects_when_blend_version_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_unknown.blend"
            self._write_blend_file(blend_file, None)
            runner = self._create_runner()
            inventory = [
                BlenderInstallation(
                    path=r"C:\Blender52\blender.exe",
                    version="5.2.0",
                    parsed_version=parse_blender_version("5.2.0"),
                    priority=0,
                )
            ]

            with patch.object(runner, "discover_blender_inventory", return_value=inventory):
                selection = runner.select_blender_for_blend_file(blend_file)

        self.assertFalse(selection.compatible)
        self.assertEqual(selection.blend_version_detected, "")
        self.assertEqual(selection.selected_blender_version, "")
        self.assertEqual(selection.selected_blender_path, "")
        self.assertIn("Could not detect", selection.compatibility_reason)

    def test_select_blender_uses_required_version_hint_when_header_detection_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            blend_file = Path(temp_dir) / "scene_unknown.blend"
            self._write_blend_file(blend_file, None)
            runner = self._create_runner()
            inventory = [
                BlenderInstallation(
                    path=r"C:\Blender50\blender.exe",
                    version="5.0.1",
                    parsed_version=parse_blender_version("5.0.1"),
                    priority=0,
                )
            ]

            with patch.object(runner, "discover_blender_inventory", return_value=inventory):
                selection = runner.select_blender_for_blend_file(
                    blend_file,
                    required_blender_version="5.0",
                    preferred_blender_major=5,
                    version_routing_policy="prefer_same_major_then_higher",
                    blend_version_detected_hint="5.0",
                )

        self.assertTrue(selection.compatible)
        self.assertEqual(selection.required_blender_version, "5.0")
        self.assertEqual(selection.preferred_blender_major, 5)
        self.assertEqual(selection.version_routing_policy, "prefer_same_major_min_version")
        self.assertEqual(selection.selected_blender_version, "5.0.1")


if __name__ == "__main__":
    unittest.main()
