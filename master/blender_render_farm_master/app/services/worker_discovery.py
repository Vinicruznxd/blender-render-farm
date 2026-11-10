from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
import socket
from typing import Any, Iterable

from app.core.models import Worker, payload_value
from app.services.worker_client import WorkerClient, WorkerClientError


@dataclass
class DiscoveredWorker:
    name: str
    ip: str
    port: int
    health_payload: dict[str, Any] = field(default_factory=dict)
    status_payload: dict[str, Any] = field(default_factory=dict)
    capabilities_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass
class WorkerDiscoveryRun:
    candidates: list[tuple[str, int]]
    scanned_networks: list[str]
    ports: list[int]
    discovered: list[DiscoveredWorker]


class WorkerDiscoveryService:
    def __init__(self, timeout_seconds: float = 0.75, max_workers: int = 64) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_workers = max_workers

    def discover(
        self,
        *,
        default_port: int,
        known_workers: Iterable[Worker],
        hosts: Iterable[str] | None = None,
        ports: Iterable[int] | None = None,
    ) -> WorkerDiscoveryRun:
        candidate_hosts, scanned_networks = self._candidate_hosts(known_workers, hosts)
        candidate_ports = self._candidate_ports(default_port, known_workers, ports)
        candidates = sorted((host, port) for host in candidate_hosts for port in candidate_ports)
        if not candidates:
            return WorkerDiscoveryRun(
                candidates=[],
                scanned_networks=scanned_networks,
                ports=candidate_ports,
                discovered=[],
            )

        client = WorkerClient(timeout_seconds=self.timeout_seconds)
        discovered: list[DiscoveredWorker] = []
        max_workers = min(self.max_workers, max(1, len(candidates)))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self._probe_candidate, client, host, port): (host, port)
                for host, port in candidates
            }
            for future in as_completed(future_map):
                item = future.result()
                if item is not None:
                    discovered.append(item)

        discovered.sort(key=lambda item: (item.name.lower(), item.ip, item.port))
        return WorkerDiscoveryRun(
            candidates=candidates,
            scanned_networks=scanned_networks,
            ports=candidate_ports,
            discovered=discovered,
        )

    def _probe_candidate(
        self,
        client: WorkerClient,
        host: str,
        port: int,
    ) -> DiscoveredWorker | None:
        worker = Worker(
            id=f"discovery-{host}-{port}",
            name=f"{host}:{port}",
            ip=host,
            port=port,
            enabled=True,
            selected=True,
        )
        try:
            health_payload = client.health(worker)
        except WorkerClientError:
            return None

        if health_payload.get("ok") is False:
            return None

        try:
            status_payload = client.status(worker)
        except WorkerClientError:
            status_payload = {
                "machineName": payload_value(health_payload, key="machineName") or worker.name,
                "status": payload_value(health_payload, key="status") or "idle",
            }

        try:
            capabilities_payload = client.capabilities(worker)
        except WorkerClientError:
            capabilities_payload = {}

        machine_name = payload_value(status_payload, capabilities_payload, health_payload, key="machineName")
        return DiscoveredWorker(
            name=str(machine_name or worker.name),
            ip=host,
            port=port,
            health_payload=health_payload,
            status_payload=status_payload,
            capabilities_payload=capabilities_payload,
        )

    def _candidate_hosts(
        self,
        known_workers: Iterable[Worker],
        hosts: Iterable[str] | None,
    ) -> tuple[set[str], list[str]]:
        if hosts is not None:
            return {host.strip() for host in hosts if host and host.strip()}, ["manual"]

        local_addresses = self._local_ipv4_addresses()
        host_set: set[str] = {"127.0.0.1"}
        network_strings: list[str] = []
        seen_networks: set[str] = set()

        for address in sorted(local_addresses):
            host_set.add(address)
            try:
                ip_value = IPv4Address(address)
            except ValueError:
                continue
            if ip_value.is_loopback or not ip_value.is_private or ip_value.is_link_local:
                continue
            network = IPv4Network(f"{address}/24", strict=False)
            network_str = str(network)
            if network_str not in seen_networks:
                seen_networks.add(network_str)
                network_strings.append(network_str)
            for candidate in network.hosts():
                host_set.add(str(candidate))

        for worker in known_workers:
            host_set.add(worker.ip)
            try:
                ip_value = IPv4Address(worker.ip)
            except ValueError:
                continue
            if ip_value.is_loopback or not ip_value.is_private or ip_value.is_link_local:
                continue
            network = IPv4Network(f"{worker.ip}/24", strict=False)
            network_str = str(network)
            if network_str not in seen_networks:
                seen_networks.add(network_str)
                network_strings.append(network_str)
            for candidate in network.hosts():
                host_set.add(str(candidate))

        return host_set, network_strings

    def _candidate_ports(
        self,
        default_port: int,
        known_workers: Iterable[Worker],
        ports: Iterable[int] | None,
    ) -> list[int]:
        if ports is not None:
            return sorted({int(port) for port in ports if int(port) > 0})

        port_set = {int(default_port)}
        port_set.update(int(worker.port) for worker in known_workers if int(worker.port) > 0)
        return sorted(port_set)

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
