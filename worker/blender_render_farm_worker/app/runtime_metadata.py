from __future__ import annotations

import json
from pathlib import Path
from typing import Any


APP_NAME = "Blender Render Farm Worker"
APP_VERSION = "1.2.2"
PROTOCOL_VERSION = 2
BUILD_INFO_FILENAME = "build_info.json"


def load_runtime_metadata(runtime_root: Path) -> dict[str, Any]:
    build_info_path = runtime_root / BUILD_INFO_FILENAME
    payload: dict[str, Any] = {
        "appName": APP_NAME,
        "appVersion": APP_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "buildId": "dev-local",
        "buildTimestampUtc": "",
        "bundleHash": "",
    }
    if not build_info_path.exists():
        return payload

    try:
        raw = json.loads(build_info_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return payload

    payload.update(
        {
            "buildId": str(raw.get("buildId") or payload["buildId"]).strip() or payload["buildId"],
            "buildTimestampUtc": str(raw.get("buildTimestampUtc") or "").strip(),
            "bundleHash": str(raw.get("bundleHash") or "").strip(),
        }
    )
    return payload
