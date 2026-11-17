# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Verify cable media, controller uplinks, LLDP neighbors, and MNDP.

UniFi responses are hand-built in the shapes the real controller returns;
MNDP packets are encoded here. No test touches a network.
"""

import json
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from labrat.collector.adapters import unifi_classic
from labrat.collector.adapters.mndp import MndpAdapter, category_for_board, decode_packet
from labrat.collector.adapters.unifi import UniFiAdapter
from labrat.collector.contracts import DeviceObservation, DiscoveryBatch, PortObservation
from labrat.collector.store import apply_batch
from labrat.config import load_local_config
from labrat.database import make_engine, make_session_factory
from labrat.discovery.models import DiscoveredLink, DiscoveredPort, DiscoverySource

BASE = "https://unifi.example"
INTEGRATION = f"{BASE}/proxy/network/integration/v1"
CLASSIC = f"{BASE}/proxy/network/api/s/default/stat/device"


class FakeJsonClient:
    """Return canned responses by URL; a callable value raises instead."""

    def __init__(self, responses: dict) -> None:
        """Prepare the URL-to-payload table.

        Args:
            responses: Payloads keyed by full URL.
        """
        self.responses = responses

    def get_json(self, url: str, headers: dict, verify) -> object:
        """Return the fixture for a URL, raising for unknown routes.

        Args:
            url: The requested URL.
            headers: Request headers (unused).
            verify: TLS verification (unused).

        Returns:
            object: The payload for that URL.

        Raises:
            AssertionError: For a URL the fixture does not know.
        """
        if url not in self.responses:
            raise AssertionError(f"Unexpected API path: {url}")
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value


def unifi_fixture(classic) -> FakeJsonClient:
    """Two adopted switches, wired aggregation <- data, plus a classic list.

    Args:
        classic: The classic stat/device response to serve.

    Returns:
        FakeJsonClient: The fixture client.
    """
    return FakeJsonClient({
        f"{INTEGRATION}/sites?offset=0&limit=100": {
            "data": [{"id": "site-1", "internalReference": "default", "name": "Default"}],
            "totalCount": 1,
        },
        f"{INTEGRATION}/sites/site-1/devices?offset=0&limit=100": {
            "data": [
                {"id": "agg", "model": "USW-Aggregation", "features": ["switching"], "macAddress": "AA:AA:AA:00:00:01"},
                {"id": "data", "model": "USW-Pro-Max-16", "features": ["switching"], "macAddress": "aa:aa:aa:00:00:02"},
            ],
            "totalCount": 2,
        },
        f"{INTEGRATION}/sites/site-1/devices/agg": {
            "id": "agg", "name": "USW Aggregation", "features": {"switching": {}},
            "interfaces": {"ports": [{"idx": i, "speedMbps": 10000, "maxSpeedMbps": 10000} for i in (4, 8)]},
        },
        f"{INTEGRATION}/sites/site-1/devices/data": {
            "id": "data", "name": "Cluster Data", "features": {"switching": {}},
            "interfaces": {"ports": [{"idx": i, "speedMbps": 10000, "maxSpeedMbps": 10000} for i in (1, 12)]},
        },
        CLASSIC: classic,
        f"{BASE}/proxy/network/api/s/default/stat/health": {"data": []},
    })


CLASSIC_DEVICES = {"data": [
    {
        "mac": "aa:aa:aa:00:00:01", "name": "USW Aggregation",
        "port_table": [
            {"port_idx": 4, "media": "SFP+", "sfp_found": True, "sfp_compliance": "DAC"},
            {"port_idx": 8, "media": "SFP+", "sfp_found": True, "sfp_compliance": "10G BASE-SR"},
        ],
        "lldp_table": [
            # A MikroTik bridge port: RouterOS naming identifies the vendor.
            {"local_port_idx": 8, "chassis_id": "4C:5E:0C:11:22:33", "port_id": "bridge/sfp-sfpplus4"},
            # A host whose identity UniFi cannot tell us: skipped, not guessed.
            {"local_port_idx": 4, "chassis_id": "58:47:ca:00:00:09", "port_id": "58:47:ca:00:00:0a"},
            # A UniFi neighbor: covered by the uplink record instead.
            {"local_port_idx": 4, "chassis_id": "aa:aa:aa:00:00:02", "port_id": "Port 12"},
        ],
    },
    {
        "mac": "aa:aa:aa:00:00:02", "name": "Cluster Data",
        "port_table": [
            {"port_idx": 1, "media": "GE"},
            {"port_idx": 12, "media": "SFP+", "sfp_found": True, "sfp_part": "SFP-H10GB-CU1M", "sfp_compliance": "Unknown"},
        ],
        "uplink": {"type": "wire", "port_idx": 12, "uplink_mac": "AA:AA:AA:00:00:01", "uplink_remote_port": 4, "speed": 10000},
    },
]}


class MediumTests(unittest.TestCase):
    """Classify cable media from classic port records."""

    def test_incomplete_neighbor_is_retained_without_becoming_inventory(self):
        """Keep the switch attachment even when its remote port is missing."""
        neighbors, _ = unifi_classic.lldp_neighbors([{
            "mac": "aa:bb:cc:dd:ee:ff", "lldp_table": [{
                "chassis_id": "00:11:22:33:44:55", "port_id": "", "local_port_idx": 5,
            }],
        }], set())
        self.assertEqual(len(neighbors), 1)
        self.assertEqual((neighbors[0].local_port, neighbors[0].port_id), ("5", ""))
        self.assertIsNone(neighbors[0].vendor)

    def test_media_classification(self) -> None:
        """Map copper, DAC, multimode, single-mode, and unknown modules."""
        cases = [
            ({"media": "GE"}, "copper"),
            ({"media": "2P5GE"}, "copper"),
            ({"media": "SFP+", "sfp_found": False}, None),
            ({"media": "SFP+", "sfp_found": True, "sfp_compliance": "DAC"}, "dac"),
            ({"media": "SFP+", "sfp_found": True, "sfp_compliance": "1000BASE-CX"}, "dac"),
            ({"media": "SFP+", "sfp_found": True, "sfp_compliance": "Unknown", "sfp_part": "JD096C"}, "dac"),
            ({"media": "SFP+", "sfp_found": True, "sfp_compliance": "10G BASE-SR"}, "fiber_multimode"),
            ({"media": "SFP+", "sfp_found": True, "sfp_compliance": "Unknown", "sfp_part": "OM-MM-10G-D"}, "fiber_multimode"),
            ({"media": "SFP", "sfp_found": True, "sfp_compliance": "1000BASE-LX"}, "fiber_singlemode"),
            ({"media": "SFP", "sfp_found": True, "sfp_compliance": "1000LX"}, "fiber_singlemode"),
            ({"media": "SFP", "sfp_found": True, "sfp_compliance": "Unknown", "sfp_part": "GLC-LH-SMD"}, "fiber_singlemode"),
            ({"media": "SFP+", "sfp_found": True, "sfp_compliance": "Unknown", "sfp_part": "SF"}, None),
            ({}, None),
        ]
        for port, expected in cases:
            with self.subTest(port=port):
                self.assertEqual(unifi_classic.medium_for_port(port), expected)

    def test_cable_assembly_ends_share_a_serial(self) -> None:
        """Two unidentified modules with one serial are one DAC (or AOC)."""
        unnamed = {"sfp_found": True, "sfp_compliance": "Unknown", "sfp_part": "VENDOR-PART-1",
                   "sfp_serial": "SERIAL-A"}
        modules = {("switch-1", "6"): unnamed, ("switch-2", "10"): dict(unnamed),
                   ("switch-1", "7"): {**unnamed, "sfp_serial": "SERIAL-B"}}
        self.assertEqual(unifi_classic.cable_assembly_media(modules),
                         {("switch-1", "6"): "dac", ("switch-2", "10"): "dac"},
                         "A lone module stays unknown")
        aoc = {key: {**port, "sfp_part": "AOC-10G"} for key, port in modules.items()}
        self.assertEqual(unifi_classic.cable_assembly_media(aoc)[("switch-1", "6")], "fiber")
        same_device = {("switch-1", "1"): unnamed, ("switch-1", "2"): dict(unnamed)}
        self.assertEqual(unifi_classic.cable_assembly_media(same_device), {},
                         "Both ends must be on different devices")
        short = {key: {**port, "sfp_serial": "AB"} for key, port in modules.items()}
        self.assertEqual(unifi_classic.cable_assembly_media(short), {}, "Too short a serial proves nothing")

    def test_connector_for_port(self) -> None:
        """Read connectors from classic media for any model."""
        for port, connector in (
            ({"media": "GE"}, "rj45"), ({"media": "2P5GE"}, "rj45"), ({"media": "10GE"}, "rj45"),
            ({"media": "SFP"}, "sfp"), ({"media": "sfp+"}, "sfp_plus"), ({"media": "SFP28"}, "sfp28"),
            ({"media": "QSFP28"}, None), ({}, None),
        ):
            with self.subTest(port=port):
                self.assertEqual(unifi_classic.connector_for_port(port), connector)

    def test_neighbor_vendor(self) -> None:
        """Recognize the packaged vendors' port names and MAC prefixes."""
        self.assertEqual(unifi_classic.neighbor_vendor("58:47:ca:00:00:01", "bridge/sfp-sfpplus4"), "MikroTik")
        self.assertEqual(unifi_classic.neighbor_vendor("58:47:ca:00:00:01", "ether3"), "MikroTik")
        self.assertEqual(unifi_classic.neighbor_vendor("4c:5e:0c:00:00:01", "x"), "MikroTik")
        self.assertIsNone(unifi_classic.neighbor_vendor("58:47:ca:00:00:01", "58:47:ca:00:00:02"))

    def test_network_os_port_names_are_recognized(self) -> None:
        """Name Cisco and Juniper neighbors by their distinctive port naming."""
        for port_id, vendor in (("Gi1/0/24", "Cisco"), ("GigabitEthernet1/0/1", "Cisco"),
                                ("Te1/1/2", "Cisco"), ("ge-0/0/3", "Juniper"), ("xe-0/1/0.0", "Juniper")):
            with self.subTest(port_id=port_id):
                self.assertEqual(unifi_classic.neighbor_vendor("00:11:22:33:44:55", port_id), vendor)

    def test_host_interface_names_stay_unnamed(self) -> None:
        """Leave hosts running lldpd to cross-source matching, not a vendor guess."""
        for port_id in ("eno1", "enp3s0f1", "eth0", "ens18", "ifname:eno2", "Gi1", "vmbr0"):
            with self.subTest(port_id=port_id):
                self.assertIsNone(unifi_classic.neighbor_vendor("00:11:22:33:44:55", port_id))

    def test_configured_vendor_entries_come_first(self) -> None:
        """A connection's own entries add vendors and win over the packaged table."""
        rules = unifi_classic.vendor_rules([
            {"vendor": "Acme", "port_pattern": r"port\d+"},
            {"vendor": "Resold", "ouis": ["4C-5E-0C"]},
        ])
        self.assertEqual(unifi_classic.neighbor_vendor("00:11:22:33:44:55", "port7", rules), "Acme")
        self.assertEqual(unifi_classic.neighbor_vendor("4c:5e:0c:00:00:01", "x", rules), "Resold")
        self.assertEqual(unifi_classic.neighbor_vendor("00:11:22:33:44:55", "ether1", rules), "MikroTik",
                         "a port name still wins over a MAC prefix")
        self.assertIs(unifi_classic.vendor_rules(None), unifi_classic.packaged_vendor_rules())

    def test_invalid_vendor_entries_are_rejected(self) -> None:
        """Refuse a vendor setting that could not match or would not compile."""
        for bad in ({"vendor": "Acme"}, [{"vendor": "", "port_pattern": "x"}],
                    [{"vendor": "Acme", "port_pattern": "("}], [{"vendor": "Acme", "ouis": ["4c5e0c"]}],
                    [{"vendor": "Acme", "ouis": "4c:5e:0c"}], ["Acme"], [{"vendor": "A" * 41, "ouis": ["4c:5e:0c"]}],
                    [{"vendor": "Acme", "port_pattern": " "}]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                unifi_classic.vendor_rules(bad)


class UniFiClassicTests(unittest.TestCase):
    """Add media, uplinks, and LLDP neighbors to a UniFi batch."""

    def poll(self, classic):
        """Poll the fixture controller with a given classic response.

        Args:
            classic: The classic stat/device response.

        Returns:
            DiscoveryBatch: The adapter's batch.
        """
        return UniFiAdapter(unifi_fixture(classic)).poll({"url": BASE, "api_key": "fake"})

    def test_media_uplinks_and_mikrotik_neighbor(self) -> None:
        """Report media, a confirmed uplink, and one MikroTik neighbor device."""
        batch = self.poll(CLASSIC_DEVICES)
        media = {(p.device_remote_id, p.remote_id): p.medium for p in batch.ports}
        self.assertEqual(media[("site-1:agg", "4")], "dac")
        self.assertEqual(media[("site-1:agg", "8")], "fiber_multimode")
        self.assertEqual(media[("site-1:data", "1")], "copper")
        self.assertEqual(media[("site-1:data", "12")], "dac")

        links = {(l.a_device_remote_id, l.a_port_remote_id, l.b_device_remote_id, l.b_port_remote_id): l
                 for l in batch.links}
        uplink = links[("site-1:agg", "4", "site-1:data", "12")]
        self.assertEqual((uplink.evidence, uplink.confidence), ("unifi-uplink", "confirmed"))

        neighbors = [d for d in batch.devices if d.details.get("platform") == "lldp"]
        self.assertEqual(len(neighbors), 1, "the unidentified host must be skipped")
        mikrotik = neighbors[0]
        self.assertEqual(mikrotik.remote_id, "site-1:lldp:4c:5e:0c:11:22:33")
        self.assertEqual(mikrotik.category, "switch")
        self.assertEqual(mikrotik.name, "MikroTik 11:22:33")
        port = next(p for p in batch.ports if p.device_remote_id == mikrotik.remote_id)
        self.assertEqual((port.remote_id, port.name), ("bridge/sfp-sfpplus4", "sfp-sfpplus4"))
        self.assertEqual(port.medium, "fiber_multimode", "a cable has one medium at both ends")
        lldp = links[("site-1:agg", "8", mikrotik.remote_id, "bridge/sfp-sfpplus4")]
        self.assertEqual((lldp.evidence, lldp.confidence), ("lldp", "confirmed"))

    def test_configured_vendor_makes_a_neighbor_device(self) -> None:
        """Turn a neighbor the connection's vendor entries identify into a device."""
        batch = UniFiAdapter(unifi_fixture(CLASSIC_DEVICES)).poll({
            "url": BASE, "api_key": "fake", "lldp_vendors": [{"vendor": "Acme", "ouis": ["58:47:ca"]}],
        })
        acme = next(d for d in batch.devices if d.details.get("vendor") == "Acme")
        self.assertEqual((acme.remote_id, acme.name, acme.category),
                         ("site-1:lldp:58:47:ca:00:00:09", "Acme 00:00:09", "other_physical"))
        self.assertTrue(any(l.evidence == "lldp" and acme.remote_id in (l.a_device_remote_id, l.b_device_remote_id)
                            for l in batch.links))

    def test_invalid_vendor_setting_fails_the_poll(self) -> None:
        """Report a broken lldp_vendors setting instead of ignoring it."""
        with self.assertRaises(ValueError):
            UniFiAdapter(unifi_fixture(CLASSIC_DEVICES)).poll(
                {"url": BASE, "api_key": "fake", "lldp_vendors": [{"vendor": "Acme"}]})

    def test_connectors_come_from_classic_media(self) -> None:
        """A model outside the packaged catalog gets connectors from classic media."""
        batch = self.poll(CLASSIC_DEVICES)
        connectors = {(p.device_remote_id, p.remote_id): p.connector_type for p in batch.ports}
        self.assertEqual(connectors[("site-1:data", "1")], "rj45")
        self.assertEqual(connectors[("site-1:data", "12")], "sfp_plus")

    def test_catalog_combo_ports_outrank_classic_media(self) -> None:
        """A known model's combo port stays combo though media names its active side."""
        from labrat.collector.adapters.unifi import _connector_type
        self.assertEqual(_connector_type({"idx": 9}, "USW Flex 2.5G 8 PoE", {"media": "SFP+"}), "combo")
        self.assertEqual(_connector_type({"idx": 9}, "Unknown Model", {"media": "SFP+"}), "sfp_plus")
        self.assertIsNone(_connector_type({"idx": 9}, None, {}))

    def test_access_point_uplink_port_comes_from_the_uplink_record(self) -> None:
        """Give a portless AP its uplink port, so the cable can be linked."""
        client = unifi_fixture({"data": CLASSIC_DEVICES["data"] + [{
            "mac": "aa:aa:aa:00:00:03", "name": "Bedroom",
            "uplink": {"type": "wire", "port_idx": 1, "uplink_mac": "aa:aa:aa:00:00:02",
                       "uplink_remote_port": 1, "speed": 2500},
        }]})
        client.responses[f"{INTEGRATION}/sites/site-1/devices?offset=0&limit=100"]["data"].append(
            {"id": "ap", "model": "U7PRO", "features": ["accessPoint"], "macAddress": "aa:aa:aa:00:00:03"})
        client.responses[f"{INTEGRATION}/sites/site-1/devices?offset=0&limit=100"]["totalCount"] = 3
        client.responses[f"{INTEGRATION}/sites/site-1/devices/ap"] = {
            "id": "ap", "name": "Bedroom", "features": {"accessPoint": {}}}
        batch = UniFiAdapter(client).poll({"url": BASE, "api_key": "fake"})
        port = next(p for p in batch.ports if p.device_remote_id == "site-1:ap")
        self.assertEqual((port.remote_id, port.name, port.speed_mbps, port.medium), ("1", "Port 1", 2500, "copper"))
        self.assertTrue(any(l.evidence == "unifi-uplink" and "site-1:ap" in (l.a_device_remote_id, l.b_device_remote_id)
                            for l in batch.links))

    def ap_client(self, state: str, upstream_state: str | None):
        """The AP fixture with a reported state and its switch port's state.

        Args:
            state: The AP's reported state, such as ``OFFLINE``.
            upstream_state: Its switch port's ``up`` flag as text, or None.

        Returns:
            FakeJsonClient: The fixture with the AP added.
        """
        client = unifi_fixture({"data": CLASSIC_DEVICES["data"] + [{
            "mac": "aa:aa:aa:00:00:03", "name": "Retired AP",
            "uplink": {"type": "wire", "port_idx": 1, "uplink_mac": "aa:aa:aa:00:00:02",
                       "uplink_remote_port": 1, "speed": 2500},
        }]})
        listing = client.responses[f"{INTEGRATION}/sites/site-1/devices?offset=0&limit=100"]
        listing["data"].append({"id": "ap", "model": "U7PRO", "features": ["accessPoint"],
                                "macAddress": "aa:aa:aa:00:00:03", "state": state})
        listing["totalCount"] = 3
        client.responses[f"{INTEGRATION}/sites/site-1/devices/ap"] = {
            "id": "ap", "name": "Retired AP", "features": {"accessPoint": {}}}
        if upstream_state is not None:
            detail = client.responses[f"{INTEGRATION}/sites/site-1/devices/data"]
            detail["interfaces"]["ports"][0]["state"] = upstream_state
        return client

    def test_offline_device_uplink_to_a_down_port_is_not_a_cable(self) -> None:
        """Skip a disconnected device's remembered uplink that ends on a down port."""
        batch = UniFiAdapter(self.ap_client("OFFLINE", "DOWN")).poll({"url": BASE, "api_key": "fake"})
        self.assertFalse(any("site-1:ap" in (l.a_device_remote_id, l.b_device_remote_id) for l in batch.links))
        self.assertFalse(any(p.device_remote_id == "site-1:ap" for p in batch.ports))
        switch_port = next(p for p in batch.ports if (p.device_remote_id, p.remote_id) == ("site-1:data", "1"))
        self.assertIs(switch_port.link_up, False, "the switch port itself is still reported")

    def test_offline_device_uplink_keeps_its_cable_but_not_an_up_port(self) -> None:
        """Keep the cable while the far port is not reported down, but not as up."""
        batch = UniFiAdapter(self.ap_client("OFFLINE", None)).poll({"url": BASE, "api_key": "fake"})
        port = next(p for p in batch.ports if p.device_remote_id == "site-1:ap")
        self.assertEqual((port.link_up, port.speed_mbps), (None, None))
        self.assertTrue(any(l.evidence == "unifi-uplink" and "site-1:ap" in (l.a_device_remote_id, l.b_device_remote_id)
                            for l in batch.links))

    def test_online_device_uplink_port_is_up(self) -> None:
        """An online device's uplink port is up at the reported speed."""
        batch = UniFiAdapter(self.ap_client("ONLINE", "UP")).poll({"url": BASE, "api_key": "fake"})
        port = next(p for p in batch.ports if p.device_remote_id == "site-1:ap")
        self.assertEqual((port.link_up, port.speed_mbps), (True, 2500))

    def test_classic_list_failure_keeps_integration_data(self) -> None:
        """Collect normally, without media or neighbors, if the classic list fails."""
        batch = self.poll(ValueError("not JSON"))
        self.assertEqual(len(batch.devices), 2)
        self.assertTrue(all(p.medium is None for p in batch.ports))
        self.assertEqual(batch.links, ())


def mndp_packet(**fields) -> bytes:
    """Encode an MNDP packet from named fields, as RouterOS would send it.

    Args:
        **fields: TLV values by name: ``mac``, ``identity``, ``platform`` ...

    Returns:
        bytes: The packet.
    """
    codes = {"mac": 1, "identity": 5, "version": 7, "platform": 8, "uptime": 10,
             "software_id": 11, "board": 12, "interface": 16}
    body = b""
    for name, value in fields.items():
        if name == "mac":
            raw = bytes(int(part, 16) for part in value.split(":"))
        elif name == "uptime":
            raw = struct.pack("<I", value)
        else:
            raw = value.encode()
        body += struct.pack("!HH", codes[name], len(raw)) + raw
    return b"\x00\x00\x00\x01" + body


class MndpTests(unittest.TestCase):
    """Decode MikroTik announcements and group them into devices."""

    def test_decode_packet(self) -> None:
        """Read identity, board, version, uptime, interface, and MAC."""
        fields = decode_packet(mndp_packet(
            mac="4c:5e:0c:11:22:33", identity="core-crs", version="7.16 (stable)",
            platform="MikroTik", uptime=7200, software_id="ABCD-1234", board="CRS326-24S+2Q+",
            interface="bridge/sfp-sfpplus4",
        ))
        self.assertEqual(fields["mac"], "4c:5e:0c:11:22:33")
        self.assertEqual(fields["identity"], "core-crs")
        self.assertEqual(fields["board"], "CRS326-24S+2Q+")
        self.assertEqual(fields["uptime_seconds"], 7200)
        self.assertEqual(fields["interface"], "bridge/sfp-sfpplus4")

    def test_truncated_packet_stops_cleanly(self) -> None:
        """Ignore a record whose length runs past the packet."""
        packet = mndp_packet(identity="x") + struct.pack("!HH", 12, 50) + b"short"
        self.assertEqual(decode_packet(packet), {"identity": "x"})

    def test_poll_groups_interfaces_by_device(self) -> None:
        """One device per software ID, one port per announcing interface."""
        packets = [
            mndp_packet(identity="core-crs", software_id="ABCD-1234", board="CRS326-24S+2Q+",
                        interface="sfp-sfpplus4", mac="4c:5e:0c:11:22:33"),
            mndp_packet(identity="core-crs", software_id="ABCD-1234", board="CRS326-24S+2Q+",
                        interface="sfp-sfpplus1", mac="4c:5e:0c:11:22:34"),
            mndp_packet(identity="hallway-cap", software_id="EEEE-0001", board="cAP ax",
                        interface="ether1"),
            mndp_packet(board="no identity: ignored"),
        ]
        heard = []
        batch = MndpAdapter(listen=lambda seconds: heard.append(seconds) or packets).poll({"listen_seconds": 10})
        self.assertEqual(heard, [10])
        devices = {d.name: d for d in batch.devices}
        self.assertEqual(set(devices), {"core-crs", "hallway-cap"})
        self.assertEqual(devices["core-crs"].category, "switch")
        self.assertEqual(devices["hallway-cap"].category, "access_point")
        self.assertFalse(devices["core-crs"].details["ports_complete"])
        ports = sorted(p.name for p in batch.ports if p.device_remote_id == devices["core-crs"].remote_id)
        self.assertEqual(ports, ["sfp-sfpplus1", "sfp-sfpplus4"])

    def test_listen_seconds_is_bounded(self) -> None:
        """Reject a listening window outside 5 to 300 seconds."""
        for bad in (0, 4, 301, "60", 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                MndpAdapter(listen=lambda seconds: []).poll({"listen_seconds": bad})

    def test_board_categories(self) -> None:
        """Map RouterBOARD names to switch, access point, or router."""
        self.assertEqual(category_for_board("CSS610-8G-2S+"), "switch")
        self.assertEqual(category_for_board("RB260GS"), "switch")
        self.assertEqual(category_for_board("RB250GS"), "switch")
        self.assertEqual(category_for_board("wAP ac"), "access_point")
        self.assertEqual(category_for_board("hEX S"), "router")
        self.assertEqual(category_for_board(None), "router")


class StoreTests(unittest.TestCase):
    """Persist media and links from a UniFi batch into a temporary database."""

    def setUp(self) -> None:
        """Use a throwaway SQLite database upgraded to the latest revision."""
        temporary = TemporaryDirectory(prefix="labrat-step5-")
        self.addCleanup(temporary.cleanup)
        config_path = Path(temporary.name) / "local_config.toml"
        config_path.write_text(
            f"[database]\npath = {json.dumps(str(Path(temporary.name) / 'db.sqlite3'))}\n",
            encoding="utf-8",
        )
        environment = patch.dict(os.environ, {"LABRAT_CONFIG": str(config_path)})
        environment.start()
        self.addCleanup(environment.stop)
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
        engine = make_engine(load_local_config())
        self.addCleanup(engine.dispose)
        self.sessions = make_session_factory(engine)
        with self.sessions.begin() as db:
            db.add(DiscoverySource(name="unifi:default", kind="unifi", config_name="default"))

    def apply(self, batch: DiscoveryBatch) -> None:
        """Apply a batch for the UniFi source in one transaction.

        Args:
            batch: The batch to store.
        """
        with self.sessions.begin() as db:
            apply_batch(db, db.scalar(select(DiscoverySource)), batch)

    def test_media_neighbors_and_links_are_saved(self) -> None:
        """Store each port's medium, the MikroTik neighbor, and both links."""
        self.apply(UniFiAdapter(unifi_fixture(CLASSIC_DEVICES)).poll({"url": BASE, "api_key": "fake"}))
        with self.sessions() as db:
            media = {port.name: port.medium for port in db.scalars(select(DiscoveredPort))}
            self.assertEqual(media["sfp-sfpplus4"], "fiber_multimode")
            evidence = sorted(link.evidence for link in db.scalars(select(DiscoveredLink)))
            self.assertEqual(evidence, ["lldp", "unifi-uplink"])

    def test_invalid_medium_is_rejected(self) -> None:
        """Refuse a cable medium outside the known set."""
        batch = DiscoveryBatch(
            devices=(DeviceObservation(remote_id="d", name="d", category="switch"),),
            ports=(PortObservation(device_remote_id="d", remote_id="1", name="1", medium="wifi"),),
        )
        with self.assertRaises(ValueError):
            self.apply(batch)


if __name__ == "__main__":
    unittest.main()
