from __future__ import annotations

import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI


class CoordinatorServerRuntime:
    def __init__(self, app: FastAPI, startup_timeout_seconds: float = 10.0):
        self.app = app
        self.settings = app.state.settings
        self.startup_timeout_seconds = startup_timeout_seconds
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        return bool(
            self._server
            and self._thread
            and self._thread.is_alive()
            and self._server.started
        )

    @property
    def startup_error(self) -> BaseException | None:
        return self._startup_error

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return

            discovery = getattr(self.app.state, "coordinator_discovery", None)
            if discovery and self.settings.coordinator_discovery_enabled:
                existing = discovery.probe_for_existing_coordinator()
                if existing:
                    api_urls = existing.get("apiUrls") or []
                    location = api_urls[0] if api_urls else existing.get("hostname", "unknown")
                    raise RuntimeError(
                        f"Ja existe um Coordinator ativo na rede: {existing.get('coordinatorId')} em {location}."
                    )
                discovery.assert_can_bind()

            self._startup_error = None
            config = uvicorn.Config(
                app=self.app,
                host=self.settings.host,
                port=self.settings.port,
                reload=False,
                lifespan="on",
                log_level="info",
                log_config=None,
                access_log=False,
            )
            self._server = uvicorn.Server(config)
            self._thread = threading.Thread(target=self._run_server, name="coordinator-uvicorn", daemon=False)
            self._thread.start()
            self._wait_until_started()

    def stop(self) -> None:
        with self._lock:
            if not self._server or not self._thread:
                return
            self._server.should_exit = True
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                self._server.force_exit = True
                self._thread.join(timeout=2.0)
            self._server = None
            self._thread = None

    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.port}"

    def docs_url(self) -> str:
        return f"{self.local_url()}/docs"

    def dashboard_url(self) -> str:
        return f"{self.local_url()}/dashboard"

    def state_url(self) -> str:
        return f"{self.local_url()}/state"

    def _run_server(self) -> None:
        assert self._server is not None
        try:
            self._server.run()
        except BaseException as exc:  # pragma: no cover - defensive thread boundary
            self._startup_error = exc

    def _wait_until_started(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            app_startup_error = getattr(self.app.state, "startup_error", None)
            if app_startup_error is not None:
                raise RuntimeError("Falha ao iniciar o servidor do Coordinator.") from app_startup_error
            if self._startup_error is not None:
                raise RuntimeError("Falha ao iniciar o servidor do Coordinator.") from self._startup_error
            if self._server and self._server.started:
                return
            if self._thread and not self._thread.is_alive():
                raise RuntimeError(
                    "O servidor do Coordinator encerrou durante a inicializacao. "
                    f"Verifique se a porta HTTP {self.settings.port} ou a porta UDP {self.app.state.settings.coordinator_discovery_port} ja estao em uso."
                )
            time.sleep(0.05)
        raise RuntimeError("O servidor do Coordinator nao conseguiu iniciar dentro do tempo esperado.")
