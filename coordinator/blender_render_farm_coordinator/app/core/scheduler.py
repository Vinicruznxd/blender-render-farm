from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Callable


class BackgroundScheduler:
    def __init__(
        self,
        callback: Callable[[], None],
        interval_seconds: float,
        *,
        on_error: Callable[[BaseException], None] | None = None,
    ):
        self.callback = callback
        self.interval_seconds = interval_seconds
        self.on_error = on_error
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._cycle_count = 0
        self._consecutive_failures = 0
        self._last_cycle_started_at: str | None = None
        self._last_cycle_finished_at: str | None = None
        self._last_cycle_duration_seconds: float | None = None
        self._last_error: str | None = None
        self._last_error_traceback: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="coordinator-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(self.interval_seconds * 2, 1.0))

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            thread_alive = bool(self._thread and self._thread.is_alive())
            return {
                "running": thread_alive and not self._stop_event.is_set(),
                "threadAlive": thread_alive,
                "cycleCount": self._cycle_count,
                "consecutiveFailures": self._consecutive_failures,
                "lastCycleStartedAt": self._last_cycle_started_at,
                "lastCycleFinishedAt": self._last_cycle_finished_at,
                "lastCycleDurationSeconds": self._last_cycle_duration_seconds,
                "lastError": self._last_error,
                "lastErrorTraceback": self._last_error_traceback,
            }

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            started_at = datetime.now(timezone.utc).isoformat()
            with self._state_lock:
                self._last_cycle_started_at = started_at
            try:
                self.callback()
            except Exception as exc:  # Keep the farm alive after one bad cycle.
                with self._state_lock:
                    self._consecutive_failures += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._last_error_traceback = traceback.format_exc(limit=20)
                if self.on_error:
                    try:
                        self.on_error(exc)
                    except Exception:
                        pass
            else:
                with self._state_lock:
                    self._consecutive_failures = 0
                    self._last_error = None
                    self._last_error_traceback = None
            finally:
                with self._state_lock:
                    self._cycle_count += 1
                    self._last_cycle_finished_at = datetime.now(timezone.utc).isoformat()
                    self._last_cycle_duration_seconds = round(time.monotonic() - started, 6)
            self._stop_event.wait(self.interval_seconds)
