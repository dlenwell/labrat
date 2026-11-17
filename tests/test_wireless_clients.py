# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Keep inferred NUT Wi-Fi attachments distinct from physical links."""

import json
import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import labrat.models  # Register canonical inventory references.
from labrat.api.topology import _wireless_record
from labrat.collector.wireless_clients import collect_nut_wireless, reconcile_nut_wireless
from labrat.database import Base
from labrat.discovery.models import DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource


class FakeUniFi:
    """Return a small documented Integration API response without networking."""

    def __init__(self, clients):
        """Script the controller's client list.

        Args:
            clients: Integration API client records, or None to fail the list.
        """
        self.clients = clients

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
            return [{"id": "example-site"}]
        if self.clients is None:
            raise ValueError("Client endpoint unavailable")
        return self.clients


class WirelessClientTests(unittest.TestCase):
    """Exercise exact IP matching, AP attribution and telemetry persistence."""

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
            source_id=self.nut.id, remote_id="nut-server:192.0.2.2:3493",
            name="Example NUT server", category="server", visibility="recognized",
            identifiers_json=json.dumps({"management_ips": ["192.0.2.2"]}),
            details_json=json.dumps({"role": "nut_server"}),
        )
        self.ap = DiscoveredDevice(
            source_id=self.unifi.id, remote_id="example-site:ap-id",
            name="Example AP", category="access_point", visibility="recognized",
        )
        self.db.add_all((self.server, self.ap))
        self.db.flush()
        self.settings = {"url": "https://controller.example", "api_key": "test-only"}
        self.now = datetime.now(timezone.utc)

    def client(self, **changes):
        """Build a matching current wireless client with an adopted AP.

        Args:
            **changes: Fields to override.

        Returns:
            dict: The client record.
        """
        return {"type": "WIRELESS", "ipAddress": "192.0.2.2",
                "macAddress": "02:00:00:00:00:02", "uplinkDeviceId": "ap-id",
                "connectedAt": self.now.isoformat(), **changes}

    def test_exact_wireless_match_creates_only_telemetry(self):
        found = collect_nut_wireless(self.db, self.unifi, FakeUniFi([
            self.client(), self.client(ipAddress="192.0.2.3"),
            self.client(type="WIRED"),
        ]), self.settings, sampled_at=self.now)
        self.assertEqual(len(found[self.server.id]), 1)
        reconcile_nut_wireless(self.db, found, sampled_at=self.now)
        self.db.refresh(self.server)
        record = json.loads(self.server.details_json)["wireless"]
        self.assertEqual(record["ap_remote_id"], "example-site:ap-id")
        self.assertEqual(record["ip_address"], "192.0.2.2")
        self.assertEqual(_wireless_record(json.loads(self.server.details_json))["state"], "connected")
        self.assertEqual(self.db.scalar(select(DiscoveredPort).limit(1)), None)
        self.assertEqual(self.db.scalar(select(DiscoveredLink).limit(1)), None)

    def test_missing_client_and_failed_lookup_are_different(self):
        found = collect_nut_wireless(self.db, self.unifi, FakeUniFi([self.client()]), self.settings)
        reconcile_nut_wireless(self.db, found)
        before = self.server.details_json
        self.assertIsNone(collect_nut_wireless(self.db, self.unifi, FakeUniFi(None), self.settings))
        self.assertEqual(self.server.details_json, before)
        reconcile_nut_wireless(self.db, {})
        self.assertEqual(json.loads(self.server.details_json)["wireless"]["state"], "not_seen")

    def test_two_clients_with_same_address_are_ambiguous(self):
        found = collect_nut_wireless(self.db, self.unifi, FakeUniFi([
            self.client(), self.client(macAddress="02:00:00:00:00:03"),
        ]), self.settings)
        reconcile_nut_wireless(self.db, found)
        self.assertEqual(json.loads(self.server.details_json)["wireless"]["state"], "ambiguous")


if __name__ == "__main__":
    unittest.main()
