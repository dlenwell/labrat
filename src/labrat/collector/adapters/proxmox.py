# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Discover Proxmox VE nodes, host NICs, and optional SNMP interface state.

The adapter uses the Proxmox JSON API under ``/api2/json``. A token with
read-only audit rights is sufficient; no VM or container endpoints are queried.
Only host NICs classified as ``eth`` by Proxmox are reported as physical ports.
Bridges, bonds, VLAN interfaces, taps, and guests are intentionally excluded.
Missing physical MACs are filled from each node's system report
(``/nodes/{node}/report``), which the same audit token can read; see
:mod:`labrat.collector.proxmox_report`. An optional SNMPv3 supplement can fill
any MACs still missing and report current link state for selected nodes.
Neither supplement creates another device observation.

Reference: https://pve.proxmox.com/pve-docs/api-viewer/

Used by: collector.__main__, collector.runner.
"""

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace
import ipaddress
import logging
import time
from typing import Any
from urllib.parse import quote

import requests

from labrat.collector.contracts import (
    DeviceObservation,
    DiscoveryBatch,
    NetworkInterfaceObservation,
    PortObservation,
)
from labrat.collector.http import JsonGetClient, controller_url, required_string, tls_verification
from labrat.collector.proxmox_report import interface_macs
from labrat.collector.snmp import read_physical_interfaces

_LOGGER = logging.getLogger(__name__)

#: How often each node's system report is re-read for NIC MACs, in seconds.
#: Producing a report runs many commands on the host and takes several seconds.
DEFAULT_REPORT_INTERVAL_SECONDS = 3600
MIN_REPORT_INTERVAL_SECONDS = 300
MAX_REPORT_INTERVAL_SECONDS = 86400


def _report_interval(settings: Mapping[str, Any]) -> int:
    """Read the report refresh interval; 0 turns report reading off.

    Args:
        settings: The Proxmox source's private settings.

    Returns:
        int: Seconds between report reads per node, or 0 when disabled.

    Raises:
        ValueError: The value is not 0 or an integer from 300 to 86400.
    """
    value = settings.get("report_interval_seconds", DEFAULT_REPORT_INTERVAL_SECONDS)
    if type(value) is not int or not (
        value == 0 or MIN_REPORT_INTERVAL_SECONDS <= value <= MAX_REPORT_INTERVAL_SECONDS
    ):
        raise ValueError(
            "Proxmox report_interval_seconds must be 0 or an integer from 300 to 86400"
        )
    return value


def _unwrap_data(payload: Any, path: str) -> list[dict[str, Any]]:
    """Extract a collection from a Proxmox JSON response envelope.

    Args:
        payload: Decoded response from ``/api2/json``.
        path: API path name for a non-sensitive schema error.

    Returns:
        list[dict[str, Any]]: Records from ``payload['data']``.

    Raises:
        ValueError: The response is not the expected list of objects.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError(f"Unexpected Proxmox collection schema for {path}")
    return data


def _is_physical_nic(
    item: Mapping[str, Any], *, exists_metadata_available: bool = False,
) -> bool:
    """Exclude known virtual device names from Proxmox Ethernet records.

    Args:
        item: Interface configuration returned by a Proxmox node.
        exists_metadata_available: True when this API response includes the
            ``exists`` flag for physical Ethernet records.

    Returns:
        bool: True when the interface is a candidate physical host NIC.

    Note:
        Proxmox normally labels physical ports ``eth``. Filtering common
        virtual-only prefixes is additional defensive protection against
        including guest tap/veth interfaces from controller variants. When the
        response exposes ``exists`` metadata, rows without an existing NIC are
        excluded; Proxmox may include configuration aliases without that flag.
    """
    iface = item.get("iface")
    if item.get("type") != "eth" or not isinstance(iface, str) or not iface.strip():
        return False
    if exists_metadata_available and item.get("exists") not in (1, True):
        return False
    return not iface.startswith((
        "tap", "veth", "fwln", "fwbr", "fwpr", "vnet", "vif", "vmbr",
    ))


