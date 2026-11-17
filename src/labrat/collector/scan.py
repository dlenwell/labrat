# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Find TCP service candidates inside explicitly allowed address ranges.

This is the reusable first stage of service discovery. It only opens and
closes TCP connections; it sends no data. An open port is a *candidate*, never
proof of a service or a physical device: a protocol probe (for example
``collector/nut.py``) must confirm each candidate with that service's own
read-only protocol before anything is recorded.

Scanning is bounded on purpose. Only hosts inside the ranges listed in
``[scan] ranges`` are ever contacted; nothing is inferred or expanded from
what is found. A scan covers at most :data:`MAX_SCAN_HOSTS` addresses, uses at
most :data:`SCAN_WORKERS` concurrent connections, and gives each connection
:data:`CONNECT_TIMEOUT_SECONDS`.

Used by: api.clients, collector.adapters.nut, discovery_settings.
"""

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import socket

#: Most addresses one scan may cover (a /20); larger configuration is refused.
MAX_SCAN_HOSTS = 4096

#: Concurrent connection attempts during a scan.
SCAN_WORKERS = 64

#: Seconds to wait for each TCP connection to be accepted.
CONNECT_TIMEOUT_SECONDS = 0.5


def allowed_hosts(ranges: Iterable[object]) -> list[str]:
    """Expand the configured ranges into the individual hosts LabRat may scan.

    Args:
        ranges: CIDR networks (``"192.0.2.0/24"``) or single addresses from
            ``[scan] ranges``.

    Returns:
        list[str]: Unique host addresses in configuration order. Network and
            broadcast addresses of IPv4 subnets are excluded.

    Raises:
        ValueError: A range is not a string, is malformed, or the ranges add
            up to more than :data:`MAX_SCAN_HOSTS` addresses.
    """
    hosts: dict[str, None] = {}
    for value in ranges:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("[scan] ranges must be a list of CIDR networks or addresses")
        try:
            network = ipaddress.ip_network(value.strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"[scan] range is not a valid network or address: {value}") from exc
        if network.num_addresses > MAX_SCAN_HOSTS:
            raise ValueError(f"[scan] range is larger than {MAX_SCAN_HOSTS} addresses: {value}")
        members = [network.network_address] if network.num_addresses == 1 else network.hosts()
        for address in members:
            hosts[str(address)] = None
            if len(hosts) > MAX_SCAN_HOSTS:
                raise ValueError(f"[scan] ranges add up to more than {MAX_SCAN_HOSTS} addresses")
    return list(hosts)


def port_open(host: str, port: int, timeout: float = CONNECT_TIMEOUT_SECONDS) -> bool:
    """Check whether a TCP connection to a host and port is accepted.

    Args:
        host: Address to try.
        port: TCP port.
        timeout: Seconds to wait for the connection.

    Returns:
        bool: True when the connection was accepted (and then closed).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_tcp(
    hosts: Iterable[str],
    port: int,
    *,
    check: Callable[[str, int], bool] = port_open,
    workers: int = SCAN_WORKERS,
) -> list[str]:
    """Return the hosts that accept TCP connections on a port.

    Args:
        hosts: Addresses from :func:`allowed_hosts`; nothing else is contacted.
        port: TCP port to try.
        check: Connection test; tests substitute their own.
        workers: Concurrent attempts.

    Returns:
        list[str]: Hosts with the port open, in the order given.
    """
    targets = list(hosts)
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets)))) as pool:
        results = list(pool.map(lambda host: check(host, port), targets))
    return [host for host, accepted in zip(targets, results) if accepted]
