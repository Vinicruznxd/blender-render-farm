from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

LOGGER_NAME = "blender_render_farm_worker"


def sanitize_for_filename(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return sanitized or "unknown"


def get_logs_dir(project_root: Path) -> Path:
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def get_job_log_path(project_root: Path, job_id: str, block_id: str) -> Path:
    safe_job_id = sanitize_for_filename(job_id)
    safe_block_id = sanitize_for_filename(block_id)
    return get_logs_dir(project_root) / f"blender_job_{safe_job_id}_{safe_block_id}.log"


def setup_logging(project_root: Path, log_level: str) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(threadName)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(
        get_logs_dir(project_root) / "worker.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)

    stderr_stream = getattr(sys, "stderr", None)
    if stderr_stream is not None and hasattr(stderr_stream, "write"):
        stream_handler = logging.StreamHandler(stderr_stream)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    return logger
