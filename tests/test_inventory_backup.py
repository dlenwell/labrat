# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise real backup routes against isolated, migrated inventory databases."""

import copy
import io
import json
import re
import unittest
from uuid import uuid4

from sqlalchemy import select

import test_step4c as fixture
from labrat.audit import AuditEvent
from labrat.discovery.models import DiscoveredDevice, DiscoverySource
from labrat.inventory_backup import BackupError, canonical, decode_backup, fingerprint, restore_backup, snapshot
from labrat.metadata import Attribute, InventoryObject
from labrat.models import Device, DeviceGroup, Group, Site, User, Interface, InterfaceLink, Network, InterfaceNetwork, IPAddress
from labrat.discovery.models import InterfaceLinkPolicy
from labrat.placement import DevicePlacement


class InventoryBackupTests(unittest.TestCase):
    """Protect inventory, authorization, preview integrity, and secret boundaries."""

    setUp = fixture.Step4CTests.setUp
    post = fixture.Step4CTests.post
    create_locations = fixture.Step4CTests.create_locations
    add_observations = fixture.Step4CTests.add_observations

    def seed(self):
        """Create a rack-mounted canonical device with curated metadata and evidence.

        Returns:
            tuple: The site's ID and the device's ID.
        """
        site, room, rack = self.create_locations()
        first, _ = self.add_observations()
        response = self.post('/api/v1/placements', {
            'discovery_id': first, 'container_id': rack, 'rack_u': 4,
            'height_u': 2, 'face': 'front',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        with self.factory.begin() as db:
            device = db.scalar(select(Device))
            device.name = 'Curated label'
            owner = db.scalar(select(InventoryObject).where(InventoryObject.device_id == device.id))
            db.add(Attribute(owner_id=owner.id, key='note', value='{"str":"Keep this annotation"}'))
            group = Group(name='Reviewed')
            db.add(group)
            db.flush()
            db.add(DeviceGroup(device_id=device.id, group_id=group.id, source='manual'))
            device_id = device.id
        return site, device_id

    def download(self):
        """Export through the admin HTML form and return its decoded payload.

        Returns:
            dict: The backup.
        """
        response = self.client.post('/settings/backup/export', data={'_csrf_token': self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment;', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return response.json

    def preview(self, data):
        """Upload a backup through the multipart preview form.

        Args:
            data: The backup to upload.

        Returns:
            The preview response.
        """
        return self.client.post('/settings/backup/preview', data={
            '_csrf_token': self.csrf,
            'backup': (io.BytesIO(canonical(data).encode()), 'inventory.json'),
        })

    def token(self, response):
        """Extract the signed opaque restore reference from a successful preview.

        Args:
            response: A successful preview response.

        Returns:
            str: The restore token from its form.
        """
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return re.search(r'name="restore_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)

    def apply(self, token, **extra):
        """Confirm a previously reviewed inventory restore.

        Args:
            token: The preview's restore token.
            **extra: Form fields added to or overriding the confirmation.

        Returns:
            The restore response.
        """
        return self.client.post('/settings/backup/restore', data={
            '_csrf_token': self.csrf, 'confirm': 'restore', 'restore_token': token, **extra,
        })

    def test_backup_upload_may_exceed_the_app_wide_request_limit(self):
        backup = self.download()
        backup['padding'] = 'x' * (400 * 1024)
        response = self.preview(backup)
        self.assertNotEqual(response.status_code, 413)

    def test_round_trip_preview_rollback_restore_and_replay(self):
        """Preview writes nothing; confirmation reverts backed-up state and is one-use."""
        _, device_id = self.seed()
        backup = self.download()
        expected = {key: backup[key] for key in ('tables', 'attributes', 'source_namespaces', 'discovery_associations')}
        with self.factory.begin() as db:
            db.get(Device, device_id).name = 'Changed after export'
        with self.factory() as db:
            before = fingerprint(db)
            audits = list(db.scalars(select(AuditEvent.id)))
        token = self.token(self.preview(backup))
        with self.factory() as db:
            self.assertEqual(fingerprint(db), before)
            self.assertEqual(list(db.scalars(select(AuditEvent.id))), audits)
        self.assertEqual(self.apply(token).status_code, 302)
        with self.factory() as db:
            self.assertEqual(snapshot(db), expected)
            self.assertEqual(db.get(Device, device_id).name, 'Curated label')
            self.assertIsNotNone(db.scalar(select(DiscoveredDevice)))
            self.assertEqual(len(list(db.scalars(select(AuditEvent.id)))), len(audits) + 1)
        self.assertEqual(self.apply(token).status_code, 400)

    def test_stale_preview_and_other_session_are_rejected(self):
        """Inventory edits or another browser session cannot reuse a restore preview."""
        site, _ = self.seed()
        backup = self.download()
        token = self.token(self.preview(backup))
        with self.factory.begin() as db:
            db.get(Site, site).name = 'Renamed after preview'
        self.assertIn('Inventory changed', self.apply(token).get_data(as_text=True))
        token = self.token(self.preview(backup))
        with self.client.session_transaction() as state:
            state['csrf_token'] = 'another-browser-token'
        self.csrf = 'another-browser-token'
        self.assertIn('another sign-in', self.apply(token).get_data(as_text=True))

    def test_conflicting_layout_is_atomic(self):
        """A collision rejects the whole backup, including earlier label changes."""
        _, device_id = self.seed()
        backup = self.download()
        other = copy.deepcopy(backup['tables']['devices'][0])
        other['id'] = device_id + 1
        other['name'] = 'Overlapping equipment'
        backup['tables']['devices'].append(other)
        place = dict(backup['tables']['device_placements'][0], device_id=other['id'])
        backup['tables']['device_placements'].append(place)
        backup['tables']['devices'][0]['name'] = 'Must roll back'
        with self.factory() as db:
            before = fingerprint(db)
        response = self.preview(backup)
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        with self.factory() as db:
            self.assertEqual(fingerprint(db), before)

    def test_extra_inventory_is_preserved_and_unplaced_state_restored(self):
        """Restore clears a backed-up device's newer placement but keeps new devices."""
        _, device_id = self.seed()
        backup = self.download()
        backup['tables']['device_placements'] = []
        device = backup['tables']['devices'][0]
        for field in ('room_id', 'rack_id', 'rack_u', 'placement'):
            device[field] = None
        with self.factory.begin() as db:
            extra = Device(name='New equipment')
            db.add(extra)
            db.flush()
            extra_id = extra.id
        response = self.preview(backup)
        self.assertIn('1 current equipment placements', response.get_data(as_text=True))
        self.assertEqual(self.apply(self.token(response)).status_code, 302)
        with self.factory() as db:
            self.assertIsNone(db.get(DevicePlacement, device_id))
            self.assertIsNotNone(db.get(Device, extra_id))

    def test_other_populated_instance_refused(self):
        """A foreign UUID cannot apply numeric IDs to unrelated curated inventory."""
        self.seed()
        backup = self.download()
        backup['instance_id'] = str(uuid4())
        response = self.preview(backup)
        self.assertEqual(response.status_code, 400)
        self.assertIn('empty inventory', response.get_data(as_text=True))

    def test_fresh_restore_remaps_sources_and_registry_owners(self):
        """Fresh inventory accepts a portable backup and retains typed attributes."""
        self.seed()
        backup = self.download()
        # A second migrated database has account rows but no curated inventory.
        second = fixture.Step4CTests()
        second.setUp()
        self.addCleanup(second.doCleanups)
        with second.factory.begin() as db:
            db.add(DiscoverySource(name='unrelated', kind='other', config_name='default'))
            db.flush()
            summary = restore_backup(db, decode_backup(canonical(backup).encode()), str(uuid4()))
            self.assertFalse(summary['same_instance'])
        with second.factory() as db:
            attribute = db.scalar(select(Attribute))
            self.assertEqual(attribute.key, 'note')
            self.assertEqual(db.scalar(select(Device.name)), 'Curated label')
            self.assertIsNone(db.scalar(select(DiscoveredDevice)))

    def test_complete_inventory_graph_and_source_mapping(self):
        """Preserve interface ownership, cables, networks, and saved source matches."""
        site_id, device_id = self.seed()
        with self.factory.begin() as db:
            first = Interface(device_id=device_id, name='eth-a', kind='physical')
            second = Interface(device_id=device_id, name='eth-b', kind='physical')
            network = Network(site_id=site_id, name='Example network', vlan_id=20)
            db.add_all([first, second, network])
            db.flush()
            cable = InterfaceLink(a_interface_id=first.id, b_interface_id=second.id)
            db.add(cable)
            db.flush()
            db.add(InterfaceLinkPolicy(interface_link_id=cable.id, manual_locked=True))
            db.add(InterfaceNetwork(interface_id=first.id, network_id=network.id))
            db.add(IPAddress(interface_id=first.id, address='192.0.2.1', prefix_length=24))
            source_id = db.scalar(select(DiscoverySource.id))
            owner_id = db.scalar(select(InventoryObject.id).where(InventoryObject.device_id == device_id))
            db.add(Attribute(owner_id=owner_id, key='discovery.lldp_port_match', value=canonical({'json': {
                'source_id': source_id, 'target_source_id': source_id,
                'chassis_id': 'example-chassis', 'port_id': '1',
                'target_device_remote_id': 'switch-a', 'target_port_remote_id': '1',
            }})))
        backup = self.download()
        token = self.token(self.preview(backup))
        self.assertEqual(self.apply(token).status_code, 302)
        second = fixture.Step4CTests()
        second.setUp()
        self.addCleanup(second.doCleanups)
        with second.factory.begin() as db:
            db.add(DiscoverySource(name='other', kind='other', config_name='default'))
            db.flush()
            restore_backup(db, decode_backup(canonical(backup).encode()), str(uuid4()))
        with second.factory() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == 'unifi'))
            self.assertNotEqual(source.id, source_id)
            saved = db.scalar(select(Attribute.value).where(Attribute.key == 'discovery.lldp_port_match'))
            self.assertEqual(json.loads(saved)['json']['source_id'], source.id)
            self.assertTrue(db.scalar(select(InterfaceLinkPolicy.manual_locked)))
            self.assertEqual(db.scalar(select(IPAddress.address)), '192.0.2.1')

    def test_fresh_route_adopts_lineage_and_allows_later_restore(self):
        """A recovered installation can use older backups of its recovered inventory."""
        self.seed()
        backup = self.download()
        second = InventoryBackupTests()
        second.setUp()
        self.addCleanup(second.doCleanups)
        token = second.token(second.preview(backup))
        self.assertEqual(second.apply(token).status_code, 302)
        exported = second.download()
        self.assertEqual(exported['instance_id'], backup['instance_id'])
        second.token(second.preview(backup))

    def test_permissions_csrf_and_explicit_confirmation(self):
        """Only administrators with valid CSRF and explicit confirmation may restore."""
        self.seed()
        backup = self.download()
        token = self.token(self.preview(backup))
        self.assertEqual(self.apply(token, confirm='').status_code, 409)
        self.assertEqual(self.client.post('/settings/backup/export').status_code, 400)
        with self.factory.begin() as db:
            db.scalar(select(User)).is_admin = False
        for route in ('export', 'preview', 'restore'):
            self.assertEqual(self.client.post('/settings/backup/' + route, data={'_csrf_token': self.csrf}).status_code, 403)
        self.assertEqual(self.client.get('/settings?panel=backup').status_code, 403)
        self.assertNotIn('Backup and restore', self.client.get('/settings').get_data(as_text=True))

    def test_upload_validation_and_secret_exclusion(self):
        """Exports use whitelists; malformed, dangling and oversized files are refused."""
        self.seed()
        backup = self.download()
        text = canonical(backup)
        for forbidden in ('password', 'secret_key', 'local_credentials', 'details_json', 'last_seen_at', 'audit_events', 'observation_ignores'):
            self.assertNotIn(forbidden, text)
        variants = []
        bad = copy.deepcopy(backup)
        bad['version'] = 99
        variants.append(bad)
        bad = copy.deepcopy(backup)
        bad['tables']['devices'][0]['parent_id'] = 9999
        variants.append(bad)
        bad = copy.deepcopy(backup)
        bad['tables']['sites'][0]['id'] = True
        variants.append(bad)
        bad = copy.deepcopy(backup)
        bad['tables']['sites'][0]['secret_key'] = 'unexpected'
        variants.append(bad)
        bad = copy.deepcopy(backup)
        bad['tables']['placement_containers'][0]['parent_id'] = bad['tables']['placement_containers'][0]['id']
        variants.append(bad)
        for variant in variants:
            with self.subTest(variant=variants.index(variant)):
                self.assertIn(self.preview(variant).status_code, (400, 409))
        for raw in (b'not json', b'{"format":1,"format":2}', b' ' * (5 * 1024 * 1024 + 1)):
            with self.assertRaises(BackupError):
                decode_backup(raw)


if __name__ == '__main__':
    unittest.main()
