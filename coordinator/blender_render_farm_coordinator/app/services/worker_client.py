from __future__ import annotations

from typing import Any

import httpx


class WorkerRequestError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        response_json: dict[str, Any] | None = None,
        response_text: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_json = response_json or {}
        self.response_text = response_text


class WorkerClient:
    def __init__(self, timeout_seconds: float = 5.0):
        self.timeout_seconds = timeout_seconds
        self._client = httpx.Client(timeout=self.timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def health(self, base_url: str) -> dict[str, Any]:
        return self._request_json("GET", base_url, "/health")

    def status(self, base_url: str) -> dict[str, Any]:
        return self._request_json("GET", base_url, "/status")

    def capabilities(self, base_url: str) -> dict[str, Any]:
        return self._request_json("GET", base_url, "/capabilities")

    def validate_paths(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", base_url, "/validate-paths", payload)

    def render_block(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", base_url, "/render-block", payload)

    def cancel(self, base_url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("POST", base_url, "/cancel", payload or {})

    def job_drain(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", base_url, "/job-drain", payload)

    def logs(self, base_url: str) -> str:
        response = self._request("GET", base_url, "/logs")
        return response.text

    def get_render_device(self, base_url: str) -> dict[str, Any]:
        return self._request_json("GET", base_url, "/render-device")

    def set_render_device(self, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", base_url, "/render-device", payload)

    def _request_json(
        self,
        method: str,
        base_url: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._request(method, base_url, path, payload)
        if not response.text.strip():
            return {}
        return response.json()

    def _request(
        self,
        method: str,
        base_url: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> httpx.Response:
        response = self._client.request(method, f"{base_url}{path}", json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            response_json: dict[str, Any] | None = None
            try:
                response_json = response.json() if response.text.strip() else {}
            except ValueError:
                response_json = None
            detail = response_json.get("message") if isinstance(response_json, dict) else None
            if not detail:
                detail = response.text.strip() or str(exc)
            raise WorkerRequestError(
                response.status_code,
                detail,
                response_json=response_json,
                response_text=response.text,
            ) from exc
        return response
