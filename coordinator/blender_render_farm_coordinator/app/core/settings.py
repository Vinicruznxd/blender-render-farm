from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import sys
from uuid import uuid4

from renderfarm_shared.i18n import SUPPORTED_LANGUAGES, normalize_language


DEFAULT_COORDINATOR_ID = "coord_local_01"


@dataclass(slots=True)
class AppSettings:
    project_root: Path
    coordinator_id: str
    host: str
    port: int
    database_path: Path
    logs_dir: Path
    general_log_name: str
    scheduler_interval_seconds: float
    lease_duration_seconds: int
    worker_request_timeout_seconds: float
    worker_status_grace_seconds: int
    max_frame_attempts: int
    discovery_timeout_seconds: float
    discovery_ports: tuple[int, ...]
    coordinator_discovery_enabled: bool
    coordinator_discovery_port: int
    coordinator_discovery_namespace: str
    coordinator_discovery_broadcast_interval_seconds: float
    coordinator_discovery_probe_timeout_seconds: float
    log_tail_lines: int
    auto_start_scheduler: bool
    auto_unblock_blocked_jobs: bool
    job_worker_consecutive_failure_limit: int = 3
    job_worker_failure_history_limit: int = 5
    worker_probe_concurrency: int = 16
    worker_validation_cache_seconds: float = 10.0
    language: str = "en"
    config_path: Path | None = None

    @classmethod
    def load(cls, config_path: Path | None = None) -> "AppSettings":
        project_root = resolve_project_root()
        resolved_config = config_path or project_root / "config.json"
        raw = _load_raw_config(resolved_config)
        coordinator_id = str(raw.get("coordinatorId") or "").strip()
        if not coordinator_id or coordinator_id == DEFAULT_COORDINATOR_ID:
            coordinator_id = f"coord_{uuid4().hex}"
            raw["coordinatorId"] = coordinator_id
            _save_raw_config(resolved_config, raw)

        def resolve_path(key: str, default: str) -> Path:
            candidate = Path(raw.get(key, default))
            if candidate.is_absolute():
                return candidate
            return (project_root / candidate).resolve()

        return cls(
            project_root=project_root,
            coordinator_id=coordinator_id,
            host=raw.get("host", "0.0.0.0"),
            port=int(raw.get("port", 8010)),
            database_path=resolve_path("databasePath", "app/data/coordinator.db"),
            logs_dir=resolve_path("logsDir", "app/data/logs"),
            general_log_name=raw.get("generalLogName", "coordinator.log"),
            scheduler_interval_seconds=float(raw.get("schedulerIntervalSeconds", 2.0)),
            lease_duration_seconds=int(raw.get("leaseDurationSeconds", 300)),
            worker_request_timeout_seconds=float(raw.get("workerRequestTimeoutSeconds", 15.0)),
            worker_status_grace_seconds=int(raw.get("workerStatusGraceSeconds", 20)),
            max_frame_attempts=int(raw.get("maxFrameAttempts", 3)),
            discovery_timeout_seconds=float(raw.get("discoveryTimeoutSeconds", 1.5)),
            discovery_ports=tuple(int(port) for port in raw.get("discoveryPorts", [9000])),
            coordinator_discovery_enabled=bool(raw.get("coordinatorDiscoveryEnabled", True)),
            coordinator_discovery_port=int(raw.get("coordinatorDiscoveryPort", 8011)),
            coordinator_discovery_namespace=raw.get("coordinatorDiscoveryNamespace", "blender_render_farm"),
            coordinator_discovery_broadcast_interval_seconds=float(raw.get("coordinatorDiscoveryBroadcastIntervalSeconds", 5.0)),
            coordinator_discovery_probe_timeout_seconds=float(raw.get("coordinatorDiscoveryProbeTimeoutSeconds", 1.0)),
            log_tail_lines=int(raw.get("logTailLines", 200)),
            auto_start_scheduler=bool(raw.get("autoStartScheduler", True)),
            auto_unblock_blocked_jobs=bool(raw.get("autoUnblockBlockedJobs", True)),
            job_worker_consecutive_failure_limit=int(raw.get("jobWorkerConsecutiveFailureLimit", 3)),
            job_worker_failure_history_limit=int(raw.get("jobWorkerFailureHistoryLimit", 5)),
            worker_probe_concurrency=max(1, int(raw.get("workerProbeConcurrency", 16))),
            worker_validation_cache_seconds=max(0.0, float(raw.get("workerValidationCacheSeconds", 10.0))),
            language=normalize_language(raw.get("language"), "en"),
            config_path=resolved_config,
        )

    def update_language(self, language: object) -> str:
        normalized = normalize_language(language, "en")
        if str(language or "").strip().lower() not in SUPPORTED_LANGUAGES:
            raise ValueError(f"Unsupported language: {language!r}")
        config_path = self.config_path or self.project_root / "config.json"
        raw = _load_raw_config(config_path)
        raw["language"] = normalized
        _save_raw_config(config_path, raw)
        self.language = normalized
        return normalized


def resolve_project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _load_raw_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8-sig"))


def _save_raw_config(config_path: Path, raw: dict) -> None:
    config_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
