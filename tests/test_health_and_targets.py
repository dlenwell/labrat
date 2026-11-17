# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check stale/error health and exact ignore identities using disposable cache data."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from labrat import models as inventory_models  # Register inventory foreign keys.
from labrat.database import Base
from labrat.discovery.models import DiscoverySource, DiscoveredDevice, DiscoveredPort
from labrat.discovery.observation_ignores import ignore_target
from labrat.pages.status import describe_age, header_sources, source_health, wan_health, _rate_label


class HealthAndTargetTests(unittest.TestCase):
    """Ensure unknown or malformed source data cannot create false identity matches."""

    def setUp(self):
        """Build an in-memory cache independent of the running application."""
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.db.close)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.source = DiscoverySource(name='unifi:test', kind='unifi', config_name='test')
        self.proxmox = DiscoverySource(name='proxmox:test', kind='proxmox', config_name='test')
        self.db.add_all([self.source, self.proxmox])
        self.db.flush()
        self.device = DiscoveredDevice(source_id=self.source.id, remote_id='switch', name='Test switch', category='switch')
        self.db.add(self.device)
        self.db.flush()
        self.port = DiscoveredPort(discovered_device_id=self.device.id, remote_id='1', name='Port 1')
        self.db.add(self.port)
        self.db.flush()

    def test_age_boundaries_and_rate_units(self):
        """Round ages down and format link rates without accepting booleans."""
        self.assertEqual(describe_age(None, self.now), 'never')
        for seconds, label in ((-1, 'just now'), (59, 'just now'), (60, '1 min ago'),
                               (3599, '59 min ago'), (3600, '1 h ago'), (86400, '1 d ago')):
            self.assertEqual(describe_age((self.now - timedelta(seconds=seconds)).replace(tzinfo=None), self.now), label)
        for rate, label in ((None, '--'), (True, '--'), ('10', '--'), (0, '0 b/s'),
                            (1500, '1.5 kb/s'), (100000, '100 kb/s'), (2e6, '2.0 Mb/s'), (3e9, '3.0 Gb/s')):
            self.assertEqual(_rate_label(rate), label)

    def test_source_health_waiting_stale_and_error(self):
        """All registered sources remain visible and errors outrank prior success."""
        waiting = source_health(self.db, self.now)
        self.assertEqual(next(x for x in waiting if x['name'] == 'LLDP')['label'], 'Awaiting UniFi discovery')
        self.source.last_success_at = self.now - timedelta(hours=1)
        self.source.last_error = 'Collection failed: TimeoutError'
        self.proxmox.enabled = False
        health = source_health(self.db, self.now)
        self.assertEqual(next(x for x in health if x['name'] == self.proxmox.name)['state'], 'waiting')
        self.assertEqual(next(x for x in health if x['name'] == self.source.name)['state'], 'error')
        self.assertEqual(next(x for x in health if x['name'] == 'LLDP')['state'], 'error')
        self.assertFalse(next(x for x in health if x['name'] == 'LLDP')['fresh'])
        self.source.last_error = None
        self.source.last_success_at = self.now
        health = source_health(self.db, self.now)
        self.assertEqual(next(x for x in health if x['name'] == 'LLDP')['label'], 'No LLDP neighbors observed')

    def test_header_folds_shared_endpoints_and_pins_wan(self):
        """Fold clients into their controller, keep LLDP apart, shorten names, and put WAN first."""
        self.source.last_success_at = self.now
        self.proxmox.last_success_at = self.now
        clients = DiscoverySource(name='Clients · unifi:test', kind='unifi_clients',
                                  config_name='test', last_error='failed')
        self.db.add(clients)
        self.db.flush()
        health = source_health(self.db, self.now)
        health.append({'name': 'WAN', 'state': 'ready', 'label': 'up', 'fresh': True,
                       'age': 'just now', 'detail': True})
        lights = header_sources(health)
        self.assertEqual([light['name'] for light in lights], ['WAN', 'proxmox', 'unifi', 'LLDP'])
        unifi = lights[2]
        self.assertEqual(unifi['state'], 'error')
        self.assertFalse(unifi['fresh'])
        self.assertEqual([line.split(':')[0] for line in unifi['lines']], ['unifi', 'Clients'])
        self.assertEqual(lights[0]['detail'], 'up')
        self.assertIsNone(unifi['detail'])

    def test_header_keeps_names_distinct_and_orphans_visible(self):
        """Use full names when short ones collide, and keep a child whose parent is gone."""
        lights = header_sources([
            {'name': 'unifi:a', 'state': 'ready', 'label': 'ok', 'fresh': True, 'age': 'now'},
            {'name': 'unifi:b', 'state': 'waiting', 'label': 'wait', 'fresh': False, 'age': 'never'},
            {'name': 'Clients · unifi:c', 'state': 'ready', 'label': 'ok', 'fresh': True,
             'age': 'now', 'parent': 'unifi:c', 'short': 'Clients'},
            {'name': 'Clients · unifi:a', 'state': 'waiting', 'label': 'wait', 'fresh': False,
             'age': 'now', 'parent': 'unifi:a', 'short': 'Clients'},
        ])
        self.assertEqual([light['name'] for light in lights], ['unifi:a', 'unifi:b', 'Clients'])
        self.assertEqual(lights[0]['state'], 'ready')

    def test_lldp_matches_only_valid_cache_identity(self):
        """Malformed JSON and addresses do not hide the one exact server match."""
        self.source.last_success_at = self.now
        for index, identifiers in enumerate(('broken', '[]', '{}', '{"management_ips": [null, "invalid"]}',
                                              '{"management_ips": ["192.0.2.1/24"]}')):
            host = DiscoveredDevice(source_id=self.proxmox.id, remote_id=f'host{index}', name='Test host', category='server', identifiers_json=identifiers)
            self.db.add(host)
            self.db.flush()
            if index == 4:
                self.db.add(DiscoveredPort(discovered_device_id=host.id, remote_id='eth0', name='eth0', mac='02:00:00:00:00:01'))
        for index, details in enumerate(('broken', '[]', '{}')):
            self.db.add(DiscoveredDevice(source_id=self.source.id, remote_id=f'extra{index}', name='Other', category='switch', details_json=details))
        self.device.details_json = json.dumps({'lldp_neighbors': [
            None, {'chassis_id': '02:00:00:00:00:01', 'management_ips': [None, 'invalid', '192.0.2.1/24']},
            {'management_ips': None}]})
        self.db.flush()
        lldp = next(x for x in source_health(self.db, self.now) if x['name'] == 'LLDP')
        self.assertEqual(lldp['label'], '3 neighbors · 1 Proxmox host · 0 Proxmox physical links')
        self.assertTrue(lldp['fresh'])
        self.device.details_json = json.dumps({'lldp_neighbors': [{'management_ips': ['192.0.2.1']}]})
        lldp = next(x for x in source_health(self.db, self.now) if x['name'] == 'LLDP')
        self.assertEqual(lldp['label'], '1 neighbor · 1 Proxmox host · 0 Proxmox physical links')

    def test_wan_health_ignores_malformed_cache_and_tracks_degradation(self):
        """Missing samples stay stale while backup and partial outages stay warnings."""
        self.device.category = 'router'
        for invalid in ('broken', '[]', '{}', '{"wan": []}'):
            self.device.details_json = invalid
            self.db.flush()
            self.assertEqual(wan_health(self.db, self.now), [])
        for uplinks, expected, fragment in (([], 'ready', 'No uplink reported'),
            ([{'enabled': True, 'up': True, 'name': 'WAN1'}], 'ready', 'WAN1'),
            ([{'enabled': True, 'up': True}, {'enabled': True, 'up': False}], 'warning', 'an uplink is down'),
            ([{'enabled': True, 'up': True}, {'enabled': True, 'up': True, 'active': True}], 'warning', 'on backup'),
            ([{'enabled': True, 'up': False}], 'error', 'every uplink is down')):
            self.device.details_json = json.dumps({'wan': {'uplinks': uplinks, 'sampled_at': 'invalid'}})
            self.db.flush()
            entry, = wan_health(self.db, self.now)
            self.assertEqual(entry['state'], expected)
            self.assertIn(fragment, entry['label'])
            self.assertFalse(entry['fresh'])
            self.assertEqual(entry['age'], 'never')

    def test_ignore_targets_reject_malformed_and_extra_scope(self):
        """A policy must reference exactly one existing source, device and port."""
        base = {'condition': 'port-down', 'source_id': self.source.id, 'device_remote_id': 'switch', 'port_remote_id': '1'}
        invalid = [None, [], {}, {**base, 'condition': []}, {**base, 'condition': 'unknown'},
            {**base, 'source_id': True}, {**base, 'source_id': 9999}, {**base, 'device_remote_id': None},
            {**base, 'device_remote_id': 'absent'}, {**base, 'port_remote_id': None},
            {**base, 'port_remote_id': 'absent'}, {**base, 'unexpected': 'x'},
            {'condition': 'source-fault', 'source_id': self.source.id, 'device_remote_id': 'switch'},
            {**base, 'condition': 'server-offline'}, {**base, 'condition': 'wan-down'}, {**base, 'condition': 'ups-alert'}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ignore_target(self.db, payload)
        self.assertEqual(ignore_target(self.db, base), (base, 'Test switch / Port 1'))
        source = {'condition': 'source-overdue', 'source_id': self.source.id}
        self.assertEqual(ignore_target(self.db, source), (source, 'unifi:test'))

    def test_wan_and_ups_targets_require_readings(self):
        """Wrong JSON shapes cannot masquerade as WAN or UPS evidence."""
        for condition, category, key in (('wan-stale', 'router', 'wan'), ('ups-alert', 'ups', 'ups')):
            self.device.category = category
            target = {'condition': condition, 'source_id': self.source.id, 'device_remote_id': 'switch'}
            for details in ('broken', '[]', '{}', json.dumps({key: []})):
                self.device.details_json = details
                with self.subTest(condition=condition, details=details), self.assertRaises(ValueError):
                    ignore_target(self.db, target)
            self.device.details_json = json.dumps({key: {}})
            self.assertEqual(ignore_target(self.db, target)[0], target)

    def test_neighbor_ignore_requires_exact_live_identity(self):
        """A chassis and remote port pair may be ignored only on its reported port."""
        base = {'condition': 'neighbor-unresolved', 'source_id': self.source.id,
                'device_remote_id': 'switch', 'port_remote_id': '1'}
        for extra in ({}, {'chassis_id': 'peer', 'remote_port_id': None}, {'chassis_id': 'peer', 'remote_port_id': 'eth0'}):
            with self.assertRaises(ValueError):
                ignore_target(self.db, {**base, **extra})
        exact = {**base, 'chassis_id': 'peer', 'remote_port_id': ''}
        self.device.details_json = json.dumps({'lldp_neighbors': [None, {'local_port': '1', 'chassis_id': 'peer', 'port_id': ''}]})
        self.assertEqual(ignore_target(self.db, exact)[0], exact)
        for details in ('[]', '{"lldp_neighbors": {}}'):
            self.device.details_json = details
            with self.assertRaises(ValueError):
                ignore_target(self.db, exact)
