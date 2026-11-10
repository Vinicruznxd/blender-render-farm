from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import getpass
import json
import socket
from uuid import uuid4

from app.core.models import Worker
from renderfarm_shared.i18n import normalize_language


DEFAULT_CONFIG: dict[str, Any] = {
    "language": "en",
    "mode": "coordinator",
    "coordinator_url": "",
    "master_id": "",
    "master_name": "",
    "default_worker_port": 9000,
    "worker_poll_interval_ms": 1000,
    "worker_timeout_seconds": 60,
    "default_block_size": 10,
    "max_block_retries": 2,
    "workers": [
        {
            "id": "mock-worker-local",
            "name": "MOCK-WORKER-01",
            "ip": "127.0.0.1",
            "port": 9000,
            "enabled": True,
            "selected": True,
        }
    ],
}


@dataclass
class AppSettings:
    language: str = "en"
    mode: str = "coordinator"
    coordinator_url: str = ""
    master_id: str = ""
    master_name: str = ""
    default_worker_port: int = 9000
    worker_poll_interval_ms: int = 1000
    worker_timeout_seconds: int = 60
    default_block_size: int = 10
    max_block_retries: int = 2
    workers: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        merged = dict(DEFAULT_CONFIG)
        merged.update(data or {})
        if "workers" not in merged or merged["workers"] is None:
            merged["workers"] = []
        settings = cls(**merged)
        settings.mode = (settings.mode or "coordinator").strip().lower()
        settings.language = normalize_language(settings.language)
        if settings.mode not in {"direct", "coordinator"}:
            settings.mode = "coordinator"
        settings.coordinator_url = (settings.coordinator_url or "").strip().rstrip("/")
        settings.master_id = (settings.master_id or "").strip()
        settings.master_name = (settings.master_name or "").strip()
        return settings

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_coordinator_mode(self) -> bool:
        return self.mode == "coordinator"

    def build_workers(self) -> list[Worker]:
        return [Worker.from_config_entry(entry) for entry in self.workers]

    def sync_workers(self, workers: list[Worker]) -> None:
        self.workers = [worker.to_config_entry() for worker in workers]

    def ensure_master_identity(self) -> bool:
        changed = False
        if not self.master_id:
            self.master_id = f"master_{uuid4().hex}"
            changed = True
        if not self.master_name:
            hostname = socket.gethostname().strip() or "MASTER"
            username = getpass.getuser().strip() or "user"
            self.master_name = f"{hostname.upper()}-{username.upper()}"
            changed = True
        return changed


def ensure_config(config_path: Path) -> None:
    if config_path.exists():
        return
    config_path.write_text(
        json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_settings(config_path: Path) -> AppSettings:
    ensure_config(config_path)
    with config_path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    return AppSettings.from_dict(data)


def save_settings(config_path: Path, settings: AppSettings) -> None:
    config_path.write_text(
        json.dumps(settings.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
