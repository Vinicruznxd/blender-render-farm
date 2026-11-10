from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from app.core.database import Database
from app.core.models import FREE_WORKER_STATUSES, ONLINE_WORKER_STATUSES, WorkerStatus, normalize_bool, utc_now_iso
from app.core.settings import AppSettings
from app.core.version_routing import (
    DEFAULT_VERSION_ROUTING_POLICY,
    PREFER_HIGHER_MAJOR_WHEN_SAME_MAJOR_OLDER_POLICY,
    PREFER_SAME_MAJOR_ANY_VERSION_POLICY,
    SAME_MAJOR_SELECTION_BUCKETS,
    SELECTION_BUCKET_EXACT,
    SELECTION_BUCKET_HIGHER_MAJOR,
    SELECTION_BUCKET_SAME_MAJOR_OLDER,
    SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT,
    classify_selected_version_bucket,
    normalize_version_routing_policy,
)
from app.services.logger import AppLogger
from app.services.worker_client import WorkerClient


WORKER_COLUMNS = [
    "workerId",
    "name",
    "ip",
    "port",
    "enabled",
    "status",
    "blenderFound",
    "blenderVersion",
    "gpu",
    "renderDevicePreference",
    "effectiveUseGpu",
    "currentJobId",
    "currentBlockId",
    "currentFrame",
    "currentAssignmentId",
    "progress",
    "persistentSessionEnabled",
    "sessionState",
    "sessionQueueDepth",
    "sessionDrainRequested",
    "queuedAssignments",
    "lastSeen",
    "lastLog",
    "error",
    "blendAccessible",
    "outputAccessible",
    "lastJob",
    "capabilities",
    "createdAt",
    "updatedAt",
]

VERSION_METADATA_KEYS = [
    "blendVersionDetected",
    "detectedBlendVersion",
    "selectedBlenderVersion",
    "blenderVersionSelected",
    "selectedBlenderPath",
    "requiredBlenderVersion",
    "preferredBlenderMajor",
    "versionRoutingPolicy",
    "versionCompatible",
    "blenderVersionCompatible",
    "compatibilityReason",
    "versionCompatibilityReason",
    "selectedVersionBucket",
    "sameMajorSufficientAvailable",
    "sameMajorOlderAvailable",
    "bestSameMajorOlderVersion",
    "higherMajorAvailable",
    "bestHigherMajorVersion",
]


