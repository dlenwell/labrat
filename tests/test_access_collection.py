# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Verify bounded evidence collection and private cache recovery with fake I/O."""

import copy
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests

from labrat import models as inventory_models
from labrat.collector import access_collection as access
from labrat.database import Base, make_engine, make_session_factory
from labrat.discovery.models import DiscoverySource


def page(rows):
    """Build a complete synthetic Integration API page.

    Args:
        rows: The page's records.

    Returns:
        dict: One complete page holding them.
    """
    return {'offset': 0, 'count': len(rows), 'limit': 100, 'totalCount': len(rows), 'data': rows}


class AccessCollectionTests(unittest.TestCase):
    """Exercise source-scoped snapshots without real controllers or network scans."""

    def setUp(self):
        """Prepare a disposable database, site, two subnets, and one allow policy."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {'database': {'path': str(self.root / 'db.sqlite')},
                       'unifi': {'url': 'https://controller.example', 'api_key': 'test-only-key'}}
        self.engine = make_engine(self.config)
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = make_session_factory(self.engine)
        with self.sessions.begin() as db:
            source = DiscoverySource(kind='unifi', name='unifi:default', config_name='default', enabled=True)
            db.add(source)
            db.flush()
            self.source_id = source.id
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.path = Path(str(self.root / 'db.sqlite') + '.access-evidence') / f'source-{self.source_id}.json'
        self.networks = [
            {'id': 'n-a', 'zoneId': 'z-a', 'name': 'Source', 'enabled': True, 'management': 'GATEWAY',
             'ipv4Configuration': {'hostIpAddress': '192.0.2.1', 'prefixLength': 24, 'secret': 'private-extra'}},
            {'id': 'n-b', 'zoneId': 'z-b', 'name': 'Destination', 'enabled': True, 'management': 'GATEWAY',
             'ipv4Configuration': {'hostIpAddress': '198.51.100.1', 'prefixLength': 24}},
        ]
        self.policy = {'id': 'r-a', 'index': 10, 'enabled': True, 'action': {'type': 'ALLOW'},
                       'source': {'zoneId': 'z-a'}, 'destination': {'zoneId': 'z-b'},
                       'ipProtocolScope': {'ipVersion': 'IPV4'}, 'metadata': {'origin': 'USER_DEFINED'}}
        self.responses = {
            '/info': {'applicationVersion': 'example-version'}, '/sites': page([{'id': 'site-a'}]),
            '/sites/site-a/networks': page([{'id': n['id']} for n in self.networks]),
            '/sites/site-a/firewall/zones': page([{'id': 'z-a', 'networkIds': ['n-a']}, {'id': 'z-b', 'networkIds': ['n-b']}]),
            '/sites/site-a/traffic-matching-lists': page([]),
            '/sites/site-a/firewall/policies': page([self.policy]),
            '/sites/site-a/firewall/policies/ordering': {'orderedFirewallPolicyIds': {'beforeSystemDefined': ['r-a'], 'afterSystemDefined': []}},
            **{f'/sites/site-a/networks/{n["id"]}': n for n in self.networks},
        }
        self.client = Mock()
        self.client.get_json.side_effect = self.get
        self.collector = access.AccessCollector(self.client, route_lookup=self.route)
        state = patch.object(access._LOG, 'disabled', False)
        state.start()
        self.addCleanup(state.stop)

    def get(self, url, headers, verify):
        """Return independent synthetic objects while checking header-only credentials.

        Args:
            url: The requested URL.
            headers: Request headers; must carry the test key.
            verify: TLS verification; must be on.

        Returns:
            object: A copy of the scripted response for that path.

        Raises:
            Exception: The scripted exception, when the path has one.
        """
        self.assertEqual(headers['X-API-Key'], 'test-only-key')
        self.assertNotIn('test-only-key', url)
        self.assertTrue(verify)
        path = urlsplit(url).path.removeprefix('/proxy/network/integration/v1')
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def route(self, target):
        """Supply local or gateway route evidence without executing commands.

        Args:
            target: The destination address.

        Returns:
            dict: A route record through a test interface.
        """
        return {'dev': 'example0', 'prefsrc': '192.0.2.10',
                **({'gateway': '192.0.2.1'} if target.startswith('198.51.100.') else {})}

    def collect(self):
        """Build a snapshot with this test's injected readers.

        Returns:
            dict: The evidence snapshot.
        """
        return self.collector.collect(self.config['unifi'], source=str(self.source_id), now=self.now)

    def test_complete_snapshot_and_cache_reuse(self):
        """Persist minimal evidence privately, reuse it, and expire decisions on read."""
        self.assertEqual(self.collector.poll(self.sessions, self.config, now=self.now), 1)
        snapshot = access.read_snapshot(self.path, now=self.now)
        destination = snapshot['sites'][0]['candidates'][1]
        self.assertEqual(destination['profiles']['nut']['access'], 'allowed')
        self.assertEqual(destination['routable'], 'unknown')
        self.assertEqual(destination['route']['local_route'], 'yes')
        self.assertEqual(snapshot['sites'][0]['candidates'][0]['profiles']['nut']['access'], 'unknown')
        self.assertNotIn('test-only-key', self.path.read_text())
        self.assertNotIn('private-extra', self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.client.get_json.reset_mock()
        self.assertEqual(self.collector.poll(self.sessions, self.config, now=self.now + timedelta(seconds=60)), 0)
        self.client.get_json.assert_not_called()
        expired = access.read_snapshot(self.path, now=self.now + timedelta(seconds=901))
        self.assertFalse(expired['available'])
        self.assertEqual(expired['collected_at'], snapshot['collected_at'])
        self.assertEqual(expired['sites'][0]['candidates'][1]['profiles']['nut']['access'], 'unknown')
        self.assertEqual(self.collector.poll(self.sessions, self.config, now=self.now + timedelta(seconds=901)), 1)
        self.collector.close()
        self.client.session.close.assert_called_once()

    def test_network_candidates_survive_missing_firewall_permission(self):
        """Unsupported policy APIs cannot prevent subnet inventory or normal discovery."""
        self.responses['/sites/site-a/firewall/policies'] = requests.HTTPError('private-response')
        snapshot = self.collect()
        self.assertTrue(snapshot['available'])
        site = snapshot['sites'][0]
        self.assertFalse(site['policy_complete'])
        self.assertEqual(len(site['candidates']), 2)
        self.assertTrue(all(c['profiles']['nut']['access'] == 'unknown' for c in site['candidates']))

    def test_order_membership_and_policy_mutation_remain_unknown(self):
        """Never assert access for contradictory zones, order, or a changing policy set."""
        self.responses['/sites/site-a/firewall/policies/ordering']['orderedFirewallPolicyIds']['beforeSystemDefined'] = []
        self.assertEqual(self.collect()['sites'][0]['candidates'][1]['profiles']['nut']['access'], 'unknown')
        self.responses['/sites/site-a/firewall/policies/ordering']['orderedFirewallPolicyIds']['beforeSystemDefined'] = ['r-a']
        self.responses['/sites/site-a/firewall/zones']['data'][0]['networkIds'].append('n-b')
        self.assertEqual(self.collect()['sites'][0]['candidates'][1]['profiles']['nut']['access'], 'unknown')
        self.responses['/sites/site-a/firewall/zones']['data'][0]['networkIds'] = ['n-a']
        original = self.get
        calls = [0]
        def changing(url, headers, verify):
            """Change the second policy list while leaving all other facts stable."""
            value = original(url, headers, verify)
            if '/firewall/policies?' in url:
                calls[0] += 1
                if calls[0] == 2:
                    value['data'][0]['action']['type'] = 'BLOCK'
            return value
        self.client.get_json.side_effect = changing
        self.assertFalse(self.collect()['sites'][0]['policy_complete'])

    def test_failure_retains_candidates_invalidates_decisions_and_throttles(self):
        """Refresh errors preserve evidence age, redact logs, and avoid retry storms."""
        self.collector.poll(self.sessions, self.config, now=self.now)
        self.client.get_json.side_effect = requests.ConnectionError('private-token-controller')
        later = self.now + timedelta(seconds=901)
        with self.assertLogs(access._LOG, level=logging.WARNING) as logs:
            self.assertEqual(self.collector.poll(self.sessions, self.config, now=later), 0)
        self.assertNotIn('private-token-controller', str(logs.output))
        snapshot = access.read_snapshot(self.path, now=later)
        self.assertFalse(snapshot['available'])
        self.assertEqual(snapshot['collected_at'], self.now.isoformat())
        self.assertEqual(snapshot['sites'][0]['candidates'][1]['profiles']['nut']['access'], 'unknown')
        self.client.get_json.reset_mock()
        self.collector.poll(self.sessions, self.config, now=later + timedelta(seconds=1))
        self.client.get_json.assert_not_called()
        with patch.object(access, '_write_snapshot', side_effect=OSError('private-path')), self.assertLogs(access._LOG):
            self.collector.poll(self.sessions, self.config, now=later + timedelta(seconds=901))

    def test_disabled_removed_invalid_and_edited_settings(self):
        """Source flags/config edits control refresh independently of cached observations."""
        for interval in [0, True, 1, 86401]:
            self.config['unifi']['access_interval_seconds'] = interval
            self.assertEqual(self.collector.poll(self.sessions, self.config, now=self.now), 0)
        self.config['unifi'].pop('access_interval_seconds')
        with self.sessions.begin() as db:
            db.get(DiscoverySource, self.source_id).enabled = False
        self.collector.poll(self.sessions, self.config, now=self.now)
        self.client.get_json.assert_not_called()
        with self.sessions.begin() as db:
            db.get(DiscoverySource, self.source_id).enabled = True
        self.collector.poll(self.sessions, {'database': self.config['database']}, now=self.now)
        self.client.get_json.assert_not_called()
        self.collector.poll(self.sessions, self.config, now=self.now)
        self.config['unifi']['display_name'] = 'Renamed'
        self.assertEqual(self.collector.poll(self.sessions, self.config, now=self.now), 1)

    def test_pagination_rejects_incomplete_duplicates_and_changed_totals(self):
        """Incomplete pages cannot masquerade as a full rule or subnet inventory."""
        original = copy.deepcopy(self.responses['/sites'])
        for value in [[], {'data': [], 'totalCount': -1, 'offset': 0, 'count': 0},
                      {**original, 'count': 9}, {**original, 'offset': 1},
                      {**original, 'totalCount': 0}, {**original, 'totalCount': 2001},
                      {**original, 'data': [], 'count': 0, 'totalCount': 1},
                      page([{'id': 'same'}, {'id': 'same'}]), page([{'id': ''}])]:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError, KeyError)):
                self.responses['/sites'] = value
                self.collect()
        self.responses['/sites'] = original
        with patch.object(access, 'MAX_REQUESTS', 1), self.assertRaises(ValueError):
            self.collect()
        with patch.object(access.time, 'monotonic', side_effect=[0, 31]), self.assertRaises(ValueError):
            self.collect()

    def test_multiple_policy_pages_and_changing_totals(self):
        """Follow real page boundaries and reject a total that changes mid-read."""
        policies = [{**copy.deepcopy(self.policy), 'id': f'r-{i}', 'index': i} for i in range(101)]
        self.responses['/sites/site-a/firewall/policies/ordering']['orderedFirewallPolicyIds']['beforeSystemDefined'] = [p['id'] for p in policies]
        original = self.get
        changing = [False]
        def paginated(url, headers, verify):
            """Serve two pages or an inconsistent second total without network I/O."""
            if '/firewall/policies?' in url:
                offset = 100 if 'offset=100' in url else 0
                rows = policies[offset:offset + 100]
                return {'offset': offset, 'count': len(rows), 'totalCount': 102 if changing[0] and offset else 101, 'data': rows}
            return original(url, headers, verify)
        self.client.get_json.side_effect = paginated
        site = self.collect()['sites'][0]
        self.assertTrue(site['policy_complete'])
        self.assertEqual(len(site['rules']), 101)
        self.assertEqual(site['candidates'][1]['profiles']['nut']['access'], 'allowed')
        changing[0] = True
        self.assertFalse(self.collect()['sites'][0]['policy_complete'])

    def test_source_database_failure_and_malformed_detail_are_isolated(self):
        """Optional source reads and malformed private fields fail without leaking data."""
        from sqlalchemy.exc import SQLAlchemyError
        with self.assertLogs(access._LOG) as logs:
            self.assertEqual(self.collector.poll(Mock(side_effect=SQLAlchemyError('private-details')), self.config, now=self.now), 0)
        self.assertNotIn('private-details', str(logs.output))
        for changes in [{'name': {'api_key': 'private'}}, {'enabled': 'false'},
                        {'ipv4Configuration': {'hostIpAddress': [], 'prefixLength': 24}}]:
            self.responses['/sites/site-a/networks/n-b'] = {**self.networks[1], **changes}
            with self.assertRaises(ValueError):
                self.collect()
        self.responses['/info'] = {'applicationVersion': {'api_key': 'private'}}
        with self.assertRaises(ValueError):
            self.collect()

    def test_detail_identity_network_limits_and_unmanaged_candidates(self):
        """Keep unknown CIDRs visible and reject mismatched identities or oversized jobs."""
        self.networks[1].pop('ipv4Configuration')
        self.responses['/sites/site-a/networks/n-b'] = self.networks[1]
        self.assertIsNone(self.collect()['sites'][0]['candidates'][1]['cidr'])
        self.responses['/sites/site-a/networks/n-b'] = {'id': 'different'}
        with self.assertRaises(ValueError):
            self.collect()
        self.responses['/sites'] = page([{'id': f's-{i}'} for i in range(17)])
        with self.assertRaises(ValueError):
            self.collect()
        self.responses['/sites'] = page([{'id': 'site-a'}])
        self.responses['/sites/site-a/networks'] = page([{'id': f'n-{i}'} for i in range(101)])
        with self.assertRaises(ValueError):
            self.collect()

    def test_cache_reader_and_route_command_fail_safely(self):
        """Corrupt/expired caches and missing routing utilities cannot invent access."""
        self.assertFalse(access.read_snapshot(self.path, now=self.now)['available'])
        self.path.parent.mkdir()
        for value in ['bad-json', '[]', '{}', '{"version":1,"sites":[],"expires_at":"invalid"}',
                      '{"version":1,"sites":[],"available":true,"expires_at":"2026-01-01T00:00:00"}']:
            self.path.write_text(value)
            self.assertFalse(access.read_snapshot(self.path, now=self.now)['available'])
        for result in [Mock(stdout='[]'), Mock(stdout='bad-json')]:
            with patch.object(access.subprocess, 'run', return_value=result):
                self.assertEqual(access.local_route('192.0.2.2'), {})
        with patch.object(access.subprocess, 'run', return_value=Mock(stdout='[{"dev":"example0"}]')) as run:
            self.assertEqual(access.local_route('192.0.2.2')['dev'], 'example0')
            self.assertEqual(run.call_args.args[0][-1], '192.0.2.2')
        with patch.object(access.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ip', 2)):
            self.assertEqual(access.local_route('192.0.2.2'), {})


if __name__ == '__main__':
    unittest.main()
