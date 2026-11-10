from __future__ import annotations

from pathlib import Path
import json

from app.core.models import RenderJob, Worker
from app.core.settings import AppSettings, load_settings, save_settings


class PersistenceService:
    def __init__(self, base_dir: Path, config_path: Path) -> None:
        self.base_dir = base_dir
        self.jobs_dir = self.base_dir / "app" / "data" / "jobs"
        self.logs_dir = self.base_dir / "app" / "data" / "logs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = config_path

    def save_job(self, job: RenderJob) -> Path:
        path = self.jobs_dir / f"{job.jobId}.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(job.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)
        return path

    def load_job(self, path: Path) -> RenderJob:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return RenderJob.from_dict(data)

    def list_job_files(self) -> list[Path]:
        return sorted(self.jobs_dir.glob("*.json"))

    def load_settings(self) -> AppSettings:
        settings = load_settings(self.config_path)
        if settings.ensure_master_identity():
            save_settings(self.config_path, settings)
        return settings

    def save_settings(self, settings: AppSettings, workers: list[Worker]) -> None:
        settings.sync_workers(workers)
        save_settings(self.config_path, settings)
