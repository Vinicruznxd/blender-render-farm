from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import WorkerConfig
from .status_manager import WorkerStatusManager

DISCOVERY_PROTOCOL = "blender-render-farm-coordinator"
DISCOVERY_NAMESPACE = "blender_render_farm"
DISCOVERY_VERSION = 1
DISCOVERY_PROBE_INTERVAL_SECONDS = 5.0
DISCOVERY_SOCKET_TIMEOUT_SECONDS = 1.0
DISCOVERY_HEALTH_CHECK_INTERVAL_SECONDS = 5.0
DISCOVERY_REGISTER_RETRY_INTERVAL_SECONDS = 5.0
DISCOVERY_HTTP_TIMEOUT_SECONDS = 3.0
DISCOVERY_LOOP_IDLE_SECONDS = 0.2


def build_discovery_bind_error_message(port: int, exc: OSError) -> str:
    message = f"Coordinator discovery could not bind UDP socket on port {port}: {exc}"
    if getattr(exc, "winerror", None) == 10013:
        message += (
            " Another process is likely already listening on this UDP port without "
            "allowing shared binds. This commonly happens when the Coordinator and "
            "Worker run on the same Windows machine and the other process owns UDP "
            f"{port} exclusively."
        )
    return message


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_discovery_probe(*, namespace: str = DISCOVERY_NAMESPACE) -> dict[str, object]:
    return {
        "protocol": DISCOVERY_PROTOCOL,
        "version": DISCOVERY_VERSION,
        "messageType": "discover",
        "namespace": namespace,
    }


def build_candidate_api_urls(payload: dict[str, Any]) -> list[str]:
    api_urls: list[str] = []

    raw_api_urls = payload.get("apiUrls")
    if isinstance(raw_api_urls, list):
        for value in raw_api_urls:
            if isinstance(value, str) and value.strip():
                api_urls.append(value.strip().rstrip("/"))

    if api_urls:
        return list(dict.fromkeys(api_urls))

    api_port = payload.get("apiPort")
    addresses = payload.get("addresses")
    if isinstance(api_port, int) and isinstance(addresses, list):
        for address in addresses:
            if isinstance(address, str) and address.strip():
                api_urls.append(f"http://{address.strip()}:{api_port}")

    return list(dict.fromkeys(api_urls))


@dataclass(slots=True)
class CoordinatorAnnouncement:
    coordinator_id: str
    hostname: str
    api_port: int
    discovery_port: int
    started_at: str
    api_urls: list[str]


def parse_coordinator_announcement(
    payload: dict[str, Any],
    *,
    expected_namespace: str = DISCOVERY_NAMESPACE,
) -> CoordinatorAnnouncement | None:
    if payload.get("protocol") != DISCOVERY_PROTOCOL:
        return None
    if int(payload.get("version", 0)) != DISCOVERY_VERSION:
        return None
    if payload.get("messageType") != "announce":
        return None
    if payload.get("namespace") != expected_namespace:
        return None
    if payload.get("enabled") is False:
        return None
    if str(payload.get("status", "running")).strip().lower() != "running":
        return None

    coordinator_id = str(payload.get("coordinatorId", "")).strip()
    if not coordinator_id:
        return None

    hostname = str(payload.get("hostname", "")).strip() or coordinator_id
    started_at = str(payload.get("startedAt", "")).strip()
    discovery_port = int(payload.get("discoveryPort", 0) or 0)
    api_port = int(payload.get("apiPort", 0) or 0)
    api_urls = build_candidate_api_urls(payload)
    if not api_urls:
        return None

    return CoordinatorAnnouncement(
        coordinator_id=coordinator_id,
        hostname=hostname,
        api_port=api_port,
        discovery_port=discovery_port,
        started_at=started_at,
        api_urls=api_urls,
    )


@dataclass(slots=True)
class CoordinatorBinding:
    coordinator_id: str
    hostname: str
    api_url: str
    api_urls: list[str]
    started_at: str
    last_seen_at: str


