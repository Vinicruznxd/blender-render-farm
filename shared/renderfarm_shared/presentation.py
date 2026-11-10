"""Presentation helpers shared by the desktop clients (no GUI dependency)."""
from __future__ import annotations

SESSION_STATUS_LABELS = {
    'queued': 'Queued', 'session_open': 'Session ready',
    'draining': 'Finishing job', 'closing': 'Closing session',
}

from datetime import datetime, timezone
import math
from pathlib import Path
import sys

from .i18n import tr


def parse_timestamp(value: object) -> datetime | None:
    """Accept API ISO timestamps and legacy Unix seconds, consistently in UTC."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, datetime):
            result = value
        else:
            text = str(value).strip()
            if not text:
                return None
            try:
                seconds = float(text)
            except ValueError:
                result = datetime.fromisoformat(text.replace("Z", "+00:00"))
            else:
                if not math.isfinite(seconds):
                    return None
                result = datetime.fromtimestamp(seconds, timezone.utc)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def relative_age(value: object, language: str = "en", *, now: datetime | None = None,
                 empty: str = "-") -> str:
    parsed = parse_timestamp(value)
    if parsed is None:
        return empty
    current = parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
    seconds = max(0, int((current - parsed).total_seconds()))
    if seconds < 10:
        return tr("Just now", language)
    for divisor, limit, key in ((1, 60, "{count}s ago"), (60, 3600, "{count}m ago"),
                                (3600, 86400, "{count}h ago")):
        if seconds < limit:
            return tr(key, language, count=seconds // divisor)
    return tr("{count}d ago", language, count=seconds // 86400)


def resource_path(base_dir: Path, *parts: str) -> Path:
    """Read bundled assets without changing the writable configuration directory."""
    bundle = getattr(sys, "_MEIPASS", None)
    roots = ([Path(bundle)] if bundle else []) + [Path(base_dir)]
    for root in roots:
        candidate = root.joinpath(*parts)
        if candidate.is_file():
            return candidate
    return Path(base_dir).joinpath(*parts)