def _mac_address(item: Mapping[str, Any]) -> str | None:
    """Extract the MAC if Proxmox exposes one for a host interface.

    Args:
        item: ``/nodes/{node}/network`` interface object.

    Returns:
        str | None: MAC address, or None when Proxmox omits it.
    """
    for key in ("hwaddress", "mac"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.lower()
    return None


def _interface_names(value: Any) -> tuple[str, ...]:
    """Normalize one Proxmox member list to stable interface names.

    Args:
        value: A string or collection returned for bond slaves or bridge ports.

    Returns:
        tuple[str, ...]: Nonempty interface names in source order.
    """
    values = value if isinstance(value, (list, tuple)) else str(value or "").split()
    return tuple(dict.fromkeys(
        item.strip() for item in values
        if isinstance(item, str) and item.strip().lower() not in {"", "none"}
    ))


def _logical_kind(value: Any) -> str | None:
    """Map a Proxmox logical-interface type to its display family.

    Args:
        value: The raw ``type`` field from a network configuration row.

    Returns:
        str | None: ``bond``, ``bridge``, or ``vlan`` when supported.
    """
    kind = str(value or "").strip().lower().replace("_", "")
    if kind in {"bond", "ovsbond"}:
        return "bond"
    if kind in {"bridge", "ovsbridge", "ovsintport", "ovsport"}:
        return "bridge"
    if kind == "vlan":
        return "vlan"
    return None


def _logical_interface(item: Mapping[str, Any], device_name: str) -> NetworkInterfaceObservation | None:
    """Convert one configured logical interface without retaining raw data.

    Args:
        item: One network record from the Proxmox node API.
        device_name: Stable Proxmox node name that owns the row.

    Returns:
        NetworkInterfaceObservation | None: Curated bond, bridge, or VLAN
            details. Physical Ethernet rows remain in :class:`PortObservation`.
    """
    name = item.get("iface")
    kind = _logical_kind(item.get("type"))
    if not isinstance(name, str) or not name.strip() or kind is None:
        return None
    members_key = "slaves" if kind == "bond" else "bridge_ports"
    members = _interface_names(item.get(members_key)) if kind in {"bond", "bridge"} else ()
    parent = item.get("vlan-raw-device") or item.get("vlan_raw_device")
    vlan_raw = item.get("vlan-id", item.get("vlan_id"))
    try:
        vlan_id = int(vlan_raw) if vlan_raw is not None else None
    except (TypeError, ValueError):
        vlan_id = None
    if vlan_id is not None and not 1 <= vlan_id <= 4094:
        vlan_id = None

    fields = (
        "address", "cidr", "gateway", "netmask", "method", "method6",
        "families", "autostart", "exists", "bridge_vlan_aware", "bridge_vids",
        "bridge_stp", "bridge_fd", "bond_miimon", "ovs_tag", "ovs_vlan_mode",
    )
    details = {
        key.replace("-", "_"): item[key]
        for key in fields
        if key in item and isinstance(item[key], (str, int, float, bool, list, type(None)))
    }
    details["configured_type"] = str(item.get("type") or kind)
    active = item.get("active")
    if type(active) is int:
        active = active == 1
    if type(active) is not bool:
        active = None
    return NetworkInterfaceObservation(
        device_remote_id=device_name,
        remote_id=name.strip(),
        name=name.strip(),
        kind=kind,
        parent_name=parent.strip() if isinstance(parent, str) and parent.strip() else None,
        vlan_id=vlan_id if kind == "vlan" else None,
        mode=str(item.get("bond_mode")) if kind == "bond" and item.get("bond_mode") else None,
        members=members,
        active=active,
        details=details,
    )


class ProxmoxAdapter:
    """Translate read-only Proxmox inventory responses into discovery batches."""

    def __init__(
        self, client: JsonGetClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a reusable HTTP client for node discovery.

        Args:
            client: Optional HTTP client for tests or connection reuse.
            clock: Monotonic clock in seconds; tests may supply their own.
        """
        self.client = client if client is not None else JsonGetClient()
        self.clock = clock
        # (API base, node) -> (last attempt, interface MACs). In memory only:
        # the report text itself is never kept.
        self._report_macs: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}
        self._report_current_macs: dict[tuple[str, str], dict[str, str]] = {}

    def _node_report_macs(
        self, base: str, node: str, headers: Mapping[str, str],
        verify: bool | str, interval: int,
    ) -> dict[str, str]:
        """Return a node's interface MACs, re-reading its report when due.

        A failed read keeps the previous result and waits a full interval
        before retrying, so an unreadable report never slows every cycle.

        Args:
            base: Proxmox ``/api2/json`` base URL.
            node: Node name.
            headers: Token headers.
            verify: Requests TLS verification setting.
            interval: Seconds between reads.

        Returns:
            dict[str, str]: MAC addresses by interface name, possibly empty.
        """
        key = (base, node)
        now = self.clock()
        cached = self._report_macs.get(key)
        if cached is not None and now - cached[0] < interval:
            return cached[1]
        previous = cached[1] if cached is not None else {}
        try:
            payload = self.client.get_json(
                f"{base}/nodes/{quote(node, safe='')}/report", headers, verify,
            )
        except (requests.RequestException, ValueError) as exc:
            _LOGGER.warning(
                "Proxmox report unavailable for node %s (%s); NIC MACs may be missing",
                node, type(exc).__name__,
            )
            self._report_macs[key] = (now, previous)
            return previous
        report = payload.get("data") if isinstance(payload, Mapping) else None
        macs = interface_macs(report) if isinstance(report, str) else {}
        if not macs:
            _LOGGER.warning("Proxmox report for node %s listed no interface MACs", node)
            macs = previous
        if isinstance(report, str) and macs:
            current = interface_macs(report, permanent=False)
            if current:
                self._report_current_macs[key] = current
        self._report_macs[key] = (now, macs)
        return macs

    def poll(self, settings: Mapping[str, Any]) -> DiscoveryBatch:
        """Fetch node identity and physical NICs, never virtual workloads.

        Args:
            settings: ``url``, ``token_id``, ``token_secret`` and optional TLS
                verification parameters from private local configuration.
                ``report_interval_seconds`` (default 3600, 0 disables) sets how
                often each node's system report is read for missing NIC MACs.
                An optional ``snmp`` table can configure SNMPv3 for selected
                nodes.

        Returns:
            DiscoveryBatch: One server per cluster node, cluster management IPs
                when available, physical NICs, and logical-interface details.

        Raises:
            ValueError: Credentials or API response structures are invalid.
            requests.RequestException: The controller cannot be contacted.

        Note:
            Offline nodes are retained in the batch even when their network
            interfaces cannot be queried. Stale NIC records remain in SQLite
            until a later successful scan refreshes them.
        """
        base = controller_url(settings) + "/api2/json"
        report_interval = _report_interval(settings)
        token_id = required_string(settings, "token_id")
        token_secret = required_string(settings, "token_secret")
        verify = tls_verification(settings)
        headers = {
            "Accept": "application/json",
            "Authorization": f"PVEAPIToken={token_id}={token_secret}",
        }
        management_ips: dict[str, list[str]] = {}
        try:
            status_rows = _unwrap_data(
                self.client.get_json(f"{base}/cluster/status", headers, verify),
                "/cluster/status",
            )
        except (requests.RequestException, ValueError):
            # Node enumeration and NIC discovery still work with older or
            # narrowly scoped Proxmox tokens that cannot read cluster status.
            _LOGGER.info("Proxmox cluster status unavailable; LLDP host matching is limited")
        else:
            for status in status_rows:
                if status.get("type") != "node":
                    continue
                name = status.get("name")
                value = status.get("ip")
                if not isinstance(name, str) or not isinstance(value, str):
                    continue
                try:
                    address = str(ipaddress.ip_interface(value).ip)
                except ValueError:
                    try:
                        address = str(ipaddress.ip_address(value))
                    except ValueError:
                        continue
                management_ips.setdefault(name.strip(), []).append(address)
        rows = _unwrap_data(self.client.get_json(f"{base}/nodes", headers, verify), "/nodes")
        devices: list[DeviceObservation] = []
        ports: list[PortObservation] = []
        network_interfaces: list[NetworkInterfaceObservation] = []
        for row in rows:
            name = row.get("node")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Proxmox node missing stable node name")
            name = name.strip()
            status = row.get("status")
            identifiers: dict[str, Any] = {"hostname": name}
            if management_ips.get(name):
                identifiers["management_ips"] = list(dict.fromkeys(management_ips[name]))
            devices.append(DeviceObservation(
                remote_id=name,
                name=name,
                category="server",
                identifiers=identifiers,
                details={
                    "platform": "proxmox",
                    "status": status if isinstance(status, str) else None,
                    # An offline node's API may not expose its NIC list. Keep
                    # its last known ports until a complete online scan.
                    "ports_complete": status != "offline",
                    "network_interfaces_complete": status != "offline",
                },
            ))
            if status == "offline":
                continue
            encoded_name = quote(name, safe="")
            network = _unwrap_data(
                self.client.get_json(f"{base}/nodes/{encoded_name}/network", headers, verify),
                "/nodes/{node}/network",
            )
            seen_macs: set[str] = set()
            exists_metadata_available = any("exists" in item for item in network)
            for interface in network:
                logical = _logical_interface(interface, name)
                if logical is not None:
                    network_interfaces.append(logical)
                if not _is_physical_nic(
                    interface, exists_metadata_available=exists_metadata_available,
                ):
                    continue
                iface = interface["iface"]
                mac = _mac_address(interface)
                # Proxmox can expose interface aliases as separate ``eth``
                # rows. A repeated hardware address identifies the same NIC.
                if mac is not None and mac in seen_macs:
                    continue
                if mac is not None:
                    seen_macs.add(mac)
                ports.append(PortObservation(
                    device_remote_id=name,
                    remote_id=iface,
                    name=iface,
                    mac=mac,
                    speed_mbps=None,  # Network configuration is not negotiated speed.
                    # Proxmox omits ``active`` for interfaces without a link.
                    link_up=(interface.get("active") is True
                             or type(interface.get("active")) is int
                             and interface.get("active") == 1),
                ))
            node_ports = [port for port in ports if port.device_remote_id == name]
            missing_logical_mac = any(
                _logical_kind(item.get("type")) and not _mac_address(item) for item in network
            )
            if report_interval and (any(port.mac is None for port in node_ports) or missing_logical_mac):
                report_macs = self._node_report_macs(
                    base, name, headers, verify, report_interval,
                )
                filled = {
                    port.remote_id: report_macs.get(port.remote_id)
                    for port in node_ports if port.mac is None
                }
                # Two NICs reporting one address cannot both be identified.
                taken = Counter(
                    [port.mac for port in node_ports if port.mac]
                    + [mac for mac in filled.values() if mac]
                )
                ports = [
                    replace(port, mac=filled[port.remote_id])
                    if (port.device_remote_id == name and port.mac is None
                        and filled.get(port.remote_id)
                        and taken[filled[port.remote_id]] == 1)
                    else port
                    for port in ports
                ]
            # Current bond/member addresses identify their host without changing
            # physical NIC permanent MACs or inventing logical cable endpoints.
            host_names = {item["iface"] for item in network
                          if isinstance(item.get("iface"), str)
                          and (_logical_kind(item.get("type")) or _is_physical_nic(
                              item, exists_metadata_available=exists_metadata_available))}
            report_macs = self._report_macs.get((base, name), (0, {}))[1]
            current_macs = self._report_current_macs.get((base, name), {})
            host_macs = {(iface, mac) for mapping in (report_macs, current_macs)
                         for iface, mac in mapping.items() if iface in host_names}
            host_macs.update((item["iface"], mac) for item in network
                             if item.get("iface") in host_names and (mac := _mac_address(item)))
            devices[-1] = replace(devices[-1], details={**devices[-1].details,
                "host_interfaces": [{"name": iface, "mac": mac} for iface, mac in sorted(host_macs)]})
        snmp_settings = settings.get("snmp")
        if isinstance(snmp_settings, Mapping):
            configured_nodes = snmp_settings.get("nodes", [])
            if (not isinstance(configured_nodes, list)
                    or not all(isinstance(node, str) for node in configured_nodes)):
                raise ValueError("Proxmox SNMP nodes must be a list of node names")
            for node_name in configured_nodes:
                address = next(iter(management_ips.get(node_name, ())), None)
                if address is None:
                    _LOGGER.warning("SNMP skipped for Proxmox node %s: management IP unavailable", node_name)
                    continue
                try:
                    snmp_interfaces = read_physical_interfaces(address, snmp_settings)
                except Exception as exc:
                    # SNMP is supplemental: a timeout or disabled agent must
                    # not make otherwise successful Proxmox discovery fail.
                    # snmp.py's own errors name the protocol problem.
                    _LOGGER.warning(
                        "SNMP supplement failed for Proxmox node %s (%s)",
                        node_name, str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__,
                    )
                    continue
                by_name = {
                    port.remote_id: port for port in ports
                    if port.device_remote_id == node_name
                }
                enriched: dict[str, PortObservation] = {}
                for port_name, port in by_name.items():
                    observed = snmp_interfaces.get(port_name)
                    if observed is None:
                        enriched[port_name] = port
                        continue
                    enriched[port_name] = replace(
                        port,
                        mac=port.mac or observed.get("mac"),
                        link_up=(observed.get("link_up")
                                 if observed.get("link_up") is not None
                                 else port.link_up),
                    )
                ports = [
                    enriched.get(port.remote_id, port)
                    if port.device_remote_id == node_name else port
                    for port in ports
                ]
        return DiscoveryBatch(
            devices=tuple(devices), ports=tuple(ports),
            network_interfaces=tuple(network_interfaces),
        )
