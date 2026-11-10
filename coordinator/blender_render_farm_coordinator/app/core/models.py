from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import uuid4


class CoordinatorError(Exception):
    """Base coordinator exception."""


class NotFoundError(CoordinatorError):
    """Raised when an entity does not exist."""


class ConflictError(CoordinatorError):
    """Raised when an operation conflicts with the current state."""


class JobStatus(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"
    BLOCKED = "blocked"


class FrameStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RENDERING = "rendering"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class WorkerStatus(StrEnum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    IDLE = "idle"
    READY = "ready"
    SESSION_OPEN = "session_open"
    QUEUED = "queued"
    RENDERING = "rendering"
    DRAINING = "draining"
    CLOSING = "closing"
    BUSY = "busy"
    OFFLINE = "offline"
    ERROR = "error"


ACTIVE_JOB_STATUSES = {
    JobStatus.VALIDATING.value,
    JobStatus.RUNNING.value,
    JobStatus.PAUSED.value,
    JobStatus.BLOCKED.value,
}

TERMINAL_JOB_STATUSES = {
    JobStatus.COMPLETED.value,
    JobStatus.CANCELLED.value,
    JobStatus.ERROR.value,
}

ONLINE_WORKER_STATUSES = {
    WorkerStatus.ONLINE.value,
    WorkerStatus.IDLE.value,
    WorkerStatus.READY.value,
    WorkerStatus.SESSION_OPEN.value,
    WorkerStatus.QUEUED.value,
    WorkerStatus.RENDERING.value,
    WorkerStatus.DRAINING.value,
    WorkerStatus.CLOSING.value,
    WorkerStatus.BUSY.value,
}

FREE_WORKER_STATUSES = {
    WorkerStatus.ONLINE.value,
    WorkerStatus.IDLE.value,
    WorkerStatus.READY.value,
    WorkerStatus.UNKNOWN.value,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def iso_after_seconds(seconds: int | float) -> str:
    return (utc_now() + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def normalize_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    return bool(value)


def new_assignment_id(frame_number: int, attempt_count: int) -> str:
    return f"lease_{frame_number:06d}_{attempt_count}_{uuid4().hex[:6]}"
