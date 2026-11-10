from __future__ import annotations

from typing import Any
import requests

from app.core.models import RenderDevicePreference, Worker


class WorkerClientError(Exception):
    """Raised when a worker request fails."""


class WorkerClient:
    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def _base_url(self, worker: Worker) -> str:
        return f"http://{worker.ip}:{worker.port}"

    def _request(
        self,
        method: str,
        worker: Worker,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.session.request(
                method=method,
                url=f"{self._base_url(worker)}{endpoint}",
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise WorkerClientError(str(exc)) from exc

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise WorkerClientError("Resposta JSON inválida do worker.") from exc

    def health(self, worker: Worker) -> dict[str, Any]:
        return self._request("GET", worker, "/health")

    def status(self, worker: Worker) -> dict[str, Any]:
        return self._request("GET", worker, "/status")

    def capabilities(self, worker: Worker) -> dict[str, Any]:
        return self._request("GET", worker, "/capabilities")

    def render_device(self, worker: Worker) -> dict[str, Any]:
        return self._request("GET", worker, "/render-device")

    def set_render_device(
        self,
        worker: Worker,
        render_device_preference: RenderDevicePreference,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            worker,
            "/render-device",
            {
                "renderDevicePreference": render_device_preference,
            },
        )

    def validate_paths(
        self,
        worker: Worker,
        blend_file: str,
        output_folder: str,
        *,
        required_blender_version: str | None = None,
        preferred_blender_major: int | None = None,
        version_routing_policy: str | None = None,
        blend_version_detected: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "blendFile": blend_file,
            "outputFolder": output_folder,
        }
        if required_blender_version is not None:
            payload["requiredBlenderVersion"] = required_blender_version
        if preferred_blender_major is not None:
            payload["preferredBlenderMajor"] = preferred_blender_major
        if version_routing_policy is not None:
            payload["versionRoutingPolicy"] = version_routing_policy
        if blend_version_detected is not None:
            payload["blendVersionDetected"] = blend_version_detected
        return self._request("POST", worker, "/validate-paths", payload)

    def render_block(self, worker: Worker, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", worker, "/render-block", payload)

    def cancel(self, worker: Worker, job_id: str | None = None) -> dict[str, Any]:
        payload = {"jobId": job_id} if job_id else {}
        return self._request("POST", worker, "/cancel", payload)

    def logs(self, worker: Worker) -> dict[str, Any]:
        return self._request("GET", worker, "/logs")
