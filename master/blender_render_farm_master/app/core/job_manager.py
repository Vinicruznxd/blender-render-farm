from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import threading
import time
from typing import Any

from app.core.block_scheduler import BlockScheduler
from app.core.blend_version import (
    BlendVersionMetadata,
    read_blend_version_metadata,
)
from app.core.frame_scanner import FrameScanner
from app.core.models import (
    RenderBlock,
    RenderDevicePreference,
    RenderJob,
    Worker,
    normalize_progress_percent,
    WorkerPathValidationResult,
    normalize_render_device_preference,
    now_iso,
    payload_value,
)
from app.core.settings import AppSettings
from app.core.version_routing import (
    DEFAULT_VERSION_ROUTING_POLICY,
    PREFER_SAME_MAJOR_ANY_VERSION_POLICY,
    SAME_MAJOR_SELECTION_BUCKETS,
    SELECTION_BUCKET_EXACT,
    SELECTION_BUCKET_HIGHER_MAJOR,
    SELECTION_BUCKET_SAME_MAJOR_OLDER,
    SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT,
    classify_selected_version_bucket,
    normalize_version_routing_policy,
)
from app.core.worker_manager import WorkerManager, WorkerTransition
from app.services.logger import AppLogger
from app.services.persistence import PersistenceService
from app.services.worker_client import WorkerClient, WorkerClientError
from app.services.worker_discovery import DiscoveredWorker, WorkerDiscoveryService


class JobValidationError(Exception):
    """Raised when the requested job is invalid."""


class WorkerPathValidationError(JobValidationError):
    def __init__(
        self,
        message: str,
        results: list[WorkerPathValidationResult],
        hint: str | None = None,
    ) -> None:
        self.message = message
        self.results = results
        self.hint = hint
        super().__init__(self.detailed_text())

    def detailed_text(self) -> str:
        lines = [self.message, ""]
        for index, result in enumerate(self.results):
            if index:
                lines.append("")
            lines.append(result.to_display_text())
        if self.hint:
            lines.extend(["", self.hint])
        return "\n".join(lines)


