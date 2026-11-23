# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Convert a router source's stored network telemetry into the shared form.

The Networks page and its widget feeds read one source-neutral shape, so
the page is written once for every router source (UniFi now; OPNsense and
RouterOS later). Each source kind keeps storing its own curated telemetry
(``unifi_network`` for UniFi), and the API converts it here when it is
read, so snapshots stored before this module existed answer at once, with
no migration and no new poll (``planning/specs/opnsense-integration.md``,
"Shared network snapshot").

The shared form for one source::

    {"source": {"id", "kind", "name"}, "site", "sampled_at", "available",
     "sections": {part: {"status", "sampled_at"}},
     "capabilities": {"verdicts": bool},
     "networks", "zones", "gateways", "policies", "paths", "sinkholes",
     "wifi", "devices", "clients", "health", "neighbours"}

Every ID is ``<kind>:<source id>:<type>:<the source's own key>``, stable
across polls. Words are normalized: origins are ``user`` or ``system``;
actions, device and port states are lowercase; a client's ``connection``
is ``wired``, ``wireless``, or ``other``. A device has ``roles`` (any of
``gateway``, ``access_point``, ``switch``: a console that routes and
broadcasts Wi-Fi has both) and ``kind``, its first role or ``other``.

A network's ``ipv4`` and ``ipv6`` list its interface addresses with their
prefix (``192.0.2.1/24``); ``cidr`` is the subnet (``192.0.2.0/24``).

