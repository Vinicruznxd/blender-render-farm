from __future__ import annotations

from pathlib import Path

from app.core.settings import ViewerSettings, load_settings, save_settings


class PersistenceService:
    def __init__(self, base_dir: Path, config_path: Path) -> None:
        self.base_dir = base_dir
        self.config_path = config_path
        self.logs_dir = self.base_dir / "app" / "data" / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def load_settings(self) -> ViewerSettings:
        return load_settings(self.config_path)

    def save_settings(self, settings: ViewerSettings) -> None:
        save_settings(self.config_path, settings)
