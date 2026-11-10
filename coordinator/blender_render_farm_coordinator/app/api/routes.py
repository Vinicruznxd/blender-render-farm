from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.dashboard import build_dashboard_html
from app.api.schemas import (
    JobCreateRequest,
    JobPreflightRequest,
    LanguageUpdateRequest,
    LogsResponse,
    MasterHeartbeatRequest,
    WorkerCreateRequest,
    WorkerDiscoverRequest,
    WorkerRenderReportRequest,
    WorkerUpdateRequest,
)
from app.core.coordinator_discovery import CoordinatorDiscoveryService
from app.core.coordinator_service import CoordinatorService
from app.core.database import Database
from app.core.frame_scanner import FrameScanner
from app.core.models import NotFoundError
from app.core.runtime_metadata import APP_VERSION
from app.core.scheduler import BackgroundScheduler
from app.core.settings import AppSettings
from app.core.worker_registry import WorkerRegistry
from app.services.logger import AppLogger
from app.services.worker_client import WorkerClient


def create_app(settings: AppSettings | None = None, worker_client: WorkerClient | None = None) -> FastAPI:
    resolved_settings = settings or AppSettings.load()
    app_root = resolved_settings.project_root
    icon_dir = app_root / "img"
    if not icon_dir.exists():
        icon_dir = Path(__file__).resolve().parents[2] / "img"
    icon_url = "/img/icon_coordinator.png"
    database = Database(resolved_settings.database_path)
    database.initialize()
    logger = AppLogger(resolved_settings, database)
    resolved_worker_client = worker_client or WorkerClient(resolved_settings.worker_request_timeout_seconds)
    discovery_worker_client = worker_client or WorkerClient(resolved_settings.discovery_timeout_seconds)
    worker_registry = WorkerRegistry(
        resolved_settings,
        database,
        logger,
        resolved_worker_client,
        discovery_worker_client=discovery_worker_client,
    )
    coordinator_discovery = CoordinatorDiscoveryService(
        resolved_settings,
        logger,
        started_at="",
        status_getter=lambda: "running",
    )
    service = CoordinatorService(
        resolved_settings,
        database,
        logger,
        worker_registry,
        FrameScanner(),
        resolved_worker_client,
        coordinator_discovery=coordinator_discovery,
    )
    coordinator_discovery.started_at = service.started_at
    scheduler = BackgroundScheduler(
        service.run_scheduler_cycle,
        resolved_settings.scheduler_interval_seconds,
        on_error=lambda exc: logger.error(
            f"Scheduler cycle failed and will be retried: {type(exc).__name__}: {exc}",
            category="scheduler",
        ),
    )
    service.set_scheduler_status_getter(scheduler.snapshot)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.startup_error = None
        try:
            coordinator_discovery.start()
            if resolved_settings.auto_start_scheduler:
                scheduler.start()
            yield
        except Exception as exc:
            app.state.startup_error = exc
            raise
        finally:
            scheduler.stop()
            coordinator_discovery.stop()
            if hasattr(resolved_worker_client, "close"):
                resolved_worker_client.close()
            if discovery_worker_client is not resolved_worker_client and hasattr(discovery_worker_client, "close"):
                discovery_worker_client.close()

    app = FastAPI(
        title="Blender Render Farm Coordinator",
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.mount("/img", StaticFiles(directory=icon_dir), name="img")
    app.state.settings = resolved_settings
    app.state.database = database
    app.state.logger = logger
    app.state.worker_client = resolved_worker_client
    app.state.worker_registry = worker_registry
    app.state.service = service
    app.state.scheduler = scheduler
    app.state.coordinator_discovery = coordinator_discovery

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> RedirectResponse:
        return RedirectResponse(url=icon_url)

    @app.get("/docs", include_in_schema=False)
    def custom_swagger_ui_html() -> object:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - Swagger UI",
            swagger_favicon_url=icon_url,
        )

    @app.get("/redoc", include_in_schema=False)
    def custom_redoc_html() -> object:
        return get_redoc_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - ReDoc",
            redoc_favicon_url=icon_url,
        )

    @app.get("/dashboard", include_in_schema=False, response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(build_dashboard_html())

    @app.get("/health")
    def health(request: Request) -> dict:
        return _service(request).health()

    @app.post("/masters/heartbeat")
    def master_heartbeat(request: Request, payload: MasterHeartbeatRequest) -> dict:
        return _run_or_400(lambda: _service(request).record_master_heartbeat(payload.model_dump()))

    @app.get("/masters")
    def list_masters(request: Request) -> list[dict]:
        return _service(request).list_masters()

    @app.get("/state")
    def state(request: Request) -> dict:
        return _service(request).get_state()

    @app.get("/settings/language")
    def get_language(request: Request) -> dict:
        return _service(request).language_state()

    @app.put("/settings/language")
    def update_language(request: Request, payload: LanguageUpdateRequest) -> dict:
        return _run_or_400(lambda: _service(request).set_language(payload.language))

    @app.get("/jobs")
    def list_jobs(request: Request) -> list[dict]:
        return _service(request).list_jobs()

    @app.get("/jobs/{job_id}")
    def get_job(request: Request, job_id: str) -> dict:
        return _run_or_404(lambda: _service(request).get_job(job_id))

    @app.get("/jobs/{job_id}/report.pdf")
    def get_job_report_pdf(request: Request, job_id: str) -> Response:
        filename, pdf_bytes = _run_or_404(lambda: _service(request).build_job_report_pdf(job_id))
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/jobs")
    def create_job(request: Request, payload: JobCreateRequest) -> dict:
        return _run_or_400(lambda: _service(request).create_job(payload.model_dump()))

    @app.post("/jobs/preflight")
    def preflight_job(request: Request, payload: JobPreflightRequest) -> dict:
        return _run_or_400(lambda: _service(request).preflight_job(payload.model_dump()))

    @app.post("/jobs/{job_id}/pause")
    def pause_job(request: Request, job_id: str) -> dict:
        return _run(lambda: _service(request).pause_job(job_id))

    @app.post("/jobs/{job_id}/resume")
    def resume_job(request: Request, job_id: str) -> dict:
        return _run(lambda: _service(request).resume_job(job_id))

    @app.post("/jobs/{job_id}/cancel")
    def cancel_job(request: Request, job_id: str) -> dict:
        return _run(lambda: _service(request).cancel_job(job_id))

    @app.get("/workers")
    def list_workers(request: Request) -> list[dict]:
        return _service(request).list_workers()

    @app.post("/workers")
    def create_worker(request: Request, payload: WorkerCreateRequest) -> dict:
        return _run_or_400(lambda: _service(request).register_worker(payload.model_dump(exclude_none=True)))

    @app.put("/workers/{worker_id}")
    def update_worker(request: Request, worker_id: str, payload: WorkerUpdateRequest) -> dict:
        return _run(lambda: _service(request).update_worker(worker_id, payload.model_dump(exclude_none=True)))

    @app.delete("/workers/{worker_id}")
    def delete_worker(request: Request, worker_id: str) -> dict:
        return _run(lambda: _service(request).delete_worker(worker_id))

    @app.post("/workers/discover")
    def discover_workers(request: Request, payload: WorkerDiscoverRequest | None = Body(default=None)) -> dict:
        hosts = payload.hosts if payload else None
        ports = payload.ports if payload else None
        return _run_or_400(lambda: _service(request).discover_workers(hosts, ports))

    @app.post("/workers/{worker_id}/test")
    def test_worker(request: Request, worker_id: str) -> dict:
        return _run(lambda: _service(request).test_worker(worker_id))

    @app.post("/workers/{worker_id}/render-reports")
    def worker_render_report(
        request: Request,
        worker_id: str,
        payload: WorkerRenderReportRequest,
    ) -> dict:
        return _run(
            lambda: _service(request).record_worker_render_report(
                worker_id,
                payload.model_dump(exclude_none=True),
            )
        )

    @app.get("/logs", response_model=LogsResponse)
    def logs(request: Request) -> LogsResponse:
        return LogsResponse(**_service(request).get_logs())

    return app


def _service(request: Request) -> CoordinatorService:
    return request.app.state.service


def _run(callback):
    try:
        return callback()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _run_or_400(callback):
    try:
        return callback()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _run_or_404(callback):
    try:
        return callback()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
