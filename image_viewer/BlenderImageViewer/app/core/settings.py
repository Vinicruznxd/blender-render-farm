from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from renderfarm_shared.i18n import normalize_language


DEFAULT_COORDINATOR_URL = "http://127.0.0.1:8010"


@dataclass(slots=True)
class ViewerSettings:
    language: str = "en"
    last_image_folder: str = ""
    last_blend_file: str = ""
    last_output_folder: str = ""
    grid_columns: int = 4


def resolve_project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def load_settings(config_path: Path) -> ViewerSettings:
    if not config_path.exists():
        return ViewerSettings()
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    legacy_view_mode = str(raw.get("view_mode") or "").strip().lower()
    default_columns = 1 if legacy_view_mode == "single" else 4
    return ViewerSettings(
        language=normalize_language(raw.get("language")),
        last_image_folder=str(raw.get("last_image_folder") or ""),
        last_blend_file=str(raw.get("last_blend_file") or ""),
        last_output_folder=str(raw.get("last_output_folder") or ""),
        grid_columns=_normalize_grid_columns(raw.get("grid_columns"), default=default_columns),
    )


def save_settings(config_path: Path, settings: ViewerSettings) -> None:
    config_path.write_text(
        json.dumps(
            {
                "language": normalize_language(settings.language),
                "last_image_folder": settings.last_image_folder,
                "last_blend_file": settings.last_blend_file,
                "last_output_folder": settings.last_output_folder,
                "grid_columns": _normalize_grid_columns(settings.grid_columns),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _normalize_grid_columns(value: object, *, default: int = 4) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = default
    return max(1, min(numeric, 4))
