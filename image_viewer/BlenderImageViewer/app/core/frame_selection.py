from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable


IMAGE_FORMAT_BY_SUFFIX = {
    ".bmp": "BMP",
    ".exr": "OPEN_EXR",
    ".jpeg": "JPEG",
    ".jpg": "JPEG",
    ".png": "PNG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".webp": "WEBP",
}

FRAME_TOKEN_RE = re.compile(r"(\d+)(?!.*\d)")


@dataclass(slots=True, frozen=True)
class ImageEntry:
    path: Path
    frame_number: int | None
    frame_digits: str = ""
    pattern_name: str = ""
    format_name: str = ""
    error: str | None = None

    @property
    def is_renderable(self) -> bool:
        return self.frame_number is not None and bool(self.pattern_name) and bool(self.format_name)


@dataclass(slots=True, frozen=True)
class FrameSubmissionGroup:
    pattern_name: str
    format_name: str
    start_frame: int
    end_frame: int
    frame_numbers: tuple[int, ...]

    def output_pattern(self, output_folder: Path) -> str:
        return str(output_folder / self.pattern_name)


def discover_image_entries(folder: Path) -> list[ImageEntry]:
    if not folder.exists() or not folder.is_dir():
        return []

    entries: list[ImageEntry] = []
    for path in sorted(folder.rglob("*"), key=lambda candidate: str(candidate).lower()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in IMAGE_FORMAT_BY_SUFFIX:
            continue
        entries.append(build_image_entry(path))
    return sorted(entries, key=_entry_sort_key)


def build_image_entry(path: Path) -> ImageEntry:
    suffix = path.suffix.lower()
    format_name = IMAGE_FORMAT_BY_SUFFIX.get(suffix)
    if not format_name:
        return ImageEntry(path=path, frame_number=None, error="Formato de imagem nao suportado.")

    match = FRAME_TOKEN_RE.search(path.stem)
    if not match:
        return ImageEntry(
            path=path,
            frame_number=None,
            format_name=format_name,
            error="Nao foi possivel detectar o frame pelo nome do arquivo.",
        )

    frame_digits = match.group(1)
    pattern_stem = f"{path.stem[:match.start()]}{'#' * len(frame_digits)}{path.stem[match.end():]}"
    return ImageEntry(
        path=path,
        frame_number=int(frame_digits),
        frame_digits=frame_digits,
        pattern_name=f"{pattern_stem}{suffix}",
        format_name=format_name,
    )


def build_submission_groups(
    entries: Iterable[ImageEntry],
) -> tuple[list[FrameSubmissionGroup], list[ImageEntry]]:
    grouped_frames: dict[tuple[str, str], set[int]] = {}
    skipped: list[ImageEntry] = []

    for entry in entries:
        if not entry.is_renderable:
            skipped.append(entry)
            continue
        key = (entry.pattern_name, entry.format_name)
        grouped_frames.setdefault(key, set()).add(int(entry.frame_number or 0))

    groups: list[FrameSubmissionGroup] = []
    for (pattern_name, format_name), frame_numbers in sorted(grouped_frames.items(), key=lambda item: item[0]):
        ordered_frames = sorted(frame_numbers)
        if not ordered_frames:
            continue
        range_start = ordered_frames[0]
        range_end = ordered_frames[0]
        current_frames = [ordered_frames[0]]
        for frame_number in ordered_frames[1:]:
            if frame_number == range_end + 1:
                range_end = frame_number
                current_frames.append(frame_number)
                continue
            groups.append(
                FrameSubmissionGroup(
                    pattern_name=pattern_name,
                    format_name=format_name,
                    start_frame=range_start,
                    end_frame=range_end,
                    frame_numbers=tuple(current_frames),
                )
            )
            range_start = frame_number
            range_end = frame_number
            current_frames = [frame_number]
        groups.append(
            FrameSubmissionGroup(
                pattern_name=pattern_name,
                format_name=format_name,
                start_frame=range_start,
                end_frame=range_end,
                frame_numbers=tuple(current_frames),
            )
        )
    groups.sort(key=lambda group: (group.pattern_name.lower(), group.start_frame, group.end_frame))
    return groups, skipped


def _entry_sort_key(entry: ImageEntry) -> tuple[int, int, str]:
    if entry.frame_number is None:
        return (1, 0, entry.path.name.lower())
    return (0, int(entry.frame_number), entry.path.name.lower())
