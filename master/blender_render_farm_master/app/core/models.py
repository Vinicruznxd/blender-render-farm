from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, TypeAlias
from uuid import uuid4


RenderDevicePreference: TypeAlias = Literal["CPU", "GPU"]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def normalize_render_device_preference(value: Any) -> RenderDevicePreference | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if normalized in {"CPU", "GPU"}:
        return normalized
    return None


def payload_value(*payloads: dict[str, Any] | None, key: str) -> Any | None:
    for payload in payloads:
        if payload and key in payload and payload[key] is not None:
            return payload[key]
    return None


def normalize_progress_percent(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if 0.0 < numeric <= 1.0:
        numeric *= 100.0
    return max(0.0, min(100.0, numeric))


@dataclass
class Worker:
    id: str
    name: str
    ip: str
    port: int
    enabled: bool = True
    selected: bool = True
    status: str = "offline"
    blenderFound: bool = False
    blenderVersion: str | None = None
    gpu: str | None = None
    renderDevicePreference: RenderDevicePreference | None = None
    effectiveUseGpu: bool | None = None
    currentJobId: str | None = None
    currentBlockId: str | None = None
    currentFrame: int | None = None
    progress: float = 0.0
    lastSeen: float | None = None
    lastLog: str = ""
    error: str | None = None
    blendFileExists: bool | None = None
    outputFolderExists: bool | None = None
    canWriteOutput: bool | None = None
    lastValidationErrors: list[str] = field(default_factory=list)
    blendAccessible: bool | None = None
    outputAccessible: bool | None = None
    lastValidatedAt: float | None = None
    lastValidationPayload: dict[str, Any] = field(default_factory=dict)

    @property
    def is_online(self) -> bool:
        return self.enabled and self.status not in {"offline", "disabled"}

    @property
    def is_available(self) -> bool:
        return self.is_online and self.status == "idle"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_config_entry(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "enabled": self.enabled,
            "selected": self.selected,
        }

    @classmethod
    def from_config_entry(cls, data: dict[str, Any]) -> "Worker":
        return cls(
            id=data.get("id") or uuid4().hex,
            name=data.get("name") or data.get("ip") or "Worker",
            ip=data.get("ip", "127.0.0.1"),
            port=int(data.get("port", 9000)),
            enabled=bool(data.get("enabled", True)),
            selected=bool(data.get("selected", True)),
            status="disabled" if not bool(data.get("enabled", True)) else "offline",
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Worker":
        return cls(**data)


@dataclass
class WorkerPathValidationResult:
    workerId: str
    workerName: str
    workerIp: str
    workerPort: int
    blendFileExists: bool | None = None
    outputFolderExists: bool | None = None
    canWriteOutput: bool | None = None
    errors: list[str] = field(default_factory=list)
    rawPayload: dict[str, Any] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return f"http://{self.workerIp}:{self.workerPort}"

    @property
    def blend_accessible(self) -> bool | None:
        return self.blendFileExists

    @property
    def output_accessible(self) -> bool | None:
        if self.outputFolderExists is False or self.canWriteOutput is False:
            return False
        if self.outputFolderExists is True and self.canWriteOutput is True:
            return True
        if self.outputFolderExists is True and self.canWriteOutput is None:
            return True
        return None

    @property
    def ok(self) -> bool:
        explicit_ok = self._explicit_validation_flag()
        if explicit_ok is False:
            return False
        if self.errors:
            return False
        if self.blendFileExists is False:
            return False
        if self.outputFolderExists is False:
            return False
        if self.canWriteOutput is False:
            return False
        if any(value is True for value in (self.blendFileExists, self.outputFolderExists, self.canWriteOutput)):
            return True
        return bool(explicit_ok)

    @classmethod
    def from_payload(cls, worker: Worker, payload: dict[str, Any] | None) -> "WorkerPathValidationResult":
        payload = payload or {}

        blend_file_exists = payload_value(payload, key="blendFileExists")
        if blend_file_exists is None:
            blend_file_exists = payload_value(payload, key="blendAccessible")
        if blend_file_exists is None:
            blend_file_exists = payload_value(payload, key="blendFileAccessible")

        output_folder_exists = payload_value(payload, key="outputFolderExists")
        if output_folder_exists is None:
            output_folder_exists = payload_value(payload, key="outputAccessible")
        if output_folder_exists is None:
            output_folder_exists = payload_value(payload, key="outputFolderAccessible")

        can_write_output = payload_value(payload, key="canWriteOutput")
        if can_write_output is None:
            can_write_output = payload_value(payload, key="outputAccessible")
        if can_write_output is None:
            can_write_output = payload_value(payload, key="outputFolderAccessible")

        errors = payload.get("errors")
        if isinstance(errors, list):
            normalized_errors = [str(item) for item in errors if str(item).strip()]
        else:
            normalized_errors = []

        message = payload_value(payload, key="message")
        if message is None:
            message = payload_value(payload, key="reason")
        explicit_ok = cls._payload_validation_flag(payload)
        if message and not normalized_errors and (
            explicit_ok is False
            or blend_file_exists is False
            or output_folder_exists is False
            or can_write_output is False
        ):
            normalized_errors.append(str(message))

        return cls(
            workerId=worker.id,
            workerName=worker.name,
            workerIp=worker.ip,
            workerPort=worker.port,
            blendFileExists=bool(blend_file_exists) if blend_file_exists is not None else None,
            outputFolderExists=bool(output_folder_exists) if output_folder_exists is not None else None,
            canWriteOutput=bool(can_write_output) if can_write_output is not None else None,
            errors=normalized_errors,
            rawPayload=dict(payload),
        )

    def to_log_text(self) -> str:
        parts = [
            f"{self.workerName} ({self.workerIp}:{self.workerPort})",
            f"blendFileExists={self._format_bool(self.blendFileExists)}",
            f"outputFolderExists={self._format_bool(self.outputFolderExists)}",
            f"canWriteOutput={self._format_bool(self.canWriteOutput)}",
        ]
        if self.errors:
            parts.append("errors=" + " | ".join(self.errors))
        return ", ".join(parts)

    def to_display_text(self) -> str:
        lines = [
            f"Worker {self.workerName} ({self.workerIp}:{self.workerPort})",
            f"- blendFileExists: {self._format_bool(self.blendFileExists)}",
            f"- outputFolderExists: {self._format_bool(self.outputFolderExists)}",
            f"- canWriteOutput: {self._format_bool(self.canWriteOutput)}",
            "- errors:",
        ]
        if self.errors:
            lines.extend([f"  - {item}" for item in self.errors])
        else:
            lines.append("  - none")
        return "\n".join(lines)

    def _format_bool(self, value: bool | None) -> str:
        if value is True:
            return "true"
        if value is False:
            return "false"
        return "unknown"

    def _explicit_validation_flag(self) -> bool | None:
        return self._payload_validation_flag(self.rawPayload)

    @staticmethod
    def _payload_validation_flag(payload: dict[str, Any]) -> bool | None:
        for key in ("ok", "valid", "success"):
            if key in payload:
                return bool(payload.get(key))
        return None


@dataclass
class RenderBlock:
    blockId: str
    startFrame: int
    endFrame: int
    status: str = "pending"
    workerId: str | None = None
    assignedAt: str | None = None
    startedAt: str | None = None
    finishedAt: str | None = None
    attemptCount: int = 0
    errorMessage: str | None = None
    verificationDueAt: float | None = None

    def frames(self) -> list[int]:
        return list(range(self.startFrame, self.endFrame + 1))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RenderBlock":
        return cls(**data)


@dataclass
class RenderJob:
    jobId: str
    blendFile: str
    outputFolder: str
    outputPattern: str
    startFrame: int
    endFrame: int
    blockSize: int
    status: str = "created"
    createdAt: str = field(default_factory=now_iso)
    startedAt: str | None = None
    finishedAt: str | None = None
    blocks: list[RenderBlock] = field(default_factory=list)
    completedFrames: list[int] = field(default_factory=list)
    pendingFrames: list[int] = field(default_factory=list)
    errorFrames: list[int] = field(default_factory=list)
    ignoreRenderedFrames: bool = True
    autoAddWorkers: bool = True
    redistributeOfflineBlocks: bool = True
    selectedWorkerIds: list[str] = field(default_factory=list)
    nextBlockSequence: int = 1
    format: str = "PNG"
    # Legacy compatibility hint only. The worker decides the real render device.
    useGpu: bool = True
    requiredBlenderVersion: str | None = None
    preferredBlenderMajor: int | None = None
    versionRoutingPolicy: str | None = None
    blendVersionDetected: str | None = None
    lastScanAt: float | None = None

    @property
    def total_frames(self) -> int:
        return max(0, self.endFrame - self.startFrame + 1)

    def elapsed_seconds(self) -> float:
        start = parse_datetime(self.startedAt)
        if not start:
            return 0.0
        end = parse_datetime(self.finishedAt) or datetime.now()
        return max(0.0, (end - start).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["blocks"] = [block.to_dict() for block in self.blocks]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RenderJob":
        blocks = [RenderBlock.from_dict(item) for item in data.get("blocks", [])]
        payload = dict(data)
        payload["blocks"] = blocks
        return cls(**payload)
