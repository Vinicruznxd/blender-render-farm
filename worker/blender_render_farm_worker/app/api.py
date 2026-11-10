from __future__ import annotations

import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .blender_runner import BlenderRunner, configure_windows_error_mode
from .capabilities import CapabilitiesService
from .config import ConfigManager, render_device_preference_to_use_gpu
from .coordinator_discovery import CoordinatorDiscoveryService
from .coordinator_reporter import CoordinatorReporter
from .device_selector import RenderDeviceWindow, build_render_device_response
from .logger import setup_logging
from .models import (
    CancelRequest,
    CancelResponse,
    HealthResponse,
    JobDrainRequest,
    JobDrainResponse,
    LogsResponse,
    RenderAcceptedResponse,
    RenderBlockRequest,
    RenderDevicePreferenceRequest,
    RenderDevicePreferenceResponse,
    ValidatePathsRequest,
    ValidatePathsResponse,
)
from .path_validator import validate_paths
from .runtime_metadata import APP_VERSION
from .status_manager import WorkerStatusManager


def create_app(project_root: Path | None = None) -> FastAPI:
    root = project_root or Path(__file__).resolve().parents[1]
    config_manager = ConfigManager(root / "config.json")
    config = config_manager.config
    logger = setup_logging(root, config.logLevel)
    status_manager = WorkerStatusManager(config, runtime_root=root)
    capabilities_service = CapabilitiesService(config, logger, runtime_root=root)
    coordinator_discovery_service = CoordinatorDiscoveryService(
        config=config,
        status_manager=status_manager,
        logger=logger,
    )
    coordinator_reporter = CoordinatorReporter(
        status_manager=status_manager,
        logger=logger,
        project_root=root,
    )
    status_manager.set_render_completion_callback(
        coordinator_reporter.report_render_completion
    )
    blender_runner = BlenderRunner(
        project_root=root,
        config=config,
        config_manager=config_manager,
        status_manager=status_manager,
        logger=logger,
        coordinator_discovery_service=coordinator_discovery_service,
    )
    render_device_window = RenderDeviceWindow(
        config_manager=config_manager,
        status_manager=status_manager,
        logger=logger,
    )

    app = FastAPI(
        title="Blender Render Farm Worker",
        version=APP_VERSION,
        description="HTTP worker for rendering Blender frame blocks on Windows.",
    )

    def run_background_startup_tasks() -> None:
        logger.info("Running worker background startup tasks")
        status_manager.append_log("Refreshing worker capabilities in background")
        try:
            capabilities_service.refresh()
            status_manager.set_gpu_name(capabilities_service.cached_gpu_name())
        except Exception as exc:
            logger.exception("Capabilities refresh failed during background startup")
            status_manager.append_log(f"Capabilities refresh failed: {exc}")

        status_manager.append_log("Refreshing Blender inventory in background")
        try:
            blender_runner.detect_blender()
        except Exception as exc:
            message = f"Blender detection failed during background startup: {exc}"
            logger.exception("Blender detection failed during background startup")
            status_manager.set_error(message)
            status_manager.append_log(message)

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if not config.auth_enabled:
            return
        if not authorization:
            raise HTTPException(status_code=401, detail="missing bearer token")

        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or token != config.authToken:
            raise HTTPException(status_code=401, detail="invalid bearer token")

    @app.on_event("startup")
    async def startup_event() -> None:
        try:
            configure_windows_error_mode()
            logger.info("Worker starting on %s:%s", config.host, config.port)
            status_manager.mark_api_server_starting("FastAPI startup in progress")
            status_manager.append_log("Worker started")
            status_manager.set_render_device_preference(config.renderDevicePreference)
            logger.info("Render device preference loaded: %s", config.renderDevicePreference)
            status_manager.append_log(f"Render device preference: {config.renderDevicePreference}")
            status_manager.mark_startup_ready("API server is ready")
            status_manager.append_log("Worker API ready")

            try:
                coordinator_discovery_service.start()
            except Exception as exc:
                message = f"Coordinator discovery failed to start: {exc}"
                logger.exception("Coordinator discovery failed during startup")
                status_manager.set_error(message)
                status_manager.append_log(message)

            background_thread = threading.Thread(
                target=run_background_startup_tasks,
                name="worker-background-startup",
                daemon=True,
            )
            background_thread.start()
            app.state.background_startup_thread = background_thread
            logger.info("Worker ready with status=%s", status_manager.status_snapshot()["status"])
        except Exception as exc:
            message = f"Worker startup failed before readiness: {exc}"
            logger.exception("Worker startup failed before readiness")
            status_manager.mark_startup_error(message)
            raise

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        logger.info("Worker shutdown requested")
        status_manager.append_log("Worker shutting down")
        coordinator_discovery_service.stop()
        coordinator_reporter.stop()
        if status_manager.api_server_state != "error":
            status_manager.mark_api_server_stopped("Worker shutting down")
        if blender_runner.is_busy():
            logger.info("Active Blender work detected during shutdown, stopping session")
            blender_runner.shutdown()

    @app.get("/health", response_model=HealthResponse, dependencies=[Depends(require_auth)])
    async def health() -> dict[str, object]:
        logger.debug("GET /health")
        return status_manager.health_snapshot()

    @app.get("/status", dependencies=[Depends(require_auth)])
    async def status() -> dict[str, object]:
        logger.debug("GET /status")
        return status_manager.status_snapshot()

    @app.get("/capabilities", dependencies=[Depends(require_auth)])
    async def capabilities() -> dict[str, object]:
        logger.info("GET /capabilities")
        snapshot = status_manager.status_snapshot()
        return capabilities_service.as_dict(
            blender_found=bool(snapshot["blenderFound"]),
            blender_path=str(snapshot["blenderPath"]),
            blender_version=str(snapshot["blenderVersion"]),
        )

    @app.post(
        "/validate-paths",
        response_model=ValidatePathsResponse,
        dependencies=[Depends(require_auth)],
    )
    async def validate_paths_endpoint(payload: ValidatePathsRequest) -> ValidatePathsResponse:
        logger.info("POST /validate-paths blendFile=%s outputFolder=%s", payload.blendFile, payload.outputFolder)
        status_manager.append_log(f"Validating paths for {payload.blendFile}")
        previous_status = status_manager.start_validation()
        try:
            validation = validate_paths(
                payload.blendFile,
                payload.outputFolder,
                logger,
                blender_runner=blender_runner,
                required_blender_version=payload.requiredBlenderVersion,
                preferred_blender_major=payload.preferredBlenderMajor,
                version_routing_policy=payload.versionRoutingPolicy,
                blend_version_detected_hint=payload.blendVersionDetected,
            )
            status_manager.append_log(f"Path validation: {validation.message}")
            return validation
        finally:
            status_manager.finish_validation(previous_status)

    @app.post(
        "/render-block",
        response_model=RenderAcceptedResponse,
        dependencies=[Depends(require_auth)],
    )
    async def render_block(payload: RenderBlockRequest) -> JSONResponse:
        logger.info(
            "POST /render-block job=%s block=%s frames=%s-%s",
            payload.jobId,
            payload.blockId,
            payload.startFrame,
            payload.endFrame,
        )
        status_manager.append_log(f"Received render request {payload.jobId}/{payload.blockId}")

        # Delivery is at-least-once: a coordinator may retry after losing the
        # HTTP response. Reusing the same assignment must never start a second
        # Blender render for the same frame.
        if payload.assignmentId:
            snapshot = status_manager.status_snapshot()
            assignment_keys: set[tuple[str, str, str]] = set()
            current_assignment_id = snapshot.get("currentAssignmentId")
            if current_assignment_id:
                assignment_keys.add(
                    (
                        str(current_assignment_id),
                        str(snapshot.get("currentJobId") or ""),
                        str(snapshot.get("currentBlockId") or ""),
                    )
                )
            for queued in snapshot.get("queuedAssignments") or []:
                if isinstance(queued, dict) and queued.get("assignmentId"):
                    assignment_keys.add(
                        (
                            str(queued["assignmentId"]),
                            str(queued.get("jobId") or ""),
                            str(queued.get("blockId") or ""),
                        )
                    )
            last_job = snapshot.get("lastJob")
            if isinstance(last_job, dict) and last_job.get("assignmentId"):
                assignment_keys.add(
                    (
                        str(last_job["assignmentId"]),
                        str(last_job.get("jobId") or ""),
                        str(last_job.get("blockId") or ""),
                    )
                )
            replay_key = (str(payload.assignmentId), payload.jobId, payload.blockId)
            if replay_key in assignment_keys:
                return JSONResponse(
                    status_code=202,
                    content={
                        "accepted": True,
                        "ok": True,
                        "idempotentReplay": True,
                        "jobId": payload.jobId,
                        "blockId": payload.blockId,
                        "assignmentId": payload.assignmentId,
                        "status": snapshot.get("status") or "rendering",
                        "currentJobId": snapshot.get("currentJobId"),
                        "currentBlockId": snapshot.get("currentBlockId"),
                        "currentAssignmentId": snapshot.get("currentAssignmentId"),
                        "currentFrame": snapshot.get("currentFrame"),
                        "queueDepth": snapshot.get("sessionQueueDepth"),
                        "sessionState": snapshot.get("sessionState"),
                        "persistentSessionEnabled": snapshot.get("persistentSessionEnabled"),
                        "renderDevicePreference": config.renderDevicePreference,
                        "effectiveUseGpu": render_device_preference_to_use_gpu(
                            config.renderDevicePreference
                        ),
                    },
                )

        if not config.allowJobs:
            status_manager.set_ready()
            return JSONResponse(
                status_code=403,
                content={"accepted": False, "error": "jobs_disabled"},
            )

        output_folder = str(Path(payload.outputPattern).parent)
        validation = validate_paths(
            payload.blendFile,
            output_folder,
            logger,
            blender_runner=blender_runner,
            required_blender_version=payload.requiredBlenderVersion,
            preferred_blender_major=payload.preferredBlenderMajor,
            version_routing_policy=payload.versionRoutingPolicy,
            blend_version_detected_hint=payload.blendVersionDetected,
        )
        status_manager.append_log(f"Render path validation: {validation.message}")
        if not validation.ok:
            error_code = "incompatible_blender_version" if not validation.compatible else "invalid_paths"
            return JSONResponse(
                status_code=400,
                content={
                    "accepted": False,
                    "error": error_code,
                    "details": validation.errors,
                    "message": validation.message,
                    "blendVersionDetected": validation.blendVersionDetected,
                    "versionSource": validation.versionSource,
                    "blendHeaderFormat": validation.blendHeaderFormat,
                    "blendCompression": validation.blendCompression,
                    "requiredBlenderVersion": validation.requiredBlenderVersion,
                    "preferredBlenderMajor": validation.preferredBlenderMajor,
                    "versionRoutingPolicy": validation.versionRoutingPolicy,
                    "selectedBlenderVersion": validation.selectedBlenderVersion,
                    "selectedBlenderPath": validation.selectedBlenderPath,
                    "selectedVersionBucket": validation.selectedVersionBucket,
                    "sameMajorSufficientAvailable": validation.sameMajorSufficientAvailable,
                    "sameMajorOlderAvailable": validation.sameMajorOlderAvailable,
                    "bestSameMajorOlderVersion": validation.bestSameMajorOlderVersion,
                    "higherMajorAvailable": validation.higherMajorAvailable,
                    "bestHigherMajorVersion": validation.bestHigherMajorVersion,
                    "compatible": validation.compatible,
                    "compatibilityReason": validation.compatibilityReason,
                },
            )

        accepted, error = blender_runner.start_render(
            payload,
            selected_blender_path=validation.selectedBlenderPath,
            selected_blender_version=validation.selectedBlenderVersion,
            blend_version_detected=validation.blendVersionDetected,
        )
        if not accepted:
            snapshot = status_manager.status_snapshot()
            active_summary = status_manager.get_active_render_summary()
            status_code = 400 if error == "incompatible_blender_version" else 409
            return JSONResponse(
                status_code=status_code,
                content={
                    "accepted": False,
                    "ok": False,
                    "status": (
                        "busy"
                        if status_code == 409 and not config.persistentSessionEnabled
                        else snapshot.get("status") or "busy"
                    ),
                    "error": error or "unknown_error",
                    "message": "Worker ja esta renderizando." if status_code == 409 else validation.message,
                    "currentJobId": snapshot.get("currentJobId") or active_summary.get("jobId"),
                    "currentBlockId": snapshot.get("currentBlockId") or active_summary.get("blockId"),
                    "currentAssignmentId": snapshot.get("currentAssignmentId") or active_summary.get("assignmentId"),
                    "assignmentId": active_summary.get("assignmentId"),
                    "queueDepth": snapshot.get("sessionQueueDepth"),
                    "sessionState": snapshot.get("sessionState"),
                    "drainRequested": snapshot.get("sessionDrainRequested"),
                    "persistentSessionEnabled": snapshot.get("persistentSessionEnabled"),
                    "blendVersionDetected": validation.blendVersionDetected,
                    "versionSource": validation.versionSource,
                    "blendHeaderFormat": validation.blendHeaderFormat,
                    "blendCompression": validation.blendCompression,
                    "requiredBlenderVersion": validation.requiredBlenderVersion,
                    "preferredBlenderMajor": validation.preferredBlenderMajor,
                    "versionRoutingPolicy": validation.versionRoutingPolicy,
                    "selectedBlenderVersion": validation.selectedBlenderVersion,
                    "selectedBlenderPath": validation.selectedBlenderPath,
                    "selectedVersionBucket": validation.selectedVersionBucket,
                    "sameMajorSufficientAvailable": validation.sameMajorSufficientAvailable,
                    "sameMajorOlderAvailable": validation.sameMajorOlderAvailable,
                    "bestSameMajorOlderVersion": validation.bestSameMajorOlderVersion,
                    "higherMajorAvailable": validation.higherMajorAvailable,
                    "bestHigherMajorVersion": validation.bestHigherMajorVersion,
                    "compatible": validation.compatible,
                    "compatibilityReason": validation.compatibilityReason,
                },
            )

        snapshot = status_manager.status_snapshot()
        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "jobId": payload.jobId,
                "blockId": payload.blockId,
                "status": snapshot.get("status") or "rendering",
                "ok": True,
                "renderDevicePreference": config.renderDevicePreference,
                "effectiveUseGpu": render_device_preference_to_use_gpu(
                    config.renderDevicePreference
                ),
                "assignmentId": payload.assignmentId,
                "currentJobId": snapshot.get("currentJobId"),
                "currentBlockId": snapshot.get("currentBlockId"),
                "currentAssignmentId": snapshot.get("currentAssignmentId"),
                "currentFrame": snapshot.get("currentFrame"),
                "coordinatorId": payload.coordinatorId,
                "submittedByMasterId": payload.submittedByMasterId,
                "submittedByMasterName": payload.submittedByMasterName,
                "blendVersionDetected": validation.blendVersionDetected,
                "versionSource": validation.versionSource,
                "blendHeaderFormat": validation.blendHeaderFormat,
                "blendCompression": validation.blendCompression,
                "requiredBlenderVersion": validation.requiredBlenderVersion,
                "preferredBlenderMajor": validation.preferredBlenderMajor,
                "versionRoutingPolicy": validation.versionRoutingPolicy,
                "selectedBlenderVersion": validation.selectedBlenderVersion,
                "selectedBlenderPath": validation.selectedBlenderPath,
                "selectedVersionBucket": validation.selectedVersionBucket,
                "sameMajorSufficientAvailable": validation.sameMajorSufficientAvailable,
                "sameMajorOlderAvailable": validation.sameMajorOlderAvailable,
                "bestSameMajorOlderVersion": validation.bestSameMajorOlderVersion,
                "higherMajorAvailable": validation.higherMajorAvailable,
                "bestHigherMajorVersion": validation.bestHigherMajorVersion,
                "compatible": validation.compatible,
                "compatibilityReason": validation.compatibilityReason,
                "queueDepth": snapshot.get("sessionQueueDepth"),
                "sessionState": snapshot.get("sessionState"),
                "drainRequested": snapshot.get("sessionDrainRequested"),
                "persistentSessionEnabled": snapshot.get("persistentSessionEnabled"),
            },
        )

    @app.get(
        "/render-device",
        response_model=RenderDevicePreferenceResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_render_device() -> dict[str, object]:
        logger.info("GET /render-device")
        return build_render_device_response(config.renderDevicePreference)

    @app.post(
        "/render-device",
        response_model=RenderDevicePreferenceResponse,
        dependencies=[Depends(require_auth)],
    )
    async def set_render_device(payload: RenderDevicePreferenceRequest) -> dict[str, object]:
        logger.info("POST /render-device preference=%s", payload.renderDevicePreference)
        saved_preference = config_manager.set_render_device_preference(payload.renderDevicePreference)
        status_manager.set_render_device_preference(saved_preference)
        status_manager.append_log(f"Render device preference changed to {saved_preference}")
        coordinator_discovery_service.request_reregister()
        return build_render_device_response(saved_preference)

    @app.post("/cancel", response_model=CancelResponse, dependencies=[Depends(require_auth)])
    async def cancel(payload: CancelRequest | None = None) -> dict[str, object]:
        logger.info(
            "POST /cancel jobId=%s assignmentId=%s",
            payload.jobId if payload else None,
            payload.assignmentId if payload else None,
        )
        return blender_runner.cancel_render(
            requested_job_id=payload.jobId if payload else None,
            requested_assignment_id=payload.assignmentId if payload else None,
        )

    @app.post(
        "/job-drain",
        response_model=JobDrainResponse,
        dependencies=[Depends(require_auth)],
    )
    async def job_drain(payload: JobDrainRequest) -> dict[str, object]:
        logger.info("POST /job-drain jobId=%s coordinatorId=%s", payload.jobId, payload.coordinatorId)
        return blender_runner.request_job_drain(
            job_id=payload.jobId,
            coordinator_id=payload.coordinatorId,
        )

    @app.get("/logs", response_model=LogsResponse, dependencies=[Depends(require_auth)])
    async def logs() -> dict[str, object]:
        logger.info("GET /logs")
        return {"logs": status_manager.get_logs()}

    app.state.config = config
    app.state.config_manager = config_manager
    app.state.logger = logger
    app.state.status_manager = status_manager
    app.state.capabilities_service = capabilities_service
    app.state.blender_runner = blender_runner
    app.state.coordinator_discovery_service = coordinator_discovery_service
    app.state.coordinator_reporter = coordinator_reporter
    app.state.render_device_window = render_device_window
    app.state.background_startup_thread = None
    return app
