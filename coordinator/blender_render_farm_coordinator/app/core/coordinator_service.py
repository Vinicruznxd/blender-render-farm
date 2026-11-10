from __future__ import annotations

from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import socket
from threading import Lock
from typing import Any, Callable

from app.core.coordinator_discovery import CoordinatorDiscoveryService
from app.core.database import DATABASE_SCHEMA_VERSION, Database
from app.core.frame_scanner import FrameScanner
from app.core.models import (
    ACTIVE_JOB_STATUSES,
    FREE_WORKER_STATUSES,
    ONLINE_WORKER_STATUSES,
    FrameStatus,
    JobStatus,
    NotFoundError,
    WorkerStatus,
    iso_after_seconds,
    new_assignment_id,
    normalize_bool,
    utc_now_iso,
)
from app.core.runtime_metadata import load_runtime_metadata
from app.core.settings import AppSettings
from app.core.worker_registry import WorkerRegistry
from app.services.logger import AppLogger
from app.services.pdf_report import build_text_pdf
from app.services.worker_client import WorkerClient, WorkerRequestError


class CoordinatorService:
    def __init__(
        self,
        settings: AppSettings,
        database: Database,
        logger: AppLogger,
        worker_registry: WorkerRegistry,
        frame_scanner: FrameScanner,
        worker_client: WorkerClient,
        coordinator_discovery: CoordinatorDiscoveryService | None = None,
    ):
        self.settings = settings
        self.database = database
        self.logger = logger
        self.worker_registry = worker_registry
        self.frame_scanner = frame_scanner
        self.worker_client = worker_client
        self.coordinator_discovery = coordinator_discovery
        self.started_at = utc_now_iso()
        self.runtime_metadata = load_runtime_metadata(self.settings.project_root)
        self._cycle_lock = Lock()
        self._scheduler_status_getter: Callable[[], dict[str, object]] | None = None

    def set_scheduler_status_getter(self, getter: Callable[[], dict[str, object]]) -> None:
        self._scheduler_status_getter = getter

    def health(self) -> dict[str, Any]:
        payload = {
            "status": "ok",
            "coordinatorId": self.settings.coordinator_id,
            "startedAt": self.started_at,
            "appVersion": self.runtime_metadata["appVersion"],
            "protocolVersion": self.runtime_metadata["protocolVersion"],
            "buildId": self.runtime_metadata["buildId"],
            "buildTimestampUtc": self.runtime_metadata["buildTimestampUtc"],
            "databaseSchemaVersion": DATABASE_SCHEMA_VERSION,
            "language": self.settings.language,
            "supportedLanguages": ["en", "pt", "es"],
        }
        if self.coordinator_discovery:
            payload["discovery"] = self.coordinator_discovery.snapshot()
        if self._scheduler_status_getter:
            scheduler = self._scheduler_status_getter()
            payload["scheduler"] = scheduler
            if (
                self.settings.auto_start_scheduler
                and not scheduler.get("threadAlive")
            ):
                payload["status"] = "degraded"
        return payload

    def record_master_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        self.database.execute(
            """
            INSERT INTO masters(masterId, name, host, username, version, protocolVersion, buildId, buildTimestampUtc, lastSeen)
            VALUES(:masterId, :name, :host, :username, :version, :protocolVersion, :buildId, :buildTimestampUtc, :lastSeen)
            ON CONFLICT(masterId) DO UPDATE SET
                name = excluded.name,
                host = excluded.host,
                username = excluded.username,
                version = excluded.version,
                protocolVersion = excluded.protocolVersion,
                buildId = excluded.buildId,
                buildTimestampUtc = excluded.buildTimestampUtc,
                lastSeen = excluded.lastSeen
            """,
            {
                "masterId": payload["masterId"],
                "name": payload["masterName"],
                "host": payload["host"],
                "username": payload["username"],
                "version": payload["version"],
                "protocolVersion": payload.get("protocolVersion"),
                "buildId": payload.get("buildId"),
                "buildTimestampUtc": payload.get("buildTimestampUtc"),
                "lastSeen": now,
            },
        )
        result = self.database.fetch_one(
            """
            SELECT masterId, name, host, username, version, protocolVersion, buildId, buildTimestampUtc, lastSeen
            FROM masters
            WHERE masterId = ?
            """,
            (payload["masterId"],),
        ) or {}
        return {**result, "language": self.settings.language}

    def language_state(self) -> dict[str, Any]:
        return {
            "language": self.settings.language,
            "supportedLanguages": ["en", "pt", "es"],
            "source": "coordinator",
        }

    def set_language(self, language: object) -> dict[str, Any]:
        normalized = self.settings.update_language(language)
        self.logger.info(
            f"Coordinator language changed to '{normalized}'.",
            category="general",
            details={"language": normalized},
        )
        return self.language_state()

    def list_masters(self) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT masterId, name, host, username, version, protocolVersion, buildId, buildTimestampUtc, lastSeen
            FROM masters
            ORDER BY lastSeen DESC, name COLLATE NOCASE
            """
        )

    def list_workers(self) -> list[dict[str, Any]]:
        return self.worker_registry.list_workers()

    def register_worker(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker = self.worker_registry.register_or_update_worker(payload)
        self.logger.info(
            f"Worker '{worker['workerId']}' registered or updated.",
            category="worker",
            entity_type="worker",
            entity_id=worker["workerId"],
            details={"ip": worker["ip"], "port": worker["port"]},
        )
        return worker

    def update_worker(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        worker = self.worker_registry.update_worker(worker_id, payload)
        if not worker:
            raise NotFoundError(f"Worker '{worker_id}' not found.")
        self.logger.info(
            f"Worker '{worker_id}' updated.",
            category="worker",
            entity_type="worker",
            entity_id=worker_id,
            details={"changes": sorted(payload.keys())},
        )
        return worker

    def delete_worker(self, worker_id: str) -> dict[str, Any]:
        worker = self.worker_registry.get_worker(worker_id)
        if not worker:
            raise NotFoundError(f"Worker '{worker_id}' not found.")
        self._recover_frames_from_worker(worker_id, force=True)
        self.worker_registry.delete_worker(worker_id)
        self.logger.warning(
            f"Worker '{worker_id}' removed from registry.",
            category="worker",
            entity_type="worker",
            entity_id=worker_id,
        )
        return {"deleted": True, "workerId": worker_id}

    def discover_workers(self, hosts: list[str] | None, ports: list[int] | None) -> dict[str, Any]:
        discovered = self.worker_registry.discover_workers(hosts=hosts, ports=ports)
        return {"count": len(discovered), "workers": discovered}

    def test_worker(self, worker_id: str) -> dict[str, Any]:
        try:
            return self.worker_registry.test_worker(worker_id)
        except KeyError as exc:
            raise NotFoundError(f"Worker '{worker_id}' not found.") from exc

    def record_worker_render_report(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._cycle_lock:
            worker = self.worker_registry.get_worker(worker_id)
            if not worker:
                raise NotFoundError(f"Worker '{worker_id}' not found.")

            normalized_payload = self._normalize_worker_render_report_payload(
                worker_id,
                worker,
                payload,
            )
            updated_worker = self.worker_registry.update_worker(worker_id, normalized_payload)
            if not updated_worker:
                raise NotFoundError(f"Worker '{worker_id}' not found.")

            last_job = normalized_payload.get("lastJob")
            action = "worker_updated"
            if isinstance(last_job, dict) and last_job:
                action = self._apply_worker_render_report(worker_id, last_job)

            self.logger.info(
                f"Worker '{worker_id}' reported a render result in real time.",
                category="worker",
                entity_type="worker",
                entity_id=worker_id,
                details={
                    "jobId": last_job.get("jobId") if isinstance(last_job, dict) else None,
                    "blockId": last_job.get("blockId") if isinstance(last_job, dict) else None,
                    "result": last_job.get("result") if isinstance(last_job, dict) else None,
                    "action": action,
                },
            )
            return {
                "accepted": True,
                "workerId": worker_id,
                "action": action,
                "jobId": last_job.get("jobId") if isinstance(last_job, dict) else None,
                "assignmentId": last_job.get("assignmentId") if isinstance(last_job, dict) else None,
                "result": last_job.get("result") if isinstance(last_job, dict) else None,
            }

    def _normalize_worker_render_report_payload(
        self,
        worker_id: str,
        worker: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        last_job = payload.get("lastJob")
        normalized_last_job = dict(last_job) if isinstance(last_job, dict) else None
        if normalized_last_job:
            for field in (
                "persistentSessionTemporarilyDisabled",
                "persistentSessionDisabledReason",
                "persistentSessionRetryAt",
                "reportedAt",
            ):
                if field not in normalized_last_job and payload.get(field) is not None:
                    normalized_last_job[field] = payload.get(field)

        error = payload.get("error")
        if error is None and isinstance(normalized_last_job, dict):
            last_result = str(normalized_last_job.get("result") or "").strip().lower()
            if last_result == "error":
                error = normalized_last_job.get("error") or normalized_last_job.get("message")
            elif last_result == "done":
                error = None

        return {
            "workerId": worker_id,
            "name": payload.get("machineName") or payload.get("name") or worker.get("name"),
            "ip": worker.get("ip"),
            "port": worker.get("port"),
            "status": payload.get("status") or worker.get("status"),
            "currentJobId": payload.get("currentJobId"),
            "currentBlockId": payload.get("currentBlockId"),
            "currentFrame": payload.get("currentFrame"),
            "currentAssignmentId": payload.get("currentAssignmentId"),
            "progress": payload.get("progress", worker.get("progress")),
            "persistentSessionEnabled": payload.get("persistentSessionEnabled"),
            "sessionState": payload.get("sessionState"),
            "sessionQueueDepth": payload.get("sessionQueueDepth"),
            "sessionDrainRequested": payload.get("sessionDrainRequested"),
            "queuedAssignments": payload.get("queuedAssignments"),
            "lastSeen": payload.get("reportedAt") or utc_now_iso(),
            "lastLog": payload.get("lastLog") or worker.get("lastLog"),
            "error": error,
            "lastJob": normalized_last_job,
        }

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._normalize_job_payload(dict(payload))
        self._validate_job_payload(payload)
        now = utc_now_iso()
        output_pattern = payload.get("outputPattern") or str(Path(payload["outputFolder"]) / "frame_####")
        total_frames = int(payload["endFrame"]) - int(payload["startFrame"]) + 1
        self._validate_no_active_output_collision(
            output_pattern,
            int(payload["startFrame"]),
            int(payload["endFrame"]),
        )

        with self.database.transaction() as connection:
            job_id = self._generate_job_id(connection)
            queue_position = self._next_queue_position(connection)
            connection.execute(
                """
                INSERT INTO jobs(
                    jobId, jobName, blendFile, outputFolder, outputPattern, startFrame, endFrame, blockSize,
                    format, requiredBlenderVersion, preferredBlenderMajor, versionRoutingPolicy, blendVersionDetected,
                    status, queuePosition, createdAt, startedAt, finishedAt, submittedByMasterId, submittedByMasterName,
                    submittedByUser, ignoreRenderedFrames, autoAddWorkers, redistributeOfflineBlocks, legacyUseGpu,
                    gpuOnlyWorkers,
                    lastError, totalFrames, completedFrames
                )
                VALUES(
                    :jobId, :jobName, :blendFile, :outputFolder, :outputPattern, :startFrame, :endFrame, :blockSize,
                    :format, :requiredBlenderVersion, :preferredBlenderMajor, :versionRoutingPolicy, :blendVersionDetected,
                    :status, :queuePosition, :createdAt, NULL, NULL, :submittedByMasterId, :submittedByMasterName,
                    :submittedByUser, :ignoreRenderedFrames, :autoAddWorkers, :redistributeOfflineBlocks,
                    :legacyUseGpu, :gpuOnlyWorkers, NULL, :totalFrames, 0
                )
                """,
                {
                    "jobId": job_id,
                    "jobName": payload["jobName"],
                    "blendFile": payload["blendFile"],
                    "outputFolder": payload["outputFolder"],
                    "outputPattern": output_pattern,
                    "startFrame": int(payload["startFrame"]),
                    "endFrame": int(payload["endFrame"]),
                    "blockSize": max(1, int(payload.get("blockSize", 1))),
                    "format": payload.get("format", "PNG").upper(),
                    "requiredBlenderVersion": payload.get("requiredBlenderVersion"),
                    "preferredBlenderMajor": payload.get("preferredBlenderMajor"),
                    "versionRoutingPolicy": payload.get("versionRoutingPolicy"),
                    "blendVersionDetected": payload.get("blendVersionDetected"),
                    "status": JobStatus.QUEUED.value,
                    "queuePosition": queue_position,
                    "createdAt": now,
                    "submittedByMasterId": payload["submittedByMasterId"],
                    "submittedByMasterName": payload["submittedByMasterName"],
                    "submittedByUser": payload["submittedByUser"],
                    "ignoreRenderedFrames": 1 if normalize_bool(payload.get("ignoreRenderedFrames", True), True) else 0,
                    "autoAddWorkers": 1 if normalize_bool(payload.get("autoAddWorkers", True), True) else 0,
                    "redistributeOfflineBlocks": 1 if normalize_bool(payload.get("redistributeOfflineBlocks", True), True) else 0,
                    "legacyUseGpu": 1 if normalize_bool(payload.get("legacyUseGpu", True), True) else 0,
                    "gpuOnlyWorkers": 1 if normalize_bool(payload.get("gpuOnlyWorkers", False), False) else 0,
                    "totalFrames": total_frames,
                },
            )

            frames = []
            for frame_number in range(int(payload["startFrame"]), int(payload["endFrame"]) + 1):
                scan_job = {
                    "outputPattern": output_pattern,
                    "format": payload.get("format", "PNG").upper(),
                }
                fingerprint = self.frame_scanner.output_fingerprint(scan_job, frame_number)
                frames.append(
                    {
                        "jobId": job_id,
                        "frameNumber": frame_number,
                        "status": FrameStatus.PENDING.value,
                        "workerId": None,
                        "assignmentId": None,
                        "leasedAt": None,
                        "leaseExpiresAt": None,
                        "startedAt": None,
                        "finishedAt": None,
                        "attemptCount": 0,
                        "errorMessage": None,
                        "baselineExists": 1 if fingerprint["exists"] else 0,
                        "baselineSize": fingerprint["size"],
                        "baselineMtimeNs": fingerprint["mtimeNs"],
                        "outputPath": self.frame_scanner.resolve_output_path(
                            scan_job,
                            frame_number,
                        ),
                    }
                )

            connection.executemany(
                """
                INSERT INTO job_frames(
                    jobId, frameNumber, status, workerId, assignmentId, leasedAt, leaseExpiresAt,
                    startedAt, finishedAt, attemptCount, errorMessage, outputPath
                    , baselineExists, baselineSize, baselineMtimeNs
                )
                VALUES(
                    :jobId, :frameNumber, :status, :workerId, :assignmentId, :leasedAt, :leaseExpiresAt,
                    :startedAt, :finishedAt, :attemptCount, :errorMessage, :outputPath
                    , :baselineExists, :baselineSize, :baselineMtimeNs
                )
                """,
                frames,
            )
            self._recompute_queue_positions(connection)

        self.logger.info(
            f"Job '{job_id}' queued by master '{payload['submittedByMasterId']}'.",
            category="job",
            entity_type="job",
            entity_id=job_id,
            details={
                "jobName": payload["jobName"],
                "submittedByUser": payload["submittedByUser"],
                "startFrame": payload["startFrame"],
                "endFrame": payload["endFrame"],
                "gpuOnlyWorkers": normalize_bool(payload.get("gpuOnlyWorkers"), False),
                "requiredBlenderVersion": payload.get("requiredBlenderVersion"),
                "preferredBlenderMajor": payload.get("preferredBlenderMajor"),
                "versionRoutingPolicy": payload.get("versionRoutingPolicy"),
            },
        )
        return self.get_job(job_id)

    def preflight_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        job = self._normalize_job_payload(dict(payload))
        self._validate_job_payload(job)
        evaluations = self.worker_registry.evaluate_workers_for_job(job, only_free=False)
        filtered_evaluations = self.worker_registry.filter_evaluations_for_job(job, evaluations)
        serialized_evaluations = self._serialize_worker_validation(filtered_evaluations)
        same_major_sufficient_available = any(
            bool((evaluation.get("response") or {}).get("selectedVersionBucket") in {"same_major_exact", "same_major_sufficient"})
            and evaluation.get("valid")
            for evaluation in filtered_evaluations
        )
        same_major_older_versions = sorted(
            {
                str(
                    (evaluation.get("response") or {}).get("bestSameMajorOlderVersion")
                    or (evaluation.get("response") or {}).get("selectedBlenderVersion")
                    or ""
                )
                for evaluation in evaluations
                if (
                    normalize_bool((evaluation.get("response") or {}).get("sameMajorOlderAvailable"))
                    or (evaluation.get("response") or {}).get("selectedVersionBucket") == "same_major_older"
                )
                and str(
                    (evaluation.get("response") or {}).get("bestSameMajorOlderVersion")
                    or (evaluation.get("response") or {}).get("selectedBlenderVersion")
                    or ""
                ).strip()
            }
            ,
            key=self._version_text_sort_key,
        )
        higher_major_versions = sorted(
            {
                str(
                    (evaluation.get("response") or {}).get("bestHigherMajorVersion")
                    or (evaluation.get("response") or {}).get("selectedBlenderVersion")
                    or ""
                )
                for evaluation in evaluations
                if (
                    normalize_bool((evaluation.get("response") or {}).get("higherMajorAvailable"))
                    or (evaluation.get("response") or {}).get("selectedVersionBucket") == "higher_major"
                )
                and str(
                    (evaluation.get("response") or {}).get("bestHigherMajorVersion")
                    or (evaluation.get("response") or {}).get("selectedBlenderVersion")
                    or ""
                ).strip()
            }
            ,
            key=self._version_text_sort_key,
        )
        same_major_older_available = bool(same_major_older_versions) or any(
            normalize_bool((evaluation.get("response") or {}).get("sameMajorOlderAvailable"))
            for evaluation in evaluations
        )
        higher_major_available = bool(higher_major_versions) or any(
            normalize_bool((evaluation.get("response") or {}).get("higherMajorAvailable"))
            for evaluation in evaluations
        )
        best_same_major_older_version = same_major_older_versions[-1] if same_major_older_versions else ""
        best_higher_major_version = higher_major_versions[0] if higher_major_versions else ""
        needs_version_choice = bool(
            job.get("requiredBlenderVersion")
            and same_major_older_available
            and not same_major_sufficient_available
        )
        return {
            "job": {
                "gpuOnlyWorkers": normalize_bool(job.get("gpuOnlyWorkers"), False),
                "requiredBlenderVersion": job.get("requiredBlenderVersion"),
                "preferredBlenderMajor": job.get("preferredBlenderMajor"),
                "versionRoutingPolicy": job.get("versionRoutingPolicy"),
                "blendVersionDetected": job.get("blendVersionDetected"),
            },
            "needsVersionChoice": needs_version_choice,
            "sameMajorSufficientAvailable": same_major_sufficient_available,
            "sameMajorOlderAvailable": same_major_older_available,
            "bestSameMajorOlderVersion": best_same_major_older_version,
            "sameMajorOlderVersions": same_major_older_versions,
            "higherMajorAvailable": higher_major_available,
            "bestHigherMajorVersion": best_higher_major_version,
            "higherMajorVersions": higher_major_versions,
            "workerValidation": serialized_evaluations,
        }

    def list_jobs(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
            SELECT *
            FROM jobs
            ORDER BY
                CASE WHEN status IN ('running', 'paused', 'blocked', 'validating') THEN 0 ELSE 1 END,
                queuePosition ASC,
                createdAt DESC
            """
        )
        return [self._job_snapshot(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._get_job_row(job_id)
        frames = self.database.fetch_all(
            """
            SELECT jf.frameNumber, jf.status, jf.workerId, current_worker.name AS workerName,
                   jf.assignmentId, jf.leasedAt, jf.leaseExpiresAt, jf.startedAt,
                   jf.finishedAt, jf.attemptCount, jf.errorMessage,
                   jf.lastFailedWorkerId, failed_worker.name AS lastFailedWorkerName,
                   jf.lastFailedAt, jf.lastFailedRenderDevicePreference, jf.lastFailureReason,
                   jf.outputPath
            FROM job_frames jf
            LEFT JOIN workers current_worker ON current_worker.workerId = jf.workerId
            LEFT JOIN workers failed_worker ON failed_worker.workerId = jf.lastFailedWorkerId
            WHERE jf.jobId = ?
            ORDER BY frameNumber
            """,
            (job_id,),
        )
        snapshot = self._job_snapshot(job)
        snapshot["frames"] = frames
        snapshot["workerStats"] = self._job_worker_stats(job_id)
        return snapshot

    def pause_job(self, job_id: str) -> dict[str, Any]:
        job = self._get_job_row(job_id)
        if job["status"] not in {
            JobStatus.QUEUED.value,
            JobStatus.VALIDATING.value,
            JobStatus.RUNNING.value,
            JobStatus.BLOCKED.value,
        }:
            return self.get_job(job_id)
        self.database.execute(
            "UPDATE jobs SET status = ?, lastError = NULL WHERE jobId = ?",
            (JobStatus.PAUSED.value, job_id),
        )
        self._cancel_active_worker_assignments(job_id, requeue_frames=True)
        self.logger.warning(
            f"Job '{job_id}' paused.",
            category="job",
            entity_type="job",
            entity_id=job_id,
        )
        return self.get_job(job_id)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        job = self._get_job_row(job_id)
        if job["status"] not in {JobStatus.PAUSED.value, JobStatus.BLOCKED.value}:
            return self.get_job(job_id)
        self.database.execute(
            "UPDATE jobs SET status = ?, lastError = NULL WHERE jobId = ?",
            (JobStatus.QUEUED.value, job_id),
        )
        self.run_scheduler_cycle()
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self._get_job_row(job_id)
        if job["status"] in {JobStatus.COMPLETED.value, JobStatus.CANCELLED.value, JobStatus.ERROR.value}:
            return self.get_job(job_id)
        now = utc_now_iso()
        self.database.execute(
            """
            UPDATE jobs
            SET status = ?, finishedAt = ?, lastError = NULL
            WHERE jobId = ?
            """,
            (JobStatus.CANCELLED.value, now, job_id),
        )
        self.database.execute(
            """
            UPDATE job_frames
            SET status = ?, workerId = NULL, assignmentId = NULL, leasedAt = NULL, leaseExpiresAt = NULL,
                errorMessage = NULL
            WHERE jobId = ? AND status != ?
            """,
            (FrameStatus.CANCELLED.value, job_id, FrameStatus.DONE.value),
        )
        self._cancel_active_worker_assignments(job_id, requeue_frames=False)
        with self.database.transaction() as connection:
            self._recompute_queue_positions(connection)
        self.logger.warning(
            f"Job '{job_id}' cancelled.",
            category="job",
            entity_type="job",
            entity_id=job_id,
        )
        return self.get_job(job_id)

    def get_state(self) -> dict[str, Any]:
        raw_jobs = self.list_jobs()
        active_job_summary = self._primary_active_job_summary(raw_jobs)
        active_job_id = active_job_summary["jobId"] if active_job_summary else None
        active_job = self._state_job_snapshot(active_job_summary, include_blocks=True) if active_job_summary else None
        jobs = [
            active_job
            if active_job and job["jobId"] == active_job_id
            else self._state_job_snapshot(job)
            for job in raw_jobs
        ]
        queue = [job for job in jobs if job["status"] == JobStatus.QUEUED.value]
        workers = [self._state_worker_snapshot(worker) for worker in self._state_workers(self.list_workers())]
        has_waiting_jobs = any(
            job["status"] in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}
            for job in raw_jobs
        )
        for worker in workers:
            worker["idleReason"] = self._worker_idle_reason(worker, has_waiting_jobs=has_waiting_jobs)
        masters = self._active_masters()
        metrics = self._state_metrics(active_job, workers, queue)
        return {
            "coordinator": {
                "coordinatorId": self.settings.coordinator_id,
                "status": "running",
                "startedAt": self.started_at,
                "appVersion": self.runtime_metadata["appVersion"],
                "protocolVersion": self.runtime_metadata["protocolVersion"],
                "buildId": self.runtime_metadata["buildId"],
                "buildTimestampUtc": self.runtime_metadata["buildTimestampUtc"],
                "language": self.settings.language,
                "supportedLanguages": ["en", "pt", "es"],
                "discovery": self.coordinator_discovery.snapshot() if self.coordinator_discovery else None,
                "scheduler": self._scheduler_status_getter() if self._scheduler_status_getter else None,
            },
            "masters": masters,
            "workers": workers,
            "activeJob": active_job,
            "job": active_job,
            "queue": queue,
            "jobs": jobs,
            "metrics": metrics,
            "logs": {
                "general": self.logger.tail_general_log(),
            },
        }

    @staticmethod
    def _worker_idle_reason(worker: dict[str, Any], *, has_waiting_jobs: bool) -> str | None:
        status = str(worker.get("status") or "").lower()
        if worker.get("currentAssignmentId") or worker.get("currentFrame") is not None:
            return None
        if not normalize_bool(worker.get("enabled"), True):
            return "worker_disabled"
        if status == WorkerStatus.OFFLINE.value:
            return "worker_offline"
        if status == WorkerStatus.ERROR.value:
            return "worker_error"
        if status in {WorkerStatus.QUEUED.value, WorkerStatus.DRAINING.value, WorkerStatus.CLOSING.value}:
            return "session_transition"
        if has_waiting_jobs:
            return "waiting_for_compatible_assignment"
        return "no_queued_work"

    def get_logs(self) -> dict[str, Any]:
        return {
            "general": self.logger.tail_general_log(),
            "events": self.logger.list_events(),
        }

    def build_job_report_pdf(self, job_id: str) -> tuple[str, bytes]:
        report = self._job_report_snapshot(job_id)
        lines = self._job_report_lines(report)
        filename = self._job_report_filename(report)
        pdf_bytes = build_text_pdf(title=f"Job Report - {report['jobName']}", lines=lines)
        return filename, pdf_bytes

    def run_scheduler_cycle(self) -> None:
        if not self._cycle_lock.acquire(blocking=False):
            return
        try:
            self.worker_registry.refresh_workers()
            self._drain_finished_job_sessions()
            self._reconcile_worker_reported_results()
            self._promote_rendering_frames_from_worker_state()
            self._refresh_leases_from_worker_queue_state()
            self._expire_old_leases()
            self._recover_offline_workers()
            self._attempt_unblock_jobs()

            for job in self._list_schedulable_jobs():
                self._run_job_cycle(job["jobId"])

            self._activate_queued_jobs_with_free_workers()
        finally:
            self._cycle_lock.release()

    def _active_masters(self) -> list[dict[str, Any]]:
        grace_seconds = max(int(self.settings.worker_status_grace_seconds), 15)
        return [
            master
            for master in self.list_masters()
            if self._is_recent_timestamp(master.get("lastSeen"), seconds=grace_seconds)
        ]

    def _state_workers(self, workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [worker for worker in workers if self._is_worker_visible_in_state(worker)]

    def _state_worker_snapshot(self, worker: dict[str, Any]) -> dict[str, Any]:
        payload = dict(worker)
        worker_id = str(payload.get("workerId") or payload.get("id") or f"{payload.get('ip')}:{payload.get('port')}")
        worker_name = str(payload.get("name") or payload.get("machineName") or worker_id)
        current_job_id = self._first_defined(
            payload.get("currentJobId"),
            payload.get("jobId"),
            payload.get("currentJob"),
        )
        current_block_id = self._first_defined(
            payload.get("currentBlockId"),
            payload.get("blockId"),
            payload.get("currentBlock"),
        )
        current_frame = payload.get("currentFrame")
        progress = self._normalize_progress_percent(payload.get("progress"))
        blender_found = normalize_bool(payload.get("blenderFound"), False)
        effective_use_gpu = normalize_bool(payload.get("effectiveUseGpu"), None)
        snapshot = {
            **payload,
            "id": worker_id,
            "workerId": worker_id,
            "name": worker_name,
            "machineName": worker_name,
            "machine_name": worker_name,
            "status": str(payload.get("status") or WorkerStatus.OFFLINE.value),
            "currentJobId": current_job_id,
            "jobId": current_job_id,
            "currentJob": current_job_id,
            "current_job_id": current_job_id,
            "currentBlockId": current_block_id,
            "blockId": current_block_id,
            "currentBlock": current_block_id,
            "current_block_id": current_block_id,
            "currentFrame": current_frame,
            "current_frame": current_frame,
            "progress": float(progress or 0.0),
            "lastLog": str(payload.get("lastLog") or ""),
            "last_log": str(payload.get("lastLog") or ""),
            "error": payload.get("error"),
            "blenderFound": bool(blender_found),
            "blender_found": bool(blender_found),
            "blenderVersion": payload.get("blenderVersion"),
            "blender_version": payload.get("blenderVersion"),
            "gpu": payload.get("gpu"),
            "renderDevicePreference": payload.get("renderDevicePreference"),
            "render_device_preference": payload.get("renderDevicePreference"),
            "effectiveUseGpu": effective_use_gpu,
            "effective_use_gpu": effective_use_gpu,
        }
        return snapshot

    def _is_worker_visible_in_state(self, worker: dict[str, Any]) -> bool:
        if (
            worker.get("currentJobId")
            or worker.get("currentBlockId")
            or worker.get("currentAssignmentId")
            or worker.get("currentFrame") is not None
        ):
            return True

        visibility_seconds = max(int(self.settings.worker_status_grace_seconds) * 15, 300)
        if self._is_recent_timestamp(worker.get("lastSeen"), seconds=visibility_seconds):
            return True
        # A failed first connection must remain visible so its address can be fixed.
        if self._is_recent_timestamp(worker.get("createdAt"), seconds=visibility_seconds):
            return True

        status = str(worker.get("status") or "").lower()
        if status not in {WorkerStatus.OFFLINE.value, WorkerStatus.ERROR.value}:
            for field in ("updatedAt", "createdAt"):
                if self._is_recent_timestamp(worker.get(field), seconds=visibility_seconds):
                    return True
        return False

    @staticmethod
    def _is_recent_timestamp(value: Any, *, seconds: int) -> bool:
        if value in {None, ""}:
            return False
        try:
            observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return False
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        return age_seconds <= float(seconds)

    @staticmethod
    def _elapsed_seconds(started_at: Any) -> float:
        if started_at in {None, ""}:
            return 0.0
        try:
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds())

    @staticmethod
    def _estimate_eta_seconds(*, elapsed_seconds: float, overall_percent: float | None) -> float | None:
        if overall_percent is None or overall_percent <= 0.0 or overall_percent >= 100.0:
            return None
        return max(0.0, elapsed_seconds * ((100.0 - overall_percent) / overall_percent))

    @staticmethod
    def _normalize_progress_percent(value: Any) -> float | None:
        if value in {None, ""}:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if 0.0 < numeric <= 1.0:
            numeric *= 100.0
        return max(0.0, min(100.0, numeric))

    @staticmethod
    def _first_defined(*values: Any) -> Any:
        for value in values:
            if value is not None:
                return value
        return None

    def _activate_next_job_if_possible(self) -> dict[str, Any] | None:
        next_job = self.database.fetch_one(
            """
            SELECT *
            FROM jobs
            WHERE status = ?
            ORDER BY queuePosition ASC, createdAt ASC
            LIMIT 1
            """,
            (JobStatus.QUEUED.value,),
        )
        if not next_job:
            return None
        return self._attempt_activate_job(next_job["jobId"])

    def _activate_queued_jobs_with_free_workers(self) -> None:
        queued_jobs = self.database.fetch_all(
            """
            SELECT *
            FROM jobs
            WHERE status = ?
            ORDER BY queuePosition ASC, createdAt ASC
            """,
            (JobStatus.QUEUED.value,),
        )
        for job in queued_jobs:
            if not self._has_eligible_worker_for_job(job, only_free=False):
                self._attempt_activate_job(job["jobId"])
                continue
            if not self._has_eligible_worker_for_job(job, only_free=True):
                continue
            activated = self._attempt_activate_job(job["jobId"])
            if activated["status"] == JobStatus.RUNNING.value:
                self._run_job_cycle(activated["jobId"])

    def _has_eligible_worker_for_job(self, job: dict[str, Any], *, only_free: bool) -> bool:
        evaluations = self.worker_registry.evaluate_workers_for_job(job, only_free=only_free)
        filtered_evaluations = self._apply_job_worker_failure_quarantine(job["jobId"], evaluations)
        filtered_evaluations = self.worker_registry.filter_evaluations_for_job(job, filtered_evaluations)
        return any(evaluation.get("valid") for evaluation in filtered_evaluations)

    def _run_job_cycle(self, job_id: str) -> None:
        job = self._get_job_row(job_id)
        if job["status"] != JobStatus.RUNNING.value:
            return
        self._scan_and_confirm_outputs(job_id)
        self._reconcile_worker_reported_results(job_id=job_id)
        self._promote_rendering_frames_from_worker_state(job_id=job_id)
        self._refresh_leases_from_worker_queue_state(job_id=job_id)
        self._dispatch_pending_frames(job_id)
        self._scan_and_confirm_outputs(job_id)
        self._reconcile_worker_reported_results(job_id=job_id)
        self._finalize_job_if_done(job_id)

    def _list_schedulable_jobs(self) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT *
            FROM jobs
            WHERE status = ?
            ORDER BY queuePosition ASC, createdAt ASC
            """,
            (JobStatus.RUNNING.value,),
        )

    def _attempt_unblock_jobs(self) -> None:
        if not self.settings.auto_unblock_blocked_jobs:
            return
        blocked_jobs = self.database.fetch_all(
            """
            SELECT *
            FROM jobs
            WHERE status = ?
            ORDER BY queuePosition ASC, createdAt ASC
            """
            ,
            (JobStatus.BLOCKED.value,),
        )
        for blocked_job in blocked_jobs:
            if self._job_has_error_frames(blocked_job["jobId"]):
                continue
            self._attempt_activate_job(blocked_job["jobId"])

    @staticmethod
    def _primary_active_job_summary(raw_jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
        preferred_order = {
            JobStatus.RUNNING.value: 0,
            JobStatus.VALIDATING.value: 1,
            JobStatus.PAUSED.value: 2,
            JobStatus.BLOCKED.value: 3,
        }
        active_jobs = [job for job in raw_jobs if job["status"] in ACTIVE_JOB_STATUSES]
        if not active_jobs:
            return None
        return min(
            active_jobs,
            key=lambda job: (
                preferred_order.get(str(job.get("status") or ""), 99),
                int(job.get("queuePosition") or 0),
                str(job.get("createdAt") or ""),
            ),
        )

    def _attempt_activate_job(self, job_id: str) -> dict[str, Any]:
        job = self._get_job_row(job_id)
        if job["status"] in {JobStatus.COMPLETED.value, JobStatus.CANCELLED.value, JobStatus.ERROR.value}:
            return job

        if normalize_bool(job["autoAddWorkers"], True) and not self.worker_registry.list_workers():
            self.worker_registry.discover_workers()

        self.database.execute("UPDATE jobs SET status = ? WHERE jobId = ?", (JobStatus.VALIDATING.value, job_id))
        job = self._get_job_row(job_id)
        evaluations = self.worker_registry.evaluate_workers_for_job(job, only_free=False)
        filtered_evaluations = self._apply_job_worker_failure_quarantine(job_id, evaluations)
        filtered_evaluations = self.worker_registry.filter_evaluations_for_job(job, filtered_evaluations)
        serialized_evaluations = self._serialize_worker_validation(filtered_evaluations)
        rejected_workers = [item for item in serialized_evaluations if not item["valid"]]
        self._sync_job_version_metadata(job_id, job, evaluations)
        job = self._get_job_row(job_id)
        eligible_workers = [evaluation["worker"] for evaluation in filtered_evaluations if evaluation["valid"]]
        if not eligible_workers:
            reason = self._build_block_reason(job, filtered_evaluations)
            self.database.execute(
                "UPDATE jobs SET status = ?, lastError = ? WHERE jobId = ?",
                (JobStatus.BLOCKED.value, reason, job_id),
            )
            self.logger.warning(
                f"Job '{job_id}' is blocked: {reason}",
                category="job",
                entity_type="job",
                entity_id=job_id,
                details={
                    "rejectedWorkers": rejected_workers,
                    "blendVersionDetected": job.get("blendVersionDetected"),
                    "requiredBlenderVersion": job.get("requiredBlenderVersion"),
                    "preferredBlenderMajor": job.get("preferredBlenderMajor"),
                    "versionRoutingPolicy": job.get("versionRoutingPolicy"),
                    "workerValidation": serialized_evaluations,
                },
            )
            return self._get_job_row(job_id)

        self.database.execute(
            """
            UPDATE jobs
            SET status = ?, startedAt = COALESCE(startedAt, ?), lastError = NULL
            WHERE jobId = ?
            """,
            (JobStatus.RUNNING.value, utc_now_iso(), job_id),
        )
        self._scan_and_confirm_outputs(job_id)
        self.logger.info(
            f"Job '{job_id}' activated.",
            category="job",
            entity_type="job",
            entity_id=job_id,
            details={
                "validatedWorkers": [worker["workerId"] for worker in eligible_workers],
                "rejectedWorkers": rejected_workers,
                "gpuOnlyWorkers": normalize_bool(job.get("gpuOnlyWorkers"), False),
                "blendVersionDetected": job.get("blendVersionDetected"),
                "requiredBlenderVersion": job.get("requiredBlenderVersion"),
                "preferredBlenderMajor": job.get("preferredBlenderMajor"),
                "versionRoutingPolicy": job.get("versionRoutingPolicy"),
            },
        )
        return self._get_job_row(job_id)

    def _dispatch_pending_frames(self, job_id: str) -> None:
        job = self._get_job_row(job_id)
        if job["status"] != JobStatus.RUNNING.value:
            return

        evaluations = self.worker_registry.evaluate_workers_for_job(job, only_free=False)
        evaluations = self._apply_job_worker_failure_quarantine(job_id, evaluations)
        evaluations = self.worker_registry.filter_evaluations_for_job(job, evaluations)
        valid_workers = [evaluation["worker"] for evaluation in evaluations if evaluation["valid"]]
        performance_profiles = self._job_worker_performance_profiles(job_id, valid_workers)
        immediate_workers = self._sort_session_affinity_workers(
            [
                worker
                for worker in valid_workers
                if self._worker_can_queue_same_job_session(
                    worker,
                    job_id,
                    performance_profiles=performance_profiles,
                )
            ]
        )
        free_workers = [
            worker
            for worker in valid_workers
            if self.worker_registry.is_worker_free(worker)
        ]
        immediate_workers = immediate_workers + [
            worker
            for worker in free_workers
            if worker["workerId"] not in {item["workerId"] for item in immediate_workers}
        ]
        failed_worker_reasons = self._job_failed_worker_reasons(job_id)
        if failed_worker_reasons:
            clean_valid_workers = [
                worker
                for worker in valid_workers
                if worker["workerId"] not in failed_worker_reasons
            ]
            if clean_valid_workers:
                valid_workers = clean_valid_workers
                immediate_workers = [
                    worker
                    for worker in immediate_workers
                    if worker["workerId"] not in failed_worker_reasons
                ]
            elif self._has_online_non_failed_worker_candidate(job, set(failed_worker_reasons)):
                return
        if not immediate_workers:
            return

        pending_frames = self.database.fetch_all(
            """
            SELECT frameNumber, status, workerId, assignmentId, leasedAt, leaseExpiresAt, startedAt,
                   finishedAt, attemptCount, errorMessage, outputPath, lastFailedWorkerId,
                   lastFailedAt, lastFailedRenderDevicePreference, lastFailureReason
            FROM job_frames
            WHERE jobId = ? AND status = ?
            ORDER BY frameNumber ASC
            """,
            (job_id, FrameStatus.PENDING.value),
        )
        if not pending_frames:
            self._drain_workers_without_future_job_frames(job_id, planned_worker_ids=set())
            return

        available_worker_ids = [str(worker["workerId"]) for worker in immediate_workers]
        planner_blocked_worker_ids: set[str] = set()
        planned_worker_ids: set[str] = set()
        planner_available_at = self._initialize_tail_dispatch_planner_loads(
            workers=self._collect_tail_dispatch_workers(
                valid_workers,
                set(available_worker_ids),
                planner_blocked_worker_ids,
                job_id=job_id,
            ),
            performance_profiles=performance_profiles,
            job_id=job_id,
        )
        for frame in pending_frames:
            available_workers = self._collect_cycle_dispatch_workers(
                available_worker_ids,
                job_id=job_id,
                performance_profiles=performance_profiles,
            )
            if not available_workers:
                return
            frame_number = int(frame["frameNumber"])
            if normalize_bool(job["ignoreRenderedFrames"], True) and self.frame_scanner.frame_exists(job, frame_number):
                self._mark_frame_done(job_id, frame_number, worker_id=None)
                continue

            immediate_worker_ids = {str(worker["workerId"]) for worker in available_workers}
            planning_workers = self._collect_tail_dispatch_workers(
                valid_workers,
                immediate_worker_ids,
                planner_blocked_worker_ids,
                job_id=job_id,
            )
            assignment_plan = self._plan_pending_frame_assignment(
                frame,
                planning_workers,
                immediate_worker_ids=immediate_worker_ids,
                performance_profiles=performance_profiles,
                planner_available_at=planner_available_at,
            )
            if not assignment_plan:
                continue
            if assignment_plan["action"] == "reserve":
                planned_worker_ids.add(str(assignment_plan["workerId"]))
                self._reserve_worker_planner_slot(
                    str(assignment_plan["workerId"]),
                    planner_available_at,
                    performance_profiles,
                )
                continue

            available_workers_by_id = {
                str(worker["workerId"]): worker
                for worker in available_workers
            }
            candidate_workers = [
                available_workers_by_id[worker_id]
                for worker_id in assignment_plan["candidateWorkerIds"]
                if worker_id in available_workers_by_id
            ]
            if not candidate_workers:
                continue

            for worker in candidate_workers:
                outcome = self._dispatch_frame_to_worker(job, frame, worker)
                refreshed_worker = self.worker_registry.get_worker(worker["workerId"]) or worker
                keep_worker_available = self._worker_can_queue_same_job_session(
                    refreshed_worker,
                    job_id,
                    performance_profiles=performance_profiles,
                )
                if outcome == "accepted":
                    planned_worker_ids.add(str(worker["workerId"]))
                    self._reserve_worker_planner_slot(
                        str(worker["workerId"]),
                        planner_available_at,
                        performance_profiles,
                    )
                    if not keep_worker_available:
                        available_worker_ids = [
                            worker_id
                            for worker_id in available_worker_ids
                            if worker_id != worker["workerId"]
                        ]
                    break
                if outcome in {"busy", "failed"}:
                    available_worker_ids = [
                        worker_id
                        for worker_id in available_worker_ids
                        if worker_id != worker["workerId"]
                    ]
                    planner_blocked_worker_ids.add(str(worker["workerId"]))
        self._drain_workers_without_future_job_frames(job_id, planned_worker_ids=planned_worker_ids)

    def _collect_cycle_dispatch_workers(
        self,
        worker_ids: list[str],
        *,
        job_id: str,
        performance_profiles: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        refreshed_workers = [
            self.worker_registry.get_worker(worker_id)
            for worker_id in worker_ids
        ]
        live_workers = [worker for worker in refreshed_workers if worker]
        sticky_workers = self._sort_session_affinity_workers(
            [
                worker
                for worker in live_workers
                if self._worker_can_queue_same_job_session(
                    worker,
                    job_id,
                    performance_profiles=performance_profiles,
                )
            ]
        )
        free_workers = [
            worker
            for worker in live_workers
            if self.worker_registry.is_worker_free(worker)
        ]
        sticky_ids = {worker["workerId"] for worker in sticky_workers}
        return sticky_workers + [
            worker
            for worker in free_workers
            if worker["workerId"] not in sticky_ids
        ]

    def _select_workers_for_pending_frame(
        self,
        frame: dict[str, Any],
        workers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        last_failed_worker_id = str(frame.get("lastFailedWorkerId") or "").strip()
        if not last_failed_worker_id:
            return list(workers)
        alternative_workers = [
            worker
            for worker in workers
            if worker["workerId"] != last_failed_worker_id
        ]
        return alternative_workers or list(workers)

    @staticmethod
    def _worker_can_accept_same_job_session(worker: dict[str, Any], job_id: str) -> bool:
        if not normalize_bool(worker.get("persistentSessionEnabled"), False):
            return False
        if str(worker.get("currentJobId") or "").strip() != str(job_id):
            return False
        session_state = str(worker.get("sessionState") or worker.get("status") or "").strip().lower()
        return session_state in {"session_open", "queued", "rendering", "draining"}

    def _worker_can_queue_same_job_session(
        self,
        worker: dict[str, Any],
        job_id: str,
        *,
        performance_profiles: dict[str, dict[str, Any]],
    ) -> bool:
        if not self._worker_can_accept_same_job_session(worker, job_id):
            return False
        worker_id = str(worker.get("workerId") or "").strip()
        if not worker_id:
            return False
        profile = performance_profiles.get(worker_id, {})
        if int(profile.get("consecutiveFailureCount") or 0) > 0:
            return False
        if int(profile.get("successCount") or 0) > 0:
            return True
        return int(worker.get("sessionQueueDepth") or 0) <= 0

    @staticmethod
    def _sort_session_affinity_workers(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            workers,
            key=lambda worker: (
                int(worker.get("sessionQueueDepth") or 0),
                1 if worker.get("currentFrame") is None else 0,
                str(worker.get("name") or worker.get("workerId")).lower(),
            ),
        )

    def _collect_tail_dispatch_workers(
        self,
        workers: list[dict[str, Any]],
        immediate_worker_ids: set[str],
        blocked_worker_ids: set[str],
        *,
        job_id: str,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen_worker_ids: set[str] = set()
        for worker in workers:
            worker_id = str(worker.get("workerId") or "").strip()
            if not worker_id or worker_id in seen_worker_ids or worker_id in blocked_worker_ids:
                continue
            refreshed_worker = self.worker_registry.get_worker(worker_id) or worker
            if (
                worker_id in immediate_worker_ids
                or self._worker_has_same_job_backlog(refreshed_worker, job_id)
            ):
                selected.append(refreshed_worker)
                seen_worker_ids.add(worker_id)
        return selected

    @staticmethod
    def _worker_has_same_job_backlog(worker: dict[str, Any], job_id: str) -> bool:
        if str(worker.get("currentJobId") or "").strip() != str(job_id):
            return False
        if worker.get("currentFrame") is not None:
            return True
        return int(worker.get("sessionQueueDepth") or 0) > 0

    def _job_worker_performance_profiles(
        self,
        job_id: str,
        workers: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        worker_ids = {
            str(worker.get("workerId") or "").strip()
            for worker in workers
            if str(worker.get("workerId") or "").strip()
        }
        if not worker_ids:
            return {}

        rows = self.database.fetch_all(
            """
            SELECT jobId, workerId, renderDevicePreference, successCount, timedSuccessCount,
                   totalSuccessRenderSeconds, lastSuccessRenderSeconds, consecutiveFailureCount
            FROM job_worker_stats
            ORDER BY workerId ASC, renderDevicePreference ASC
            """,
        )
        rows_by_worker: dict[str, list[dict[str, Any]]] = {}
        current_rows_by_worker: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            worker_id = str(row.get("workerId") or "").strip()
            if not worker_id or worker_id not in worker_ids:
                continue
            rows_by_worker.setdefault(worker_id, []).append(row)
            if str(row.get("jobId")) == str(job_id):
                current_rows_by_worker.setdefault(worker_id, []).append(row)

        profiles: dict[str, dict[str, Any]] = {}
        known_average_seconds: list[float] = []

        for worker in workers:
            worker_id = str(worker.get("workerId") or "").strip()
            if not worker_id or worker_id in profiles:
                continue
            current_device = self._normalize_render_device_preference(worker.get("renderDevicePreference"))
            worker_rows = current_rows_by_worker.get(worker_id, [])
            matching_rows = [
                row
                for row in worker_rows
                if self._normalize_render_device_preference(row.get("renderDevicePreference")) == current_device
            ]
            preferred_rows = matching_rows or worker_rows
            timed_success_count = sum(int(row.get("timedSuccessCount") or 0) for row in preferred_rows)
            total_success_render_seconds = sum(
                float(row.get("totalSuccessRenderSeconds") or 0.0)
                for row in preferred_rows
            )
            success_count = sum(int(row.get("successCount") or 0) for row in preferred_rows)
            consecutive_failure_count = max(
                (int(row.get("consecutiveFailureCount") or 0) for row in preferred_rows),
                default=0,
            )
            timing_rows = preferred_rows
            timing_source = "job"
            if timed_success_count <= 0:
                global_rows = rows_by_worker.get(worker_id, [])
                global_matching_rows = [
                    row
                    for row in global_rows
                    if self._normalize_render_device_preference(row.get("renderDevicePreference")) == current_device
                ]
                timing_rows = global_matching_rows or global_rows
                timing_source = "worker_history" if timing_rows else "fallback"
                total_success_render_seconds = sum(
                    float(row.get("totalSuccessRenderSeconds") or 0.0)
                    for row in timing_rows
                )
                timed_success_count = sum(int(row.get("timedSuccessCount") or 0) for row in timing_rows)

            average_frame_seconds: float | None = None
            if timed_success_count > 0:
                average_frame_seconds = max(
                    0.1,
                    total_success_render_seconds / float(timed_success_count),
                )
                known_average_seconds.append(average_frame_seconds)

            profiles[worker_id] = {
                "workerId": worker_id,
                "successCount": success_count,
                "timedSuccessCount": timed_success_count,
                "totalSuccessRenderSeconds": total_success_render_seconds,
                "averageFrameSeconds": average_frame_seconds,
                "consecutiveFailureCount": consecutive_failure_count,
                "knownTiming": average_frame_seconds is not None,
                "timingSource": timing_source,
            }

        sorted_known_averages = sorted(known_average_seconds)
        if sorted_known_averages:
            fallback_index = len(sorted_known_averages) // 2
            fallback_average_seconds = float(sorted_known_averages[fallback_index])
        else:
            fallback_average_seconds = 1.0

        for worker_id, profile in profiles.items():
            if profile["averageFrameSeconds"] is not None:
                continue
            profile["averageFrameSeconds"] = fallback_average_seconds

        return profiles

    def _initialize_tail_dispatch_planner_loads(
        self,
        *,
        workers: list[dict[str, Any]],
        performance_profiles: dict[str, dict[str, Any]],
        job_id: str,
    ) -> dict[str, float]:
        planner_available_at: dict[str, float] = {}
        for worker in workers:
            worker_id = str(worker.get("workerId") or "").strip()
            if not worker_id:
                continue
            average_frame_seconds = float(
                performance_profiles.get(worker_id, {}).get("averageFrameSeconds") or 1.0
            )
            planner_available_at[worker_id] = self._estimate_worker_backlog_seconds(
                worker,
                job_id=job_id,
                average_frame_seconds=average_frame_seconds,
            )
        return planner_available_at

    def _estimate_worker_backlog_seconds(
        self,
        worker: dict[str, Any],
        *,
        job_id: str,
        average_frame_seconds: float,
    ) -> float:
        if average_frame_seconds <= 0.0:
            average_frame_seconds = 0.1
        if str(worker.get("currentJobId") or "").strip() != str(job_id):
            return 0.0

        backlog_seconds = float(max(0, int(worker.get("sessionQueueDepth") or 0))) * average_frame_seconds
        if worker.get("currentFrame") is None:
            return backlog_seconds

        progress = self._normalize_progress_percent(worker.get("progress"))
        remaining_factor = 1.0
        if progress is not None:
            remaining_factor = max(0.0, 1.0 - (progress / 100.0))
        backlog_seconds += remaining_factor * average_frame_seconds
        return max(0.0, backlog_seconds)

    def _plan_pending_frame_assignment(
        self,
        frame: dict[str, Any],
        workers: list[dict[str, Any]],
        *,
        immediate_worker_ids: set[str],
        performance_profiles: dict[str, dict[str, Any]],
        planner_available_at: dict[str, float],
    ) -> dict[str, Any] | None:
        candidate_workers = self._select_workers_for_pending_frame(frame, workers)
        if not candidate_workers:
            return None

        scored_candidates: list[tuple[float, int, str, str]] = []
        for worker in candidate_workers:
            worker_id = str(worker.get("workerId") or "").strip()
            if not worker_id:
                continue
            average_frame_seconds = float(
                performance_profiles.get(worker_id, {}).get("averageFrameSeconds") or 1.0
            )
            predicted_finish_seconds = float(planner_available_at.get(worker_id, 0.0)) + average_frame_seconds
            scored_candidates.append(
                (
                    predicted_finish_seconds,
                    0 if worker_id in immediate_worker_ids else 1,
                    str(worker.get("name") or worker_id).lower(),
                    worker_id,
                )
            )

        if not scored_candidates:
            return None

        scored_candidates.sort()
        selected_worker_id = scored_candidates[0][3]
        if selected_worker_id not in immediate_worker_ids:
            return {
                "action": "reserve",
                "workerId": selected_worker_id,
            }

        immediate_scored_candidates = [
            item
            for item in scored_candidates
            if item[3] in immediate_worker_ids
        ]
        return {
            "action": "dispatch",
            "candidateWorkerIds": [item[3] for item in immediate_scored_candidates],
        }

    @staticmethod
    def _reserve_worker_planner_slot(
        worker_id: str,
        planner_available_at: dict[str, float],
        performance_profiles: dict[str, dict[str, Any]],
    ) -> None:
        average_frame_seconds = float(
            performance_profiles.get(worker_id, {}).get("averageFrameSeconds") or 1.0
        )
        planner_available_at[worker_id] = float(planner_available_at.get(worker_id, 0.0)) + average_frame_seconds

    def _dispatch_frame_to_worker(self, job: dict[str, Any], frame: dict[str, Any], worker: dict[str, Any]) -> str:
        frame_number = int(frame["frameNumber"])
        attempt_count = int(frame["attemptCount"]) + 1
        assignment_id = new_assignment_id(frame_number, attempt_count)
        block_id = f"frame_{frame_number:06d}"
        now = utc_now_iso()
        reserved = self.database.execute(
            """
            UPDATE job_frames
            SET status = ?, workerId = ?, assignmentId = ?, leasedAt = ?, leaseExpiresAt = ?,
                startedAt = COALESCE(startedAt, ?), attemptCount = ?, errorMessage = NULL
            WHERE jobId = ? AND frameNumber = ? AND status = ? AND attemptCount = ?
            """,
            (
                FrameStatus.LEASED.value,
                worker["workerId"],
                assignment_id,
                now,
                iso_after_seconds(self.settings.lease_duration_seconds),
                now,
                attempt_count,
                job["jobId"],
                frame_number,
                FrameStatus.PENDING.value,
                int(frame["attemptCount"]),
            ),
        )
        if reserved != 1:
            return "busy"
        payload = {
            "jobId": job["jobId"],
            "blockId": block_id,
            "blendFile": job["blendFile"],
            "outputPattern": job["outputPattern"],
            "startFrame": frame_number,
            "endFrame": frame_number,
            "format": job.get("format", "PNG"),
            "useGpu": normalize_bool(job["legacyUseGpu"], True),
            "assignmentId": assignment_id,
            "coordinatorId": self.settings.coordinator_id,
            "submittedByMasterId": job["submittedByMasterId"],
        }
        for field in ("requiredBlenderVersion", "preferredBlenderMajor", "versionRoutingPolicy", "blendVersionDetected"):
            if job.get(field) is not None:
                payload[field] = job.get(field)
        base_url = f"http://{worker['ip']}:{worker['port']}"
        worker_was_free = self.worker_registry.is_worker_free(worker)
        try:
            response = self.worker_client.render_block(base_url, payload)
        except WorkerRequestError as exc:
            self._release_dispatch_reservation(job["jobId"], frame_number, assignment_id)
            if exc.status_code == 409:
                self._sync_busy_worker_from_response(worker, exc.response_json)
                log_method = self.logger.info if self._is_transient_worker_rejection(exc.response_json) else self.logger.warning
                log_method(
                    f"Worker '{worker['workerId']}' rejected frame {frame_number} because it is already busy.",
                    category="worker",
                    entity_type="worker",
                    entity_id=worker["workerId"],
                    details={"jobId": job["jobId"], "frameNumber": frame_number, "response": exc.response_json},
                )
                return "busy"
            self.logger.warning(
                f"Dispatch failed for worker '{worker['workerId']}' frame {frame_number}: {exc}",
                category="worker",
                entity_type="worker",
                entity_id=worker["workerId"],
                details={"jobId": job["jobId"], "frameNumber": frame_number, "statusCode": exc.status_code},
            )
            self.worker_registry.update_worker(worker["workerId"], {"status": WorkerStatus.ERROR.value, "error": str(exc)})
            return "failed"
        except Exception as exc:
            # A network timeout is ambiguous: the worker may have accepted the
            # assignment. Keep the lease until worker reconciliation or expiry
            # instead of issuing a duplicate assignment immediately.
            self.logger.warning(
                f"Dispatch failed for worker '{worker['workerId']}' frame {frame_number}: {exc}",
                category="worker",
                entity_type="worker",
                entity_id=worker["workerId"],
                details={"jobId": job["jobId"], "frameNumber": frame_number},
            )
            self.worker_registry.update_worker(worker["workerId"], {"status": WorkerStatus.ERROR.value, "error": str(exc)})
            return "failed"

        if not self._render_accepted(response):
            self._release_dispatch_reservation(job["jobId"], frame_number, assignment_id)
            self.logger.warning(
                f"Worker '{worker['workerId']}' rejected frame {frame_number}.",
                category="worker",
                entity_type="worker",
                entity_id=worker["workerId"],
                details={"jobId": job["jobId"], "response": response},
            )
            return "failed"

        response_current_job_id = response.get("currentJobId") or response.get("jobId")
        response_current_block_id = response.get("currentBlockId") or response.get("blockId")
        response_current_assignment_id = response.get("currentAssignmentId") or response.get("assignmentId")
        response_current_frame = response.get("currentFrame")
        if (
            worker_was_free
            or (
                str(response_current_job_id or "") == str(job["jobId"])
                and str(response_current_block_id or "") == str(block_id)
                and str(response_current_assignment_id or "") == str(assignment_id)
                and response_current_frame == frame_number
            )
        ):
            self.worker_registry.set_worker_busy(
                worker["workerId"],
                job_id=job["jobId"],
                block_id=block_id,
                frame_number=frame_number,
                assignment_id=assignment_id,
            )
        else:
            self.worker_registry.update_worker(
                worker["workerId"],
                {
                    "status": self._resolve_worker_status_from_response(
                        response,
                        fallback=worker.get("status") or WorkerStatus.BUSY.value,
                    ),
                    "currentJobId": response_current_job_id or worker.get("currentJobId"),
                    "currentBlockId": response_current_block_id or worker.get("currentBlockId"),
                    "currentFrame": response_current_frame if response_current_frame is not None else worker.get("currentFrame"),
                    "currentAssignmentId": response_current_assignment_id or worker.get("currentAssignmentId"),
                    "persistentSessionEnabled": response.get("persistentSessionEnabled"),
                    "sessionState": response.get("sessionState"),
                    "sessionQueueDepth": response.get("queueDepth"),
                    "sessionDrainRequested": response.get("drainRequested"),
                    "error": None,
                },
            )
        self.logger.info(
            f"Dispatched frame {frame_number} of job '{job['jobId']}' to worker '{worker['workerId']}'.",
            category="job",
            entity_type="job",
            entity_id=job["jobId"],
            details={"workerId": worker["workerId"], "frameNumber": frame_number, "assignmentId": assignment_id},
        )
        return "accepted"

    def _release_dispatch_reservation(self, job_id: str, frame_number: int, assignment_id: str) -> None:
        self.database.execute(
            """
            UPDATE job_frames
            SET status = ?, workerId = NULL, assignmentId = NULL, leasedAt = NULL,
                leaseExpiresAt = NULL, errorMessage = NULL
            WHERE jobId = ? AND frameNumber = ? AND assignmentId = ? AND status = ?
            """,
            (
                FrameStatus.PENDING.value,
                job_id,
                frame_number,
                assignment_id,
                FrameStatus.LEASED.value,
            ),
        )

    def _promote_rendering_frames_from_worker_state(self, job_id: str | None = None) -> None:
        query = """
            SELECT jf.jobId, jf.frameNumber, jf.workerId, jf.assignmentId, jf.status,
                   w.currentJobId, w.currentFrame, w.currentAssignmentId, w.status AS workerStatus
            FROM job_frames jf
            JOIN workers w ON w.workerId = jf.workerId
            WHERE jf.status IN (?, ?)
        """
        params: list[Any] = [FrameStatus.LEASED.value, FrameStatus.RENDERING.value]
        if job_id:
            query += " AND jf.jobId = ?"
            params.append(job_id)
        rows = self.database.fetch_all(query, tuple(params))
        active_worker_states = {
            WorkerStatus.RENDERING.value,
            WorkerStatus.BUSY.value,
            WorkerStatus.QUEUED.value,
            WorkerStatus.DRAINING.value,
        }
        for row in rows:
            if row["workerStatus"] not in active_worker_states:
                continue
            if row["currentJobId"] != row["jobId"]:
                continue
            if row["currentFrame"] != row["frameNumber"]:
                continue
            if row.get("assignmentId") and row.get("currentAssignmentId") and row["assignmentId"] != row["currentAssignmentId"]:
                continue
            self.database.execute(
                """
                UPDATE job_frames
                SET status = ?, leaseExpiresAt = ?, startedAt = COALESCE(startedAt, ?)
                WHERE jobId = ? AND frameNumber = ?
                """,
                (
                    FrameStatus.RENDERING.value,
                    iso_after_seconds(self.settings.lease_duration_seconds),
                    utc_now_iso(),
                    row["jobId"],
                    row["frameNumber"],
                ),
            )

    def _refresh_leases_from_worker_queue_state(self, job_id: str | None = None) -> None:
        query = """
            SELECT jf.jobId, jf.frameNumber, jf.workerId, jf.assignmentId, jf.status, w.queuedAssignments
            FROM job_frames jf
            JOIN workers w ON w.workerId = jf.workerId
            WHERE jf.status IN (?, ?)
        """
        params: list[Any] = [FrameStatus.LEASED.value, FrameStatus.RENDERING.value]
        if job_id:
            query += " AND jf.jobId = ?"
            params.append(job_id)
        rows = self.database.fetch_all(query, tuple(params))
        queued_assignment_ids: set[tuple[str, int, str]] = set()
        for row in rows:
            raw_assignments = row.get("queuedAssignments")
            assignments = raw_assignments
            if isinstance(raw_assignments, str) and raw_assignments.strip():
                try:
                    assignments = json.loads(raw_assignments)
                except ValueError:
                    assignments = None
            if not isinstance(assignments, list):
                continue
            for item in assignments:
                if not isinstance(item, dict):
                    continue
                assignment_id = str(item.get("assignmentId") or "").strip()
                block_job_id = str(item.get("jobId") or row.get("jobId") or "").strip()
                try:
                    frame_number = int(item.get("startFrame"))
                except (TypeError, ValueError):
                    continue
                if not assignment_id or not block_job_id:
                    continue
                queued_assignment_ids.add((block_job_id, frame_number, assignment_id))

        for row in rows:
            assignment_id = str(row.get("assignmentId") or "").strip()
            if not assignment_id:
                continue
            identity = (str(row["jobId"]), int(row["frameNumber"]), assignment_id)
            if identity not in queued_assignment_ids:
                continue
            self.database.execute(
                """
                UPDATE job_frames
                SET leaseExpiresAt = ?
                WHERE jobId = ? AND frameNumber = ?
                """,
                (
                    iso_after_seconds(self.settings.lease_duration_seconds),
                    row["jobId"],
                    row["frameNumber"],
                ),
            )

    def _scan_and_confirm_outputs(self, job_id: str) -> None:
        job = self._get_job_row(job_id)
        frames = self.database.fetch_all(
            """
            SELECT frameNumber, status, workerId, baselineExists, baselineSize, baselineMtimeNs
            FROM job_frames
            WHERE jobId = ? AND status NOT IN (?, ?)
            ORDER BY frameNumber ASC
            """,
            (job_id, FrameStatus.DONE.value, FrameStatus.CANCELLED.value),
        )
        changed_frames: list[int] = []
        for frame in frames:
            frame_number = int(frame["frameNumber"])
            if self._frame_output_confirmed(job, frame):
                self._mark_frame_done(job_id, frame_number, worker_id=frame.get("workerId"))
                changed_frames.append(frame_number)
        if changed_frames:
            self.logger.info(
                f"Confirmed {len(changed_frames)} frame(s) by filesystem for job '{job_id}'.",
                category="job",
                entity_type="job",
                entity_id=job_id,
                details={"frames": changed_frames[:20]},
            )

    def _mark_frame_done(
        self,
        job_id: str,
        frame_number: int,
        worker_id: str | None,
        *,
        elapsed_seconds: float | None = None,
    ) -> None:
        frame = self.database.fetch_one(
            """
            SELECT startedAt, finishedAt
            FROM job_frames
            WHERE jobId = ? AND frameNumber = ?
            """,
            (job_id, frame_number),
        ) or {}
        worker = self.worker_registry.get_worker(worker_id) if worker_id else None
        if worker_id:
            self._record_job_worker_success(
                job_id,
                worker_id,
                worker.get("renderDevicePreference") if worker else None,
                elapsed_seconds=(
                    self._normalize_elapsed_seconds(elapsed_seconds)
                    if elapsed_seconds is not None
                    else self._resolve_frame_elapsed_seconds(frame)
                ),
            )
        now = utc_now_iso()
        self.database.execute(
            """
            UPDATE job_frames
            SET status = ?, finishedAt = ?, leaseExpiresAt = NULL, assignmentId = NULL, leasedAt = NULL,
                workerId = NULL, errorMessage = NULL
            WHERE jobId = ? AND frameNumber = ?
            """,
            (FrameStatus.DONE.value, now, job_id, frame_number),
        )
        self._refresh_job_progress(job_id)
        if worker_id:
            if worker and worker.get("currentJobId") == job_id and worker.get("currentFrame") == frame_number:
                self.worker_registry.clear_worker_assignment(worker_id, status=WorkerStatus.IDLE.value)

    def _apply_worker_render_report(self, worker_id: str, last_job: dict[str, Any]) -> str:
        frame = self._find_frame_for_worker_report(worker_id, last_job)
        if not frame:
            return "worker_updated"

        result = str(last_job.get("result") or "").strip().lower()
        if not result:
            return "worker_updated"

        if result == "done":
            job = self._get_job_row(frame["jobId"])
            output_verified = normalize_bool(last_job.get("outputVerified"), False)
            if self._frame_output_confirmed(job, frame, output_verified=output_verified):
                self._mark_frame_done(
                    frame["jobId"],
                    int(frame["frameNumber"]),
                    worker_id=frame.get("workerId"),
                    elapsed_seconds=self._normalize_elapsed_seconds(last_job.get("elapsedSeconds")),
                )
                return "frame_completed"
            self._queue_frame_retry(
                frame,
                "Worker reported success, but the expected output file is still not visible.",
                cancel_worker=False,
            )
            return "frame_requeued_missing_output"

        if result == "cancelled":
            self._queue_frame_retry(
                frame,
                "Worker reported that the assignment was cancelled before completion.",
                cancel_worker=False,
            )
            return "frame_requeued_cancelled"

        reason = (
            last_job.get("error")
            or last_job.get("message")
            or "Worker reported a render failure."
        )
        self._queue_frame_retry(
            frame,
            str(reason),
            cancel_worker=False,
        )
        return "frame_requeued_error"

    def _find_frame_for_worker_report(
        self,
        worker_id: str,
        last_job: dict[str, Any],
    ) -> dict[str, Any] | None:
        job_id = str(last_job.get("jobId") or "").strip()
        if not job_id:
            return None
        rows = self.database.fetch_all(
            """
            SELECT jobId, frameNumber, workerId, assignmentId, attemptCount, status,
                   baselineExists, baselineSize, baselineMtimeNs
            FROM job_frames
            WHERE workerId = ? AND jobId = ? AND status IN (?, ?)
            ORDER BY frameNumber ASC
            """,
            (
                worker_id,
                job_id,
                FrameStatus.LEASED.value,
                FrameStatus.RENDERING.value,
            ),
        )
        for row in rows:
            if self._worker_report_matches_frame(row, last_job):
                return row
        return None

    def _reconcile_worker_reported_results(self, job_id: str | None = None) -> None:
        query = """
            SELECT jf.jobId, jf.frameNumber, jf.workerId, jf.assignmentId, jf.attemptCount,
                   jf.baselineExists, jf.baselineSize, jf.baselineMtimeNs,
                   w.lastJob, w.status AS workerStatus
            FROM job_frames jf
            JOIN workers w ON w.workerId = jf.workerId
            WHERE jf.status IN (?, ?)
        """
        params: list[Any] = [FrameStatus.LEASED.value, FrameStatus.RENDERING.value]
        if job_id:
            query += " AND jf.jobId = ?"
            params.append(job_id)

        rows = self.database.fetch_all(query, tuple(params))
        for row in rows:
            last_job = row.get("lastJob")
            if isinstance(last_job, str) and last_job.strip():
                try:
                    last_job = json.loads(last_job)
                except ValueError:
                    continue
            if not isinstance(last_job, dict):
                continue
            if not self._worker_report_matches_frame(row, last_job):
                continue

            result = str(last_job.get("result") or "").strip().lower()
            if not result:
                continue

            if result == "done":
                job = self._get_job_row(row["jobId"])
                output_verified = normalize_bool(last_job.get("outputVerified"), False)
                if self._frame_output_confirmed(job, row, output_verified=output_verified):
                    self._mark_frame_done(
                        row["jobId"],
                        int(row["frameNumber"]),
                        worker_id=row.get("workerId"),
                        elapsed_seconds=self._normalize_elapsed_seconds(last_job.get("elapsedSeconds")),
                    )
                    continue
                self._queue_frame_retry(
                    row,
                    "Worker reported success, but the expected output file is still not visible.",
                    cancel_worker=False,
                )
                continue

            if result == "cancelled":
                self._queue_frame_retry(
                    row,
                    "Worker reported that the assignment was cancelled before completion.",
                    cancel_worker=False,
                )
                continue

            reason = (
                last_job.get("error")
                or last_job.get("message")
                or "Worker reported a render failure."
            )
            self._queue_frame_retry(
                row,
                str(reason),
                cancel_worker=False,
            )

    @staticmethod
    def _worker_report_matches_frame(frame: dict[str, Any], last_job: dict[str, Any]) -> bool:
        assignment_id = frame.get("assignmentId")
        reported_assignment_id = last_job.get("assignmentId")
        if assignment_id and reported_assignment_id:
            return str(assignment_id) == str(reported_assignment_id)
        if assignment_id and not reported_assignment_id:
            return False
        if str(last_job.get("jobId") or "") != str(frame.get("jobId") or ""):
            return False
        try:
            reported_frame = int(last_job.get("startFrame"))
        except (TypeError, ValueError):
            return False
        return reported_frame == int(frame["frameNumber"])

    def _frame_output_confirmed(
        self,
        job: dict[str, Any],
        frame: dict[str, Any],
        *,
        output_verified: bool = False,
    ) -> bool:
        if normalize_bool(job.get("ignoreRenderedFrames"), True):
            return output_verified or self.frame_scanner.frame_exists(
                job, int(frame["frameNumber"])
            )
        return self.frame_scanner.output_is_fresh(job, frame)

    def _queue_frame_retry(
        self,
        frame: dict[str, Any],
        reason: str,
        *,
        cancel_worker: bool,
    ) -> None:
        job_id = str(frame["jobId"])
        frame_number = int(frame["frameNumber"])
        worker_id = frame.get("workerId")
        assignment_id = frame.get("assignmentId")
        attempt_count = int(frame.get("attemptCount") or 0)
        failed_worker = self.worker_registry.get_worker(str(worker_id)) if worker_id else None
        failed_render_device = (
            str(failed_worker.get("renderDevicePreference") or "").strip()
            if failed_worker
            else ""
        )
        next_worker_status = self._resolve_post_failure_worker_status(
            failed_worker,
            job_id=job_id,
            frame_number=frame_number,
            assignment_id=assignment_id,
        )
        cancel_response: dict[str, Any] | None = None

        if cancel_worker:
            cancel_response = self._cancel_worker_assignment(worker_id, assignment_id=assignment_id, job_id=job_id)

        self._record_job_worker_failure(job_id, worker_id, failed_render_device, reason)

        if attempt_count >= int(self.settings.max_frame_attempts):
            self._mark_frame_error_and_block_job(frame, reason)
            return

        failed_worker_name = (
            str(failed_worker.get("name") or worker_id)
            if failed_worker or worker_id
            else None
        )
        self.database.execute(
            """
            UPDATE job_frames
            SET status = ?, workerId = NULL, assignmentId = NULL, leasedAt = NULL, leaseExpiresAt = NULL,
                errorMessage = ?, lastFailedWorkerId = ?, lastFailedAt = ?, lastFailedRenderDevicePreference = ?, lastFailureReason = ?
            WHERE jobId = ? AND frameNumber = ?
            """,
            (
                FrameStatus.PENDING.value,
                reason,
                worker_id,
                utc_now_iso(),
                failed_render_device or None,
                reason,
                job_id,
                frame_number,
            ),
        )
        if worker_id:
            self.worker_registry.update_worker(
                str(worker_id),
                {
                    "status": next_worker_status,
                    "error": reason,
                },
            )
        self._clear_worker_assignment_if_matches(
            worker_id,
            job_id=job_id,
            frame_number=frame_number,
            assignment_id=assignment_id,
            status=next_worker_status,
        )
        self.logger.warning(
            f"Frame {frame_number} of job '{job_id}' returned to queue after a worker failure.",
            category="job",
            entity_type="job",
            entity_id=job_id,
            details={
                "frameNumber": frame_number,
                "workerId": worker_id,
                "workerName": failed_worker_name,
                "attemptCount": attempt_count,
                "reason": reason,
            },
        )
        if cancel_response:
            self._recover_cancelled_session_assignments(
                worker_id=worker_id,
                job_id=job_id,
                cancelled_assignment_ids=cancel_response.get("cancelledAssignmentIds"),
                excluded_frame_number=frame_number,
            )

    def _mark_frame_error_and_block_job(self, frame: dict[str, Any], reason: str) -> None:
        job_id = str(frame["jobId"])
        frame_number = int(frame["frameNumber"])
        worker_id = frame.get("workerId")
        assignment_id = frame.get("assignmentId")
        failed_at = utc_now_iso()
        failed_worker = self.worker_registry.get_worker(str(worker_id)) if worker_id else None
        failed_render_device = (
            str(failed_worker.get("renderDevicePreference") or "").strip()
            if failed_worker
            else ""
        )
        self.database.execute(
            """
            UPDATE job_frames
            SET status = ?, finishedAt = ?, workerId = NULL, assignmentId = NULL,
                leasedAt = NULL, leaseExpiresAt = NULL, errorMessage = ?,
                lastFailedWorkerId = ?, lastFailedAt = ?, lastFailedRenderDevicePreference = ?, lastFailureReason = ?
            WHERE jobId = ? AND frameNumber = ?
            """,
            (
                FrameStatus.ERROR.value,
                failed_at,
                reason,
                worker_id,
                failed_at,
                failed_render_device or None,
                reason,
                job_id,
                frame_number,
            ),
        )
        self._clear_worker_assignment_if_matches(
            worker_id,
            job_id=job_id,
            frame_number=frame_number,
            assignment_id=assignment_id,
            status=WorkerStatus.IDLE.value,
        )
        self.database.execute(
            "UPDATE jobs SET lastError = ? WHERE jobId = ?",
            (reason, job_id),
        )

        if self._job_has_remaining_work(job_id):
            self.logger.error(
                f"Frame {frame_number} of job '{job_id}' reached the retry limit and was marked as error; remaining frames will continue.",
                category="job",
                entity_type="job",
                entity_id=job_id,
                details={"frameNumber": frame_number, "reason": reason},
            )
            return

        self.database.execute("UPDATE jobs SET lastError = ? WHERE jobId = ?", (reason, job_id))
        self.logger.error(
            f"Job '{job_id}' exhausted all remaining frame attempts and will finish with errors.",
            category="job",
            entity_type="job",
            entity_id=job_id,
            details={"frameNumber": frame_number, "reason": reason},
        )

    def _clear_worker_assignment_if_matches(
        self,
        worker_id: str | None,
        *,
        job_id: str,
        frame_number: int,
        assignment_id: str | None,
        status: str,
    ) -> None:
        if not worker_id:
            return
        worker = self.worker_registry.get_worker(worker_id)
        if not worker:
            return
        current_assignment_id = worker.get("currentAssignmentId")
        if assignment_id and current_assignment_id and current_assignment_id != assignment_id:
            return
        if worker.get("currentJobId") not in {None, job_id}:
            return
        if worker.get("currentFrame") not in {None, frame_number}:
            return
        if status in {
            WorkerStatus.SESSION_OPEN.value,
            WorkerStatus.QUEUED.value,
            WorkerStatus.DRAINING.value,
            WorkerStatus.CLOSING.value,
        }:
            self.worker_registry.update_worker(
                worker_id,
                {
                    "status": status,
                    "currentBlockId": None,
                    "currentFrame": None,
                    "currentAssignmentId": None,
                    "progress": 0.0,
                },
            )
            return
        self.worker_registry.clear_worker_assignment(worker_id, status=status)

    @staticmethod
    def _resolve_post_failure_worker_status(
        worker: dict[str, Any] | None,
        *,
        job_id: str,
        frame_number: int,
        assignment_id: str | None,
    ) -> str:
        if not worker:
            return WorkerStatus.ERROR.value

        current_status = str(worker.get("status") or "").strip().lower()
        current_job_id = worker.get("currentJobId")
        current_frame = worker.get("currentFrame")
        current_assignment_id = worker.get("currentAssignmentId")

        if current_status in {
            WorkerStatus.SESSION_OPEN.value,
            WorkerStatus.QUEUED.value,
            WorkerStatus.DRAINING.value,
            WorkerStatus.CLOSING.value,
        }:
            return current_status

        assignment_matches = (
            not assignment_id
            or not current_assignment_id
            or str(current_assignment_id) == str(assignment_id)
        )
        job_matches = current_job_id in {None, job_id}
        frame_matches = current_frame in {None, frame_number}

        if current_status in {WorkerStatus.RENDERING.value, WorkerStatus.BUSY.value}:
            if assignment_matches and job_matches and frame_matches:
                return WorkerStatus.IDLE.value
            return current_status

        if current_status in ONLINE_WORKER_STATUSES and assignment_matches and job_matches and frame_matches:
            return WorkerStatus.IDLE.value

        return WorkerStatus.ERROR.value

    def _cancel_worker_assignment(
        self,
        worker_id: str | None,
        *,
        assignment_id: str | None,
        job_id: str,
    ) -> dict[str, Any] | None:
        if not worker_id:
            return None
        worker = self.worker_registry.get_worker(worker_id)
        if not worker:
            return None
        payload: dict[str, Any] = {"jobId": job_id}
        if assignment_id:
            payload["assignmentId"] = assignment_id
        try:
            return self.worker_client.cancel(f"http://{worker['ip']}:{worker['port']}", payload)
        except Exception:
            return None

    def _recover_cancelled_session_assignments(
        self,
        *,
        worker_id: str | None,
        job_id: str,
        cancelled_assignment_ids: Any,
        excluded_frame_number: int,
    ) -> None:
        if not worker_id or not isinstance(cancelled_assignment_ids, list):
            return
        normalized_assignment_ids = [
            str(item).strip()
            for item in cancelled_assignment_ids
            if str(item).strip()
        ]
        if not normalized_assignment_ids:
            return
        placeholders = ", ".join("?" for _ in normalized_assignment_ids)
        rows = self.database.fetch_all(
            f"""
            SELECT jobId, frameNumber, workerId, assignmentId, attemptCount,
                   baselineExists, baselineSize, baselineMtimeNs
            FROM job_frames
            WHERE workerId = ? AND jobId = ? AND assignmentId IN ({placeholders})
              AND frameNumber != ? AND status IN (?, ?)
            ORDER BY frameNumber ASC
            """,
            (
                worker_id,
                job_id,
                *normalized_assignment_ids,
                excluded_frame_number,
                FrameStatus.LEASED.value,
                FrameStatus.RENDERING.value,
            ),
        )
        for frame in rows:
            self._queue_frame_retry(
                frame,
                "Worker session was terminated and the queued assignment was returned to the coordinator queue.",
                cancel_worker=False,
            )

    def _sync_busy_worker_from_response(self, worker: dict[str, Any], response: dict[str, Any]) -> None:
        transient_rejection = self._is_transient_worker_rejection(response)
        self.worker_registry.update_worker(
            worker["workerId"],
            {
                "status": self._resolve_worker_status_from_response(
                    response,
                    fallback=WorkerStatus.BUSY.value,
                ),
                "currentJobId": response.get("currentJobId"),
                "currentBlockId": response.get("currentBlockId"),
                "currentFrame": response.get("currentFrame"),
                "currentAssignmentId": response.get("currentAssignmentId") or response.get("assignmentId"),
                "persistentSessionEnabled": response.get("persistentSessionEnabled"),
                "sessionState": response.get("sessionState"),
                "sessionQueueDepth": response.get("queueDepth"),
                "sessionDrainRequested": response.get("drainRequested"),
                "error": None if transient_rejection else (response.get("message") or response.get("error")),
            },
        )

    @staticmethod
    def _resolve_worker_status_from_response(
        response: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        session_state = str(response.get("sessionState") or "").strip().lower()
        if session_state in {"queued", "session_open", "rendering", "draining", "closing", "error"}:
            return session_state
        status = str(response.get("status") or "").strip().lower()
        if status:
            return status
        return str(fallback)

    @staticmethod
    def _is_transient_worker_rejection(response: dict[str, Any] | None) -> bool:
        if not isinstance(response, dict):
            return False
        error_code = str(response.get("error") or "").strip().lower()
        return error_code in {
            "busy",
            "session_queue_full",
            "active_session_incompatible",
            "worker_shutting_down",
        }

    def _expire_old_leases(self) -> None:
        expired = self.database.fetch_all(
            """
            SELECT jobId, frameNumber, workerId, assignmentId, attemptCount
            FROM job_frames
            WHERE status IN (?, ?) AND leaseExpiresAt IS NOT NULL AND leaseExpiresAt < ?
            ORDER BY leaseExpiresAt ASC
            """,
            (FrameStatus.LEASED.value, FrameStatus.RENDERING.value, utc_now_iso()),
        )
        for frame in expired:
            job = self._get_job_row(frame["jobId"])
            if self._frame_output_confirmed(job, frame):
                self._mark_frame_done(frame["jobId"], int(frame["frameNumber"]), worker_id=frame.get("workerId"))
                continue
            self._queue_frame_retry(
                frame,
                "Lease expired; frame returned to queue.",
                cancel_worker=True,
            )
            self.logger.warning(
                f"Lease expired for job '{frame['jobId']}' frame {frame['frameNumber']}.",
                category="job",
                entity_type="job",
                entity_id=frame["jobId"],
                details={"frameNumber": frame["frameNumber"], "workerId": frame.get("workerId")},
            )

    def _recover_offline_workers(self) -> None:
        workers = self.database.fetch_all(
            """
            SELECT DISTINCT w.workerId, w.status
            FROM workers w
            JOIN job_frames jf ON jf.workerId = w.workerId
            WHERE jf.status IN (?, ?) AND w.status IN (?, ?)
            """,
            (
                FrameStatus.LEASED.value,
                FrameStatus.RENDERING.value,
                WorkerStatus.OFFLINE.value,
                WorkerStatus.ERROR.value,
            ),
        )
        for worker in workers:
            self._recover_frames_from_worker(worker["workerId"], force=False)

    def _recover_frames_from_worker(self, worker_id: str, *, force: bool) -> None:
        worker = self.worker_registry.get_worker(worker_id)
        if not worker:
            return
        if not force and worker["status"] not in {WorkerStatus.OFFLINE.value, WorkerStatus.ERROR.value}:
            return
        frames = self.database.fetch_all(
            """
            SELECT jobId, frameNumber, workerId, assignmentId, attemptCount,
                   baselineExists, baselineSize, baselineMtimeNs
            FROM job_frames
            WHERE workerId = ? AND status IN (?, ?)
            ORDER BY jobId ASC, frameNumber ASC
            """,
            (
                worker_id,
                FrameStatus.LEASED.value,
                FrameStatus.RENDERING.value,
            ),
        )
        if not frames:
            return

        recovered_frames: list[int] = []
        for frame in frames:
            job = self._get_job_row(frame["jobId"])
            if not force and not normalize_bool(job["redistributeOfflineBlocks"], True):
                continue
            frame_number = int(frame["frameNumber"])
            if self._frame_output_confirmed(job, frame):
                self._mark_frame_done(frame["jobId"], frame_number, worker_id=worker_id)
                continue
            self._queue_frame_retry(
                frame,
                f"Worker '{worker_id}' became unavailable; frame returned to queue.",
                cancel_worker=False,
            )
            recovered_frames.append(frame_number)
        self.worker_registry.clear_worker_assignment(worker_id, status=WorkerStatus.OFFLINE.value if not force else WorkerStatus.UNKNOWN.value)
        self.logger.warning(
            f"Recovered frames from worker '{worker_id}'.",
            category="worker",
            entity_type="worker",
            entity_id=worker_id,
            details={"recoveredFrames": recovered_frames},
        )

    def _finalize_job_if_done(self, job_id: str) -> None:
        job = self._get_job_row(job_id)
        counts = self._frame_counts(job_id)
        if counts["done"] == int(job["totalFrames"]):
            self.database.execute(
                """
                UPDATE jobs
                SET status = ?, finishedAt = ?, completedFrames = ?, lastError = NULL
                WHERE jobId = ?
                """,
                (JobStatus.COMPLETED.value, utc_now_iso(), counts["done"], job_id),
            )
            self._notify_workers_job_drained(job_id)
            with self.database.transaction() as connection:
                self._recompute_queue_positions(connection)
            self.logger.info(
                f"Job '{job_id}' completed.",
                category="job",
                entity_type="job",
                entity_id=job_id,
            )
            return

        if counts["error"] > 0 and counts["pending"] == 0 and counts["leased"] == 0 and counts["rendering"] == 0:
            self.database.execute(
                """
                UPDATE jobs
                SET status = ?, finishedAt = ?, completedFrames = ?
                WHERE jobId = ?
                """,
                (JobStatus.ERROR.value, utc_now_iso(), counts["done"], job_id),
            )
            self._notify_workers_job_drained(job_id)
            with self.database.transaction() as connection:
                self._recompute_queue_positions(connection)
            self.logger.error(
                f"Job '{job_id}' finished with frame errors.",
                category="job",
                entity_type="job",
                entity_id=job_id,
                details={"errorFrames": counts["error"], "completedFrames": counts["done"]},
            )

    def _refresh_job_progress(self, job_id: str) -> None:
        counts = self._frame_counts(job_id)
        self.database.execute(
            "UPDATE jobs SET completedFrames = ? WHERE jobId = ?",
            (counts["done"], job_id),
        )

    def _clear_worker_assignments_for_job(self, job_id: str) -> None:
        workers = self.database.fetch_all(
            "SELECT workerId FROM workers WHERE currentJobId = ?",
            (job_id,),
        )
        for worker in workers:
            self.worker_registry.clear_worker_assignment(worker["workerId"], status=WorkerStatus.IDLE.value)

    def _notify_workers_job_drained(self, job_id: str) -> None:
        workers = self.database.fetch_all(
            """
            SELECT workerId, ip, port, persistentSessionEnabled
            FROM workers
            WHERE currentJobId = ?
            """,
            (job_id,),
        )
        for worker in workers:
            if not normalize_bool(worker.get("persistentSessionEnabled"), False):
                self.worker_registry.clear_worker_assignment(worker["workerId"], status=WorkerStatus.IDLE.value)
                continue
            self._request_worker_job_drain(worker, job_id=job_id)

    def _drain_finished_job_sessions(self) -> None:
        """Retry a missed completion drain using the worker's freshly polled state.

        A completion callback can clear the coordinator assignment before the
        terminal job notification finds its worker. A later poll restores the
        actual session owner, so do not wait for Blender's idle timeout.
        """
        workers = self.database.fetch_all(
            """SELECT workers.* FROM workers JOIN jobs ON jobs.jobId = workers.currentJobId
               WHERE jobs.status IN (?, ?, ?) AND workers.persistentSessionEnabled = 1""",
            (JobStatus.COMPLETED.value, JobStatus.CANCELLED.value, JobStatus.ERROR.value),
        )
        for worker in workers:
            state = str(worker.get("sessionState") or worker.get("status") or "").lower()
            if worker.get("status") == WorkerStatus.OFFLINE.value:
                continue
            if state in {"draining", "closing"} or normalize_bool(worker.get("sessionDrainRequested"), False):
                continue
            if state in {"session_open", "queued", "rendering", "error"}:
                self._request_worker_job_drain(worker, job_id=str(worker["currentJobId"]))

    def _drain_workers_without_future_job_frames(
        self,
        job_id: str,
        *,
        planned_worker_ids: set[str],
    ) -> None:
        workers = [
            worker
            for worker in self.worker_registry.list_workers()
            if normalize_bool(worker.get("persistentSessionEnabled"), False)
            and str(worker.get("currentJobId") or "").strip() == str(job_id)
        ]
        for worker in workers:
            worker_id = str(worker.get("workerId") or "").strip()
            if not worker_id or worker_id in planned_worker_ids:
                continue
            if normalize_bool(worker.get("sessionDrainRequested"), False):
                continue
            session_state = str(worker.get("sessionState") or worker.get("status") or "").strip().lower()
            if session_state in {"draining", "closing"}:
                continue
            self._request_worker_job_drain(worker, job_id=job_id)

    def _request_worker_job_drain(self, worker: dict[str, Any], *, job_id: str) -> None:
        worker_id = str(worker.get("workerId") or "").strip()
        if not worker_id:
            return
        try:
            response = self.worker_client.job_drain(
                f"http://{worker['ip']}:{worker['port']}",
                {
                    "jobId": job_id,
                    "coordinatorId": self.settings.coordinator_id,
                },
            )
            if response.get("accepted") is False:
                return
            self.worker_registry.update_worker(
                worker_id,
                {
                    "status": response.get("sessionState") or WorkerStatus.DRAINING.value,
                    "sessionState": response.get("sessionState"),
                    "sessionQueueDepth": response.get("queueDepth"),
                    "sessionDrainRequested": response.get("drainRequested"),
                    "error": None,
                },
            )
            self.logger.info(
                f"Worker '{worker_id}' accepted job drain for job '{job_id}'.",
                category="worker",
                entity_type="worker",
                entity_id=worker_id,
                details={"jobId": job_id},
            )
        except Exception as exc:
            self.logger.warning(
                f"Failed to request early job drain for worker '{worker_id}' on job '{job_id}': {exc}",
                category="worker",
                entity_type="worker",
                entity_id=worker_id,
                details={"jobId": job_id},
            )

    def _cancel_active_worker_assignments(self, job_id: str, *, requeue_frames: bool) -> None:
        workers = self.database.fetch_all(
            """
            SELECT workerId, ip, port, currentAssignmentId
            FROM workers
            WHERE currentJobId = ?
            """,
            (job_id,),
        )
        for worker in workers:
            try:
                payload: dict[str, Any] = {"jobId": job_id}
                if worker.get("currentAssignmentId"):
                    payload["assignmentId"] = worker["currentAssignmentId"]
                self.worker_client.cancel(f"http://{worker['ip']}:{worker['port']}", payload)
            except Exception:
                pass
            self.worker_registry.clear_worker_assignment(worker["workerId"], status=WorkerStatus.IDLE.value)
        if requeue_frames:
            self.database.execute(
                """
                UPDATE job_frames
                SET status = ?, workerId = NULL, assignmentId = NULL, leasedAt = NULL, leaseExpiresAt = NULL,
                    errorMessage = 'Job paused; frame returned to queue.'
                WHERE jobId = ? AND status IN (?, ?)
                """,
                (
                    FrameStatus.PENDING.value,
                    job_id,
                    FrameStatus.LEASED.value,
                    FrameStatus.RENDERING.value,
                ),
            )

    def _job_snapshot(self, row: dict[str, Any]) -> dict[str, Any]:
        counts = self._frame_counts(row["jobId"])
        total_frames = int(row["totalFrames"])
        progress_percent = round((counts["done"] / total_frames) * 100, 2) if total_frames else 0.0
        snapshot = dict(row)
        snapshot["gpuOnlyWorkers"] = normalize_bool(snapshot.get("gpuOnlyWorkers"), False)
        snapshot["frameStats"] = counts
        snapshot["progressPercent"] = progress_percent
        return snapshot

    def _job_worker_stats(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
            SELECT jws.workerId, w.name AS workerName, jws.renderDevicePreference, jws.successCount, jws.failureCount,
                   jws.timedSuccessCount, jws.totalSuccessRenderSeconds, jws.lastSuccessRenderSeconds,
                   jws.consecutiveFailureCount, jws.lastFailureReason, jws.lastFailureAt, jws.lastSuccessAt
            FROM job_worker_stats jws
            LEFT JOIN workers w ON w.workerId = jws.workerId
            WHERE jws.jobId = ?
            ORDER BY jws.successCount DESC, jws.failureCount ASC, jws.workerId ASC, jws.renderDevicePreference ASC
            """,
            (job_id,),
        )
        for row in rows:
            timed_success_count = int(row.get("timedSuccessCount") or 0)
            total_success_render_seconds = float(row.get("totalSuccessRenderSeconds") or 0.0)
            row["averageFrameSeconds"] = (
                round(total_success_render_seconds / float(timed_success_count), 2)
                if timed_success_count > 0
                else None
            )
            row["totalSuccessRenderSeconds"] = round(total_success_render_seconds, 2)
            if row.get("lastSuccessRenderSeconds") is not None:
                row["lastSuccessRenderSeconds"] = round(float(row["lastSuccessRenderSeconds"]), 2)
        return rows

    def _job_report_snapshot(self, job_id: str) -> dict[str, Any]:
        snapshot = self.get_job(job_id)
        snapshot["events"] = self.database.fetch_all(
            """
            SELECT level, category, message, details, createdAt
            FROM events
            WHERE entityId = ?
            ORDER BY id DESC
            LIMIT 40
            """,
            (job_id,),
        )
        return snapshot

    def _job_report_lines(self, report: dict[str, Any]) -> list[str]:
        frame_stats = report.get("frameStats") or {}
        frames = list(report.get("frames") or [])
        worker_stats = list(report.get("workerStats") or [])
        events = list(report.get("events") or [])

        problematic_frames = [
            frame
            for frame in frames
            if int(frame.get("attemptCount") or 0) > 1
            or str(frame.get("status") or "").lower() == FrameStatus.ERROR.value
            or frame.get("lastFailureReason")
            or frame.get("errorMessage")
        ]
        problematic_frames.sort(
            key=lambda frame: (
                0 if str(frame.get("status") or "").lower() == FrameStatus.ERROR.value else 1,
                -int(frame.get("attemptCount") or 0),
                int(frame.get("frameNumber") or 0),
            )
        )

        lines = [
            f"Job name: {report.get('jobName')}",
            f"Job id: {report.get('jobId')}",
            f"Status: {report.get('status')}",
            f"Blend file: {report.get('blendFile')}",
            f"Output folder: {report.get('outputFolder')}",
            f"Frames: {report.get('startFrame')}-{report.get('endFrame')} ({report.get('totalFrames')} total)",
            f"Worker filter: {'GPU only' if normalize_bool(report.get('gpuOnlyWorkers'), False) else 'Any worker'}",
            f"Progress: {report.get('progressPercent', 0)}%",
            f"Created at: {report.get('createdAt') or '-'}",
            f"Started at: {report.get('startedAt') or '-'}",
            f"Finished at: {report.get('finishedAt') or '-'}",
            f"Last error: {report.get('lastError') or 'none'}",
            "",
            "Frame summary:",
            f"  done={frame_stats.get('done', 0)} pending={frame_stats.get('pending', 0)} "
            f"leased={frame_stats.get('leased', 0)} rendering={frame_stats.get('rendering', 0)} "
            f"error={frame_stats.get('error', 0)} cancelled={frame_stats.get('cancelled', 0)}",
            "",
            "Machine performance for this job:",
        ]

        if worker_stats:
            for item in worker_stats:
                worker_name = item.get("workerName") or item.get("workerId")
                timing_text = "timing=insufficient"
                if int(item.get("timedSuccessCount") or 0) > 0:
                    timing_text = (
                        f"avg={item.get('averageFrameSeconds', 0)}s "
                        f"last={item.get('lastSuccessRenderSeconds', 0)}s "
                        f"samples={item.get('timedSuccessCount', 0)}"
                    )
                lines.append(
                    "  - "
                    f"{worker_name} [{item.get('workerId')}] "
                    f"device={item.get('renderDevicePreference') or 'AUTO'} "
                    f"success={item.get('successCount', 0)} "
                    f"failure={item.get('failureCount', 0)} "
                    f"consecutive={item.get('consecutiveFailureCount', 0)} "
                    f"lastFailure={item.get('lastFailureReason') or 'none'} "
                    f"{timing_text}"
                )
        else:
            lines.append("  - No worker history recorded for this job.")

        lines.extend(["", "Frames with retries or errors:"])
        if problematic_frames:
            for frame in problematic_frames[:30]:
                failed_worker_label = frame.get("lastFailedWorkerName") or frame.get("lastFailedWorkerId") or "-"
                active_worker_label = frame.get("workerName") or frame.get("workerId") or "-"
                lines.append(
                    "  - "
                    f"frame={frame.get('frameNumber')} "
                    f"status={frame.get('status')} "
                    f"attempts={frame.get('attemptCount', 0)} "
                    f"activeWorker={active_worker_label} "
                    f"lastFailedWorker={failed_worker_label} "
                    f"device={frame.get('lastFailedRenderDevicePreference') or '-'} "
                    f"reason={frame.get('errorMessage') or frame.get('lastFailureReason') or '-'}"
                )
        else:
            lines.append("  - No frame retries or errors were recorded.")

        lines.extend(["", "Recent job events:"])
        if events:
            for event in events[:20]:
                lines.append(
                    "  - "
                    f"{event.get('createdAt')} "
                    f"[{event.get('level')}] "
                    f"{event.get('message')}"
                )
        else:
            lines.append("  - No structured events were recorded for this job.")
        return lines

    @staticmethod
    def _job_report_filename(report: dict[str, Any]) -> str:
        raw_name = str(report.get("jobName") or report.get("jobId") or "job_report")
        safe_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in raw_name).strip("_")
        if not safe_name:
            safe_name = "job_report"
        return f"{safe_name}_{report.get('jobId')}.pdf"

    def _state_job_snapshot(self, row: dict[str, Any] | None, *, include_blocks: bool = False) -> dict[str, Any] | None:
        if not row:
            return None
        snapshot = dict(row)
        job_id = str(snapshot.get("jobId") or snapshot.get("id") or "")
        counts = snapshot.get("frameStats")
        if not isinstance(counts, dict) and job_id:
            counts = self._frame_counts(job_id)
        counts = counts or {
            "pending": 0,
            "leased": 0,
            "rendering": 0,
            "done": 0,
            "error": 0,
            "cancelled": 0,
        }
        start_frame = int(snapshot.get("startFrame") or 0)
        end_frame = int(snapshot.get("endFrame") or 0)
        total_frames = int(snapshot.get("totalFrames") or max(0, end_frame - start_frame + 1))
        completed_frames = int(snapshot.get("completedFrames") or counts.get("done") or 0)
        rendering_frames = int(counts.get("rendering") or 0) + int(counts.get("leased") or 0)
        overall_percent = self._normalize_progress_percent(snapshot.get("progressPercent"))
        if overall_percent is None:
            overall_percent = self._normalize_progress_percent(snapshot.get("overallPercent"))
        if (overall_percent is None or overall_percent == 0.0) and total_frames and completed_frames > 0:
            overall_percent = (float(completed_frames) / total_frames) * 100.0
        blocks = self._job_blocks(job_id) if include_blocks and job_id else list(snapshot.get("blocks") or [])
        resolved_job_name = snapshot.get("jobName") or job_id
        resolved_blend_file = snapshot.get("blendFile")

        snapshot.update(
            {
                "jobId": job_id,
                "jobName": resolved_job_name,
                "job_name": resolved_job_name,
                "blendFile": resolved_blend_file,
                "blend_file": resolved_blend_file,
                "gpuOnlyWorkers": normalize_bool(snapshot.get("gpuOnlyWorkers"), False),
                "startFrame": start_frame,
                "start_frame": start_frame,
                "endFrame": end_frame,
                "end_frame": end_frame,
                "status": snapshot.get("status", JobStatus.QUEUED.value),
                "blocks": blocks,
                "frameStats": counts,
                "progressPercent": round(float(overall_percent or 0.0), 2),
                "overallPercent": round(float(overall_percent or 0.0), 2),
                "overall_percent": round(float(overall_percent or 0.0), 2),
                "completedFramesCount": completed_frames,
                "completedFrameCount": completed_frames,
                "completed_frames": completed_frames,
                "totalFrames": total_frames,
                "total_frames": total_frames,
                "pending_frames": int(counts.get("pending") or 0),
                "rendering_frames": rendering_frames,
                "error_frames": int(counts.get("error") or 0),
            }
        )
        return snapshot

    def _job_blocks(self, job_id: str) -> list[dict[str, Any]]:
        frames = self.database.fetch_all(
            """
            SELECT frameNumber, status, workerId
            FROM job_frames
            WHERE jobId = ?
            ORDER BY frameNumber
            """,
            (job_id,),
        )
        return [self._state_block_snapshot(frame) for frame in frames]

    def _state_block_snapshot(self, frame: dict[str, Any]) -> dict[str, Any]:
        frame_number = int(frame.get("frameNumber") or 0)
        raw_status = str(frame.get("status") or FrameStatus.PENDING.value)
        normalized_status = "assigned" if raw_status == FrameStatus.LEASED.value else raw_status
        block_id = f"frame_{frame_number:06d}"
        worker_id = frame.get("workerId")
        return {
            "blockId": block_id,
            "block_id": block_id,
            "workerId": worker_id,
            "worker_id": worker_id,
            "status": normalized_status,
            "rawStatus": raw_status,
            "startFrame": frame_number,
            "start_frame": frame_number,
            "endFrame": frame_number,
            "end_frame": frame_number,
        }

    def _state_metrics(
        self,
        active_job: dict[str, Any] | None,
        workers: list[dict[str, Any]],
        queue: list[dict[str, Any]],
    ) -> dict[str, Any]:
        active_job = active_job or {}
        total_frames = int(active_job.get("totalFrames") or active_job.get("total_frames") or 0)
        completed_frames = int(active_job.get("completedFramesCount") or active_job.get("completed_frames") or 0)
        pending_frames = int(active_job.get("pending_frames") or 0)
        rendering_frames = int(active_job.get("rendering_frames") or 0)
        error_frames = int(active_job.get("error_frames") or 0)
        overall_percent = self._normalize_progress_percent(
            self._first_defined(active_job.get("overall_percent"), active_job.get("overallPercent"))
        )
        if (overall_percent is None or overall_percent == 0.0) and total_frames and completed_frames > 0:
            overall_percent = (float(completed_frames) / total_frames) * 100.0
        elapsed_seconds = self._elapsed_seconds(active_job.get("startedAt"))
        eta_seconds = self._estimate_eta_seconds(elapsed_seconds=elapsed_seconds, overall_percent=overall_percent)
        machines_online = sum(
            1
            for worker in workers
            if str(worker.get("status") or "").lower() in {"online", "idle", "ready", "session_open", "queued", "rendering", "draining", "closing", "busy"}
        )
        machines_rendering = sum(
            1 for worker in workers if str(worker.get("status") or "").lower() in {"queued", "rendering", "draining", "closing", "busy"}
        )

        metrics = {
            "job_name": active_job.get("jobName") or active_job.get("jobId") or "",
            "blend_file": active_job.get("blendFile") or "",
            "total_frames": total_frames,
            "completed_frames": completed_frames,
            "pending_frames": pending_frames,
            "rendering_frames": rendering_frames,
            "error_frames": error_frames,
            "overall_percent": round(float(overall_percent or 0.0), 2),
            "elapsed_seconds": round(elapsed_seconds, 2),
            "eta_seconds": None if eta_seconds is None else round(eta_seconds, 2),
            "machines_online": machines_online,
            "machines_rendering": machines_rendering,
            "total_queued_jobs": len(queue),
        }
        metrics.update(
            {
                "totalQueuedJobs": metrics["total_queued_jobs"],
                "machinesOnline": metrics["machines_online"],
                "machinesRendering": metrics["machines_rendering"],
                "overallPercent": metrics["overall_percent"],
                "elapsedSeconds": metrics["elapsed_seconds"],
                "etaSeconds": metrics["eta_seconds"],
                "pendingFrames": metrics["pending_frames"],
                "renderingFrames": metrics["rendering_frames"],
                "errorFrames": metrics["error_frames"],
            }
        )
        return metrics

    def _sync_job_version_metadata(
        self,
        job_id: str,
        job: dict[str, Any],
        evaluations: list[dict[str, Any]],
    ) -> None:
        resolved = {
            "requiredBlenderVersion": job.get("requiredBlenderVersion"),
            "preferredBlenderMajor": job.get("preferredBlenderMajor"),
            "versionRoutingPolicy": job.get("versionRoutingPolicy"),
            "blendVersionDetected": job.get("blendVersionDetected"),
        }
        for evaluation in evaluations:
            response = evaluation.get("response") or {}
            if resolved["requiredBlenderVersion"] in {None, ""} and response.get("requiredBlenderVersion") not in {None, ""}:
                resolved["requiredBlenderVersion"] = response.get("requiredBlenderVersion")
            if resolved["preferredBlenderMajor"] is None and response.get("preferredBlenderMajor") not in {None, ""}:
                try:
                    resolved["preferredBlenderMajor"] = int(response.get("preferredBlenderMajor"))
                except (TypeError, ValueError):
                    pass
            if resolved["versionRoutingPolicy"] in {None, ""} and response.get("versionRoutingPolicy") not in {None, ""}:
                resolved["versionRoutingPolicy"] = response.get("versionRoutingPolicy")
            if resolved["blendVersionDetected"] in {None, ""} and response.get("blendVersionDetected") not in {None, ""}:
                resolved["blendVersionDetected"] = response.get("blendVersionDetected")

        if all(job.get(field) == value for field, value in resolved.items()):
            return

        self.database.execute(
            """
            UPDATE jobs
            SET requiredBlenderVersion = ?, preferredBlenderMajor = ?, versionRoutingPolicy = ?, blendVersionDetected = ?
            WHERE jobId = ?
            """,
            (
                resolved["requiredBlenderVersion"],
                resolved["preferredBlenderMajor"],
                resolved["versionRoutingPolicy"],
                resolved["blendVersionDetected"],
                job_id,
            ),
        )

    def _build_block_reason(self, job: dict[str, Any], evaluations: list[dict[str, Any]]) -> str:
        if not evaluations:
            return "No enabled workers are available to validate this job."

        gpu_only_rejections = [
            evaluation
            for evaluation in evaluations
            if evaluation.get("response", {}).get("gpuOnlyFiltered")
        ]
        if gpu_only_rejections and len(gpu_only_rejections) == len(evaluations):
            details = " | ".join(self._format_worker_validation(evaluation) for evaluation in gpu_only_rejections[:3])
            return (
                "Job is restricted to GPU-only workers, but no enabled GPU worker is currently eligible. "
                + details
            )

        runtime_rejections = [
            evaluation
            for evaluation in evaluations
            if evaluation.get("response", {}).get("runtimeFailure")
        ]
        if runtime_rejections:
            details = " | ".join(self._format_worker_validation(evaluation) for evaluation in runtime_rejections[:3])
            return (
                "No enabled workers are currently eligible because they already failed while rendering this job. "
                + details
            )

        version_rejections = [
            evaluation
            for evaluation in evaluations
            if evaluation.get("response", {}).get("versionValidationSupported")
            and evaluation.get("response", {}).get("versionCompatible") is False
        ]
        if version_rejections:
            target_version = job.get("requiredBlenderVersion") or job.get("blendVersionDetected")
            if target_version:
                prefix = f"No enabled workers are compatible with Blender {target_version}."
            else:
                prefix = "No enabled workers are compatible with the Blender version required by this job."
            details = " | ".join(self._format_worker_validation(evaluation) for evaluation in version_rejections[:3])
            return f"{prefix} {details}".strip()

        detailed_failures = [
            self._format_worker_validation(evaluation)
            for evaluation in evaluations
            if evaluation.get("response", {}).get("reason")
        ]
        if detailed_failures:
            return (
                "No enabled workers validated the blend/output paths for this job. "
                + " | ".join(detailed_failures[:3])
            )
        return "No enabled workers validated the blend/output paths for this job."

    @staticmethod
    def _format_worker_validation(evaluation: dict[str, Any]) -> str:
        worker = evaluation.get("worker") or {}
        response = evaluation.get("response") or {}
        worker_name = worker.get("name") or worker.get("workerId") or "worker"
        reason = response.get("reason") or response.get("compatibilityReason") or "validation failed"
        return f"{worker_name}: {reason}"

    def _serialize_worker_validation(self, evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "workerId": evaluation["worker"].get("workerId"),
                "workerName": evaluation["worker"].get("name"),
                "valid": evaluation.get("valid"),
                "reason": evaluation.get("response", {}).get("reason"),
                "versionCompatible": evaluation.get("response", {}).get("versionCompatible"),
                "selectedBlenderVersion": evaluation.get("response", {}).get("selectedBlenderVersion"),
                "selectedVersionBucket": evaluation.get("response", {}).get("selectedVersionBucket"),
                "sameMajorSufficientAvailable": evaluation.get("response", {}).get("sameMajorSufficientAvailable"),
                "sameMajorOlderAvailable": evaluation.get("response", {}).get("sameMajorOlderAvailable"),
                "bestSameMajorOlderVersion": evaluation.get("response", {}).get("bestSameMajorOlderVersion"),
                "higherMajorAvailable": evaluation.get("response", {}).get("higherMajorAvailable"),
                "bestHigherMajorVersion": evaluation.get("response", {}).get("bestHigherMajorVersion"),
                "gpuOnlyFiltered": evaluation.get("response", {}).get("gpuOnlyFiltered"),
                "routingFiltered": evaluation.get("response", {}).get("routingFiltered"),
                "runtimeFailure": evaluation.get("response", {}).get("runtimeFailure"),
            }
            for evaluation in evaluations
        ]

    @staticmethod
    def _version_text_sort_key(value: str) -> tuple[int, int, int]:
        parsed = str(value or "").strip().split(".")
        major = int(parsed[0]) if len(parsed) > 0 and parsed[0].isdigit() else 999
        minor = int(parsed[1]) if len(parsed) > 1 and parsed[1].isdigit() else 0
        patch = int(parsed[2]) if len(parsed) > 2 and parsed[2].isdigit() else 0
        return (major, minor, patch)

    @staticmethod
    def _normalize_render_device_preference(value: Any) -> str:
        return str(value or "").strip().upper()

    @staticmethod
    def _normalize_elapsed_seconds(value: Any) -> float | None:
        if value in {None, ""}:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric < 0.0:
            return None
        return numeric

    def _resolve_frame_elapsed_seconds(self, frame: dict[str, Any]) -> float | None:
        started_at = frame.get("startedAt")
        if started_at in {None, ""}:
            return None
        finished_at = frame.get("finishedAt") or utc_now_iso()
        try:
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        elapsed_seconds = max(0.0, (finished.astimezone(timezone.utc) - started.astimezone(timezone.utc)).total_seconds())
        return elapsed_seconds

    def _record_job_worker_success(
        self,
        job_id: str,
        worker_id: str | None,
        render_device_preference: Any,
        *,
        elapsed_seconds: float | None = None,
    ) -> None:
        if not worker_id:
            return
        device = self._normalize_render_device_preference(render_device_preference)
        normalized_elapsed_seconds = self._normalize_elapsed_seconds(elapsed_seconds)
        self.database.execute(
            """
            INSERT INTO job_worker_stats(
                jobId, workerId, renderDevicePreference,
                successCount, timedSuccessCount, totalSuccessRenderSeconds, lastSuccessRenderSeconds,
                failureCount, consecutiveFailureCount, lastFailureReason, lastFailureAt, lastSuccessAt
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(jobId, workerId, renderDevicePreference) DO UPDATE SET
                successCount = job_worker_stats.successCount + 1,
                timedSuccessCount = job_worker_stats.timedSuccessCount + ?,
                totalSuccessRenderSeconds = job_worker_stats.totalSuccessRenderSeconds + ?,
                lastSuccessRenderSeconds = COALESCE(excluded.lastSuccessRenderSeconds, job_worker_stats.lastSuccessRenderSeconds),
                consecutiveFailureCount = 0,
                lastSuccessAt = excluded.lastSuccessAt
            """,
            (
                job_id,
                str(worker_id),
                device,
                1,
                1 if normalized_elapsed_seconds is not None else 0,
                normalized_elapsed_seconds or 0.0,
                normalized_elapsed_seconds,
                0,
                0,
                None,
                None,
                utc_now_iso(),
                1 if normalized_elapsed_seconds is not None else 0,
                normalized_elapsed_seconds or 0.0,
            ),
        )

    def _record_job_worker_failure(
        self,
        job_id: str,
        worker_id: str | None,
        render_device_preference: Any,
        reason: str,
    ) -> None:
        if not worker_id:
            return
        device = self._normalize_render_device_preference(render_device_preference)
        self.database.execute(
            """
            INSERT INTO job_worker_stats(
                jobId, workerId, renderDevicePreference,
                successCount, failureCount, consecutiveFailureCount,
                lastFailureReason, lastFailureAt, lastSuccessAt
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(jobId, workerId, renderDevicePreference) DO UPDATE SET
                failureCount = job_worker_stats.failureCount + 1,
                consecutiveFailureCount = job_worker_stats.consecutiveFailureCount + 1,
                lastFailureReason = excluded.lastFailureReason,
                lastFailureAt = excluded.lastFailureAt
            """,
            (
                job_id,
                str(worker_id),
                device,
                0,
                1,
                1,
                reason,
                utc_now_iso(),
                None,
            ),
        )

    def _should_quarantine_worker_for_job(self, stats: dict[str, Any]) -> bool:
        consecutive_limit = int(self.settings.job_worker_consecutive_failure_limit)
        history_limit = int(self.settings.job_worker_failure_history_limit)
        consecutive_failures = int(stats.get("consecutiveFailureCount") or 0)
        failure_count = int(stats.get("failureCount") or 0)
        success_count = int(stats.get("successCount") or 0)

        if consecutive_limit > 0 and consecutive_failures >= consecutive_limit:
            return True
        if history_limit > 0 and failure_count >= history_limit and failure_count > success_count:
            return True
        return False

    def _format_job_worker_quarantine_reason(
        self,
        stats: dict[str, Any],
        current_render_device: str,
    ) -> str:
        device_text = f" on {current_render_device}" if current_render_device else ""
        consecutive_failures = int(stats.get("consecutiveFailureCount") or 0)
        failure_count = int(stats.get("failureCount") or 0)
        success_count = int(stats.get("successCount") or 0)
        last_reason = str(stats.get("lastFailureReason") or "previous render failure")

        if (
            int(self.settings.job_worker_consecutive_failure_limit) > 0
            and consecutive_failures >= int(self.settings.job_worker_consecutive_failure_limit)
        ):
            return (
                f"Worker failed {consecutive_failures} consecutive time(s){device_text} for this job: "
                f"{last_reason}"
            )
        if (
            int(self.settings.job_worker_failure_history_limit) > 0
            and failure_count >= int(self.settings.job_worker_failure_history_limit)
            and failure_count > success_count
        ):
            return (
                f"Worker shows repeated failures{device_text} for this job "
                f"({failure_count} failures, {success_count} successes). Last failure: {last_reason}"
            )
        return last_reason

    def _job_failed_worker_reasons(self, job_id: str) -> dict[str, str]:
        rows = self.database.fetch_all(
            """
            SELECT workerId, renderDevicePreference, successCount, failureCount,
                   consecutiveFailureCount, lastFailureReason, lastFailureAt, lastSuccessAt
            FROM job_worker_stats
            WHERE jobId = ?
            ORDER BY lastFailureAt DESC, lastSuccessAt DESC, workerId ASC
            """,
            (job_id,),
        )
        reasons: dict[str, str] = {}
        for row in rows:
            worker_id = str(row.get("workerId") or "").strip()
            if not worker_id or worker_id in reasons:
                continue
            worker = self.worker_registry.get_worker(worker_id)
            failed_render_device = self._normalize_render_device_preference(row.get("renderDevicePreference"))
            current_render_device = (
                self._normalize_render_device_preference(worker.get("renderDevicePreference"))
                if worker is not None
                else ""
            )
            if failed_render_device and current_render_device and current_render_device != failed_render_device:
                continue
            if current_render_device == "CPU" and failed_render_device != "CPU":
                continue
            if not self._should_quarantine_worker_for_job(row):
                continue
            reasons[worker_id] = self._format_job_worker_quarantine_reason(row, current_render_device)
        return reasons

    def _apply_job_worker_failure_quarantine(
        self,
        job_id: str,
        evaluations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        failed_worker_reasons = self._job_failed_worker_reasons(job_id)
        if not failed_worker_reasons:
            return evaluations

        clean_valid_exists = any(
            evaluation.get("valid")
            and evaluation.get("worker", {}).get("workerId") not in failed_worker_reasons
            for evaluation in evaluations
        )
        if not clean_valid_exists:
            return evaluations

        filtered: list[dict[str, Any]] = []
        for evaluation in evaluations:
            worker = evaluation.get("worker") or {}
            worker_id = worker.get("workerId")
            response = dict(evaluation.get("response") or {})
            valid = bool(evaluation.get("valid"))
            if valid and worker_id in failed_worker_reasons:
                response["reason"] = (
                    "Worker previously failed during this job: "
                    f"{failed_worker_reasons[worker_id]}"
                )
                response["runtimeFailure"] = True
                valid = False
            filtered.append(
                {
                    **evaluation,
                    "response": response,
                    "valid": valid,
                }
            )
        return filtered

    def _has_online_non_failed_worker_candidate(self, job: dict[str, Any], failed_worker_ids: set[str]) -> bool:
        for worker in self.worker_registry.list_workers():
            if not worker["enabled"]:
                continue
            if worker["workerId"] in failed_worker_ids:
                continue
            if not self.worker_registry.worker_matches_job_device_filter(worker, job):
                continue
            if worker["status"] in ONLINE_WORKER_STATUSES or worker["status"] == WorkerStatus.UNKNOWN.value:
                return True
        return False

    def _frame_counts(self, job_id: str) -> dict[str, int]:
        rows = self.database.fetch_all(
            """
            SELECT status, COUNT(*) AS count
            FROM job_frames
            WHERE jobId = ?
            GROUP BY status
            """,
            (job_id,),
        )
        counts = {
            FrameStatus.PENDING.value: 0,
            FrameStatus.LEASED.value: 0,
            FrameStatus.RENDERING.value: 0,
            FrameStatus.DONE.value: 0,
            FrameStatus.ERROR.value: 0,
            FrameStatus.CANCELLED.value: 0,
        }
        for row in rows:
            counts[row["status"]] = int(row["count"])
        return {
            "pending": counts[FrameStatus.PENDING.value],
            "leased": counts[FrameStatus.LEASED.value],
            "rendering": counts[FrameStatus.RENDERING.value],
            "done": counts[FrameStatus.DONE.value],
            "error": counts[FrameStatus.ERROR.value],
            "cancelled": counts[FrameStatus.CANCELLED.value],
        }

    def _job_has_remaining_work(self, job_id: str) -> bool:
        row = self.database.fetch_one(
            """
            SELECT 1
            FROM job_frames
            WHERE jobId = ? AND status IN (?, ?, ?)
            LIMIT 1
            """,
            (
                job_id,
                FrameStatus.PENDING.value,
                FrameStatus.LEASED.value,
                FrameStatus.RENDERING.value,
            ),
        )
        return bool(row)

    def _job_has_error_frames(self, job_id: str) -> bool:
        row = self.database.fetch_one(
            """
            SELECT 1
            FROM job_frames
            WHERE jobId = ? AND status = ?
            LIMIT 1
            """,
            (job_id, FrameStatus.ERROR.value),
        )
        return bool(row)

    def _generate_job_id(self, connection: Any) -> str:
        prefix = utc_now_iso()[:10].replace("-", "_")
        count = connection.execute(
            "SELECT COUNT(*) AS total FROM jobs WHERE jobId LIKE ?",
            (f"job_{prefix}_%",),
        ).fetchone()[0]
        return f"job_{prefix}_{count + 1:03d}"

    def _next_queue_position(self, connection: Any) -> int:
        current = connection.execute(
            """
            SELECT COALESCE(MAX(queuePosition), 0)
            FROM jobs
            WHERE status NOT IN ('completed', 'cancelled', 'error')
            """
        ).fetchone()[0]
        return int(current) + 1

    def _recompute_queue_positions(self, connection: Any) -> None:
        rows = connection.execute(
            """
            SELECT jobId
            FROM jobs
            WHERE status NOT IN ('completed', 'cancelled', 'error')
            ORDER BY queuePosition ASC, createdAt ASC
            """
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            connection.execute("UPDATE jobs SET queuePosition = ? WHERE jobId = ?", (index, row["jobId"]))

    def _get_job_row(self, job_id: str) -> dict[str, Any]:
        job = self.database.fetch_one("SELECT * FROM jobs WHERE jobId = ?", (job_id,))
        if not job:
            raise NotFoundError(f"Job '{job_id}' not found.")
        return job

    def _get_active_job_row(self) -> dict[str, Any] | None:
        return self.database.fetch_one(
            """
            SELECT *
            FROM jobs
            WHERE status IN ('validating', 'running', 'paused', 'blocked')
            ORDER BY queuePosition ASC, createdAt ASC
            LIMIT 1
            """
        )

    @staticmethod
    def _render_accepted(response: dict[str, Any]) -> bool:
        if not response:
            return True
        if "accepted" in response:
            return normalize_bool(response["accepted"])
        if "success" in response:
            return normalize_bool(response["success"])
        if "ok" in response:
            return normalize_bool(response["ok"])
        return True

    @staticmethod
    def _normalize_job_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)

        master_id = str(normalized.get("submittedByMasterId") or "").strip()
        master_name = str(normalized.get("submittedByMasterName") or "").strip()
        submitted_by_user = str(normalized.get("submittedByUser") or "").strip()

        if not master_id:
            master_id = "master_unknown"
        if not master_name:
            if master_id and master_id != "master_unknown":
                master_name = master_id.upper()
            else:
                hostname = socket.gethostname().strip() or "MASTER"
                master_name = f"{hostname.upper()}-UNKNOWN"
        if not submitted_by_user:
            submitted_by_user = getpass.getuser().strip() or "unknown"

        normalized["submittedByMasterId"] = master_id
        normalized["submittedByMasterName"] = master_name
        normalized["submittedByUser"] = submitted_by_user
        return normalized

    @staticmethod
    def _validate_job_payload(payload: dict[str, Any]) -> None:
        required_fields = [
            "jobName",
            "blendFile",
            "outputFolder",
            "startFrame",
            "endFrame",
            "submittedByMasterId",
            "submittedByMasterName",
            "submittedByUser",
        ]
        for field in required_fields:
            if payload.get(field) in {None, ""}:
                raise ValueError(f"Field '{field}' is required.")

        start_frame = int(payload["startFrame"])
        end_frame = int(payload["endFrame"])
        if end_frame < start_frame:
            raise ValueError("endFrame must be greater than or equal to startFrame.")
        if not str(payload["blendFile"]).lower().endswith(".blend"):
            raise ValueError("blendFile must point to a .blend file.")
        if int(payload.get("blockSize", 1)) <= 0:
            raise ValueError("blockSize must be greater than zero.")
        blend_path = Path(str(payload["blendFile"]))
        if blend_path.exists() and not blend_path.is_file():
            raise ValueError("blendFile exists locally but is not a file.")
        output_folder = Path(str(payload["outputFolder"]))
        if output_folder.exists() and not output_folder.is_dir():
            raise ValueError("outputFolder exists locally but is not a directory.")

    def _validate_no_active_output_collision(
        self,
        output_pattern: str,
        start_frame: int,
        end_frame: int,
    ) -> None:
        try:
            normalized_pattern = str(Path(output_pattern).resolve(strict=False)).casefold()
        except OSError:
            normalized_pattern = str(Path(output_pattern)).casefold()
        candidates = self.database.fetch_all(
            """
            SELECT jobId, jobName, outputPattern, startFrame, endFrame, status
            FROM jobs
            WHERE status NOT IN (?, ?, ?)
              AND NOT (endFrame < ? OR startFrame > ?)
            """,
            (
                JobStatus.COMPLETED.value,
                JobStatus.CANCELLED.value,
                JobStatus.ERROR.value,
                start_frame,
                end_frame,
            ),
        )
        for candidate in candidates:
            try:
                candidate_pattern = str(Path(candidate["outputPattern"]).resolve(strict=False)).casefold()
            except OSError:
                candidate_pattern = str(Path(candidate["outputPattern"])).casefold()
            if candidate_pattern != normalized_pattern:
                continue
            raise ValueError(
                "Output collision with active job "
                f"'{candidate['jobId']}' ({candidate['jobName']}) on overlapping frames."
            )
