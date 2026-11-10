from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from renderfarm_shared.i18n import normalize_language

DEFAULT_CONFIG = {
    "language": "en",
    "port": 9000,
    "host": "0.0.0.0",
    "workerName": "",
    "blenderPath": "",
    "allowNewerMajorBlender": True,
    "coordinatorDiscoveryEnabled": True,
    "coordinatorDiscoveryPort": 8011,
    "renderDevicePreference": "GPU",
    "gpuBackend": "AUTO",
    "gpuDeviceIds": [],
    "gpuEnableAll": True,
    "gpuIncludeCpu": False,
    "persistCpuFallback": False,
    "showRenderDeviceWindow": True,
    "allowJobs": True,
    "persistentSessionEnabled": False,
    "persistentSessionIdleTimeoutSeconds": 120,
    "persistentSessionMaxQueueDepth": 2,
    "persistentSessionOpenTimeoutSeconds": 30,
    "authToken": "",
    "logLevel": "INFO",
}

RENDER_DEVICE_CHOICES = {"CPU", "GPU"}
GPU_BACKEND_CHOICES = {"AUTO", "OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"}

COMMON_BLENDER_PATHS = [
    r"C:\Program Files\Blender Foundation\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.4\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.1\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.0\blender.exe",
    r"C:\Program Files (x86)\Blender Foundation\blender.exe",
    r"C:\Program Files (x86)\Blender Foundation\Blender\blender.exe",
]


class WorkerConfig(BaseModel):
    language: str = Field(default="en")
    port: int = Field(default=9000)
    host: str = Field(default="0.0.0.0")
    workerName: str = Field(default="")
    blenderPath: str = Field(default="")
    allowNewerMajorBlender: bool = Field(default=True)
    coordinatorDiscoveryEnabled: bool = Field(default=True)
    coordinatorDiscoveryPort: int = Field(default=8011)
    renderDevicePreference: str = Field(default="GPU")
    gpuBackend: str = Field(default="AUTO")
    gpuDeviceIds: list[str] = Field(default_factory=list)
    gpuEnableAll: bool = Field(default=True)
    gpuIncludeCpu: bool = Field(default=False)
    persistCpuFallback: bool = Field(default=False)
    showRenderDeviceWindow: bool = Field(default=True)
    allowJobs: bool = Field(default=True)
    persistentSessionEnabled: bool = Field(default=False)
    persistentSessionIdleTimeoutSeconds: int = Field(default=120)
    persistentSessionMaxQueueDepth: int = Field(default=2)
    persistentSessionOpenTimeoutSeconds: int = Field(default=30)
    authToken: str = Field(default="")
    logLevel: str = Field(default="INFO")

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if value < 1 or value > 65535:
            raise ValueError("port must be between 1 and 65535")
        return value

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return normalize_language(value)

    @field_validator("coordinatorDiscoveryPort")
    @classmethod
    def validate_discovery_port(cls, value: int) -> int:
        if value < 1 or value > 65535:
            raise ValueError("coordinatorDiscoveryPort must be between 1 and 65535")
        return value

    @field_validator(
        "persistentSessionIdleTimeoutSeconds",
        "persistentSessionMaxQueueDepth",
        "persistentSessionOpenTimeoutSeconds",
    )
    @classmethod
    def validate_positive_session_value(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("persistent session values must be greater than zero")
        return value

    @field_validator("logLevel")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if normalized not in allowed:
            raise ValueError(f"logLevel must be one of: {', '.join(sorted(allowed))}")
        return normalized

    @field_validator("renderDevicePreference")
    @classmethod
    def validate_render_device_preference(cls, value: str) -> str:
        return normalize_render_device_preference(value)

    @field_validator("gpuBackend")
    @classmethod
    def validate_gpu_backend(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in GPU_BACKEND_CHOICES:
            raise ValueError(f"gpuBackend must be one of: {', '.join(sorted(GPU_BACKEND_CHOICES))}")
        return normalized

    @field_validator("gpuDeviceIds")
    @classmethod
    def validate_gpu_device_ids(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]

    @property
    def resolved_worker_name(self) -> str:
        return self.workerName.strip() or socket.gethostname()

    @property
    def auth_enabled(self) -> bool:
        return bool(self.authToken.strip())


def ensure_config_file(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")


def load_config(config_path: Path) -> WorkerConfig:
    ensure_config_file(config_path)
    with config_path.open("r", encoding="utf-8-sig") as file:
        raw_data = json.load(file)
    merged_data = DEFAULT_CONFIG | raw_data
    return WorkerConfig.model_validate(merged_data)


def save_config(config_path: Path, config: WorkerConfig) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config.model_dump(), indent=2),
        encoding="utf-8",
    )


def normalize_render_device_preference(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in RENDER_DEVICE_CHOICES:
        raise ValueError(
            f"renderDevicePreference must be one of: {', '.join(sorted(RENDER_DEVICE_CHOICES))}"
        )
    return normalized


def render_device_preference_to_use_gpu(value: str) -> bool:
    return normalize_render_device_preference(value) == "GPU"


class ConfigManager:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self._lock = threading.RLock()
        self._config = load_config(config_path)

    @property
    def config(self) -> WorkerConfig:
        return self._config

    def set_render_device_preference(self, value: str) -> str:
        normalized = normalize_render_device_preference(value)
        with self._lock:
            self._config.renderDevicePreference = normalized
            save_config(self.config_path, self._config)
        return normalized

    def set_language(self, value: str) -> str:
        normalized = normalize_language(value)
        with self._lock:
            self._config.language = normalized
            save_config(self.config_path, self._config)
        return normalized
