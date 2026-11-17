# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Validate VPN state, telemetry units, optional failures, and secret exclusion."""
import json
from datetime import datetime, timezone
import unittest
import requests
from labrat.collector.adapters.unifi import UniFiAdapter
from labrat.collector.vpn import vpn_snapshot

NOW = datetime(2026, 11, 10, 17, tzinfo=timezone.utc)


class VpnSnapshotTests(unittest.TestCase):
    """Keep configuration separate from observed connectivity and activity."""

    def test_connected_idle_is_not_down_and_secrets_never_leave_parser(self):
        """An idle CONNECTED connection stays connected; persist only approved fields."""
        network = {'_id': 'vpn-1', 'purpose': 'vpn-client', 'name': 'Example tunnel', 'enabled': True,
                   'vpn_type': 'openvpn-client', 'openvpn_configuration': 'private-key-secret',
                   'x_openvpn_password': 'password-secret', 'openvpn_username': 'private-user'}
        row = {'network_id': 'vpn-1', 'status': 'CONNECTED', 'assoc_time': NOW.timestamp() - 3600,
               'rx_rate_bps': 0, 'tx_rate_bps': 0, 'notes': ['private-note'], 'remote_ip': '192.0.2.1'}
        snapshot = vpn_snapshot([row], [network], 0, NOW)
        connection = snapshot['connections'][0]
        self.assertEqual(connection['state'], 'connected')
        self.assertEqual(connection['name'], 'Example tunnel')
        self.assertEqual(connection['connected_at'], NOW.timestamp() - 3600)
        self.assertEqual(connection['rx_bps'], 0)
        self.assertEqual(snapshot['remote_access_sessions'], 0)
        serialized = json.dumps(snapshot)
        for secret in ['private-key-secret', 'password-secret', 'private-user', 'private-note', '192.0.2.1']:
            self.assertNotIn(secret, serialized)

    def test_enabled_profile_without_runtime_is_unknown_not_connected_or_down(self):
        """Neither configuration nor tunnel absence is sufficient to infer an outage."""
        result = vpn_snapshot([], [{'_id': 'a', 'purpose': 'vpn-client', 'enabled': True}], None, NOW)
        self.assertEqual(result['connections'][0]['state'], 'unknown')
        self.assertIsNone(result['connections'][0]['rx_bps'])
        self.assertIsNone(result['remote_access_sessions'])

    def test_explicit_disconnect_and_disabled_profiles_remain_distinct(self):
        """Positive traffic cannot override a reported disconnect or paused profile."""
        runtime = [{'network_id': 'a', 'status': 'DISCONNECTED', 'tx_rate_bps': 1781},
                   {'network_id': 'b', 'status': 'CONNECTED'}]
        configs = [{'_id': 'b', 'purpose': 'vpn-client', 'enabled': False}]
        first, second = vpn_snapshot(runtime, configs, 0, NOW)['connections']
        self.assertEqual(first['state'], 'disconnected')
        self.assertEqual(first['tx_bps'], 1781)  # Already bit/s, never multiply by eight.
        self.assertEqual(second['state'], 'disabled')

    def test_invalid_numbers_and_future_start_times_are_unknown(self):
        """Reject booleans, nonfinite rates, and impossible start times."""
        row = {'network_id': 'a', 'assoc_time': NOW.timestamp() + 60,
               'rx_rate_bps': float('inf'), 'tx_rate_bps': True}
        result = vpn_snapshot([row], [], None, NOW)['connections'][0]
        self.assertIsNone(result['connected_at'])
        self.assertIsNone(result['rx_bps'])
        self.assertIsNone(result['tx_bps'])
        self.assertEqual(result['state'], 'unknown')

    def test_optional_endpoint_failure_retains_only_last_sanitized_reading(self):
        """Unavailability preserves age and cannot fabricate a disconnected tunnel."""
        class Client:
            """Return a minimal read-only fixture, then simulate an unsupported API."""
            failed = False

            def get_json(self, url, headers, verify):
                if self.failed:
                    raise requests.HTTPError('do-not-store-raw-exception')
                if url.endswith('/vpn/connections'):
                    return {'connections': [{'network_id': 'a', 'status': 'CONNECTED'}]}
                return {'data': []}
        client = Client()
        adapter = UniFiAdapter(client)
        first = adapter._vpn_snapshot({'url': 'https://unifi.example'}, 'default', {}, True)
        client.failed = True
        failed = adapter._vpn_snapshot({'url': 'https://unifi.example'}, 'default', {}, True)
        self.assertTrue(first['available'])
        self.assertFalse(failed['available'])
        self.assertEqual(failed['sampled_at'], first['sampled_at'])
        self.assertEqual(failed['connections'][0]['state'], 'connected')
        self.assertNotIn('do-not-store', json.dumps(failed))
        fresh_adapter = UniFiAdapter(client)
        self.assertIsNone(fresh_adapter._vpn_snapshot({'url': 'https://unifi.example'}, 'default', {}, True)['sampled_at'])
