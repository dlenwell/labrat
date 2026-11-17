# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Verify the initial SQLite inventory schema and Alembic migrations.

These tests use temporary local databases and never contact an external service
or modify the developer's real ``local_config.toml`` or SQLite inventory.
Run with ``python -m unittest discover -s tests -v``.
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
from sqlalchemy.exc import IntegrityError

from labrat.config import load_local_config
from labrat.database import Base, database_path, make_engine, make_session_factory
from labrat.models import (
    AuthIdentity,
    Device,
    DeviceManagementInterface,
    Interface,
    InterfaceLink,
    InterfaceMembership,
    InterfaceNetwork,
    LocalCredential,
    Network,
    Rack,
    Room,
    Site,
    User,
)


class DatabaseTests(unittest.TestCase):
    """Exercise database setup, migration, and core relational constraints."""

    def setUp(self) -> None:
        """Prepare a fresh, isolated SQLite file and local TOML configuration."""
        temporary_directory = TemporaryDirectory(prefix="labrat-db-test-")
        self.addCleanup(temporary_directory.cleanup)
        self.db_path = Path(temporary_directory.name) / "inventory.sqlite3"
        config_path = Path(temporary_directory.name) / "local_config.toml"
        config_path.write_text(
            f"[database]\npath = {json.dumps(str(self.db_path))}\n",
            encoding="utf-8",
        )

        environment = patch.dict(os.environ, {"LABRAT_CONFIG": str(config_path)})
        environment.start()
        self.addCleanup(environment.stop)
        self.alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    def test_engine_does_not_create_application_tables(self) -> None:
        """Require explicit Alembic migrations instead of implicit table creation."""
        engine = make_engine(load_local_config())
        try:
            self.assertEqual(inspect(engine).get_table_names(), [])
        finally:
            engine.dispose()

    def test_initial_migration_and_database_settings(self) -> None:
        """Create all model tables and enable foreign keys and WAL journal mode."""
        command.upgrade(self.alembic_config, "head")
        engine = make_engine(load_local_config())
        try:
            tables = set(inspect(engine).get_table_names())
            self.assertEqual(tables, set(Base.metadata.tables) | {"alembic_version"})
            with engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
                self.assertEqual(connection.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")
        finally:
            engine.dispose()

    def test_inventory_relationships_and_multiple_vlans(self) -> None:
        """Persist a rack, child BMC, cable, bond members, and two VLANs."""
        command.upgrade(self.alembic_config, "head")
        engine = make_engine(load_local_config())
        try:
            factory = make_session_factory(engine)
            with factory.begin() as session:
                site = Site(name="Home")
                session.add(site)
                session.flush()
                room = Room(site_id=site.id, name="Basement")
                session.add(room)
                session.flush()
                rack = Rack(room_id=room.id, name="Main Rack", height_u=36)
                session.add(rack)
                session.flush()

                host = Device(name="one", rack_id=rack.id, rack_u=7, height_u=3)
                switch = Device(name="data-switch", room_id=room.id, placement="shelf")
                session.add_all([host, switch])
                session.flush()
                bmc = Device(name="one-amt", parent_id=host.id)
                session.add(bmc)
                session.flush()

                nic = Interface(device_id=host.id, name="enp3s0f0np0", speed_mbps=10000)
                bond = Interface(device_id=host.id, name="bond0", kind="bond")
                port = Interface(device_id=switch.id, name="port1", speed_mbps=10000)
                amt = Interface(device_id=bmc.id, name="amt0", speed_mbps=1000)
                session.add_all([nic, bond, port, amt])
                session.flush()

                session.add(InterfaceMembership(group_interface_id=bond.id, member_interface_id=nic.id))
                session.add(
                    InterfaceLink(
                        a_interface_id=min(nic.id, port.id),
                        b_interface_id=max(nic.id, port.id),
                    )
                )
                session.add(DeviceManagementInterface(device_id=host.id, interface_id=amt.id))
                vlan4 = Network(site_id=site.id, vlan_id=4, name="Ceph storage")
                vlan5 = Network(site_id=site.id, vlan_id=5, name="Ceph sync")
                session.add_all([vlan4, vlan5])
                session.flush()
                session.add_all([
                    InterfaceNetwork(interface_id=bond.id, network_id=vlan4.id),
                    InterfaceNetwork(interface_id=bond.id, network_id=vlan5.id),
                ])

            with factory() as session:
                saved_bond = session.scalar(select(Interface).where(Interface.name == "bond0"))
                memberships = session.scalars(
                    select(InterfaceNetwork).where(InterfaceNetwork.interface_id == saved_bond.id)
                ).all()
                self.assertEqual(len(memberships), 2)
                self.assertEqual(session.scalar(select(Device).where(Device.name == "one-amt")).parent_id, host.id)
        finally:
            engine.dispose()

    def test_foreign_keys_are_enforced(self) -> None:
        """Reject a device referencing a nonexistent rack."""
        command.upgrade(self.alembic_config, "head")
        engine = make_engine(load_local_config())
        try:
            factory = make_session_factory(engine)
            with self.assertRaises(IntegrityError):
                with factory.begin() as session:
                    session.add(Device(name="ghost", rack_id=999, rack_u=1, height_u=1))
        finally:
            engine.dispose()

    def test_identity_mapping_is_independent_of_passwords(self) -> None:
        """Allow a user to have a future external identity and a local credential."""
        command.upgrade(self.alembic_config, "head")
        engine = make_engine(load_local_config())
        try:
            factory = make_session_factory(engine)
            with factory.begin() as session:
                user = User(username="admin", is_admin=True)
                session.add(user)
                session.flush()
                session.add(AuthIdentity(user_id=user.id, provider="oidc", subject="issuer-user-id"))
                session.add(LocalCredential(user_id=user.id, password_hash="$argon2id$v=19$test-only"))
            with factory() as session:
                self.assertEqual(session.scalar(select(User)).username, "admin")
                self.assertEqual(session.scalar(select(AuthIdentity)).provider, "oidc")
        finally:
            engine.dispose()

    def test_downgrade_returns_to_empty_schema(self) -> None:
        """Ensure the first migration is reversible on an empty inventory."""
        command.upgrade(self.alembic_config, "head")
        command.downgrade(self.alembic_config, "base")
        engine = make_engine(load_local_config())
        try:
            self.assertFalse(set(Base.metadata.tables) & set(inspect(engine).get_table_names()))
        finally:
            engine.dispose()

    def test_database_path_follows_local_configuration(self) -> None:
        """Resolve a configured path without changing the project's working dir."""
        self.assertEqual(database_path(load_local_config()), self.db_path.resolve())


if __name__ == "__main__":
    unittest.main()
