from __future__ import annotations

import json
from pathlib import Path

import pytest


def _register_worker(
    client,
    ip: str = "127.0.0.1",
    port: int = 9000,
    name: str = "WORKER-A",
    render_device_preference: str = "GPU",
):
    response = client.post(
        "/workers",
        json={
            "name": name,
            "ip": ip,
            "port": port,
            "enabled": True,
            "renderDevicePreference": render_device_preference,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_job(client, output_folder: Path, *, name: str, start_frame: int, end_frame: int, **extra_fields):
    output_folder.mkdir(parents=True, exist_ok=True)
    blend_path = output_folder / f"{name}.blend"
    blend_path.write_text("blend mock", encoding="utf-8")
    payload = {
        "jobName": name,
        "blendFile": str(blend_path),
        "outputFolder": str(output_folder),
        "startFrame": start_frame,
        "endFrame": end_frame,
        "blockSize": 10,
        "ignoreRenderedFrames": True,
        "autoAddWorkers": True,
        "redistributeOfflineBlocks": True,
        "legacyUseGpu": True,
        "submittedByMasterId": "master_a",
        "submittedByMasterName": "MASTER-A",
        "submittedByUser": "vinicius",
    }
    payload.update(extra_fields)
    response = client.post(
        "/jobs",
        json=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _version_validation_response(
    *,
    detected: str,
    selected: str,
    compatible: bool,
    required: str | None = None,
    preferred_major: int | None = None,
    policy: str | None = None,
    reason: str | None = None,
):
    def builder(payload, worker):
        response = {
            "valid": True,
            "blendAccessible": True,
            "outputAccessible": True,
            "compatible": compatible,
            "versionCompatible": compatible,
            "blendVersionDetected": detected,
            "requiredBlenderVersion": required or detected,
            "preferredBlenderMajor": preferred_major,
            "versionRoutingPolicy": policy,
            "selectedBlenderVersion": selected,
        }
        if reason:
            response["compatibilityReason"] = reason
        return response

    return builder


class _FailOnProbeClient:
    def health(self, base_url: str):
        raise AssertionError(f"Regular worker client should not probe discovery target {base_url}.")

    def status(self, base_url: str):
        raise AssertionError(f"Regular worker client should not probe discovery target {base_url}.")

    def capabilities(self, base_url: str):
        raise AssertionError(f"Regular worker client should not probe discovery target {base_url}.")


class _DiscoveryProbeClient:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def health(self, base_url: str) -> dict:
        self.calls.append(("health", base_url))
        return {"workerId": "worker_discovery_9000", "name": "DISCOVERY-WORKER"}

    def status(self, base_url: str) -> dict:
        self.calls.append(("status", base_url))
        return {
            "workerId": "worker_discovery_9000",
            "name": "DISCOVERY-WORKER",
            "status": "idle",
            "blenderVersion": "5.0",
            "progress": 0.0,
        }

    def capabilities(self, base_url: str) -> dict:
        self.calls.append(("capabilities", base_url))
        return {
            "blenderFound": True,
            "blenderVersion": "5.0",
            "gpu": "RTX Discovery",
            "renderDevicePreference": "GPU",
        }


def test_language_is_persisted_and_exposed_to_clients(coordinator_app):
    app, client, _, _ = coordinator_app

    assert client.get("/settings/language").json()["language"] == "en"
    updated = client.put("/settings/language", json={"language": "pt"})
    assert updated.status_code == 200
    assert updated.json()["language"] == "pt"
    assert client.get("/health").json()["language"] == "pt"
    assert client.get("/state").json()["coordinator"]["language"] == "pt"

    heartbeat = client.post(
        "/masters/heartbeat",
        json={
            "masterId": "master_language",
            "masterName": "MASTER-LANGUAGE",
            "host": "127.0.0.1",
            "username": "test",
            "version": "1.2.0",
        },
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["language"] == "pt"
    assert app.state.service.settings.language == "pt"

def test_multiple_masters_and_state_snapshot(coordinator_app):
    app, client, fake_worker_client, _ = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_a",
        name="WORKER-A",
    )
    _register_worker(client)

    response_a = client.post(
        "/masters/heartbeat",
        json={
            "masterId": "master_a",
            "masterName": "MASTER-A",
            "host": "192.168.0.10",
            "username": "vinicius",
            "version": "1.0.0",
        },
    )
    response_b = client.post(
        "/masters/heartbeat",
        json={
            "masterId": "master_b",
            "masterName": "MASTER-B",
            "host": "192.168.0.11",
            "username": "laura",
            "version": "1.0.0",
        },
    )
    assert response_a.status_code == 200
    assert response_b.status_code == 200

    app.state.service.run_scheduler_cycle()
    snapshot = client.get("/state")
    assert snapshot.status_code == 200
    payload = snapshot.json()
    assert payload["coordinator"]["coordinatorId"] == "coord_test_01"
    assert len(payload["masters"]) == 2
    assert len(payload["workers"]) == 1
    assert payload["metrics"]["machinesOnline"] == 1


def test_state_hides_stale_masters_and_workers(coordinator_app):
    app, client, _, _ = coordinator_app

    client.post(
        "/masters/heartbeat",
        json={
            "masterId": "master_stale",
            "masterName": "MASTER-STALE",
            "host": "192.168.0.10",
            "username": "vinicius",
            "version": "1.0.0",
        },
    )
    client.post(
        "/masters/heartbeat",
        json={
            "masterId": "master_fresh",
            "masterName": "MASTER-FRESH",
            "host": "192.168.0.11",
            "username": "laura",
            "version": "1.0.0",
        },
    )
    app.state.database.execute(
        "UPDATE masters SET lastSeen = ? WHERE masterId = ?",
        ("2000-01-01T00:00:00+00:00", "master_stale"),
    )

    app.state.worker_registry.register_or_update_worker(
        {
            "workerId": "worker_stale",
            "name": "WORKER-STALE",
            "ip": "127.0.0.10",
            "port": 9000,
            "status": "offline",
        }
    )
    app.state.database.execute(
        """
        UPDATE workers
        SET status = ?, lastSeen = ?, createdAt = ?, updatedAt = ?
        WHERE workerId = ?
        """,
        ("offline", "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", "worker_stale"),
    )
    app.state.worker_registry.register_or_update_worker(
        {
            "workerId": "worker_fresh",
            "name": "WORKER-FRESH",
            "ip": "127.0.0.11",
            "port": 9000,
            "status": "idle",
            "lastSeen": app.state.service.started_at,
        }
    )

    payload = client.get("/state").json()

    assert [master["masterId"] for master in payload["masters"]] == ["master_fresh"]
    assert [worker["workerId"] for worker in payload["workers"]] == ["worker_fresh"]
    assert payload["metrics"]["machinesOnline"] == 1


def test_state_keeps_recently_offline_worker_visible(coordinator_app):
    app, client, _, _ = coordinator_app

    app.state.worker_registry.register_or_update_worker(
        {
            "workerId": "worker_recent_offline",
            "name": "WORKER-RECENT-OFFLINE",
            "ip": "127.0.0.12",
            "port": 9000,
            "status": "offline",
            "lastSeen": app.state.service.started_at,
            "error": "timed out",
        }
    )

    payload = client.get("/state").json()

    assert [worker["workerId"] for worker in payload["workers"]] == ["worker_recent_offline"]
    assert payload["metrics"]["machinesOnline"] == 0


def test_state_normalizes_fractional_worker_progress(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(base_url, worker_id="worker_a", name="WORKER-A", auto_complete=False)
    _register_worker(client)
    job = _create_job(client, tmp_path / "job_state_progress_fractional", name="CenaState01", start_frame=1, end_frame=1)

    app.state.service.run_scheduler_cycle()
    fake_worker_client.workers[base_url]["progress"] = 0.5
    app.state.worker_registry.refresh_workers()

    payload = client.get("/state").json()
    worker = next(item for item in payload["workers"] if item["name"] == "WORKER-A")

    assert payload["activeJob"]["jobId"] == job["jobId"]
    assert worker["id"] == worker["workerId"]
    assert worker["name"] == "WORKER-A"
    assert worker["status"] == "rendering"
    assert worker["currentJobId"] == job["jobId"]
    assert worker["currentBlockId"] == "frame_000001"
    assert worker["currentFrame"] == 1
    assert worker["progress"] == 50.0
    assert "lastLog" in worker
    assert "error" in worker
    assert worker["blenderFound"] is True
    assert worker["blenderVersion"] == "4.2-mock"
    assert worker["gpu"] == "RTX Mock"
    assert worker["renderDevicePreference"] == "GPU"
    assert worker["effectiveUseGpu"] is True


def test_state_preserves_percent_worker_progress(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(base_url, worker_id="worker_a", name="WORKER-A", auto_complete=False)
    _register_worker(client)
    _create_job(client, tmp_path / "job_state_progress_percent", name="CenaState02", start_frame=1, end_frame=1)

    app.state.service.run_scheduler_cycle()
    fake_worker_client.workers[base_url]["progress"] = 75.0
    app.state.worker_registry.refresh_workers()

    payload = client.get("/state").json()
    worker = next(item for item in payload["workers"] if item["name"] == "WORKER-A")

    assert worker["progress"] == 75.0


def test_dashboard_route_renders_html_shell(coordinator_app):
    _, client, _, _ = coordinator_app

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Coordinator Web Dashboard" in response.text
    assert "Relatorio por job" in response.text
    assert "Desempenho por maquina" in response.text
    assert "By Vinicius D'Arcadia Cruz - https://novaxd.tech/" in response.text


def test_gpu_only_job_dispatches_only_to_gpu_workers(coordinator_app, tmp_path):
    app, client, fake_worker_client, _ = coordinator_app
    gpu_url = "http://127.0.0.1:9000"
    cpu_url = "http://127.0.0.2:9000"
    fake_worker_client.add_worker(gpu_url, worker_id="worker_gpu", name="WORKER-GPU", auto_complete=False)
    fake_worker_client.add_worker(
        cpu_url,
        worker_id="worker_cpu",
        name="WORKER-CPU",
        auto_complete=False,
        render_device_preference="CPU",
        effective_use_gpu=False,
    )
    _register_worker(client, ip="127.0.0.1", port=9000, name="WORKER-GPU", render_device_preference="GPU")
    _register_worker(client, ip="127.0.0.2", port=9000, name="WORKER-CPU", render_device_preference="CPU")

    job = _create_job(
        client,
        tmp_path / "job_gpu_only_dispatch",
        name="CenaGpuOnly01",
        start_frame=1,
        end_frame=1,
        gpuOnlyWorkers=True,
    )

    app.state.service.run_scheduler_cycle()

    assert fake_worker_client.render_requests
    assert fake_worker_client.render_requests[0]["baseUrl"] == gpu_url
    assert client.get(f"/jobs/{job['jobId']}").json()["gpuOnlyWorkers"] is True

    preflight = client.post(
        "/jobs/preflight",
        json={
            "jobName": "CenaGpuOnlyPreview",
            "blendFile": job["blendFile"],
            "outputFolder": job["outputFolder"],
            "startFrame": 1,
            "endFrame": 1,
            "gpuOnlyWorkers": True,
        },
    )
    assert preflight.status_code == 200, preflight.text
    worker_validation = preflight.json()["workerValidation"]
    cpu_validation = next(item for item in worker_validation if item["workerName"] == "WORKER-CPU")
    assert cpu_validation["valid"] is False
    assert cpu_validation["gpuOnlyFiltered"] is True


def test_gpu_only_job_blocks_when_only_cpu_workers_exist(coordinator_app, tmp_path):
    app, client, fake_worker_client, _ = coordinator_app
    cpu_url = "http://127.0.0.2:9000"
    fake_worker_client.add_worker(
        cpu_url,
        worker_id="worker_cpu",
        name="WORKER-CPU",
        auto_complete=False,
        render_device_preference="CPU",
        effective_use_gpu=False,
    )
    _register_worker(client, ip="127.0.0.2", port=9000, name="WORKER-CPU", render_device_preference="CPU")

    job = _create_job(
        client,
        tmp_path / "job_gpu_only_blocked",
        name="CenaGpuOnly02",
        start_frame=1,
        end_frame=1,
        gpuOnlyWorkers=True,
    )

    app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    assert payload["status"] == "blocked"
    assert "GPU-only workers" in payload["lastError"]


def test_state_active_job_exposes_blocks(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(base_url, worker_id="worker_a", name="WORKER-A", auto_complete=False)
    _register_worker(client)
    job = _create_job(client, tmp_path / "job_state_blocks", name="CenaState03", start_frame=1, end_frame=3)

    app.state.service.run_scheduler_cycle()

    payload = client.get("/state").json()
    active_job = payload["activeJob"]

    assert active_job["jobId"] == job["jobId"]
    assert active_job["jobName"] == "CenaState03"
    assert active_job["blendFile"].endswith("CenaState03.blend")
    assert active_job["startFrame"] == 1
    assert active_job["endFrame"] == 3
    assert active_job["status"] == "running"
    assert len(active_job["blocks"]) == 3
    worker_id = payload["workers"][0]["workerId"]
    assert active_job["blocks"][0] == {
        "blockId": "frame_000001",
        "block_id": "frame_000001",
        "workerId": worker_id,
        "worker_id": worker_id,
        "status": "assigned",
        "rawStatus": "leased",
        "startFrame": 1,
        "start_frame": 1,
        "endFrame": 1,
        "end_frame": 1,
    }
    assert active_job["blocks"][1]["status"] == "pending"
    assert active_job["blocks"][2]["blockId"] == "frame_000003"


def test_job_details_include_worker_history_stats(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(base_url, worker_id="worker_a", name="WORKER-A", auto_complete=True)
    _register_worker(client)
    job = _create_job(client, tmp_path / "job_worker_history_stats", name="CenaStats01", start_frame=1, end_frame=1)

    app.state.service.run_scheduler_cycle()
    response = client.get(f"/jobs/{job['jobId']}")

    assert response.status_code == 200
    payload = response.json()
    assert "workerStats" in payload
    assert len(payload["workerStats"]) == 1
    stats = payload["workerStats"][0]
    assert stats["workerId"] == "worker_127_0_0_1_9000"
    assert stats["workerName"] == "WORKER-A"
    assert stats["renderDevicePreference"] == "GPU"
    assert stats["successCount"] == 1
    assert stats["failureCount"] == 0
    assert stats["consecutiveFailureCount"] == 0
    assert stats["lastFailureReason"] is None
    assert stats["lastFailureAt"] is None
    assert stats["lastSuccessAt"]
    assert stats["timedSuccessCount"] == 1
    assert stats["totalSuccessRenderSeconds"] >= 0.0
    assert stats["lastSuccessRenderSeconds"] >= 0.0
    assert stats["averageFrameSeconds"] >= 0.0


def test_tail_optimizer_waits_for_faster_same_job_worker_before_using_slow_worker(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fast_url = "http://127.0.0.1:9000"
    slow_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(fast_url, worker_id="worker_fast", name="FAST-WORKER", auto_complete=False)
    fake_worker_client.add_worker(slow_url, worker_id="worker_slow", name="SLOW-WORKER", auto_complete=False)
    fast_worker = _register_worker(client, port=9000, name="FAST-WORKER")
    slow_worker = _register_worker(client, port=9001, name="SLOW-WORKER")
    job = _create_job(client, tmp_path / "job_tail_wait_fast", name="CenaTailWait", start_frame=1, end_frame=3)

    started_at = "2026-06-15T13:00:00+00:00"
    lease_expires_at = "2026-06-15T14:00:00+00:00"
    app.state.database.execute(
        "UPDATE jobs SET status = ?, startedAt = ? WHERE jobId = ?",
        ("running", started_at, job["jobId"]),
    )
    app.state.database.execute(
        """
        UPDATE job_frames
        SET status = ?, workerId = ?, assignmentId = ?, leasedAt = ?, leaseExpiresAt = ?, startedAt = ?, attemptCount = ?
        WHERE jobId = ? AND frameNumber = ?
        """,
        (
            "rendering",
            fast_worker["workerId"],
            "lease_fast_000001",
            started_at,
            lease_expires_at,
            started_at,
            1,
            job["jobId"],
            1,
        ),
    )
    app.state.database.execute(
        """
        INSERT INTO job_worker_stats(
            jobId, workerId, renderDevicePreference,
            successCount, timedSuccessCount, totalSuccessRenderSeconds, lastSuccessRenderSeconds,
            failureCount, consecutiveFailureCount, lastSuccessAt
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["jobId"],
            fast_worker["workerId"],
            "GPU",
            4,
            4,
            40.0,
            10.0,
            0,
            0,
            started_at,
        ),
    )
    app.state.database.execute(
        """
        INSERT INTO job_worker_stats(
            jobId, workerId, renderDevicePreference,
            successCount, timedSuccessCount, totalSuccessRenderSeconds, lastSuccessRenderSeconds,
            failureCount, consecutiveFailureCount, lastSuccessAt
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["jobId"],
            slow_worker["workerId"],
            "GPU",
            2,
            2,
            60.0,
            30.0,
            0,
            0,
            started_at,
        ),
    )

    fake_worker_client.workers[fast_url].update(
        {
            "status": "rendering",
            "currentJobId": job["jobId"],
            "currentBlockId": "frame_000001",
            "currentFrame": 1,
            "currentAssignmentId": "lease_fast_000001",
            "progress": 50.0,
        }
    )
    fake_worker_client.workers[slow_url].update(
        {
            "status": "idle",
            "currentJobId": None,
            "currentBlockId": None,
            "currentFrame": None,
            "currentAssignmentId": None,
            "progress": 0.0,
        }
    )

    app.state.service.run_scheduler_cycle()

    assert fake_worker_client.render_requests == []
    payload = client.get(f"/jobs/{job['jobId']}").json()
    pending_frames = {
        frame["frameNumber"]
        for frame in payload["frames"]
        if frame["status"] == "pending"
    }
    assert pending_frames == {2, 3}


def test_tail_optimizer_still_uses_slow_worker_when_that_finishes_sooner_overall(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fast_url = "http://127.0.0.1:9000"
    slow_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(fast_url, worker_id="worker_fast", name="FAST-WORKER", auto_complete=False)
    fake_worker_client.add_worker(slow_url, worker_id="worker_slow", name="SLOW-WORKER", auto_complete=False)
    fast_worker = _register_worker(client, port=9000, name="FAST-WORKER")
    slow_worker = _register_worker(client, port=9001, name="SLOW-WORKER")
    job = _create_job(client, tmp_path / "job_tail_use_slow", name="CenaTailUseSlow", start_frame=1, end_frame=4)

    started_at = "2026-06-15T13:00:00+00:00"
    lease_expires_at = "2026-06-15T14:00:00+00:00"
    app.state.database.execute(
        "UPDATE jobs SET status = ?, startedAt = ? WHERE jobId = ?",
        ("running", started_at, job["jobId"]),
    )
    app.state.database.execute(
        """
        UPDATE job_frames
        SET status = ?, workerId = ?, assignmentId = ?, leasedAt = ?, leaseExpiresAt = ?, startedAt = ?, attemptCount = ?
        WHERE jobId = ? AND frameNumber = ?
        """,
        (
            "rendering",
            fast_worker["workerId"],
            "lease_fast_000001",
            started_at,
            lease_expires_at,
            started_at,
            1,
            job["jobId"],
            1,
        ),
    )
    app.state.database.execute(
        """
        INSERT INTO job_worker_stats(
            jobId, workerId, renderDevicePreference,
            successCount, timedSuccessCount, totalSuccessRenderSeconds, lastSuccessRenderSeconds,
            failureCount, consecutiveFailureCount, lastSuccessAt
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["jobId"],
            fast_worker["workerId"],
            "GPU",
            4,
            4,
            40.0,
            10.0,
            0,
            0,
            started_at,
        ),
    )
    app.state.database.execute(
        """
        INSERT INTO job_worker_stats(
            jobId, workerId, renderDevicePreference,
            successCount, timedSuccessCount, totalSuccessRenderSeconds, lastSuccessRenderSeconds,
            failureCount, consecutiveFailureCount, lastSuccessAt
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["jobId"],
            slow_worker["workerId"],
            "GPU",
            2,
            2,
            60.0,
            30.0,
            0,
            0,
            started_at,
        ),
    )

    fake_worker_client.workers[fast_url].update(
        {
            "status": "rendering",
            "currentJobId": job["jobId"],
            "currentBlockId": "frame_000001",
            "currentFrame": 1,
            "currentAssignmentId": "lease_fast_000001",
            "progress": 50.0,
        }
    )
    fake_worker_client.workers[slow_url].update(
        {
            "status": "idle",
            "currentJobId": None,
            "currentBlockId": None,
            "currentFrame": None,
            "currentAssignmentId": None,
            "progress": 0.0,
        }
    )

    app.state.service.run_scheduler_cycle()

    assert len(fake_worker_client.render_requests) == 1
    assert fake_worker_client.render_requests[0]["baseUrl"] == slow_url
    payload = client.get(f"/jobs/{job['jobId']}").json()
    pending_frames = {
        frame["frameNumber"]
        for frame in payload["frames"]
        if frame["status"] == "pending"
    }
    assert pending_frames == {2, 3}


def test_tail_optimizer_requests_early_drain_for_persistent_worker_without_future_frames(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fast_url = "http://127.0.0.1:9000"
    slow_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(
        fast_url,
        worker_id="worker_fast",
        name="FAST-WORKER",
        auto_complete=False,
        persistent_session_enabled=True,
    )
    fake_worker_client.add_worker(
        slow_url,
        worker_id="worker_slow",
        name="SLOW-WORKER",
        auto_complete=False,
        persistent_session_enabled=True,
    )
    fast_worker = _register_worker(client, port=9000, name="FAST-WORKER")
    slow_worker = _register_worker(client, port=9001, name="SLOW-WORKER")
    job = _create_job(client, tmp_path / "job_tail_early_drain", name="CenaTailDrain", start_frame=1, end_frame=3)

    started_at = "2026-06-15T13:00:00+00:00"
    lease_expires_at = "2026-06-15T14:00:00+00:00"
    app.state.database.execute(
        "UPDATE jobs SET status = ?, startedAt = ? WHERE jobId = ?",
        ("running", started_at, job["jobId"]),
    )
    app.state.database.execute(
        """
        UPDATE job_frames
        SET status = ?, workerId = ?, assignmentId = ?, leasedAt = ?, leaseExpiresAt = ?, startedAt = ?, attemptCount = ?
        WHERE jobId = ? AND frameNumber = ?
        """,
        (
            "rendering",
            fast_worker["workerId"],
            "lease_fast_000001",
            started_at,
            lease_expires_at,
            started_at,
            1,
            job["jobId"],
            1,
        ),
    )
    app.state.database.execute(
        """
        UPDATE job_frames
        SET status = ?, workerId = ?, assignmentId = ?, leasedAt = ?, leaseExpiresAt = ?, startedAt = ?, attemptCount = ?
        WHERE jobId = ? AND frameNumber = ?
        """,
        (
            "rendering",
            slow_worker["workerId"],
            "lease_slow_000002",
            started_at,
            lease_expires_at,
            started_at,
            1,
            job["jobId"],
            2,
        ),
    )
    app.state.database.execute(
        """
        INSERT INTO job_worker_stats(
            jobId, workerId, renderDevicePreference,
            successCount, timedSuccessCount, totalSuccessRenderSeconds, lastSuccessRenderSeconds,
            failureCount, consecutiveFailureCount, lastSuccessAt
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["jobId"],
            fast_worker["workerId"],
            "GPU",
            4,
            4,
            40.0,
            10.0,
            0,
            0,
            started_at,
        ),
    )
    app.state.database.execute(
        """
        INSERT INTO job_worker_stats(
            jobId, workerId, renderDevicePreference,
            successCount, timedSuccessCount, totalSuccessRenderSeconds, lastSuccessRenderSeconds,
            failureCount, consecutiveFailureCount, lastSuccessAt
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["jobId"],
            slow_worker["workerId"],
            "GPU",
            2,
            2,
            60.0,
            30.0,
            0,
            0,
            started_at,
        ),
    )

    fake_worker_client.workers[fast_url].update(
        {
            "status": "rendering",
            "sessionState": "rendering",
            "currentJobId": job["jobId"],
            "currentBlockId": "frame_000001",
            "currentFrame": 1,
            "currentAssignmentId": "lease_fast_000001",
            "progress": 50.0,
        }
    )
    fake_worker_client.workers[slow_url].update(
        {
            "status": "rendering",
            "sessionState": "rendering",
            "currentJobId": job["jobId"],
            "currentBlockId": "frame_000002",
            "currentFrame": 2,
            "currentAssignmentId": "lease_slow_000002",
            "progress": 0.0,
        }
    )

    app.state.service.run_scheduler_cycle()

    assert len(fake_worker_client.render_requests) == 1
    assert fake_worker_client.render_requests[0]["baseUrl"] == fast_url
    assert fake_worker_client.job_drain_requests[-1]["jobId"] == job["jobId"]
    assert fake_worker_client.job_drain_requests[-1]["baseUrl"] == slow_url
    assert fake_worker_client.workers[slow_url]["sessionDrainRequested"] is True
    assert fake_worker_client.workers[slow_url]["status"] == "draining"


def test_state_metrics_recalculate_overall_percent_from_completed_frames(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(base_url, worker_id="worker_a", name="WORKER-A", auto_complete=False)
    _register_worker(client)
    _create_job(client, tmp_path / "job_state_metrics", name="CenaState04", start_frame=1, end_frame=2)

    app.state.service.run_scheduler_cycle()
    first_request = fake_worker_client.render_requests[0]
    fake_worker_client.complete_current_frame(base_url, first_request)
    app.state.service.run_scheduler_cycle()

    payload = client.get("/state").json()
    metrics = payload["metrics"]

    assert metrics["total_frames"] == 2
    assert metrics["completed_frames"] == 1
    assert metrics["pending_frames"] == 0
    assert metrics["rendering_frames"] == 1
    assert metrics["error_frames"] == 0
    assert metrics["overall_percent"] == 50.0
    assert metrics["machines_online"] == 1
    assert metrics["machines_rendering"] == 1
    assert metrics["totalQueuedJobs"] == 0
    assert metrics["machinesOnline"] == 1
    assert metrics["machinesRendering"] == 1
    assert metrics["elapsed_seconds"] >= 0.0


def test_discovery_uses_dedicated_probe_client(coordinator_app, monkeypatch):
    app, client, _, _ = coordinator_app
    discovery_client = _DiscoveryProbeClient()
    app.state.worker_registry.worker_client = _FailOnProbeClient()
    app.state.worker_registry.discovery_worker_client = discovery_client
    monkeypatch.setattr(app.state.worker_registry, "_is_worker_port_open", lambda host, port: True)

    response = client.post(
        "/workers/discover",
        json={
            "hosts": ["127.0.0.1"],
            "ports": [9000],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["count"] == 1
    workers = client.get("/workers").json()
    assert workers[0]["name"] == "DISCOVERY-WORKER"
    assert [call[0] for call in discovery_client.calls] == ["health", "status", "capabilities"]


def test_create_job_accepts_missing_submitter_metadata_with_defaults(coordinator_app, tmp_path):
    _, client, _, _ = coordinator_app
    output_folder = tmp_path / "job_missing_submitter_output"
    output_folder.mkdir(parents=True, exist_ok=True)
    blend_path = output_folder / "CenaSemMaster.blend"
    blend_path.write_text("blend mock", encoding="utf-8")

    response = client.post(
        "/jobs",
        json={
            "jobName": "CenaSemMaster",
            "blendFile": str(blend_path),
            "outputFolder": str(output_folder),
            "startFrame": 1,
            "endFrame": 2,
            "blockSize": 1,
        },
    )

    assert response.status_code == 200, response.text
    payload = client.get(f"/jobs/{response.json()['jobId']}").json()
    assert payload["submittedByMasterId"] == "master_unknown"
    assert payload["submittedByMasterName"]
    assert payload["submittedByUser"]


def test_application_serves_icon_in_docs(coordinator_app):
    _, client, _, _ = coordinator_app
    docs = client.get("/docs")
    icon = client.get("/img/icon_coordinator.png")
    favicon = client.get("/favicon.ico")

    assert docs.status_code == 200
    assert "/img/icon_coordinator.png" in docs.text
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/png"
    assert favicon.status_code == 200


def test_discovery_deduplicates_same_local_worker_aliases(coordinator_app, monkeypatch):
    app, client, fake_worker_client, _ = coordinator_app
    fake_worker_client.add_worker("http://127.0.0.1:9000", worker_id=None, name="WORKER-LOCAL")
    fake_worker_client.add_worker("http://localhost:9000", worker_id=None, name="WORKER-LOCAL")
    fake_worker_client.add_worker("http://192.168.0.167:9000", worker_id=None, name="WORKER-LOCAL")

    monkeypatch.setattr(app.state.worker_registry, "_local_ipv4_addresses", lambda: {"192.168.0.167"})
    monkeypatch.setattr(app.state.worker_registry, "_is_worker_port_open", lambda host, port: True)

    response = client.post(
        "/workers/discover",
        json={
            "hosts": ["127.0.0.1", "localhost", "192.168.0.167"],
            "ports": [9000],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    workers = client.get("/workers").json()

    assert payload["count"] == 1
    assert len(workers) == 1
    assert workers[0]["port"] == 9000


def test_fifo_queue_and_single_active_job(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=False,
    )
    _register_worker(client)

    job1 = _create_job(client, tmp_path / "job1_output", name="Cena01", start_frame=1, end_frame=1)
    job2 = _create_job(client, tmp_path / "job2_output", name="Cena02", start_frame=2, end_frame=2)

    app.state.service.run_scheduler_cycle()
    jobs = client.get("/jobs").json()
    active_job = next(job for job in jobs if job["jobId"] == job1["jobId"])
    queued_job = next(job for job in jobs if job["jobId"] == job2["jobId"])

    assert active_job["status"] == "running"
    assert queued_job["status"] == "queued"
    assert fake_worker_client.render_requests[0]["startFrame"] == 1
    assert fake_worker_client.render_requests[0]["endFrame"] == 1


def test_dispatches_frames_individually_and_confirms_by_real_file(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=True,
    )
    _register_worker(client)
    job = _create_job(client, tmp_path / "job3_output", name="Cena03", start_frame=1, end_frame=3)

    for _ in range(5):
        app.state.service.run_scheduler_cycle()

    details = client.get(f"/jobs/{job['jobId']}")
    assert details.status_code == 200
    payload = details.json()
    assert payload["status"] == "completed"
    assert [request["startFrame"] for request in fake_worker_client.render_requests] == [1, 2, 3]
    assert all(request["startFrame"] == request["endFrame"] for request in fake_worker_client.render_requests)
    assert all(frame["status"] == "done" for frame in payload["frames"])


def test_frame_is_reserved_before_worker_http_dispatch(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(base_url, worker_id="worker_a", name="WORKER-A")
    _register_worker(client)
    job = _create_job(client, tmp_path / "atomic_dispatch", name="Atomic", start_frame=1, end_frame=1)
    original_render_block = fake_worker_client.render_block

    def inspect_reservation(url, payload):
        frame = app.state.database.fetch_one(
            "SELECT status, assignmentId, workerId FROM job_frames WHERE jobId = ? AND frameNumber = ?",
            (job["jobId"], 1),
        )
        assert frame is not None
        assert frame["status"] == "leased"
        assert frame["assignmentId"] == payload["assignmentId"]
        assert frame["workerId"] == "worker_127_0_0_1_9000"
        return original_render_block(url, payload)

    fake_worker_client.render_block = inspect_reservation
    app.state.service.run_scheduler_cycle()
    assert len(fake_worker_client.render_requests) == 1


def test_rerender_does_not_complete_from_preexisting_output(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(base_url, worker_id="worker_a", name="WORKER-A")
    _register_worker(client)
    output_folder = tmp_path / "fresh_output"
    output_folder.mkdir(parents=True)
    old_output = output_folder / "frame_0001.png"
    old_output.write_text("old", encoding="utf-8")
    job = _create_job(
        client,
        output_folder,
        name="Fresh",
        start_frame=1,
        end_frame=1,
        outputPattern=str(output_folder / "frame_####"),
        ignoreRenderedFrames=False,
    )
    app.state.service.run_scheduler_cycle()
    app.state.service.run_scheduler_cycle()
    details = client.get(f"/jobs/{job['jobId']}").json()
    assert details["status"] == "running"
    assert details["frames"][0]["status"] in {"leased", "rendering"}


def test_rejects_overlapping_active_jobs_with_same_output_pattern(coordinator_app):
    _app, client, _fake_worker_client, tmp_path = coordinator_app
    output_folder = tmp_path / "collision"
    first = _create_job(
        client,
        output_folder,
        name="CollisionA",
        start_frame=1,
        end_frame=10,
        outputPattern=str(output_folder / "frame_####"),
    )
    assert first["status"] == "queued"
    output_folder.mkdir(parents=True, exist_ok=True)
    blend_path = output_folder / "CollisionB.blend"
    blend_path.write_text("blend mock", encoding="utf-8")
    response = client.post(
        "/jobs",
        json={
            "jobName": "CollisionB",
            "blendFile": str(blend_path),
            "outputFolder": str(output_folder),
            "outputPattern": str(output_folder / "frame_####"),
            "startFrame": 5,
            "endFrame": 12,
            "blockSize": 1,
            "submittedByMasterId": "master_a",
            "submittedByMasterName": "MASTER-A",
            "submittedByUser": "vinicius",
        },
    )
    assert response.status_code == 400
    assert "Output collision" in response.json()["detail"]


def test_requeues_frame_when_worker_goes_offline(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(
        base_url,
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=False,
    )
    _register_worker(client)
    job = _create_job(client, tmp_path / "job4_output", name="Cena04", start_frame=10, end_frame=10)

    app.state.service.run_scheduler_cycle()
    first_snapshot = client.get(f"/jobs/{job['jobId']}").json()
    assert first_snapshot["frames"][0]["status"] in {"leased", "rendering"}

    fake_worker_client.set_online(base_url, False)
    app.state.worker_registry.update_worker(
        "worker_127_0_0_1_9000",
        {"lastSeen": "2000-01-01T00:00:00+00:00"},
    )
    app.state.service.run_scheduler_cycle()

    second_snapshot = client.get(f"/jobs/{job['jobId']}").json()
    assert second_snapshot["frames"][0]["status"] == "pending"
    assert second_snapshot["frames"][0]["workerId"] is None


def test_blocked_job_auto_unblocks_when_paths_become_valid(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(
        base_url,
        worker_id="worker_a",
        name="WORKER-A",
        valid_paths=False,
        auto_complete=True,
    )
    _register_worker(client)
    job = _create_job(client, tmp_path / "job5_output", name="Cena05", start_frame=20, end_frame=20)

    app.state.service.run_scheduler_cycle()
    blocked = client.get(f"/jobs/{job['jobId']}").json()
    assert blocked["status"] == "blocked"

    fake_worker_client.set_valid_paths(base_url, True)
    for _ in range(3):
        app.state.service.run_scheduler_cycle()

    recovered = client.get(f"/jobs/{job['jobId']}").json()
    assert recovered["status"] in {"running", "completed"}


def test_blocks_job_when_worker_reports_blender_version_incompatibility(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_a",
        name="WORKER-A",
        reported_blender_version="5.1",
        validate_paths_response=_version_validation_response(
            detected="5.2",
            required="5.2",
            selected="5.1",
            compatible=False,
            preferred_major=5,
            policy="prefer_same_major_then_higher",
            reason="Worker only has Blender 5.1; job requires 5.2 or newer.",
        ),
    )
    _register_worker(client)
    job = _create_job(
        client,
        tmp_path / "job6_output",
        name="Cena06",
        start_frame=1,
        end_frame=1,
        requiredBlenderVersion="5.2",
        preferredBlenderMajor=5,
        versionRoutingPolicy="prefer_same_major_then_higher",
    )

    app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    snapshot = client.get("/state").json()
    state_job = next(item for item in snapshot["jobs"] if item["jobId"] == job["jobId"])

    assert payload["status"] == "blocked"
    assert payload["blendVersionDetected"] == "5.2"
    assert payload["requiredBlenderVersion"] == "5.2"
    assert "Blender 5.2" in payload["lastError"]
    assert "WORKER-A" in payload["lastError"]
    assert state_job["blendVersionDetected"] == "5.2"


def test_block_log_includes_rejected_workers_with_reason(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_old",
        name="WORKER-OLD",
        reported_blender_version="5.1",
        validate_paths_response=_version_validation_response(
            detected="5.2",
            required="5.2",
            selected="5.1",
            compatible=False,
            preferred_major=5,
            policy="prefer_same_major_then_higher",
            reason="Worker only has Blender 5.1; job requires 5.2 or newer.",
        ),
    )
    _register_worker(client, name="WORKER-OLD")
    job = _create_job(
        client,
        tmp_path / "job6b_output",
        name="Cena06B",
        start_frame=1,
        end_frame=1,
        requiredBlenderVersion="5.2",
        preferredBlenderMajor=5,
        versionRoutingPolicy="prefer_same_major_then_higher",
    )

    app.state.service.run_scheduler_cycle()

    logs = client.get("/logs").json()
    blocked_event = next(
        item
        for item in logs["events"]
        if item["entityId"] == job["jobId"]
        and item["message"] == f"Job '{job['jobId']}' is blocked: No enabled workers are compatible with Blender 5.2. WORKER-OLD: Worker only has Blender 5.1; job requires 5.2 or newer."
    )
    details = json.loads(blocked_event["details"])

    assert len(details["rejectedWorkers"]) == 1
    assert details["rejectedWorkers"][0]["workerName"] == "WORKER-OLD"
    assert details["rejectedWorkers"][0]["valid"] is False
    assert details["rejectedWorkers"][0]["reason"] == "Worker only has Blender 5.1; job requires 5.2 or newer."
    assert details["rejectedWorkers"][0]["versionCompatible"] is False
    assert details["rejectedWorkers"][0]["selectedBlenderVersion"] == "5.1"


def test_accepts_job_when_worker_reports_matching_blender_version(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=True,
        reported_blender_version="5.2",
        validate_paths_response=_version_validation_response(
            detected="5.2",
            required="5.2",
            selected="5.2",
            compatible=True,
            preferred_major=5,
            policy="prefer_same_major_then_higher",
        ),
    )
    _register_worker(client)
    job = _create_job(
        client,
        tmp_path / "job7_output",
        name="Cena07",
        start_frame=1,
        end_frame=1,
        requiredBlenderVersion="5.2",
        preferredBlenderMajor=5,
        versionRoutingPolicy="prefer_same_major_then_higher",
    )

    for _ in range(4):
        app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()

    assert payload["status"] == "completed"
    assert payload["blendVersionDetected"] == "5.2"
    assert fake_worker_client.render_requests[0]["requiredBlenderVersion"] == "5.2"
    assert fake_worker_client.render_requests[0]["blendVersionDetected"] == "5.2"


def test_activation_log_includes_rejected_workers_with_reason(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_ok",
        name="WORKER-OK",
        auto_complete=False,
        reported_blender_version="5.0",
        validate_paths_response=_version_validation_response(
            detected="5.0",
            required="5.0",
            selected="5.0",
            compatible=True,
            preferred_major=5,
            policy="prefer_same_major_then_higher",
        ),
    )
    fake_worker_client.add_worker(
        "http://127.0.0.1:9001",
        worker_id="worker_old",
        name="WORKER-OLD",
        auto_complete=False,
        reported_blender_version="4.2",
        validate_paths_response=_version_validation_response(
            detected="5.0",
            required="5.0",
            selected="4.2",
            compatible=False,
            preferred_major=5,
            policy="prefer_same_major_then_higher",
            reason="Worker only has Blender 4.2; job requires 5.0 or newer.",
        ),
    )
    _register_worker(client, port=9000, name="WORKER-OK")
    _register_worker(client, port=9001, name="WORKER-OLD")
    job = _create_job(
        client,
        tmp_path / "job7b_output",
        name="Cena07B",
        start_frame=1,
        end_frame=1,
        requiredBlenderVersion="5.0",
        preferredBlenderMajor=5,
        versionRoutingPolicy="prefer_same_major_then_higher",
    )

    app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    logs = client.get("/logs").json()
    activation_event = next(
        item
        for item in logs["events"]
        if item["entityId"] == job["jobId"] and item["message"] == f"Job '{job['jobId']}' activated."
    )
    details = json.loads(activation_event["details"])

    assert payload["status"] == "running"
    assert len(details["validatedWorkers"]) == 1
    assert len(details["rejectedWorkers"]) == 1
    assert details["rejectedWorkers"][0]["workerName"] == "WORKER-OLD"
    assert details["rejectedWorkers"][0]["valid"] is False
    assert details["rejectedWorkers"][0]["reason"] == "Worker only has Blender 4.2; job requires 5.0 or newer."
    assert details["rejectedWorkers"][0]["versionCompatible"] is False
    assert details["rejectedWorkers"][0]["selectedBlenderVersion"] == "4.2"
    assert fake_worker_client.render_requests[0]["baseUrl"] == "http://127.0.0.1:9000"


def test_prefers_same_major_worker_for_4x_job_when_multiple_workers_are_compatible(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_5x",
        name="A-WORKER-5X",
        auto_complete=False,
        reported_blender_version="5.2",
        validate_paths_response=_version_validation_response(
            detected="4.5",
            required="4.5",
            selected="5.2",
            compatible=True,
            preferred_major=4,
            policy="prefer_same_major_then_higher",
        ),
    )
    fake_worker_client.add_worker(
        "http://127.0.0.1:9001",
        worker_id="worker_4x",
        name="Z-WORKER-4X",
        auto_complete=False,
        reported_blender_version="4.6",
        validate_paths_response=_version_validation_response(
            detected="4.5",
            required="4.5",
            selected="4.6",
            compatible=True,
            preferred_major=4,
            policy="prefer_same_major_then_higher",
        ),
    )
    _register_worker(client, port=9000, name="A-WORKER-5X")
    _register_worker(client, port=9001, name="Z-WORKER-4X")
    job = _create_job(
        client,
        tmp_path / "job8_output",
        name="Cena08",
        start_frame=1,
        end_frame=1,
        requiredBlenderVersion="4.5",
        preferredBlenderMajor=4,
        versionRoutingPolicy="prefer_same_major_then_higher",
    )

    app.state.service.run_scheduler_cycle()

    assert fake_worker_client.render_requests[0]["baseUrl"] == "http://127.0.0.1:9001"


def test_allows_higher_major_fallback_when_worker_reports_compatibility(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=True,
        reported_blender_version="5.2",
        validate_paths_response=_version_validation_response(
            detected="4.5",
            required="4.5",
            selected="5.2",
            compatible=True,
            preferred_major=4,
            policy="prefer_same_major_then_higher",
        ),
    )
    _register_worker(client)
    job = _create_job(
        client,
        tmp_path / "job9_output",
        name="Cena09",
        start_frame=1,
        end_frame=1,
        requiredBlenderVersion="4.5",
        preferredBlenderMajor=4,
        versionRoutingPolicy="prefer_same_major_then_higher",
    )

    for _ in range(4):
        app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()

    assert payload["status"] == "completed"
    assert fake_worker_client.render_requests[0]["baseUrl"] == "http://127.0.0.1:9000"
    assert payload["blendVersionDetected"] == "4.5"


def test_preflight_requests_version_choice_when_only_older_same_major_and_higher_major_exist(coordinator_app, tmp_path):
    _, client, fake_worker_client, _ = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_old",
        name="WORKER-OLD",
        reported_blender_version="4.3",
        validate_paths_response={
            "valid": True,
            "blendAccessible": True,
            "outputAccessible": True,
            "versionCompatible": False,
            "compatibilityReason": "Only older Blender 4.x is available on this worker.",
            "blendVersionDetected": "4.5",
            "requiredBlenderVersion": "4.5",
            "preferredBlenderMajor": 4,
            "versionRoutingPolicy": "prefer_same_major_then_higher",
            "selectedBlenderVersion": "4.3",
            "selectedVersionBucket": "same_major_older",
            "sameMajorOlderAvailable": True,
            "bestSameMajorOlderVersion": "4.3",
            "higherMajorAvailable": False,
        },
    )
    fake_worker_client.add_worker(
        "http://127.0.0.1:9001",
        worker_id="worker_high",
        name="WORKER-HIGH",
        reported_blender_version="5.1",
        validate_paths_response={
            "valid": True,
            "blendAccessible": True,
            "outputAccessible": True,
            "versionCompatible": True,
            "blendVersionDetected": "4.5",
            "requiredBlenderVersion": "4.5",
            "preferredBlenderMajor": 4,
            "versionRoutingPolicy": "prefer_same_major_then_higher",
            "selectedBlenderVersion": "5.1",
            "selectedVersionBucket": "higher_major",
            "sameMajorOlderAvailable": False,
            "higherMajorAvailable": True,
            "bestHigherMajorVersion": "5.1",
        },
    )
    _register_worker(client, port=9000, name="WORKER-OLD")
    _register_worker(client, port=9001, name="WORKER-HIGH")

    output_folder = tmp_path / "job_preflight_output"
    output_folder.mkdir(parents=True, exist_ok=True)
    blend_path = output_folder / "CenaPreflight.blend"
    blend_path.write_text("blend mock", encoding="utf-8")
    response = client.post(
        "/jobs/preflight",
        json={
            "jobName": "CenaPreflight",
            "blendFile": str(blend_path),
            "outputFolder": str(output_folder),
            "startFrame": 1,
            "endFrame": 1,
            "blockSize": 1,
            "requiredBlenderVersion": "4.5",
            "preferredBlenderMajor": 4,
            "versionRoutingPolicy": "prefer_same_major_then_higher",
            "blendVersionDetected": "4.5",
            "submittedByMasterId": "master_a",
            "submittedByMasterName": "MASTER-A",
            "submittedByUser": "vinicius",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["needsVersionChoice"] is True
    assert payload["sameMajorOlderVersions"] == ["4.3"]
    assert payload["higherMajorVersions"] == ["5.1"]


def test_blocks_legacy_worker_when_reported_blender_version_is_too_old(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_a",
        name="WORKER-A",
        reported_blender_version="5.1",
        validate_paths_response={
            "valid": True,
            "blendAccessible": True,
            "outputAccessible": True,
        },
    )
    _register_worker(client)
    job = _create_job(
        client,
        tmp_path / "job10_output",
        name="Cena10",
        start_frame=1,
        end_frame=1,
        requiredBlenderVersion="5.2",
        preferredBlenderMajor=5,
        versionRoutingPolicy="prefer_same_major_then_higher",
    )

    app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    assert payload["status"] == "blocked"
    assert "WORKER-A" in payload["lastError"]
    assert "5.2" in payload["lastError"]


def test_accepts_legacy_worker_when_reported_blender_version_is_compatible(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    fake_worker_client.add_worker(
        "http://127.0.0.1:9000",
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=True,
        reported_blender_version="5.2",
        validate_paths_response={
            "valid": True,
            "blendAccessible": True,
            "outputAccessible": True,
        },
    )
    _register_worker(client)
    job = _create_job(
        client,
        tmp_path / "job11_output",
        name="Cena11",
        start_frame=1,
        end_frame=1,
        requiredBlenderVersion="5.2",
        preferredBlenderMajor=5,
        versionRoutingPolicy="prefer_same_major_then_higher",
    )

    for _ in range(4):
        app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    assert payload["status"] == "completed"


def test_worker_with_active_lease_is_not_reused_when_status_reports_idle(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(
        base_url,
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=False,
    )
    _register_worker(client)
    job = _create_job(client, tmp_path / "job12_output", name="Cena12", start_frame=1, end_frame=2)

    app.state.service.run_scheduler_cycle()

    worker_state = fake_worker_client.workers[base_url]
    worker_state["status"] = "idle"
    worker_state["currentJobId"] = None
    worker_state["currentBlockId"] = None
    worker_state["currentFrame"] = None
    worker_state["currentAssignmentId"] = None
    worker_state["progress"] = 100.0

    app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    assert len(fake_worker_client.render_requests) == 1
    assert payload["frames"][0]["status"] in {"leased", "rendering"}
    assert payload["frames"][1]["status"] == "pending"


def test_transient_status_failure_within_grace_does_not_redispatch(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    worker_a_url = "http://127.0.0.1:9000"
    worker_b_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(worker_a_url, worker_id="worker_a", name="A-WORKER", auto_complete=False)
    fake_worker_client.add_worker(worker_b_url, worker_id="worker_b", name="B-WORKER", auto_complete=False)
    _register_worker(client, port=9000, name="A-WORKER")
    _register_worker(client, port=9001, name="B-WORKER")
    job = _create_job(client, tmp_path / "job13_output", name="Cena13", start_frame=1, end_frame=1)

    app.state.service.run_scheduler_cycle()
    fake_worker_client.workers[worker_a_url]["statusFailuresRemaining"] = 1

    app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    assert len(fake_worker_client.render_requests) == 1
    assert fake_worker_client.render_requests[0]["baseUrl"] == worker_a_url
    assert payload["frames"][0]["attemptCount"] == 1
    assert payload["frames"][0]["status"] in {"leased", "rendering"}


@pytest.mark.parametrize("worker_status", ["queued", "draining"])
def test_active_persistent_session_states_keep_frame_lease_alive(coordinator_app, worker_status):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(
        base_url,
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=False,
        persistent_session_enabled=True,
    )
    worker = _register_worker(client)
    job = _create_job(client, tmp_path / f"job_active_{worker_status}_lease", name="CenaLease", start_frame=1, end_frame=1)

    app.state.service.run_scheduler_cycle()

    request = fake_worker_client.render_requests[0]
    worker_state = fake_worker_client.workers[base_url]
    worker_state["status"] = worker_status
    worker_state["sessionState"] = worker_status
    worker_state["sessionQueueDepth"] = 0
    worker_state["sessionDrainRequested"] = worker_status == "draining"
    worker_state["queuedAssignments"] = []

    app.state.database.execute(
        """
        UPDATE job_frames
        SET leaseExpiresAt = ?
        WHERE jobId = ? AND frameNumber = ?
        """,
        ("2000-01-01T00:00:00+00:00", job["jobId"], 1),
    )

    app.state.service.run_scheduler_cycle()

    frame = app.state.database.fetch_one(
        """
        SELECT status, workerId, assignmentId
        FROM job_frames
        WHERE jobId = ? AND frameNumber = ?
        """,
        (job["jobId"], 1),
    )

    assert frame is not None
    assert frame["status"] in {"leased", "rendering"}
    assert frame["workerId"] == worker["workerId"]
    assert frame["assignmentId"] == request["assignmentId"]
    assert len(fake_worker_client.render_requests) == 1


def test_worker_reported_success_completes_frame_without_filesystem_confirmation(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(
        base_url,
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=False,
    )
    worker = _register_worker(client)
    job = _create_job(client, tmp_path / "job14_output", name="Cena14", start_frame=1, end_frame=1)

    app.state.service.run_scheduler_cycle()
    request = fake_worker_client.render_requests[0]
    worker_state = fake_worker_client.workers[base_url]
    worker_state["status"] = "idle"
    worker_state["currentJobId"] = None
    worker_state["currentBlockId"] = None
    worker_state["currentFrame"] = None
    worker_state["currentAssignmentId"] = None
    worker_state["progress"] = 100.0
    worker_state["lastJob"] = {
        "assignmentId": request["assignmentId"],
        "jobId": request["jobId"],
        "blockId": request["blockId"],
        "startFrame": request["startFrame"],
        "endFrame": request["endFrame"],
        "result": "done",
        "outputVerified": True,
        "outputPaths": [str(tmp_path / "worker_only_visible_frame.png")],
    }

    app.state.service.run_scheduler_cycle()
    payload = client.get(f"/jobs/{job['jobId']}").json()

    assert payload["status"] == "completed"
    assert len(fake_worker_client.render_requests) == 1

    report_response = client.post(
        f"/workers/{worker['workerId']}/render-reports",
        json={
            "workerId": worker["workerId"],
            "machineName": "WORKER-A",
            "status": "idle",
            "progress": 100.0,
            "lastLog": "Render finished successfully with exitCode=0",
            "reportedAt": "2026-06-19T17:41:00+00:00",
            "persistentSessionEnabled": False,
            "sessionQueueDepth": 0,
            "sessionDrainRequested": False,
            "queuedAssignments": [],
            "lastJob": {
                "assignmentId": request["assignmentId"],
                "jobId": request["jobId"],
                "blockId": request["blockId"],
                "startFrame": request["startFrame"],
                "endFrame": request["endFrame"],
                "result": "done",
                "executionMode": "direct",
                "outputVerified": True,
                "outputPaths": [str(tmp_path / "worker_only_visible_frame.png")],
            },
        },
    )

    assert report_response.status_code == 200, report_response.text
    assert report_response.json()["action"] in {"frame_completed", "worker_updated"}


def test_worker_render_report_endpoint_requeues_failed_frame_without_poisoning_worker(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    worker_a_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(worker_a_url, worker_id="worker_a", name="A-WORKER", auto_complete=False)
    worker = _register_worker(client, port=9000, name="A-WORKER")
    job = _create_job(client, tmp_path / "job15a_output", name="Cena15A", start_frame=1, end_frame=1)

    app.state.service.run_scheduler_cycle()
    first_request = fake_worker_client.render_requests[0]
    worker_state = fake_worker_client.workers[worker_a_url]
    worker_state["status"] = "idle"
    worker_state["currentJobId"] = None
    worker_state["currentBlockId"] = None
    worker_state["currentFrame"] = None
    worker_state["currentAssignmentId"] = None
    worker_state["progress"] = 0.0
    worker_state["error"] = "simulated render failure"
    worker_state["lastJob"] = {
        "assignmentId": first_request["assignmentId"],
        "jobId": first_request["jobId"],
        "blockId": first_request["blockId"],
        "startFrame": first_request["startFrame"],
        "endFrame": first_request["endFrame"],
        "result": "error",
        "error": "simulated render failure",
        "message": "Persistent session error: simulated render failure",
        "executionMode": "direct_fallback",
        "recoveryActions": [
            "Persistent Blender session failed, so the worker switched to one-shot render automatically."
        ],
        "outputVerified": False,
        "outputPaths": [],
    }

    report_response = client.post(
        f"/workers/{worker['workerId']}/render-reports",
        json={
            "workerId": worker["workerId"],
            "machineName": "A-WORKER",
            "status": "idle",
            "progress": 0.0,
            "error": "simulated render failure",
            "lastLog": "Persistent session error: simulated render failure",
            "reportedAt": "2026-06-19T17:42:00+00:00",
            "persistentSessionEnabled": False,
            "sessionQueueDepth": 0,
            "sessionDrainRequested": False,
            "queuedAssignments": [],
            "lastJob": worker_state["lastJob"],
        },
    )

    assert report_response.status_code == 200, report_response.text
    assert report_response.json()["action"] == "frame_requeued_error"

    frame = app.state.database.fetch_one(
        """
        SELECT status, assignmentId, workerId, errorMessage
        FROM job_frames
        WHERE jobId = ? AND frameNumber = ?
        """,
        (job["jobId"], 1),
    )
    assert frame is not None
    assert frame["status"] == "pending"
    assert frame["assignmentId"] is None
    assert frame["workerId"] is None
    assert frame["errorMessage"] == "simulated render failure"

    updated_worker = app.state.worker_registry.get_worker(worker["workerId"])
    assert updated_worker is not None
    assert updated_worker["status"] == "idle"
    assert updated_worker["error"] == "simulated render failure"

    app.state.service.run_scheduler_cycle()
    assert len(fake_worker_client.render_requests) == 2
    assert fake_worker_client.render_requests[1]["baseUrl"] == worker_a_url


def test_busy_worker_rejection_is_retried_on_other_worker(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    worker_a_url = "http://127.0.0.1:9000"
    worker_b_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(worker_a_url, worker_id="worker_a", name="A-WORKER", auto_complete=False)
    fake_worker_client.add_worker(worker_b_url, worker_id="worker_b", name="B-WORKER", auto_complete=True)
    fake_worker_client.workers[worker_a_url]["rejectBusyOnce"] = True
    _register_worker(client, port=9000, name="A-WORKER")
    _register_worker(client, port=9001, name="B-WORKER")
    job = _create_job(client, tmp_path / "job15_output", name="Cena15", start_frame=1, end_frame=1)

    for _ in range(5):
        app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    assert payload["status"] == "completed"
    assert len(fake_worker_client.render_requests) == 1
    assert fake_worker_client.render_requests[0]["baseUrl"] == worker_b_url


def test_worker_is_retried_after_single_render_failure_when_alternative_exists(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    worker_a_url = "http://127.0.0.1:9000"
    worker_b_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(worker_a_url, worker_id="worker_a", name="A-WORKER", auto_complete=False)
    fake_worker_client.add_worker(worker_b_url, worker_id="worker_b", name="B-WORKER", auto_complete=True)
    _register_worker(client, port=9000, name="A-WORKER")
    _register_worker(client, port=9001, name="B-WORKER")
    job = _create_job(client, tmp_path / "job16_output", name="Cena16", start_frame=1, end_frame=1)

    app.state.service.run_scheduler_cycle()
    first_request = fake_worker_client.render_requests[0]
    worker_a_state = fake_worker_client.workers[worker_a_url]
    worker_a_state["status"] = "idle"
    worker_a_state["currentJobId"] = None
    worker_a_state["currentBlockId"] = None
    worker_a_state["currentFrame"] = None
    worker_a_state["currentAssignmentId"] = None
    worker_a_state["progress"] = 0.0
    worker_a_state["error"] = "simulated render failure"
    worker_a_state["lastJob"] = {
        "assignmentId": first_request["assignmentId"],
        "jobId": first_request["jobId"],
        "blockId": first_request["blockId"],
        "startFrame": first_request["startFrame"],
        "endFrame": first_request["endFrame"],
        "result": "error",
        "error": "simulated render failure",
        "outputVerified": False,
        "outputPaths": [],
    }

    for _ in range(4):
        app.state.service.run_scheduler_cycle()

    assert len(fake_worker_client.render_requests) == 2
    assert fake_worker_client.render_requests[0]["baseUrl"] == worker_a_url
    assert fake_worker_client.render_requests[1]["baseUrl"] == worker_b_url
    fake_worker_client.complete_current_frame(worker_b_url, fake_worker_client.render_requests[1])
    app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    assert payload["status"] == "completed"
    assert len(fake_worker_client.render_requests) == 2


def test_worker_is_quarantined_after_three_consecutive_failures_for_same_job(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    worker_a_url = "http://127.0.0.1:9000"
    worker_b_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(worker_a_url, worker_id="worker_a", name="A-WORKER", auto_complete=False)
    fake_worker_client.add_worker(worker_b_url, worker_id="worker_b", name="B-WORKER", auto_complete=True)
    worker_a = _register_worker(client, port=9000, name="A-WORKER")
    _register_worker(client, port=9001, name="B-WORKER")
    job = _create_job(client, tmp_path / "job16b_output", name="Cena16B", start_frame=1, end_frame=1)

    app.state.database.execute(
        """
        INSERT INTO job_worker_stats(
            jobId, workerId, renderDevicePreference,
            successCount, failureCount, consecutiveFailureCount,
            lastFailureReason, lastFailureAt, lastSuccessAt
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["jobId"],
            worker_a["workerId"],
            "GPU",
            0,
            3,
            3,
            "simulated repeated render failure",
            "2026-05-27T12:30:00+00:00",
            None,
        ),
    )

    for _ in range(4):
        app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    assert payload["status"] == "completed"
    assert len(fake_worker_client.render_requests) == 1
    assert fake_worker_client.render_requests[0]["baseUrl"] == worker_b_url


def test_worker_with_bad_failure_history_is_quarantined_for_same_job(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    worker_a_url = "http://127.0.0.1:9000"
    worker_b_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(worker_a_url, worker_id="worker_a", name="A-WORKER", auto_complete=False)
    fake_worker_client.add_worker(worker_b_url, worker_id="worker_b", name="B-WORKER", auto_complete=True)
    worker_a = _register_worker(client, port=9000, name="A-WORKER")
    _register_worker(client, port=9001, name="B-WORKER")
    job = _create_job(client, tmp_path / "job16c_output", name="Cena16C", start_frame=1, end_frame=1)

    app.state.database.execute(
        """
        INSERT INTO job_worker_stats(
            jobId, workerId, renderDevicePreference,
            successCount, failureCount, consecutiveFailureCount,
            lastFailureReason, lastFailureAt, lastSuccessAt
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["jobId"],
            worker_a["workerId"],
            "GPU",
            4,
            5,
            2,
            "simulated unstable history",
            "2026-05-27T12:45:00+00:00",
            "2026-05-27T12:40:00+00:00",
        ),
    )

    for _ in range(4):
        app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    assert payload["status"] == "completed"
    assert len(fake_worker_client.render_requests) == 1
    assert fake_worker_client.render_requests[0]["baseUrl"] == worker_b_url

def test_frame_error_does_not_cancel_other_frames_still_running(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    app.state.service.settings.max_frame_attempts = 1
    worker_a_url = "http://127.0.0.1:9000"
    worker_b_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(worker_a_url, worker_id="worker_a", name="A-WORKER", auto_complete=False)
    fake_worker_client.add_worker(worker_b_url, worker_id="worker_b", name="B-WORKER", auto_complete=True)
    _register_worker(client, port=9000, name="A-WORKER")
    _register_worker(client, port=9001, name="B-WORKER")
    job = _create_job(client, tmp_path / "job17_output", name="Cena17", start_frame=1, end_frame=2)

    app.state.service.run_scheduler_cycle()
    first_request = fake_worker_client.render_requests[0]
    worker_a_state = fake_worker_client.workers[worker_a_url]
    worker_a_state["status"] = "idle"
    worker_a_state["currentJobId"] = None
    worker_a_state["currentBlockId"] = None
    worker_a_state["currentFrame"] = None
    worker_a_state["currentAssignmentId"] = None
    worker_a_state["progress"] = 0.0
    worker_a_state["lastJob"] = {
        "assignmentId": first_request["assignmentId"],
        "jobId": first_request["jobId"],
        "blockId": first_request["blockId"],
        "startFrame": first_request["startFrame"],
        "endFrame": first_request["endFrame"],
        "result": "error",
        "error": "simulated render failure",
        "outputVerified": False,
        "outputPaths": [],
    }

    for _ in range(4):
        app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    frame_1 = next(frame for frame in payload["frames"] if frame["frameNumber"] == 1)
    frame_2 = next(frame for frame in payload["frames"] if frame["frameNumber"] == 2)

    assert frame_1["status"] == "error"
    assert frame_2["status"] == "done"
    assert payload["status"] == "error"


def test_persistent_session_affinity_and_job_drain_notification(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(
        base_url,
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=False,
        persistent_session_enabled=True,
    )
    _register_worker(client)
    job = _create_job(client, tmp_path / "job_persistent_affinity", name="CenaPersistente", start_frame=1, end_frame=2)

    app.state.service.run_scheduler_cycle()
    assert len(fake_worker_client.render_requests) == 1
    first_request = dict(fake_worker_client.render_requests[0])

    app.state.service.run_scheduler_cycle()
    assert len(fake_worker_client.render_requests) == 2
    second_request = dict(fake_worker_client.render_requests[1])
    assert first_request["jobId"] == second_request["jobId"] == job["jobId"]
    assert first_request["baseUrl"] == second_request["baseUrl"] == base_url
    assert fake_worker_client.workers[base_url]["status"] == "queued"
    assert fake_worker_client.workers[base_url]["sessionQueueDepth"] == 1

    fake_worker_client.complete_current_frame(base_url, first_request)
    app.state.service.run_scheduler_cycle()
    assert fake_worker_client.workers[base_url]["currentFrame"] == 2

    active_payload = dict(fake_worker_client.workers[base_url]["activePayload"])
    fake_worker_client.complete_current_frame(base_url, active_payload)
    app.state.service.run_scheduler_cycle()

    payload = client.get(f"/jobs/{job['jobId']}").json()
    assert payload["status"] == "completed"
    assert fake_worker_client.job_drain_requests[-1]["jobId"] == job["jobId"]
    assert fake_worker_client.workers[base_url]["status"] == "idle"
    assert len(fake_worker_client.render_requests) == 2


def test_unproven_persistent_session_worker_only_queues_one_extra_frame_until_first_success(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    base_url = "http://127.0.0.1:9000"
    fake_worker_client.add_worker(
        base_url,
        worker_id="worker_a",
        name="WORKER-A",
        auto_complete=False,
        persistent_session_enabled=True,
    )
    _register_worker(client)
    job = _create_job(client, tmp_path / "job_persistent_unproven_limit", name="CenaPersistenteLimitada", start_frame=1, end_frame=3)

    app.state.service.run_scheduler_cycle()
    assert len(fake_worker_client.render_requests) == 1
    first_request = dict(fake_worker_client.render_requests[0])

    app.state.service.run_scheduler_cycle()
    assert len(fake_worker_client.render_requests) == 2
    assert fake_worker_client.workers[base_url]["sessionQueueDepth"] == 1

    app.state.service.run_scheduler_cycle()
    assert len(fake_worker_client.render_requests) == 2
    payload = client.get(f"/jobs/{job['jobId']}").json()
    pending_frames = {
        frame["frameNumber"]
        for frame in payload["frames"]
        if frame["status"] == "pending"
    }
    assert pending_frames == {3}

    fake_worker_client.complete_current_frame(base_url, first_request)
    app.state.service.run_scheduler_cycle()

    assert len(fake_worker_client.render_requests) == 3
    assert fake_worker_client.render_requests[-1]["jobId"] == job["jobId"]
    assert fake_worker_client.render_requests[-1]["blockId"] == "frame_000003"
    assert fake_worker_client.workers[base_url]["sessionQueueDepth"] == 1


def test_scheduler_does_not_retry_queue_full_persistent_workers_within_same_cycle(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    worker_urls = [
        "http://127.0.0.1:9000",
        "http://127.0.0.1:9001",
        "http://127.0.0.1:9002",
        "http://127.0.0.1:9003",
    ]
    for index, base_url in enumerate(worker_urls):
        fake_worker_client.add_worker(
            base_url,
            worker_id=f"worker_{index}",
            name=f"WORKER-{index}",
            auto_complete=False,
            persistent_session_enabled=True,
            persistent_session_max_queue_depth=1,
        )
        _register_worker(client, port=9000 + index, name=f"WORKER-{index}")

    job = _create_job(
        client,
        tmp_path / "job_persistent_queue_limit",
        name="CenaFilaLimitada",
        start_frame=1,
        end_frame=8,
    )

    app.state.service.run_scheduler_cycle()
    assert len(fake_worker_client.render_requests) == 4

    app.state.service.run_scheduler_cycle()

    assert len(fake_worker_client.render_requests) == 8
    workers_payload = client.get("/workers").json()
    assert all(worker.get("error") in {None, ""} for worker in workers_payload)
    assert sum(len(fake_worker_client.workers[base_url]["queuedAssignments"]) for base_url in worker_urls) == 4
    assert all(fake_worker_client.workers[base_url]["sessionQueueDepth"] == 1 for base_url in worker_urls)

    job_payload = client.get(f"/jobs/{job['jobId']}").json()
    leased_or_rendering = {
        frame["frameNumber"]
        for frame in job_payload["frames"]
        if frame["status"] in {"leased", "rendering"}
    }
    assert leased_or_rendering == {1, 2, 3, 4, 5, 6, 7, 8}


def test_idle_worker_can_start_next_job_while_previous_job_is_still_running(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    worker_a_url = "http://127.0.0.1:9000"
    worker_b_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(worker_a_url, worker_id="worker_a", name="A-WORKER", auto_complete=False)
    fake_worker_client.add_worker(worker_b_url, worker_id="worker_b", name="B-WORKER", auto_complete=False)
    _register_worker(client, port=9000, name="A-WORKER")
    _register_worker(client, port=9001, name="B-WORKER")

    job1 = _create_job(client, tmp_path / "job18_output", name="Cena18A", start_frame=1, end_frame=1)
    job2 = _create_job(client, tmp_path / "job19_output", name="Cena18B", start_frame=2, end_frame=2)

    app.state.service.run_scheduler_cycle()

    payload = client.get("/jobs").json()
    first_job = next(job for job in payload if job["jobId"] == job1["jobId"])
    second_job = next(job for job in payload if job["jobId"] == job2["jobId"])

    assert first_job["status"] == "running"
    assert second_job["status"] == "running"
    assert len(fake_worker_client.render_requests) == 2
    assert {request["baseUrl"] for request in fake_worker_client.render_requests} == {worker_a_url, worker_b_url}


def test_worker_quarantined_for_one_job_can_help_another_job(coordinator_app):
    app, client, fake_worker_client, tmp_path = coordinator_app
    worker_a_url = "http://127.0.0.1:9000"
    worker_b_url = "http://127.0.0.1:9001"
    fake_worker_client.add_worker(worker_a_url, worker_id="worker_a", name="A-WORKER", auto_complete=False)
    fake_worker_client.add_worker(worker_b_url, worker_id="worker_b", name="B-WORKER", auto_complete=False)
    worker_a = _register_worker(client, port=9000, name="A-WORKER")
    _register_worker(client, port=9001, name="B-WORKER")

    job1 = _create_job(client, tmp_path / "job20_output", name="Cena20A", start_frame=1, end_frame=1)
    job2 = _create_job(client, tmp_path / "job21_output", name="Cena20B", start_frame=2, end_frame=2)

    app.state.database.execute(
        """
        INSERT INTO job_worker_stats(
            jobId, workerId, renderDevicePreference,
            successCount, failureCount, consecutiveFailureCount,
            lastFailureReason, lastFailureAt, lastSuccessAt
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job1["jobId"],
            worker_a["workerId"],
            "GPU",
            0,
            3,
            3,
            "simulated repeated render failure",
            "2026-05-27T12:30:00+00:00",
            None,
        ),
    )

    app.state.service.run_scheduler_cycle()

    job1_request = next(request for request in fake_worker_client.render_requests if request["jobId"] == job1["jobId"])
    job2_request = next(request for request in fake_worker_client.render_requests if request["jobId"] == job2["jobId"])

    assert job1_request["baseUrl"] == worker_b_url
    assert job2_request["baseUrl"] == worker_a_url


def test_job_report_pdf_route_returns_pdf(coordinator_app, tmp_path):
    app, client, fake_worker_client, _ = coordinator_app
    fake_worker_client.add_worker("http://127.0.0.1:9000", worker_id="worker_a", name="WORKER-A", auto_complete=True)
    _register_worker(client)
    job = _create_job(client, tmp_path / "job_report_pdf", name="CenaPDF", start_frame=1, end_frame=1)

    app.state.service.run_scheduler_cycle()
    response = client.get(f"/jobs/{job['jobId']}/report.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-1.4")
