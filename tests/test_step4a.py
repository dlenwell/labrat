# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise Step 4A's storage, collector contract, and audit trail.

Every test uses an isolated SQLite database and local config path. Nothing in
this suite contacts Proxmox, UniFi, or any external network endpoint.
"""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from labrat.audit import AuditEvent, record_audit_event
from labrat.collector.contracts import (
    DeviceObservation, DiscoveryBatch, LinkObservation, PortObservation,
)
from labrat.collector.runner import poll_once
from labrat.collector.store import apply_batch
from labrat.config import load_local_config
from labrat.database import Base, make_engine, make_session_factory
from labrat.discovery.models import (
    DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource,
    InterfaceLinkPolicy,
)
from labrat.metadata import Attribute, InventoryObject
from labrat.metadata_values import decode_value, encode_value, validate_value
from labrat.models import Device, Interface, InterfaceLink, Rack, Room, Site
from labrat.placement import DevicePlacement, PlacementContainer


class Step4ATests(unittest.TestCase):
    """Check migration compatibility and the independent collector lifecycle."""

    def setUp(self) -> None:
        """Prepare a temporary database and redirect Alembic's config lookup."""
        temp = TemporaryDirectory(prefix="labrat-step4a-")
        self.addCleanup(temp.cleanup)
        self.db_path = Path(temp.name) / "test.sqlite3"
        config_path = Path(temp.name) / "local_config.toml"
        config_path.write_text(
            f"[database]\npath = {json.dumps(str(self.db_path))}\n",
            encoding="utf-8",
        )
        environment = patch.dict(os.environ, {"LABRAT_CONFIG": str(config_path)})
        environment.start()
        self.addCleanup(environment.stop)
        self.alembic_config = Config(
            str(Path(__file__).resolve().parents[1] / "alembic.ini")
        )

    def _connect(self):
        """Return a configured database engine and register its cleanup.

        Returns:
            Engine: The engine.
        """
        engine = make_engine(load_local_config())
        self.addCleanup(engine.dispose)
        return engine

    def _migrate(self):
        """Apply all migrations to a new test database and return sessions.

        Returns:
            sessionmaker: Sessions on the migrated database.
        """
        command.upgrade(self.alembic_config, "head")
        return make_session_factory(self._connect())

    def _create_sample(self, db):
        """Persist one physical site, room, rack and server in a session.

        Args:
            db: The open session.

        Returns:
            tuple: The site, room, rack, and server records.
        """
        site = Site(name="Home")
        db.add(site)
        db.flush()
        room = Room(name="Basement", site_id=site.id)
        db.add(room)
        db.flush()
        rack = Rack(name="Main", room_id=room.id, height_u=36)
        db.add(rack)
        db.flush()
        server = Device(name="one", rack_id=rack.id, rack_u=7, height_u=3)
        db.add(server)
        db.flush()
        return site, room, rack, server

    def test_upgrade_preserves_legacy_placements(self) -> None:
        """Backfill registries and placements for preexisting Step 2 records."""
        command.upgrade(self.alembic_config, "0001_initial")
        engine = self._connect()
        sessions = make_session_factory(engine)
        with sessions.begin() as db:
            site = Site(name="Home")
            db.add(site)
            db.flush()
            room = Room(name="Basement", site_id=site.id)
            db.add(room)
            db.flush()
            # Use the historical rack columns before revision 0004 exists.
            rack_id = db.execute(text(
                "INSERT INTO racks (room_id, name, height_u) "
                "VALUES (:room_id, 'Main', 36) RETURNING id"
            ), {"room_id": room.id}).scalar_one()
            # The current Device model maps columns added by later revisions
            # (for example 0005's port_facing), so write the 0001 shape directly.
            device_id = db.execute(text(
                "INSERT INTO devices (name, rack_id, rack_u, height_u) "
                "VALUES ('one', :rack_id, 7, 3) RETURNING id"
            ), {"rack_id": rack_id}).scalar_one()
            ids = (site.id, room.id, rack_id, device_id)
        command.upgrade(self.alembic_config, "head")
        with sessions() as db:
            for column, identity in zip(("site_id", "room_id", "rack_id", "device_id"), ids):
                self.assertIsNotNone(db.scalar(select(InventoryObject).where(getattr(InventoryObject, column) == identity)))
            rack_container = db.scalar(select(PlacementContainer).where(PlacementContainer.rack_id == ids[2]))
            self.assertEqual(rack_container.kind, "rack")
            self.assertTrue(db.get(Rack, ids[2]).has_rear)
            self.assertEqual(db.get(DevicePlacement, ids[3]).rack_u, 7)
            self.assertEqual(db.get(DevicePlacement, ids[3]).container_id, rack_container.id)

    def test_ceiling_migration_backfills_and_adds_to_new_rooms(self) -> None:
        """Revision 0010 gives old and new rooms one ceiling; downgrade keeps it."""
        command.upgrade(self.alembic_config, "0009_port_type_and_max_speed")
        engine = self._connect()
        with engine.begin() as connection:
            site_id = connection.execute(text(
                "INSERT INTO sites (name) VALUES ('Home') RETURNING id"
            )).scalar_one()
            old_room = connection.execute(text(
                "INSERT INTO rooms (site_id, name) VALUES (:site, 'Old') RETURNING id"
            ), {"site": site_id}).scalar_one()
        command.upgrade(self.alembic_config, "head")

        def ceilings(room_id: int) -> int:
            with engine.connect() as connection:
                return connection.execute(text(
                    "SELECT count(*) FROM placement_containers "
                    "WHERE room_id = :room AND kind = 'ceiling'"
                ), {"room": room_id}).scalar_one()

        self.assertEqual(ceilings(old_room), 1)
        with engine.begin() as connection:
            new_room = connection.execute(text(
                "INSERT INTO rooms (site_id, name) VALUES (:site, 'New') RETURNING id"
            ), {"site": site_id}).scalar_one()
        self.assertEqual(ceilings(new_room), 1)

        # Downgrading stops adding ceilings but keeps the ones that exist.
        command.downgrade(self.alembic_config, "0009_port_type_and_max_speed")
        self.assertEqual(ceilings(old_room), 1)
        with engine.begin() as connection:
            later_room = connection.execute(text(
                "INSERT INTO rooms (site_id, name) VALUES (:site, 'Later') RETURNING id"
            ), {"site": site_id}).scalar_one()
        self.assertEqual(ceilings(later_room), 0)

    def test_new_owners_and_attributes_with_duplicate_keys(self) -> None:
        """Attribute owner FKs are real and duplicate keys remain valid."""
        sessions = self._migrate()
        with sessions.begin() as db:
            site, room, rack, server = self._create_sample(db)
            owner = db.scalar(select(InventoryObject).where(InventoryObject.device_id == server.id))
            self.assertIsNotNone(owner)
            db.add_all([
                Attribute(owner_id=owner.id, key="temperature", value=encode_value({"float": 35.7})),
                Attribute(owner_id=owner.id, key="temperature", value=encode_value({"float": 40.2})),
                Attribute(owner_id=owner.id, key="dimensions", value=encode_value({"json": {"width": 1.2}})),
            ])
        with sessions() as db:
            values = db.scalars(select(Attribute).where(Attribute.key == "temperature")).all()
            self.assertEqual(len(values), 2)
            self.assertEqual(decode_value(values[0].value), {"float": 35.7})

        with self.assertRaises(IntegrityError):
            with sessions.begin() as db:
                db.add(Attribute(owner_id=999999, key="ghost", value='{"str":"oops"}'))

    def test_type_tagged_json_validation(self) -> None:
        """Support typed values but reject malformed and non-finite inputs."""
        for value in ({"str": "ok"}, {"int": 9}, {"bool": False}, {"json": [1, {"a": 2}]}):
            self.assertEqual(decode_value(encode_value(value)), value)
        for invalid in ({"int": True}, {"str": 5}, {"float": float("nan")}, {"bogus": 1}, {"str": "x", "int": 4}):
            with self.assertRaises(ValueError):
                validate_value(invalid)

    def test_container_supports_rack_shelf_and_single_placement(self) -> None:
        """Allow nested surfaces and one active placement without moving cables."""
        sessions = self._migrate()
        with sessions.begin() as db:
            _, room, rack, host = self._create_sample(db)
            rack_container = db.scalar(select(PlacementContainer).where(PlacementContainer.rack_id == rack.id))
            shelf = PlacementContainer(name="Top Shelf", kind="shelf", room_id=room.id, parent_id=rack_container.id)
            db.add(shelf)
            db.flush()
            # New application writes use the placement table explicitly;
            # the migration only backfills *existing* legacy placements.
            self.assertIsNone(db.get(DevicePlacement, host.id))
            db.add(DevicePlacement(
                device_id=host.id, container_id=shelf.id,
                position_json='{"order":1}',
            ))
        with sessions() as db:
            placement = db.get(DevicePlacement, host.id)
            self.assertEqual(placement.container_id, shelf.id)
            self.assertIsNone(placement.rack_u)
        with self.assertRaises(IntegrityError):
            with sessions.begin() as db:
                db.add(DevicePlacement(device_id=host.id, container_id=shelf.id))

    def test_manual_link_policy_survives_observed_changes(self) -> None:
        """A manual cable lock remains independent of candidate links."""
        sessions = self._migrate()
        with sessions.begin() as db:
            _, _, _, host = self._create_sample(db)
            other = Device(name="switch")
            db.add(other)
            db.flush()
            a = Interface(device_id=host.id, name="nic0")
            b = Interface(device_id=other.id, name="port1")
            db.add_all([a, b])
            db.flush()
            link = InterfaceLink(a_interface_id=min(a.id, b.id), b_interface_id=max(a.id, b.id))
            db.add(link)
            db.flush()
            db.add(InterfaceLinkPolicy(interface_link_id=link.id, manual_locked=True, suppress_conflicts=True))
            link_id = link.id
        with sessions() as db:
            policy = db.get(InterfaceLinkPolicy, link_id)
            self.assertTrue(policy.manual_locked)
            self.assertTrue(policy.suppress_conflicts)

    def test_collector_upserts_only_changes_and_audits(self) -> None:
        """Poll twice without duplicate devices, ports, links, or audit spam."""
        sessions = self._migrate()
        with sessions.begin() as db:
            source = DiscoverySource(name="Test UniFi", kind="unifi", config_name="home")
            db.add(source)
            db.flush()
            source_id = source.id
        batch = DiscoveryBatch(
            devices=(
                DeviceObservation(remote_id="sw", name="Switch", category="switch"),
                DeviceObservation(remote_id="pve", name="one", category="server"),
            ),
            ports=(
                PortObservation(device_remote_id="sw", remote_id="1", name="port1", speed_mbps=10000),
                PortObservation(device_remote_id="pve", remote_id="enp1", name="enp1", speed_mbps=10000),
            ),
            links=(LinkObservation("sw", "1", "pve", "enp1", evidence="lldp", confidence="confirmed"),),
        )
        with sessions.begin() as db:
            apply_batch(db, db.get(DiscoverySource, source_id), batch)
        with sessions() as db:
            self.assertEqual(len(db.scalars(select(DiscoveredDevice)).all()), 2)
            self.assertEqual(len(db.scalars(select(DiscoveredPort)).all()), 2)
            self.assertEqual(len(db.scalars(select(DiscoveredLink)).all()), 1)
            self.assertEqual(len(db.scalars(select(AuditEvent)).all()), 5)
        with sessions.begin() as db:
            apply_batch(db, db.get(DiscoverySource, source_id), batch)
        with sessions() as db:
            self.assertEqual(len(db.scalars(select(AuditEvent)).all()), 5)
        with sessions.begin() as db:
            changed = DiscoveryBatch(
                devices=batch.devices,
                ports=(batch.ports[0], PortObservation("pve", "enp1", "enp1", speed_mbps=2500)),
                links=batch.links,
            )
            apply_batch(db, db.get(DiscoverySource, source_id), changed)
        with sessions() as db:
            events = db.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
            self.assertEqual(len(events), 6)
            self.assertEqual(events[-1].action, "discovery.port.changed")
            self.assertEqual(json.loads(events[-1].details_json), {"changed_fields": ["speed_mbps"]})
            self.assertTrue(all(event.actor_kind == "collector" for event in events))
            # Discovery observations do not silently produce canonical records.
            self.assertEqual(db.scalar(select(Device).where(Device.name == "Switch")), None)

    def test_collector_failure_sanitizes_audit_details(self) -> None:
        """Do not persist sensitive transport errors when a poll fails."""
        sessions = self._migrate()
        with sessions.begin() as db:
            db.add(DiscoverySource(name="Bad", kind="unifi", config_name="home"))

        class FailingAdapter:
            """Stand-in collector whose errors deliberately contain fake secrets."""

            def poll(self, settings):
                """Raise a transport error that includes a token-like string."""
                raise RuntimeError("Authorization Bearer abc123 TEST_SECRET")

        result = poll_once(sessions, {"integrations": {"unifi": {"home": {"token": "TEST_SECRET"}}}}, {"unifi": FailingAdapter()})
        self.assertEqual(result, 0)
        with sessions() as db:
            events = db.scalars(select(AuditEvent)).all()
            self.assertEqual(events[0].action, "discovery.collection.failed")
            self.assertNotIn("TEST_SECRET", events[0].details_json)
            self.assertNotIn("TEST_SECRET", db.scalar(select(DiscoverySource)).last_error)

    def test_audit_entries_are_append_only_and_transactional(self) -> None:
        """Record user actions and reject SQL UPDATE/DELETE of audit history."""
        sessions = self._migrate()
        with sessions.begin() as db:
            event = record_audit_event(
                db, actor_kind="user", actor_id=7, actor_name="admin",
                action="placement.moved", subject_kind="device", subject_id=42,
                details={"from_container": 1, "to_container": 2},
            )
            db.flush()
            event_id = event.id
        with sessions() as db:
            saved = db.get(AuditEvent, event_id)
            self.assertEqual(saved.actor_kind, "user")
            self.assertEqual(saved.subject_id, 42)
            self.assertIsNotNone(saved.occurred_at)
            self.assertEqual(json.loads(saved.details_json)["to_container"], 2)
        with self.assertRaises(IntegrityError):
            with sessions.begin() as db:
                db.execute(text("UPDATE audit_events SET action='forged' WHERE id=:id"), {"id": event_id})
        with self.assertRaises(IntegrityError):
            with sessions.begin() as db:
                db.execute(text("DELETE FROM audit_events WHERE id=:id"), {"id": event_id})
        with sessions() as db:
            self.assertEqual(db.get(AuditEvent, event_id).action, "placement.moved")
        with self.assertRaises(ValueError):
            with sessions.begin() as db:
                record_audit_event(db, actor_kind="user", action="test", subject_kind="device", details={"nested": {"api_key": "LEAK"}})
        with self.assertRaises(RuntimeError):
            with sessions.begin() as db:
                record_audit_event(db, actor_kind="system", action="test", subject_kind="device")
                raise RuntimeError("rollback")
        with sessions() as db:
            self.assertEqual(len(db.scalars(select(AuditEvent)).all()), 1)

    def test_downgrade_restores_step2_schema(self) -> None:
        """Undo all newly added tables and retain preexisting inventory."""
        sessions = self._migrate()
        with sessions.begin() as db:
            _, _, _, host = self._create_sample(db)
            host_id = host.id
        command.downgrade(self.alembic_config, "0001_initial")
        engine = self._connect()
        tables = set(inspect(engine).get_table_names())
        self.assertNotIn("audit_events", tables)
        self.assertNotIn("attributes", tables)
        self.assertNotIn("placement_containers", tables)
        with engine.connect() as con:
            self.assertEqual(con.execute(text("SELECT name FROM devices WHERE id=:id"), {"id": host_id}).scalar(), "one")


if __name__ == "__main__":
    unittest.main()