class WorkerRegistry:
    def __init__(
        self,
        settings: AppSettings,
        database: Database,
        logger: AppLogger,
        worker_client: WorkerClient,
        discovery_worker_client: WorkerClient | None = None,
    ):
        self.settings = settings
        self.database = database
        self.logger = logger
        self.worker_client = worker_client
        self.discovery_worker_client = discovery_worker_client or WorkerClient(self.settings.discovery_timeout_seconds)
        self._validation_cache: dict[tuple[object, ...], tuple[float, bool, dict[str, Any]]] = {}
        self._validation_cache_lock = threading.RLock()

    def list_workers(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
            SELECT workerId, name, ip, port, enabled, status, blenderFound, blenderVersion, gpu,
                   renderDevicePreference, effectiveUseGpu, currentJobId, currentBlockId, currentFrame,
                   currentAssignmentId, progress, persistentSessionEnabled, sessionState,
                   sessionQueueDepth, sessionDrainRequested, queuedAssignments, lastSeen, lastLog, error, blendAccessible,
                   outputAccessible, lastJob, capabilities, createdAt, updatedAt
            FROM workers
            ORDER BY name COLLATE NOCASE, ip, port
            """
        )
        return [self._deserialize_worker(row) for row in rows]

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            """
            SELECT workerId, name, ip, port, enabled, status, blenderFound, blenderVersion, gpu,
                   renderDevicePreference, effectiveUseGpu, currentJobId, currentBlockId, currentFrame,
                   currentAssignmentId, progress, persistentSessionEnabled, sessionState,
                   sessionQueueDepth, sessionDrainRequested, queuedAssignments, lastSeen, lastLog, error, blendAccessible,
                   outputAccessible, lastJob, capabilities, createdAt, updatedAt
            FROM workers
            WHERE workerId = ?
            """,
            (worker_id,),
        )
        return self._deserialize_worker(row) if row else None

    def register_or_update_worker(self, data: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(data)
        prepared["ip"] = self._normalize_host_alias(str(prepared["ip"]))
        prepared["port"] = int(prepared["port"])
        existing = self._find_existing_worker(prepared)
        worker_id = prepared.get("workerId") or (existing["workerId"] if existing else None)
        worker_id = worker_id or self._make_worker_id(prepared["ip"], int(prepared["port"]))
        now = utc_now_iso()
        chosen_ip = self._choose_preferred_ip(existing["ip"] if existing else None, prepared["ip"])
        chosen_name = prepared.get("name")
        if not chosen_name and existing:
            chosen_name = existing.get("name")
        if not chosen_name:
            chosen_name = f"{chosen_ip}:{prepared['port']}"
        payload = {
            "workerId": worker_id,
            "name": chosen_name,
            "ip": chosen_ip,
            "port": prepared["port"],
            "enabled": 1 if normalize_bool(prepared.get("enabled", True), True) else 0,
            "status": prepared.get("status", WorkerStatus.UNKNOWN.value),
            "blenderFound": self._optional_int(prepared.get("blenderFound")),
            "blenderVersion": prepared.get("blenderVersion"),
            "gpu": prepared.get("gpu"),
            "renderDevicePreference": prepared.get("renderDevicePreference"),
            "effectiveUseGpu": self._optional_int(prepared.get("effectiveUseGpu")),
            "currentJobId": prepared.get("currentJobId"),
            "currentBlockId": prepared.get("currentBlockId"),
            "currentFrame": prepared.get("currentFrame"),
            "currentAssignmentId": prepared.get("currentAssignmentId"),
            "progress": prepared.get("progress"),
            "persistentSessionEnabled": self._optional_int(prepared.get("persistentSessionEnabled")),
            "sessionState": prepared.get("sessionState"),
            "sessionQueueDepth": prepared.get("sessionQueueDepth"),
            "sessionDrainRequested": self._optional_int(prepared.get("sessionDrainRequested")),
            "queuedAssignments": json.dumps(prepared.get("queuedAssignments")) if isinstance(prepared.get("queuedAssignments"), list) else prepared.get("queuedAssignments"),
            "lastSeen": prepared.get("lastSeen"),
            "lastLog": prepared.get("lastLog"),
            "error": prepared.get("error"),
            "blendAccessible": self._optional_int(prepared.get("blendAccessible")),
            "outputAccessible": self._optional_int(prepared.get("outputAccessible")),
            "lastJob": json.dumps(prepared.get("lastJob")) if isinstance(prepared.get("lastJob"), dict) else prepared.get("lastJob"),
            "capabilities": json.dumps(prepared.get("capabilities")) if isinstance(prepared.get("capabilities"), dict) else prepared.get("capabilities"),
            "createdAt": data.get("createdAt", existing["createdAt"] if existing else now),
            "updatedAt": now,
        }
        columns = ", ".join(WORKER_COLUMNS)
        placeholders = ", ".join(f":{column}" for column in WORKER_COLUMNS)
        updates = ", ".join(f"{column} = excluded.{column}" for column in WORKER_COLUMNS if column not in {"workerId", "createdAt"})
        self.database.execute(
            f"""
            INSERT INTO workers ({columns})
            VALUES ({placeholders})
            ON CONFLICT(workerId) DO UPDATE SET {updates}
            """,
            payload,
        )
        self.consolidate_workers()
        return self.get_worker(worker_id) or payload

    def update_worker(self, worker_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        worker = self.get_worker(worker_id)
        if not worker:
            return None
        worker.update(changes)
        worker["workerId"] = worker_id
        return self.register_or_update_worker(worker)

    def delete_worker(self, worker_id: str) -> bool:
        return self.database.execute("DELETE FROM workers WHERE workerId = ?", (worker_id,)) > 0

    def refresh_workers(self) -> list[dict[str, Any]]:
        self.consolidate_workers()
        refreshed: list[dict[str, Any]] = []
        workers = self.list_workers()
        enabled_workers = [worker for worker in workers if worker["enabled"]]
        probe_results: dict[str, tuple[dict[str, Any] | None, Exception | None]] = {}

        def probe(worker: dict[str, Any]) -> tuple[str, dict[str, Any] | None, Exception | None]:
            try:
                return worker["workerId"], self._probe_worker(worker["ip"], int(worker["port"])), None
            except Exception as exc:
                return worker["workerId"], None, exc

        if enabled_workers:
            with ThreadPoolExecutor(
                max_workers=min(len(enabled_workers), int(self.settings.worker_probe_concurrency))
            ) as executor:
                for worker_id, result, error in executor.map(probe, enabled_workers):
                    probe_results[worker_id] = (result, error)

        for worker in workers:
            if not worker["enabled"]:
                refreshed.append(worker)
                continue
            probe_result, probe_error = probe_results.get(worker["workerId"], (None, RuntimeError("probe missing")))
            try:
                if probe_error is not None:
                    raise probe_error
                assert probe_result is not None
                probe_result["workerId"] = worker["workerId"]
                if not probe_result.get("renderDevicePreference"):
                    probe_result["renderDevicePreference"] = worker.get("renderDevicePreference")
                updated = self.register_or_update_worker(probe_result)
                refreshed.append(updated)
            except Exception as exc:
                if self._is_within_status_grace(worker.get("lastSeen")):
                    updated = self.update_worker(
                        worker["workerId"],
                        {
                            "error": str(exc),
                        },
                    )
                    if updated:
                        refreshed.append(updated)
                    continue
                updated = self.update_worker(
                    worker["workerId"],
                    {
                        "status": WorkerStatus.OFFLINE.value,
                        "error": str(exc),
                    },
                )
                if updated:
                    refreshed.append(updated)
        return refreshed

    def discover_workers(self, hosts: list[str] | None = None, ports: list[int] | None = None) -> list[dict[str, Any]]:
        target_hosts = hosts or self._default_discovery_hosts()
        target_ports = ports or list(self.settings.discovery_ports)
        normalized_hosts = sorted({self._normalize_host_alias(host) for host in target_hosts})
        targets = [(host, int(port)) for host in normalized_hosts for port in target_ports]
        discovered_by_id: dict[str, dict[str, Any]] = {}

        max_workers = min(max(len(targets), 1), 128)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(self._safe_probe_worker, host, port): (host, port) for host, port in targets}
            for future in as_completed(future_map):
                result = future.result()
                if not result:
                    continue
                worker = self.register_or_update_worker(result)
                discovered_by_id[worker["workerId"]] = worker

        self.consolidate_workers()
        discovered_group_keys = {self._worker_group_key(worker) for worker in discovered_by_id.values()}
        discovered: list[dict[str, Any]] = []
        seen_worker_ids: set[str] = set()
        for worker in self.list_workers():
            if worker["workerId"] in seen_worker_ids:
                continue
            if self._worker_group_key(worker) not in discovered_group_keys:
                continue
            seen_worker_ids.add(worker["workerId"])
            discovered.append(worker)

        if discovered:
            self.logger.info(
                f"Worker discovery found {len(discovered)} worker(s).",
                category="worker",
                details={"count": len(discovered)},
            )
        return discovered

    def test_worker(self, worker_id: str) -> dict[str, Any]:
        worker = self.get_worker(worker_id)
        if not worker:
            raise KeyError(worker_id)
        base_url = self._base_url(worker)
        health = self.worker_client.health(base_url)
        status = self.worker_client.status(base_url)
        capabilities = self.worker_client.capabilities(base_url)
        render_device = {}
        try:
            render_device = self.worker_client.get_render_device(base_url)
        except Exception:
            render_device = {}
        updated = self.register_or_update_worker(
            {
                **worker,
                **self._extract_worker_state(worker["ip"], int(worker["port"]), health, status, capabilities),
            }
        )
        return {
            "connected": True,
            "worker": updated,
            "health": health,
            "status": status,
            "capabilities": capabilities,
            "renderDevice": render_device,
        }

    def validate_worker_for_job(self, worker: dict[str, Any], job: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        if not self.worker_matches_job_device_filter(worker, job):
            return False, {
                "valid": False,
                "gpuOnlyFiltered": True,
                "reason": self._device_filter_rejection_reason(job, worker),
            }

        cache_key = (
            worker.get("workerId"), worker.get("ip"), worker.get("port"),
            worker.get("renderDevicePreference"), job.get("blendFile"), job.get("outputFolder"),
            job.get("requiredBlenderVersion"), job.get("preferredBlenderMajor"),
            job.get("versionRoutingPolicy"), job.get("blendVersionDetected"),
        )
        with self._validation_cache_lock:
            cached = self._validation_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] <= self.settings.worker_validation_cache_seconds:
                return cached[1], dict(cached[2])

        payload = {
            "blendFile": job["blendFile"],
            "outputFolder": job["outputFolder"],
            "outputPattern": job["outputPattern"],
        }
        for field in ("requiredBlenderVersion", "preferredBlenderMajor", "versionRoutingPolicy", "blendVersionDetected"):
            if job.get(field) is not None:
                payload[field] = job.get(field)
        try:
            response = self.worker_client.validate_paths(
                self._base_url(worker),
                payload,
            )
        except Exception as exc:
            self.update_worker(
                worker["workerId"],
                {
                    "blendAccessible": 0,
                    "outputAccessible": 0,
                    "error": str(exc),
                },
            )
            return False, {"valid": False, "reason": str(exc)}

        normalized = self._normalize_validation_response(worker, job, response)

        self.update_worker(
            worker["workerId"],
            {
                "blendAccessible": self._optional_int(normalized.get("blendAccessible")),
                "outputAccessible": self._optional_int(normalized.get("outputAccessible")),
                "error": None if normalized.get("valid") else normalized.get("reason"),
            },
        )
        valid = bool(normalized.get("valid"))
        if valid:
            with self._validation_cache_lock:
                self._validation_cache[cache_key] = (time.monotonic(), valid, dict(normalized))
        return valid, normalized

    def evaluate_workers_for_job(self, job: dict[str, Any], *, only_free: bool) -> list[dict[str, Any]]:
        evaluations: list[dict[str, Any]] = []
        candidates = []
        for worker in self.list_workers():
            if not worker["enabled"]:
                continue
            if worker["status"] not in ONLINE_WORKER_STATUSES and worker["status"] != WorkerStatus.UNKNOWN.value:
                continue
            if only_free and not self.is_worker_free(worker):
                continue
            candidates.append(worker)

        def validate(worker: dict[str, Any]) -> tuple[dict[str, Any], bool, dict[str, Any]]:
            valid, response = self.validate_worker_for_job(worker, job)
            return worker, valid, response

        if not candidates:
            return evaluations
        with ThreadPoolExecutor(
            max_workers=min(len(candidates), int(self.settings.worker_probe_concurrency))
        ) as executor:
            results = list(executor.map(validate, candidates))
        for worker, valid, response in results:
            refreshed = self.get_worker(worker["workerId"]) or worker
            evaluations.append(
                {
                    "worker": refreshed,
                    "valid": valid,
                    "response": response,
                    "sortKey": self._compatibility_sort_key(job, refreshed, response),
                }
            )
        evaluations.sort(
            key=lambda item: (
                0 if item["valid"] else 1,
                item["sortKey"],
                str(item["worker"].get("name") or item["worker"]["workerId"]).lower(),
            )
        )
        return evaluations

    def filter_evaluations_for_job(
        self,
        job: dict[str, Any],
        evaluations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        policy = normalize_version_routing_policy(job.get("versionRoutingPolicy"))
        valid_evaluations = [evaluation for evaluation in evaluations if evaluation.get("valid")]
        if not valid_evaluations:
            return evaluations

        same_major_sufficient = [
            evaluation
            for evaluation in valid_evaluations
            if self._evaluation_bucket(evaluation, job) in {SELECTION_BUCKET_EXACT, SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT}
        ]
        same_major_any = [
            evaluation
            for evaluation in valid_evaluations
            if self._evaluation_bucket(evaluation, job) in SAME_MAJOR_SELECTION_BUCKETS
        ]
        higher_major = [
            evaluation
            for evaluation in valid_evaluations
            if self._evaluation_bucket(evaluation, job) == SELECTION_BUCKET_HIGHER_MAJOR
        ]

        allowed_ids: set[str] | None = None
        group_reason = ""
        if policy == PREFER_SAME_MAJOR_ANY_VERSION_POLICY:
            if same_major_any:
                allowed_ids = {
                    str(evaluation.get("worker", {}).get("workerId"))
                    for evaluation in same_major_any
                    if evaluation.get("worker", {}).get("workerId")
                }
                group_reason = "Job is currently routing only to same-major Blender candidates."
            elif higher_major:
                allowed_ids = {
                    str(evaluation.get("worker", {}).get("workerId"))
                    for evaluation in higher_major
                    if evaluation.get("worker", {}).get("workerId")
                }
                group_reason = "Job is currently routing only to higher-major Blender candidates."
        else:
            if same_major_sufficient:
                allowed_ids = {
                    str(evaluation.get("worker", {}).get("workerId"))
                    for evaluation in same_major_sufficient
                    if evaluation.get("worker", {}).get("workerId")
                }
                group_reason = "Job is currently routing only to same-major Blender candidates that meet the file version."
            elif higher_major:
                allowed_ids = {
                    str(evaluation.get("worker", {}).get("workerId"))
                    for evaluation in higher_major
                    if evaluation.get("worker", {}).get("workerId")
                }
                if policy == PREFER_HIGHER_MAJOR_WHEN_SAME_MAJOR_OLDER_POLICY:
                    group_reason = "Job is currently routing only to a higher available Blender major version."
                else:
                    group_reason = "Job is currently routing only to higher-major Blender candidates."

        if not allowed_ids:
            return evaluations

        filtered: list[dict[str, Any]] = []
        for evaluation in evaluations:
            worker = evaluation.get("worker") or {}
            worker_id = str(worker.get("workerId") or "")
            if evaluation.get("valid") and worker_id and worker_id not in allowed_ids:
                response = dict(evaluation.get("response") or {})
                response["valid"] = False
                response["routingFiltered"] = True
                response["reason"] = group_reason
                filtered.append(
                    {
                        **evaluation,
                        "valid": False,
                        "response": response,
                    }
                )
                continue
            filtered.append(evaluation)
        return filtered

    def find_eligible_workers_for_job(self, job: dict[str, Any], *, only_free: bool) -> list[dict[str, Any]]:
        return [
            evaluation["worker"]
            for evaluation in self.filter_evaluations_for_job(
                job,
                self.evaluate_workers_for_job(job, only_free=only_free),
            )
            if evaluation["valid"]
        ]

    def worker_matches_job_device_filter(self, worker: dict[str, Any], job: dict[str, Any]) -> bool:
        if not normalize_bool(job.get("gpuOnlyWorkers"), False):
            return True
        return self.worker_uses_gpu(worker)

    def worker_uses_gpu(self, worker: dict[str, Any]) -> bool:
        effective_use_gpu = worker.get("effectiveUseGpu")
        if effective_use_gpu is not None:
            return normalize_bool(effective_use_gpu, False)
        return str(worker.get("renderDevicePreference") or "").strip().upper() == "GPU"

    def is_worker_free(self, worker: dict[str, Any]) -> bool:
        active_frame = self.database.fetch_one(
            """
            SELECT 1
            FROM job_frames
            WHERE workerId = ? AND status IN (?, ?)
            LIMIT 1
            """,
            (
                worker["workerId"],
                "leased",
                "rendering",
            ),
        )
        if active_frame:
            return False
        return (
            worker["status"] in FREE_WORKER_STATUSES
            and not worker.get("currentJobId")
            and worker.get("currentFrame") is None
            and not worker.get("currentAssignmentId")
        )

    def set_worker_busy(
        self,
        worker_id: str,
        *,
        job_id: str,
        block_id: str,
        frame_number: int,
        assignment_id: str | None = None,
    ) -> None:
        self.update_worker(
            worker_id,
            {
                "status": WorkerStatus.BUSY.value,
                "currentJobId": job_id,
                "currentBlockId": block_id,
                "currentFrame": frame_number,
                "currentAssignmentId": assignment_id,
                "progress": 0.0,
                "error": None,
            },
        )

    def clear_worker_assignment(self, worker_id: str, *, status: str | None = None) -> None:
        changes: dict[str, Any] = {
            "currentJobId": None,
            "currentBlockId": None,
            "currentFrame": None,
            "currentAssignmentId": None,
            "progress": 0.0,
            "sessionState": None,
            "sessionQueueDepth": 0,
            "sessionDrainRequested": 0,
            "queuedAssignments": [],
        }
        if status:
            changes["status"] = status
        self.update_worker(worker_id, changes)

    def _safe_probe_worker(self, host: str, port: int) -> dict[str, Any] | None:
        try:
            if not self._is_worker_port_open(host, port):
                return None
            return self._probe_worker(host, port, client=self.discovery_worker_client)
        except Exception:
            return None

    def _probe_worker(self, host: str, port: int, *, client: WorkerClient | None = None) -> dict[str, Any]:
        resolved_client = client or self.worker_client
        base_url = f"http://{host}:{port}"
        health = resolved_client.health(base_url)
        status = resolved_client.status(base_url)
        try:
            capabilities = resolved_client.capabilities(base_url)
        except Exception:
            capabilities = {}
        return self._extract_worker_state(host, port, health, status, capabilities)

    def _extract_worker_state(
        self,
        host: str,
        port: int,
        health: dict[str, Any],
        status: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        blender_found = self._first_defined(
            status.get("blenderFound"),
            capabilities.get("blenderFound"),
            health.get("blenderFound"),
        )
        effective_use_gpu = self._first_defined(
            status.get("effectiveUseGpu"),
            status.get("useGpu"),
            capabilities.get("effectiveUseGpu"),
        )
        return {
            "workerId": self._first_defined(status.get("workerId"), health.get("workerId"), self._make_worker_id(host, port)),
            "name": self._first_defined(
                status.get("name"), status.get("machineName"),
                health.get("name"), health.get("machineName"),
                capabilities.get("name"), capabilities.get("machineName"), f"{host}:{port}",
            ),
            "ip": host,
            "port": port,
            "enabled": 1,
            "status": self._normalize_worker_status(status),
            "blenderFound": self._optional_int(blender_found),
            "blenderVersion": self._first_defined(status.get("blenderVersion"), capabilities.get("blenderVersion")),
            "gpu": self._first_defined(status.get("gpu"), capabilities.get("gpu")),
            "renderDevicePreference": self._first_defined(status.get("renderDevicePreference"), capabilities.get("renderDevicePreference")),
            "effectiveUseGpu": self._optional_int(effective_use_gpu),
            "currentJobId": self._first_defined(status.get("currentJobId"), status.get("jobId")),
            "currentBlockId": self._first_defined(status.get("currentBlockId"), status.get("blockId")),
            "currentFrame": self._first_defined(status.get("currentFrame"), status.get("frame")),
            "currentAssignmentId": status.get("assignmentId"),
            "progress": self._normalize_progress_percent(self._first_defined(status.get("progress"), 0.0)) or 0.0,
            "persistentSessionEnabled": self._optional_int(status.get("persistentSessionEnabled")),
            "sessionState": status.get("sessionState"),
            "sessionQueueDepth": self._coerce_int(status.get("sessionQueueDepth")) or 0,
            "sessionDrainRequested": self._optional_int(status.get("sessionDrainRequested")),
            "queuedAssignments": status.get("queuedAssignments"),
            "lastSeen": now,
            "lastLog": self._first_defined(status.get("lastLog"), health.get("lastLog")),
            "error": self._first_defined(status.get("error"), health.get("error")),
            "blendAccessible": None,
            "outputAccessible": None,
            "lastJob": status.get("lastJob"),
            "capabilities": capabilities,
        }

    def _normalize_validation_response(
        self,
        worker: dict[str, Any],
        job: dict[str, Any],
        response: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = dict(response or {})
        blend_ok = self._response_flag(payload, ["blendAccessible", "blendFileAccessible", "blendExists"], default=None)
        output_ok = self._response_flag(payload, ["outputAccessible", "outputFolderAccessible", "outputWritable"], default=None)
        valid = self._response_flag(payload, ["valid", "success", "ok"], default=None)

        if valid is None:
            valid = (blend_ok if blend_ok is not None else True) and (output_ok if output_ok is not None else True)

        version_supported = any(key in payload for key in VERSION_METADATA_KEYS)
        version_compatible = self._extract_version_compatibility(payload, version_supported=version_supported)

        detected_version = self._first_defined(
            payload.get("blendVersionDetected"),
            payload.get("detectedBlendVersion"),
            job.get("blendVersionDetected"),
        )
        required_version = self._first_defined(
            payload.get("requiredBlenderVersion"),
            job.get("requiredBlenderVersion"),
            detected_version,
        )
        preferred_major = self._coerce_int(
            self._first_defined(
                payload.get("preferredBlenderMajor"),
                job.get("preferredBlenderMajor"),
            )
        )
        if preferred_major is None:
            preferred_major = self._version_major(required_version)
        raw_policy = self._first_defined(payload.get("versionRoutingPolicy"), job.get("versionRoutingPolicy"))
        policy = normalize_version_routing_policy(raw_policy)
        selected_version = self._first_defined(
            payload.get("selectedBlenderVersion"),
            payload.get("blenderVersionSelected"),
            worker.get("blenderVersion"),
        )
        selected_bucket = self._first_defined(
            payload.get("selectedVersionBucket"),
            payload.get("versionBucket"),
        )
        inferred_version_compatible = None
        if version_compatible is None:
            inferred_version_compatible = self._infer_version_compatibility(
                required_version=required_version,
                selected_version=selected_version,
            )
            version_compatible = inferred_version_compatible
        effective_version_support = version_supported or inferred_version_compatible is not None
        compatible = bool(valid) and (version_compatible if version_compatible is not None else True)
        selected_path = payload.get("selectedBlenderPath")
        if not selected_bucket:
            selected_bucket = classify_selected_version_bucket(
                required_version,
                selected_version,
                preferred_major=preferred_major,
            )
        same_major_sufficient_available = normalize_bool(
            self._first_defined(payload.get("sameMajorSufficientAvailable"), selected_bucket in {
                SELECTION_BUCKET_EXACT,
                SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT,
            }),
            selected_bucket in {
                SELECTION_BUCKET_EXACT,
                SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT,
            },
        )
        same_major_older_available = normalize_bool(
            self._first_defined(payload.get("sameMajorOlderAvailable"), selected_bucket == SELECTION_BUCKET_SAME_MAJOR_OLDER),
            selected_bucket == SELECTION_BUCKET_SAME_MAJOR_OLDER,
        )
        best_same_major_older_version = str(
            self._first_defined(
                payload.get("bestSameMajorOlderVersion"),
                selected_version if selected_bucket == SELECTION_BUCKET_SAME_MAJOR_OLDER else "",
            )
            or ""
        )
        higher_major_available = normalize_bool(
            self._first_defined(payload.get("higherMajorAvailable"), selected_bucket == SELECTION_BUCKET_HIGHER_MAJOR),
            selected_bucket == SELECTION_BUCKET_HIGHER_MAJOR,
        )
        best_higher_major_version = str(
            self._first_defined(
                payload.get("bestHigherMajorVersion"),
                selected_version if selected_bucket == SELECTION_BUCKET_HIGHER_MAJOR else "",
            )
            or ""
        )
        reason = self._build_validation_reason(
            payload,
            valid=bool(valid),
            version_supported=effective_version_support,
            version_compatible=version_compatible,
            required_version=required_version,
            selected_version=selected_version,
            worker_name=worker.get("name") or worker["workerId"],
        )

        payload.update(
            {
                "valid": compatible,
                "blendAccessible": blend_ok,
                "outputAccessible": output_ok,
                "versionValidationSupported": effective_version_support,
                "versionCompatible": version_compatible,
                "blendVersionDetected": detected_version,
                "requiredBlenderVersion": required_version,
                "preferredBlenderMajor": preferred_major,
                "versionRoutingPolicy": policy,
                "selectedBlenderVersion": selected_version,
                "selectedBlenderPath": selected_path,
                "selectedVersionBucket": selected_bucket,
                "sameMajorSufficientAvailable": same_major_sufficient_available,
                "sameMajorOlderAvailable": same_major_older_available,
                "bestSameMajorOlderVersion": best_same_major_older_version,
                "higherMajorAvailable": higher_major_available,
                "bestHigherMajorVersion": best_higher_major_version,
                "compatibilityReason": self._first_defined(
                    payload.get("compatibilityReason"),
                    payload.get("versionCompatibilityReason"),
                ),
                "reason": reason,
            }
        )
        return payload

    def _build_validation_reason(
        self,
        payload: dict[str, Any],
        *,
        valid: bool,
        version_supported: bool,
        version_compatible: bool | None,
        required_version: str | None,
        selected_version: str | None,
        worker_name: str,
    ) -> str | None:
        if valid and (version_compatible is not False or not version_supported):
            return None
        if version_supported and version_compatible is False:
            return self._first_defined(
                payload.get("compatibilityReason"),
                payload.get("versionCompatibilityReason"),
                payload.get("reason"),
                payload.get("message"),
            ) or self._default_version_mismatch_reason(worker_name, required_version, selected_version)
        return self._first_defined(payload.get("reason"), payload.get("message"))

    def _compatibility_sort_key(
        self,
        job: dict[str, Any],
        worker: dict[str, Any],
        response: dict[str, Any],
    ) -> tuple[Any, ...]:
        candidate_version = self._first_defined(response.get("selectedBlenderVersion"), worker.get("blenderVersion"))
        bucket = self._evaluation_bucket({"worker": worker, "response": response}, job)
        candidate_tuple = self._version_tuple(candidate_version)

        priority = 5
        if bucket == SELECTION_BUCKET_EXACT:
            priority = 0
        elif bucket == SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT:
            priority = 1
        elif bucket == SELECTION_BUCKET_SAME_MAJOR_OLDER:
            priority = 2
        elif bucket == SELECTION_BUCKET_HIGHER_MAJOR:
            priority = 3
        elif candidate_tuple:
            priority = 4

        return (
            priority,
            self._version_sort_value(candidate_tuple),
            str(worker.get("name") or worker["workerId"]).lower(),
            str(worker["workerId"]).lower(),
        )

    @staticmethod
    def _infer_version_compatibility(
        *,
        required_version: str | None,
        selected_version: str | None,
    ) -> bool | None:
        required_tuple = WorkerRegistry._version_tuple(required_version)
        selected_tuple = WorkerRegistry._version_tuple(selected_version)
        if not required_tuple or not selected_tuple:
            return None

        bucket = classify_selected_version_bucket(
            required_version,
            selected_version,
            preferred_major=required_tuple[0],
        )
        return bucket in {
            SELECTION_BUCKET_EXACT,
            SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT,
            SELECTION_BUCKET_HIGHER_MAJOR,
        }

    def _evaluation_bucket(self, evaluation: dict[str, Any], job: dict[str, Any]) -> str:
        response = evaluation.get("response") or {}
        worker = evaluation.get("worker") or {}
        bucket = self._first_defined(response.get("selectedVersionBucket"), response.get("versionBucket"))
        if bucket:
            return str(bucket)
        required_version = self._first_defined(
            response.get("requiredBlenderVersion"),
            job.get("requiredBlenderVersion"),
            response.get("blendVersionDetected"),
            job.get("blendVersionDetected"),
        )
        selected_version = self._first_defined(response.get("selectedBlenderVersion"), worker.get("blenderVersion"))
        preferred_major = self._coerce_int(
            self._first_defined(response.get("preferredBlenderMajor"), job.get("preferredBlenderMajor"))
        )
        if preferred_major is None:
            preferred_major = self._version_major(required_version)
        return classify_selected_version_bucket(
            required_version,
            selected_version,
            preferred_major=preferred_major,
        )

    def _default_discovery_hosts(self) -> list[str]:
        hosts = {"127.0.0.1"}
        local_ips = self._local_ipv4_addresses()
        for raw_ip in local_ips:
            try:
                address = ipaddress.ip_address(raw_ip)
            except ValueError:
                continue
            if not (address.is_private or address.is_loopback):
                continue
            network = ipaddress.ip_network(f"{raw_ip}/24", strict=False)
            for host in network.hosts():
                host_value = str(host)
                if host_value in local_ips:
                    continue
                hosts.add(host_value)
        return sorted(hosts)

    def _local_ipv4_addresses(self) -> set[str]:
        addresses: set[str] = set()
        hostname = socket.gethostname()
        try:
            _, _, host_ips = socket.gethostbyname_ex(hostname)
            addresses.update(host_ips)
        except socket.gaierror:
            pass
        try:
            addresses.add(socket.gethostbyname(hostname))
        except socket.gaierror:
            pass
        return {ip for ip in addresses if "." in ip}

    def consolidate_workers(self) -> None:
        workers = self.list_workers()
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for worker in workers:
            groups.setdefault(self._worker_group_key(worker), []).append(worker)

        for group_workers in groups.values():
            if len(group_workers) < 2:
                continue
            survivor = self._select_survivor(group_workers)
            merged = dict(survivor)
            for duplicate in group_workers:
                if duplicate["workerId"] == survivor["workerId"]:
                    continue
                merged = self._merge_worker_rows(merged, duplicate)
                self.database.execute(
                    "UPDATE job_frames SET workerId = ? WHERE workerId = ?",
                    (survivor["workerId"], duplicate["workerId"]),
                )
                self.database.execute("DELETE FROM workers WHERE workerId = ?", (duplicate["workerId"],))
            self._update_worker_row_direct(merged)

    def _find_existing_worker(self, data: dict[str, Any]) -> dict[str, Any] | None:
        worker_id = data.get("workerId")
        if worker_id:
            existing = self.get_worker(worker_id)
            if existing:
                return existing

        port = int(data["port"])
        normalized_ip = self._normalize_host_alias(str(data["ip"]))
        local_identity_hosts = self._local_identity_hosts()

        candidates = [worker for worker in self.list_workers() if int(worker["port"]) == port]
        exact_matches = [worker for worker in candidates if self._normalize_host_alias(worker["ip"]) == normalized_ip]
        if exact_matches:
            return self._select_survivor(exact_matches)

        if normalized_ip in local_identity_hosts:
            local_matches = [worker for worker in candidates if self._normalize_host_alias(worker["ip"]) in local_identity_hosts]
            if local_matches:
                return self._select_survivor(local_matches)
        return None

    def _worker_group_key(self, worker: dict[str, Any]) -> tuple:
        normalized_ip = self._normalize_host_alias(worker["ip"])
        port = int(worker["port"])
        if normalized_ip in self._local_identity_hosts():
            return ("local-machine", port)
        return ("endpoint", normalized_ip, port)

    def _select_survivor(self, workers: list[dict[str, Any]]) -> dict[str, Any]:
        def rank(worker: dict[str, Any]) -> tuple:
            normalized_ip = self._normalize_host_alias(worker["ip"])
            is_loopback = 1 if normalized_ip == "127.0.0.1" else 0
            status_rank = 0 if worker.get("status") in ONLINE_WORKER_STATUSES else 1
            last_seen = str(worker.get("lastSeen") or "")
            created_at = str(worker.get("createdAt") or "")
            return (status_rank, is_loopback, -len(worker.get("ip") or ""), created_at, last_seen)

        return sorted(workers, key=rank)[0]

    def _merge_worker_rows(self, primary: dict[str, Any], duplicate: dict[str, Any]) -> dict[str, Any]:
        merged = dict(primary)
        merged["ip"] = self._choose_preferred_ip(primary.get("ip"), duplicate.get("ip"))
        merged["name"] = self._choose_preferred_name(primary, duplicate)
        for field in [
            "status",
            "blenderFound",
            "blenderVersion",
            "gpu",
            "renderDevicePreference",
            "effectiveUseGpu",
            "currentJobId",
            "currentBlockId",
            "currentFrame",
            "currentAssignmentId",
            "progress",
            "persistentSessionEnabled",
            "sessionState",
            "sessionQueueDepth",
            "sessionDrainRequested",
            "queuedAssignments",
            "lastSeen",
            "lastLog",
            "error",
            "blendAccessible",
            "outputAccessible",
            "lastJob",
            "capabilities",
        ]:
            merged[field] = self._prefer_field_value(merged.get(field), duplicate.get(field), field_name=field)
        return merged

    def _update_worker_row_direct(self, worker: dict[str, Any]) -> None:
        payload = {
            "workerId": worker["workerId"],
            "name": worker.get("name"),
            "ip": self._normalize_host_alias(str(worker["ip"])),
            "port": int(worker["port"]),
            "enabled": 1 if normalize_bool(worker.get("enabled", True), True) else 0,
            "status": worker.get("status", WorkerStatus.UNKNOWN.value),
            "blenderFound": worker.get("blenderFound"),
            "blenderVersion": worker.get("blenderVersion"),
            "gpu": worker.get("gpu"),
            "renderDevicePreference": worker.get("renderDevicePreference"),
            "effectiveUseGpu": worker.get("effectiveUseGpu"),
            "currentJobId": worker.get("currentJobId"),
            "currentBlockId": worker.get("currentBlockId"),
            "currentFrame": worker.get("currentFrame"),
            "currentAssignmentId": worker.get("currentAssignmentId"),
            "progress": worker.get("progress"),
            "persistentSessionEnabled": worker.get("persistentSessionEnabled"),
            "sessionState": worker.get("sessionState"),
            "sessionQueueDepth": worker.get("sessionQueueDepth"),
            "sessionDrainRequested": worker.get("sessionDrainRequested"),
            "queuedAssignments": json.dumps(worker.get("queuedAssignments")) if isinstance(worker.get("queuedAssignments"), list) else worker.get("queuedAssignments"),
            "lastSeen": worker.get("lastSeen"),
            "lastLog": worker.get("lastLog"),
            "error": worker.get("error"),
            "blendAccessible": worker.get("blendAccessible"),
            "outputAccessible": worker.get("outputAccessible"),
            "lastJob": json.dumps(worker.get("lastJob")) if isinstance(worker.get("lastJob"), dict) else worker.get("lastJob"),
            "capabilities": json.dumps(worker.get("capabilities")) if isinstance(worker.get("capabilities"), dict) else worker.get("capabilities"),
            "createdAt": worker.get("createdAt"),
            "updatedAt": utc_now_iso(),
        }
        self.database.execute(
            """
            UPDATE workers
            SET name = :name,
                ip = :ip,
                port = :port,
                enabled = :enabled,
                status = :status,
                blenderFound = :blenderFound,
                blenderVersion = :blenderVersion,
                gpu = :gpu,
                renderDevicePreference = :renderDevicePreference,
                effectiveUseGpu = :effectiveUseGpu,
                currentJobId = :currentJobId,
                currentBlockId = :currentBlockId,
                currentFrame = :currentFrame,
                currentAssignmentId = :currentAssignmentId,
                progress = :progress,
                persistentSessionEnabled = :persistentSessionEnabled,
                sessionState = :sessionState,
                sessionQueueDepth = :sessionQueueDepth,
                sessionDrainRequested = :sessionDrainRequested,
                queuedAssignments = :queuedAssignments,
                lastSeen = :lastSeen,
                lastLog = :lastLog,
                error = :error,
                blendAccessible = :blendAccessible,
                outputAccessible = :outputAccessible,
                lastJob = :lastJob,
                capabilities = :capabilities,
                updatedAt = :updatedAt
            WHERE workerId = :workerId
            """,
            payload,
        )

    def _local_identity_hosts(self) -> set[str]:
        return {"127.0.0.1", *{self._normalize_host_alias(ip) for ip in self._local_ipv4_addresses()}}

    def _is_worker_port_open(self, host: str, port: int) -> bool:
        timeout_seconds = min(max(float(self.settings.discovery_timeout_seconds), 0.1), 0.5)
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                return True
        except OSError:
            return False

    def _choose_preferred_ip(self, current_ip: str | None, new_ip: str | None) -> str:
        current = self._normalize_host_alias(current_ip) if current_ip else None
        new = self._normalize_host_alias(new_ip) if new_ip else None
        if not current:
            return new or "127.0.0.1"
        if not new:
            return current
        if current == new:
            return current
        if current == "127.0.0.1" and new != "127.0.0.1":
            return new
        return current

    def _choose_preferred_name(self, primary: dict[str, Any], duplicate: dict[str, Any]) -> str | None:
        primary_name = primary.get("name")
        duplicate_name = duplicate.get("name")
        duplicate_default = f"{duplicate.get('ip')}:{duplicate.get('port')}"
        primary_default = f"{primary.get('ip')}:{primary.get('port')}"
        if primary_name and primary_name != primary_default:
            return primary_name
        if duplicate_name and duplicate_name != duplicate_default:
            return duplicate_name
        return primary_name or duplicate_name

    def _prefer_field_value(self, current: Any, candidate: Any, *, field_name: str) -> Any:
        if field_name in {"lastSeen", "createdAt", "updatedAt"}:
            if not current:
                return candidate
            if not candidate:
                return current
            return max(str(current), str(candidate))
        if field_name in {"status"}:
            if current in ONLINE_WORKER_STATUSES:
                return current
            if candidate in ONLINE_WORKER_STATUSES:
                return candidate
        if (current is None or current == "" or current == 0) and (candidate is not None and candidate != ""):
            return candidate
        return current

    def _device_filter_rejection_reason(self, job: dict[str, Any], worker: dict[str, Any]) -> str:
        if normalize_bool(job.get("gpuOnlyWorkers"), False):
            worker_name = str(worker.get("name") or worker.get("workerId") or "worker")
            preference = str(worker.get("renderDevicePreference") or "CPU/AUTO").strip() or "CPU/AUTO"
            return f"Job requires GPU-only workers; {worker_name} is currently configured as {preference}."
        return "Worker does not match the device filter required by this job."

    @staticmethod
    def _normalize_host_alias(host: str | None) -> str:
        if not host:
            return "127.0.0.1"
        value = host.strip().lower()
        if value == "localhost":
            return "127.0.0.1"
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            pass
        try:
            resolved = socket.gethostbyname(value)
            if resolved and "." in resolved:
                return resolved
        except socket.gaierror:
            pass
        return value

    @staticmethod
    def _make_worker_id(host: str, port: int) -> str:
        sanitized_host = host.replace(".", "_").replace(":", "_")
        return f"worker_{sanitized_host}_{port}"

    @staticmethod
    def _normalize_worker_status(status_payload: dict[str, Any]) -> str:
        value = str(status_payload.get("status") or status_payload.get("state") or "unknown").strip().lower()
        if value == "cancelling":
            return WorkerStatus.BUSY.value
        if value in {"idle", "ready", "online", "rendering", "busy", "offline", "error", "queued", "session_open", "draining", "closing"}:
            return value
        if status_payload.get("currentFrame") is not None:
            return WorkerStatus.RENDERING.value
        return WorkerStatus.UNKNOWN.value

    def _is_within_status_grace(self, last_seen: Any) -> bool:
        if not last_seen:
            return False
        try:
            observed = datetime.fromisoformat(str(last_seen))
        except ValueError:
            return False
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - observed).total_seconds()
        return elapsed <= float(self.settings.worker_status_grace_seconds)

    @staticmethod
    def _first_defined(*values: Any) -> Any:
        for value in values:
            if value is not None:
                return value
        return None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None:
            return None
        return 1 if normalize_bool(value) else 0

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value in {None, ""}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _response_flag(payload: dict[str, Any], keys: list[str], default: bool | None) -> bool | None:
        for key in keys:
            if key in payload:
                return normalize_bool(payload.get(key))
        return default

    @staticmethod
    def _extract_version_compatibility(payload: dict[str, Any], *, version_supported: bool) -> bool | None:
        direct = WorkerRegistry._response_flag(payload, ["versionCompatible", "blenderVersionCompatible"], default=None)
        if direct is not None:
            return direct
        if version_supported and "compatible" in payload:
            return normalize_bool(payload.get("compatible"))
        return None

    @staticmethod
    def _default_version_mismatch_reason(
        worker_name: str,
        required_version: str | None,
        selected_version: str | None,
    ) -> str:
        if required_version and selected_version:
            return (
                f"Worker '{worker_name}' selected Blender {selected_version}, "
                f"but the job requires {required_version} or a compatible higher version."
            )
        if required_version:
            return f"Worker '{worker_name}' is not compatible with the required Blender version ({required_version})."
        return f"Worker '{worker_name}' reported an incompatible Blender version."

    @staticmethod
    def _version_tuple(value: Any) -> tuple[int, ...] | None:
        if value in {None, ""}:
            return None
        match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(value))
        if not match:
            return None
        parts = [segment for segment in match.groups() if segment is not None]
        return tuple(int(part) for part in parts)

    @staticmethod
    def _version_major(value: Any) -> int | None:
        parsed = WorkerRegistry._version_tuple(value)
        return parsed[0] if parsed else None

    @staticmethod
    def _version_sort_value(value: tuple[int, ...] | None) -> tuple[int, int, int]:
        if not value:
            return (999, 999, 999)
        padded = list(value[:3])
        while len(padded) < 3:
            padded.append(0)
        return tuple(padded[:3])

    @staticmethod
    def _base_url(worker: dict[str, Any]) -> str:
        return f"http://{worker['ip']}:{worker['port']}"

    @staticmethod
    def _deserialize_worker(worker: dict[str, Any]) -> dict[str, Any]:
        payload = dict(worker)
        payload["progress"] = WorkerRegistry._normalize_progress_percent(payload.get("progress")) or 0.0
        capabilities = payload.get("capabilities")
        if isinstance(capabilities, str) and capabilities.strip():
            try:
                payload["capabilities"] = json.loads(capabilities)
            except ValueError:
                pass
        last_job = payload.get("lastJob")
        if isinstance(last_job, str) and last_job.strip():
            try:
                payload["lastJob"] = json.loads(last_job)
            except ValueError:
                pass
        queued_assignments = payload.get("queuedAssignments")
        if isinstance(queued_assignments, str) and queued_assignments.strip():
            try:
                payload["queuedAssignments"] = json.loads(queued_assignments)
            except ValueError:
                pass
        return payload

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
