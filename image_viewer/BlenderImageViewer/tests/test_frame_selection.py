from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.frame_selection import build_submission_groups, discover_image_entries


def test_discover_image_entries_extracts_frame_and_pattern(tmp_path: Path):
    folder = tmp_path / "renders"
    folder.mkdir()
    (folder / "shot_0007.png").write_bytes(b"mock")
    nested = folder / "nested"
    nested.mkdir()
    (nested / "shot_0010.png").write_bytes(b"mock")

    entries = discover_image_entries(folder)

    assert [entry.frame_number for entry in entries] == [7, 10]
    assert entries[0].pattern_name == "shot_####.png"
    assert entries[0].format_name == "PNG"
    assert entries[0].is_renderable is True


def test_build_submission_groups_splits_contiguous_ranges_and_skips_invalid_entries(tmp_path: Path):
    folder = tmp_path / "renders"
    folder.mkdir()
    for name in ["shot_0001.png", "shot_0002.png", "shot_0007.png", "beauty_0010.jpg", "preview.png"]:
        (folder / name).write_bytes(b"mock")

    entries = discover_image_entries(folder)
    groups, skipped = build_submission_groups(entries)

    assert [(group.pattern_name, group.start_frame, group.end_frame) for group in groups] == [
        ("beauty_####.jpg", 10, 10),
        ("shot_####.png", 1, 2),
        ("shot_####.png", 7, 7),
    ]
    assert [entry.path.name for entry in skipped] == ["preview.png"]
