from __future__ import annotations

import json
import logging
import unittest
import urllib.parse
from unittest.mock import patch

from app.config import WorkerConfig
from app.coordinator_reporter import CoordinatorReporter
from app.status_manager import WorkerStatusManager


class _DummyHttpResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_DummyHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class CoordinatorReporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = WorkerConfig(
            port=9100,
            coordinatorDiscoveryEnabled=False,
            persistentSessionEnabled=True,
        )
        self.status_manager = WorkerStatusManager(self.config)
        self.logger = logging.getLogger(f"tests.coordinator_reporter.{id(self)}")

    def test_finish_render_callback_contains_structured_diagnostics(self) -> None:
        reports: list[dict[str, object]] = []
        self.status_manager.set_render_completion_callback(reports.append)
        self.status_manager.set_coordinator_registered(
            coordinator_id="coord_test",
            api_url="http://127.0.0.1:8010",
            hostname="coord-host",
            last_seen_at="2026-06-19T17:30:00+00:00",
            last_registered_at="2026-06-19T17:30:05+00:00",
        )
        self.status_manager.set_persistent_session_availability(
            False,
            reason="Persistent socket timeout",
            retry_at="2026-06-19T17:35:00+00:00",
        )
        self.status_manager.begin_render_block(self._render_payload())

        self.status_manager.finish_render_error(
            exit_code=-1,
            elapsed_seconds=12,
            error="Persistent socket timeout",
            message="Persistent session error: Persistent socket timeout",
            failure_category="persistent_session",
            failure_code="persistent_session_exception",
            failure_details="Persistent socket timeout",
        )

        self.assertEqual(len(reports), 1)
        report = reports[0]
        last_job = report["lastJob"]

        assert isinstance(last_job, dict)
        self.assertEqual(report["workerId"], self.status_manager.worker_id)
        self.assertEqual(last_job["result"], "error")
        self.assertEqual(last_job["executionMode"], "direct_fallback")
        self.assertEqual(last_job["failureCategory"], "persistent_session")
        self.assertEqual(last_job["failureCode"], "persistent_session_exception")
        self.assertEqual(last_job["failureDetails"], "Persistent socket timeout")
        self.assertTrue(last_job["persistentSessionTemporarilyDisabled"])
        self.assertEqual(
            last_job["persistentSessionDisabledReason"],
            "Persistent socket timeout",
        )
        self.assertEqual(
            last_job["persistentSessionRetryAt"],
            "2026-06-19T17:35:00+00:00",
        )
        self.assertEqual(
            last_job["recoveryActions"],
            [
                "Persistent Blender session failed, so the worker switched to one-shot render automatically."
            ],
        )

    def test_reporter_posts_render_report_and_marks_delivery(self) -> None:
        reporter = CoordinatorReporter(
            status_manager=self.status_manager,
            logger=self.logger,
        )
        self.status_manager.set_render_completion_callback(reporter.report_render_completion)
        self.status_manager.set_coordinator_registered(
            coordinator_id="coord_test",
            api_url="http://127.0.0.1:8010",
            hostname="coord-host",
            last_seen_at="2026-06-19T17:30:00+00:00",
            last_registered_at="2026-06-19T17:30:05+00:00",
        )
        self.status_manager.begin_render_block(self._render_payload())

        with patch(
            "app.coordinator_reporter.urllib.request.urlopen",
            return_value=_DummyHttpResponse('{"accepted": true, "action": "frame_completed"}'),
        ) as mocked_urlopen:
            self.status_manager.finish_render_success(
                exit_code=0,
                elapsed_seconds=8,
                output_paths=[r"C:\renders\frame_0001.png"],
                output_verified=True,
                message="Render finished successfully with exitCode=0",
            )
            self.assertTrue(reporter.flush())

        self.assertEqual(mocked_urlopen.call_count, 1)
        request = mocked_urlopen.call_args.args[0]
        expected_path = (
            f"/workers/{urllib.parse.quote(self.status_manager.worker_id, safe='')}/render-reports"
        )
        self.assertTrue(request.full_url.endswith(expected_path))

        posted_payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(posted_payload["workerId"], self.status_manager.worker_id)
        self.assertEqual(posted_payload["lastJob"]["result"], "done")

        snapshot = self.status_manager.status_snapshot()
        coordinator_report = snapshot["lastJob"]["coordinatorReport"]
        self.assertEqual(coordinator_report["status"], "delivered")
        self.assertEqual(
            coordinator_report["response"]["action"],
            "frame_completed",
        )

    def _render_payload(self) -> dict[str, object]:
        return {
            "jobId": "job_report_01",
            "blockId": "frame_000001",
            "blendFile": r"C:\scene.blend",
            "outputPattern": r"C:\renders\frame_####",
            "startFrame": 1,
            "endFrame": 1,
            "format": "PNG",
            "useGpu": False,
            "requestedUseGpu": True,
            "renderDevicePreference": "CPU",
            "selectedBlenderVersion": "5.1.2",
            "selectedBlenderPath": r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
            "assignmentId": "lease_000001_1_mocked",
            "coordinatorId": "coord_test",
            "submittedByMasterId": "master_a",
            "submittedByMasterName": "MASTER-A",
            "executionMode": "direct_fallback",
            "recoveryActions": [
                "Persistent Blender session failed, so the worker switched to one-shot render automatically."
            ],
            "persistentSessionFallbackReason": "Persistent socket timeout",
        }
