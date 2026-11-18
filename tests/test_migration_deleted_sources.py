# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check that the migration clears source rows left by deleted integrations."""

import json
import unittest

from alembic import command
from sqlalchemy import select

import test_step4a as fixture
from labrat.audit import record_audit_event
from labrat.database import make_session_factory
from labrat.discovery.models import DiscoveredDevice, DiscoverySource
from labrat.metadata import Attribute, InventoryObject
from labrat.models import Device


class DeletedSourceMigrationTests(unittest.TestCase):
    """Run 0016 over a database holding deleted, switched-off, and live sources."""

    setUp = fixture.Step4ATests.setUp
    _connect = fixture.Step4ATests._connect

    def source(self, db, kind, name, *, enabled=True, actions=()):
        """Add a source with one discovered device and the given audit trail.

        Args:
            db: The open session.
            kind: Source kind.
            name: Config name.
            enabled: Whether collection is on.
            actions: Audit actions recorded for it, oldest first.

        Returns:
            int: The source's ID.
        """
        source = DiscoverySource(name=f'{kind}:{name}', kind=kind, config_name=name, enabled=enabled)
        db.add(source)
        db.flush()
        db.add(DiscoveredDevice(source_id=source.id, remote_id='device', name='Reported', category='switch'))
        for action in actions:
            record_audit_event(db, actor_kind='user', actor_name='admin', action=action,
                               subject_kind='discovery_source', subject_id=source.id, details={})
        return source.id

    def test_removes_deleted_sources_only(self):
        """Deleted sources and their clients go; switched-off and live ones stay."""
        command.upgrade(self.alembic_config, '0015_user_session_version')
        sessions = make_session_factory(self._connect())
        with sessions.begin() as db:
            gone = self.source(db, 'unifi', 'gone', enabled=False,
                               actions=('integration.created', 'integration.deleted'))
            clients = self.source(db, 'unifi_clients', 'gone')
            off = self.source(db, 'unifi', 'off', enabled=False,
                              actions=('integration.deleted', 'integration.updated'))
            live = self.source(db, 'proxmox', 'live')
            device = Device(name='Curated')
            db.add(device)
            db.flush()
            owner = db.scalar(select(InventoryObject.id).where(InventoryObject.device_id == device.id))
            for target in (gone, live):
                db.add(Attribute(owner_id=owner, key='discovery.lldp_port_match', value=json.dumps({'json': {
                    'source_id': target, 'target_source_id': target, 'chassis_id': 'chassis', 'port_id': str(target),
                    'target_device_remote_id': 'device', 'target_port_remote_id': '1'}})))
        command.upgrade(self.alembic_config, 'head')
        with sessions() as db:
            remaining = set(db.scalars(select(DiscoverySource.id)))
            self.assertEqual(remaining, {off, live})
            self.assertNotIn(clients, remaining)
            self.assertEqual(set(db.scalars(select(DiscoveredDevice.source_id))), {off, live})
            matches = [json.loads(value)['json']['source_id'] for value in db.scalars(
                select(Attribute.value).where(Attribute.key == 'discovery.lldp_port_match'))]
            self.assertEqual(matches, [live])
            self.assertEqual(db.scalar(select(Device.name)), 'Curated')


if __name__ == '__main__':
    unittest.main()
