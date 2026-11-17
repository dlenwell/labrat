# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Infer a wired NUT server's switch port from UniFi client records."""

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import requests
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import labrat.models  # noqa: F401  Register canonical inventory references.
from labrat.audit import AuditEvent
from labrat.collector.contracts import DiscoveryBatch
from labrat.collector.nut_wired import EVIDENCE, PORT_NAME, PORT_REMOTE_ID, collect_nut_wired, reconcile_nut_wired
from labrat.collector.store import apply_batch
from labrat.database import Base
from labrat.discovery.models import DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource

NOW = 1_800_000_000.0
SERVER_MAC = "02:00:00:00:00:44"
SWITCH_MAC = "02:00:00:00:00:99"


class FakeUniFi:
    """Answer the Integration client list and classic per-MAC lookups offline."""

    def __init__(self, clients, stations=None, error=None):
        """Script a controller's client list and classic station records.

        Args:
            clients: Integration API client records, or None to fail the list.
            stations: Classic records by MAC.
            error: An exception the classic lookup raises, if any.
        """
        self.clients = clients
        self.stations = stations or {}
        self.client = Mock()
        self.client.get_json.side_effect = self.station
        self.error = error

    def _page(self, base, path, headers, verify):
        """Supply one site and its current clients; fail if requested.

        Args:
            base: The controller root (unused).
            path: The API path requested.
            headers: Request headers (unused).
            verify: TLS verification (unused).

        Returns:
            list: The sites, or the client list.

        Raises:
            ValueError: When the client list is set to fail (clients is None).
        """
        if path == "sites":
            return [{"id": "example-site", "internalReference": "default"}]
        if self.clients is None:
            raise ValueError("Client endpoint unavailable")
        return self.clients

    def station(self, url, headers, verify):
        """Return the classic record for the MAC at the end of the URL.

        Args:
            url: The classic stat/user URL, ending in the MAC.
            headers: Request headers (unused).
            verify: TLS verification (unused).

        Returns:
            dict: The classic API envelope for that MAC.

        Raises:
            Exception: The scripted ``error``, when one is set.
        """
        if self.error is not None:
            raise self.error
        mac = url.rsplit("/", 1)[-1].replace("%3A", ":").lower()
        return {"meta": {"rc": "ok"}, "data": self.stations.get(mac, [])}


