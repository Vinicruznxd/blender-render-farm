from __future__ import annotations

import threading
import time

from app.core.frame_scanner import FrameScanner
from app.core.scheduler import BackgroundScheduler


def test_scheduler_survives_a_failed_cycle() -> None:
    completed = threading.Event()
    calls = 0

    def callback() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient failure")
        completed.set()

    scheduler = BackgroundScheduler(callback, 0.01)
    scheduler.start()
    assert completed.wait(1.0)
    scheduler.stop()
    snapshot = scheduler.snapshot()
    assert calls >= 2
    assert int(snapshot["cycleCount"]) >= 2
    assert snapshot["consecutiveFailures"] == 0


def test_frame_scanner_rejects_directories_and_empty_files(tmp_path) -> None:
    scanner = FrameScanner()
    job = {"outputPattern": str(tmp_path / "frame_####"), "format": "PNG"}
    output = scanner.build_output_path(job["outputPattern"], 1, "PNG")
    output.mkdir(parents=True)
    assert not scanner.frame_exists(job, 1)
    output.rmdir()
    output.touch()
    assert not scanner.frame_exists(job, 1)
    output.write_bytes(b"rendered")
    assert scanner.frame_exists(job, 1)


def test_fingerprint_requires_a_changed_rerender_output(tmp_path) -> None:
    scanner = FrameScanner()
    job = {"outputPattern": str(tmp_path / "frame_####"), "format": "PNG"}
    output = scanner.build_output_path(job["outputPattern"], 1, "PNG")
    output.write_bytes(b"old")
    baseline = scanner.output_fingerprint(job, 1)
    frame = {
        "frameNumber": 1,
        "baselineExists": True,
        "baselineSize": baseline["size"],
        "baselineMtimeNs": baseline["mtimeNs"],
    }
    assert not scanner.output_is_fresh(job, frame)
    time.sleep(0.002)
    output.write_bytes(b"new-render")
    assert scanner.output_is_fresh(job, frame)
