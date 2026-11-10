from __future__ import annotations

from pathlib import Path
import time

from app.core.models import RenderJob


class FrameScanner:
    def __init__(self, stability_seconds: float = 2.0) -> None:
        self.stability_seconds = stability_seconds

    def frame_path(self, output_pattern: str, frame_number: int, image_format: str) -> Path:
        hashes = self._hash_placeholder(output_pattern)
        padded = str(frame_number).zfill(len(hashes))
        replaced = output_pattern.replace(hashes, padded)
        path = Path(replaced)
        if path.suffix:
            return path
        return path.with_suffix(f".{image_format.lower()}")

    def scan_job(self, job: RenderJob) -> set[int]:
        completed: set[int] = set()
        now = time.time()
        for frame_number in range(job.startFrame, job.endFrame + 1):
            frame_path = self.frame_path(job.outputPattern, frame_number, job.format)
            if not frame_path.exists() or not frame_path.is_file():
                continue
            try:
                stat = frame_path.stat()
            except OSError:
                continue
            if stat.st_size <= 0:
                continue
            if now - stat.st_mtime < self.stability_seconds:
                continue
            completed.add(frame_number)
        return completed

    def _hash_placeholder(self, output_pattern: str) -> str:
        if "####" in output_pattern:
            return "####"
        count = 0
        for char in output_pattern:
            if char == "#":
                count += 1
        if count <= 0:
            raise ValueError("O padrão de saída precisa conter # para o número do frame.")
        return "#" * count
