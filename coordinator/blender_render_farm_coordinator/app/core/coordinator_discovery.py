from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable

from app.core.settings import AppSettings


DISCOVERY_PROTOCOL = "blender-render-farm-coordinator"
DISCOVERY_VERSION = 1
DISCOVER_MESSAGE_TYPE = "discover"
ANNOUNCE_MESSAGE_TYPE = "announce"


class DuplicateCoordinatorError(RuntimeError):
    """Raised when another coordinator is already active in the same namespace."""


@dataclass(slots=True)
class CoordinatorDiscoverySnapshot:
    coordinator_id: str
    namespace: str
    discovery_port: int
    api_port: int
    started_at: str
    hostname: str
    addresses: list[str]
    api_urls: list[str]
    enabled: bool
    status: str
    language: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": DISCOVERY_PROTOCOL,
            "version": DISCOVERY_VERSION,
            "messageType": ANNOUNCE_MESSAGE_TYPE,
            "coordinatorId": self.coordinator_id,
            "namespace": self.namespace,
            "discoveryPort": self.discovery_port,
            "apiPort": self.api_port,
            "startedAt": self.started_at,
            "hostname": self.hostname,
            "addresses": self.addresses,
            "apiUrls": self.api_urls,
            "enabled": self.enabled,
            "status": self.status,
            "language": self.language,
        }


