# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Build a self-contained demo instance with made-up data, for screenshots.

The docs and README screenshots must never show a real lab, so they are
taken from this demo instead: an invented home lab (a gateway with two
WANs and a VPN tunnel, three switches, two access points, a six-node
Proxmox cluster with Ceph and ZFS storage and a dozen guests, a UPS, and a
dozen wired and Wi-Fi clients) placed in one location
with a 19-inch rack in the server closet and a 10-inch mini rack in the
office. Every name is
invented, addresses are documentation ranges (RFC 5737), and credentials
are placeholders that never reach a real controller.

Usage, from the repository root::

    .venv/bin/python scripts/demo_seed.py /tmp/labrat-demo

It writes ``local_config.toml``, ``labrat.sqlite3``, and ``admin.txt`` (a
generated sign-in password) into that directory, replacing any earlier
demo there. Serve it with ``LABRAT_CONFIG=<dir>/local_config.toml``; the
data is "fresh" for a few minutes, so take screenshots right after seeding.
Nothing here touches your own ``local_config.toml`` or database.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import demo_telemetry  # Beside this script: invented cluster and network telemetry.

ROOT = Path(__file__).resolve().parents[1]

CONFIG = """\
[server]
allowed_hosts = ["localhost", "127.0.0.1"]

[database]
path = {db}

[auth]
secret_key = {secret}
secure_cookies = false

[discovery]
network = true
nut = true
snmp = false
mndp = false

[scan]
ranges = ["192.0.2.0/24"]
probes = ["nut"]

[integrations.unifi.office]
display_name = "Office gateway"
url = "https://unifi.example.lan"
api_key = "demo-placeholder-key"

[integrations.proxmox.cluster]
display_name = "Lab cluster"
url = "https://pve.example.lan:8006"
token_id = "labrat@pve!readonly"
token_secret = "demo-placeholder-secret"
"""


def mac(number: int) -> str:
    """Make up a locally administered MAC address.

    Args:
        number: A number unique within the demo (0 to 65535).

    Returns:
        str: The address, colon-separated.
    """
    return f"02:00:5e:00:{number // 256:02x}:{number % 256:02x}"


