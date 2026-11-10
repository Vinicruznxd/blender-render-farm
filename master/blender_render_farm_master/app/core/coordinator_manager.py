from __future__ import annotations

from copy import deepcopy
import getpass
from pathlib import Path
import socket
import time
from typing import Any

from app.core.blend_version import (
    BlendVersionMetadata,
    evaluate_worker_blender_version,
    read_blend_version_metadata,
)
from app.core.job_manager import JobValidationError
from app.core.models import normalize_progress_percent, payload_value
from app.core.runtime_metadata import load_runtime_metadata
from app.core.settings import AppSettings
from app.services.coordinator_client import CoordinatorClient, CoordinatorClientError
from app.services.coordinator_discovery import (
    DISCOVERY_NAMESPACE,
    CoordinatorAnnouncement,
    CoordinatorDiscoveryService,
)
from app.services.logger import AppLogger
from app.services.persistence import PersistenceService
from renderfarm_shared.i18n import normalize_language


class CoordinatorManager:
    mode = "coordinator"
    supports_local_jobs = False
    supports_local_job_files = False
    supports_worker_selection = False
    supports_worker_device_update = False
    supports_rescan = False

    def __init__(
        self,
        settings: AppSettings,
        persistence: PersistenceService,
        logger: AppLogger,
        client: CoordinatorClient | None = None,
        discovery_service: CoordinatorDiscoveryService | None = None,
    ) -> None:
        self.settings = settings
        self.persistence = persistence
        self.logger = logger
        self.discovery_service = discovery_service or CoordinatorDiscoveryService()
        self.client = client
        self.runtime_metadata = load_runtime_metadata(self.persistence.base_dir)
        self._preferred_coordinator_url = (settings.coordinator_url or "").strip().rstrip("/")
        self._active_api_url = getattr(client, "base_url", "").strip().rstrip("/") or self._preferred_coordinator_url or None
        self._active_discovery: CoordinatorAnnouncement | None = None
        self._active_source = "configured" if self._active_api_url else None
        self._heartbeat_interval_seconds = 5.0
        self._configured_retry_interval_seconds = 5.0
        self._last_heartbeat_at = 0.0
        self._last_configured_attempt_at = 0.0
        self._last_error_message = ""
        self._last_status_message = ""
        self._snapshot = self._empty_snapshot()

    def close(self) -> None:
        self.discovery_service.stop()

    def get_snapshot(self) -> dict[str, Any]:
        return deepcopy(self._snapshot)

    def set_language(self, language: str) -> dict[str, Any]:
        normalized = normalize_language(language)
        client = self._client_or_raise()
        client.update_language(normalized)
        self.settings.language = normalized
        return self.tick()

    def tick(self) -> dict[str, Any]:
        self.discovery_service.start()
        discovery = self._refresh_discovery_state()

        if discovery["status"] == "conflict" and self._active_source != "configured":
            self._clear_active_coordinator()
            snapshot = self._discovery_snapshot(discovery=discovery)
            self._snapshot = snapshot
            self._log_discovery_message(snapshot["coordinatorDiscovery"]["message"])
            return self.get_snapshot()

        if self.client is None and self._try_connect_configured_url():
            discovery = self._refresh_discovery_state()

        if self.client is None and discovery["selected"] is not None:
            if self._activate_announcement(discovery["selected"]):
                discovery = self._refresh_discovery_state()

        if self.client is None:
            snapshot = self._discovery_snapshot(discovery=discovery)
            self._snapshot = snapshot
            self._log_discovery_message(snapshot["coordinatorDiscovery"]["message"])
            return self.get_snapshot()

        try:
            now = time.time()
            if now - self._last_heartbeat_at >= self._heartbeat_interval_seconds:
                self._send_master_heartbeat()
                self._last_heartbeat_at = now

            state_payload = self.client.state()
            snapshot = self._normalize_state(state_payload)
            snapshot["coordinator"] = {
                **snapshot.get("coordinator", {}),
                **self._active_coordinator_payload(),
            }
            discovery_message = self._connected_discovery_message(discovery)
            snapshot["coordinatorDiscovery"] = self._discovery_payload(
                status="connected",
                message=discovery_message,
                candidates=discovery["candidates"],
                conflict=bool(discovery["conflict"]),
            )
            logs = snapshot.setdefault("logs", {})
            current_general = str(logs.get("general") or "").strip()
            if discovery["status"] == "conflict" and self._active_source == "configured":
                warning = discovery["message"]
                if warning and warning not in current_general:
                    logs["general"] = f"{warning}\n{current_general}".strip()
            self._snapshot = snapshot
            if self._last_error_message:
                self.logger.info("Conexao com coordinator restabelecida.")
                self._last_error_message = ""
            self._last_status_message = ""
            return self.get_snapshot()
        except CoordinatorClientError as exc:
            message = str(exc)
            if message != self._last_error_message:
                self.logger.warning(f"Coordinator indisponivel: {message}")
                self._last_error_message = message
            self._clear_active_coordinator()
            discovery = self._refresh_discovery_state(force_probe=True)
            snapshot = self._discovery_snapshot(discovery=discovery, error_message=message)
            self._snapshot = snapshot
            self._log_discovery_message(snapshot["coordinatorDiscovery"]["message"])
            return self.get_snapshot()

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
        client = self._client_or_raise()
        self._ensure_master_identity_ready()
        warnings: list[str] = []
        blend_version, detection_warning = self._detect_local_blend_version(blend_file)
        payload = self._build_job_payload(
            blend_file=blend_file,
            start_frame=start_frame,
            end_frame=end_frame,
            output_folder=output_folder,
            block_size=block_size,
            ignore_rendered_frames=ignore_rendered_frames,
            auto_add_workers=auto_add_workers,
            redistribute_offline_blocks=redistribute_offline_blocks,
            legacy_use_gpu=legacy_use_gpu,
        )
        metadata_fields = self._blend_version_payload_fields(
            blend_version,
            version_routing_policy_override=version_routing_policy_override,
        )
        payload.update(metadata_fields)

        metadata_sent = bool(metadata_fields)
        try:
            client.create_job(payload)
        except CoordinatorClientError as exc:
            if metadata_fields and exc.status_code in {400, 422}:
                fallback_payload = {key: value for key, value in payload.items() if key not in metadata_fields}
                client.create_job(fallback_payload)
                metadata_sent = False
                warnings.append(
                    "O Coordinator atual nao aceitou os metadados opcionais de versao. "
                    "O job foi reenviado sem requiredBlenderVersion, preferredBlenderMajor e versionRoutingPolicy "
                    "para manter compatibilidade."
                )
            else:
                raise

        snapshot = self.tick()
        warnings.insert(0, "Job enviado para a fila global do Coordinator.")
        if blend_version and metadata_sent:
            warnings.append(
                f"Metadados de versao enviados ao Coordinator: "
                f"Blender do arquivo {blend_version.required_blender_version}; "
                f"politica {blend_version.policy_text}."
            )
            worker_advisory = self._coordinator_worker_version_advisory(snapshot.get("workers", []), blend_version)
            if worker_advisory:
                warnings.append(worker_advisory)
        elif detection_warning:
            warnings.append(detection_warning)
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
        client = self._client_or_raise()
        self._ensure_master_identity_ready()
        blend_version, _ = self._detect_local_blend_version(blend_file)
        if blend_version is None:
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

        payload = self._build_job_payload(
            blend_file=blend_file,
            start_frame=start_frame,
            end_frame=end_frame,
            output_folder=output_folder,
            block_size=block_size,
            ignore_rendered_frames=ignore_rendered_frames,
            auto_add_workers=auto_add_workers,
            redistribute_offline_blocks=redistribute_offline_blocks,
            legacy_use_gpu=legacy_use_gpu,
        )
        payload.update(self._blend_version_payload_fields(blend_version))
        return client.preflight_job(payload)

    def pause_distribution(self, job_id: str | None = None) -> None:
        client = self._client_or_raise()
        target_job_id = job_id or self._active_job_id()
        if not target_job_id:
            raise JobValidationError("Nao ha job ativo ou selecionado para pausar.")
        client.pause_job(target_job_id)
        self.tick()

    def resume_distribution(self, job_id: str | None = None) -> None:
        client = self._client_or_raise()
        target_job_id = job_id or self._active_job_id()
        if not target_job_id:
            raise JobValidationError("Nao ha job ativo ou selecionado para retomar.")
        client.resume_job(target_job_id)
        self.tick()

    def cancel_job(self, job_id: str | None = None) -> None:
        client = self._client_or_raise()
        target_job_id = job_id or self._active_job_id()
        if not target_job_id:
            raise JobValidationError("Nao ha job ativo ou selecionado para cancelar.")
        client.cancel_job(target_job_id)
        self.tick()

    def force_rescan(self) -> None:
        raise JobValidationError("Em modo coordinator, o reescaneamento e feito centralmente.")

    def save_current_job(self):
        raise JobValidationError("Em modo coordinator, a fila e central. O save local esta desabilitado.")

    def load_job(self, path):
        raise JobValidationError("Em modo coordinator, carregamento local de job esta desabilitado.")

    def add_worker(self, name: str, ip: str, port: int, enabled: bool = True) -> dict[str, Any]:
        client = self._client_or_raise()
        payload = {
            "name": name or f"Worker-{ip}:{port}",
            "ip": ip,
            "port": int(port),
            "enabled": bool(enabled),
        }
        result = client.add_worker(payload)
        self.tick()
        return result

    def discover_workers(
        self,
        hosts: list[str] | None = None,
        ports: list[int] | None = None,
    ) -> dict[str, Any]:
        client = self._client_or_raise()
        payload: dict[str, Any] = {}
        if hosts:
            payload["hosts"] = hosts
        if ports:
            payload["ports"] = ports
        result = client.discover_workers(payload or None)
        snapshot = self.tick()
        discovered = result.get("workers") or result.get("discovered") or []
        return {
            "discoveredCount": int(result.get("discoveredCount", len(discovered))),
            "addedCount": int(result.get("addedCount", len(discovered))),
            "updatedCount": int(result.get("updatedCount", 0)),
            "scannedCount": int(result.get("scannedCount", 0)),
            "scannedNetworks": result.get("scannedNetworks", []),
            "ports": result.get("ports", []),
            "workers": discovered,
            "snapshot": snapshot,
        }

    def remove_worker(self, worker_id: str) -> None:
        client = self._client_or_raise()
        client.delete_worker(worker_id)
        self.tick()

    def test_worker(
        self,
        worker_id: str,
        blend_file: str | None = None,
        output_folder: str | None = None,
    ) -> dict[str, Any]:
        client = self._client_or_raise()
        payload: dict[str, Any] = {}
        if blend_file:
            payload["blendFile"] = blend_file
        if output_folder:
            payload["outputFolder"] = output_folder
        result = client.test_worker(worker_id, payload or None)
        self.tick()
        return result

    def update_worker_selected(self, worker_id: str, selected: bool) -> None:
        self.logger.info("Selecao local de worker ignorada em modo coordinator.")

    def update_worker_render_device(self, worker_id: str, render_device_preference: str) -> None:
        raise JobValidationError("Troca de render device via coordinator ainda nao foi implementada.")

    def update_job_options(self, auto_add_workers: bool, redistribute_offline_blocks: bool) -> None:
        return

    def _client_or_raise(self) -> CoordinatorClient:
        if self.client is None:
            self.tick()
        if self.client is None:
            raise JobValidationError(self._coordinator_unavailable_message())
        return self.client

    def _heartbeat_payload(self) -> dict[str, Any]:
        self._ensure_master_identity_ready()
        return {
            "masterId": self.settings.master_id,
            "masterName": self.settings.master_name,
            "host": socket.gethostname(),
            "username": getpass.getuser(),
            "version": self.runtime_metadata["appVersion"],
            "protocolVersion": self.runtime_metadata["protocolVersion"],
            "buildId": self.runtime_metadata["buildId"],
            "buildTimestampUtc": self.runtime_metadata["buildTimestampUtc"],
        }

    def _ensure_master_identity_ready(self) -> None:
        if not self.settings.ensure_master_identity():
            return
        try:
            self.persistence.save_settings(self.settings, self.settings.build_workers())
        except Exception as exc:
            self.logger.warning(
                "Nao foi possivel persistir a identidade do Master no config.json: %s. "
                "A execucao atual continuara com a identidade em memoria.",
                exc,
            )

    def _send_master_heartbeat(self) -> None:
        payload = self._heartbeat_payload()
        try:
            self.client.master_heartbeat(payload)
            return
        except CoordinatorClientError as exc:
            if exc.status_code not in {400, 422}:
                raise
            fallback_payload = {
                key: value
                for key, value in payload.items()
                if key not in {"protocolVersion", "buildId", "buildTimestampUtc"}
            }
            self.client.master_heartbeat(fallback_payload)

    def _refresh_discovery_state(self, *, force_probe: bool = False) -> dict[str, Any]:
        candidates = self.discovery_service.recent_announcements()
        if force_probe or not candidates:
            candidates = self.discovery_service.probe(force=force_probe)

        eligible = [
            item
            for item in candidates
            if item.enabled and str(item.status or "running").lower() == "running"
        ]
        unique_ids = {item.coordinator_id for item in eligible}

        if len(unique_ids) > 1:
            message = (
                f"Conflito de Coordinator na LAN. Foram encontrados {len(unique_ids)} coordinators "
                f"no namespace {self.settings_coordinator_namespace}. Resolva o conflito antes de continuar."
            )
            return {
                "status": "conflict",
                "message": message,
                "conflict": True,
                "selected": None,
                "candidates": [item.to_dict() for item in eligible],
            }

        if eligible:
            selected = eligible[0]
            return {
                "status": "candidate",
                "message": (
                    f"Coordinator encontrado: {selected.hostname} ({', '.join(selected.api_urls)})"
                ),
                "conflict": False,
                "selected": selected,
                "candidates": [item.to_dict() for item in eligible],
            }

        message = "Aguardando descoberta automatica do Coordinator na LAN."
        if self.discovery_service.listen_error:
            message += (
                f" Escuta passiva UDP indisponivel em {self.discovery_service.listen_port}: "
                f"{self.discovery_service.listen_error}"
            )
        return {
            "status": "discovering",
            "message": message,
            "conflict": False,
            "selected": None,
            "candidates": [item.to_dict() for item in candidates],
        }

    @property
    def settings_coordinator_namespace(self) -> str:
        return DISCOVERY_NAMESPACE

    def _try_connect_configured_url(self) -> bool:
        if not self._preferred_coordinator_url:
            return False
        now = time.time()
        if now - self._last_configured_attempt_at < self._configured_retry_interval_seconds:
            return False
        self._last_configured_attempt_at = now
        return self._activate_api_urls(
            [self._preferred_coordinator_url],
            source="configured",
            announcement=None,
        )

    def _activate_announcement(self, announcement: CoordinatorAnnouncement) -> bool:
        return self._activate_api_urls(
            announcement.api_urls,
            source="discovered",
            announcement=announcement,
        )

    def _activate_api_urls(
        self,
        api_urls: list[str],
        *,
        source: str,
        announcement: CoordinatorAnnouncement | None,
    ) -> bool:
        errors: list[str] = []
        for api_url in api_urls:
            candidate_client = CoordinatorClient(api_url, timeout_seconds=5.0)
            try:
                health_payload = candidate_client.health()
            except CoordinatorClientError as exc:
                errors.append(f"{api_url}: {exc}")
                continue
            if health_payload.get("ok") is False:
                errors.append(f"{api_url}: /health retornou ok=false")
                continue
            self.client = candidate_client
            self._active_api_url = api_url.rstrip("/")
            self._active_discovery = announcement
            self._active_source = source
            self._last_heartbeat_at = 0.0
            return True

        if errors:
            self._last_error_message = "; ".join(errors)
        return False

    def _clear_active_coordinator(self) -> None:
        self.client = None
        self._active_api_url = None
        self._active_discovery = None
        self._active_source = None
        self._last_heartbeat_at = 0.0

    def _coordinator_unavailable_message(self) -> str:
        discovery = self._snapshot.get("coordinatorDiscovery", {})
        message = str(discovery.get("message") or "").strip()
        if message:
            return message
        if self._last_error_message:
            return f"Coordinator indisponivel: {self._last_error_message}"
        return "Nenhum Coordinator ativo foi encontrado. O Master continua em modo discovery na LAN."

    def _connected_discovery_message(self, discovery: dict[str, Any]) -> str:
        if self._active_source == "configured" and discovery["status"] == "conflict":
            return (
                f"Conectado ao Coordinator configurado em {self._active_api_url}, "
                "mas existe conflito com outros coordinators anunciados na LAN."
            )
        if self._active_api_url:
            return f"Conectado ao Coordinator em {self._active_api_url}."
        return "Conectado ao Coordinator."

    def _active_coordinator_payload(self) -> dict[str, Any]:
        payload = {
            "apiUrl": self._active_api_url,
            "source": self._active_source,
        }
        if self._active_discovery is not None:
            payload.update(
                {
                    "coordinatorId": self._active_discovery.coordinator_id,
                    "hostname": self._active_discovery.hostname,
                    "startedAt": self._active_discovery.started_at,
                    "addresses": list(self._active_discovery.addresses),
                    "apiUrls": list(self._active_discovery.api_urls),
                }
            )
        return payload

    def _discovery_payload(
        self,
        *,
        status: str,
        message: str,
        candidates: list[dict[str, Any]],
        conflict: bool,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "message": message,
            "namespace": self.settings_coordinator_namespace,
            "udpPort": self.discovery_service.udp_port,
            "listenPort": self.discovery_service.listen_port,
            "conflict": conflict,
            "activeApiUrl": self._active_api_url,
            "activeSource": self._active_source,
            "candidates": candidates,
        }

    def _active_job_id(self) -> str | None:
        return (self._snapshot.get("job") or {}).get("jobId")

    def _derive_job_name(self, blend_file: str) -> str:
        return Path(blend_file).stem or "RenderJob"

    def _build_job_payload(
        self,
        *,
        blend_file: str,
        start_frame: int,
        end_frame: int,
        output_folder: str,
        block_size: int,
        ignore_rendered_frames: bool,
        auto_add_workers: bool,
        redistribute_offline_blocks: bool,
        legacy_use_gpu: bool,
    ) -> dict[str, Any]:
        return {
            "jobName": self._derive_job_name(blend_file),
            "blendFile": blend_file,
            "outputFolder": output_folder,
            "startFrame": int(start_frame),
            "endFrame": int(end_frame),
            "blockSize": int(block_size),
            "ignoreRenderedFrames": bool(ignore_rendered_frames),
            "autoAddWorkers": bool(auto_add_workers),
            "redistributeOfflineBlocks": bool(redistribute_offline_blocks),
            "legacyUseGpu": bool(legacy_use_gpu),
            "submittedByMasterId": self.settings.master_id,
            "submittedByMasterName": self.settings.master_name,
            "submittedByUser": getpass.getuser(),
        }

    def _blend_version_payload_fields(
        self,
        blend_version: BlendVersionMetadata | None,
        *,
        version_routing_policy_override: str | None = None,
    ) -> dict[str, Any]:
        if blend_version is None:
            return {}
        payload = blend_version.to_payload_fields()
        if version_routing_policy_override:
            payload["versionRoutingPolicy"] = version_routing_policy_override
        return payload

    def _detect_local_blend_version(self, blend_file: str) -> tuple[BlendVersionMetadata | None, str | None]:
        blend_path = Path(blend_file)
        if blend_path.suffix.lower() != ".blend" or not blend_path.exists():
            return None, None

        blend_version = read_blend_version_metadata(blend_path)
        if blend_version is None:
            return (
                None,
                "Nao foi possivel detectar localmente a versao do .blend. "
                "O job foi enviado sem metadados opcionais de compatibilidade.",
            )
        return blend_version, None

    def _coordinator_worker_version_advisory(
        self,
        workers: list[dict[str, Any]],
        blend_version: BlendVersionMetadata,
    ) -> str | None:
        if not workers:
            return None

        preferred_count = 0
        compatible_other_major_count = 0
        older_count = 0
        comparable_count = 0
        for worker in workers:
            is_compatible, is_preferred = evaluate_worker_blender_version(
                worker.get("blenderVersion"),
                blend_version,
            )
            if is_compatible is None:
                continue
            comparable_count += 1
            if is_compatible and is_preferred:
                preferred_count += 1
            elif is_compatible:
                compatible_other_major_count += 1
            else:
                older_count += 1

        if comparable_count == 0 or preferred_count > 0:
            return None
        if compatible_other_major_count > 0:
            return (
                f"No snapshot atual do Coordinator, ainda nao ha workers na faixa preferida "
                f"{blend_version.policy_text}. Existem workers com versao igual ou superior fora da major preferida."
            )
        if older_count > 0:
            return (
                f"No snapshot atual do Coordinator, nenhum worker reporta Blender "
                f"{blend_version.required_blender_version} ou superior para este arquivo."
            )
        return None

    def _normalize_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        workers = [self._normalize_worker(item) for item in payload.get("workers", [])]
        active_job = self._normalize_job(payload.get("activeJob") or payload.get("job"))
        queue = [self._normalize_job(item) for item in payload.get("queue", [])]
        if active_job:
            queue = [item for item in queue if item and item.get("jobId") != active_job.get("jobId")]
        jobs = [self._normalize_job(item) for item in payload.get("jobs", [])]
        metrics = self._normalize_metrics(payload.get("metrics", {}), active_job, workers, queue)
        logs = self._normalize_logs(payload.get("logs", {}))
        return {
            "coordinator": payload.get("coordinator", {}),
            "coordinatorDiscovery": self._discovery_payload(
                status="connected",
                message=self._connected_discovery_message({"status": "connected"}),
                candidates=[],
                conflict=False,
            ),
            "masters": payload.get("masters", []),
            "workers": workers,
            "job": active_job,
            "queue": queue,
            "jobs": jobs,
            "metrics": metrics,
            "logs": logs,
        }

    def _normalize_worker(self, payload: dict[str, Any]) -> dict[str, Any]:
        ip = str(payload_value(payload, key="ip") or payload_value(payload, key="host") or "127.0.0.1")
        port = int(payload_value(payload, key="port") or self.settings.default_worker_port)
        worker_id = str(
            payload_value(payload, key="workerId")
            or payload_value(payload, key="worker_id")
            or payload_value(payload, key="id")
            or f"{ip}:{port}"
        )
        status = str(payload_value(payload, key="status") or "offline")
        worker_name = (
            payload_value(payload, key="name")
            or payload_value(payload, key="machineName")
            or payload_value(payload, key="machine_name")
            or worker_id
        )
        current_job_id = (
            payload_value(payload, key="currentJobId")
            or payload_value(payload, key="current_job_id")
            or payload_value(payload, key="jobId")
            or payload_value(payload, key="job_id")
            or payload_value(payload, key="currentJob")
        )
        current_block_id = (
            payload_value(payload, key="currentBlockId")
            or payload_value(payload, key="current_block_id")
            or payload_value(payload, key="blockId")
            or payload_value(payload, key="block_id")
            or payload_value(payload, key="currentBlock")
        )
        return {
            "id": worker_id,
            "name": worker_name,
            "ip": ip,
            "port": port,
            "enabled": bool(payload.get("enabled", True)),
            "selected": bool(payload.get("selected", True)),
            "status": status,
            "blenderFound": bool(
                payload_value(payload, key="blenderFound") or payload_value(payload, key="blender_found") or False
            ),
            "blenderVersion": payload_value(payload, key="blenderVersion")
            or payload_value(payload, key="blender_version"),
            "gpu": payload_value(payload, key="gpu"),
            "renderDevicePreference": payload_value(payload, key="renderDevicePreference")
            or payload_value(payload, key="render_device_preference"),
            "effectiveUseGpu": payload_value(payload, key="effectiveUseGpu")
            if payload_value(payload, key="effectiveUseGpu") is not None
            else payload_value(payload, key="effective_use_gpu"),
            "currentJobId": current_job_id,
            "currentBlockId": current_block_id,
            "currentFrame": payload_value(payload, key="currentFrame")
            if payload_value(payload, key="currentFrame") is not None
            else payload_value(payload, key="current_frame"),
            "progress": float(normalize_progress_percent(payload.get("progress")) or 0.0),
            "lastSeen": payload.get("lastSeen"),
            "lastLog": payload_value(payload, key="lastLog") or payload_value(payload, key="last_log") or "",
            "error": payload.get("error"),
            "blendAccessible": payload.get("blendAccessible"),
            "outputAccessible": payload.get("outputAccessible"),
        }

    def _normalize_job(self, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if not payload:
            return None
        job_id = payload_value(payload, key="jobId") or payload_value(payload, key="job_id") or payload.get("id")
        job_name = payload_value(payload, key="jobName") or payload_value(payload, key="job_name") or job_id
        blend_file = payload_value(payload, key="blendFile") or payload_value(payload, key="blend_file")
        start_frame = int(payload_value(payload, key="startFrame") or payload_value(payload, key="start_frame") or 0)
        end_frame = int(payload_value(payload, key="endFrame") or payload_value(payload, key="end_frame") or 0)
        total_frames = max(0, end_frame - start_frame + 1) if end_frame >= start_frame else 0
        completed_frames = payload_value(payload, key="completedFrames") or payload_value(payload, key="completed_frames")
        completed_count = payload_value(payload, key="completedFramesCount")
        if completed_count is None:
            completed_count = payload_value(payload, key="completedFrameCount")
        if completed_count is None:
            completed_count = payload_value(payload, key="completed_frame_count")
        if completed_count is None and isinstance(completed_frames, list):
            completed_count = len(completed_frames)
        if completed_count is None:
            completed_count = 0
        overall_percent = normalize_progress_percent(
            payload_value(payload, key="overallPercent") or payload_value(payload, key="overall_percent")
        )
        if overall_percent is None and total_frames:
            overall_percent = max(0.0, min(100.0, (float(completed_count or 0) / total_frames) * 100.0))
        if overall_percent == 0.0 and total_frames and completed_count:
            overall_percent = max(0.0, min(100.0, (float(completed_count or 0) / total_frames) * 100.0))
        return {
            **payload,
            "jobId": job_id,
            "jobName": job_name,
            "blendFile": blend_file,
            "startFrame": start_frame,
            "endFrame": end_frame,
            "blockSize": int(
                payload_value(payload, key="blockSize")
                or payload_value(payload, key="block_size")
                or self.settings.default_block_size
            ),
            "status": payload.get("status", "queued"),
            "queuePosition": payload_value(payload, key="queuePosition")
            if payload_value(payload, key="queuePosition") is not None
            else payload_value(payload, key="queue_position"),
            "overallPercent": float(overall_percent or 0.0),
            "completedFramesCount": int(completed_count or 0),
            "totalFrames": total_frames,
            "blocks": [self._normalize_block(block) for block in payload.get("blocks", []) if isinstance(block, dict)],
        }

    def _normalize_block(self, payload: dict[str, Any]) -> dict[str, Any]:
        block_id = payload_value(payload, key="blockId") or payload_value(payload, key="block_id")
        worker_id = payload_value(payload, key="workerId") or payload_value(payload, key="worker_id")
        start_frame = int(payload_value(payload, key="startFrame") or payload_value(payload, key="start_frame") or 0)
        end_frame = int(payload_value(payload, key="endFrame") or payload_value(payload, key="end_frame") or 0)
        status = str(payload_value(payload, key="status") or "pending")
        if status == "leased":
            status = "assigned"
        return {
            **payload,
            "blockId": block_id,
            "workerId": worker_id,
            "status": status,
            "startFrame": start_frame,
            "endFrame": end_frame,
        }

    def _normalize_metrics(
        self,
        metrics_payload: dict[str, Any],
        active_job: dict[str, Any] | None,
        workers: list[dict[str, Any]],
        queue: list[dict[str, Any]],
    ) -> dict[str, Any]:
        active_job = active_job or {}
        total_frames = int(
            payload_value(metrics_payload, key="total_frames")
            or payload_value(metrics_payload, key="totalFrames")
            or active_job.get("totalFrames", 0)
        )
        completed_frames = int(
            payload_value(metrics_payload, key="completed_frames")
            or payload_value(metrics_payload, key="completedFrames")
            or active_job.get("completedFramesCount", 0)
        )
        pending_frames = int(metrics_payload.get("pending_frames", metrics_payload.get("pendingFrames", 0)))
        rendering_frames = int(metrics_payload.get("rendering_frames", metrics_payload.get("renderingFrames", 0)))
        error_frames = int(metrics_payload.get("error_frames", metrics_payload.get("errorFrames", 0)))
        if total_frames and pending_frames == 0 and rendering_frames == 0 and error_frames == 0:
            pending_frames = max(0, total_frames - completed_frames)

        overall_percent = normalize_progress_percent(metrics_payload.get("overall_percent"))
        if overall_percent is None:
            overall_percent = normalize_progress_percent(
                metrics_payload.get("overallPercent", active_job.get("overallPercent", 0.0))
            )
        if overall_percent is None:
            overall_percent = 0.0
        if overall_percent == 0.0 and total_frames and completed_frames > 0:
            overall_percent = max(0.0, min(100.0, (float(completed_frames) / total_frames) * 100.0))

        return {
            "job_name": active_job.get("jobName") or active_job.get("jobId", ""),
            "blend_file": active_job.get("blendFile") or active_job.get("blend_file", ""),
            "total_frames": total_frames,
            "completed_frames": completed_frames,
            "pending_frames": pending_frames,
            "rendering_frames": rendering_frames,
            "error_frames": error_frames,
            "overall_percent": float(overall_percent or 0.0),
            "elapsed_seconds": payload_value(metrics_payload, key="elapsed_seconds")
            or payload_value(metrics_payload, key="elapsedSeconds")
            or 0.0,
            "eta_seconds": payload_value(metrics_payload, key="eta_seconds")
            if payload_value(metrics_payload, key="eta_seconds") is not None
            else payload_value(metrics_payload, key="etaSeconds"),
            "machines_online": int(
                metrics_payload.get(
                    "machines_online",
                    metrics_payload.get(
                        "machinesOnline",
                        sum(1 for worker in workers if worker.get("status") not in {"offline", "disabled"}),
                    ),
                )
            ),
            "machines_rendering": int(
                metrics_payload.get(
                    "machines_rendering",
                    metrics_payload.get(
                        "machinesRendering",
                        sum(1 for worker in workers if worker.get("status") == "rendering"),
                    ),
                )
            ),
            "pending_frame_list": payload_value(metrics_payload, key="pending_frame_list") or [],
            "error_frame_list": payload_value(metrics_payload, key="error_frame_list") or [],
            "total_queued_jobs": int(
                payload_value(metrics_payload, key="total_queued_jobs")
                or payload_value(metrics_payload, key="totalQueuedJobs")
                or len(queue)
            ),
        }

    def _normalize_logs(self, logs_payload: dict[str, Any] | None) -> dict[str, Any]:
        logs_payload = logs_payload or {}
        general = logs_payload.get("general", "")
        if isinstance(general, list):
            general = "\n".join(str(item) for item in general)
        return {
            "general": str(general or ""),
            "workers": logs_payload.get("workers", {}),
        }

    def _discovery_snapshot(
        self,
        *,
        discovery: dict[str, Any],
        error_message: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self._empty_snapshot()
        snapshot["coordinator"] = self._active_coordinator_payload()
        snapshot["coordinatorDiscovery"] = self._discovery_payload(
            status=str(discovery.get("status") or "discovering"),
            message=self._build_discovery_message(discovery, error_message),
            candidates=list(discovery.get("candidates") or []),
            conflict=bool(discovery.get("conflict")),
        )
        if error_message:
            snapshot["coordinatorError"] = error_message
        snapshot["logs"]["general"] = snapshot["coordinatorDiscovery"]["message"]
        return snapshot

    def _build_discovery_message(self, discovery: dict[str, Any], error_message: str | None) -> str:
        discovery_message = str(discovery.get("message") or "").strip()
        if error_message:
            prefix = f"Coordinator indisponivel: {error_message}"
            if discovery_message:
                return f"{prefix}\n{discovery_message}"
            return prefix
        return discovery_message or "Aguardando descoberta automatica do Coordinator na LAN."

    def _log_discovery_message(self, message: str) -> None:
        if not message or message == self._last_status_message:
            return
        self._last_status_message = message
        if "Conflito de Coordinator" in message:
            self.logger.warning(message)
        elif "indisponivel" in message.lower():
            self.logger.warning(message)
        else:
            self.logger.info(message)

    def _empty_snapshot(self) -> dict[str, Any]:
        return {
            "coordinator": {},
            "coordinatorDiscovery": self._discovery_payload(
                status="discovering",
                message="Aguardando descoberta automatica do Coordinator na LAN.",
                candidates=[],
                conflict=False,
            ),
            "masters": [],
            "workers": [],
            "job": None,
            "queue": [],
            "jobs": [],
            "metrics": {
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
                "machines_online": 0,
                "machines_rendering": 0,
                "pending_frame_list": [],
                "error_frame_list": [],
                "total_queued_jobs": 0,
            },
            "logs": {
                "general": "",
                "workers": {},
            },
        }
