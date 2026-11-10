from __future__ import annotations

from typing import Any
import json

import requests


class CoordinatorClientError(Exception):
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
    def __init__(self, base_url: str, timeout_seconds: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/jobs", payload)

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
