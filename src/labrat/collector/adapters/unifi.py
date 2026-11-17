# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Discover adopted physical devices through the official local UniFi API.

The Integration API supplies sites and adopted devices; optional classic/v2
GET requests supply link, WAN, and VPN telemetry. The cross-source reconciler can look up a
known physical NIC MAC in the classic client API and record an inferred link.
Connected clients are collected independently by ``collector.clients`` and
remain observations until an explicit inventory action.

Documentation for the version installed at a site lives in UniFi Network's
Integrations section. API shapes evolve; unrecognized fields are ignored rather
than invented as evidence.

Used by: collector.__main__, collector.client_links, collector.nut_wired,
collector.runner, collector.wireless_clients.
"""

from collections.abc import Mapping
from dataclasses import replace
from functools import lru_cache
from importlib.resources import files
import json
import logging
from typing import Any
from urllib.parse import quote

import requests

from labrat.collector.contracts import (
    DeviceObservation,
    DiscoveryBatch,
    LinkObservation,
    PortObservation,
    WanObservation,
)
from labrat.collector.adapters import unifi_classic
from labrat.collector.http import JsonGetClient, required_string, tls_verification, unifi_network_root
from labrat.collector.vpn import vpn_snapshot

_LOGGER = logging.getLogger(__name__)


_DEVICE_TYPES = {
    "SWITCH": "switch",
    "GATEWAY": "router",
    "ROUTER": "router",
    "FIREWALL": "firewall",
    "ACCESS_POINT": "access_point",
    "AP": "access_point",
}


def _category(device: Mapping[str, Any]) -> str | None:
    """Classify an adopted infrastructure device from type, model and features.

    Args:
        device: Overview and detail fields from the local UniFi API.

    Returns:
        str | None: Recognized infrastructure category, or None. The caller
            excludes a device whose explicit ``type`` is not infrastructure
            and lists one with no type as other equipment.

    Note:
        The official adopted-device API often has no ``type`` field; it may
        supply a feature map such as ``{"switching": {...}}``. A gateway
        can also support switching, so known model prefixes take precedence.
    """
    kind = device.get("type")
    if isinstance(kind, str) and kind.upper() in _DEVICE_TYPES:
        return _DEVICE_TYPES[kind.upper()]
    model = device.get("model")
    if isinstance(model, str):
        normalized_model = model.upper().replace("-", "").replace(" ", "")
        if normalized_model.startswith(("UDM", "UCG", "UXG", "USG", "UDR")):
            return "router"
        if normalized_model.startswith(("UAP", "U6", "U7", "UHDIW")):
            return "access_point"
        if normalized_model.startswith(("USW", "US8", "US16", "US24", "US48")):
            return "switch"
    features = device.get("features")
    if isinstance(features, dict):
        if features.get("gateway") is not None or features.get("routing") is not None:
            return "router"
        if features.get("accessPoint") is not None:
            return "access_point"
        if features.get("switching") is not None:
            return "switch"
    if isinstance(features, list):
        normalized = {v.lower() for v in features if isinstance(v, str)}
        if "routing" in normalized or "gateway" in normalized:
            return "router"
        if "accesspoint" in normalized or "wireless" in normalized:
            return "access_point"
        if "switching" in normalized:
            return "switch"
    return None


def _port_index(port: Mapping[str, Any]) -> str | None:
    """Identify a documented stable physical port index.

    Args:
        port: One object in ``interfaces.ports``.

    Returns:
        str | None: Index as a string, or None if not provided.
    """
    for field in ("portIdx", "idx"):
        value = port.get(field)
        if type(value) is int and value >= 0:
            return str(value)
    return None


def _speed_mbps(port: Mapping[str, Any]) -> int | None:
    """Extract an explicit, numeric link speed without assuming capacity.

    Args:
        port: Physical port API object.

    Returns:
        int | None: Reported speed in Mbps, or None when not provided.
    """
    for field in ("speedMbps", "linkSpeedMbps"):
        value = port.get(field)
        if type(value) is int and value > 0:
            return value
    return None


def _max_speed_mbps(port: Mapping[str, Any]) -> int | None:
    """Read maximum physical port speed separately from negotiated speed.

    Args:
        port: Physical port API object.

    Returns:
        int | None: Maximum speed in Mbps, or None when not provided.
    """
    value = port.get("maxSpeedMbps")
    return value if type(value) is int and value > 0 else None


@lru_cache(maxsize=1)
def _unifi_port_layouts() -> dict[str, list[dict[str, Any]]]:
    """Load the packaged, offline catalog of known UniFi port layouts.

    Returns:
        dict[str, list[dict[str, Any]]]: Normalized model names mapped to
            connector ranges. This catalog is local data; no model lookup runs
            against the network or an AI service.
    """
    resource = files("labrat.collector").joinpath("profiles/unifi_port_layouts.json")
    catalog = json.loads(resource.read_text(encoding="utf-8"))
    profiles: dict[str, list[dict[str, Any]]] = {}
    for profile in catalog.get("profiles", []):
        for model_name in profile.get("models", []):
            normalized = " ".join(str(model_name).lower().replace("-", " ").split())
            profiles[normalized] = profile.get("ports", [])
    return profiles


def _connector_type(
    port: Mapping[str, Any], model: str | None, classic_port: Mapping[str, Any] | None = None,
) -> str | None:
    """Resolve a connector from API metadata, classic media, or the catalog.

    The Integration API's own fields come first, then the port name, then
    the packaged model catalog (which knows combo ports), and last the
    classic port's ``media``, which the controller reports for every model
    but which names only a combo port's active side.

    Args:
        port: Physical port API object.
        model: UniFi model name, if provided by the controller.
        classic_port: The same port's classic ``port_table`` entry, if read.

    Returns:
        str | None: ``rj45``, ``sfp``, ``sfp_plus``, ``sfp28``, or ``combo``.
    """
    for field in ("connectorType", "mediaType", "portType", "type"):
        value = port.get(field)
        if isinstance(value, str):
            normalized = value.strip().lower().replace("+", "_plus")
            if "combo" in normalized:
                return "combo"
            if "sfp28" in normalized:
                return "sfp28"
            if "sfp_plus" in normalized or normalized == "sfpplus":
                return "sfp_plus"
            if normalized == "sfp":
                return "sfp"
            if "rj45" in normalized or "copper" in normalized:
                return "rj45"

    name = port.get("name")
    if isinstance(name, str):
        normalized_name = name.strip().lower()
        if "sfp+" in normalized_name or "sfp plus" in normalized_name:
            return "sfp_plus"
        if "sfp28" in normalized_name:
            return "sfp28"
        if "sfp" in normalized_name:
            return "sfp"
        if "rj45" in normalized_name:
            return "rj45"

    index = _port_index(port)
    if model is not None and index is not None and index.isdigit():
        normalized_model = " ".join(model.lower().replace("-", " ").split())
        for port_range in _unifi_port_layouts().get(normalized_model, []):
            if port_range["from"] <= int(index) <= port_range["through"]:
                return port_range["connector"]
    return unifi_classic.connector_for_port(classic_port or {})


def _link_up(port: Mapping[str, Any]) -> bool | None:
    """Extract explicit current link state without guessing from port speed.

    Args:
        port: Physical port API object.

    Returns:
        bool | None: Current link state, or None when the controller omits it.
    """
    state = port.get("state")
    if isinstance(state, str):
        normalized = state.strip().upper()
        if normalized in {"UP", "CONNECTED", "LINK_UP"}:
            return True
        if normalized in {"DOWN", "DISCONNECTED", "NOT_CONNECTED", "LINK_DOWN"}:
            return False
    return None


def _ports_of(device: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the reported physical port array from a device-detail object.

    Args:
        device: UniFi adopted device detail, possibly without port data.

    Returns:
        list[dict[str, Any]]: Port records, or empty when not advertised.
    """
    interfaces = device.get("interfaces")
    records = interfaces.get("ports") if isinstance(interfaces, dict) else None
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def _neighbor(port: Mapping[str, Any]) -> tuple[str, str] | None:
    """Read a direct LLDP neighbor claim when both port IDs are explicit.

    Args:
        port: One detailed switch physical port.

    Returns:
        tuple[str, str] | None: Device ID and port index, or None if the
            observation cannot identify a concrete, directly attached port.

    Note:
        A learned client MAC or generic uplink label is insufficient. Other
        controller variants can be supported after inspecting real responses.
    """
    if _link_up(port) is False:
        return None
    peer = port.get("lldpNeighbor")
    if not isinstance(peer, dict):
        return None
    device_id = peer.get("deviceId")
    port_idx = peer.get("portIdx")
    if isinstance(device_id, str) and device_id and type(port_idx) is int and port_idx >= 0:
        return device_id, str(port_idx)
    return None


