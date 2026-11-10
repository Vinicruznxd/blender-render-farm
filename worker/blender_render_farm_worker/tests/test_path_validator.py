from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.blender_runner import BlenderSelectionResult
from app.path_validator import validate_paths


class ValidatePathsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.logger = logging.getLogger("tests.path_validator")
        cls.logger.addHandler(logging.NullHandler())

    def _compatible_runner(
        self,
        *,
        blend_version: str = "5.2",
        selected_version: str = "5.2.1",
        selected_path: str = r"C:\Blender52\blender.exe",
    ):
        class CompatibleRunner:
            def select_blender_for_blend_file(self, _blend_path, **_kwargs):
                return BlenderSelectionResult(
                    compatible=True,
                    blend_version_detected=blend_version,
                    required_blender_version=blend_version,
                    selected_blender_version=selected_version,
                    selected_blender_path=selected_path,
                    compatibility_reason=(
                        f"Selected Blender {selected_version} at {selected_path} "
                        f"for blend version {blend_version} (exact major/minor match)."
                    ),
                )

        return CompatibleRunner()

    def test_validate_paths_success_and_master_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend_file = root / "scene.blend"
            output_folder = root / "renders"
            blend_file.write_bytes(b"BLENDER")
            output_folder.mkdir()

            result = validate_paths(
                str(blend_file),
                str(output_folder),
                self.logger,
                blender_runner=self._compatible_runner(),
            )
            payload = result.model_dump()

        self.assertTrue(result.ok)
        self.assertTrue(result.blendAccessible)
        self.assertTrue(result.outputAccessible)
        self.assertTrue(result.blendFileExists)
        self.assertTrue(result.outputFolderExists)
        self.assertTrue(result.canWriteOutput)
        self.assertEqual(result.errors, [])
        self.assertIn("ok", payload)
        self.assertIn("blendAccessible", payload)
        self.assertIn("outputAccessible", payload)
        self.assertIn("message", payload)
        self.assertIn("blendFileExists", payload)
        self.assertIn("outputFolderExists", payload)
        self.assertIn("canWriteOutput", payload)
        self.assertEqual(result.blendVersionDetected, "5.2")
        self.assertEqual(result.selectedBlenderVersion, "5.2.1")
        self.assertEqual(result.selectedBlenderPath, r"C:\Blender52\blender.exe")
        self.assertTrue(result.compatible)
        self.assertIn("compatibilityReason", payload)
        self.assertIn("errors", payload)

    def test_validate_paths_with_x_drive_visible(self) -> None:
        with patch("app.path_validator._drive_root_exists", return_value=True), patch(
            "app.path_validator._path_exists",
            return_value=True,
        ), patch(
            "app.path_validator._path_is_file",
            side_effect=lambda path: str(path).lower().endswith(".blend"),
        ), patch(
            "app.path_validator._path_is_dir",
            side_effect=lambda path: not str(path).lower().endswith(".blend"),
        ), patch(
            "app.path_validator._probe_blend_file_readable",
            return_value=None,
        ), patch(
            "app.path_validator._probe_output_folder_writable",
            return_value=None,
        ):
            result = validate_paths(
                r"X:\Projetos\Cena01\cena.blend",
                r"X:\Projetos\Cena01\renders",
                self.logger,
                blender_runner=self._compatible_runner(
                    blend_version="4.5",
                    selected_version="4.5.0",
                    selected_path=r"C:\Blender45\blender.exe",
                ),
            )

        self.assertTrue(result.ok)
        self.assertTrue(result.blendAccessible)
        self.assertTrue(result.outputAccessible)
        self.assertEqual(result.message, "Paths validated successfully.")
        self.assertEqual(result.errors, [])

    def test_validate_paths_with_x_drive_not_visible(self) -> None:
        with patch("app.path_validator._drive_root_exists", return_value=False):
            result = validate_paths(
                r"X:\Projetos\Cena01\cena.blend",
                r"X:\Projetos\Cena01\renders",
                self.logger,
                blender_runner=self._compatible_runner(),
            )

        self.assertFalse(result.ok)
        self.assertFalse(result.blendAccessible)
        self.assertFalse(result.outputAccessible)
        self.assertFalse(result.blendFileExists)
        self.assertFalse(result.outputFolderExists)
        self.assertFalse(result.canWriteOutput)
        self.assertIn("Drive X: nao esta montado", result.message)
        self.assertTrue(any("Drive X: nao esta montado" in error for error in result.errors))

    def test_validate_paths_creates_missing_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend_file = root / "scene.blend"
            output_folder = root / "renders" / "nested"
            blend_file.write_bytes(b"BLENDER")

            result = validate_paths(
                str(blend_file),
                str(output_folder),
                self.logger,
                blender_runner=self._compatible_runner(),
            )

            self.assertTrue(output_folder.exists())
            self.assertTrue(output_folder.is_dir())

        self.assertTrue(result.ok)
        self.assertTrue(result.outputCreated)
        self.assertTrue(result.outputAccessible)
        self.assertTrue(result.canWriteOutput)

    def test_validate_paths_reports_output_folder_creation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend_file = root / "scene.blend"
            output_folder = root / "renders" / "nested"
            blend_file.write_bytes(b"BLENDER")

            with patch(
                "app.path_validator._mkdir_path",
                side_effect=PermissionError("Access denied"),
            ):
                result = validate_paths(
                    str(blend_file),
                    str(output_folder),
                    self.logger,
                    blender_runner=self._compatible_runner(),
                )

        self.assertFalse(result.ok)
        self.assertFalse(result.outputAccessible)
        self.assertFalse(result.outputCreated)
        self.assertIn("Permission denied creating output folder", result.message)
        self.assertTrue(
            any("Permission denied creating output folder" in error for error in result.errors)
        )

    def test_validate_paths_reports_incompatible_blender_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend_file = root / "scene.blend"
            output_folder = root / "renders"
            blend_file.write_bytes(b"BLENDER17-01v0502REND")
            output_folder.mkdir()

            class IncompatibleRunner:
                def select_blender_for_blend_file(self, _blend_path, **_kwargs):
                    return BlenderSelectionResult(
                        compatible=False,
                        blend_version_detected="5.2",
                        required_blender_version="5.2",
                        compatibility_reason=(
                            "No compatible Blender installation found for blend version 5.2. "
                            "The worker never uses a Blender version lower than the file version."
                        ),
                    )

            result = validate_paths(
                str(blend_file),
                str(output_folder),
                self.logger,
                blender_runner=IncompatibleRunner(),
            )

        self.assertFalse(result.ok)
        self.assertTrue(result.blendAccessible)
        self.assertTrue(result.outputAccessible)
        self.assertEqual(result.blendVersionDetected, "5.2")
        self.assertFalse(result.compatible)
        self.assertEqual(result.selectedBlenderVersion, "")
        self.assertEqual(result.selectedBlenderPath, "")
        self.assertIn("No compatible Blender installation found", result.compatibilityReason)
        self.assertIn("No compatible Blender installation found", result.message)

    def test_validate_paths_uses_provided_version_hint_when_worker_needs_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blend_file = root / "scene_unknown.blend"
            output_folder = root / "renders"
            blend_file.write_bytes(b"NOT_A_VALID_BLEND")
            output_folder.mkdir()

            class HintAwareRunner:
                def select_blender_for_blend_file(self, _blend_path, **kwargs):
                    self.kwargs = kwargs
                    return BlenderSelectionResult(
                        compatible=True,
                        blend_version_detected="5.0",
                        required_blender_version="5.0",
                        preferred_blender_major=5,
                        version_routing_policy="prefer_same_major_then_higher",
                        selected_blender_version="5.0.1",
                        selected_blender_path=r"C:\Blender50\blender.exe",
                        compatibility_reason="Fallback to provided version hint 5.0 succeeded.",
                    )

            runner = HintAwareRunner()
            result = validate_paths(
                str(blend_file),
                str(output_folder),
                self.logger,
                blender_runner=runner,
                required_blender_version="5.0",
                preferred_blender_major=5,
                version_routing_policy="prefer_same_major_then_higher",
                blend_version_detected_hint="5.0",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.requiredBlenderVersion, "5.0")
        self.assertEqual(result.preferredBlenderMajor, 5)
        self.assertEqual(result.versionRoutingPolicy, "prefer_same_major_min_version")
        self.assertEqual(runner.kwargs["required_blender_version"], "5.0")


if __name__ == "__main__":
    unittest.main()
