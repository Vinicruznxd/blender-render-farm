from __future__ import annotations

import platform
import socket
import subprocess
from pathlib import Path

import psutil

from .config import WorkerConfig
from .runtime_metadata import load_runtime_metadata
from .subprocess_utils import decode_subprocess_output
from .version_routing import SUPPORTED_VERSION_ROUTING_POLICIES


def detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


class CapabilitiesService:
    def __init__(self, config: WorkerConfig, logger, runtime_root: Path | None = None) -> None:
        self.config = config
        self.logger = logger
        self._cache: dict[str, object] = {}
        self.runtime_metadata = load_runtime_metadata(runtime_root or Path.cwd())

    def refresh(self) -> None:
        gpus = self._detect_gpus()
        self._cache = {
            "machineName": self.config.resolved_worker_name,
            "os": platform.system() or "Windows",
            "cpu": self._detect_cpu_name(),
            "ramGb": int(round(psutil.virtual_memory().total / (1024**3))),
            "gpu": ", ".join(str(item["name"]) for item in gpus) if gpus else "unknown",
            "gpus": gpus,
            "gpuCount": len(gpus),
            "ip": detect_local_ip(),
            "port": self.config.port,
        }

    def cached_gpu_name(self) -> str:
        return str(self._cache.get("gpu", "") or "unknown")

    def as_dict(
        self,
        *,
        blender_found: bool,
        blender_path: str,
        blender_version: str,
    ) -> dict[str, object]:
        if not self._cache:
            self.refresh()
        payload = dict(self._cache)
        gpu_name = str(payload.get("gpu", "") or "")
        payload.update(
            {
                "blenderFound": blender_found,
                "blenderPath": blender_path,
                "blenderVersion": blender_version,
                "renderDevicePreference": self.config.renderDevicePreference,
                "effectiveUseGpu": self.config.renderDevicePreference == "GPU",
                "gpuBackend": self.config.gpuBackend,
                "gpuDeviceIds": list(self.config.gpuDeviceIds),
                "gpuEnableAll": bool(self.config.gpuEnableAll),
                "gpuIncludeCpu": bool(self.config.gpuIncludeCpu),
                "renderSlots": [
                    {
                        "slotId": "combined",
                        "mode": "multi_gpu_combined",
                        "deviceIds": list(self.config.gpuDeviceIds),
                        "enableAll": bool(self.config.gpuEnableAll),
                        "capacity": 1,
                    }
                ],
                "supportsGpu": gpu_name.lower() not in {"", "unknown"},
                "appVersion": self.runtime_metadata["appVersion"],
                "protocolVersion": self.runtime_metadata["protocolVersion"],
                "buildId": self.runtime_metadata["buildId"],
                "buildTimestampUtc": self.runtime_metadata["buildTimestampUtc"],
                "versionValidationSupported": True,
                "supportsBlendVersionHints": True,
                "supportedBlendCompressions": ["raw", "gzip", "zstd"],
                "supportedBlendHeaderFormats": ["legacy_v0", "modern_v1"],
                "supportedVersionRoutingPolicies": list(SUPPORTED_VERSION_ROUTING_POLICIES),
            }
        )
        return payload

    def _detect_cpu_name(self) -> str:
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)",
        ]
        value = self._run_command(command)
        return value or platform.processor() or "unknown"

    def _detect_gpu_name(self) -> str:
        gpus = self._detect_gpus()
        return ", ".join(str(item["name"]) for item in gpus) if gpus else "unknown"

    def _detect_gpus(self) -> list[dict[str, object]]:
        nvidia_output = self._run_command(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ]
        )
        nvidia_gpus: list[dict[str, object]] = []
        for line in nvidia_output.splitlines():
            parts = [part.strip() for part in line.split(",", 4)]
            if len(parts) != 5:
                continue
            try:
                memory_mb: int | None = int(parts[3])
            except ValueError:
                memory_mb = None
            nvidia_gpus.append(
                {
                    "index": int(parts[0]) if parts[0].isdigit() else parts[0],
                    "id": parts[1],
                    "name": parts[2],
                    "memoryMb": memory_mb,
                    "driverVersion": parts[4],
                    "vendor": "NVIDIA",
                }
            )
        if nvidia_gpus:
            return nvidia_gpus

        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name)",
        ]
        value = self._run_command(command)
        if not value:
            return []
        gpu_names = [line.strip() for line in value.splitlines() if line.strip()]
        return [
            {"index": index, "id": name, "name": name, "vendor": "unknown"}
            for index, name in enumerate(dict.fromkeys(gpu_names))
        ]

    def _run_command(self, command: list[str]) -> str:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=False,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self.logger.debug("Capability detection command failed: %s", exc)
            return ""

        stdout = decode_subprocess_output(result.stdout).strip()
        if result.returncode != 0 and not stdout:
            stderr = decode_subprocess_output(result.stderr).strip()
            self.logger.debug("Capability detection returned %s: %s", result.returncode, stderr)
        return stdout
