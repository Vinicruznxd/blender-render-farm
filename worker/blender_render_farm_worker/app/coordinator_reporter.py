from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4


class CoordinatorReporter:
    """Durable, asynchronous delivery of render completion reports."""

    def __init__(
        self,
        *,
        status_manager,
        logger,
        timeout_seconds: float = 5.0,
        project_root: Path | None = None,
    ) -> None:
        self.status_manager = status_manager
        self.logger = logger
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.outbox_path = (
            Path(project_root) / "state" / "coordinator_report_outbox.json"
            if project_root is not None
            else None
        )
        self._condition = threading.Condition(threading.RLock())
        self._queue: list[dict[str, Any]] = self._load_outbox()
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run,
            name="coordinator-report-outbox",
            daemon=True,
        )
        self._thread.start()

    def report_render_completion(self, payload: dict[str, object]) -> None:
        last_job = payload.get("lastJob")
        if not isinstance(last_job, dict) or not last_job:
            self.status_manager.update_last_job_delivery(
                "skipped",
                detail="Worker finished a render but no structured lastJob payload was available.",
            )
            return
        entry = {
            "id": uuid4().hex,
            "payload": dict(payload),
            "attempts": 0,
            "nextAttemptAt": 0.0,
        }
        with self._condition:
            self._queue.append(entry)
            self._save_outbox_locked()
            self.status_manager.update_last_job_delivery(
                "queued",
                detail="Render completion report stored in the durable delivery queue.",
            )
            self._condition.notify_all()

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while self._queue and time.monotonic() < deadline:
                self._condition.notify_all()
                self._condition.wait(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            return not self._queue

    def stop(self, timeout_seconds: float = 2.0) -> None:
        self.flush(timeout_seconds)
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.1, timeout_seconds))

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                now = time.time()
                entry = next(
                    (item for item in self._queue if float(item.get("nextAttemptAt") or 0) <= now),
                    None,
                )
                if entry is None:
                    wait_for = 1.0
                    if self._queue:
                        wait_for = max(
                            0.1,
                            min(float(item.get("nextAttemptAt") or now) for item in self._queue) - now,
                        )
                    self._condition.wait(timeout=min(wait_for, 5.0))
                    continue

            delivered, permanent_failure = self._deliver(dict(entry.get("payload") or {}))
            with self._condition:
                if entry not in self._queue:
                    continue
                if delivered or permanent_failure:
                    self._queue.remove(entry)
                else:
                    entry["attempts"] = int(entry.get("attempts") or 0) + 1
                    entry["nextAttemptAt"] = time.time() + min(60.0, 2.0 ** min(entry["attempts"], 6))
                self._save_outbox_locked()
                self._condition.notify_all()

    def _deliver(self, payload: dict[str, object]) -> tuple[bool, bool]:
        last_job = payload.get("lastJob")
        coordinator_api_url = str(
            payload.get("coordinatorApiUrl") or self.status_manager.coordinator_api_url or ""
        ).strip()
        worker_id = str(payload.get("workerId") or self.status_manager.worker_id or "").strip()
        if not coordinator_api_url or not worker_id:
            self.status_manager.update_last_job_delivery(
                "queued", detail="Render report is waiting for a coordinator URL and stable workerId."
            )
            return False, False

        endpoint = (
            f"{coordinator_api_url.rstrip('/')}/workers/"
            f"{urllib.parse.quote(worker_id, safe='')}/render-reports"
        )
        request = urllib.request.Request(
            url=endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                parsed_body = self._parse_optional_json(response.read().decode("utf-8", errors="replace"))
                self.status_manager.update_last_job_delivery(
                    "delivered",
                    detail=f"Coordinator accepted render report at {endpoint}",
                    response=parsed_body,
                )
                self.logger.info(
                    "Reported render completion to coordinator worker=%s job=%s block=%s status=%s",
                    worker_id,
                    last_job.get("jobId") if isinstance(last_job, dict) else None,
                    last_job.get("blockId") if isinstance(last_job, dict) else None,
                    response.status,
                )
                return True, False
        except urllib.error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            parsed_body = self._parse_optional_json(raw_body)
            permanent = 400 <= exc.code < 500 and exc.code not in {408, 429}
            self.status_manager.update_last_job_delivery(
                "failed" if permanent else "queued",
                detail=f"Coordinator returned HTTP {exc.code}: {raw_body.strip()}",
                response=parsed_body,
            )
            return False, permanent
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            self.status_manager.update_last_job_delivery(
                "queued",
                detail=f"Coordinator unavailable; report retained for retry: {exc}",
            )
            self.logger.warning("Render report retained for retry worker=%s error=%s", worker_id, exc)
            return False, False

    def _load_outbox(self) -> list[dict[str, Any]]:
        if self.outbox_path is None or not self.outbox_path.is_file():
            return []
        try:
            payload = json.loads(self.outbox_path.read_text(encoding="utf-8"))
            return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
        except (OSError, ValueError):
            return []

    def _save_outbox_locked(self) -> None:
        if self.outbox_path is None:
            return
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.outbox_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._queue, indent=2), encoding="utf-8")
        temporary.replace(self.outbox_path)

    @staticmethod
    def _parse_optional_json(raw_body: str) -> dict[str, Any] | None:
        text = str(raw_body or "").strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