A section's ``status`` is ``complete`` (read in full, possibly empty),
``partial`` (read, but capped or missing details), ``failed`` (its read
failed; the list is empty or the previous one), or ``not_available``
(the stored sample has no such section). ``available`` is false when the
source's last refresh failed and this is the previous sample.
"""

from datetime import datetime, timezone
import ipaddress
import re
from typing import Any

from labrat.collector.adapters.opnsense import peer_key

#: Device models that are gateways, whatever their feature list says.
_GATEWAY_MODEL = re.compile(r"dream machine|gateway|udm|usg|uxg", re.IGNORECASE)

#: Per shared section: the snapshot key holding it, then the snapshot error
#: names that fail it (the list read) or leave it partial (any later name:
#: a detail read, or a cap such as ``network_list: limited``).
_UNIFI_SECTIONS = {
    "networks": ("networks", "networks", "network_detail", "network_list"),
    "zones": ("zones", "zones"),
    "policies": ("policies", "policies", "lists"),
    "gateways": ("wans", "wans"),
    "wifi": ("wifi", "wifi"),
    "devices": ("devices", "devices", "device_detail", "device_stats"),
    "clients": ("clients", "clients", "classic_clients"),
    "health": ("health", "health"),
    "neighbours": ("neighbours", "neighbours"),
}


def reference(kind: str, source_id: int, item: str, key: Any) -> str | None:
    """Build a shared ID from a source's own key.

    Args:
        kind: The source kind, such as ``unifi``.
        source_id: The discovery source's ID.
        item: What the key names, such as ``network`` or ``zone``.
        key: The source's own key; None or empty gives no ID.

    Returns:
        str | None: ``<kind>:<source id>:<item>:<key>``, or None.
    """
    if key is None or key == "":
        return None
    return f"{kind}:{source_id}:{item}:{key}"


def _sections(snapshot: dict) -> dict[str, dict]:
    """Each shared section's status from the UniFi snapshot's named errors.

    Args:
        snapshot: The stored ``unifi_network`` value.

    Returns:
        dict[str, dict]: ``status`` and ``sampled_at`` per section.
    """
    errors = snapshot.get("errors") if isinstance(snapshot.get("errors"), dict) else {}
    found = {}
    for section, (key, *names) in _UNIFI_SECTIONS.items():
        if not isinstance(snapshot.get(key), list):
            found[section] = {"status": "not_available", "sampled_at": None}
            continue
        status = "complete"
        if names[0] in errors:
            status = "failed"
        elif any(name in errors for name in names[1:]):
            status = "partial"
        found[section] = {"status": status, "sampled_at": snapshot.get("sampled_at")}
    return found


def _origin(value: Any) -> str:
    """Normalize a UniFi origin.

    Args:
        value: ``USER_DEFINED``, ``SYSTEM_DEFINED``, or None.

    Returns:
        str: ``user`` or ``system``.
    """
    return "user" if value == "USER_DEFINED" else "system"


def _lower(value: Any) -> str | None:
    """Lowercase a reported word, keeping None.

    Args:
        value: A word such as ``ONLINE``, or None.

    Returns:
        str | None: The word in lowercase, or None.
    """
    return value.lower() if isinstance(value, str) else None


def _device_roles(device: dict) -> list[str]:
    """A UniFi device's roles from its model and features, primary first.

    Args:
        device: A curated UniFi device.

    Returns:
        list[str]: Any of ``gateway`` (by model), ``access_point``, and
            ``switch`` (by feature), in that order.
    """
    features = device.get("features") or []
    roles = []
    if _GATEWAY_MODEL.search(device.get("model") or ""):
        roles.append("gateway")
    if "accessPoint" in features:
        roles.append("access_point")
    if "switching" in features:
        roles.append("switch")
    return roles


def _interface(network: dict) -> str | None:
    """A network's gateway interface address with its prefix.

    Args:
        network: A curated network with ``gateway`` and ``prefix``.

    Returns:
        str | None: Such as ``192.0.2.1/24``, or None when either is missing
            or invalid.
    """
    try:
        return str(ipaddress.ip_interface(f"{network['gateway']}/{network['prefix']}"))
    except (KeyError, TypeError, ValueError):
        return None


def _endpoint(ref: Any, endpoint: dict) -> dict:
    """Convert a policy's source or destination summary.

    Args:
        ref: Builds a shared ID from an item name and key.
        endpoint: The curated summary.

    Returns:
        dict: ``zone`` (shared ID), ``kind``, ``values``, ``ports``, and ``negate``.
    """
    return {"zone": ref("zone", endpoint.get("zone")), "kind": endpoint.get("kind"),
            "values": endpoint.get("values") or [], "ports": endpoint.get("ports") or [],
            "negate": bool(endpoint.get("negate"))}


def from_unifi(source_id: int, name: str, snapshot: dict, paths: list[dict], sinkholes: list[dict]) -> dict:
    """Convert a stored UniFi network snapshot into the shared form.

    Args:
        source_id: The UniFi discovery source's ID.
        name: Its display name.
        snapshot: The stored ``unifi_network`` value, with ``cidr`` on its
            networks and ``guest`` on its clients already added.
        paths: Its zone paths (``zone_paths``), keyed by UniFi zone IDs.
        sinkholes: Its DNS sinkholes.

    Returns:
        dict: The shared form described in the module docstring.
    """
    def ref(item: str, key: Any) -> str | None:
        return reference("unifi", source_id, item, key)

    site = snapshot.get("site") if isinstance(snapshot.get("site"), dict) else {}
    interfaces = [(item, _interface(item)) for item in snapshot.get("networks") or []]
    networks = [{
        "id": ref("network", item.get("id")), "name": item.get("name"), "vlan": item.get("vlan"),
        "zone": ref("zone", item.get("zone_id")), "cidr": item.get("cidr"),
        "ipv4": [address] if address and ":" not in address else [],
        "ipv6": [address] if address and ":" in address else [],
        "gateway": item.get("gateway"), "management": _lower(item.get("management")), "dhcp": item.get("dhcp"),
        "enabled": item.get("enabled") is not False, "default": item.get("default") is True,
        "isolated": item.get("isolated") is True, "internet": item.get("internet") is not False,
        "mdns": item.get("mdns") is True,
    } for item, address in interfaces]
    gateways = [{"id": ref("network", item.get("id")), "name": item.get("name")} for item in snapshot.get("wans") or []]
    zones = [{
        "id": ref("zone", item.get("id")), "name": item.get("name"), "origin": _origin(item.get("origin")),
        "networks": [ref("network", key) for key in item.get("network_ids") or []],
    } for item in snapshot.get("zones") or []]
    policies = [{
        "id": ref("policy", item.get("id")), "name": item.get("name"), "order": item.get("index"),
        "action": _lower(item.get("action")), "enabled": item.get("enabled") is not False,
        "origin": _origin(item.get("origin")),
        "source": _endpoint(ref, item.get("source") or {}), "destination": _endpoint(ref, item.get("destination") or {}),
        "protocol": item.get("protocol"), "ip_version": item.get("ip_version"), "states": item.get("states"),
        "logging": item.get("logging") is True,
    } for item in snapshot.get("policies") or []]
    shared_paths = [{
        **path, "source": ref("zone", path.get("source")), "destination": ref("zone", path.get("destination")),
        "exceptions": [{**item, "origin": _origin(item.get("origin"))} for item in path.get("exceptions") or []],
    } for path in paths]
    devices = [{
        "id": ref("device", item.get("id")), "name": item.get("name"), "model": item.get("model"),
        "roles": roles, "kind": (roles or ["other"])[0],
        "state": _lower(item.get("state")), "ip": item.get("ip"), "mac": item.get("mac"),
        "firmware": item.get("firmware"), "updatable": item.get("updatable") is True,
        "uplink": ref("device", item.get("uplink_device")),
        "ports": [{**port, "state": _lower(port.get("state")), "poe": _lower(port.get("poe"))}
                  for port in item.get("ports") or []],
        "radios": item.get("radios") or [], "uptime": item.get("uptime"), "cpu": item.get("cpu"),
        "memory": item.get("memory"), "tx_bps": item.get("tx_bps"), "rx_bps": item.get("rx_bps"),
    } for item, roles in ((item, _device_roles(item)) for item in snapshot.get("devices") or [])]
    connection = {"WIRED": "wired", "WIRELESS": "wireless"}
    clients = [{
        "name": item.get("name"), "hostname": item.get("hostname"), "mac": item.get("mac"), "ip": item.get("ip"),
        "network": item.get("network"), "connection": connection.get(item.get("type"), "other"),
        "uplink": ref("device", item.get("uplink_device")), "essid": item.get("essid"), "radio": item.get("radio"),
        "channel": item.get("channel"), "signal": item.get("signal"), "satisfaction": item.get("satisfaction"),
        "wired_rate": item.get("wired_rate"), "tx_bytes": item.get("tx_bytes"), "rx_bytes": item.get("rx_bytes"),
        "uptime": item.get("uptime"), "guest": item.get("guest"),
    } for item in snapshot.get("clients") or []]
    return {
        "source": {"id": source_id, "kind": "unifi", "name": name},
        "site": site.get("name"), "sampled_at": snapshot.get("sampled_at"),
        "available": snapshot.get("available") is not False,
        "sections": _sections(snapshot), "capabilities": {"verdicts": True},
        "networks": networks, "zones": zones, "gateways": gateways, "policies": policies, "paths": shared_paths,
        "sinkholes": sinkholes, "wifi": snapshot.get("wifi") or [], "devices": devices, "clients": clients,
        "health": snapshot.get("health") or [], "neighbours": snapshot.get("neighbours") or [],
    }


#: Shared sections OPNsense has no source for: a router has no Wi-Fi,
#: controller health, or neighbouring networks.
_OPNSENSE_ABSENT = ("wifi", "health", "neighbours")

#: Worst first: the status a shared section takes from several reads.
_STATUS_ORDER = ("failed", "partial", "complete", "not_available")


def _combined(sections: dict, names: tuple[str, ...]) -> dict:
    """One shared section's status from the reads it is built from.

    Args:
        sections: Stored section outcomes by read name.
        names: The reads that build it.

    Returns:
        dict: ``status`` (the worst of theirs; ``not_available`` only when
            none was read) and ``sampled_at`` (the oldest of theirs).
    """
    outcomes = [sections.get(name) or {"status": "not_available", "sampled_at": None} for name in names]
    read = [item for item in outcomes if item.get("status") != "not_available"]
    if not read:
        return {"status": "not_available", "sampled_at": None}
    status = min((item.get("status") for item in read), key=lambda value: _STATUS_ORDER.index(value)
                 if value in _STATUS_ORDER else 0)
    times = [item["sampled_at"] for item in read if item.get("sampled_at")]
    return {"status": status, "sampled_at": min(times) if times else None}


def _cidr(address: str | None) -> str | None:
    """The subnet of an interface address.

    Args:
        address: Such as ``192.0.2.1/24``.

    Returns:
        str | None: Such as ``192.0.2.0/24``, or None when invalid.
    """
    try:
        return str(ipaddress.ip_interface(address).network) if address else None
    except ValueError:
        return None


def _kea_serving(identifier: str | None, network: dict, clients: list[dict]) -> bool:
    """Whether a current Kea lease shows Kea serving an interface.

    Only a lease from the latest Kea lease read counts, and only when that
    read was complete and the lease has a known expiry that is still ahead
    both of that read and of now (the collector may have stopped since): a
    lease kept as history (a sighting no longer reported, or read before a
    failed refresh) proves nothing about now.

    Args:
        identifier: The interface's identifier (``opt2``).
        network: The stored ``opnsense_network`` telemetry.
        clients: The firewall's sightings.

    Returns:
        bool: True when such a lease exists.
    """
    section = (network.get("sections") or {}).get("kea_leases") or {}
    if section.get("status") != "complete" or not section.get("sampled_at"):
        return False
    try:
        read_at = datetime.fromisoformat(section["sampled_at"]).timestamp()
    except (TypeError, ValueError):
        return False
    for client in clients:
        lease = client.get("lease") or {}
        expires = lease.get("expires")
        if (client.get("scope") == identifier and lease.get("server") == "kea"
                and client.get("lease_sampled_at") == section["sampled_at"]
                and isinstance(expires, (int, float)) and expires > max(read_at, now())):
            return True
    return False


def now() -> float:
    """The current time, as a Unix timestamp (a seam for tests).

    Returns:
        float: Seconds since the epoch.
    """
    return datetime.now(timezone.utc).timestamp()


def opnsense_dhcp(interface: dict, network: dict, clients: list[dict], available: bool = True) -> dict | None:
    """The DHCP service an OPNsense interface's clients get, from dnsmasq or Kea.

    dnsmasq counts only while it is switched on (a saved range of a
    switched-off dnsmasq serves nothing). Kea's on/off switch is not read
    (its settings privilege allows changes), so a Kea subnet is
    ``serving`` only when a Kea lease on that interface shows it;
    otherwise it is just configured (see :func:`_kea_serving`).

    Args:
        interface: The stored interface (``identifier``, ``ipv4``).
        network: The stored ``opnsense_network`` telemetry.
        clients: The firewall's sightings (client details with ``lease``).
        available: False when the firewall's latest poll failed: no stored
            lease shows what Kea serves now.

    Returns:
        dict | None: ``mode`` (``dnsmasq`` or ``kea``), ``start``,
            ``stop``, ``dns`` (handed out, when set; empty means the
            firewall itself), ``domain``, and ``serving``; None when
            neither is set up for it.
    """
    identifier = interface.get("identifier")
    dnsmasq = network.get("dnsmasq") if isinstance(network.get("dnsmasq"), dict) else {}
    for item in dnsmasq.get("ranges") or [] if dnsmasq.get("enabled") else []:
        if item.get("interface") == identifier and ":" not in str(item.get("start") or ""):
            return {"mode": "dnsmasq", "start": item.get("start"), "stop": item.get("end"),
                    "dns": (dnsmasq.get("dns") or {}).get(identifier, []), "domain": item.get("domain"), "serving": True}
    subnet = _cidr((interface.get("ipv4") or [None])[0])
    for item in network.get("kea_subnets") or []:
        if subnet and item.get("subnet") == subnet:
            start, _, stop = ((item.get("pools") or [""])[0]).partition("-")
            serving = available and _kea_serving(identifier, network, clients)
            return {"mode": "kea", "start": start.strip() or None, "stop": stop.strip() or None,
                    "dns": item.get("dns") or [], "domain": item.get("domain"), "serving": serving}
    return None


def _opnsense_policies(ref: Any, rules: list[dict], names: dict[str, str], groups: set[str]) -> list[dict]:
    """OPNsense rules in the shared policy shape, in evaluation order.

    Args:
        ref: Builds a shared ID from an item name and key.
        rules: Stored rules (sorted by ``order``).
        names: Interface descriptions by identifier.
        groups: Interface group names.

    Returns:
        list[dict]: ``id``, ``name``, ``order``, ``action``, ``enabled``,
            ``origin`` (``system`` for automatic rules), ``scope``,
            ``interfaces`` (identifiers or group names; an old-format
            rule's descriptions resolved to identifiers where they match),
            ``interface_names``, ``direction``, ``quick`` (None when not
            reported), ``legacy``, ``ref``, ``source``, ``destination``,
            ``protocol``, ``ip_version``, and ``logging``.
    """
    by_description = {description: identifier for identifier, description in names.items()}

    def resolve(token: str) -> str:
        return token if token in names or token in groups else by_description.get(token, token)

    def side(endpoint: dict) -> dict:
        net = endpoint.get("net") or "any"
        return {"zone": None, "kind": None if net == "any" else "network", "values": [] if net == "any" else net.split(","),
                "ports": [endpoint["port"]] if endpoint.get("port") else [], "negate": endpoint.get("not") is True}

    found = []
    for index, rule in enumerate(rules):
        interfaces = [resolve(token) for token in rule.get("interfaces") or []]
        found.append({
            "id": ref("policy", rule.get("id") or rule.get("order")), "name": rule.get("description"), "order": index,
            "action": rule.get("action"), "enabled": rule.get("enabled") is True,
            "origin": "system" if rule.get("automatic") else "user", "scope": rule.get("scope"),
            "interfaces": interfaces, "interface_names": [names.get(item, item) for item in interfaces],
            "direction": rule.get("direction"), "quick": rule.get("quick"), "legacy": rule.get("legacy") is True,
            "ref": rule.get("ref"), "source": side(rule.get("source") or {}),
            "destination": side(rule.get("destination") or {}), "protocol": rule.get("protocol"),
            "ip_version": rule.get("ip_version"), "logging": rule.get("log") is True,
        })
    return found


def _firewall_sinkhole(system: dict, lans: list[dict], network: dict, networks: list[dict], available: bool) -> dict:
    """The firewall itself as a DNS sinkhole, when a DNS blocklist is enabled.

    OPNsense's "blocklist enabled" only says a saved blocklist is enabled,
    not that Unbound enforces it. So the entry's ``state`` is ``enforcing``
    only when, in a current reading (the latest poll succeeded and the
    blocklist, DNS, and services reads are complete), Unbound is enabled
    and running; ``configured`` when it is not; and ``last_known`` when the
    reading is not current. Only an enforcing entry claims the networks
    whose DHCP hands out the firewall as DNS.

    Args:
        system: Its stored system reading (hostname).
        lans: Its non-WAN interfaces.
        network: The stored ``opnsense_network`` telemetry.
        networks: The shared networks (their DHCP says which hand out the
            firewall itself as DNS).
        available: False when the firewall's latest poll failed.

    Returns:
        dict: A sinkhole entry like a Pi-hole's: ``ip`` (its first LAN-side
            address), ``names``, ``state``, ``evidence``, ``networks``, and
            ``blocklists`` (names and sizes).
    """
    sections = network.get("sections") or {}
    lists = [item for item in network.get("blocklists") or [] if item.get("enabled")]
    address = next((item["ipv4"][0].split("/")[0] for item in lans if item.get("ipv4")), "")
    current = available and all((sections.get(name) or {}).get("status") == "complete"
                                for name in ("blocklist_enabled", "dns", "services"))
    resolver = (network.get("dns") or {}).get("enabled") is True
    running = any(item.get("id") == "unbound" and item.get("running") for item in network.get("services") or [])
    count = f"{len(lists)} blocklist{'' if len(lists) == 1 else 's'}"
    if not current:
        since = (sections.get("blocklist_enabled") or {}).get("sampled_at")
        state, evidence = "last_known", [f"A DNS blocklist was enabled in OPNsense when last read ({since or 'time unknown'}); "
                                         "whether it is enforced now is unknown."]
    elif resolver and running:
        state, evidence = "enforcing", [f"OPNsense's DNS blocklist is on ({count}), and Unbound is running."]
    else:
        state, evidence = "configured", [f"A DNS blocklist is enabled in OPNsense ({count}), but Unbound is "
                                         f"{'not running' if resolver else 'switched off'}: nothing is blocked."]
    served = [item["name"] for item in networks if state == "enforcing" and (item.get("dhcp") or {}).get("serving")
              and not item["dhcp"].get("dns")]
    if served:
        evidence.append(f"DHCP hands out the firewall as DNS on {', '.join(served)}.")
    return {"ip": address, "names": [system.get("hostname") or "OPNsense"], "state": state, "evidence": evidence,
            "networks": served, "client": None, "guest": None, "likely_pihole": False, "integration": None,
            "kind": "opnsense",
            "blocklists": [{key: item.get(key) for key in ("description", "lists", "urls", "domains", "allowed")}
                           for item in lists]}


def _opnsense_carp(ref: Any, carp: Any) -> dict | None:
    """CARP virtual IPs by VHID, each with the IP aliases that ride it.

    Args:
        ref: Builds a shared ID from an item name and key.
        carp: The stored ``carp`` reading (``addresses`` by mode).

    Returns:
        dict | None: ``vips`` (``id``, ``interface``, ``vhid``, ``address``,
            ``state``, ``aliases``), ``demotion``, ``maintenance``, and
            ``message``; None when not read.
    """
    if not isinstance(carp, dict):
        return None
    addresses = carp.get("addresses") or []
    vips = [{
        "id": ref("carp", f"{item.get('interface')}:{item.get('vhid')}"), "interface": item.get("interface"),
        "vhid": item.get("vhid"), "address": item.get("address"), "state": item.get("state"),
        "aliases": [alias.get("address") for alias in addresses if alias.get("mode") != "carp"
                    and alias.get("vhid") == item.get("vhid") and alias.get("interface") == item.get("interface")],
    } for item in addresses if item.get("mode") == "carp"]
    return {"vips": vips, "demotion": carp.get("demotion"), "maintenance": carp.get("maintenance") is True,
            "message": carp.get("message")}

def from_opnsense(source_id: int, name: str, health: dict, network: dict, clients: list[dict],
                  sinkholes: list[dict], available: bool) -> dict:
    """Convert a stored OPNsense firewall into the shared form.

    Networks are the firewall's assigned interfaces (not loopback, and not
    WANs: an interface with gateways is upstream and its gateways are
    listed instead). Zones are its interface groups that have members,
    plus one per interface in no group; an interface in two groups is in
    both zones (display only). Policies are its rules as reported, in
    evaluation order, each with its ``scope`` (``floating``, ``group``,
    ``interface``, ``system``) and interfaces resolved to identifiers:
    OPNsense evaluates them in an order this form does not model (a
    non-``quick`` match can be overridden later), so there are no
    verdicts (``capabilities.verdicts`` is false). ``vpn`` holds WireGuard
    peers, ``services`` and ``carp`` what the firewall reports, and an
    enabled DNS blocklist makes the firewall one of the ``sinkholes``.

    Args:
        source_id: The OPNsense discovery source's ID.
        name: Its display name.
        health: The stored ``opnsense`` telemetry (system and interfaces).
        network: The stored ``opnsense_network`` telemetry.
        clients: Its current sightings (client details from its clients
            source), each with ``guest`` already added.
        sinkholes: Its DNS sinkholes.
        available: False when the source's last poll failed (this is the
            previous sample).

    Returns:
        dict: The shared form described in the module docstring.
    """
    def ref(item: str, key: Any) -> str | None:
        return reference("opnsense", source_id, item, key)

    sections = {**(health.get("sections") or {}), **(network.get("sections") or {})}
    interfaces = [item for item in health.get("interfaces") or []
                  if isinstance(item, dict) and item.get("identifier") and not str(item.get("device") or "").startswith("lo")]
    lans = [item for item in interfaces if not item.get("gateways")]
    wans = [item for item in interfaces if item.get("gateways")]
    networks = [{
        "id": ref("network", item["identifier"]), "name": item.get("description") or item["identifier"],
        "vlan": item.get("vlan"), "zone": ref("zone", item["identifier"]),
        "cidr": _cidr((item.get("ipv4") or [None])[0]), "ipv4": item.get("ipv4") or [], "ipv6": item.get("ipv6") or [],
        "gateway": str(ipaddress.ip_interface(item["ipv4"][0]).ip) if item.get("ipv4") and _cidr(item["ipv4"][0]) else None,
        "management": None, "dhcp": opnsense_dhcp(item, network, clients, available), "enabled": item.get("enabled") is not False,
        "default": item["identifier"] == "lan", "isolated": False, "internet": None, "mdns": False,
    } for item in lans]
    defaults = {route.get("gateway") for route in network.get("routes") or []}
    by_address = {address: item for item in wans for address in item.get("gateways") or []}
    gateways = [{
        "id": ref("gateway", item.get("name")), "name": item.get("name"), "address": item.get("address"),
        "network": ref("network", (by_address.get(item.get("address")) or {}).get("identifier")),
        "interface": (by_address.get(item.get("address")) or {}).get("description"),
        "status": item.get("status"), "label": item.get("label"), "monitored": item.get("monitored") is True,
        "delay_ms": item.get("delay_ms"), "stddev_ms": item.get("stddev_ms"), "loss_pct": item.get("loss_pct"),
        "default": item.get("address") in defaults,
    } for item in network.get("gateways") or []]
    groups = [item for item in network.get("groups") or [] if item.get("members")]
    grouped = {member for item in groups for member in item["members"]}
    zones = [{"id": ref("zone", item["name"]), "name": item["name"], "origin": "user", "kind": "group",
              "networks": [ref("network", member) for member in item["members"]]} for item in groups]
    zones += [{"id": ref("zone", item["identifier"]), "name": item.get("description") or item["identifier"],
               "origin": "system", "kind": "interface", "networks": [ref("network", item["identifier"])]}
              for item in interfaces if item["identifier"] not in grouped]
    zone_of = {member: ref("zone", item["name"]) for item in sorted(groups, key=lambda group: group["name"], reverse=True)
               for member in item["members"]}
    for item in networks:
        key = item["id"].rsplit(":", 1)[1]
        item["zone"] = zone_of.get(key, item["zone"])
    system = health.get("system") if isinstance(health.get("system"), dict) else {}
    memory = health.get("memory") if isinstance(health.get("memory"), dict) else {}
    used, total = memory.get("used"), memory.get("total")
    device = {
        "id": ref("device", "firewall"), "name": system.get("hostname") or name, "model": system.get("product"),
        "roles": ["gateway"], "kind": "gateway", "state": "online" if available else "unknown", "ip": None,
        "mac": None, "firmware": system.get("product"), "updatable": False, "uplink": None,
        "ports": [{"idx": item.get("device"), "state": item.get("status"), "speed": item.get("speed"), "connector": None,
                   "poe": None} for item in health.get("interfaces") or [] if isinstance(item, dict) and item.get("physical")],
        "radios": [], "uptime": None, "cpu": None,
        "memory": round(used / total * 100) if isinstance(used, (int, float)) and isinstance(total, (int, float)) and total else None,
        "tx_bps": None, "rx_bps": None,
    }
    names = {item["identifier"]: item.get("description") or item["identifier"] for item in interfaces}
    policies = _opnsense_policies(ref, network.get("rules") or [], names, {item["name"] for item in groups})
    wireguard = network.get("wireguard") if isinstance(network.get("wireguard"), dict) else {}
    vpn = [{
        # A peer is its public key on its interface: names can be edited and
        # repeated. Only a digest of the key leaves the API.
        "id": ref("vpn", f"wireguard:{peer_key(item)}") if peer_key(item) else None,
        "kind": "wireguard",
        "name": item.get("name"), "instance": item.get("instance"), "endpoint": item.get("endpoint"),
        "allowed_ips": item.get("allowed_ips") or [], "handshake": item.get("handshake"),
        "rx_bytes": item.get("rx_bytes"), "tx_bytes": item.get("tx_bytes"), "keepalive": item.get("keepalive"),
        "status": item.get("status"),
    } for item in wireguard.get("peers") or []]
    if network.get("blocklist_enabled") is True:
        sinkholes = [_firewall_sinkhole(system, lans, network, networks, available), *sinkholes]
    carp = _opnsense_carp(ref, network.get("carp"))
    shared_clients = [{
        "name": item.get("hostname") or item.get("mac_address"), "hostname": item.get("hostname"),
        # The address seen in use; a lease-only client shows its lease's.
        "mac": item.get("mac_address"), "ip": item.get("ip_address") or (item.get("lease") or {}).get("ip"),
        "ipv6": item.get("ipv6") or [],
        "network": names.get(item.get("scope")) or item.get("network_name"), "connection": "other", "uplink": None,
        "essid": None, "radio": None, "channel": None, "signal": None, "satisfaction": None, "wired_rate": None,
        "tx_bytes": None, "rx_bytes": None, "uptime": None, "guest": item.get("guest"),
        "state": item.get("state"), "seen_at": item.get("seen_at"),
    } for item in clients if item.get("state") in ("seen", "lease")]
    shared_sections = {
        "networks": _combined(sections, ("interfaces",)), "zones": _combined(sections, ("interfaces", "groups")),
        "gateways": _combined(sections, ("gateways", "routes")), "devices": _combined(sections, ("system",)),
        "clients": _combined(sections, ("arp", "ndp", "dnsmasq_leases", "kea_leases")),
        "policies": _combined(sections, ("rules",)), "vpn": _combined(sections, ("wireguard",)),
        "services": _combined(sections, ("services",)), "carp": _combined(sections, ("carp",)),
        "sinkholes": _combined(sections, ("blocklist_enabled", "blocklists")),
        **{key: {"status": "not_available", "sampled_at": None} for key in _OPNSENSE_ABSENT},
    }
    return {
        "source": {"id": source_id, "kind": "opnsense", "name": name},
        "site": None, "sampled_at": network.get("sampled_at") or health.get("sampled_at"), "available": available,
        "sections": shared_sections, "capabilities": {"verdicts": False},
        "networks": networks, "zones": zones, "gateways": gateways, "policies": policies, "paths": [],
        "sinkholes": sinkholes, "wifi": [], "devices": [device], "clients": shared_clients,
        "health": [], "neighbours": [], "dns": network.get("dns"), "vpn": vpn,
        "services": network.get("services") or [], "carp": carp,
    }
