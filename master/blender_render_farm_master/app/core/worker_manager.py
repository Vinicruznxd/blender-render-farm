from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import time
from typing import Iterable
from uuid import uuid4

from app.core.models import Worker, normalize_progress_percent, normalize_render_device_preference, payload_value
from app.services.logger import AppLogger
from app.services.worker_client import WorkerClient, WorkerClientError


@dataclass
class WorkerTransition:
    worker_id: str
    previous_status: str
    current_status: str
    was_online: bool
    is_online: bool
    newly_online: bool
    went_offline: bool


class WorkerManager:
    def __init__(self, workers: Iterable[Worker] | None = None) -> None:
        self._workers: dict[str, Worker] = {}
        for worker in workers or []:
            self._workers[worker.id] = worker

    def list_workers(self) -> list[Worker]:
        return list(self._workers.values())

    def get_worker(self, worker_id: str) -> Worker | None:
        return self._workers.get(worker_id)

    def add_worker(self, name: str, ip: str, port: int, enabled: bool = True) -> Worker:
        worker = Worker(
            id=uuid4().hex,
            name=name or f"Worker-{ip}:{port}",
            ip=ip,
            port=port,
            enabled=enabled,
            selected=True,
            status="offline" if enabled else "disabled",
        )
        self._workers[worker.id] = worker
        return worker

    def remove_worker(self, worker_id: str) -> Worker | None:
        return self._workers.pop(worker_id, None)

    def update_selected(self, worker_id: str, selected: bool) -> None:
        worker = self.get_worker(worker_id)
        if worker:
            worker.selected = selected

    def selected_workers(self) -> list[Worker]:
        return [worker for worker in self._workers.values() if worker.selected and worker.enabled]

    def _needs_capabilities(self, status_payload: dict) -> bool:
        return (
            "blenderFound" not in status_payload
            or not status_payload.get("gpu")
            or "renderDevicePreference" not in status_payload
            or "effectiveUseGpu" not in status_payload
        )

    def _apply_worker_payload(
        self,
        worker: Worker,
        status_payload: dict,
        capabilities_payload: dict,
        now: float,
    ) -> None:
        machine_name = payload_value(status_payload, key="machineName")
        if machine_name is not None:
            worker.name = str(machine_name)

        status = payload_value(status_payload, key="status")
        if status is not None:
            worker.status = str(status)
        else:
            worker.status = "idle"

        blender_found = payload_value(status_payload, capabilities_payload, key="blenderFound")
        if blender_found is not None:
            worker.blenderFound = bool(blender_found)

        blender_version = payload_value(status_payload, capabilities_payload, key="blenderVersion")
        if blender_version is not None:
            worker.blenderVersion = str(blender_version)

        gpu = payload_value(status_payload, capabilities_payload, key="gpu")
        if gpu is not None:
            worker.gpu = str(gpu)

        render_device_preference = normalize_render_device_preference(
            payload_value(status_payload, capabilities_payload, key="renderDevicePreference")
        )
        if render_device_preference is not None:
            worker.renderDevicePreference = render_device_preference

        effective_use_gpu = payload_value(status_payload, capabilities_payload, key="effectiveUseGpu")
        if effective_use_gpu is not None:
            worker.effectiveUseGpu = bool(effective_use_gpu)

        current_job_id = payload_value(status_payload, key="jobId")
        if current_job_id is None:
            current_job_id = payload_value(status_payload, key="currentJob")
        worker.currentJobId = current_job_id

        current_block_id = payload_value(status_payload, key="blockId")
        if current_block_id is None:
            current_block_id = payload_value(status_payload, key="currentBlock")
        worker.currentBlockId = current_block_id

        current_frame = payload_value(status_payload, key="currentFrame")
        if current_frame is not None:
            worker.currentFrame = int(current_frame)
        elif worker.status != "rendering":
            worker.currentFrame = None

        progress = payload_value(status_payload, key="progress")
        if progress is not None:
            worker.progress = float(normalize_progress_percent(progress) or 0.0)
        elif worker.status != "rendering":
            worker.progress = 0.0

        last_log = payload_value(status_payload, key="lastLog")
        if last_log is not None:
            worker.lastLog = str(last_log)

        worker.error = payload_value(status_payload, key="error")
        worker.lastSeen = now

    def poll_workers(
        self,
        client: WorkerClient,
        timeout_seconds: int,
        logger: AppLogger,
    ) -> list[WorkerTransition]:
        transitions: list[WorkerTransition] = []
        now = time.time()
        workers = self.list_workers()
        enabled_workers = [worker for worker in workers if worker.enabled]

        for worker in workers:
            if not worker.enabled:
                previous_status = worker.status
                worker.status = "disabled"
                transitions.append(
                    WorkerTransition(
                        worker_id=worker.id,
                        previous_status=previous_status,
                        current_status=worker.status,
                        was_online=previous_status not in {"offline", "disabled"},
                        is_online=False,
                        newly_online=False,
                        went_offline=previous_status not in {"offline", "disabled"},
                    )
                )

        if not enabled_workers:
            return transitions

        def fetch(worker: Worker) -> tuple[str, dict, dict]:
            status_payload = client.status(worker)
            capabilities_payload: dict = {}
            if self._needs_capabilities(status_payload):
                try:
                    capabilities_payload = client.capabilities(worker)
                except WorkerClientError:
                    capabilities_payload = {}
            return worker.id, status_payload, capabilities_payload

        max_workers = min(8, max(1, len(enabled_workers)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(fetch, worker): worker for worker in enabled_workers}
            for future in as_completed(future_map):
                worker = future_map[future]
                previous_status = worker.status
                was_online = worker.is_online
                try:
                    _, status_payload, capabilities_payload = future.result()
                except Exception as exc:  # noqa: BLE001
                    if worker.lastSeen is None or now - worker.lastSeen >= timeout_seconds:
                        worker.status = "offline"
                    worker.error = str(exc)
                    worker.lastLog = str(exc)
                    logger.warning(
                        f"Falha ao consultar worker {worker.name}: {exc}",
                        worker_id=worker.id,
                    )
                else:
                    self._apply_worker_payload(worker, status_payload, capabilities_payload, now)
                is_online = worker.is_online
                transitions.append(
                    WorkerTransition(
                        worker_id=worker.id,
                        previous_status=previous_status,
                        current_status=worker.status,
                        was_online=was_online,
                        is_online=is_online,
                        newly_online=not was_online and is_online,
                        went_offline=was_online and not is_online,
                    )
                )
        transitions.sort(key=lambda item: item.worker_id)
        return transitions
