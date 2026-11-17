# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Verify cached topology, authenticated placement and observed link safety.

Every test uses a temporary SQLite database upgraded through Alembic. No test
connects to UniFi, Proxmox or any other external service.
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
from labrat.discovery.models import (
    DiscoveredDevice, DiscoveredLink, DiscoveredNetworkInterface, DiscoveredPort,
    DiscoverySource,
)
from labrat.models import Device, Rack, Room, Site, User
from labrat.placement import DevicePlacement, PlacementContainer


class Step4CTests(unittest.TestCase):
    """Test the physical discovery UI API and manually controlled inventory."""

    def setUp(self) -> None:
        """Create isolated storage, a running Flask client, and a local admin."""
        temporary = TemporaryDirectory(prefix="labrat-step4c-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config_path = root / "local_config.toml"
        config_path.write_text(
            "[database]\n"
            f"path = {json.dumps(str(root / 'db.sqlite3'))}\n"
            "[auth]\n"
            f"secret_key = {json.dumps('test-' + 'p' * 64)}\n",
            encoding="utf-8",
        )
        environment = patch.dict(os.environ, {"LABRAT_CONFIG": str(config_path)})
        environment.start()
        self.addCleanup(environment.stop)
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
        from labrat.app import create_app
        self.app = create_app()
        self.addCleanup(self.app.extensions["labrat_engine"].dispose)
        self.factory = self.app.extensions["labrat_session_factory"]
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as state:
            token = state["csrf_token"]
        response = self.client.post("/setup", data={
            "_csrf_token": token,
            "setup_code": self.app.extensions["labrat_setup_code_path"].read_text().strip(),
            "username": "admin", "password": "safe-test-password-2026",
            "confirm_password": "safe-test-password-2026",
        })
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as state:
            self.csrf = state["csrf_token"]

    def post(self, endpoint: str, payload: dict) -> object:
        """Post a CSRF-protected JSON request from the signed-in admin.

        Args:
            endpoint: API path under test.
            payload: Untrusted user submission to validate.

        Returns:
            object: Flask test response.
        """
        return self.client.post(endpoint, json=payload, headers={"X-CSRF-Token": self.csrf})

    def add_observations(self) -> tuple[int, int]:
        """Insert two recognized devices, ports, and one confirmed link.

        Returns:
            tuple[int, int]: Primary keys of the discovered physical devices.
        """
        with self.factory.begin() as db:
            source = DiscoverySource(name="unifi:default", kind="unifi", config_name="default")
            db.add(source)
            db.flush()
            first = DiscoveredDevice(
                source_id=source.id, remote_id="switch-a", name="Lab Switch", category="switch",
            )
            second = DiscoveredDevice(
                source_id=source.id, remote_id="switch-b", name="Other Switch", category="switch",
            )
            db.add_all([first, second])
            db.flush()
            a = DiscoveredPort(discovered_device_id=first.id, remote_id="1", name="Port 1")
            b = DiscoveredPort(discovered_device_id=second.id, remote_id="2", name="Port 2")
            db.add_all([a, b])
            db.flush()
            db.add(DiscoveredLink(
                source_id=source.id,
                a_port_id=min(a.id, b.id), b_port_id=max(a.id, b.id),
                confidence="confirmed", evidence="lldp",
            ))
            ids = (first.id, second.id)
        return ids

    def create_locations(self) -> tuple[int, int, int]:
        """Build an editable site, room and rack through the public API.

        Returns:
            tuple[int, int, int]: Created site, room and rack container IDs.
        """
        site = self.post("/api/v1/locations", {"kind": "site", "name": "Home"})
        self.assertEqual(site.status_code, 201)
        room = self.post("/api/v1/locations", {
            "kind": "room", "site_id": site.json["id"], "name": "Basement",
        })
        self.assertEqual(room.status_code, 201)
        rack = self.post("/api/v1/locations", {
            "kind": "rack", "room_id": room.json["id"], "name": "Main Rack", "height_u": 36,
        })
        self.assertEqual(rack.status_code, 201)
        locations = self.client.get("/api/v1/locations").json
        container = next(c for c in locations["containers"] if c["rack_id"] == rack.json["id"])
        return site.json["id"], room.json["id"], container["id"]

    def test_topology_requires_login_and_renders_template(self) -> None:
        """Require login for HTML and JSON discovery data."""
        self.assertEqual(self.client.get("/topology").status_code, 200)
        self.assertIn(b"topology-canvas", self.client.get("/topology").data)
        self.assertEqual(self.client.get("/static/js/topology.js").status_code, 200)
        self.assertEqual(self.client.get("/static/js/racks.js").status_code, 200)
        self.client.post("/signout", data={"_csrf_token": self.csrf})
        self.assertEqual(self.client.get("/topology").status_code, 302)
        self.assertEqual(self.client.get("/api/v1/topology").status_code, 401)

    def test_equipment_provides_room_wiring_without_requiring_a_rack(self) -> None:
        """Load the room diagram and its controls without inserting a dummy rack."""
        with self.factory.begin() as db:
            site = Site(name="Example location")
            db.add(site)
            db.flush()
            room = Room(name="Example room", site_id=site.id)
            db.add(room)
            db.flush()
            room_id = room.id
        response = self.client.get(f"/areas/{room_id}/wiring")
        self.assertEqual(response.status_code, 200)
        # The framed room heading names the room and shows the diagram summary.
        self.assertIn(b'<h1 id="room-title">Example room</h1>', response.data)
        self.assertIn(b'id="room-wiring-summary"', response.data)
        self.assertNotIn(b'room-wiring-places', response.data)
        self.assertIn(b'aria-label="Add equipment" title="Add equipment"', response.data)
        self.assertNotIn(b'id="room-wiring-reset"', response.data)
        # Rename or delete the area from its heading (owner): Edit area.
        self.assertIn(b'id="area-edit-tool"', response.data)
        self.assertIn(b'data-view="area-edit" id="area-edit-view"', response.data)
        self.assertIn(b'id="area-edit-delete"', response.data)
        self.assertIn(b'popovertarget="room-diagram-help"', response.data)
        self.assertLess(response.data.index(b'id="rack-empty-add"'), response.data.index(b'id="room-wiring"'))
        for marker in (b'id="room-wiring"', b'id="room-wiring-canvas"', b'id="room-diagram-help"',
                       b'id="room-wiring-style"', b'id="room-wiring-status"',
                       b'id="room-wiring-diagram"',
                       b'id="room-wiring-zoom-in"', b'id="room-wiring-zoom-out"', b'id="room-wiring-zoom-fit"',
                       b'js/topology-layout.js', b'js/room-wiring.js', b'css/room-wiring.css'):
            self.assertIn(marker, response.data)
        self.assertNotIn(b"No racks in this area yet", response.data)
        for asset in ("js/room-wiring.js", "css/room-wiring.css"):
            asset_response = self.client.get(f"/static/{asset}")
            self.assertEqual(asset_response.status_code, 200)
            asset_response.close()
        self.assertEqual(self.client.get("/api/v1/locations").json["racks"], [])
        default = self.client.get(f"/areas/{room_id}")
        self.assertIn(b'data-room-view="wiring"', default.data)
        self.assertIn(b'aria-current="page">Diagram', default.data)
        explicit = self.client.get(f"/areas/{room_id}/equipment")
        self.assertIn(b'data-room-view="racks"', explicit.data)
        self.assertIn(b'No racks in this area.', explicit.data)

    def test_observations_requires_login_and_escapes_room_labels(self) -> None:
        """Keep the operations screen private and render untrusted names safely."""
        with self.factory.begin() as db:
            site = Site(name="Example location")
            db.add(site)
            db.flush()
            db.add(Room(site_id=site.id, name="<script>alert(1)</script>"))
        response = self.client.get("/observations")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Whole lab', response.data)
        self.assertIn(b'&lt;script&gt;', response.data)
        self.assertNotIn(b'<script>alert(1)</script>', response.data)
        self.client.post("/signout", data={"_csrf_token": self.csrf})
        self.assertEqual(self.client.get("/observations").status_code, 302)

    def test_observation_ignore_survives_port_replacement_and_restores(self) -> None:
        """Rules use remote identities, preserve evidence, and audit only changes."""
        first, _ = self.add_observations()
        with self.factory() as db:
            device = db.get(DiscoveredDevice, first)
            target = {"condition": "port-down", "source_id": device.source_id,
                      "device_remote_id": device.remote_id, "port_remote_id": "1"}
        endpoint = "/api/v1/observation-ignores"
        saved = self.post(endpoint, {"target": target})
        self.assertEqual(saved.status_code, 200)
        rule_id = saved.json["ignore"]["id"]
        self.assertEqual(self.post(endpoint, {"target": target}).json["ignore"]["id"], rule_id)
        with self.factory.begin() as db:
            old = db.scalar(select(DiscoveredPort).where(DiscoveredPort.discovered_device_id == first))
            old_id = old.id
            # A complete discovery snapshot may replace a port's row ID.
            for link in db.scalars(select(DiscoveredLink)):
                db.delete(link)
            db.flush()
            db.delete(old)
            db.flush()
            db.add(DiscoveredPort(id=old_id + 100, discovered_device_id=first,
                                  remote_id="1", name="Renamed port", link_up=False))
        self.assertEqual(self.post(endpoint, {"target": target}).json["ignore"]["id"], rule_id)
        self.assertEqual(len(self.client.get(endpoint).json["ignores"]), 1)
        restored = self.post(f"{endpoint}/{rule_id}/remove", {})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(self.client.get(endpoint).json["ignores"], [])
        with self.factory() as db:
            port = db.get(DiscoveredPort, old_id + 100)
            self.assertFalse(port.link_up)
            self.assertIsNotNone(db.get(DiscoveredDevice, first))
            actions = list(db.scalars(select(AuditEvent.action).where(
                AuditEvent.action.in_(["observation.ignored", "observation.restored"]))))
            self.assertEqual(actions, ["observation.ignored", "observation.restored"])

    def test_observation_ignore_validates_scope_and_requires_admin_csrf(self) -> None:
        """Reject forged, unknown and cross-source targets and unauthorized writes."""
        first, _ = self.add_observations()
        with self.factory() as db:
            source_id = db.get(DiscoveredDevice, first).source_id
        endpoint = "/api/v1/observation-ignores"
        target = {"condition": "port-down", "source_id": source_id,
                  "device_remote_id": "switch-a", "port_remote_id": "1"}
        for bad in ({}, {**target, "condition": []}, {**target, "port_remote_id": "absent"},
                    {**target, "device_remote_id": "other-source-device"}, {**target, "source_id": True},
                    {**target, "wildcard": True}):
            self.assertEqual(self.post(endpoint, {"target": bad}).status_code, 400)
        self.assertEqual(self.client.post(endpoint, json={"target": target}).status_code, 403)
        saved = self.post(endpoint, {"target": target}).json["ignore"]
        self.assertEqual(self.client.post(f"{endpoint}/{saved['id']}/remove", json={}).status_code, 403)
        with self.client.session_transaction() as state:
            user_id = state["user_id"]
        with self.factory.begin() as db:
            db.get(User, user_id).is_admin = False
        self.assertEqual(self.post(endpoint, {"target": target}).status_code, 403)
        self.assertEqual(self.post(f"{endpoint}/{saved['id']}/remove", {}).status_code, 403)
        self.assertEqual(self.client.get(endpoint).status_code, 200)
        self.client.post("/signout", data={"_csrf_token": self.csrf})
        self.assertEqual(self.client.get(endpoint).status_code, 401)
        self.assertEqual(self.post(endpoint, {"target": target}).status_code, 401)

    def test_ignored_device_stays_hidden_until_restored(self) -> None:
        """Ignore a device: hidden with its cable, kept hidden on a rescan, then restored."""
        from labrat.collector.contracts import DeviceObservation, DiscoveryBatch
        from labrat.collector.store import apply_batch

        first, second = self.add_observations()
        with self.factory() as db:
            source_id = db.get(DiscoveredDevice, second).source_id
        endpoint = "/api/v1/observation-ignores"
        target = {"condition": "device", "source_id": source_id, "device_remote_id": "switch-b"}
        self.assertEqual(self.post(endpoint, {"target": {**target, "port_remote_id": "2"}}).status_code, 400)
        saved = self.post(endpoint, {"target": target})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json["ignore"]["label"], "Other Switch")

        def shown() -> tuple[set[str], int]:
            graph = self.client.get("/api/v1/topology").json
            names = {observation["remote_id"] for node in graph["nodes"] for observation in node["observations"]}
            return names, len(graph["links"])

        self.assertEqual(shown(), ({"switch-a"}, 0))
        # The source reports it again: it stays hidden, its evidence kept.
        with self.factory.begin() as db:
            source = db.get(DiscoverySource, source_id)
            apply_batch(db, source, DiscoveryBatch(devices=(
                DeviceObservation(remote_id="switch-a", name="Lab Switch", category="switch"),
                DeviceObservation(remote_id="switch-b", name="Other Switch", category="switch"),
            )))
        self.assertEqual(shown()[0], {"switch-a"})
        with self.factory() as db:
            self.assertEqual(db.get(DiscoveredDevice, second).visibility, "ignored")
        self.assertEqual(self.post(f"{endpoint}/{saved.json['ignore']['id']}/remove", {}).status_code, 200)
        self.assertEqual(shown()[0], {"switch-a", "switch-b"})

    def test_device_in_inventory_cannot_be_ignored(self) -> None:
        """Refuse to hide an observation that inventory relies on."""
        first, _ = self.add_observations()
        with self.factory.begin() as db:
            observed = db.get(DiscoveredDevice, first)
            device = Device(name="Lab Switch")
            db.add(device)
            db.flush()
            observed.device_id = device.id
            source_id = observed.source_id
        target = {"condition": "device", "source_id": source_id, "device_remote_id": "switch-a"}
        response = self.post("/api/v1/observation-ignores", {"target": target})
        self.assertEqual(response.status_code, 400)
        self.assertIn("inventory", response.json["error"])

    def add_ups(self):
        """Add a NUT source with a server and one UPS reporting readings.

        Returns:
            int: The NUT source's ID.
        """
        with self.factory.begin() as db:
            source = DiscoverySource(name="nut:default", kind="nut", config_name="default")
            db.add(source)
            db.flush()
            db.add_all([
                DiscoveredDevice(source_id=source.id, remote_id="nut-server:192.0.2.5:3493", name="ups-host",
                                 category="server", details_json=json.dumps({"status": "online"})),
                DiscoveredDevice(source_id=source.id, remote_id="nut:192.0.2.5:3493/rack", name="Rack UPS",
                                 category="ups", details_json=json.dumps({
                                     "driver": "usbhid-ups", "attachment": "usb",
                                     "server_remote_id": "nut-server:192.0.2.5:3493", "private": "x",
                                     "ups": {"state": "on_battery", "battery_charge": 80.0, "secret": "y"},
                                 })),
            ])
            return source.id

    def test_topology_exposes_ups_readings_and_server(self) -> None:
        """Pass whitelisted UPS readings and the serving computer's name."""
        self.add_ups()
        nodes = self.client.get("/api/v1/topology").json["nodes"]
        ups = next(node for node in nodes if node["name"] == "Rack UPS")["observations"][0]["ups"]
        self.assertEqual((ups["state"], ups["battery_charge"], ups["attachment"], ups["server_name"]),
                         ("on_battery", 80.0, "usb", "ups-host"))
        self.assertNotIn("secret", ups)
        server_node = next(node for node in nodes if node["name"] == "ups-host")
        # The server's graph node, so pages need not match names.
        self.assertEqual(ups["server_node_id"], server_node["id"])
        server = server_node["observations"][0]
        self.assertIsNone(server["ups"])
        self.assertNotIn('"private"', json.dumps(nodes))

    def test_ups_conditions_can_be_ignored_only_on_a_ups(self) -> None:
        """Accept UPS ignore targets for a UPS with readings, never for other devices."""
        source_id = self.add_ups()
        endpoint = "/api/v1/observation-ignores"
        for condition in ("ups-low-battery", "ups-on-battery", "ups-alert"):
            target = {"condition": condition, "source_id": source_id, "device_remote_id": "nut:192.0.2.5:3493/rack"}
            self.assertEqual(self.post(endpoint, {"target": target}).status_code, 200, condition)
        server = {"condition": "ups-alert", "source_id": source_id, "device_remote_id": "nut-server:192.0.2.5:3493"}
        self.assertEqual(self.post(endpoint, {"target": server}).status_code, 400)

    def test_wan_endpoint_returns_gateway_telemetry(self) -> None:
        """List gateways with WAN readings, keyed to their topology node."""
        first, _ = self.add_observations()
        with self.factory.begin() as db:
            gateway = db.get(DiscoveredDevice, first)
            gateway.category = "router"
            gateway.details_json = json.dumps({
                "platform": "unifi", "model": "private-model",
                "wan": {"uplinks": [{"name": "WAN1", "up": True}], "internet": {}},
                "wan_history": [[1, 2, 3, 4, 1, False]],
                "vpn_history": {"tunnel": [[1, 5, 6, 1]]},
            })
        body = self.client.get("/api/v1/wan").json
        self.assertEqual(len(body["gateways"]), 1)
        entry = body["gateways"][0]
        self.assertEqual(entry["node_id"], f"observed:{first}")
        self.assertEqual(entry["wan"]["uplinks"][0]["name"], "WAN1")
        self.assertEqual(entry["history"], [[1, 2, 3, 4, 1, False]])
        self.assertEqual(entry["vpn_history"], {"tunnel": [[1, 5, 6, 1]]})
        self.assertNotIn("private-model", json.dumps(body))
        self.client.post("/signout", data={"_csrf_token": self.csrf})
        self.assertEqual(self.client.get("/api/v1/wan").status_code, 401)

    def test_sources_endpoint_reports_collection_state_only(self) -> None:
        """Expose the polling fields pages need, behind login."""
        self.add_observations()
        response = self.client.get("/api/v1/sources")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json), {"sources"})
        self.assertEqual(set(response.json["sources"][0]), {
            "id", "name", "kind", "last_success_at", "last_attempt_at", "poll_interval_seconds", "last_error",
        })
        self.client.post("/signout", data={"_csrf_token": self.csrf})
        self.assertEqual(self.client.get("/api/v1/sources").status_code, 401)

    def test_links_keep_confidence_and_no_credentials(self) -> None:
        """Show confirmed, inferred, and ambiguous links with their provenance.

        Inferred links are trusted by default and ambiguous links are still
        returned so the UI can draw them as tentative; neither is dropped.
        """
        self.add_observations()
        topology = self.client.get("/api/v1/topology").json
        self.assertEqual(len(topology["nodes"]), 2)
        self.assertEqual(len(topology["links"]), 1)
        self.assertEqual(topology["links"][0]["evidence"], "lldp")
        self.assertEqual(topology["links"][0]["confidence"], "confirmed")
        self.assertNotIn("token", json.dumps(topology))
        for confidence in ("inferred", "ambiguous"):
            with self.subTest(confidence=confidence):
                with self.factory.begin() as db:
                    db.scalar(select(DiscoveredLink)).confidence = confidence
                links = self.client.get("/api/v1/topology").json["links"]
                self.assertEqual(len(links), 1)
                self.assertEqual(links[0]["confidence"], confidence)
                self.assertEqual(links[0]["evidence"], "lldp")
                self.assertEqual(links[0]["source"], "unifi:default")

    def test_observations_report_source_run_state(self) -> None:
        """Pass a source's own run state through, and nothing else from details."""
        first, second = self.add_observations()
        with self.factory.begin() as db:
            db.get(DiscoveredDevice, first).details_json = json.dumps({"status": "offline", "secret": "x"})
            db.get(DiscoveredDevice, second).details_json = json.dumps({"status": 3})
        nodes = self.client.get("/api/v1/topology").json["nodes"]
        states = {node["name"]: node["observations"][0]["status"] for node in nodes}
        self.assertEqual(states, {"Lab Switch": "offline", "Other Switch": None})
        self.assertNotIn("secret", json.dumps(nodes))

    def test_timestamps_carry_a_utc_offset(self) -> None:
        """Browsers must not read stored UTC times as local time."""
        self.add_observations()
        topology = self.client.get("/api/v1/topology").json
        stamps = [observation["last_seen_at"] for node in topology["nodes"] for observation in node["observations"]]
        self.assertTrue(stamps)
        self.assertTrue(all(stamp.endswith("+00:00") for stamp in stamps), stamps)

    def test_header_shows_source_lights_when_signed_in(self) -> None:
        """Show one status light per source on pages, never error text."""
        self.add_observations()
        with self.factory.begin() as db:
            db.scalar(select(DiscoverySource)).last_error = "secret controller detail"
        page = self.client.get("/locations").data
        self.assertIn(b'class="source-strip"', page)
        self.assertIn(b"unifi:default", page)
        self.assertIn(b"led-error", page)
        self.assertNotIn(b"secret controller detail", page)
        self.client.post("/signout", data={"_csrf_token": self.csrf})
        self.assertNotIn(b'class="source-strip"', self.client.get("/").data)

    def test_source_health_states_and_freshness(self) -> None:
        """Describe waiting, fresh, stale, and failed sources."""
        from datetime import datetime, timedelta, timezone
        from labrat.pages.status import describe_age, source_health

        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(describe_age(None, now), "never")
        self.assertEqual(describe_age(now - timedelta(seconds=20), now), "just now")
        self.assertEqual(describe_age(now - timedelta(minutes=4), now), "4 min ago")
        self.assertEqual(describe_age(now - timedelta(hours=3), now), "3 h ago")
        self.assertEqual(describe_age(now - timedelta(days=2), now), "2 d ago")
        self.add_observations()
        with self.factory.begin() as db:
            source = db.scalar(select(DiscoverySource))
            self.assertEqual(source_health(db, now)[0]["state"], "waiting")
            source.last_success_at = (now - timedelta(minutes=5)).replace(tzinfo=None)
            self.assertTrue(source_health(db, now)[0]["fresh"])
            source.last_success_at = (now - timedelta(hours=2)).replace(tzinfo=None)
            health = source_health(db, now)[0]
            self.assertEqual((health["state"], health["fresh"], health["age"]), ("ready", False, "2 h ago"))
            source.last_error = "timeout"
            self.assertEqual(source_health(db, now)[0]["state"], "error")

    def test_profile_lists_recent_changes_without_raw_details(self) -> None:
        """Show the viewer's audited changes as readable entries."""
        empty = self.client.get("/profile").data
        self.assertIn(b"No changes yet", empty)
        self.assertIn(b"Local password", empty)
        self.create_locations()
        page = self.client.get("/profile").data
        self.assertIn(b"Created a location", page)
        self.assertIn(b"Main Rack", page)
        self.assertNotIn(b"details_json", page)
        self.assertNotIn(b"No changes yet", page)

    def test_describe_event_falls_back_for_unknown_actions(self) -> None:
        """Build a label and subject even without a known action or name."""
        from datetime import datetime, timezone
        from labrat.pages.activity import describe_event

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        event = AuditEvent(
            actor_kind="user", action="inventory.shelf.reordered",
            subject_kind="placement_container", subject_id=7,
            details_json="not json", occurred_at=now,
        )
        entry = describe_event(event, now)
        self.assertEqual(entry["label"], "Reordered")
        self.assertEqual(entry["subject"], "placement container #7")
        self.assertEqual(entry["age"], "just now")

    def test_topology_lists_ports_in_natural_order(self) -> None:
        """Return nic2 before nic10 regardless of storage order."""
        first, _ = self.add_observations()
        with self.factory.begin() as db:
            for remote_id, name in (("9", "nic10"), ("8", "nic2"), ("7", "Port 10")):
                db.add(DiscoveredPort(discovered_device_id=first, remote_id=remote_id, name=name))
        node = next(
            node for node in self.client.get("/api/v1/topology").json["nodes"]
            if node["name"] == "Lab Switch"
        )
        self.assertEqual([port["name"] for port in node["ports"]], ["nic2", "nic10", "Port 1", "Port 10"])

    def test_room_has_floor_plan_and_equipment_views(self) -> None:
        """Both room views share a header with a Floor plan | Equipment switch."""
        site, room, _ = self.create_locations()
        floor = self.client.get(f"/areas/{room}/floor-plan").data.decode()
        equipment = self.client.get(f"/areas/{room}/equipment").data.decode()
        wiring = self.client.get(f"/areas/{room}/wiring").data.decode()
        for page in (floor, equipment, wiring):
            self.assertIn(f'href="/areas/{room}/floor-plan"', page)
            self.assertIn(f'href="/areas/{room}/equipment"', page)
            self.assertIn(f'href="/areas/{room}/wiring"', page)
            self.assertIn('aria-label="Refresh" title="Refresh" data-tooltip="Refresh"', page)
            self.assertNotIn('type="button">Refresh</button>', page)
            self.assertIn(f'href="/locations/{site}"', page)
            self.assertIn('view-frame room-frame', page)
            self.assertNotIn("Back to Home", page)
        self.assertIn('aria-current="page" aria-label="Floor plan"', floor)
        self.assertIn('aria-current="page" aria-label="Equipment"', equipment)
        self.assertIn('aria-current="page">Diagram', wiring)
        self.assertIn('data-room-view="wiring"', wiring)
        default = self.client.get(f"/areas/{room}")
        self.assertEqual(default.status_code, 200)
        self.assertIn(b'aria-current="page" aria-label="Equipment"', default.data)
        self.assertEqual(self.client.get("/areas/9999").status_code, 404)
        self.assertEqual(self.client.get("/areas/9999/floor-plan").status_code, 404)
        self.assertEqual(self.client.get("/areas/9999/wiring").status_code, 404)
        self.assertIn(f'data-room-id="{room}"', equipment)
        self.assertIn('id="rack-room-racks"', equipment)
        self.assertIn('id="rack-room-other"', equipment)
        self.assertEqual(self.client.get("/areas/9999/equipment").status_code, 404)

    def test_device_links_open_details_on_their_view(self) -> None:
        """A device link keeps its view and names the device for the page."""
        _, room, _ = self.create_locations()
        cases = {
            f"/areas/{room}/equipment/35": ("device:35", 'data-room-view="racks"'),
            f"/areas/{room}/35": ("device:35", 'data-room-view="racks"'),
            f"/areas/{room}/wiring/observed-110": ("observed:110", 'data-room-view="wiring"'),
            f"/areas/{room}/floor-plan/35": ("device:35", 'id="room-app"'),
            "/topology/35": ("device:35", 'id="topology-app"'),
            "/topology/clients/observed-110": ("observed:110", 'id="clients-app"'),
        }
        for path, (node_id, marker) in cases.items():
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            page = response.data.decode()
            self.assertIn(f'data-open-device="{node_id}"', page, path)
            self.assertIn(marker, page, path)
        self.assertNotIn("data-open-device", self.client.get(f"/areas/{room}/equipment").data.decode())
        for bad in (f"/areas/{room}/equipment/abc", f"/areas/{room}/observed-x", "/topology/device-1",
                    "/topology/clients/35a", "/areas/9999/35"):
            self.assertEqual(self.client.get(bad).status_code, 404, bad)

    def test_old_rack_addresses_redirect_to_the_rooms_equipment_view(self) -> None:
        """/racks and /racks/<id>-<slug> open the matching room's Equipment view."""
        _, room, _ = self.create_locations()
        rack_id = self.client.get("/api/v1/locations").json["racks"][0]["id"]
        listing = self.client.get("/racks")
        self.assertEqual((listing.status_code, listing.headers["Location"]), (302, f"/areas/{room}/equipment"))
        detail = self.client.get(f"/racks/{rack_id}-any-old-name")
        self.assertEqual(
            (detail.status_code, detail.headers["Location"]),
            (302, f"/areas/{room}/equipment#rack-{rack_id}"),
        )
        self.assertEqual(self.client.get("/racks/9999-gone").status_code, 404)

    def test_racks_link_goes_to_locations_when_there_are_no_rooms(self) -> None:
        """With no rooms yet, the Racks link opens Locations to create one."""
        empty = self.client.get("/racks")
        self.assertEqual((empty.status_code, empty.headers["Location"]), (302, "/locations"))

    def test_room_rack_form_payload_creates_a_rack_in_that_room(self) -> None:
        """Accept the Room View "Add a rack" payload and show it in the room."""
        _, room, _ = self.create_locations()
        created = self.post("/api/v1/locations", {
            "kind": "rack", "room_id": room, "name": "Wall Rack",
            "height_u": 24, "width_inches": 10, "has_rear": False,
        })
        self.assertEqual(created.status_code, 201)
        with self.factory() as db:
            rack = db.scalar(select(Rack).where(Rack.name == "Wall Rack"))
            self.assertEqual((rack.room_id, rack.height_u, rack.width_inches, rack.has_rear), (room, 24, 10, False))
            self.assertIsNotNone(db.scalar(select(AuditEvent).where(
                AuditEvent.action == "inventory.location.created", AuditEvent.subject_id == rack.id,
            )))
        layout = self.client.get(f"/api/v1/rooms/{room}/layout").json
        self.assertIn("Wall Rack", [c["name"] for c in layout["containers"] if c["kind"] == "rack"])
        page = self.client.get(f"/areas/{room}/floor-plan").data
        self.assertIn(b'id="room-rack-form"', page)
        self.assertIn(b'id="area-edit-tool"', page)
        self.assertIn(b'js/area-editor.js', page)

    def test_locations_nest_rooms_and_racks_inside_each_site(self) -> None:
        """Draw each site as a building containing its rooms and racks."""
        first, second = self.add_observations()
        site, room, rack_container = self.create_locations()
        self.post("/api/v1/locations", {"kind": "room", "site_id": site, "name": "attic"})
        placed = self.post("/api/v1/placements", {
            "discovery_id": first, "container_id": rack_container, "rack_u": 10,
        })
        self.assertEqual(placed.status_code, 200)
        floor = next(c for c in self.client.get(f"/api/v1/rooms/{room}/layout").json["containers"]
                     if c["kind"] == "floor")
        self.assertEqual(self.post("/api/v1/placements", {
            "discovery_id": second, "container_id": floor["id"], "position": {"x": 0.5, "y": 0.5},
        }).status_code, 200)

        # A single location's page is the Locations page (owner).
        only = self.client.get("/locations")
        self.assertEqual((only.status_code, only.headers["Location"]), (302, f"/locations/{site}"))
        page = self.client.get("/locations", follow_redirects=True).data.decode()
        # The room views' compact frame heading (owner), no totals.
        self.assertIn('class="locations-page view-frame room-frame"', page)
        self.assertNotIn("location-stats", page)
        self.assertNotIn('aria-label="Locations"', page, "One location: no tabs")
        self.assertIn(f'id="site-{site}"', page)
        # Rooms sort without regard to case: "attic" before "Basement".
        self.assertLess(page.index(">attic<"), page.index(">Basement<"))
        # An area card (owner): a ledger list of its top level, a View button,
        # no rename, delete, or count.
        self.assertIn('<li class="location-area-item" data-kind="rack">', page)
        self.assertIn("19 inch · 36U · 1 mounted", page)
        self.assertIn('<li class="location-area-item" data-kind="device">', page)
        self.assertIn("On the floor", page)
        self.assertIn(f'href="/areas/{room}">View →</a>', page)
        self.assertNotIn('data-kind="rooms"', page)
        self.assertNotIn('data-kind="racks"', page)
        self.assertNotIn("items</span>", page)
        self.assertIn(f'data-location-kind="room" data-site-id="{site}"', page)
        self.assertIn(">Add area</label>", page)
        # A new location is added in the drawer, opened from the heading.
        self.assertIn('id="location-add-tool"', page)
        self.assertIn('<form class="locations-drawer-body" id="location-add-form" data-location-kind="site">', page)

        detail = self.client.get(f"/locations/{site}").data.decode()
        self.assertIn(f'<h2 id="site-title-{site}">Areas</h2>', detail)
        self.assertNotIn("Inside ", detail)
        self.assertIn(">Locations</a>", detail)
        self.assertIn('<h1 id="locations-title">', detail)

        # Two locations show as pill tabs: All, then each, the current one marked.
        other = self.post("/api/v1/locations", {"kind": "site", "name": "Workshop"}).json["id"]
        tabs = self.client.get(f"/locations/{other}").data.decode()
        self.assertIn('<nav class="room-view-switch" aria-label="Locations">', tabs)
        self.assertIn(f'href="/locations/{other}" aria-current="page">Workshop</a>', tabs)
        self.assertIn('href="/locations">All</a>', tabs)
        listing = self.client.get("/locations").data.decode()
        self.assertIn('<h1 id="locations-title">Locations</h1>', listing)
        self.assertIn('href="/locations" aria-current="page">All</a>', listing)
        self.assertEqual(self.client.get("/locations/9999").status_code, 404)

        # Areas | Map views (owner): the map never redirects, keeps the
        # location tabs on the map, and draws from the APIs.
        self.assertIn('href="/locations/map">Map</a>', listing)
        whole = self.client.get("/locations/map")
        self.assertEqual(whole.status_code, 200)
        whole = whole.data.decode()
        self.assertIn('href="/locations/map" aria-current="page">Map</a>', whole)
        self.assertIn('href="/locations/map" aria-current="page">All</a>', whole)
        self.assertIn(f'href="/locations/{other}/map">Workshop</a>', whole)
        self.assertIn('id="locations-map-svg"', whole)
        self.assertIn('data-area-url="/areas/0"', whole)
        self.assertNotIn('id="location-add-tool"', whole)
        one = self.client.get(f"/locations/{other}/map").data.decode()
        self.assertIn(f'data-site-id="{other}"', one)
        self.assertIn(f'href="/locations/{other}" >Areas</a>'.replace(" >", ">"), one)
        self.assertEqual(self.client.get("/locations/9999/map").status_code, 404)

        # Rooms are areas now: old room addresses redirect permanently,
        # keeping the rest of the path and the query string.
        old = self.client.get(f"/rooms/{room}/equipment/35?inspect=x")
        self.assertEqual((old.status_code, old.headers["Location"]), (301, f"/areas/{room}/equipment/35?inspect=x"))
        old = self.client.get(f"/rooms/{room}")
        self.assertEqual((old.status_code, old.headers["Location"]), (301, f"/areas/{room}"))

        # The former Sites addresses redirect permanently to Locations.
        legacy = self.client.get("/sites")
        self.assertEqual((legacy.status_code, legacy.headers["Location"]), (301, "/locations"))
        legacy = self.client.get(f"/sites/{site}")
        self.assertEqual((legacy.status_code, legacy.headers["Location"]), (301, f"/locations/{site}"))

    def test_access_point_can_be_mounted_on_the_ceiling(self) -> None:
        """Place a discovered device on a room's ceiling with x/y coordinates."""
        first, _ = self.add_observations()
        _, room, _ = self.create_locations()
        layout = self.client.get(f"/api/v1/rooms/{room}/layout").json
        ceiling = [c for c in layout["containers"] if c["kind"] == "ceiling"]
        self.assertEqual(len(ceiling), 1)
        placed = self.post("/api/v1/placements", {
            "discovery_id": first, "container_id": ceiling[0]["id"],
            "position": {"x": 0.4, "y": 0.6},
        })
        self.assertEqual(placed.status_code, 200)
        self.assertEqual(placed.json["placement"]["container_kind"], "ceiling")
        self.assertIsNone(placed.json["placement"]["rack_u"])
        moved = next(
            d for d in self.client.get(f"/api/v1/rooms/{room}/layout").json["devices"]
            if d["placement"] and d["placement"]["container_id"] == ceiling[0]["id"]
        )
        self.assertEqual(moved["placement"]["position"], {"x": 0.4, "y": 0.6})

    def test_room_device_moves_floor_to_ceiling_and_back_out(self) -> None:
        """Move placed equipment between floor and ceiling, then unplace it."""
        first, _ = self.add_observations()
        _, room, _ = self.create_locations()
        containers = self.client.get(f"/api/v1/rooms/{room}/layout").json["containers"]
        floor = next(c for c in containers if c["kind"] == "floor")
        ceiling = next(c for c in containers if c["kind"] == "ceiling")
        placed = self.post("/api/v1/placements", {
            "discovery_id": first, "container_id": floor["id"], "position": {"x": 0.3, "y": 0.7},
        })
        device_id = placed.json["placement"]["device_id"]
        # Room View's "drop on Ceiling" keeps the device's spot on the plan.
        lifted = self.post(f"/api/v1/rack-devices/{device_id}/placement", {
            "container_id": ceiling["id"], "position": {"x": 0.3, "y": 0.7},
        })
        self.assertEqual(lifted.status_code, 200)
        self.assertEqual(lifted.json["placement"]["container_kind"], "ceiling")
        self.assertEqual(lifted.json["placement"]["position"], {"x": 0.3, "y": 0.7})
        # Dropping it on the equipment list takes it out of the room.
        removed = self.post(f"/api/v1/rack-devices/{device_id}/unplace", {})
        self.assertEqual(removed.status_code, 200)
        self.assertIsNone(removed.json["placement"])
        with self.factory() as db:
            self.assertIsNone(db.get(DevicePlacement, device_id))
            self.assertIsNotNone(db.get(Device, device_id))

    def test_home_counts_every_observed_link(self) -> None:
        """Count inferred and ambiguous links on the home page, not only confirmed."""
        self.add_observations()
        self.assertIn(b"1 observed link currently recorded", self.client.get("/home").data)
        for confidence in ("inferred", "ambiguous"):
            with self.subTest(confidence=confidence):
                with self.factory.begin() as db:
                    db.scalar(select(DiscoveredLink)).confidence = confidence
                self.assertIn(
                    b"1 observed link currently recorded", self.client.get("/home").data,
                )

    def test_requires_admin_csrf_for_changes(self) -> None:
        """Reject session forgery without changing the site's inventory."""
        result = self.client.post("/api/v1/locations", json={"kind": "site", "name": "Evil"})
        self.assertEqual(result.status_code, 403)
        with self.factory() as db:
            self.assertEqual(list(db.scalars(select(Site))), [])

    def test_create_locations_place_move_and_preserve_identity(self) -> None:
        """Create and move inventory without recreating devices or observations."""
        first, second = self.add_observations()
        _, room, rack = self.create_locations()
        placement = self.post("/api/v1/placements", {
            "discovery_id": first, "container_id": rack, "rack_u": 7, "height_u": 3, "face": "front",
        })
        self.assertEqual(placement.status_code, 200)
        identity = placement.json["device_id"]
        other = self.post("/api/v1/placements", {
            "discovery_id": second, "container_id": rack, "rack_u": 8, "height_u": 1, "face": "front",
        })
        self.assertEqual(other.status_code, 409)
        moved = self.post("/api/v1/placements", {
            "discovery_id": first, "container_id": rack, "rack_u": 20, "face": "rear",
        })
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.json["device_id"], identity)
        with self.factory() as db:
            device = db.get(Device, identity)
            self.assertEqual(device.room_id, room)
            self.assertEqual(device.height_u, 3)
            self.assertEqual(db.get(DevicePlacement, identity).rack_u, 20)
            self.assertEqual(len(list(db.scalars(select(Device)))), 1)
            self.assertGreaterEqual(len(list(db.scalars(select(AuditEvent)))), 5)
        topology = self.client.get("/api/v1/topology").json
        self.assertEqual(topology["nodes"][0]["placement"]["rack_u"], 20)
        self.assertEqual(len(topology["links"]), 1)

    def test_same_source_observations_cannot_share_an_inventory_identity(self) -> None:
        """Keep distinct controller records visible while allowing cross-source matches."""
        first, second = self.add_observations()
        _, _, rack = self.create_locations()
        original = self.post("/api/v1/placements", {
            "discovery_id": first, "container_id": rack, "rack_u": 7, "face": "front",
        })
        self.assertEqual(original.status_code, 200)
        device_id = original.json["device_id"]
        rejected = self.post("/api/v1/placements", {
            "discovery_id": second, "existing_device_id": device_id,
            "container_id": rack, "rack_u": 8, "face": "front",
        })
        self.assertEqual(rejected.status_code, 409)
        with self.factory() as db:
            self.assertIsNone(db.get(DiscoveredDevice, second).device_id)
            self.assertEqual(db.get(DevicePlacement, device_id).rack_u, 7)

        with self.factory.begin() as db:
            other_source = DiscoverySource(
                name="proxmox:default", kind="proxmox", config_name="default",
            )
            db.add(other_source)
            db.flush()
            other = DiscoveredDevice(
                source_id=other_source.id, remote_id="switch-a", name="Lab Switch",
                category="switch",
            )
            db.add(other)
            db.flush()
            other_id = other.id
        matched = self.post("/api/v1/placements", {
            "discovery_id": other_id, "existing_device_id": device_id,
            "container_id": rack, "rack_u": 7, "face": "front",
        })
        self.assertEqual(matched.status_code, 200)
        self.assertEqual(matched.json["device_id"], device_id)

    def test_manual_devices_use_face_specific_rack_space(self) -> None:
        """Allow opposite-face equipment at the same U and reject collisions."""
        _, room, rack = self.create_locations()
        rear = self.post("/api/v1/rack-devices", {
            "name": "Rack UPS", "class_name": "UPS", "height_u": 1,
            "container_id": rack, "rack_u": 7, "face": "rear",
        })
        self.assertEqual(rear.status_code, 201)
        front = self.post("/api/v1/rack-devices", {
            "name": "Front patch panel", "class_name": "Patch panel", "height_u": 1,
            "container_id": rack, "rack_u": 7, "face": "front",
        })
        self.assertEqual(front.status_code, 201)
        collision = self.post("/api/v1/rack-devices", {
            "name": "Another rear device", "height_u": 1,
            "container_id": rack, "rack_u": 7, "face": "rear",
        })
        self.assertEqual(collision.status_code, 409)
        moved = self.post(f"/api/v1/rack-devices/{rear.json['device_id']}/placement", {
            "container_id": rack, "rack_u": 9, "height_u": 2, "face": "rear",
        })
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.json["placement"]["rack_u"], 9)
        locations = self.client.get("/api/v1/locations").json
        self.assertEqual(len(locations["devices"]), 2)
        other_rack = self.post("/api/v1/locations", {
            "kind": "rack", "room_id": room, "name": "Second Rack", "height_u": 12,
        })
        self.assertEqual(other_rack.status_code, 201)
        other_container = next(
            item["id"] for item in self.client.get("/api/v1/locations").json["containers"]
            if item["rack_id"] == other_rack.json["id"]
        )
        moved_across = self.post(
            f"/api/v1/rack-devices/{rear.json['device_id']}/placement", {
                "container_id": other_container, "rack_u": 4,
                "height_u": 2, "face": "front",
            },
        )
        self.assertEqual(moved_across.status_code, 200)
        self.assertEqual(moved_across.json["placement"]["container_id"], other_container)
        unplaced = self.post(
            f"/api/v1/rack-devices/{rear.json['device_id']}/unplace", {},
        )
        self.assertEqual(unplaced.status_code, 200)
        self.assertIsNone(unplaced.json["placement"])

    def test_move_between_racks_and_unplace_preserves_discovery(self) -> None:
        """Move across rooms, then clear location without losing identity or links."""
        first, _ = self.add_observations()
        site, _, first_rack = self.create_locations()
        other_room = self.post("/api/v1/locations", {
            "kind": "room", "site_id": site, "name": "Workshop",
        })
        self.assertEqual(other_room.status_code, 201)
        second_rack = self.post("/api/v1/locations", {
            "kind": "rack", "room_id": other_room.json["id"],
            "name": "Workshop Rack", "height_u": 12,
        })
        self.assertEqual(second_rack.status_code, 201)
        containers = self.client.get("/api/v1/locations").json["containers"]
        second_container = next(
            item["id"] for item in containers if item["rack_id"] == second_rack.json["id"]
        )
        initial = self.post("/api/v1/placements", {
            "discovery_id": first, "container_id": first_rack,
            "rack_u": 5, "face": "front",
        })
        self.assertEqual(initial.status_code, 200)
        device_id = initial.json["device_id"]
        moved = self.post("/api/v1/placements", {
            "discovery_id": first, "container_id": second_container,
            "rack_u": 9, "face": "rear",
        })
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.json["placement"]["container_id"], second_container)
        with self.factory() as db:
            device = db.get(Device, device_id)
            self.assertEqual(device.room_id, other_room.json["id"])
            self.assertEqual(device.rack_id, second_rack.json["id"])
            self.assertEqual(device.rack_u, 9)

        rejected = self.client.post(
            f"/api/v1/rack-devices/{device_id}/unplace", json={},
        )
        self.assertEqual(rejected.status_code, 403)
        removed = self.post(f"/api/v1/rack-devices/{device_id}/unplace", {})
        self.assertEqual(removed.status_code, 200)
        self.assertIsNone(removed.json["placement"])
        with self.factory() as db:
            device = db.get(Device, device_id)
            self.assertIsNone(db.get(DevicePlacement, device_id))
            self.assertIsNone(device.room_id)
            self.assertIsNone(device.rack_id)
            self.assertIsNone(device.rack_u)
            self.assertIsNone(device.placement)
            self.assertEqual(db.get(DiscoveredDevice, first).device_id, device_id)
            self.assertTrue(db.scalar(select(AuditEvent).where(
                AuditEvent.action == "inventory.device.unplaced",
                AuditEvent.subject_id == device_id,
            )))
        topology = self.client.get("/api/v1/topology").json
        self.assertIsNone(next(node for node in topology["nodes"]
                               if node["device_id"] == device_id)["placement"])
        self.assertEqual(len(topology["links"]), 1)
        self.assertEqual(self.post(
            f"/api/v1/rack-devices/{device_id}/unplace", {},
        ).status_code, 200)

    def test_inventory_display_name_survives_source_rename(self) -> None:
        """A user label persists while the collector's reported name changes."""
        observed_id, _ = self.add_observations()
        _, _, rack = self.create_locations()
        placed = self.post("/api/v1/placements", {
            "discovery_id": observed_id, "container_id": rack,
            "rack_u": 4, "face": "front",
        })
        self.assertEqual(placed.status_code, 200)
        device_id = placed.json["device_id"]
        endpoint = f"/api/v1/rack-devices/{device_id}/name"
        self.assertEqual(self.client.post(endpoint, json={"name": "Core switch"}).status_code, 403)
        self.assertEqual(self.post(endpoint, {"name": "  "}).status_code, 400)
        saved = self.post(endpoint, {"name": "  Core switch  "})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json["name"], "Core switch")
        with self.factory.begin() as db:
            observed = db.get(DiscoveredDevice, observed_id)
            self.assertEqual(observed.name, "Lab Switch")
            observed.name = "Controller renamed switch"
        topology = self.client.get("/api/v1/topology").json
        node = next(item for item in topology["nodes"] if item["device_id"] == device_id)
        self.assertEqual(node["name"], "Core switch")
        self.assertEqual(node["observations"][0]["reported_name"], "Controller renamed switch")
        locations = self.client.get("/api/v1/locations").json
        self.assertEqual(next(item for item in locations["devices"] if item["id"] == device_id)["name"], "Core switch")
        with self.factory() as db:
            events = list(db.scalars(select(AuditEvent).where(
                AuditEvent.action == "inventory.device.renamed",
                AuditEvent.subject_id == device_id,
            )))
            self.assertEqual(len(events), 1)
            self.assertEqual(db.get(Device, device_id).name, "Core switch")

    def test_rack_editor_respects_single_sided_placements(self) -> None:
        """Rename a rack and reject detail changes that strand equipment."""
        site = self.post("/api/v1/locations", {"kind": "site", "name": "Home"})
        room = self.post("/api/v1/locations", {
            "kind": "room", "site_id": site.json["id"], "name": "Workshop",
        })
        created = self.post("/api/v1/locations", {
            "kind": "rack", "room_id": room.json["id"],
            "name": "Wall Cabinet", "height_u": 12, "has_rear": False,
        })
        self.assertEqual(created.status_code, 201)
        rack_id = created.json["id"]
        locations = self.client.get("/api/v1/locations").json
        rack = next(item for item in locations["racks"] if item["id"] == rack_id)
        self.assertFalse(rack["has_rear"])
        container_id = next(item["id"] for item in locations["containers"]
                            if item["rack_id"] == rack_id)
        rear = self.post("/api/v1/rack-devices", {
            "name": "Rear UPS", "height_u": 1, "container_id": container_id,
            "rack_u": 10, "face": "rear",
        })
        self.assertEqual(rear.status_code, 400)
        front = self.post("/api/v1/rack-devices", {
            "name": "Front switch", "height_u": 1, "container_id": container_id,
            "rack_u": 10, "face": "front",
        })
        self.assertEqual(front.status_code, 201)
        invalid_height = self.post(f"/api/v1/racks/{rack_id}", {
            "name": "Wall Cabinet", "height_u": 8, "has_rear": False,
        })
        self.assertEqual(invalid_height.status_code, 409)
        expanded = self.post(f"/api/v1/racks/{rack_id}", {
            "name": "Network Cabinet", "height_u": 12, "has_rear": True,
        })
        self.assertEqual(expanded.status_code, 200)
        self.assertEqual(expanded.json["name"], "Network Cabinet")
        rear = self.post("/api/v1/rack-devices", {
            "name": "Rear UPS", "height_u": 1, "container_id": container_id,
            "rack_u": 10, "face": "rear",
        })
        self.assertEqual(rear.status_code, 201)
        blocked = self.post(f"/api/v1/racks/{rack_id}", {
            "name": "Network Cabinet", "height_u": 12, "has_rear": False,
        })
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.post(
            f"/api/v1/rack-devices/{rear.json['device_id']}/unplace", {},
        ).status_code, 200)
        self.assertEqual(self.post(
            f"/api/v1/rack-devices/{front.json['device_id']}/placement", {
                "container_id": container_id, "rack_u": 5,
                "height_u": 1, "face": "front",
            },
        ).status_code, 200)
        updated = self.post(f"/api/v1/racks/{rack_id}", {
            "name": "Network Cabinet", "height_u": 8, "has_rear": False,
        })
        self.assertEqual(updated.status_code, 200)
        with self.factory() as db:
            self.assertEqual(db.get(Rack, rack_id).height_u, 8)
            self.assertFalse(db.get(Rack, rack_id).has_rear)
            self.assertEqual(db.get(PlacementContainer, container_id).name, "Network Cabinet")

    def test_non_rack_container_does_not_require_u(self) -> None:
        """Place a device on a shelf without inventing rack unit positions."""
        first, _ = self.add_observations()
        _, room, _ = self.create_locations()
        # Plain shelves sit on a wall, floor, surface, or rack top; shelves
        # inside a rack are the separate one-U ``rack_shelf`` kind.
        floor = next(
            c for c in self.client.get("/api/v1/locations").json["containers"]
            if c["room_id"] == room and c["kind"] == "floor"
        )
        shelf = self.post("/api/v1/locations", {
            "kind": "shelf", "name": "Top Shelf", "room_id": room,
            "parent_id": floor["id"],
        })
        self.assertEqual(shelf.status_code, 201)
        placement = self.post("/api/v1/placements", {
            "discovery_id": first, "container_id": shelf.json["id"],
        })
        self.assertEqual(placement.status_code, 200)
        self.assertIsNone(placement.json["placement"]["rack_u"])

    def neighbor_fixture(self):
        """Create an unresolved switch neighbor and an inventory server NIC.

        Returns:
            dict: The neighbor-match payload naming the switch's neighbor and the
                server NIC.
        """
        switch_id, _ = self.add_observations()
        neighbor = {"local_port": "1", "chassis_id": "aa:11:22:33:44:55",
                    "port_id": "remote-ethernet", "management_ips": ["192.0.2.50"]}
        with self.factory.begin() as db:
            for link in db.scalars(select(DiscoveredLink)):
                db.delete(link)
            switch = db.get(DiscoveredDevice, switch_id)
            switch.details_json = json.dumps({"lldp_neighbors": [neighbor]})
            source = DiscoverySource(name="proxmox:test", kind="proxmox", config_name="test")
            inventory = Device(name="Existing server")
            db.add_all([source, inventory]); db.flush()
            server = DiscoveredDevice(source_id=source.id, remote_id="server", name="Existing server",
                                      category="server", device_id=inventory.id)
            db.add(server); db.flush()
            nic = DiscoveredPort(discovered_device_id=server.id, remote_id="nic0", name="nic0", link_up=True)
            db.add(nic); db.flush()
            payload = {"switch_observation_id": switch_id, "target_port_id": nic.id,
                       **{key: neighbor[key] for key in ("local_port", "chassis_id", "port_id")}}
        return payload

    def test_neighbor_identity_match_survives_reconciliation_and_can_be_removed(self):
        """Save curated identity without changing placement or canonical cables."""
        from labrat.collector.links import reconcile_proxmox_lldp_links
        from labrat.discovery.neighbor_matches import saved_neighbor_matches
        payload = self.neighbor_fixture()
        graph = self.client.get("/api/v1/topology").json
        switch = next(node for node in graph["nodes"] if node["discovery_id"] == payload["switch_observation_id"])
        self.assertFalse(switch["observations"][0]["neighbors"][0]["resolved"])
        response = self.post("/api/v1/neighbor-matches", payload)
        self.assertEqual(response.status_code, 201, response.json)
        match_id = response.json["id"]
        with self.factory.begin() as db:
            source = db.get(DiscoverySource, db.get(DiscoveredDevice, payload["switch_observation_id"]).source_id)
            proxmox = list(db.scalars(select(DiscoverySource).where(DiscoverySource.kind == "proxmox")))
            self.assertEqual(reconcile_proxmox_lldp_links(db, source, proxmox), 1)
            self.assertEqual(len(saved_neighbor_matches(db)), 1)
            link = db.scalar(select(DiscoveredLink))
            self.assertEqual((link.evidence, link.confidence), ("lldp-user-match", "confirmed"))
            self.assertEqual(list(db.scalars(select(DevicePlacement))), [])
        graph = self.client.get("/api/v1/topology").json
        switch = next(node for node in graph["nodes"] if node["discovery_id"] == payload["switch_observation_id"])
        self.assertTrue(switch["observations"][0]["neighbors"][0]["resolved"])
        with self.factory.begin() as db:
            switch_observation = db.get(DiscoveredDevice, payload["switch_observation_id"])
            switch_observation.details_json = json.dumps({"lldp_neighbors": []})
            source = db.get(DiscoverySource, switch_observation.source_id)
            proxmox = list(db.scalars(select(DiscoverySource).where(DiscoverySource.kind == "proxmox")))
            self.assertEqual(reconcile_proxmox_lldp_links(db, source, proxmox), 0)
            self.assertEqual(len(saved_neighbor_matches(db)), 1)
            self.assertIsNone(db.scalar(select(DiscoveredLink)))
        self.assertEqual(self.post(f"/api/v1/neighbor-matches/{match_id}/remove", {}).status_code, 200)
        with self.factory() as db:
            self.assertEqual(saved_neighbor_matches(db), [])
            self.assertIsNone(db.scalar(select(DiscoveredLink)))

    def test_ambiguous_neighbor_identity_cannot_be_matched(self):
        """A repeated chassis/remote-port identity cannot identify one cable."""
        payload = self.neighbor_fixture()
        with self.factory.begin() as db:
            switch = db.get(DiscoveredDevice, payload["switch_observation_id"])
            details = json.loads(switch.details_json)
            details["lldp_neighbors"].append({**details["lldp_neighbors"][0], "local_port": "2"})
            switch.details_json = json.dumps(details)
        self.assertEqual(self.post("/api/v1/neighbor-matches", payload).status_code, 409)

    def lacp_neighbor_graph(self, *, secondary_mac="AA:BB:CC:00:11:23", bond_mode="802.3ad",
                            bonded=True, cabled=True):
        """Return a switch's neighbors after cabling an LACP member NIC.

        Args:
            secondary_mac: MAC the extra neighbor reports as chassis and port.
            bond_mode: Mode of the server's bond.
            bonded: Whether the cabled NIC is a member of the bond.
            cabled: Whether the switch port has a cable to the NIC.

        Returns:
            list[dict]: The switch observation's neighbor records.
        """
        payload = self.neighbor_fixture()
        with self.factory.begin() as db:
            switch = db.get(DiscoveredDevice, payload["switch_observation_id"])
            nic = db.get(DiscoveredPort, payload["target_port_id"])
            nic.mac = "aa:bb:cc:00:11:22"
            local = db.scalar(select(DiscoveredPort).where(
                DiscoveredPort.discovered_device_id == switch.id, DiscoveredPort.remote_id == "1",
            ))
            db.add(DiscoveredNetworkInterface(
                discovered_device_id=nic.discovered_device_id, remote_id="bond0", name="bond0",
                kind="bond", mode=bond_mode,
                members_json=json.dumps(["nic0", "nic1"] if bonded else ["nic1"]),
            ))
            if cabled:
                db.add(DiscoveredLink(
                    source_id=switch.source_id, a_port_id=min(local.id, nic.id),
                    b_port_id=max(local.id, nic.id), evidence="lldp-proxmox", confidence="confirmed",
                ))
            switch.details_json = json.dumps({"lldp_neighbors": [
                {"local_port": "1", "chassis_id": "aa:bb:cc:00:00:01", "port_id": "nic0"},
                {"local_port": "1", "chassis_id": secondary_mac, "port_id": secondary_mac},
            ]})
        graph = self.client.get("/api/v1/topology").json
        switch = next(node for node in graph["nodes"] if node["discovery_id"] == payload["switch_observation_id"])
        return switch["observations"][0]["neighbors"]

    def test_lacp_member_secondary_identity_is_not_unresolved(self):
        """An adjacent MAC on a cabled LACP member's port is that NIC, not a warning."""
        primary, secondary = self.lacp_neighbor_graph()
        self.assertTrue(primary["resolved"])
        self.assertFalse(primary["lacp_secondary"])
        self.assertTrue(secondary["resolved"])
        self.assertTrue(secondary["lacp_secondary"])
        self.assertEqual(secondary["chassis_id"], "AA:BB:CC:00:11:23")

    def test_lacp_secondary_identity_requires_bond_cable_and_mac_prefix(self):
        """Anything short of every condition stays an unresolved neighbor."""
        cases = {
            "different NIC prefix": {"secondary_mac": "AA:BB:CC:00:12:23"},
            "not a bond member": {"bonded": False},
            "not an LACP bond": {"bond_mode": "active-backup"},
            "no cable on the port": {"cabled": False},
        }
        for label, options in cases.items():
            with self.subTest(label):
                self.setUp()  # Fresh database per case; cleanups stack.
                secondary = self.lacp_neighbor_graph(**options)[1]
                self.assertFalse(secondary["resolved"])
                self.assertFalse(secondary["lacp_secondary"])

    def blank_port_neighbor(self, chassis_owner: str) -> dict:
        """Return a blank-port neighbor whose chassis is a NIC on some host.

        The switch port is cabled to the server's ``nic0``; the chassis ID is
        the MAC of ``mgmt0`` on either the same server or a second host.

        Args:
            chassis_owner: ``"same"`` or ``"other"`` host owns the chassis MAC.

        Returns:
            dict: The switch observation's single neighbor record.
        """
        payload = self.neighbor_fixture()
        with self.factory.begin() as db:
            switch = db.get(DiscoveredDevice, payload["switch_observation_id"])
            nic = db.get(DiscoveredPort, payload["target_port_id"])
            nic.mac = "aa:bb:cc:00:11:22"
            owner_id = nic.discovered_device_id
            if chassis_owner == "other":
                server = db.get(DiscoveredDevice, owner_id)
                other = DiscoveredDevice(source_id=server.source_id, remote_id="other",
                                         name="Other server", category="server")
                db.add(other); db.flush()
                owner_id = other.id
            db.add(DiscoveredPort(discovered_device_id=owner_id, remote_id="mgmt0",
                                  name="mgmt0", mac="aa:bb:cc:00:11:21"))
            local = db.scalar(select(DiscoveredPort).where(
                DiscoveredPort.discovered_device_id == switch.id, DiscoveredPort.remote_id == "1",
            ))
            db.add(DiscoveredLink(
                source_id=switch.source_id, a_port_id=min(local.id, nic.id),
                b_port_id=max(local.id, nic.id), evidence="unifi-client-mac", confidence="inferred",
            ))
            switch.details_json = json.dumps({"lldp_neighbors": [
                {"local_port": "1", "chassis_id": "aa:bb:cc:00:11:21", "port_id": ""},
            ]})
        graph = self.client.get("/api/v1/topology").json
        switch = next(node for node in graph["nodes"] if node["discovery_id"] == payload["switch_observation_id"])
        return switch["observations"][0]["neighbors"][0]

    def test_blank_port_neighbor_resolves_when_chassis_names_cabled_host(self):
        """A host chassis ID on a port cabled to another of its NICs is resolved."""
        neighbor = self.blank_port_neighbor("same")
        self.assertTrue(neighbor["resolved"])
        self.assertFalse(neighbor["lacp_secondary"])

    def test_blank_port_neighbor_stays_unresolved_for_a_different_host(self):
        """A chassis ID naming another host does not match the cabled one."""
        self.assertFalse(self.blank_port_neighbor("other")["resolved"])

    def test_neighbor_matching_rejects_forged_or_conflicting_targets(self):
        """Require CSRF, a current neighbor, and an unoccupied physical NIC."""
        payload = self.neighbor_fixture()
        self.assertEqual(self.client.post("/api/v1/neighbor-matches", json=payload).status_code, 403)
        self.assertEqual(self.post("/api/v1/neighbor-matches", {**payload, "chassis_id": "different"}).status_code, 409)
        self.assertEqual(self.post("/api/v1/neighbor-matches", {**payload, "target_port_id": 99999}).status_code, 400)
        with self.factory.begin() as db:
            ports = list(db.scalars(select(DiscoveredPort)))
            local, other = ports[:2]
            db.add(DiscoveredLink(source_id=db.get(DiscoveredDevice, local.discovered_device_id).source_id,
                                 a_port_id=min(local.id, other.id), b_port_id=max(local.id, other.id),
                                 evidence="lldp", confidence="confirmed"))
        self.assertEqual(self.post("/api/v1/neighbor-matches", payload).status_code, 409)



if __name__ == "__main__":
    unittest.main()