class UniFiAdapter:
    """Poll UniFi Network's supported local API for physical network gear."""

    def __init__(self, client: JsonGetClient | None = None) -> None:
        """Initialize a connection-reusing client.

        Args:
            client: Optional HTTP client for mock tests and connection reuse.
        """
        self.client = client if client is not None else JsonGetClient()
        self._vpn_snapshots: dict[tuple[str, str], dict[str, Any]] = {}

    def _page(
        self,
        base: str,
        path: str,
        headers: Mapping[str, str],
        verify: bool | str,
        *, max_records: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read all pages from one UniFi Integration API collection.

        Args:
            base: Controller's complete local Network Integration v1 base URL.
            path: Site or device collection path relative to that base.
            headers: Required API-key headers.
            verify: Requests TLS verification setting.
            max_records: Optional hard cap with stable-total/count checks for
                complete client snapshots; excess data is refused early.

        Returns:
            list[dict[str, Any]]: All collection records in the response pages.

        Raises:
            ValueError: Malformed paging or an excessive number of pages.
        """
        records: list[dict[str, Any]] = []
        offset = 0
        expected_total = None
        for _ in range(100):
            payload = self.client.get_json(
                f"{base}/{path}?offset={offset}&limit=100", headers, verify,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("Unexpected UniFi paginated response")
            page = payload["data"]
            if not all(isinstance(item, dict) for item in page):
                raise ValueError("Unexpected UniFi collection record")
            records.extend(page)
            total = payload.get("totalCount")
            if type(total) is not int or total < 0:
                raise ValueError("UniFi collection missing totalCount")
            if max_records is not None:
                if total > max_records or len(records) > total:
                    raise ValueError("UniFi collection exceeds its snapshot bound")
                if expected_total is not None and total != expected_total:
                    raise ValueError("UniFi collection changed during pagination")
                if payload.get("offset", offset) != offset or payload.get("count", len(page)) != len(page):
                    raise ValueError("UniFi collection paging is inconsistent")
                expected_total = total
            if len(records) >= total:
                return records
            if not page:
                raise ValueError("UniFi returned an empty incomplete page")
            offset += len(page)
        raise ValueError("UniFi pagination limit exceeded")

    def _classic_devices(
        self, settings: Mapping[str, Any], site: Mapping[str, Any],
        headers: Mapping[str, str], verify: bool | str,
    ) -> list[dict[str, Any]]:
        """Read a site's classic device list, or nothing if it is unavailable.

        The classic list adds cable media, uplinks, and LLDP neighbors. It is
        optional: if a controller rejects it, collection continues with the
        Integration API data alone and the failure is logged without detail.

        Args:
            settings: The source's private settings.
            site: Integration API site record; ``internalReference`` is the
                classic short site name (for example ``default``).
            headers: API-key headers.
            verify: Requests TLS verification setting.

        Returns:
            list[dict[str, Any]]: Device records, possibly empty.
        """
        reference = site.get("internalReference")
        if not isinstance(reference, str) or not reference:
            return []
        url = (unifi_network_root(settings) + "/api/s/"
               + quote(reference, safe="") + "/stat/device")
        try:
            payload = self.client.get_json(url, headers, verify)
        except (requests.RequestException, ValueError):
            _LOGGER.warning("UniFi classic device list unavailable; skipping media and neighbors")
            return []
        data = payload.get("data") if isinstance(payload, dict) else None
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def _add_classic_links(
        self, site_id: str, classic: list[dict[str, Any]], mac_to_remote: dict[str, str],
        known_ports: set[tuple[str, str]], port_medium: dict[tuple[str, str], str],
        unique_links: set, devices: list, ports: list, links: list,
        vendor_rules: tuple[unifi_classic.VendorRule, ...] | None = None,
    ) -> None:
        """Add controller-reported uplinks and identifiable LLDP neighbors.

        Uplinks become confirmed links between adopted devices (evidence
        ``unifi-uplink``). The controller keeps a disconnected device's last
        uplink, so an uplink from a device it reports ``OFFLINE`` that ends
        on a port reported down is history, not a cable: it is skipped, and
        the snapshot then retires the link instead of raising Link down on
        the upstream switch. A port the uplink record adds is up unless its
        device reports a state other than ``ONLINE``. Non-UniFi LLDP neighbors
        the vendor table identifies become observed devices with one port per announced port ID
        and confirmed ``lldp`` links to the UniFi ports that hear them. Their
        ports take the cable medium of the UniFi port at the other end.

        Args:
            site_id: Integration API site ID, used in remote IDs.
            classic: Classic device records for the site.
            mac_to_remote: Adopted device MAC to batch remote ID.
            known_ports: Batch (device remote ID, port ID) pairs.
            port_medium: Cable medium already found per UniFi port.
            unique_links: Endpoint pairs already linked in this batch.
            devices: Batch device list to extend.
            ports: Batch port list to extend.
            links: Batch link list to extend.
            vendor_rules: LLDP vendor rules; the packaged table by default.
        """
        def add_link(a: tuple[str, str], b: tuple[str, str], evidence: str) -> None:
            endpoints = tuple(sorted((a, b)))
            if endpoints[0] == endpoints[1] or endpoints in unique_links:
                return
            unique_links.add(endpoints)
            links.append(LinkObservation(
                a_device_remote_id=endpoints[0][0], a_port_remote_id=endpoints[0][1],
                b_device_remote_id=endpoints[1][0], b_port_remote_id=endpoints[1][1],
                evidence=evidence, confidence="confirmed",
            ))

        device_state = {
            device.remote_id: str(device.details.get("state") or "").upper() for device in devices
        }
        port_up = {(item.device_remote_id, item.remote_id): item.link_up for item in ports}
        for uplink in unifi_classic.uplinks(classic):
            a = (mac_to_remote.get(uplink.device_mac), uplink.local_port)
            b = (mac_to_remote.get(uplink.upstream_mac), uplink.upstream_port)
            if a[0] is None or b not in known_ports:
                continue
            # A device without a reported state counts as up, as before.
            online = device_state.get(a[0], "") in ("", "ONLINE")
            if device_state.get(a[0]) == "OFFLINE" and port_up.get(b) is False:
                continue
            if a not in known_ports:
                # Access points often list no ports in the Integration API.
                # The controller's uplink record names the port, so add it.
                ports.append(PortObservation(
                    device_remote_id=a[0], remote_id=a[1], name=f"Port {a[1]}",
                    speed_mbps=uplink.speed_mbps if online else None,
                    link_up=True if online else None,
                    medium=port_medium.get(b),
                ))
                known_ports.add(a)
            add_link(a, b, "unifi-uplink")

        neighbors, skipped = unifi_classic.lldp_neighbors(classic, set(mac_to_remote), vendor_rules)
        if skipped:
            _LOGGER.info("Found %s LLDP neighbor(s) without a recognized vendor", skipped)
        neighbors_by_device: dict[str, list[dict[str, Any]]] = {}
        for neighbor in neighbors:
            owner = mac_to_remote.get(neighbor.device_mac)
            if owner is None:
                continue
            neighbors_by_device.setdefault(owner, []).append({
                "local_port": neighbor.local_port,
                "chassis_id": neighbor.chassis_id,
                "port_id": neighbor.port_id,
                "management_ips": list(neighbor.management_ips),
            })
        if neighbors_by_device:
            for index, device in enumerate(devices):
                records = neighbors_by_device.get(device.remote_id)
                if not records:
                    continue
                details = dict(device.details)
                details["lldp_neighbors"] = records
                devices[index] = DeviceObservation(
                    remote_id=device.remote_id, name=device.name,
                    category=device.category, identifiers=device.identifiers,
                    details=details,
                )
        neighbor_ports: dict[str, dict[str, str | None]] = {}
        vendors: dict[str, str] = {}
        for neighbor in neighbors:
            if neighbor.vendor is None:
                continue
            switch_port = (mac_to_remote.get(neighbor.device_mac), neighbor.local_port)
            if switch_port not in known_ports:
                continue
            remote_id = f"{site_id}:lldp:{neighbor.chassis_id}"
            vendors[remote_id] = neighbor.vendor
            # The same cable has the same medium at both ends.
            neighbor_ports.setdefault(remote_id, {}).setdefault(
                neighbor.port_id, port_medium.get(switch_port),
            )
            add_link(switch_port, (remote_id, neighbor.port_id), "lldp")
        for remote_id, announced in neighbor_ports.items():
            chassis = remote_id.rsplit(":lldp:", 1)[1]
            bridged = any(port_id.startswith("bridge/") for port_id in announced)
            devices.append(DeviceObservation(
                remote_id=remote_id,
                name=f"{vendors[remote_id]} {chassis[-8:]}",
                category="switch" if bridged else "other_physical",
                identifiers={"mac": chassis},
                details={
                    "platform": "lldp", "vendor": vendors[remote_id],
                    "heard_by": "unifi", "site_id": site_id, "ports_complete": True,
                },
            ))
            for port_id, medium in announced.items():
                ports.append(PortObservation(
                    device_remote_id=remote_id, remote_id=port_id,
                    name=port_id.split("/", 1)[-1], medium=medium,
                ))

    def poll(self, settings: Mapping[str, Any]) -> DiscoveryBatch:
        """Collect adopted routers, switches, APs, and their physical ports.

        Args:
            settings: ``url``, ``api_key``, optional TLS settings, and optional
                ``lldp_vendors`` entries from the server-only configuration
                file.

        Returns:
            DiscoveryBatch: Source-scoped physical network infrastructure and
                explicit LLDP neighbor claims between adopted device ports.

        Raises:
            ValueError: Invalid configuration or unrecognized API structure.
            requests.RequestException: The controller is unavailable.
        """
        base = unifi_network_root(settings) + "/integration/v1"
        api_key = required_string(settings, "api_key")
        verify = tls_verification(settings)
        rules = unifi_classic.vendor_rules(settings.get("lldp_vendors"))
        headers = {"Accept": "application/json", "X-API-Key": api_key}
        devices: list[DeviceObservation] = []
        ports: list[PortObservation] = []
        links: list[LinkObservation] = []
        wan: list[WanObservation] = []
        for site in self._page(base, "sites", headers, verify):
            site_id = site.get("id")
            if not isinstance(site_id, str) or not site_id:
                raise ValueError("UniFi site lacks stable identifier")
            encoded_site = quote(site_id, safe="")
            wan_upstreams: list[tuple[str, str, str, str, Mapping[str, Any]]] = []
            site_devices = self._page(base, f"sites/{encoded_site}/devices", headers, verify)
            classic = self._classic_devices(settings, site, headers, verify)
            reference = site.get("internalReference")
            health = (self._classic_health(settings, reference, headers, verify)
                      if classic and isinstance(reference, str) and reference else [])
            classic_by_mac = {
                str(record.get("mac")).lower(): record for record in classic if record.get("mac")
            }
            mac_to_remote: dict[str, str] = {}
            port_medium: dict[tuple[str, str], str] = {}
            unknown_modules: dict[tuple[str, str], Mapping[str, Any]] = {}
            known_devices: set[str] = set()
            details_by_id: dict[str, dict[str, Any]] = {}
            for overview in site_devices:
                device_id = overview.get("id")
                if not isinstance(device_id, str) or not device_id:
                    raise ValueError("UniFi adopted device lacks stable identifier")
                encoded_device = quote(device_id, safe="")
                detail = self.client.get_json(
                    f"{base}/sites/{encoded_site}/devices/{encoded_device}", headers, verify,
                )
                if not isinstance(detail, dict):
                    raise ValueError("Unexpected UniFi adopted device details")
                merged = {**overview, **detail}
                category = _category(merged)
                overview_mac = merged.get("macAddress") or merged.get("mac")
                classic_record = classic_by_mac.get(overview_mac.lower(), {}) if isinstance(overview_mac, str) else {}
                # A device with WAN interfaces is a router, even when it also
                # has switch ports (a Dream Machine reports "switching" too).
                if classic_record and unifi_classic.has_wan_interfaces(classic_record):
                    category = "router"
                if category is None and merged.get("type"):
                    continue  # An explicit type that is not infrastructure.
                if category is None:
                    # An adopted device without a type is network equipment
                    # even when its model is new to LabRat: keep it.
                    category = "other_physical"
                    _LOGGER.info("UniFi device model %s has no recognized type; listed as other equipment",
                                 str(merged.get("model") or "unknown")[:60])
                remote_id = f"{site_id}:{device_id}"
                known_devices.add(device_id)
                details_by_id[device_id] = merged
                name = merged.get("name") or merged.get("model") or device_id
                mac = merged.get("macAddress") or merged.get("mac")
                identifiers: dict[str, str] = {}
                if isinstance(mac, str) and mac.strip():
                    identifiers["mac"] = mac.lower()
                    mac_to_remote[mac.lower()] = remote_id
                classic_ports = {
                    str(item.get("port_idx")): item
                    for item in classic_by_mac.get(identifiers.get("mac", ""), {}).get("port_table") or []
                    if isinstance(item, Mapping)
                }
                model = merged.get("model")
                if category == "router" and classic_record:
                    snapshot = unifi_classic.wan_snapshot(classic_record, health)
                    if snapshot is not None and identifiers.get("mac"):
                        primary_mac = next((item.get("gw_mac") for item in health if item.get("subsystem") == "wan"), None)
                        if (isinstance(reference, str) and reference and isinstance(primary_mac, str)
                                and primary_mac.lower() == identifiers["mac"].lower()):
                            snapshot["vpn"] = self._vpn_snapshot(settings, reference, headers, verify)
                        wan.append(WanObservation(gateway_mac=identifiers["mac"], snapshot=snapshot))
                        for uplink in snapshot.get("uplinks", []):
                            if (isinstance(uplink, Mapping) and uplink.get("enabled") is True
                                    and isinstance(uplink.get("id"), str)
                                    and type(uplink.get("port")) is int):
                                upstream_id = f"{remote_id}:wan:{uplink['id']}"
                                isp = uplink.get("isp")
                                uplink_name = str(uplink.get("name") or uplink["id"].upper())
                                upstream_name = (
                                    f"{isp.strip()} {uplink_name} upstream"
                                    if isinstance(isp, str) and isp.strip()
                                    else f"{uplink_name} upstream"
                                )
                                wan_upstreams.append((
                                    remote_id, str(uplink["port"]), upstream_id,
                                    upstream_name, uplink,
                                ))
                devices.append(DeviceObservation(
                    remote_id=remote_id,
                    name=str(name),
                    category=category,
                    identifiers=identifiers,
                    details={
                        "platform": "unifi", "site_id": site_id,
                        "model": model if isinstance(model, str) else None,
                        "state": merged.get("state") if isinstance(merged.get("state"), str) else None,
                        "ports_complete": True,
                    },
                ))
                for port in _ports_of(merged):
                    idx = _port_index(port)
                    if idx is None:
                        continue
                    port_name = port.get("name")
                    medium = unifi_classic.medium_for_port(classic_ports.get(idx, {}))
                    if medium is not None:
                        port_medium[(remote_id, idx)] = medium
                    elif classic_ports.get(idx, {}).get("sfp_found"):
                        unknown_modules[(remote_id, idx)] = classic_ports[idx]
                    ports.append(PortObservation(
                        device_remote_id=remote_id,
                        remote_id=idx,
                        name=port_name if isinstance(port_name, str) and port_name else f"Port {idx}",
                        mac=port.get("macAddress") if isinstance(port.get("macAddress"), str) else None,
                        speed_mbps=_speed_mbps(port),
                        link_up=_link_up(port),
                        max_speed_mbps=_max_speed_mbps(port),
                        connector_type=_connector_type(
                            port, model if isinstance(model, str) else None, classic_ports.get(idx),
                        ),
                        medium=medium,
                    ))
            # Two unidentified modules with one serial are the ends of one
            # cable assembly (unifi_classic.cable_assembly_media).
            assembled = unifi_classic.cable_assembly_media(unknown_modules)
            port_medium.update(assembled)
            ports = [replace(item, medium=assembled[(item.device_remote_id, item.remote_id)])
                     if (item.device_remote_id, item.remote_id) in assembled else item for item in ports]
            known_ports = {(item.device_remote_id, item.remote_id) for item in ports}
            unique_links: set[tuple[tuple[str, str], tuple[str, str]]] = set()
            for gateway_remote_id, wan_port_id, upstream_id, upstream_name, uplink in wan_upstreams:
                gateway_port = (gateway_remote_id, wan_port_id)
                if gateway_port not in known_ports:
                    ports.append(PortObservation(
                        device_remote_id=gateway_remote_id, remote_id=wan_port_id,
                        name=str(uplink.get("name") or f"WAN {wan_port_id}"),
                        link_up=uplink.get("up") is True,
                        medium=port_medium.get(gateway_port),
                    ))
                    known_ports.add(gateway_port)
                devices.append(DeviceObservation(
                    remote_id=upstream_id, name=upstream_name, category="modem",
                    details={"platform": "unifi-wan-inference", "site_id": site_id,
                             "ports_complete": True, "wan_uplink_id": uplink.get("id")},
                ))
                ports.append(PortObservation(
                    device_remote_id=upstream_id, remote_id="wan", name="WAN",
                    link_up=uplink.get("up") is True,
                    medium=port_medium.get(gateway_port),
                ))
                links.append(LinkObservation(
                    a_device_remote_id=gateway_remote_id, a_port_remote_id=wan_port_id,
                    b_device_remote_id=upstream_id, b_port_remote_id="wan",
                    evidence="unifi-wan-upstream", confidence="inferred",
                ))
            for device_id, detail in details_by_id.items():
                remote_id = f"{site_id}:{device_id}"
                for port in _ports_of(detail):
                    idx = _port_index(port)
                    peer = _neighbor(port)
                    if idx is None or peer is None or peer[0] not in known_devices:
                        continue
                    other_id = f"{site_id}:{peer[0]}"
                    endpoints = tuple(sorted(((remote_id, idx), (other_id, peer[1]))))
                    if endpoints[0] == endpoints[1] or not all(x in known_ports for x in endpoints):
                        continue
                    if endpoints in unique_links:
                        continue
                    unique_links.add(endpoints)
                    links.append(LinkObservation(
                        a_device_remote_id=endpoints[0][0],
                        a_port_remote_id=endpoints[0][1],
                        b_device_remote_id=endpoints[1][0],
                        b_port_remote_id=endpoints[1][1],
                        evidence="lldp", confidence="confirmed",
                    ))
            self._add_classic_links(
                site_id, classic, mac_to_remote, known_ports, port_medium,
                unique_links, devices, ports, links, rules,
            )
        return DiscoveryBatch(
            devices=tuple(devices), ports=tuple(ports), links=tuple(links), wan=tuple(wan),
        )

    def _classic_health(
        self, settings: Mapping[str, Any], reference: str,
        headers: Mapping[str, str], verify: bool | str,
    ) -> list[dict[str, Any]]:
        """Read a site's classic health subsystems, or nothing if unavailable.

        Args:
            settings: The source's private settings.
            reference: Classic short site name (``internalReference``).
            headers: API-key headers.
            verify: Requests TLS verification setting.

        Returns:
            list[dict[str, Any]]: Subsystem records such as ``wan`` and ``www``.
        """
        url = (unifi_network_root(settings) + "/api/s/"
               + quote(reference, safe="") + "/stat/health")
        try:
            payload = self.client.get_json(url, headers, verify)
        except (requests.RequestException, ValueError):
            _LOGGER.warning("UniFi health unavailable; skipping WAN status")
            return []
        data = payload.get("data") if isinstance(payload, Mapping) else None
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def poll_wan(self, settings: Mapping[str, Any]) -> tuple[WanObservation, ...]:
        """Read only gateway uplink state, for frequent polling between scans.

        Reads the site list, health subsystems (which name the gateway), and
        that gateway's classic record. Optional VPN reads supplement connection
        state, safe profile names, and a remote-access session count.

        Args:
            settings: ``url``, ``api_key`` and optional TLS settings.

        Returns:
            tuple[WanObservation, ...]: One reading per gateway found.

        Raises:
            ValueError: Invalid configuration.
            requests.RequestException: The controller cannot be reached.
        """
        base = unifi_network_root(settings)
        headers = {"Accept": "application/json", "X-API-Key": required_string(settings, "api_key")}
        verify = tls_verification(settings)
        readings: list[WanObservation] = []
        for site in self._page(base + "/integration/v1", "sites", headers, verify):
            reference = site.get("internalReference")
            if not isinstance(reference, str) or not reference:
                continue
            health = self._classic_health(settings, reference, headers, verify)
            wan = next((item for item in health if item.get("subsystem") == "wan"), {})
            gateway_mac = wan.get("gw_mac")
            if not isinstance(gateway_mac, str) or not gateway_mac:
                continue
            payload = self.client.get_json(
                base + "/api/s/" + quote(reference, safe="")
                + "/stat/device/" + quote(gateway_mac.lower(), safe=":"), headers, verify,
            )
            records = payload.get("data") if isinstance(payload, Mapping) else None
            record = next((item for item in records or [] if isinstance(item, Mapping)), None)
            snapshot = unifi_classic.wan_snapshot(record, health) if record else None
            if snapshot is not None:
                snapshot["vpn"] = self._vpn_snapshot(settings, reference, headers, verify)
                readings.append(WanObservation(gateway_mac=gateway_mac.lower(), snapshot=snapshot))
        return tuple(readings)

    def _vpn_snapshot(
        self, settings: Mapping[str, Any], reference: str,
        headers: Mapping[str, str], verify: bool | str,
    ) -> dict[str, Any]:
        """Read optional VPN telemetry, keeping failures independent of WAN state.

        Raw network configuration is discarded after whitelist-only extraction.
        Failure retains only a prior sanitized snapshot, marked unavailable with
        its original sample time; an unsupported API never becomes a false outage.
        Remote-access sessions are counted without storing user/session records.

        Args:
            settings: Private controller settings.
            reference: Classic site reference.
            headers: API-key authentication headers.
            verify: TLS verification setting.

        Returns:
            dict[str, Any]: Sanitized VPN snapshot or explicit unavailability.
        """
        base = unifi_network_root(settings)
        encoded = quote(reference, safe="")
        classic = f"{base}/api/s/{encoded}"
        cache_key = (base, reference)
        try:
            payload = self.client.get_json(
                f"{base}/v2/api/site/{encoded}/vpn/connections", headers, verify,
            )
            connections = payload.get("connections") if isinstance(payload, Mapping) else None
            if not isinstance(connections, list):
                raise ValueError("Unexpected VPN connection envelope")
        except (requests.RequestException, ValueError):
            previous = self._vpn_snapshots.get(cache_key, {"sampled_at": None, "connections": [], "remote_access_sessions": None})
            return {**previous, "available": False}
        networks = []
        sessions = None
        try:
            payload = self.client.get_json(f"{classic}/rest/networkconf", headers, verify)
            if isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
                networks = payload["data"]
        except (requests.RequestException, ValueError):
            pass  # Runtime connection state remains useful without profile names.
        try:
            payload = self.client.get_json(f"{classic}/stat/remoteuservpn", headers, verify)
            if isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
                sessions = len(payload["data"])
        except (requests.RequestException, ValueError):
            pass  # Missing session data is unknown, not zero.
        snapshot = vpn_snapshot(connections, networks, sessions)
        self._vpn_snapshots[cache_key] = snapshot
        return snapshot