class NutWiredTests(unittest.TestCase):
    """Exercise matching, the inferred NIC and cable, and their removal."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.nut = DiscoverySource(name="Example NUT", kind="nut", config_name="nut")
        self.unifi = DiscoverySource(name="Example UniFi", kind="unifi", config_name="unifi")
        self.db.add_all((self.nut, self.unifi))
        self.db.flush()
        self.server = DiscoveredDevice(
            source_id=self.nut.id, remote_id="nut-server:192.0.2.4:3493",
            name="Example NUT server", category="server", visibility="recognized",
            identifiers_json=json.dumps({"management_ips": ["192.0.2.4"]}),
            details_json=json.dumps({"role": "nut_server"}),
        )
        self.switch = DiscoveredDevice(
            source_id=self.unifi.id, remote_id="example-site:switch-id",
            name="Example switch", category="switch", visibility="recognized",
            identifiers_json=json.dumps({"mac": SWITCH_MAC}),
            details_json=json.dumps({"site_id": "example-site"}),
        )
        self.db.add_all((self.server, self.switch))
        self.db.flush()
        self.ports = {}
        for index in (2, 3):
            port = DiscoveredPort(discovered_device_id=self.switch.id, remote_id=str(index),
                                  name=f"Port {index}", link_up=True, speed_mbps=100,
                                  connector_type="rj45", medium="copper")
            self.db.add(port)
            self.ports[index] = port
        self.db.flush()
        self.settings = {"url": "https://controller.example", "api_key": "test-only"}

    def client(self, **changes):
        """Build an Integration API client record for the NUT server.

        Args:
            **changes: Fields to override.

        Returns:
            dict: The client record.
        """
        return {"type": "WIRED", "ipAddress": "192.0.2.4", "macAddress": SERVER_MAC, **changes}

    def station(self, **changes):
        """Build a fresh classic wired client record on switch port 2.

        Args:
            **changes: Fields to override.

        Returns:
            dict: The classic record.
        """
        return {"mac": SERVER_MAC, "is_wired": True, "last_seen": NOW - 5,
                "sw_mac": SWITCH_MAC, "sw_port": 2, **changes}

    def collect(self, clients, station=None, **kwargs):
        """Run the lookup against the fake controller.

        Args:
            clients: The client list the fake controller returns.
            station: The classic record for the server; a fresh wired one by default.
            **kwargs: Passed to FakeUniFi (``error``).

        Returns:
            The collection's result.
        """
        stations = {SERVER_MAC: [station if station is not None else self.station()]}
        return collect_nut_wired(self.db, self.unifi, FakeUniFi(clients, stations, **kwargs),
                                 self.settings, now=NOW)

    def inferred(self):
        """Return the inferred NIC and its cable, if present.

        Returns:
            tuple: The inferred port and link, each None when absent.
        """
        port = self.db.scalar(select(DiscoveredPort).where(
            DiscoveredPort.discovered_device_id == self.server.id,
            DiscoveredPort.remote_id == PORT_REMOTE_ID))
        link = self.db.scalar(select(DiscoveredLink).where(DiscoveredLink.evidence == EVIDENCE))
        return port, link

    def audits(self):
        """Count collector audit events so far.

        Returns:
            int: The number of audit events.
        """
        return self.db.scalar(select(func.count(AuditEvent.id)))

    def test_wired_client_gets_an_inferred_nic_and_cable(self):
        """Create one audited NIC and cable; an unchanged repeat writes no audit."""
        found = self.collect([self.client()])
        self.assertEqual(found, {self.server.id: [{
            "switch_port_id": self.ports[2].id, "mac": SERVER_MAC, "source_id": self.unifi.id}]})
        self.assertEqual(reconcile_nut_wired(self.db, found), 1)
        port, link = self.inferred()
        self.assertEqual((port.name, port.mac, port.speed_mbps, port.medium, port.link_up),
                         (PORT_NAME, SERVER_MAC, 100, "copper", True))
        self.assertEqual((link.confidence, link.source_id), ("inferred", self.unifi.id))
        self.assertEqual({link.a_port_id, link.b_port_id}, {port.id, self.ports[2].id})
        events = self.audits()
        self.assertEqual(events, 2)
        reconcile_nut_wired(self.db, found)
        self.assertEqual(self.audits(), events)
        # A UniFi snapshot does not delete the cable; collect/reconcile own it.
        apply_batch(self.db, self.unifi, DiscoveryBatch())
        self.assertIsNotNone(self.inferred()[1])

    def test_only_fresh_wired_unambiguous_known_ports_count(self):
        """Skip Wi-Fi, stale, unwired, unknown, down, and conflicting records."""
        cases = [
            ([self.client(type="WIRELESS")], None),
            ([self.client(ipAddress="192.0.2.9")], None),
            ([self.client()], self.station(last_seen=NOW - 3600)),
            ([self.client()], self.station(is_wired=False)),
            ([self.client()], self.station(sw_mac="02:00:00:00:00:98")),
            ([self.client()], self.station(sw_port=7)),
            ([self.client()], self.station(sw_port=2, uplink={"sw_port": 3})),
            ([self.client(macAddress="not-a-mac")], None),
        ]
        for clients, station in cases:
            with self.subTest(clients=clients, station=station):
                self.assertEqual(self.collect(clients, station), {})
        self.ports[2].link_up = False
        self.assertEqual(self.collect([self.client()]), {})

    def test_failures_keep_evidence_and_unknown_station_is_empty(self):
        """A failed read returns None; UniFi's unknown-station answer is empty."""
        self.assertIsNone(self.collect(None))
        broken = requests.HTTPError(response=Mock(status_code=500))
        self.assertIsNone(self.collect([self.client()], error=broken))
        unknown = Mock(status_code=400)
        unknown.json.return_value = {"meta": {"msg": "api.err.UnknownStation"}}
        self.assertEqual(self.collect([self.client()], error=requests.HTTPError(response=unknown)), {})

    def test_absence_ambiguity_moves_and_stronger_evidence(self):
        """Remove on absence or ambiguity, follow a move, and yield to other links."""
        found = self.collect([self.client()])
        reconcile_nut_wired(self.db, found)
        record = found[self.server.id][0]
        moved = {self.server.id: [{**record, "switch_port_id": self.ports[3].id}]}
        reconcile_nut_wired(self.db, moved)
        port, link = self.inferred()
        self.assertEqual({link.a_port_id, link.b_port_id}, {port.id, self.ports[3].id})
        reconcile_nut_wired(self.db, {self.server.id: [record, record]})
        self.assertEqual(self.inferred(), (None, None))
        reconcile_nut_wired(self.db, found)
        reconcile_nut_wired(self.db, {})
        self.assertEqual(self.inferred(), (None, None))
        # An existing (stronger) cable on the switch port wins.
        other = DiscoveredPort(discovered_device_id=self.switch.id, remote_id="9", name="Port 9")
        self.db.add(other)
        self.db.flush()
        self.db.add(DiscoveredLink(source_id=self.unifi.id, a_port_id=self.ports[2].id,
                                   b_port_id=other.id, evidence="lldp", confidence="confirmed",
                                   last_seen_at=datetime.now(timezone.utc)))
        self.db.flush()
        self.assertEqual(reconcile_nut_wired(self.db, found), 0)
        self.assertEqual(self.inferred(), (None, None))


if __name__ == "__main__":
    unittest.main()
