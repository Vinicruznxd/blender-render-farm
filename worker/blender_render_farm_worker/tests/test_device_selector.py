from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from app.device_selector import (
    build_render_device_response,
    display_status,
    format_relative_time,
    humanize_status,
)


class DeviceSelectorTests(unittest.TestCase):
    def test_humanize_status_maps_known_values(self) -> None:
        self.assertEqual(humanize_status("idle"), "Ready")
        self.assertEqual(humanize_status("rendering"), "Rendering")
        self.assertEqual(humanize_status("custom_state"), "Custom State")

    def test_display_status_prioritizes_api_failure(self) -> None:
        self.assertEqual(
            display_status({"status": "idle", "apiServerState": "error", "error": None}),
            "API Offline",
        )
        self.assertEqual(
            display_status({"status": "idle", "apiServerState": "running", "error": "boom"}),
            "Attention",
        )

    def test_format_relative_time_handles_recent_timestamp(self) -> None:
        just_now = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        self.assertEqual(format_relative_time(just_now), "Just now")

    def test_relative_time_uses_selected_language(self) -> None:
        timestamp = (datetime.now(timezone.utc) - timedelta(seconds=125)).isoformat()
        self.assertEqual(format_relative_time(timestamp, "en"), "2m ago")
        self.assertEqual(format_relative_time(timestamp, "pt"), "há 2min")
        self.assertEqual(format_relative_time(timestamp, "es"), "hace 2min")

    def test_registration_address_respects_explicit_bind_host(self) -> None:
        from app.config import WorkerConfig
        from app.status_manager import WorkerStatusManager
        manager = WorkerStatusManager(WorkerConfig(host="127.0.0.1", port=19000))
        self.assertEqual(manager.get_worker_registration_payload()["ip"], "127.0.0.1")

    def test_build_render_device_response_normalizes_preference(self) -> None:
        payload = build_render_device_response("gpu")
        self.assertEqual(payload["renderDevicePreference"], "GPU")
        self.assertTrue(payload["effectiveUseGpu"])
        self.assertTrue(payload["saved"])


if __name__ == "__main__":
    unittest.main()