class JobManager:
    mode = "direct"
    supports_local_jobs = True
    supports_local_job_files = True
    supports_worker_selection = True
    supports_worker_device_update = True
    supports_rescan = True

    def __init__(
        self,
        settings: AppSettings,
        persistence: PersistenceService,
        logger: AppLogger,
        worker_client: WorkerClient | None = None,
        worker_discovery: WorkerDiscoveryService | None = None,
    ) -> None:
        self.settings = settings
        self.persistence = persistence
        self.logger = logger
        self.worker_client = worker_client or WorkerClient(timeout_seconds=5.0)
        self.worker_discovery = worker_discovery or WorkerDiscoveryService(timeout_seconds=0.75)
        self.worker_manager = WorkerManager(settings.build_workers())
        self.frame_scanner = FrameScanner(stability_seconds=2.0)
        self.block_scheduler = BlockScheduler()
        self.current_job: RenderJob | None = None
        self.last_path_validation_results: list[WorkerPathValidationResult] = []
        self._lock = threading.RLock()
        self._scan_interval_seconds = 3.0

    def list_workers(self) -> list[Worker]:
        with self._lock:
            return [Worker.from_dict(worker.to_dict()) for worker in self.worker_manager.list_workers()]

    def add_worker(self, name: str, ip: str, port: int, enabled: bool = True) -> Worker:
        with self._lock:
            worker = self.worker_manager.add_worker(name=name, ip=ip, port=port, enabled=enabled)
            self._save_settings()
            self.logger.info(f"Worker cadastrado: {worker.name} ({worker.ip}:{worker.port})", worker_id=worker.id)
            return Worker.from_dict(worker.to_dict())

    def discover_workers(
        self,
        hosts: list[str] | None = None,
        ports: list[int] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            known_workers = [Worker.from_dict(worker.to_dict()) for worker in self.worker_manager.list_workers()]
            default_port = self.settings.default_worker_port

        run = self.worker_discovery.discover(
            default_port=default_port,
            known_workers=known_workers,
            hosts=hosts,
            ports=ports,
        )

        added_workers: list[Worker] = []
        updated_workers: list[Worker] = []
        with self._lock:
            for item in run.discovered:
                worker = self._find_existing_worker_for_discovery(item)
                if worker is None:
                    worker = self.worker_manager.add_worker(
                        name=item.name,
                        ip=item.ip,
                        port=item.port,
                        enabled=True,
                    )
                    added_workers.append(worker)
                    action_text = "descoberto e cadastrado"
                else:
                    worker.name = item.name or worker.name
                    worker.ip = item.ip
                    worker.port = item.port
                    updated_workers.append(worker)
                    action_text = "redescoberto"

                self._apply_worker_payload(
                    worker,
                    item.status_payload,
                    item.capabilities_payload,
                    item.status_payload,
                )
                self.logger.info(
                    f"Worker {action_text}: {worker.name} ({worker.ip}:{worker.port})",
                    worker_id=worker.id,
                )

            if run.discovered:
                self._save_settings()

            snapshot = self.get_snapshot()

        return {
            "discoveredCount": len(run.discovered),
            "addedCount": len(added_workers),
            "updatedCount": len(updated_workers),
            "scannedCount": len(run.candidates),
            "scannedNetworks": run.scanned_networks,
            "ports": run.ports,
            "workers": [
                {
                    "name": item.name,
                    "ip": item.ip,
                    "port": item.port,
                    "endpoint": item.endpoint,
                }
                for item in run.discovered
            ],
            "snapshot": snapshot,
        }

    def remove_worker(self, worker_id: str) -> None:
        with self._lock:
            worker = self.worker_manager.remove_worker(worker_id)
            if not worker:
                return
            if self.current_job:
                self._requeue_blocks_for_worker(worker.id, "Worker removido da lista.")
            self._save_settings()
            self.logger.warning(f"Worker removido: {worker.name}", worker_id=worker.id)

    def update_worker_selected(self, worker_id: str, selected: bool) -> None:
        with self._lock:
            self.worker_manager.update_selected(worker_id, selected)
            worker = self.worker_manager.get_worker(worker_id)
            if not worker:
                return
            self.logger.info(
                f"Worker {'selecionado' if selected else 'desselecionado'} para jobs: {worker.name}",
                worker_id=worker.id,
            )
            if self.current_job and self.current_job.autoAddWorkers and selected and worker.id not in self.current_job.selectedWorkerIds:
                self.current_job.selectedWorkerIds.append(worker.id)
            self._save_settings()

    def test_worker(self, worker_id: str, blend_file: str | None = None, output_folder: str | None = None) -> dict[str, Any]:
        with self._lock:
            worker = self.worker_manager.get_worker(worker_id)
            if not worker:
                raise JobValidationError("Worker nao encontrado.")
            test_client = WorkerClient(timeout_seconds=5.0)

        result: dict[str, Any] = {}
        result["health"] = test_client.health(worker)
        result["status"] = test_client.status(worker)
        try:
            result["capabilities"] = test_client.capabilities(worker)
        except WorkerClientError:
            result["capabilities"] = {}
        try:
            result["renderDevice"] = test_client.render_device(worker)
        except WorkerClientError:
            result["renderDevice"] = {}
        if blend_file and output_folder:
            result["validate"] = test_client.validate_paths(worker, blend_file, output_folder)

        with self._lock:
            self._apply_worker_payload(
                worker,
                result.get("status", {}),
                result.get("capabilities", {}),
                result.get("renderDevice", {}),
            )
            self._apply_validation_payload(worker, result.get("validate"))
        self.logger.info(f"Teste de conexao concluido para {worker.name}.", worker_id=worker.id)
        return result

    def update_worker_render_device(
        self,
        worker_id: str,
        render_device_preference: RenderDevicePreference,
    ) -> dict[str, Any]:
        normalized_preference = normalize_render_device_preference(render_device_preference)
        if normalized_preference is None:
            raise JobValidationError("Render device invalido. Use CPU ou GPU.")

        with self._lock:
            worker = self.worker_manager.get_worker(worker_id)
            if not worker:
                raise JobValidationError("Worker nao encontrado.")

        result = {
            "set": self.worker_client.set_render_device(worker, normalized_preference),
        }
        try:
            result["renderDevice"] = self.worker_client.render_device(worker)
        except WorkerClientError:
            result["renderDevice"] = result["set"]

        result["status"] = self.worker_client.status(worker)
        try:
            result["capabilities"] = self.worker_client.capabilities(worker)
        except WorkerClientError:
            result["capabilities"] = {}

        with self._lock:
            self._apply_worker_payload(
                worker,
                result.get("status", {}),
                result.get("capabilities", {}),
                result.get("renderDevice", {}),
            )
            self.logger.info(
                f"Render device atualizado para {normalized_preference}.",
                worker_id=worker.id,
            )
        return result

    def start_job(
        self,
        blend_file: str,
        start_frame: int,
        end_frame: int,
        output_folder: str,
        block_size: int,
        ignore_rendered_frames: bool,
        auto_add_workers: bool,
        redistribute_offline_blocks: bool,
        legacy_use_gpu: bool = True,
        version_routing_policy_override: str | None = None,
    ) -> list[str]:
        warnings: list[str] = []
        blend_path = Path(blend_file)
        output_path = Path(output_folder)
        blend_version = read_blend_version_metadata(blend_path)
        with self._lock:
            self._validate_job_inputs(blend_path, start_frame, end_frame, output_path, block_size)
            warnings.extend(self._path_warnings(blend_path))
            output_path.mkdir(parents=True, exist_ok=True)

            selected_workers = self.worker_manager.selected_workers()
            if not selected_workers:
                raise JobValidationError("Selecione pelo menos um worker.")

            online_workers = [worker for worker in selected_workers if worker.is_online]
            if not online_workers:
                raise JobValidationError("Nenhum worker selecionado esta online.")

        version_metadata = self._version_metadata_fields(
            blend_version,
            version_routing_policy_override=version_routing_policy_override,
        )
        evaluations, invalid_results = self._validate_workers_for_submission(
            online_workers,
            blend_path=blend_path,
            output_path=output_path,
            version_metadata=version_metadata,
            log_results=True,
        )
        filtered_evaluations = self._filter_version_evaluations(version_metadata, evaluations)
        valid_workers = [evaluation["worker"] for evaluation in filtered_evaluations if evaluation["valid"]]

        if invalid_results:
            warning_text = self._validation_warning_text(invalid_results)
            warnings.append(warning_text)
            self.logger.warning(
                "Alguns workers falharam na validacao de caminhos e foram ignorados neste job."
            )

        if not valid_workers:
            raise WorkerPathValidationError(
                message="Nenhum worker selecionado conseguiu validar os caminhos.",
                results=invalid_results,
                hint=self._mapped_drive_hint([str(blend_path), str(output_path)], invalid_results),
            )

        missing_frames: list[int]
        with self._lock:
            job_id = self._new_job_id()
            output_pattern = str(output_path / "frame_####")
            job = RenderJob(
                jobId=job_id,
                blendFile=str(blend_path),
                outputFolder=str(output_path),
                outputPattern=output_pattern,
                startFrame=start_frame,
                endFrame=end_frame,
                blockSize=block_size,
                status="running",
                startedAt=now_iso(),
                ignoreRenderedFrames=ignore_rendered_frames,
                autoAddWorkers=auto_add_workers,
                redistributeOfflineBlocks=redistribute_offline_blocks,
                selectedWorkerIds=[worker.id for worker in valid_workers],
                useGpu=legacy_use_gpu,
                requiredBlenderVersion=blend_version.required_blender_version if blend_version else None,
                preferredBlenderMajor=blend_version.preferred_blender_major if blend_version else None,
                versionRoutingPolicy=version_metadata.get("versionRoutingPolicy"),
                blendVersionDetected=version_metadata.get("blendVersionDetected"),
            )
            completed_frames = self.frame_scanner.scan_job(job)
            job.completedFrames = sorted(completed_frames)
            all_frames = set(range(start_frame, end_frame + 1))
            if ignore_rendered_frames:
                missing_frames = sorted(all_frames - completed_frames)
            else:
                missing_frames = sorted(all_frames)
            job.pendingFrames = missing_frames
            self.block_scheduler.populate_job(job, missing_frames)
            if not missing_frames:
                job.status = "completed"
                job.finishedAt = now_iso()
                self.logger.info(f"Job {job.jobId} ja estava concluido na pasta de saida.")
            self.current_job = job
            self.logger.info(f"Job criado: {job.jobId} ({blend_path.name})")
            self._refresh_job_metrics()
            if self.current_job.status == "running":
                self._assign_pending_blocks()
            self._autosave_current_job()
            return warnings

    def preflight_job(
        self,
        blend_file: str,
        start_frame: int,
        end_frame: int,
        output_folder: str,
        block_size: int,
        ignore_rendered_frames: bool,
        auto_add_workers: bool,
        redistribute_offline_blocks: bool,
        legacy_use_gpu: bool = True,
    ) -> dict[str, Any]:
        del ignore_rendered_frames, auto_add_workers, redistribute_offline_blocks, legacy_use_gpu
        blend_path = Path(blend_file)
        output_path = Path(output_folder)
        blend_version = read_blend_version_metadata(blend_path)
        with self._lock:
            self._validate_job_inputs(blend_path, start_frame, end_frame, output_path, block_size)
            output_path.mkdir(parents=True, exist_ok=True)
            selected_workers = self.worker_manager.selected_workers()
            if not selected_workers:
                raise JobValidationError("Selecione pelo menos um worker.")
            online_workers = [worker for worker in selected_workers if worker.is_online]
            if not online_workers:
                raise JobValidationError("Nenhum worker selecionado esta online.")

        if blend_version is None:
            return self._empty_version_preflight()

        version_metadata = self._version_metadata_fields(blend_version)
        evaluations, _ = self._validate_workers_for_submission(
            online_workers,
            blend_path=blend_path,
            output_path=output_path,
            version_metadata=version_metadata,
            log_results=False,
        )
        return self._build_version_preflight_summary(blend_version, version_metadata, evaluations)

    def pause_distribution(self, job_id: str | None = None) -> None:
        with self._lock:
            if not self.current_job:
                return
            self.current_job.status = "paused"
            self.logger.info(f"Distribuicao pausada para o job {self.current_job.jobId}.")
            self._autosave_current_job()

    def resume_distribution(self, job_id: str | None = None) -> None:
        with self._lock:
            if not self.current_job:
                return
            if self.current_job.status in {"completed", "cancelled"}:
                return
            self.current_job.status = "running"
            self.logger.info(f"Distribuicao retomada para o job {self.current_job.jobId}.")
            self._assign_pending_blocks()
            self._autosave_current_job()

    def cancel_job(self, job_id: str | None = None) -> None:
        with self._lock:
            job = self.current_job
            if not job:
                return
            job.status = "cancelled"
            job.finishedAt = now_iso()
            active_blocks = [block for block in job.blocks if block.status in {"assigned", "rendering"}]
            for block in active_blocks:
                worker = self.worker_manager.get_worker(block.workerId or "")
                if worker:
                    try:
                        self.worker_client.cancel(worker, job.jobId)
                    except WorkerClientError as exc:
                        self.logger.warning(
                            f"Falha ao cancelar no worker {worker.name}: {exc}",
                            worker_id=worker.id,
                            block_id=block.blockId,
                        )
                block.status = "cancelled"
                block.finishedAt = now_iso()
                block.errorMessage = "Job cancelado pelo usuario."
            self.logger.warning(f"Job {job.jobId} cancelado.")
            self._refresh_job_metrics()
            self._autosave_current_job()

    def save_current_job(self) -> Path:
        with self._lock:
            if not self.current_job:
                raise JobValidationError("Nao ha job atual para salvar.")
            path = self.persistence.save_job(self.current_job)
            self.logger.info(f"Job salvo em {path}.")
            return path

    def load_job(self, path: Path) -> None:
        with self._lock:
            job = self.persistence.load_job(path)
            completed_frames = self.frame_scanner.scan_job(job)
            self._rebuild_job_for_resume(job, completed_frames)
            self.current_job = job
            self.logger.info(f"Job carregado: {job.jobId}")
            self._autosave_current_job()

    def list_job_files(self) -> list[Path]:
        return self.persistence.list_job_files()

    def force_rescan(self) -> None:
        with self._lock:
            if not self.current_job:
                return
            completed_frames = self.frame_scanner.scan_job(self.current_job)
            self._apply_completed_frames(completed_frames)
            self._refresh_job_metrics()
            self._autosave_current_job()
            self.logger.info(f"Reescaneamento concluido para o job {self.current_job.jobId}.")

    def update_job_options(self, auto_add_workers: bool, redistribute_offline_blocks: bool) -> None:
        with self._lock:
            if not self.current_job:
                return
            self.current_job.autoAddWorkers = auto_add_workers
            self.current_job.redistributeOfflineBlocks = redistribute_offline_blocks
            self._autosave_current_job()

    def tick(self) -> dict[str, Any]:
        with self._lock:
            transitions = self.worker_manager.poll_workers(
                client=self.worker_client,
                timeout_seconds=self.settings.worker_timeout_seconds,
                logger=self.logger,
            )

            if self.current_job:
                self._sync_job_with_workers(transitions)

            snapshot = self.get_snapshot()
            return snapshot

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            workers = [worker.to_dict() for worker in self.worker_manager.list_workers()]
            job_dict = self.current_job.to_dict() if self.current_job else None
            return {
                "workers": workers,
                "job": job_dict,
                "metrics": self._build_metrics(),
                "logs": {
                    "general": self.logger.general_text(),
                },
            }

    def _sync_job_with_workers(self, transitions: list[WorkerTransition]) -> None:
        if not self.current_job:
            return
        job = self.current_job
        now = time.time()

        if job.lastScanAt is None or now - job.lastScanAt >= self._scan_interval_seconds:
            completed_frames = self.frame_scanner.scan_job(job)
            self._apply_completed_frames(completed_frames)
            job.lastScanAt = now

        self._reconcile_active_blocks()

        if job.status == "running":
            self._assign_pending_blocks(transitions)

        self._refresh_job_metrics()
        self._autosave_current_job()

    def _reconcile_active_blocks(self) -> None:
        if not self.current_job:
            return

        job = self.current_job
        completed_frames = set(job.completedFrames)
        for block in job.blocks:
            if block.status not in {"assigned", "rendering"}:
                continue

            if self.block_scheduler.block_is_complete(block, completed_frames):
                self._mark_block_done(block)
                continue

            worker = self.worker_manager.get_worker(block.workerId or "")
            if not worker:
                self._schedule_block_recovery(block, "Worker inexistente.")
                continue

            if worker.currentJobId == job.jobId and worker.currentBlockId == block.blockId:
                if worker.status == "rendering":
                    block.status = "rendering"
                    if not block.startedAt:
                        block.startedAt = now_iso()
                    worker.progress = self._estimate_worker_progress(worker, block)
                    block.verificationDueAt = None
                    continue
                self._schedule_block_recovery(
                    block,
                    "Worker finalizou ou perdeu o bloco antes da confirmacao dos frames.",
                )
                continue

            if worker.status == "rendering":
                self._schedule_block_recovery(
                    block,
                    "Worker nao confirma mais o bloco ativo.",
                )
                continue

            if not worker.is_online:
                if job.redistributeOfflineBlocks:
                    self._schedule_block_recovery(block, "Worker offline.")
                continue

            if worker.status in {"idle", "error"}:
                self._schedule_block_recovery(
                    block,
                    worker.error or "Worker liberado sem concluir o bloco.",
                )

    def _schedule_block_recovery(self, block: RenderBlock, reason: str) -> None:
        due_time = time.time() + self.frame_scanner.stability_seconds + 1.0
        if block.verificationDueAt is None:
            block.verificationDueAt = due_time
            block.errorMessage = reason
            self.logger.warning(
                f"Aguardando confirmacao do bloco {block.blockId} antes de reenfileirar.",
                worker_id=block.workerId,
                block_id=block.blockId,
            )
            return
        if time.time() < block.verificationDueAt:
            return
        self._recover_block(block, reason)

    def _recover_block(self, block: RenderBlock, reason: str) -> None:
        if not self.current_job:
            return
        job = self.current_job
        completed_frames = set(job.completedFrames)
        if self.block_scheduler.block_is_complete(block, completed_frames):
            self._mark_block_done(block)
            return

        missing_frames = self.block_scheduler.missing_frames(block, completed_frames)
        if not missing_frames:
            self._mark_block_done(block)
            return

        self._release_worker_for_block(block)

        if block.attemptCount <= self.settings.max_block_retries:
            block.status = "cancelled"
            block.finishedAt = now_iso()
            block.errorMessage = reason
            block.verificationDueAt = None
            new_blocks = self.block_scheduler.add_retry_blocks(
                job,
                missing_frames,
                carry_attempt_count=block.attemptCount,
            )
            self.logger.warning(
                f"Bloco {block.blockId} reenfileirado em {len(new_blocks)} bloco(s).",
                worker_id=block.workerId,
                block_id=block.blockId,
            )
        else:
            block.status = "error"
            block.finishedAt = now_iso()
            block.errorMessage = reason
            block.verificationDueAt = None
            self.logger.error(
                f"Bloco {block.blockId} excedeu o limite de retries.",
                worker_id=block.workerId,
                block_id=block.blockId,
            )

    def _mark_block_done(self, block: RenderBlock) -> None:
        if block.status == "done":
            return
        block.status = "done"
        block.finishedAt = block.finishedAt or now_iso()
        block.verificationDueAt = None
        self._release_worker_for_block(block, mark_idle=True)
        self.logger.info(
            f"Bloco concluido: {block.startFrame}-{block.endFrame}",
            worker_id=block.workerId,
            block_id=block.blockId,
        )

    def _assign_pending_blocks(self, transitions: list[WorkerTransition] | None = None) -> None:
        if not self.current_job or self.current_job.status != "running":
            return

        job = self.current_job
        active_transition_ids = {
            item.worker_id for item in transitions or [] if item.newly_online
        }
        completed_frames = set(job.completedFrames)
        version_metadata = {
            "requiredBlenderVersion": job.requiredBlenderVersion,
            "preferredBlenderMajor": job.preferredBlenderMajor,
            "versionRoutingPolicy": normalize_version_routing_policy(job.versionRoutingPolicy),
            "blendVersionDetected": job.blendVersionDetected or job.requiredBlenderVersion,
        }
        candidate_evaluations: list[dict[str, Any]] = []

        for worker in self.worker_manager.list_workers():
            if not self._worker_can_receive_block(worker, active_transition_ids):
                continue

            if not self._ensure_worker_paths(worker):
                continue
            candidate_evaluations.append(
                {
                    "worker": worker,
                    "payload": dict(worker.lastValidationPayload),
                    "valid": True,
                }
            )

        eligible_workers = [
            evaluation["worker"]
            for evaluation in self._filter_version_evaluations(version_metadata, candidate_evaluations)
            if evaluation.get("valid")
        ]

        for worker in eligible_workers:
            next_block = self.block_scheduler.next_pending_block(job)
            while next_block and self.block_scheduler.block_is_complete(next_block, completed_frames):
                self._mark_block_done(next_block)
                completed_frames = set(job.completedFrames)
                next_block = self.block_scheduler.next_pending_block(job)

            if not next_block:
                break

            next_block = self.block_scheduler.claim_pending_block(job, next_block, frame_count=1)

            payload = {
                "jobId": job.jobId,
                "blockId": next_block.blockId,
                "blendFile": job.blendFile,
                "outputPattern": job.outputPattern,
                "startFrame": next_block.startFrame,
                "endFrame": next_block.endFrame,
                "format": job.format,
                # Legacy compatibility only. The worker local setting wins.
                "useGpu": job.useGpu,
            }
            for field in ("requiredBlenderVersion", "preferredBlenderMajor", "versionRoutingPolicy", "blendVersionDetected"):
                value = version_metadata.get(field)
                if value is not None:
                    payload[field] = value
            try:
                self.worker_client.render_block(worker, payload)
            except WorkerClientError as exc:
                worker.error = str(exc)
                worker.lastLog = str(exc)
                self.logger.warning(
                    f"Falha ao enviar bloco {next_block.blockId} para {worker.name}: {exc}",
                    worker_id=worker.id,
                    block_id=next_block.blockId,
                )
                continue

            next_block.status = "assigned"
            next_block.workerId = worker.id
            next_block.assignedAt = now_iso()
            next_block.startedAt = None
            next_block.finishedAt = None
            next_block.verificationDueAt = None
            next_block.attemptCount += 1
            worker.currentJobId = job.jobId
            worker.currentBlockId = next_block.blockId
            worker.currentFrame = next_block.startFrame
            worker.progress = 0.0
            worker.status = "rendering"
            self.logger.info(
                f"Bloco {next_block.blockId} enviado para {worker.name}.",
                worker_id=worker.id,
                block_id=next_block.blockId,
            )

    def _estimate_worker_progress(self, worker: Worker, block: RenderBlock) -> float:
        reported = float(worker.progress or 0.0)
        if block.endFrame <= block.startFrame:
            return max(reported, 50.0 if worker.status == "rendering" else 0.0)
        if worker.currentFrame is None:
            return reported

        bounded_frame = min(max(worker.currentFrame, block.startFrame), block.endFrame)
        completed_portion = (bounded_frame - block.startFrame + 0.5) / max(1, len(block.frames()))
        estimated = max(1.0, min(99.0, completed_portion * 100.0))
        return max(reported, estimated)

    def _release_worker_for_block(self, block: RenderBlock, mark_idle: bool = False) -> None:
        worker = self.worker_manager.get_worker(block.workerId or "")
        if not worker:
            return
        if worker.currentBlockId != block.blockId:
            return

        worker.currentBlockId = None
        worker.currentJobId = None
        worker.currentFrame = None
        worker.progress = 0.0
        if mark_idle and worker.is_online:
            worker.status = "idle"

    def _worker_can_receive_block(self, worker: Worker, newly_online_ids: set[str]) -> bool:
        if not self.current_job:
            return False
        if not worker.enabled or not worker.selected:
            return False
        if not worker.is_available:
            return False
        if not worker.blenderFound:
            return False
        if not self.current_job.autoAddWorkers and worker.id not in self.current_job.selectedWorkerIds:
            return False
        if worker.id in newly_online_ids and not self.current_job.autoAddWorkers and worker.id not in self.current_job.selectedWorkerIds:
            return False
        return True

    def _ensure_worker_paths(self, worker: Worker) -> bool:
        if not self.current_job:
            return False
        now = time.time()
        version_metadata = {
            "requiredBlenderVersion": self.current_job.requiredBlenderVersion,
            "preferredBlenderMajor": self.current_job.preferredBlenderMajor,
            "versionRoutingPolicy": normalize_version_routing_policy(self.current_job.versionRoutingPolicy),
            "blendVersionDetected": self.current_job.blendVersionDetected or self.current_job.requiredBlenderVersion,
        }
        if (
            worker.blendAccessible
            and worker.outputAccessible
            and worker.lastValidatedAt
            and now - worker.lastValidatedAt < 15
            and self._validation_payload_matches_job(worker.lastValidationPayload, version_metadata)
        ):
            return True
        try:
            payload = self.worker_client.validate_paths(
                worker,
                self.current_job.blendFile,
                self.current_job.outputFolder,
                required_blender_version=version_metadata.get("requiredBlenderVersion"),
                preferred_blender_major=version_metadata.get("preferredBlenderMajor"),
                version_routing_policy=version_metadata.get("versionRoutingPolicy"),
                blend_version_detected=version_metadata.get("blendVersionDetected"),
            )
        except WorkerClientError as exc:
            worker.error = str(exc)
            self.logger.warning(
                f"Validacao de paths falhou em {worker.name}: {exc}",
                worker_id=worker.id,
            )
            return False
        result = self._apply_validation_payload(worker, payload)
        if not result.ok:
            self._log_validation_result(result, worker_id=worker.id)
        return bool(result.ok)

    def _requeue_blocks_for_worker(self, worker_id: str, reason: str) -> None:
        if not self.current_job:
            return
        for block in self.current_job.blocks:
            if block.workerId != worker_id or block.status not in {"assigned", "rendering"}:
                continue
            self._recover_block(block, reason)

    def _apply_completed_frames(self, completed_frames: set[int]) -> None:
        if not self.current_job:
            return
        job = self.current_job
        job.completedFrames = sorted(completed_frames)
        for block in job.blocks:
            if self.block_scheduler.block_is_complete(block, completed_frames):
                self._mark_block_done(block)

    def _refresh_job_metrics(self) -> None:
        if not self.current_job:
            return
        metrics = self._build_metrics()
        self.current_job.pendingFrames = metrics["pending_frame_list"]
        self.current_job.errorFrames = metrics["error_frame_list"]
        if metrics["completed_frames"] >= metrics["total_frames"] and metrics["total_frames"] > 0:
            self.current_job.status = "completed"
            self.current_job.finishedAt = self.current_job.finishedAt or now_iso()
            return
        if metrics["error_frames"] and not metrics["pending_frames"] and not metrics["rendering_frames"]:
            self.current_job.status = "error"
            self.current_job.finishedAt = self.current_job.finishedAt or now_iso()

    def _build_metrics(self) -> dict[str, Any]:
        if not self.current_job:
            return {
                "job_name": "",
                "blend_file": "",
                "total_frames": 0,
                "completed_frames": 0,
                "pending_frames": 0,
                "rendering_frames": 0,
                "error_frames": 0,
                "overall_percent": 0.0,
                "elapsed_seconds": 0.0,
                "eta_seconds": None,
                "machines_online": sum(1 for worker in self.worker_manager.list_workers() if worker.is_online),
                "machines_rendering": sum(
                    1 for worker in self.worker_manager.list_workers() if worker.status == "rendering"
                ),
                "pending_frame_list": [],
                "error_frame_list": [],
            }

        job = self.current_job
        all_frames = set(range(job.startFrame, job.endFrame + 1))
        completed = set(job.completedFrames)
        error = set()
        in_progress = set()

        for block in job.blocks:
            missing_frames = set(self.block_scheduler.missing_frames(block, completed))
            if not missing_frames:
                continue
            if block.status in {"assigned", "rendering"}:
                in_progress.update(missing_frames)
            elif block.status == "error":
                error.update(missing_frames)

        pending = all_frames - completed - error - in_progress
        total_frames = job.total_frames
        completed_count = len(completed)
        elapsed = job.elapsed_seconds()
        eta = None
        if completed_count > 0 and job.status in {"running", "paused"}:
            rate = elapsed / max(1, completed_count)
            eta = rate * max(0, total_frames - completed_count)

        return {
            "job_name": job.jobId,
            "blend_file": job.blendFile,
            "total_frames": total_frames,
            "completed_frames": completed_count,
            "pending_frames": len(pending),
            "rendering_frames": len(in_progress),
            "error_frames": len(error),
            "overall_percent": (completed_count / total_frames * 100.0) if total_frames else 0.0,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
            "machines_online": sum(1 for worker in self.worker_manager.list_workers() if worker.is_online),
            "machines_rendering": sum(
                1
                for worker in self.worker_manager.list_workers()
                if worker.status == "rendering" and worker.currentJobId == job.jobId
            ),
            "pending_frame_list": sorted(pending),
            "error_frame_list": sorted(error),
        }

    def _rebuild_job_for_resume(self, job: RenderJob, completed_frames: set[int]) -> None:
        all_frames = set(range(job.startFrame, job.endFrame + 1))
        job.completedFrames = sorted(completed_frames)
        for block in job.blocks:
            if self.block_scheduler.block_is_complete(block, completed_frames):
                block.status = "done"
                block.finishedAt = block.finishedAt or now_iso()
            else:
                block.status = "cancelled"
                block.workerId = None
                block.verificationDueAt = None
        missing_frames = sorted(all_frames - completed_frames)
        if missing_frames:
            new_blocks = self.block_scheduler.create_pending_blocks(job, missing_frames)
            job.blocks.extend(new_blocks)
            job.status = "paused"
            job.finishedAt = None
        else:
            job.status = "completed"
            job.finishedAt = now_iso()
        job.lastScanAt = time.time()
        self._refresh_job_metrics()

    def _apply_worker_payload(
        self,
        worker: Worker,
        status_payload: dict[str, Any],
        capabilities_payload: dict[str, Any] | None = None,
        render_device_payload: dict[str, Any] | None = None,
    ) -> None:
        capabilities_payload = capabilities_payload or {}
        render_device_payload = render_device_payload or {}

        machine_name = payload_value(status_payload, key="machineName")
        if machine_name is not None:
            worker.name = str(machine_name)

        status = payload_value(status_payload, key="status")
        if status is not None:
            worker.status = str(status)

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
            payload_value(
                status_payload,
                capabilities_payload,
                render_device_payload,
                key="renderDevicePreference",
            )
        )
        if render_device_preference is not None:
            worker.renderDevicePreference = render_device_preference

        effective_use_gpu = payload_value(
            status_payload,
            capabilities_payload,
            render_device_payload,
            key="effectiveUseGpu",
        )
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
        worker.lastSeen = time.time()

    def _apply_validation_payload(
        self,
        worker: Worker,
        payload: dict[str, Any] | None,
    ) -> WorkerPathValidationResult:
        result = WorkerPathValidationResult.from_payload(worker, payload)
        worker.blendFileExists = result.blendFileExists
        worker.outputFolderExists = result.outputFolderExists
        worker.canWriteOutput = result.canWriteOutput
        worker.blendAccessible = result.blend_accessible
        worker.outputAccessible = result.output_accessible
        worker.lastValidationErrors = list(result.errors)
        worker.lastValidatedAt = time.time()
        worker.lastValidationPayload = dict(payload or {})
        if result.errors:
            worker.lastLog = result.errors[-1]
        elif payload and payload.get("reason"):
            worker.lastLog = str(payload.get("reason"))
        elif payload and payload.get("message"):
            worker.lastLog = str(payload.get("message"))
        return result

    def _log_validation_result(self, result: WorkerPathValidationResult, worker_id: str) -> None:
        self.logger.warning(
            f"Validacao de paths falhou: {result.to_log_text()}",
            worker_id=worker_id,
        )
        for error in result.errors:
            self.logger.warning(
                f"Erro de validacao retornado pelo worker: {error}",
                worker_id=worker_id,
            )

    def _validation_warning_text(self, results: list[WorkerPathValidationResult]) -> str:
        lines = [
            "Alguns workers falharam na validacao de caminhos e foram ignorados neste job.",
            "",
        ]
        for index, result in enumerate(results):
            if index:
                lines.append("")
            lines.append(result.to_display_text())
        return "\n".join(lines)

    def _version_metadata_fields(
        self,
        blend_version: BlendVersionMetadata | None,
        *,
        version_routing_policy_override: str | None = None,
    ) -> dict[str, Any]:
        if blend_version is None:
            return {
                "requiredBlenderVersion": None,
                "preferredBlenderMajor": None,
                "versionRoutingPolicy": None,
                "blendVersionDetected": None,
            }
        version_routing_policy = normalize_version_routing_policy(
            version_routing_policy_override or blend_version.version_routing_policy or DEFAULT_VERSION_ROUTING_POLICY
        )
        return {
            "requiredBlenderVersion": blend_version.required_blender_version,
            "preferredBlenderMajor": blend_version.preferred_blender_major,
            "versionRoutingPolicy": version_routing_policy,
            "blendVersionDetected": blend_version.required_blender_version,
        }

    def _validate_workers_for_submission(
        self,
        workers: list[Worker],
        *,
        blend_path: Path,
        output_path: Path,
        version_metadata: dict[str, Any],
        log_results: bool,
    ) -> tuple[list[dict[str, Any]], list[WorkerPathValidationResult]]:
        evaluations: list[dict[str, Any]] = []
        invalid_results: list[WorkerPathValidationResult] = []
        validation_results: list[WorkerPathValidationResult] = []

        for worker in workers:
            if not worker.blenderFound:
                result = self._manual_validation_failure_result(worker, "Blender not found on worker.")
                validation_results.append(result)
                invalid_results.append(result)
                evaluations.append(
                    {
                        "worker": worker,
                        "result": result,
                        "payload": dict(result.rawPayload),
                        "valid": False,
                    }
                )
                if log_results:
                    self._log_validation_result(result, worker_id=worker.id)
                continue

            try:
                payload = self.worker_client.validate_paths(
                    worker,
                    str(blend_path),
                    str(output_path),
                    required_blender_version=version_metadata.get("requiredBlenderVersion"),
                    preferred_blender_major=version_metadata.get("preferredBlenderMajor"),
                    version_routing_policy=version_metadata.get("versionRoutingPolicy"),
                    blend_version_detected=version_metadata.get("blendVersionDetected"),
                )
            except WorkerClientError as exc:
                result = self._manual_validation_failure_result(worker, f"HTTP validation failed: {exc}")
                validation_results.append(result)
                invalid_results.append(result)
                evaluations.append(
                    {
                        "worker": worker,
                        "result": result,
                        "payload": dict(result.rawPayload),
                        "valid": False,
                    }
                )
                if log_results:
                    self._log_validation_result(result, worker_id=worker.id)
                continue

            with self._lock:
                result = self._apply_validation_payload(worker, payload)

            validation_results.append(result)
            evaluation = {
                "worker": worker,
                "result": result,
                "payload": dict(result.rawPayload),
                "valid": bool(result.ok),
            }
            evaluations.append(evaluation)
            if result.ok:
                if log_results:
                    self.logger.info(
                        f"Validacao de paths aprovada: {result.to_log_text()}",
                        worker_id=worker.id,
                    )
            else:
                invalid_results.append(result)
                if log_results:
                    self._log_validation_result(result, worker_id=worker.id)

        with self._lock:
            self.last_path_validation_results = validation_results
        return evaluations, invalid_results

    def _manual_validation_failure_result(self, worker: Worker, message: str) -> WorkerPathValidationResult:
        result = WorkerPathValidationResult(
            workerId=worker.id,
            workerName=worker.name,
            workerIp=worker.ip,
            workerPort=worker.port,
            errors=[message],
            rawPayload={
                "valid": False,
                "reason": message,
            },
        )
        with self._lock:
            worker.blendAccessible = False
            worker.outputAccessible = False
            worker.lastValidationErrors = list(result.errors)
            worker.lastValidationPayload = dict(result.rawPayload)
            worker.lastValidatedAt = time.time()
            worker.lastLog = message
        return result

    def _filter_version_evaluations(
        self,
        version_metadata: dict[str, Any],
        evaluations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        policy = normalize_version_routing_policy(version_metadata.get("versionRoutingPolicy"))
        valid_evaluations = [evaluation for evaluation in evaluations if evaluation.get("valid")]
        if not valid_evaluations:
            return evaluations

        same_major_sufficient = [
            evaluation
            for evaluation in valid_evaluations
            if self._routing_bucket_for_evaluation(evaluation, version_metadata)
            in {SELECTION_BUCKET_EXACT, SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT}
        ]
        same_major_any = [
            evaluation
            for evaluation in valid_evaluations
            if self._routing_bucket_for_evaluation(evaluation, version_metadata) in SAME_MAJOR_SELECTION_BUCKETS
        ]
        higher_major = [
            evaluation
            for evaluation in valid_evaluations
            if self._routing_bucket_for_evaluation(evaluation, version_metadata) == SELECTION_BUCKET_HIGHER_MAJOR
        ]

        allowed_ids: set[str] | None = None
        group_reason = ""
        if policy == PREFER_SAME_MAJOR_ANY_VERSION_POLICY:
            if same_major_any:
                allowed_ids = {evaluation["worker"].id for evaluation in same_major_any}
                group_reason = "Job is currently routing only to same-major Blender candidates."
            elif higher_major:
                allowed_ids = {evaluation["worker"].id for evaluation in higher_major}
                group_reason = "Job is currently routing only to higher-major Blender candidates."
        else:
            if same_major_sufficient:
                allowed_ids = {evaluation["worker"].id for evaluation in same_major_sufficient}
                group_reason = "Job is currently routing only to same-major Blender candidates that meet the file version."
            elif higher_major:
                allowed_ids = {evaluation["worker"].id for evaluation in higher_major}
                group_reason = "Job is currently routing only to higher-major Blender candidates."

        if not allowed_ids:
            return evaluations

        filtered: list[dict[str, Any]] = []
        for evaluation in evaluations:
            worker = evaluation["worker"]
            if evaluation.get("valid") and worker.id not in allowed_ids:
                payload = dict(evaluation.get("payload") or {})
                payload["valid"] = False
                payload["routingFiltered"] = True
                payload["reason"] = group_reason
                filtered.append(
                    {
                        **evaluation,
                        "valid": False,
                        "payload": payload,
                    }
                )
                continue
            filtered.append(evaluation)
        return filtered

    def _routing_bucket_for_evaluation(
        self,
        evaluation: dict[str, Any],
        version_metadata: dict[str, Any],
    ) -> str:
        payload = evaluation.get("payload") or {}
        bucket = payload.get("selectedVersionBucket")
        if bucket:
            return str(bucket)
        selected_version = payload.get("selectedBlenderVersion") or evaluation["worker"].blenderVersion
        return classify_selected_version_bucket(
            version_metadata.get("requiredBlenderVersion"),
            selected_version,
            preferred_major=version_metadata.get("preferredBlenderMajor"),
        )

    def _build_version_preflight_summary(
        self,
        blend_version: BlendVersionMetadata,
        version_metadata: dict[str, Any],
        evaluations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        same_major_sufficient_available = any(
            evaluation.get("valid")
            and self._routing_bucket_for_evaluation(evaluation, version_metadata)
            in {SELECTION_BUCKET_EXACT, SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT}
            for evaluation in evaluations
        )
        same_major_older_versions = self._collect_version_list_from_evaluations(
            evaluations,
            direct_key="selectedBlenderVersion",
            best_key="bestSameMajorOlderVersion",
            match_bucket=SELECTION_BUCKET_SAME_MAJOR_OLDER,
            availability_key="sameMajorOlderAvailable",
        )
        higher_major_versions = self._collect_version_list_from_evaluations(
            evaluations,
            direct_key="selectedBlenderVersion",
            best_key="bestHigherMajorVersion",
            match_bucket=SELECTION_BUCKET_HIGHER_MAJOR,
            availability_key="higherMajorAvailable",
        )
        same_major_older_available = bool(same_major_older_versions) or any(
            bool((evaluation.get("payload") or {}).get("sameMajorOlderAvailable"))
            for evaluation in evaluations
        )
        higher_major_available = bool(higher_major_versions) or any(
            bool((evaluation.get("payload") or {}).get("higherMajorAvailable"))
            for evaluation in evaluations
        )
        return {
            "job": {
                "requiredBlenderVersion": version_metadata.get("requiredBlenderVersion"),
                "preferredBlenderMajor": version_metadata.get("preferredBlenderMajor"),
                "versionRoutingPolicy": version_metadata.get("versionRoutingPolicy"),
                "blendVersionDetected": version_metadata.get("blendVersionDetected"),
            },
            "needsVersionChoice": bool(
                blend_version.required_blender_version
                and same_major_older_available
                and not same_major_sufficient_available
            ),
            "sameMajorSufficientAvailable": same_major_sufficient_available,
            "sameMajorOlderAvailable": same_major_older_available,
            "bestSameMajorOlderVersion": same_major_older_versions[-1] if same_major_older_versions else "",
            "sameMajorOlderVersions": same_major_older_versions,
            "higherMajorAvailable": higher_major_available,
            "bestHigherMajorVersion": higher_major_versions[0] if higher_major_versions else "",
            "higherMajorVersions": higher_major_versions,
            "workerValidation": self._serialize_validation_evaluations(evaluations, version_metadata),
        }

    def _empty_version_preflight(self) -> dict[str, Any]:
        return {
            "job": {
                "requiredBlenderVersion": None,
                "preferredBlenderMajor": None,
                "versionRoutingPolicy": None,
                "blendVersionDetected": None,
            },
            "needsVersionChoice": False,
            "sameMajorSufficientAvailable": False,
            "sameMajorOlderAvailable": False,
            "bestSameMajorOlderVersion": "",
            "sameMajorOlderVersions": [],
            "higherMajorAvailable": False,
            "bestHigherMajorVersion": "",
            "higherMajorVersions": [],
            "workerValidation": [],
        }

    def _serialize_validation_evaluations(
        self,
        evaluations: list[dict[str, Any]],
        version_metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for evaluation in evaluations:
            payload = dict(evaluation.get("payload") or {})
            worker = evaluation["worker"]
            serialized.append(
                {
                    "workerId": worker.id,
                    "workerName": worker.name,
                    "valid": bool(evaluation.get("valid")),
                    "reason": payload.get("reason") or payload.get("message"),
                    "selectedBlenderVersion": payload.get("selectedBlenderVersion") or worker.blenderVersion,
                    "selectedVersionBucket": self._routing_bucket_for_evaluation(evaluation, version_metadata),
                    "sameMajorSufficientAvailable": bool(payload.get("sameMajorSufficientAvailable")),
                    "sameMajorOlderAvailable": bool(payload.get("sameMajorOlderAvailable")),
                    "bestSameMajorOlderVersion": payload.get("bestSameMajorOlderVersion") or "",
                    "higherMajorAvailable": bool(payload.get("higherMajorAvailable")),
                    "bestHigherMajorVersion": payload.get("bestHigherMajorVersion") or "",
                }
            )
        return serialized

    def _collect_version_list_from_evaluations(
        self,
        evaluations: list[dict[str, Any]],
        *,
        direct_key: str,
        best_key: str,
        match_bucket: str,
        availability_key: str,
    ) -> list[str]:
        versions: set[str] = set()
        for evaluation in evaluations:
            payload = dict(evaluation.get("payload") or {})
            bucket = payload.get("selectedVersionBucket")
            if bucket == match_bucket and str(payload.get(direct_key) or "").strip():
                versions.add(str(payload.get(direct_key)).strip())
            best_value = str(payload.get(best_key) or "").strip()
            if best_value and bool(payload.get(availability_key)):
                versions.add(best_value)
        return sorted(versions, key=self._version_text_sort_key)

    @staticmethod
    def _version_text_sort_key(value: str) -> tuple[int, int, int]:
        parsed = value.strip().split(".")
        major = int(parsed[0]) if len(parsed) > 0 and parsed[0].isdigit() else 999
        minor = int(parsed[1]) if len(parsed) > 1 and parsed[1].isdigit() else 0
        patch = int(parsed[2]) if len(parsed) > 2 and parsed[2].isdigit() else 0
        return (major, minor, patch)

    def _validation_payload_matches_job(
        self,
        payload: dict[str, Any] | None,
        version_metadata: dict[str, Any],
    ) -> bool:
        if not version_metadata.get("requiredBlenderVersion"):
            return True
        payload = payload or {}
        for field in ("requiredBlenderVersion", "preferredBlenderMajor", "versionRoutingPolicy", "blendVersionDetected"):
            if payload.get(field) != version_metadata.get(field):
                return False
        return True

    def _mapped_drive_hint(
        self,
        path_values: list[str],
        results: list[WorkerPathValidationResult],
    ) -> str | None:
        if not results:
            return None
        if not any(re.match(r"^[A-Za-z]:\\", path) and path[0].upper() != "C" for path in path_values):
            return None

        keywords = (
            "not found",
            "cannot write",
            "could not create",
            "access",
            "permission",
            "sem acesso",
            "inacess",
            "nao encontrado",
            "nao enxerga",
        )
        all_relevant = True
        for result in results:
            if result.errors:
                error_text = " ".join(result.errors).lower()
                if not any(keyword in error_text for keyword in keywords):
                    all_relevant = False
                    break
            else:
                flags = [
                    result.blendFileExists is False,
                    result.outputFolderExists is False,
                    result.canWriteOutput is False,
                ]
                if not any(flags):
                    all_relevant = False
                    break

        if not all_relevant:
            return None

        return (
            "Dica: se os workers estiverem rodando como servico no Windows, unidades mapeadas "
            "como X: podem nao existir nesse contexto. Considere usar caminho UNC, como "
            "\\\\SERVIDOR\\Projetos\\..., ou executar o servico com um usuario que tenha "
            "acesso ao compartilhamento."
        )

    def _validate_job_inputs(
        self,
        blend_path: Path,
        start_frame: int,
        end_frame: int,
        output_path: Path,
        block_size: int,
    ) -> None:
        if not blend_path.exists():
            raise JobValidationError("O arquivo .blend nao existe.")
        if blend_path.suffix.lower() != ".blend":
            raise JobValidationError("Selecione um arquivo com extensao .blend.")
        if start_frame > end_frame:
            raise JobValidationError("O frame inicial precisa ser menor ou igual ao frame final.")
        if block_size <= 0:
            raise JobValidationError("O tamanho do bloco precisa ser maior que zero.")
        if output_path.exists() and not output_path.is_dir():
            raise JobValidationError("A pasta de saida informada nao e um diretorio valido.")

    def _path_warnings(self, blend_path: Path) -> list[str]:
        normalized = str(blend_path).lower()
        warnings: list[str] = []
        if "\\users\\" in normalized or "\\desktop\\" in normalized or "\\downloads\\" in normalized:
            warnings.append(
                "O arquivo .blend parece estar em um caminho local de usuario. Prefira um drive compartilhado, como X:\\, para evitar falhas nos workers."
            )
        return warnings

    def _new_job_id(self) -> str:
        timestamp = datetime.now().strftime("%Y_%m_%d_%H%M%S")
        return f"job_{timestamp}"

    def _find_existing_worker_for_discovery(self, discovered: DiscoveredWorker) -> Worker | None:
        exact_match = next(
            (
                worker
                for worker in self.worker_manager.list_workers()
                if worker.ip == discovered.ip and int(worker.port) == int(discovered.port)
            ),
            None,
        )
        if exact_match is not None:
            return exact_match

        same_name = [
            worker
            for worker in self.worker_manager.list_workers()
            if worker.name.strip().lower() == discovered.name.strip().lower()
        ]
        if len(same_name) == 1:
            return same_name[0]
        return None

    def _save_settings(self) -> None:
        self.persistence.save_settings(self.settings, self.worker_manager.list_workers())

    def _autosave_current_job(self) -> None:
        if not self.current_job:
            return
        self.persistence.save_job(self.current_job)
