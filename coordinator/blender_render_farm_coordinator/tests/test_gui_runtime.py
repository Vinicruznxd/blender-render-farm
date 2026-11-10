from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest

from app.api.routes import create_app
from app.core.settings import AppSettings
from app.gui.runtime import CoordinatorServerRuntime
from app.gui.window import launch_coordinator_window


class DummyWorkerClient:
    pass


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_runtime_starts_server_and_stops_cleanly(tmp_path: Path):
    port = _unused_tcp_port()
    project_root = tmp_path / "gui_runtime_project"
    settings = AppSettings(
        project_root=project_root,
        coordinator_id="coord_runtime_test",
        host="127.0.0.1",
        port=port,
        database_path=project_root / "app" / "data" / "coordinator.db",
        logs_dir=project_root / "app" / "data" / "logs",
        general_log_name="coordinator.log",
        scheduler_interval_seconds=0.1,
        lease_duration_seconds=1,
        worker_request_timeout_seconds=0.1,
        worker_status_grace_seconds=5,
        max_frame_attempts=3,
        discovery_timeout_seconds=0.1,
        discovery_ports=(9000,),
        coordinator_discovery_enabled=False,
        coordinator_discovery_port=18021,
        coordinator_discovery_namespace="blender_render_farm_runtime_test",
        coordinator_discovery_broadcast_interval_seconds=0.25,
        coordinator_discovery_probe_timeout_seconds=0.1,
        log_tail_lines=50,
        auto_start_scheduler=False,
        auto_unblock_blocked_jobs=True,
    )
    app = create_app(settings=settings, worker_client=DummyWorkerClient())
    runtime = CoordinatorServerRuntime(app, startup_timeout_seconds=5.0)

    assert runtime.dashboard_url() == f"http://127.0.0.1:{port}/dashboard"
    runtime.start()
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
        assert response.status_code == 200
        assert response.json()["coordinatorId"] == "coord_runtime_test"
        assert runtime.is_running
    finally:
        runtime.stop()

    assert not runtime.is_running


def test_launch_stops_runtime_when_window_creation_fails(tmp_path: Path, monkeypatch):
    port = _unused_tcp_port()
    project_root = tmp_path / "gui_launch_project"
    settings = AppSettings(
        project_root=project_root,
        coordinator_id="coord_launch_test",
        host="127.0.0.1",
        port=port,
        database_path=project_root / "app" / "data" / "coordinator.db",
        logs_dir=project_root / "app" / "data" / "logs",
        general_log_name="coordinator.log",
        scheduler_interval_seconds=0.1,
        lease_duration_seconds=1,
        worker_request_timeout_seconds=0.1,
        worker_status_grace_seconds=5,
        max_frame_attempts=3,
        discovery_timeout_seconds=0.1,
        discovery_ports=(9000,),
        coordinator_discovery_enabled=False,
        coordinator_discovery_port=18022,
        coordinator_discovery_namespace="blender_render_farm_runtime_test",
        coordinator_discovery_broadcast_interval_seconds=0.25,
        coordinator_discovery_probe_timeout_seconds=0.1,
        log_tail_lines=50,
        auto_start_scheduler=False,
        auto_unblock_blocked_jobs=True,
    )
    app = create_app(settings=settings, worker_client=DummyWorkerClient())
    captured: dict[str, CoordinatorServerRuntime] = {}

    class CapturingRuntime(CoordinatorServerRuntime):
        def __init__(self, app, startup_timeout_seconds: float = 10.0):
            super().__init__(app, startup_timeout_seconds=startup_timeout_seconds)
            captured["runtime"] = self

    class ExplodingWindow:
        def __init__(self, app, runtime):
            raise RuntimeError("boom")

    monkeypatch.setattr("app.gui.window.CoordinatorServerRuntime", CapturingRuntime)
    monkeypatch.setattr("app.gui.window.CoordinatorWindow", ExplodingWindow)

    with pytest.raises(RuntimeError, match="boom"):
        launch_coordinator_window(app)

    runtime = captured["runtime"]
    assert not runtime.is_running
