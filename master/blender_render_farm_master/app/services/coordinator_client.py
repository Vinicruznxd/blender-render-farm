from __future__ import annotations

from typing import Any
import json
import requests


class CoordinatorClientError(Exception):
    """Raised when a coordinator request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class CoordinatorClient:
    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.session.request(
                method=method,
                url=f"{self.base_url}{endpoint}",
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            response = exc.response
            detail = self._extract_error_detail(response) if response is not None else ""
            message = str(exc)
            if detail:
                message = f"{message} | Coordinator detail: {detail}"
            raise CoordinatorClientError(
                message,
                status_code=response.status_code if response is not None else None,
                response_text=response.text if response is not None else None,
            ) from exc
        except requests.RequestException as exc:
            raise CoordinatorClientError(str(exc)) from exc

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise CoordinatorClientError("Resposta JSON invalida do coordinator.") from exc

    @staticmethod
    def _extract_error_detail(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict) and "detail" in payload:
            detail = payload.get("detail")
            if isinstance(detail, str):
                return detail.strip()
            if detail is not None:
                return json.dumps(detail, ensure_ascii=False)

        text = (response.text or "").strip()
        return text[:500] if text else ""

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def master_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/masters/heartbeat", payload)

    def masters(self) -> dict[str, Any]:
        return self._request("GET", "/masters")

    def state(self) -> dict[str, Any]:
        return self._request("GET", "/state")

    def update_language(self, language: str) -> dict[str, Any]:
        return self._request("PUT", "/settings/language", {"language": language})

    def jobs(self) -> dict[str, Any]:
        return self._request("GET", "/jobs")

    def job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jobs/{job_id}")

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/jobs", payload)

    def preflight_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/jobs/preflight", payload)

    def pause_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/pause")

    def resume_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/resume")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/cancel")

    def workers(self) -> dict[str, Any]:
        return self._request("GET", "/workers")

    def add_worker(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/workers", payload)

    def update_worker(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/workers/{worker_id}", payload)

    def delete_worker(self, worker_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/workers/{worker_id}")

    def discover_workers(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", "/workers/discover", payload or {})

    def test_worker(self, worker_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", f"/workers/{worker_id}/test", payload or {})

    def logs(self) -> dict[str, Any]:
        return self._request("GET", "/logs")
