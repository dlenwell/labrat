# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Test Step 4B physical Proxmox and UniFi discovery without network access.

Fixtures represent deliberately small subsets of documented API responses.
Each adapter uses a fake HTTP client, allowing tests to verify request paths,
authentication headers, safe link evidence and database idempotence without
requiring live infrastructure or putting credentials in source control.
"""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from labrat.audit import AuditEvent
from labrat.collector.adapters.proxmox import ProxmoxAdapter
from labrat.collector.adapters.unifi import UniFiAdapter
from labrat.collector.http import JsonGetClient, controller_url, tls_verification
from labrat.collector.runner import poll_once
from labrat.collector.sources import configured_sources, sync_configured_sources
from labrat.config import load_local_config
from labrat.database import make_engine, make_session_factory
from labrat.discovery.models import DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource


class FakeJsonClient:
    """Return canned responses and record GET-only requests for inspection."""

    def __init__(self, responses: dict[str, object]) -> None:
        """Prepare a fixed URL-to-payload lookup table.

        Args:
            responses: Expected full URLs mapped to decoded JSON values.
        """
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str], bool | str]] = []

    def get_json(self, url: str, headers: dict, verify: bool | str) -> object:
        """Return one fixture and assert that the adapter used a known route.

        Args:
            url: Complete URL requested by the adapter.
            headers: Authorization headers passed to the HTTP layer.
            verify: TLS verification selector.

        Returns:
            object: Decoded JSON fixture for the requested endpoint.

        Raises:
            AssertionError: For a route the fixture does not know.
        """
        self.calls.append((url, dict(headers), verify))
        if url not in self.responses:
            raise AssertionError(f"Unexpected API path: {url.split('?', 1)[0]}")
        return self.responses[url]


class FakeResponse:
    """Mimic a minimal successful response from a requests HTTP session."""

    def raise_for_status(self) -> None:
        """Simulate HTTP 200 with no raised exception."""

    def json(self) -> dict:
        """Return a harmless fixture payload.

        Returns:
            dict: An empty data envelope.
        """
        return {"data": []}


class FakeSession:
    """Capture HTTP transport options to ensure tokens stay in headers."""

    def __init__(self) -> None:
        """Initialize an empty list of intercepted HTTP requests."""
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        """Record a GET operation and return an empty JSON envelope.

        Args:
            url: Requested endpoint.
            **kwargs: HTTP headers, TLS options, redirect policy and timeout.

        Returns:
            FakeResponse: A successful synthetic response.
        """
        self.calls.append((url, kwargs))
        return FakeResponse()


class Step4BTests(unittest.TestCase):
    """Validate real adapter behavior and source registration boundaries."""

    def setUp(self) -> None:
        """Prepare a source-free SQLite database that cannot touch user data."""
        temporary = TemporaryDirectory(prefix="labrat-step4b-")
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "test.sqlite3"
        config_path = Path(temporary.name) / "local_config.toml"
        config_path.write_text(
            f"[database]\npath = {json.dumps(str(self.db_path))}\n",
            encoding="utf-8",
        )
        environment = patch.dict(os.environ, {"LABRAT_CONFIG": str(config_path)})
        environment.start()
        self.addCleanup(environment.stop)

    def _sessions(self):
        """Upgrade schema and return a session factory with engine cleanup.

        Returns:
            sessionmaker: Sessions on the migrated test database.
        """
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(config, "head")
        engine = make_engine(load_local_config())
        self.addCleanup(engine.dispose)
        return make_session_factory(engine)

    def test_proxmox_physical_nodes_and_nics_only(self) -> None:
        """Ignore guest workloads and logical network interfaces."""
        base = "https://pve.example:8006/api2/json"
        client = FakeJsonClient({
            f"{base}/cluster/status": {"data": []},
            f"{base}/nodes": {"data": [
                {"node": "one", "status": "online"},
                {"node": "two", "status": "offline"},
            ]},
            f"{base}/nodes/one/network": {"data": [
                {"iface": "enp3s0f0np0", "type": "eth", "hwaddress": "AA:BB:CC:DD:EE:FF"},
                {"iface": "enp3s0f1np1", "type": "eth"},
                {"iface": "bond0", "type": "bond"},
                {"iface": "vmbr0", "type": "bridge"},
                {"iface": "tap101i0", "type": "eth", "virtual": True},
            ]},
            f"{base}/nodes/one/report": {"data": (
                "3: enp3s0f1np1: <BROADCAST,MULTICAST,UP> mtu 1500\n"
                "    link/ether 11:22:33:44:55:66 brd ff:ff:ff:ff:ff:ff\n"
            )},
        })
        batch = ProxmoxAdapter(client).poll({
            "url": "https://pve.example:8006", "token_id": "labrat@pve!ro",
            "token_secret": "TEST_SECRET_NOT_REAL",
        })
        self.assertEqual([d.remote_id for d in batch.devices], ["one", "two"])
        self.assertEqual([p.name for p in batch.ports], [
            "enp3s0f0np0", "enp3s0f1np1",
        ])
        self.assertEqual(batch.ports[0].mac, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(batch.ports[1].mac, "11:22:33:44:55:66")  # From the report.
        self.assertEqual(batch.ports[0].speed_mbps, None)
        self.assertFalse(batch.links)
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(all("TEST_SECRET_NOT_REAL" not in u for u, _, _ in client.calls))
        self.assertTrue(all(c["Authorization"].startswith("PVEAPIToken=") for _, c, _ in client.calls))

    def test_proxmox_rejects_invalid_response(self) -> None:
        """Reject malformed API data rather than treating it as an empty scan."""
        base = "https://pve.example:8006/api2/json"
        with self.assertRaises(ValueError):
            ProxmoxAdapter(FakeJsonClient({f"{base}/cluster/status": {"data": []}, f"{base}/nodes": {"oops": []}})).poll({
                "url": "https://pve.example:8006", "token_id": "a@pve!b",
                "token_secret": "secret",
            })

    def test_unifi_adopted_appliances_and_explicit_neighbors(self) -> None:
        """Store adopted routers/switches, ports and only direct LLDP evidence."""
        base = "https://unifi.example/proxy/network/integration/v1"
        switch = "a"
        router = "b"
        client = FakeJsonClient({
            f"{base}/sites?offset=0&limit=100": {
                "data": [{"id": "home", "name": "Home"}], "totalCount": 1,
            },
            f"{base}/sites/home/devices?offset=0&limit=100": {
                "data": [
                    {"id": switch, "type": "SWITCH"},
                    {"id": router, "type": "GATEWAY"},
                    {"id": "c", "type": "CLIENT"},
                ], "totalCount": 3,
            },
            f"{base}/sites/home/devices/a": {
                "id": switch, "name": "Aggregation", "macAddress": "AA:AA:AA:AA:AA:AA",
                "interfaces": {"ports": [
                    {"portIdx": 1, "name": "Port 1", "speedMbps": 10000,
                     "lldpNeighbor": {"deviceId": "b", "portIdx": 9}},
                    {"portIdx": 2, "name": "Port 2", "clientMacAddress": "AA:FA:AA:AA:AA:AA"},
                ]},
            },
            f"{base}/sites/home/devices/b": {
                "id": router, "name": "UDM", "interfaces": {"ports": [
                    {"portIdx": 9, "name": "Port 9", "speedMbps": 1000},
                ]},
            },
            f"{base}/sites/home/devices/c": {"id": "c", "name": "Laptop"},
        })
        batch = UniFiAdapter(client).poll({
            "url": "https://unifi.example", "api_key": "TEST_API_KEY_NOT_REAL",
        })
        self.assertEqual(len(batch.devices), 2)
        self.assertEqual({d.category for d in batch.devices}, {"switch", "router"})
        self.assertEqual(len(batch.ports), 3)
        self.assertEqual(batch.ports[0].speed_mbps, 10000)
        self.assertEqual(len(batch.links), 1)
        self.assertEqual(batch.links[0].confidence, "confirmed")
        self.assertEqual(batch.links[0].evidence, "lldp")
        self.assertTrue(all("TEST_API_KEY_NOT_REAL" not in u for u, _, _ in client.calls))
        self.assertTrue(all(c["X-API-Key"] == "TEST_API_KEY_NOT_REAL" for _, c, _ in client.calls))

    def test_unifi_official_feature_map_without_type(self) -> None:
        """Classify modern Network API details that omit the legacy type field."""
        base = "https://unifi.example/proxy/network/integration/v1"
        client = FakeJsonClient({
            f"{base}/sites?offset=0&limit=100": {
                "data": [{"id": "home"}], "totalCount": 1,
            },
            f"{base}/sites/home/devices?offset=0&limit=100": {
                "data": [{"id": "sw", "model": "USW-Aggregation", "features": ["switching"]},
                         {"id": "gw", "model": "UDMPROSE", "features": ["switching"]},
                         {"id": "ap", "model": "U7PRO", "features": ["switching"]}],
                "totalCount": 3,
            },
            f"{base}/sites/home/devices/sw": {
                "id": "sw", "model": "USW-Aggregation",
                "features": {"switching": {"lags": []}},
                "interfaces": {"ports": [{"idx": 4, "speedMbps": 10000, "maxSpeedMbps": 10000}]},
            },
            f"{base}/sites/home/devices/gw": {
                "id": "gw", "model": "UDMPROSE", "features": {"switching": {}},
            },
            f"{base}/sites/home/devices/ap": {
                "id": "ap", "model": "U7PRO", "features": {"accessPoint": {}},
            },
        })
        batch = UniFiAdapter(client).poll({"url": "https://unifi.example", "api_key": "fake"})
        self.assertEqual([device.category for device in batch.devices], [
            "switch", "router", "access_point",
        ])
        self.assertEqual(batch.ports[0].remote_id, "4")
        self.assertEqual(batch.ports[0].speed_mbps, 10000)

    def test_unifi_pagination(self) -> None:
        """Handle multi-page sites without silently ignoring later sites."""
        base = "https://unifi.example/proxy/network/integration/v1"
        client = FakeJsonClient({
            f"{base}/sites?offset=0&limit=100": {
                "data": [{"id": "first"}], "totalCount": 2,
            },
            f"{base}/sites?offset=1&limit=100": {
                "data": [{"id": "second"}], "totalCount": 2,
            },
            f"{base}/sites/first/devices?offset=0&limit=100": {"data": [], "totalCount": 0},
            f"{base}/sites/second/devices?offset=0&limit=100": {"data": [], "totalCount": 0},
        })
        self.assertEqual(len(UniFiAdapter(client).poll({
            "url": "https://unifi.example", "api_key": "fake",
        }).devices), 0)
        self.assertEqual(len(client.calls), 4)

    def test_unifi_no_inferred_client_cables(self) -> None:
        """Ignore client MAC tables and unsupported neighbor descriptions."""
        base = "https://unifi.example/proxy/network/integration/v1"
        client = FakeJsonClient({
            f"{base}/sites?offset=0&limit=100": {"data": [{"id": "home"}], "totalCount": 1},
            f"{base}/sites/home/devices?offset=0&limit=100": {
                "data": [{"id": "a", "type": "SWITCH"}], "totalCount": 1,
            },
            f"{base}/sites/home/devices/a": {
                "id": "a", "interfaces": {"ports": [{
                    "portIdx": 1, "clientMacAddress": "00:01:02:03:04:05",
                    "lldpNeighbor": {"macAddress": "00:01:02:03:04:05"},
                }]},
            },
        })
        batch = UniFiAdapter(client).poll({"url": "https://unifi.example", "api_key": "fake"})
        self.assertEqual(len(batch.devices), 1)
        self.assertEqual(len(batch.ports), 1)
        self.assertEqual(len(batch.links), 0)

    def test_local_config_sources_are_idempotent_and_secret_free(self) -> None:
        """Discover pre-existing credential sections with no migration or leak."""
        sessions = self._sessions()
        config = {
            "unifi": {"url": "https://unifi.local", "api_key": "SENSITIVE_KEY"},
            "proxmox": {"url": "https://pve.local:8006", "token_id": "a@pve!ro", "token_secret": "SENSITIVE_SECRET"},
        }
        self.assertEqual(sync_configured_sources(sessions, config), 2)
        self.assertEqual(sync_configured_sources(sessions, config), 0)
        with sessions() as db:
            sources = db.scalars(select(DiscoverySource)).all()
            self.assertEqual({s.kind for s in sources}, {"proxmox", "unifi"})
            db.get(DiscoverySource, sources[0].id).enabled = False
            db.commit()
        self.assertEqual(sync_configured_sources(sessions, config), 0)
        with sessions() as db:
            self.assertEqual(sum(1 for source in db.scalars(select(DiscoverySource)) if not source.enabled), 1)
            evidence = " ".join([str(source.__dict__) for source in db.scalars(select(DiscoverySource))])
            evidence += " ".join(event.details_json for event in db.scalars(select(AuditEvent)))
            self.assertNotIn("SENSITIVE_KEY", evidence)
            self.assertNotIn("SENSITIVE_SECRET", evidence)

    def test_named_sources_and_duplicate_alias_are_rejected(self) -> None:
        """Support multiple controllers while rejecting ambiguous default aliases."""
        resolved = configured_sources({"integrations": {
            "unifi": {"house": {"url": "https://one"}, "shed": {"url": "https://two"}},
        }})
        self.assertEqual(set(resolved["unifi"]), {"house", "shed"})
        with self.assertRaises(ValueError):
            configured_sources({"unifi": {}, "integrations": {"unifi": {"default": {}}}})

    def test_transport_disables_redirects_and_verifies_tls(self) -> None:
        """Keep API tokens out of URLs and don't forward them on redirects."""
        session = FakeSession()
        JsonGetClient(session).get_json(
            "https://unifi.example/api", {"X-API-Key": "fake"}, True,
        )
        url, kwargs = session.calls[0]
        self.assertNotIn("fake", url)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["verify"], True)
        self.assertEqual(kwargs["timeout"], (3.0, 15.0))
        self.assertEqual(tls_verification({}), True)
        self.assertEqual(tls_verification({"ca_bundle": "/tmp/ca.pem"}), "/tmp/ca.pem")
        with self.assertRaises(ValueError):
            controller_url({"url": "http://unifi.local"})
        with self.assertRaises(ValueError):
            controller_url({"url": "https://u:p@unifi.local"})

    def test_looping_collector_records_its_interval(self) -> None:
        """Record the loop interval per source; a one-off run leaves it alone."""
        sessions = self._sessions()
        config = {
            "proxmox": {"url": "https://pve.local:8006", "token_id": "a@pve!ro", "token_secret": "not-real"},
        }
        sync_configured_sources(sessions, config)
        base = "https://pve.local:8006/api2/json"
        adapter = ProxmoxAdapter(FakeJsonClient({
            f"{base}/cluster/status": {"data": []},
            f"{base}/nodes": {"data": []},
        }))
        poll_once(sessions, config, {"proxmox": adapter})
        with sessions() as db:
            self.assertIsNone(db.scalar(select(DiscoverySource)).poll_interval_seconds)
        poll_once(sessions, config, {"proxmox": adapter}, 300)
        poll_once(sessions, config, {"proxmox": adapter})
        with sessions() as db:
            self.assertEqual(db.scalar(select(DiscoverySource)).poll_interval_seconds, 300)

    def test_poll_populates_database_without_changing_inventory(self) -> None:
        """Finish a mocked cycle using the existing persistence and audit layer."""
        sessions = self._sessions()
        config = {
            "proxmox": {"url": "https://pve.local:8006", "token_id": "a@pve!ro", "token_secret": "not-real"},
        }
        self.assertEqual(sync_configured_sources(sessions, config), 1)
        base = "https://pve.local:8006/api2/json"
        fixture = {
            f"{base}/cluster/status": {"data": []},
            f"{base}/nodes": {"data": [{"node": "one", "status": "online"}]},
            f"{base}/nodes/one/network": {"data": [{"type": "eth", "iface": "eno1"}]},
            f"{base}/nodes/one/report": {"data": ""},
        }
        adapter = ProxmoxAdapter(FakeJsonClient(fixture))
        self.assertEqual(poll_once(sessions, config, {"proxmox": adapter}), 1)
        self.assertEqual(poll_once(sessions, config, {"proxmox": adapter}), 1)
        with sessions() as db:
            self.assertEqual(len(db.scalars(select(DiscoveredDevice)).all()), 1)
            self.assertEqual(len(db.scalars(select(DiscoveredPort)).all()), 1)
            self.assertEqual(len(db.scalars(select(DiscoveredLink)).all()), 0)
            self.assertEqual(len(db.scalars(select(AuditEvent)).all()), 3)
            source = db.scalar(select(DiscoverySource))
            self.assertIsNotNone(source.last_success_at)
            self.assertIsNone(source.last_error)
