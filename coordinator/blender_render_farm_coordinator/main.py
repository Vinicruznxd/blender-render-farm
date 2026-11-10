from __future__ import annotations

from pathlib import Path
import sys
import traceback

from app.api.routes import create_app
from app.gui.window import launch_coordinator_window


def resolve_runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def write_startup_error(exc: BaseException) -> None:
    error_path = resolve_runtime_root() / "startup_error.txt"
    details = [
        "Blender Render Farm Coordinator startup error",
        "",
        f"type: {type(exc).__name__}",
        f"message: {exc}",
        "",
        traceback.format_exc(),
    ]
    cause = exc.__cause__
    if cause is not None:
        details.extend(
            [
                "",
                f"cause_type: {type(cause).__name__}",
                f"cause_message: {cause}",
                "",
                "".join(traceback.format_exception(type(cause), cause, cause.__traceback__)),
            ]
        )
    error_path.write_text("\n".join(details), encoding="utf-8")


def main() -> int:
    app = create_app()
    launch_coordinator_window(app)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code in {0, None}:
            raise
        write_startup_error(exc)
        raise
