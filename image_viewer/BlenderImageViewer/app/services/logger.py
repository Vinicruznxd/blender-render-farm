from __future__ import annotations

from collections import deque
from datetime import datetime
import logging
from pathlib import Path
import threading
import traceback


class AppLogger:
    def __init__(self, log_dir: Path, limit: int = 2000) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.entries: deque[str] = deque(maxlen=limit)
        self._lock = threading.RLock()
        self._logger = logging.getLogger(f"blender_image_viewer_{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = logging.FileHandler(self.log_dir / "image_viewer.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.handlers.clear()
        self._logger.addHandler(handler)

    def info(self, message: str) -> None:
        self._write("info", message)

    def warning(self, message: str) -> None:
        self._write("warning", message)

    def error(self, message: str) -> None:
        self._write("error", message)

    def exception(self, message: str, exc: BaseException) -> None:
        self.error(f"{message}: {exc}")
        self._logger.error("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    def general_text(self, limit: int = 500) -> str:
        with self._lock:
            items = list(self.entries)[-limit:]
        return "\n".join(items)

    def close(self) -> None:
        with self._lock:
            handlers = list(self._logger.handlers)
            for handler in handlers:
                handler.flush()
                handler.close()
                self._logger.removeHandler(handler)

    def _write(self, level: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] [{level.upper()}] {message}"
        with self._lock:
            self.entries.append(line)
        self._logger.info(line)
