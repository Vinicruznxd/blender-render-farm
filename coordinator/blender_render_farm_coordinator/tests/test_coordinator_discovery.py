from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app.core.coordinator_discovery import CoordinatorDiscoveryService, DuplicateCoordinatorError
from app.core.settings import AppSettings


class StubLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


def _unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(tmp_path: Path, *, coordinator_id: str, port: int, discovery_port: int, namespace: str) -> AppSettings:
    project_root = tmp_path / coordinator_id
    return AppSettings(
        project_root=project_root,
        coordinator_id=coordinator_id,
        host="0.0.0.0",
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
        coordinator_discovery_enabled=True,
        coordinator_discovery_port=discovery_port,
        coordinator_discovery_namespace=namespace,
        coordinator_discovery_broadcast_interval_seconds=0.2,
        coordinator_discovery_probe_timeout_seconds=0.2,
        log_tail_lines=50,
        auto_start_scheduler=False,
        auto_unblock_blocked_jobs=True,
    )


def test_discovery_service_answers_probe(tmp_path: Path):
    discovery_port = _unused_udp_port()
    settings = _settings(
        tmp_path,
        coordinator_id="coord_test_a",
        port=8010,
        discovery_port=discovery_port,
        namespace="farm_test_probe",
    )
    discovery = CoordinatorDiscoveryService(
        settings,
        StubLogger(),
        started_at="2026-05-25T00:00:00+00:00",
        discovery_targets_override=["127.0.0.1"],
    )
    discovery.start()
    try:
        responses = CoordinatorDiscoveryService.discover_coordinators(
            port=discovery_port,
            namespace="farm_test_probe",
            timeout_seconds=0.2,
            target_hosts=["127.0.0.1"],
        )
    finally:
        discovery.stop()

    assert len(responses) == 1
    assert responses[0]["coordinatorId"] == "coord_test_a"
    assert responses[0]["apiPort"] == 8010
    assert "http://127.0.0.1:8010" in responses[0]["apiUrls"]


def test_start_fails_when_another_coordinator_is_already_announcing(tmp_path: Path):
    discovery_port = _unused_udp_port()
    namespace = "farm_test_duplicate"
    first = CoordinatorDiscoveryService(
        _settings(
            tmp_path,
            coordinator_id="coord_first",
            port=8010,
            discovery_port=discovery_port,
            namespace=namespace,
        ),
        StubLogger(),
        started_at="2026-05-25T00:00:00+00:00",
        discovery_targets_override=["127.0.0.1"],
    )
    second = CoordinatorDiscoveryService(
        _settings(
            tmp_path,
            coordinator_id="coord_second",
            port=8012,
            discovery_port=discovery_port,
            namespace=namespace,
        ),
        StubLogger(),
        started_at="2026-05-25T00:01:00+00:00",
        discovery_targets_override=["127.0.0.1"],
    )

    first.start()
    try:
        with pytest.raises(DuplicateCoordinatorError):
            second.start()
    finally:
        first.stop()


def test_assert_can_bind_reports_clear_error_when_udp_port_is_busy(tmp_path: Path):
    discovery_port = _unused_udp_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("", discovery_port))

    discovery = CoordinatorDiscoveryService(
        _settings(
            tmp_path,
            coordinator_id="coord_bind_test",
            port=8010,
            discovery_port=discovery_port,
            namespace="farm_test_bind",
        ),
        StubLogger(),
        started_at="2026-05-25T00:00:00+00:00",
        discovery_targets_override=["127.0.0.1"],
    )
    try:
        with pytest.raises(RuntimeError, match="porta UDP de discovery"):
            discovery.assert_can_bind()
    finally:
        holder.close()