class CoordinatorDiscoveryService:
    def __init__(
        self,
        settings: AppSettings,
        logger: Any,
        *,
        started_at: str,
        status_getter: Callable[[], str] | None = None,
        discovery_targets_override: list[str] | None = None,
    ):
        self.settings = settings
        self.logger = logger
        self.started_at = started_at
        self.status_getter = status_getter or (lambda: "running")
        self.discovery_targets_override = discovery_targets_override
        self._stop_event = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._announcer_thread: threading.Thread | None = None
        self._listen_socket: socket.socket | None = None
        self._hostname = socket.gethostname()
        self._started = False

    def start(self) -> None:
        if not self.settings.coordinator_discovery_enabled:
            return
        if self._started or (self._listener_thread and self._listener_thread.is_alive()):
            return

        existing = self.probe_for_existing_coordinator()
        if existing:
            raise DuplicateCoordinatorError(
                "Another coordinator is already active in the same LAN namespace: "
                f"{existing.get('coordinatorId')} at {existing.get('apiUrls', [])}"
            )

        self._stop_event.clear()
        self._listen_socket = self._bind_listener_socket()
        self._listener_thread = threading.Thread(target=self._listen_loop, name="coordinator-discovery-listener", daemon=True)
        self._announcer_thread = threading.Thread(target=self._announce_loop, name="coordinator-discovery-announcer", daemon=True)
        self._listener_thread.start()
        self._announcer_thread.start()
        self._started = True
        self.logger.info(
            "Coordinator LAN discovery started.",
            category="general",
            details={
                "namespace": self.settings.coordinator_discovery_namespace,
                "discoveryPort": self.settings.coordinator_discovery_port,
                "apiUrls": self.snapshot().get("apiUrls", []),
            },
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._listen_socket:
            self._listen_socket.close()
            self._listen_socket = None
        for thread in (self._listener_thread, self._announcer_thread):
            if thread and thread.is_alive():
                thread.join(timeout=1.0)
        self._listener_thread = None
        self._announcer_thread = None
        self._started = False

    def snapshot(self) -> dict[str, Any]:
        data = CoordinatorDiscoverySnapshot(
            coordinator_id=self.settings.coordinator_id,
            namespace=self.settings.coordinator_discovery_namespace,
            discovery_port=self.settings.coordinator_discovery_port,
            api_port=self.settings.port,
            started_at=self.started_at,
            hostname=self._hostname,
            addresses=self._listening_addresses(),
            api_urls=self._api_urls(),
            enabled=self.settings.coordinator_discovery_enabled,
            status=str(self.status_getter()) if self.settings.coordinator_discovery_enabled else "disabled",
            language=self.settings.language,
        )
        return data.to_dict()

    def probe_for_existing_coordinator(self) -> dict[str, Any] | None:
        responses = self.discover_coordinators(
            port=self.settings.coordinator_discovery_port,
            namespace=self.settings.coordinator_discovery_namespace,
            timeout_seconds=self.settings.coordinator_discovery_probe_timeout_seconds,
            target_hosts=self.discovery_targets_override,
        )
        return responses[0] if responses else None

    def assert_can_bind(self) -> None:
        if not self.settings.coordinator_discovery_enabled:
            return
        test_socket = self._bind_listener_socket()
        test_socket.close()

    @classmethod
    def discover_coordinators(
        cls,
        *,
        port: int,
        namespace: str,
        timeout_seconds: float,
        target_hosts: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        request_payload = {
            "protocol": DISCOVERY_PROTOCOL,
            "version": DISCOVERY_VERSION,
            "messageType": DISCOVER_MESSAGE_TYPE,
            "namespace": namespace,
        }
        message = json.dumps(request_payload, ensure_ascii=True).encode("utf-8")
        destinations = target_hosts or cls._broadcast_targets()
        responses: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
            probe_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            probe_socket.settimeout(timeout_seconds)
            for destination in destinations:
                try:
                    probe_socket.sendto(message, (destination, port))
                except OSError:
                    continue

            while True:
                try:
                    payload, _ = probe_socket.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError:
                    break

                parsed = cls._parse_payload(payload)
                if not parsed:
                    continue
                if parsed.get("protocol") != DISCOVERY_PROTOCOL:
                    continue
                if parsed.get("messageType") != ANNOUNCE_MESSAGE_TYPE:
                    continue
                if parsed.get("namespace") != namespace:
                    continue
                coordinator_id = str(parsed.get("coordinatorId") or "")
                if coordinator_id in seen_ids:
                    continue
                seen_ids.add(coordinator_id)
                responses.append(parsed)

        return responses

    def _listen_loop(self) -> None:
        while not self._stop_event.is_set():
            current_socket = self._listen_socket
            if current_socket is None:
                return
            try:
                payload, address = current_socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return

            message = self._parse_payload(payload)
            if not message:
                continue
            if message.get("protocol") != DISCOVERY_PROTOCOL:
                continue
            if message.get("namespace") != self.settings.coordinator_discovery_namespace:
                continue

            if message.get("messageType") == DISCOVER_MESSAGE_TYPE:
                self._send_announce(to_host=address[0], to_port=address[1])

    def _announce_loop(self) -> None:
        while not self._stop_event.is_set():
            self.broadcast_announce()
            self._stop_event.wait(self.settings.coordinator_discovery_broadcast_interval_seconds)

    def broadcast_announce(self) -> None:
        payload = self.snapshot()
        self._broadcast(payload)

    def _send_announce(self, *, to_host: str, to_port: int) -> None:
        payload = self.snapshot()
        self._send_payload(payload, to_host=to_host, to_port=to_port, broadcast=False)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        for host in self.discovery_targets_override or self._broadcast_targets():
            self._send_payload(payload, to_host=host, to_port=self.settings.coordinator_discovery_port, broadcast=True)

    def _send_payload(self, payload: dict[str, Any], *, to_host: str, to_port: int, broadcast: bool) -> None:
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as outbound_socket:
            if broadcast:
                outbound_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                outbound_socket.sendto(data, (to_host, to_port))
            except OSError:
                return

    def _api_urls(self) -> list[str]:
        host = str(self.settings.host or "0.0.0.0").strip().strip("[]")
        if host not in {"0.0.0.0", "::", ""}:
            authority = f"[{host}]" if ":" in host else host
            return [f"http://{authority}:{self.settings.port}"]
        urls = [f"http://{address}:{self.settings.port}" for address in self._local_ipv4_addresses()]
        hostname_url = f"http://{self._hostname}:{self.settings.port}"
        if hostname_url not in urls:
            urls.append(hostname_url)
        return urls

    def _listening_addresses(self) -> list[str]:
        host = str(self.settings.host or "0.0.0.0").strip().strip("[]")
        return [host] if host not in {"0.0.0.0", "::", ""} else self._local_ipv4_addresses()

    def _local_ipv4_addresses(self) -> list[str]:
        addresses = {"127.0.0.1"}
        try:
            _, _, host_ips = socket.gethostbyname_ex(self._hostname)
            addresses.update(ip for ip in host_ips if "." in ip)
        except socket.gaierror:
            pass
        try:
            addresses.add(socket.gethostbyname(self._hostname))
        except socket.gaierror:
            pass
        valid_with_order: list[tuple[int, str]] = []
        for ip in addresses:
            try:
                parsed = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if parsed.version != 4:
                continue
            if parsed.is_loopback or parsed.is_private:
                order = 1 if parsed.is_loopback else 0
                valid_with_order.append((order, ip))
        valid_with_order.sort(key=lambda item: (item[0], item[1]))
        return [ip for _, ip in valid_with_order]

    @classmethod
    def _broadcast_targets(cls) -> list[str]:
        targets = {"255.255.255.255", "127.255.255.255"}
        for ip in cls._static_local_ipv4_addresses():
            try:
                parsed = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if parsed.version != 4:
                continue
            if not (parsed.is_private or parsed.is_loopback):
                continue
            network = ipaddress.ip_network(f"{ip}/24", strict=False)
            targets.add(str(network.broadcast_address))
        return sorted(targets)

    @staticmethod
    def _static_local_ipv4_addresses() -> list[str]:
        hostname = socket.gethostname()
        addresses = {"127.0.0.1"}
        try:
            _, _, host_ips = socket.gethostbyname_ex(hostname)
            addresses.update(ip for ip in host_ips if "." in ip)
        except socket.gaierror:
            pass
        try:
            addresses.add(socket.gethostbyname(hostname))
        except socket.gaierror:
            pass
        return sorted(addresses)

    @staticmethod
    def _parse_payload(payload: bytes) -> dict[str, Any] | None:
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    def _bind_listener_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        try:
            sock.bind(("", self.settings.coordinator_discovery_port))
        except OSError as exc:
            sock.close()
            self._raise_bind_conflict(exc)
        return sock

    def _raise_bind_conflict(self, exc: OSError) -> None:
        existing = self.probe_for_existing_coordinator()
        if existing:
            api_urls = existing.get("apiUrls") or []
            location = api_urls[0] if api_urls else existing.get("hostname", "desconhecido")
            raise DuplicateCoordinatorError(
                "A porta UDP de discovery ja esta em uso por um Coordinator ativo: "
                f"{existing.get('coordinatorId')} em {location}."
            ) from exc

        port = self.settings.coordinator_discovery_port
        message = (
            f"A porta UDP de discovery {port} ja esta em uso por outro processo nesta maquina. "
            "Se voce estiver rodando Worker e Master localmente, eles nao devem escutar fixamente essa porta; "
            "somente o Coordinator deve fazer bind nela. Worker e Master devem enviar probes de discovery a partir de portas efemeras."
        )
        if os.name == "nt" and getattr(exc, "winerror", None) == 10048:
            raise RuntimeError(message) from exc
        raise RuntimeError(message) from exc
