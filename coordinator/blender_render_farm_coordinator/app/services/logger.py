from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.core.database import Database
from app.core.models import utc_now_iso
from app.core.settings import AppSettings


class AppLogger:
    def __init__(self, settings: AppSettings, database: Database):
        self.settings = settings
        self.database = database
        self.logs_dir = settings.logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.general_log_path = self.logs_dir / settings.general_log_name
        self._logger = logging.getLogger(f"blender-render-coordinator:{self.general_log_path}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

        if not self._logger.handlers:
            handler = logging.FileHandler(self.general_log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            self._logger.addHandler(handler)

    def info(
        self,
        message: str,
        *,
        category: str = "general",
        entity_type: str | None = None,
        entity_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._log("INFO", message, category, entity_type, entity_id, details)

    def warning(
        self,
        message: str,
        *,
        category: str = "general",
        entity_type: str | None = None,
        entity_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._log("WARNING", message, category, entity_type, entity_id, details)

    def error(
        self,
        message: str,
        *,
        category: str = "general",
        entity_type: str | None = None,
        entity_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._log("ERROR", message, category, entity_type, entity_id, details)

    def _log(
        self,
        level: str,
        message: str,
        category: str,
        entity_type: str | None,
        entity_id: str | None,
        details: dict[str, Any] | None,
    ) -> None:
        payload = {
            "level": level,
            "category": category,
            "entityType": entity_type,
            "entityId": entity_id,
            "message": message,
            "details": json.dumps(details, ensure_ascii=True) if details else None,
            "createdAt": utc_now_iso(),
        }
        log_line = message if not details else f"{message} | {json.dumps(details, ensure_ascii=True)}"
        getattr(self._logger, level.lower())(log_line)
        self.database.execute(
            """
            INSERT INTO events(level, category, entityType, entityId, message, details, createdAt)
            VALUES(:level, :category, :entityType, :entityId, :message, :details, :createdAt)
            """,
            payload,
        )

    def tail_general_log(self, lines: int | None = None) -> str:
        if not self.general_log_path.exists():
            return ""
        target_lines = lines or self.settings.log_tail_lines
        content = self.general_log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(content[-target_lines:])

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT id, level, category, entityType, entityId, message, details, createdAt
            FROM events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