class CoordinatorDiscoveryService:
    def __init__(
        self,
        *,
        config: WorkerConfig,
        status_manager: WorkerStatusManager,
        logger,
    ) -> None:
        self.config = config
        self.status_manager = status_manager
        self.logger = logger
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.RLock()
        self._socket: socket.socket | None = None
        self._passive_discovery_message: str | None = None
        self._binding: CoordinatorBinding | None = None
        self._registered = False
        self._force_reregister = False
        self._last_probe_at = 0.0
        self._last_health_check_at = 0.0
        self._last_register_attempt_at = 0.0

    def start(self) -> None:
        if not self.config.coordinatorDiscoveryEnabled:
            self.status_manager.set_coordinator_disabled()
            self.logger.info("Coordinator discovery disabled by config")
            return

        if self._thread and self._thread.is_alive():
            return

        self.status_manager.set_coordinator_discovering("Waiting for coordinator discovery")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="coordinator-discovery",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)

    def request_reregister(self) -> None:
        with self._state_lock:
            self._registered = False
            self._force_reregister = True
            self._last_register_attempt_at = 0.0

    def _run_loop(self) -> None:
        try:
            self._socket = self._create_discovery_socket()
        except OSError as exc:
            message = build_discovery_bind_error_message(
                self.config.coordinatorDiscoveryPort,
                exc,
            )
            self._socket = None
            self._passive_discovery_message = (
                f"{message} Falling back to active probe mode."
            )
            self.logger.warning(self._passive_discovery_message)
            self.status_manager.set_coordinator_discovering(
                self._passive_discovery_message
            )
            self.status_manager.append_log(self._passive_discovery_message)
        else:
            self._passive_discovery_message = None

        if self._socket is not None:
            self.logger.info(
                "Coordinator discovery started on UDP port %s",
                self.config.coordinatorDiscoveryPort,
            )
            self.status_manager.append_log("Coordinator discovery started")
        else:
            self.logger.info(
                "Coordinator discovery started in active probe mode on UDP port %s",
                self.config.coordinatorDiscoveryPort,
            )
            self.status_manager.append_log("Coordinator discovery started in active probe mode")

        while not self._stop_event.is_set():
            now = time.monotonic()
            if self._should_send_probe(now):
                if self._socket is not None:
                    self._send_discovery_probe()
                else:
                    self._probe_for_announcements()
                self._last_probe_at = now

            self._maybe_monitor_coordinator(now)
            if self._socket is not None:
                self._receive_announce()
            else:
                self._stop_event.wait(DISCOVERY_LOOP_IDLE_SECONDS)

        self.logger.info("Coordinator discovery stopped")

    def _create_discovery_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", self.config.coordinatorDiscoveryPort))
        sock.settimeout(DISCOVERY_SOCKET_TIMEOUT_SECONDS)
        return sock

    def _should_send_probe(self, now: float) -> bool:
        with self._state_lock:
            return self._binding is None and (
                now - self._last_probe_at >= DISCOVERY_PROBE_INTERVAL_SECONDS
            )

    def _send_discovery_probe(self) -> None:
        sock = self._socket
        if sock is None:
            return

        payload = json.dumps(build_discovery_probe()).encode("utf-8")
        try:
            sock.sendto(payload, ("255.255.255.255", self.config.coordinatorDiscoveryPort))
            self.logger.info(
                "Sent coordinator discovery probe to UDP %s",
                self.config.coordinatorDiscoveryPort,
            )
            self.status_manager.append_log("Coordinator discovery probe sent")
        except OSError as exc:
            self.logger.warning("Failed to send discovery probe: %s", exc)

    def _maybe_monitor_coordinator(self, now: float) -> None:
        with self._state_lock:
            binding = self._binding
            needs_register = binding is not None and (
                self._force_reregister or not self._registered
            )
            should_register = needs_register and (
                now - self._last_register_attempt_at >= DISCOVERY_REGISTER_RETRY_INTERVAL_SECONDS
            )
            should_health_check = binding is not None and (
                now - self._last_health_check_at >= DISCOVERY_HEALTH_CHECK_INTERVAL_SECONDS
            )

        if binding is None:
            return

        if should_health_check:
            self._last_health_check_at = now
            if not self._validate_coordinator_health(binding.api_url):
                message = (
                    f"Coordinator {binding.coordinator_id} at {binding.api_url} became unavailable"
                )
                self.logger.warning(message)
                self.status_manager.append_log(message)
                self._clear_binding(message)
                return
            self.status_manager.set_coordinator_seen(
                coordinator_id=binding.coordinator_id,
                api_url=binding.api_url,
                hostname=binding.hostname,
                last_seen_at=utc_now_iso(),
            )

        if should_register:
            self._last_register_attempt_at = now
            self._register_with_coordinator(binding)

    def _receive_announce(self) -> None:
        sock = self._socket
        if sock is None:
            return

        try:
            data, address = sock.recvfrom(65535)
        except socket.timeout:
            return
        except OSError as exc:
            if not self._stop_event.is_set():
                self.logger.debug("Discovery socket recv failed: %s", exc)
            return

        self._handle_announce_datagram(data, source_ip=address[0])

    def _probe_for_announcements(self) -> None:
        payload = json.dumps(build_discovery_probe()).encode("utf-8")
        targets = self._build_probe_targets()

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
                probe_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                probe_socket.settimeout(DISCOVERY_SOCKET_TIMEOUT_SECONDS)
                probe_socket.bind(("", 0))

                sent = False
                for host in targets:
                    try:
                        probe_socket.sendto(
                            payload,
                            (host, self.config.coordinatorDiscoveryPort),
                        )
                        sent = True
                    except OSError as exc:
                        self.logger.debug(
                            "Failed to send discovery probe to %s:%s: %s",
                            host,
                            self.config.coordinatorDiscoveryPort,
                            exc,
                        )

                if sent:
                    self.logger.info(
                        "Sent coordinator discovery probe to UDP %s via active probe mode",
                        self.config.coordinatorDiscoveryPort,
                    )
                    self.status_manager.append_log("Coordinator discovery probe sent")

                while not self._stop_event.is_set():
                    try:
                        data, address = probe_socket.recvfrom(65535)
                    except socket.timeout:
                        break
                    except OSError as exc:
                        self.logger.debug("Active discovery probe recv failed: %s", exc)
                        break

                    self._handle_announce_datagram(data, source_ip=address[0])
        except OSError as exc:
            self.logger.warning("Failed to open active discovery probe socket: %s", exc)

    def _build_probe_targets(self) -> list[str]:
        targets = ["255.255.255.255", "127.0.0.1"]
        local_ip = str(self.status_manager.ip or "").strip()
        if local_ip and local_ip not in {"0.0.0.0", "127.0.0.1"}:
            targets.append(local_ip)
        return list(dict.fromkeys(targets))

    def _handle_announce_datagram(self, data: bytes, *, source_ip: str) -> None:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.logger.debug("Ignoring non-JSON UDP datagram from %s", source_ip)
            return

        if not isinstance(payload, dict):
            return

        announcement = parse_coordinator_announcement(payload)
        if announcement is None:
            return

        self._handle_announcement(announcement)

    def _handle_announcement(self, announcement: CoordinatorAnnouncement) -> None:
        now_iso = utc_now_iso()
        with self._state_lock:
            current = self._binding

        if current is not None and current.coordinator_id != announcement.coordinator_id:
            self.logger.debug(
                "Ignoring coordinator %s because worker is currently bound to %s",
                announcement.coordinator_id,
                current.coordinator_id,
            )
            return

        if current is not None and current.coordinator_id == announcement.coordinator_id:
            updated = CoordinatorBinding(
                coordinator_id=current.coordinator_id,
                hostname=announcement.hostname,
                api_url=current.api_url,
                api_urls=announcement.api_urls,
                started_at=announcement.started_at,
                last_seen_at=now_iso,
            )
            with self._state_lock:
                self._binding = updated
            self.status_manager.set_coordinator_seen(
                coordinator_id=updated.coordinator_id,
                api_url=updated.api_url,
                hostname=updated.hostname,
                last_seen_at=updated.last_seen_at,
            )
            return

        api_url = self._choose_accessible_api_url(announcement.api_urls)
        if api_url is None:
            self.logger.warning(
                "Received coordinator announce from %s but no apiUrl was reachable",
                announcement.coordinator_id,
            )
            return

        binding = CoordinatorBinding(
            coordinator_id=announcement.coordinator_id,
            hostname=announcement.hostname,
            api_url=api_url,
            api_urls=announcement.api_urls,
            started_at=announcement.started_at,
            last_seen_at=now_iso,
        )
        with self._state_lock:
            self._binding = binding
            self._registered = False
            self._force_reregister = True
            self._last_register_attempt_at = 0.0
            self._last_health_check_at = 0.0

        self.logger.info(
            "Coordinator discovered id=%s apiUrl=%s",
            binding.coordinator_id,
            binding.api_url,
        )
        self.status_manager.set_coordinator_connecting(
            coordinator_id=binding.coordinator_id,
            api_url=binding.api_url,
            hostname=binding.hostname,
            last_seen_at=binding.last_seen_at,
        )
        self.status_manager.append_log(
            f"Coordinator discovered: {binding.coordinator_id} at {binding.api_url}"
        )

    def _choose_accessible_api_url(self, api_urls: list[str]) -> str | None:
        for api_url in api_urls:
            if self._validate_coordinator_health(api_url):
                return api_url.rstrip("/")
        return None

    def _validate_coordinator_health(self, api_url: str) -> bool:
        url = f"{api_url.rstrip('/')}/health"
        try:
            status_code, payload, _raw = self._http_request_json(url=url, method="GET")
        except OSError as exc:
            self.logger.debug("Coordinator health check failed for %s: %s", url, exc)
            return False

        if status_code != 200:
            self.logger.debug("Coordinator health check returned %s for %s", status_code, url)
            return False

        if isinstance(payload, dict) and payload.get("ok") is False:
            return False
        if isinstance(payload, dict) and payload.get("language"):
            self.status_manager.set_ui_language(str(payload["language"]))
        return True

    def _register_with_coordinator(self, binding: CoordinatorBinding) -> bool:
        url = f"{binding.api_url.rstrip('/')}/workers"
        payload = self.status_manager.get_worker_registration_payload()
        try:
            status_code, _response_payload, raw_body = self._http_request_json(
                url=url,
                method="POST",
                payload=payload,
            )
        except OSError as exc:
            message = f"Worker registration to coordinator failed: {exc}"
            self.logger.warning(message)
            self.status_manager.set_coordinator_error(message)
            self.status_manager.append_log(message)
            with self._state_lock:
                self._registered = False
                self._force_reregister = True
            return False

        if status_code in {200, 201, 202, 204, 409}:
            registered_at = utc_now_iso()
            with self._state_lock:
                self._registered = True
                self._force_reregister = False
                if self._binding and self._binding.coordinator_id == binding.coordinator_id:
                    self._binding = CoordinatorBinding(
                        coordinator_id=binding.coordinator_id,
                        hostname=binding.hostname,
                        api_url=binding.api_url,
                        api_urls=binding.api_urls,
                        started_at=binding.started_at,
                        last_seen_at=binding.last_seen_at,
                    )
            self.status_manager.set_coordinator_registered(
                coordinator_id=binding.coordinator_id,
                api_url=binding.api_url,
                hostname=binding.hostname,
                last_seen_at=binding.last_seen_at,
                last_registered_at=registered_at,
            )
            self.status_manager.append_log(
                f"Registered with coordinator {binding.coordinator_id} at {binding.api_url}"
            )
            self.logger.info(
                "Worker registered with coordinator id=%s status=%s",
                binding.coordinator_id,
                status_code,
            )
            return True

        message = (
            f"Coordinator registration returned {status_code}"
            + (f": {raw_body}" if raw_body else "")
        )
        self.logger.warning(message)
        self.status_manager.set_coordinator_error(message)
        self.status_manager.append_log(message)
        with self._state_lock:
            self._registered = False
            self._force_reregister = True
        return False

    def _clear_binding(self, reason: str | None = None) -> None:
        with self._state_lock:
            self._binding = None
            self._registered = False
            self._force_reregister = False
            self._last_health_check_at = 0.0
            self._last_register_attempt_at = 0.0
            self._last_probe_at = 0.0
        self.status_manager.set_coordinator_discovering(reason)

    def _http_request_json(
        self,
        *,
        url: str,
        method: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, Any] | None, str]:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url=url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=DISCOVERY_HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return response.status, self._parse_optional_json(raw), raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return exc.code, self._parse_optional_json(raw), raw
        except (urllib.error.URLError, TimeoutError) as exc:
            raise OSError(str(exc)) from exc

    def _parse_optional_json(self, raw: str) -> dict[str, Any] | None:
        text = raw.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
