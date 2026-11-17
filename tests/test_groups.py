# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Test device groups, multi-source membership, and migration integrity.

Every test uses its own disposable SQLite database and runs Alembic in that
isolated directory. These tests never require controller credentials or network
access, and they never modify the developer's real LabRat inventory.
"""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from labrat.audit import AuditEvent, record_audit_event
from labrat.config import load_local_config
from labrat.database import make_engine, make_session_factory
from labrat.models import Device, DeviceGroup, Group


class DeviceGroupTests(unittest.TestCase):
    """Exercise durable device classifications without external dependencies."""

    def setUp(self) -> None:
        """Configure a private temporary database for this individual test."""
        temporary = TemporaryDirectory(prefix="labrat-groups-test-")
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "inventory.sqlite3"
        config_path = Path(temporary.name) / "local_config.toml"
        config_path.write_text(
            f"[database]\npath = {json.dumps(str(self.database_path))}\n", encoding="utf-8"
        )
        environment = patch.dict(os.environ, {"LABRAT_CONFIG": str(config_path)})
        environment.start()
        self.addCleanup(environment.stop)
        self.alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        self.engine = None

    def _sessions(self):
        """Upgrade all migrations and return a session factory for the test.

        Returns:
            sessionmaker: Sessions on the migrated test database.
        """
        command.upgrade(self.alembic_config, "head")
        self.engine = make_engine(load_local_config())
        self.addCleanup(self.engine.dispose)
        return make_session_factory(self.engine)

    def test_arbitrary_groups_and_multiple_memberships(self) -> None:
        """Assign any number of user-created group names to one device."""
        sessions = self._sessions()
        with sessions.begin() as db:
            device = Device(name="UDM Pro SE")
            db.add(device)
            db.flush()
            for name in ("gateway", "firewall", "switch", "experimental"):
                group = Group(name=name)
                db.add(group)
                db.flush()
                db.add(DeviceGroup(device_id=device.id, group_id=group.id, source="manual"))
            device_id = device.id
        with sessions() as db:
            self.assertEqual(
                set(db.scalars(select(Group.name).join(DeviceGroup).where(DeviceGroup.device_id == device_id))),
                {"gateway", "firewall", "switch", "experimental"},
            )

    def test_discovery_and_manual_claims_can_coexist(self) -> None:
        """Retain a manual group even after a collector withdraws its assertion."""
        sessions = self._sessions()
        with sessions.begin() as db:
            device = Device(name="gateway")
            group = Group(name="switch")
            db.add_all([device, group])
            db.flush()
            ids = device.id, group.id
            db.add_all([
                DeviceGroup(device_id=ids[0], group_id=ids[1], source="manual"),
                DeviceGroup(device_id=ids[0], group_id=ids[1], source="unifi:default"),
            ])
        with sessions.begin() as db:
            discovered = db.get(DeviceGroup, (*ids, "unifi:default"))
            db.delete(discovered)
        with sessions() as db:
            memberships = db.scalars(select(DeviceGroup).where(DeviceGroup.device_id == ids[0])).all()
            self.assertEqual(len(memberships), 1)
            self.assertEqual(memberships[0].source, "manual")

    def test_unique_case_insensitive_names_and_source_keys(self) -> None:
        """Reject duplicate group labels and same-source duplicate membership."""
        sessions = self._sessions()
        with sessions.begin() as db:
            db.add(Group(name="router"))
        with self.assertRaises(IntegrityError):
            with sessions.begin() as db:
                db.add(Group(name="Router"))
                db.flush()
        with sessions.begin() as db:
            device = Device(name="one")
            db.add(device)
            group = db.scalar(select(Group).where(Group.name == "router"))
            db.flush()
            ids = device.id, group.id
            db.add(DeviceGroup(device_id=ids[0], group_id=ids[1], source="manual"))
        with self.assertRaises(IntegrityError):
            with sessions.begin() as db:
                db.add(DeviceGroup(device_id=ids[0], group_id=ids[1], source="manual"))
                db.flush()

    def test_referential_integrity_and_cascade(self) -> None:
        """Reject orphaned memberships and cascade on group or device deletion."""
        sessions = self._sessions()
        with self.assertRaises(IntegrityError):
            with sessions.begin() as db:
                db.add(DeviceGroup(device_id=99999, group_id=99999, source="manual"))
                db.flush()
        with sessions.begin() as db:
            device = Device(name="two")
            group = Group(name="switch")
            db.add_all([device, group])
            db.flush()
            db.add(DeviceGroup(device_id=device.id, group_id=group.id))
            group_id = group.id
        with sessions.begin() as db:
            db.delete(db.get(Group, group_id))
        with sessions() as db:
            self.assertEqual(db.scalars(select(DeviceGroup)).all(), [])

    def test_audit_helper_can_capture_user_assignment(self) -> None:
        """Record a membership change in the same transaction as the write."""
        sessions = self._sessions()
        with sessions.begin() as db:
            device = Device(name="three")
            group = Group(name="experimental")
            db.add_all([device, group])
            db.flush()
            db.add(DeviceGroup(device_id=device.id, group_id=group.id, source="manual"))
            record_audit_event(
                db, actor_kind="user", actor_id=1, action="device.group.added",
                subject_kind="device", subject_id=device.id,
                details={"group": group.name, "source": "manual"},
            )
        with sessions() as db:
            events = db.scalars(select(AuditEvent).where(AuditEvent.action == "device.group.added")).all()
            self.assertEqual(len(events), 1)

    def test_downgrade_preserves_prior_inventory(self) -> None:
        """Remove only group tables when downgrading from revision 0003."""
        sessions = self._sessions()
        with sessions.begin() as db:
            db.add(Device(name="four"))
        command.downgrade(self.alembic_config, "0002_discovery_foundation")
        self.assertNotIn("groups", inspect(self.engine).get_table_names())
        self.assertNotIn("device_groups", inspect(self.engine).get_table_names())
        with sessions() as db:
            self.assertEqual(db.scalar(select(Device.name)), "four")
