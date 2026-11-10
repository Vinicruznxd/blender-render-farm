from __future__ import annotations

import re
from pathlib import Path


FORMAT_EXTENSION_MAP = {
    "PNG": "png",
    "JPEG": "jpg",
    "JPG": "jpg",
    "OPEN_EXR": "exr",
    "EXR": "exr",
    "TIFF": "tif",
    "BMP": "bmp",
}


class FrameScanner:
    def build_output_path(self, output_pattern: str, frame_number: int, image_format: str) -> Path:
        format_extension = FORMAT_EXTENSION_MAP.get(image_format.upper(), image_format.lower())
        if "#" in output_pattern:
            rendered = re.sub(
                r"#+",
                lambda match: f"{frame_number:0{len(match.group(0))}d}",
                output_pattern,
                count=1,
            )
            path = Path(rendered)
            return path if path.suffix else path.with_suffix(f".{format_extension}")

        path = Path(output_pattern)
        if path.suffix:
            return path.with_name(f"{path.stem}{frame_number:04d}{path.suffix}")
        return Path(f"{output_pattern}{frame_number:04d}.{format_extension}")

    def frame_exists(self, job: dict, frame_number: int) -> bool:
        path = self.build_output_path(job["outputPattern"], frame_number, job.get("format", "PNG"))
        return self.is_valid_output(path)

    @staticmethod
    def is_valid_output(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def output_fingerprint(self, job: dict, frame_number: int) -> dict[str, int | bool | None]:
        path = self.build_output_path(job["outputPattern"], frame_number, job.get("format", "PNG"))
        try:
            stat = path.stat()
            return {
                "exists": path.is_file() and stat.st_size > 0,
                "size": int(stat.st_size),
                "mtimeNs": int(stat.st_mtime_ns),
            }
        except OSError:
            return {"exists": False, "size": None, "mtimeNs": None}

    def output_is_fresh(self, job: dict, frame: dict) -> bool:
        current = self.output_fingerprint(job, int(frame["frameNumber"]))
        if not current["exists"]:
            return False
        if not bool(frame.get("baselineExists")):
            return True
        return (
            current["size"] != frame.get("baselineSize")
            or current["mtimeNs"] != frame.get("baselineMtimeNs")
        )

    def resolve_output_path(self, job: dict, frame_number: int) -> str:
        return str(self.build_output_path(job["outputPattern"], frame_number, job.get("format", "PNG")))
