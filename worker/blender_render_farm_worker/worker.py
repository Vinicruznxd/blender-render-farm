from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from app.api import create_app


def resolve_project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_ROOT = resolve_project_root()
SERVER_START_TIMEOUT_SECONDS = 15.0

app = create_app(PROJECT_ROOT)


def run_server(application: Any | None = None) -> None:
    application = application or app
    config = application.state.config
    uvicorn.run(
        application,
        host=config.host,
        port=config.port,
        reload=False,
        log_level=config.logLevel.lower(),
        log_config=None,
        access_log=False,
    )


def build_server(application: Any) -> uvicorn.Server:
    config = application.state.config
    server_config = uvicorn.Config(
        app=application,
        host=config.host,
        port=config.port,
        reload=False,
        log_level=config.logLevel.lower(),
        log_config=None,
        access_log=False,
    )
    return uvicorn.Server(server_config)


def resolve_startup_failure_message(application: Any, fallback_message: str) -> str:
    status_manager = application.state.status_manager
    snapshot = status_manager.status_snapshot()
    startup_error = str(snapshot.get("startupError") or "").strip()
    api_state = str(snapshot.get("apiServerState") or "").strip().lower()
    api_message = str(snapshot.get("apiServerMessage") or "").strip()
    error = str(snapshot.get("error") or "").strip()
    candidates = [startup_error]
    if api_state not in {"", "starting"}:
        candidates.append(api_message)
    candidates.append(error)
    for candidate in candidates:
        if candidate:
            return candidate
    return fallback_message


def wait_for_server_start(
    application: Any,
    server: uvicorn.Server,
    server_thread: threading.Thread,
    *,
    timeout_seconds: float = SERVER_START_TIMEOUT_SECONDS,
) -> tuple[bool, str | None]:
    status_manager = application.state.status_manager
    logger = application.state.logger
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if server.started:
            status_manager.mark_api_server_running("API server is listening for requests")
            logger.info("Worker API server started successfully")
            return True, None
        if not server_thread.is_alive():
            message = resolve_startup_failure_message(
                application,
                "Worker API failed to start before opening the local window",
            )
            status_manager.mark_startup_error(message)
            logger.error("Worker API server thread exited before startup completed: %s", message)
            return False, message
        time.sleep(0.1)

    message = resolve_startup_failure_message(
        application,
        f"Worker API did not become ready within {int(timeout_seconds)} seconds",
    )
    status_manager.mark_startup_error(message)
    server.should_exit = True
    logger.error("Worker API server startup timed out: %s", message)
    return False, message


def main(application: Any | None = None) -> None:
    application = application or app
    config = application.state.config

    if not config.showRenderDeviceWindow:
        run_server(application)
        return

    render_device_window = application.state.render_device_window
    logger = application.state.logger
    status_manager = application.state.status_manager
    status_manager.mark_api_server_starting("Launching local API server")
    server = build_server(application)

    server_thread = threading.Thread(
        target=server.run,
        name="uvicorn-server",
        daemon=False,
    )
    server_thread.start()

    started, startup_error = wait_for_server_start(application, server, server_thread)
    if not started:
        if server_thread.is_alive():
            server_thread.join(timeout=15)
        render_device_window.show_startup_error(startup_error or "Worker API failed to start")
        return

    def close_application() -> None:
        logger.info("Window close requested, stopping worker")
        server.should_exit = True
        deadline = time.time() + 15
        while server_thread.is_alive() and time.time() < deadline:
            time.sleep(0.1)

    window_started = render_device_window.run(on_close=close_application)
    if not window_started:
        logger.warning("Render device window unavailable, continuing in headless mode")
        server_thread.join()
        return

    if server_thread.is_alive():
        server.should_exit = True
        server_thread.join(timeout=15)


if __name__ == "__main__":
    main()
