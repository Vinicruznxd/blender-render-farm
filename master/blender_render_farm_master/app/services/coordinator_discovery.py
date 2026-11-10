from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
import json
import socket
import threading
import time
from typing import Any, Iterable


DISCOVERY_PROTOCOL = "blender-render-farm-coordinator"
DISCOVERY_VERSION = 1
DISCOVERY_NAMESPACE = "blender_render_farm"
DISCOVERY_PORT = 8011
DISCOVERY_PROBE = {
    "protocol": DISCOVERY_PROTOCOL,
    "version": DISCOVERY_VERSION,
    "messageType": "discover",
    "namespace": DISCOVERY_NAMESPACE,
}


@dataclass(slots=True)
class CoordinatorAnnouncement:
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
    received_at: float
    source_ip: str
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coordinatorId": self.coordinator_id,
            "namespace": self.namespace,
            "discoveryPort": self.discovery_port,
            "apiPort": self.api_port,
            "startedAt": self.started_at,
            "hostname": self.hostname,
            "addresses": list(self.addresses),
            "apiUrls": list(self.api_urls),
            "enabled": self.enabled,
            "status": self.status,
            "receivedAt": self.received_at,
            "sourceIp": self.source_ip,
        }


class CoordinatorDiscoveryService:
    def __init__(
        self,
        *,
        namespace: str = DISCOVERY_NAMESPACE,
        udp_port: int = DISCOVERY_PORT,
        listen_port: int | None = None,
        probe_timeout_seconds: float = 0.75,
        announcement_ttl_seconds: float = 15.0,
        probe_interval_seconds: float = 3.0,
        broadcast_hosts: Iterable[str] | None = None,
    ) -> None:
        self.namespace = namespace
        self.udp_port = int(udp_port)
        self.listen_port = int(listen_port if listen_port is not None else udp_port)
        self.probe_timeout_seconds = float(probe_timeout_seconds)
        self.announcement_ttl_seconds = float(announcement_ttl_seconds)
        self.probe_interval_seconds = float(probe_interval_seconds)
        self.broadcast_hosts = [host.strip() for host in (broadcast_hosts or []) if host and host.strip()]
        self._announcements: dict[str, CoordinatorAnnouncement] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._listener_socket: socket.socket | None = None
        self._listen_error = ""
        self._last_probe_at = 0.0

    @property
    def listen_error(self) -> str:
        return self._listen_error

    def start(self) -> None:
        if self._listener_thread and self._listener_thread.is_alive():
            return
        self._stop_event.clear()
        self._listener_thread = threading.Thread(
            target=self._listen_loop,
            name="CoordinatorDiscoveryListener",
            daemon=True,
        )
        self._listener_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        listener_socket = self._listener_socket
        if listener_socket is not None:
            try:
                listener_socket.close()
            except OSError:
                pass
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=1.0)
        self._listener_thread = None
        self._listener_socket = None

    def recent_announcements(self) -> list[CoordinatorAnnouncement]:
        with self._lock:
            self._prune_locked(time.time())
            return sorted(
                self._announcements.values(),
                key=lambda item: (item.coordinator_id, item.hostname, item.source_ip),
            )

    def probe(self, *, force: bool = False, wait_seconds: float | None = None) -> list[CoordinatorAnnouncement]:
        now = time.time()
        if not force and now - self._last_probe_at < self.probe_interval_seconds:
            return self.recent_announcements()

        self._last_probe_at = now
        payload = json.dumps(
            {
                **DISCOVERY_PROBE,
                "namespace": self.namespace,
            }
        ).encode("utf-8")
        timeout_seconds = self.probe_timeout_seconds if wait_seconds is None else float(wait_seconds)
        broadcast_targets = {(host, self.udp_port) for host in self._broadcast_targets()}
        if not broadcast_targets:
            broadcast_targets.add(("255.255.255.255", self.udp_port))

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
            probe_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            probe_socket.settimeout(timeout_seconds)
            probe_socket.bind(("", 0))
            for host, port in sorted(broadcast_targets):
                try:
                    probe_socket.sendto(payload, (host, port))
                except OSError:
                    continue

            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                try:
                    raw_payload, address = probe_socket.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError:
                    break
                self._handle_datagram(raw_payload, address[0])

        return self.recent_announcements()

    def _listen_loop(self) -> None:
        try:
            listener_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            listener_socket.bind(("", self.listen_port))
            listener_socket.settimeout(0.5)
            self._listener_socket = listener_socket
            self._listen_error = ""
        except OSError as exc:
            self._listener_socket = None
            self._listen_error = str(exc)
            return

        while not self._stop_event.is_set():
            try:
                raw_payload, address = listener_socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_datagram(raw_payload, address[0])

        try:
            listener_socket.close()
        except OSError:
            pass

    def _handle_datagram(self, raw_payload: bytes, source_ip: str) -> None:
        announcement = self._parse_announcement(raw_payload, source_ip)
        if announcement is None:
            return
        with self._lock:
            self._announcements[announcement.coordinator_id] = announcement
            self._prune_locked(time.time())

    def _parse_announcement(
        self,
        raw_payload: bytes,
        source_ip: str,
    ) -> CoordinatorAnnouncement | None:
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

        if not isinstance(payload, dict):
            return None
        if payload.get("protocol") != DISCOVERY_PROTOCOL:
            return None
        if int(payload.get("version", 0)) != DISCOVERY_VERSION:
            return None
        if payload.get("messageType") != "announce":
            return None
        if payload.get("namespace") != self.namespace:
            return None

        coordinator_id = str(payload.get("coordinatorId") or "").strip()
        if not coordinator_id:
            return None

        api_port = int(payload.get("apiPort") or 8010)
        discovery_port = int(payload.get("discoveryPort") or self.udp_port)
        addresses = self._dedupe_strings(payload.get("addresses") or [])
        if source_ip and source_ip not in addresses:
            addresses.append(source_ip)

        api_urls = self._dedupe_strings(payload.get("apiUrls") or [])
        if not api_urls:
            api_urls = [f"http://{address}:{api_port}" for address in addresses if address]

        return CoordinatorAnnouncement(
            coordinator_id=coordinator_id,
            namespace=self.namespace,
            discovery_port=discovery_port,
            api_port=api_port,
            started_at=str(payload.get("startedAt") or ""),
            hostname=str(payload.get("hostname") or coordinator_id),
            addresses=addresses,
            api_urls=api_urls,
            enabled=bool(payload.get("enabled", True)),
            status=str(payload.get("status") or "running"),
            received_at=time.time(),
            source_ip=source_ip,
            raw_payload=payload,
        )

    def _broadcast_targets(self) -> set[str]:
        if self.broadcast_hosts:
            return set(self.broadcast_hosts)

        targets = {"255.255.255.255"}
        for address in self._local_ipv4_addresses():
            try:
                ip_value = IPv4Address(address)
            except ValueError:
                continue
            if ip_value.is_loopback:
                continue
            network = IPv4Network(f"{address}/24", strict=False)
            targets.add(str(network.broadcast_address))
        return targets

    def _local_ipv4_addresses(self) -> set[str]:
        addresses = {"127.0.0.1"}
        hostnames = {socket.gethostname(), socket.getfqdn()}
        for hostname in hostnames:
            if not hostname:
                continue
            try:
                addresses.update(socket.gethostbyname_ex(hostname)[2])
            except OSError:
                continue
            try:
                for item in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
                    addresses.add(item[4][0])
            except OSError:
                continue
        return {item for item in addresses if item}

    def _prune_locked(self, now: float) -> None:
        expired = [
            coordinator_id
            for coordinator_id, item in self._announcements.items()
            if now - item.received_at > self.announcement_ttl_seconds
        ]
        for coordinator_id in expired:
            self._announcements.pop(coordinator_id, None)

    def _dedupe_strings(self, items: Iterable[Any]) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for item in items:
            value = str(item or "").strip().rstrip("/")
            if not value or value in seen:
                continue
            seen.add(value)
            values.append(value)
        return values
