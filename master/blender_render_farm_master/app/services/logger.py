from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
import logging
import threading
import traceback


class AppLogger:
    def __init__(self, log_dir: Path, general_limit: int = 4000, worker_limit: int = 1000) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.general_entries: deque[str] = deque(maxlen=general_limit)
        self.worker_entries: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=worker_limit))
        self._lock = threading.RLock()
        self._logger = logging.getLogger(f"render_farm_master_{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = logging.FileHandler(self.log_dir / "master.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.handlers.clear()
        self._logger.addHandler(handler)

    def _write(self, level: str, message: str, worker_id: str | None = None, block_id: str | None = None) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        context_parts = []
        if worker_id:
            context_parts.append(f"worker={worker_id}")
        if block_id:
            context_parts.append(f"block={block_id}")
        context = f" [{' | '.join(context_parts)}]" if context_parts else ""
        line = f"[{timestamp}] [{level.upper()}]{context} {message}"
        with self._lock:
            self.general_entries.append(line)
            if worker_id:
                self.worker_entries[worker_id].append(line)
        self._logger.info(line)

    def info(self, message: str, worker_id: str | None = None, block_id: str | None = None) -> None:
        self._write("info", message, worker_id=worker_id, block_id=block_id)

    def warning(self, message: str, worker_id: str | None = None, block_id: str | None = None) -> None:
        self._write("warning", message, worker_id=worker_id, block_id=block_id)

    def error(self, message: str, worker_id: str | None = None, block_id: str | None = None) -> None:
        self._write("error", message, worker_id=worker_id, block_id=block_id)

    def exception(self, message: str, exc: BaseException, worker_id: str | None = None, block_id: str | None = None) -> None:
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.error(f"{message}: {exc}", worker_id=worker_id, block_id=block_id)
        self._logger.error(formatted)

    def general_text(self, limit: int = 500) -> str:
        with self._lock:
            items = list(self.general_entries)[-limit:]
        return "\n".join(items)

    def worker_text(self, worker_id: str, limit: int = 500) -> str:
        with self._lock:
            items = list(self.worker_entries.get(worker_id, []))[-limit:]
        return "\n".join(items)

    def close(self) -> None:
        with self._lock:
            handlers = list(self._logger.handlers)
            for handler in handlers:
                handler.flush()
                handler.close()
                self._logger.removeHandler(handler)