def main(target: Path) -> None:
    """Create the demo's config, schema, observations, and inventory.

    Args:
        target: The directory to write the demo into; its database is
            replaced.
    """
    target.mkdir(parents=True, exist_ok=True)
    for name in ("labrat.sqlite3", "labrat.sqlite3-wal", "labrat.sqlite3-shm"):
        (target / name).unlink(missing_ok=True)
    password = secrets.token_urlsafe(18)
    (target / "admin.txt").write_text(f"demo\n{password}\n", encoding="utf-8")
    config_path = target / "local_config.toml"
    config_path.write_text(CONFIG.format(db=json.dumps(str(target / "labrat.sqlite3")),
                                         secret=json.dumps(secrets.token_urlsafe(48))), encoding="utf-8")
    os.environ["LABRAT_CONFIG"] = str(config_path)

    from alembic import command
    from alembic.config import Config
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

    from sqlalchemy import select
    from labrat.app import create_app
    from labrat.collector.contracts import (DeviceObservation, DiscoveryBatch, LinkObservation,
                                            NetworkInterfaceObservation, PortObservation, WanObservation)
    from labrat.collector.clients import reconcile_clients
    from labrat.collector.sources import sync_configured_sources
    from labrat.collector.store import apply_batch, record_telemetry, record_wan
    from labrat.config import load_local_config
    from labrat.discovery.models import DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource

    app = create_app()
    sessions = app.extensions["labrat_session_factory"]
    config = load_local_config()
    config["nut"] = {"default": {}}
    sync_configured_sources(sessions, load_local_config())
    now = datetime.now(timezone.utc)

    with sessions.begin() as db:
        sources = {source.kind: source for source in db.scalars(select(DiscoverySource))}
        if "nut" not in sources:
            sources["nut"] = DiscoverySource(name="nut:default", kind="nut", config_name="default", enabled=True)
            db.add(sources["nut"])
            db.flush()
        unifi, proxmox, nut = sources["unifi"], sources["proxmox"], sources["nut"]

        def ports(device: str, names: list[str], speed: int, medium: str = "copper",
                  connector: str = "rj45", up: set[str] | None = None) -> list[PortObservation]:
            return [PortObservation(device_remote_id=device, remote_id=name, name=name, speed_mbps=speed,
                                    max_speed_mbps=speed, link_up=(up is None or name in up),
                                    connector_type=connector, medium=medium) for name in names]

        apply_batch(db, unifi, DiscoveryBatch(
            devices=(
                DeviceObservation("gw", "Gateway", "router", {"mac": mac(1), "management_ips": ["192.0.2.1"]},
                                  {"model": "Example Gateway", "site_id": "default", "network_interfaces_complete": True}),
                DeviceObservation("core", "Core Switch", "switch", {"mac": mac(2)}, {"model": "Example 16-port", "site_id": "default"}),
                DeviceObservation("rack", "Rack Switch", "switch", {"mac": mac(3)}, {"model": "Example 8-port 10G", "site_id": "default"}),
                DeviceObservation("hall", "Hall AP", "access_point", {"mac": mac(4)}, {"model": "Example AP", "site_id": "default"}),
                DeviceObservation("roof", "Roof AP", "access_point", {"mac": mac(5)}, {"model": "Example outdoor AP", "site_id": "default"}),
                DeviceObservation("office", "Office Switch", "switch", {"mac": mac(6)},
                                  {"model": "Example 8-port mini", "site_id": "default"}),
            ),
            ports=tuple(
                ports("gw", ["Port 1", "Port 2", "Port 3", "Port 4"], 2500, up={"Port 1", "Port 2", "Port 3"})
                + ports("core", [f"Port {n}" for n in range(1, 17)], 1000, up={f"Port {n}" for n in (1, 2, 3, 5, 6, 9)})
                + ports("core", ["Port 17"], 10000, "dac", "sfp_plus")
                + ports("rack", [f"Port {n}" for n in range(1, 8)], 10000, "dac", "sfp_plus", up={"Port 1", "Port 2", "Port 3"})
                + ports("rack", ["Port 8"], 10000, "dac", "sfp_plus")
                + ports("hall", ["Port 1"], 1000)
                + ports("roof", ["Port 1"], 1000)
                + ports("office", [f"Port {n}" for n in range(1, 9)], 1000, up={f"Port {n}" for n in (1, 2, 3, 4, 5, 6, 8)})
            ),
            links=(
                LinkObservation("gw", "Port 1", "core", "Port 1", "unifi-uplink", "confirmed"),
                LinkObservation("core", "Port 17", "rack", "Port 8", "unifi-uplink", "confirmed"),
                LinkObservation("core", "Port 2", "hall", "Port 1", "unifi-uplink", "confirmed"),
                LinkObservation("core", "Port 3", "roof", "Port 1", "unifi-uplink", "confirmed"),
                LinkObservation("core", "Port 5", "office", "Port 8", "unifi-uplink", "confirmed"),
            ),
            # The gateway's LAN bridge and two tagged networks on it.
            network_interfaces=(
                NetworkInterfaceObservation("gw", "br0", "br0", "bridge", members=("Port 1", "Port 2"), active=True,
                                            details={"cidr": "192.0.2.1/25", "network": "Default"}),
                NetworkInterfaceObservation("gw", "br0.20", "br0.20", "vlan", parent_name="br0", vlan_id=20, active=True,
                                            details={"cidr": "192.0.2.129/26", "network": "IoT"}),
                NetworkInterfaceObservation("gw", "br0.30", "br0.30", "vlan", parent_name="br0", vlan_id=30, active=True,
                                            details={"cidr": "192.0.2.193/26", "network": "Guest"}),
            ),
        ), seen_at=now)
        apply_batch(db, proxmox, DiscoveryBatch(
            devices=tuple(DeviceObservation(f"node/{name}", name, "server", {"mac": mac(10 + index)},
                                            {"status": "online", "model": "Example server", "platform": "proxmox"})
                          for index, name in enumerate(["pve1", "pve2", "pve3", "mini1", "mini2", "mini3"])),
            ports=tuple(port for name in ["pve1", "pve2", "pve3"]
                        for port in ports(f"node/{name}", ["nic0", "nic1"], 10000, "dac", "sfp_plus", up={"nic0"}))
                  + tuple(port for name in ["mini1", "mini2", "mini3"]
                          for port in ports(f"node/{name}", ["nic0"], 1000)),
        ), seen_at=now)
        # The cluster's live telemetry (demo_telemetry.py), stored the way
        # the collector stores it, so Cluster, the storage shelves, and the
        # rack view's drive bays have data.
        for index, name in enumerate(["pve1", "pve2", "pve3", "mini1", "mini2", "mini3"]):
            node = db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.source_id == proxmox.id,
                                                            DiscoveredDevice.remote_id == f"node/{name}"))
            details = json.loads(node.details_json or "{}")
            details["proxmox_node"] = demo_telemetry.node_telemetry(name, index, now)
            details["proxmox_clients"] = demo_telemetry.clients_telemetry(name, now)
            if name == "pve1":
                details["proxmox_cluster"] = demo_telemetry.cluster_telemetry(now)
            node.details_json = json.dumps(details)
        apply_batch(db, nut, DiscoveryBatch(devices=(
            DeviceObservation("ups/rack", "Rack UPS", "ups", {"serial": "DEMO0001"},
                              {"model": "Example 1500VA", "manufacturer": "Example"}),
        )), seen_at=now)
        record_telemetry(db, nut, "ups/rack", "ups", {
            "battery_charge": 100, "battery_runtime_s": 2580, "load_percent": 24, "real_power_w": 186,
            "input_voltage": 120, "status_raw": "OL", "flags": ["online"], "state": "online",
            "sampled_at": now.isoformat(),
        })
        # The switch ports the servers' first NICs plug into (cross-source LLDP).
        def port_id(source: DiscoverySource, remote: str, name: str) -> int:
            device = db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id,
                                                              DiscoveredDevice.remote_id == remote))
            return db.scalar(select(DiscoveredPort.id).where(DiscoveredPort.discovered_device_id == device.id,
                                                             DiscoveredPort.name == name))
        for index, name in enumerate(["pve1", "pve2", "pve3"], start=1):
            a, b = port_id(unifi, "rack", f"Port {index}"), port_id(proxmox, f"node/{name}", "nic0")
            db.add(DiscoveredLink(source_id=unifi.id, a_port_id=min(a, b), b_port_id=max(a, b),
                                  confidence="confirmed", evidence="lldp", last_seen_at=now))
        for index, name in ((1, "mini1"), (2, "mini2"), (6, "mini3")):
            a, b = port_id(unifi, "office", f"Port {index}"), port_id(proxmox, f"node/{name}", "nic0")
            db.add(DiscoveredLink(source_id=unifi.id, a_port_id=min(a, b), b_port_id=max(a, b),
                                  confidence="confirmed", evidence="lldp", last_seen_at=now))
        # Connected clients: a few wired to the office switch, the rest on Wi-Fi.
        clients = sources.get("unifi_clients")
        if clients is None:
            clients = DiscoverySource(name="unifi_clients:office", kind="unifi_clients", config_name="office",
                                      enabled=True)
            db.add(clients)
            db.flush()
            sources["unifi_clients"] = clients
        wired = [("Office printer", "office", 3, "Example Printing"), ("Desk PC", "office", 4, "Example Computers"),
                 ("Desk dock", "office", 5, "Example Docks")]
        wireless = [("Living room TV", "hall", "Example Displays", "ac", -58), ("Laptop", "hall", "Example Computers", "ax", -49),
                    ("Phone", "hall", "Example Mobile", "ax", -55), ("Tablet", "hall", "Example Mobile", "ac", -63),
                    ("Thermostat", "hall", "Example Climate", "ng", -66), ("Doorbell camera", "roof", "Example Security", "ng", -70),
                    ("Garden sensor", "roof", "Example Sensors", "ng", -74), ("Speaker", "hall", "Example Audio", "ac", -60),
                    ("Game console", "hall", "Example Games", "ax", -52)]
        records = []
        for number, (name, switch, port, vendor) in enumerate(wired, start=1):
            records.append({"remote_id": f"default:client:w{number}", "site_id": "default", "site_name": "Default",
                            "controller_id": f"w{number}", "name": name, "hostname": name.lower().replace(" ", "-"),
                            "ip_address": f"192.0.2.{20 + number}", "mac_address": mac(100 + number),
                            "connection": "wired", "state": "connected",
                            "connected_at": (now - timedelta(days=number)).isoformat(),
                            "uplink_remote_id": switch, "classic_available": True,
                            "switch_mac": mac(6).replace(":", ""), "switch_port": port, "oui": vendor,
                            "network": "Default", "vlan": 1})
        for number, (name, ap, vendor, proto, rssi) in enumerate(wireless, start=1):
            records.append({"remote_id": f"default:client:x{number}", "site_id": "default", "site_name": "Default",
                            "controller_id": f"x{number}", "name": name, "hostname": name.lower().replace(" ", "-"),
                            "ip_address": f"192.0.2.{40 + number}", "mac_address": mac(120 + number),
                            "connection": "wireless", "state": "connected",
                            "connected_at": (now - timedelta(hours=3 * number)).isoformat(),
                            "uplink_remote_id": ap, "classic_available": True, "oui": vendor,
                            "essid": "Example WiFi", "radio_proto": proto, "signal": rssi,
                            "channel": 36 if proto != "ng" else 6, "network": "Default", "vlan": 1})
        reconcile_clients(db, clients, unifi, records, sampled_at=now)
        # The gateway's network snapshot for the Networks page.
        unifi_devices = [
            ("gw", "Gateway", "Example Gateway", 1, "192.0.2.1", ["switching", "gateway"], 4, None),
            ("core", "Core Switch", "Example 16-port", 2, "192.0.2.2", ["switching"], 17, "Gateway"),
            ("rack", "Rack Switch", "Example 8-port 10G", 3, "192.0.2.3", ["switching"], 8, "Core Switch"),
            ("hall", "Hall AP", "Example AP", 4, "192.0.2.4", ["accessPoint"], 1, "Core Switch"),
            ("roof", "Roof AP", "Example outdoor AP", 5, "192.0.2.5", ["accessPoint"], 1, "Core Switch"),
            ("office", "Office Switch", "Example 8-port mini", 6, "192.0.2.6", ["switching"], 8, "Core Switch"),
        ]
        snapshot_devices = [{
            "id": device_id, "name": name, "model": model, "mac": mac(number), "ip": ip, "features": features,
            "state": "ONLINE", "firmware": "7.1.66", "updatable": device_id == "roof", "uptime": 9 * 86400 + number * 3600,
            "cpu": 0.08 + number / 100, "memory": 0.42 + number / 50, "rx_bps": 2e6 * number, "tx_bps": 1.4e6 * number,
            "uplink_device": uplink or "", "radios": [],
            "ports": [{"idx": port, "state": "UP" if port <= max(1, count - 2) else "DOWN", "speed": 1000 if port <= max(1, count - 2) else None,
                       "max_speed": 10000 if device_id == "rack" else 1000, "connector": "rj45", "poe": "off"}
                      for port in range(1, count + 1)],
        } for device_id, name, model, number, ip, features, count, uplink in unifi_devices]
        snapshot_clients = [{
            "name": record["name"], "hostname": record["hostname"], "ip": record["ip_address"], "mac": record["mac_address"],
            "type": "WIRED" if record["connection"] == "wired" else "WIRELESS", "access": "DEFAULT", "network": "Default",
            "is_guest": False, "oui": record["oui"], "connected_at": record["connected_at"], "uptime": 3600 * (number + 2),
            "uplink_device": {"office": "Office Switch", "hall": "Hall AP", "roof": "Roof AP"}[record["uplink_remote_id"]],
            "essid": record.get("essid"), "radio": record.get("radio_proto"), "channel": record.get("channel"),
            "signal": record.get("signal"), "satisfaction": 96 - number, "wired_rate": 1000 if record["connection"] == "wired" else 0,
            "rx_bytes": None, "tx_bytes": None,
        } for number, record in enumerate(records)]
        gateway = db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.source_id == unifi.id,
                                                           DiscoveredDevice.remote_id == "gw"))
        details = json.loads(gateway.details_json or "{}")
        details["unifi_network"] = demo_telemetry.network_snapshot(now, snapshot_devices, snapshot_clients)
        gateway.details_json = json.dumps(details)
        # A day of WAN and VPN history, five minutes apart, ending now.
        for step in range(288, -1, -1):
            at = now - timedelta(minutes=5 * step)
            wave = 0.5 + 0.5 * math.sin(step / 288 * 2 * math.pi * 3)
            snapshot = {
                "internet": {"status": "ok", "isp": "Example Fiber", "latency_ms": 11, "drops": 0,
                             "uptime_s": 864000, "speedtest": None},
                "uplinks": [
                    {"id": "wan1", "name": "WAN1", "isp": "Example Fiber", "interface": "Port 4", "enabled": True,
                     "up": True, "active": True, "media": "GE", "port": 4, "speed_mbps": 2500, "max_speed_mbps": 2500,
                     "latency_ms": 11, "availability": 100, "uptime_s": 864000,
                     "rx_bps": int(4e6 + 30e6 * wave), "tx_bps": int(1e6 + 6e6 * wave),
                     "ip": "203.0.113.24"},
                    {"id": "wan2", "name": "WAN2", "isp": "Example Cable", "interface": "Port 3", "enabled": True,
                     "up": True, "active": False, "media": "GE", "port": 3, "speed_mbps": 1000, "max_speed_mbps": 1000,
                     "latency_ms": 23, "availability": 99.9, "uptime_s": 432000,
                     "rx_bps": 2400, "tx_bps": 1800, "ip": "198.51.100.40"},
                ],
                "vpn": {"available": True, "sampled_at": at.isoformat(), "remote_access_sessions": 0,
                        "connections": [{"id": "vpn-1", "name": "office-tunnel", "protocol": "wireguard",
                                         "enabled": True, "state": "connected", "reported_status": "CONNECTED",
                                         "connected_at": int((now - timedelta(days=3)).timestamp()),
                                         "rx_bps": int(2e5 + 9e5 * wave), "tx_bps": int(1e5 + 3e5 * wave)}]},
            }
            record_wan(db, unifi, WanObservation(gateway_mac=mac(1), snapshot=snapshot), seen_at=at)
        for source in sources.values():
            source.last_attempt_at = source.last_success_at = now
            source.last_error = None

    # Inventory, through the public API as an administrator would.
    client = app.test_client()
    client.get("/setup")
    with client.session_transaction() as state:
        token = state["csrf_token"]
    client.post("/setup", data={"_csrf_token": token, "username": "demo",
                                "password": password, "confirm_password": password})
    with client.session_transaction() as state:
        csrf = state["csrf_token"]

    def post(path: str, payload: dict) -> dict:
        response = client.post(path, json=payload, headers={"X-CSRF-Token": csrf})
        if response.status_code >= 400:
            raise SystemExit(f"{path}: {response.status_code} {response.get_data(as_text=True)}")
        return response.json

    site = post("/api/v1/locations", {"kind": "site", "name": "Example Home"})["id"]
    closet = post("/api/v1/locations", {"kind": "room", "site_id": site, "name": "Server closet"})["id"]
    office = post("/api/v1/locations", {"kind": "room", "site_id": site, "name": "Office"})["id"]
    roof = post("/api/v1/locations", {"kind": "room", "site_id": site, "name": "Roof"})["id"]
    rack = post("/api/v1/locations", {"kind": "rack", "room_id": closet, "name": "Rack A", "height_u": 24})["id"]
    mini = post("/api/v1/locations", {"kind": "rack", "room_id": office, "name": "Mini rack", "height_u": 12,
                                      "width_inches": 10, "has_rear": False})["id"]
    layout = client.get("/api/v1/locations").json
    container = {(item["kind"], item["room_id"]): item["id"] for item in layout["containers"]}
    rack_container = next(item["id"] for item in layout["containers"] if item.get("rack_id") == rack)
    mini_container = next(item["id"] for item in layout["containers"] if item.get("rack_id") == mini)
    with sessions() as db:
        observed = {item.name: item.id for item in db.scalars(select(DiscoveredDevice))}
    # The rack servers are 3U, tall enough for their faceplates to show
    # their drives as hot-swap bays.
    for name, unit, height in (("Core Switch", 22, 1), ("Rack Switch", 21, 1), ("pve1", 18, 3), ("pve2", 15, 3),
                               ("pve3", 12, 3), ("Rack UPS", 2, 1)):
        post("/api/v1/placements", {"discovery_id": observed[name], "container_id": rack_container, "rack_u": unit,
                                    "height_u": height})
    for name, unit in (("Office Switch", 11), ("mini1", 9), ("mini2", 8), ("mini3", 7)):
        post("/api/v1/placements", {"discovery_id": observed[name], "container_id": mini_container, "rack_u": unit})
    for name, height, kind, unit in (("Patch panel", 1, "Patch panel", 12), ("Mini NAS", 2, "Storage", 4),
                                     ("Power strip", 1, "Power", 1)):
        post("/api/v1/rack-devices", {"name": name, "height_u": height, "class_name": kind,
                                      "container_id": mini_container, "rack_u": unit})
    post("/api/v1/placements", {"discovery_id": observed["Gateway"], "container_id": container[("floor", closet)],
                                "position": {"x": 0.7, "y": 0.6}})
    post("/api/v1/placements", {"discovery_id": observed["Hall AP"], "container_id": container[("ceiling", office)],
                                "position": {"x": 0.5, "y": 0.5}})
    post("/api/v1/placements", {"discovery_id": observed["Roof AP"], "container_id": container[("ceiling", roof)],
                                "position": {"x": 0.4, "y": 0.4}})
    app.extensions["labrat_engine"].dispose()
    print(f"Demo ready in {target}. Sign in as 'demo' with the password in admin.txt.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: demo_seed.py <directory>")
    main(Path(sys.argv[1]).expanduser().resolve())
