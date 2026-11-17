# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Test explicit discovery writes and safe cached UI using isolated private files."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import select
from werkzeug.datastructures import MultiDict

import test_step4c as fixture
from labrat.audit import AuditEvent
from labrat.collector.access_collection import _write_snapshot
from labrat.collector.sources import configured_sources, effective_sources
from labrat.discovery.models import DiscoverySource
from labrat.integrations import read_settings
from labrat.models import User


class DiscoverySettingsTests(unittest.TestCase):
    """Keep toggles, configuration, permission, and derived evidence independent."""

    setUp = fixture.Step4CTests.setUp

    @property
    def path(self):
        """Return this test app's private configuration path.

        Returns:
            Path: The configuration file.
        """
        return self.app.extensions['labrat_config_path']

    def form(self, **changes):
        """Bind one method form to the current config revision and CSRF token.

        Args:
            **changes: Fields added to or overriding the form.

        Returns:
            dict: The form.
        """
        return {'_csrf_token': self.csrf, 'revision': read_settings(self.path)[2],
                'method': 'network', 'operation': 'toggle', 'enabled': '1', **changes}

    def save(self, **changes):
        """Submit an administrator change without sending controller requests.

        Args:
            **changes: Fields added to or overriding the form.

        Returns:
            The response.
        """
        return self.client.post('/settings/discovery/save', data=self.form(**changes))

    def seed(self):
        """Create commented legacy configuration and immutable source identities."""
        self.path.write_text(self.path.read_text() + '\n# preserve scan comment\n[scan]\n'
            'ranges = ["192.0.2.0/30"]\nprobes = ["nut"]\n[scan.nut]\nport = 3493\n'
            '[unifi]\nurl = "https://controller.example"\napi_key = "test-controller-secret"\n'
            '[proxmox]\nurl = "https://compute.example"\ntoken_id = "reader@pve!test"\ntoken_secret = "test-compute-secret"\n'
            '[proxmox.snmp]\nnodes = ["example-node"]\nusername = "reader"\nauth_passphrase = "test-auth-secret"\npriv_passphrase = "test-privacy-secret"\n')
        with self.factory.begin() as db:
            for kind in ('nut', 'proxmox', 'unifi'):
                db.add(DiscoverySource(kind=kind, config_name='default', name=f'{kind}:default', enabled=True))

    def test_overview_and_generic_forms_do_not_reflect_secrets(self):
        """Render compact activities and schema-driven NUT/SNMP/MNDP drawers."""
        self.seed()
        for suffix in ['', '&method=nut', '&method=snmp', '&method=snmp&target=default', '&method=mndp', '&method=network']:
            response = self.client.get('/settings?panel=discovery' + suffix)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('Network scanning', html)
            for secret in ['test-controller-secret', 'test-auth-secret', 'test-privacy-secret', 'test-compute-secret']:
                self.assertNotIn(secret, html)
        html = self.client.get('/settings?panel=discovery&method=snmp&target=default').get_data(as_text=True)
        self.assertIn('name="auth_passphrase"', html)
        self.assertIn('Leave blank to keep it', html)

    def test_master_toggle_retains_method_intent_and_stops_fast_polls(self):
        """Disable scanning without deleting ranges, source identity, or NUT choices."""
        self.seed()
        with self.factory() as db:
            source_id = db.scalar(select(DiscoverySource.id).where(DiscoverySource.kind == 'nut'))
        self.assertEqual(self.save(enabled='').status_code, 302)
        config = read_settings(self.path)[1]
        self.assertTrue(config['discovery']['nut'])
        self.assertFalse(config['discovery']['network'])
        self.assertEqual(config['scan']['ranges'], ['192.0.2.0/30'])
        self.assertEqual(effective_sources(config)['nut'], {})
        with self.factory() as db:
            self.assertFalse(db.get(DiscoverySource, source_id).enabled)
        self.assertEqual(self.save().status_code, 302)
        with self.factory() as db:
            self.assertTrue(db.get(DiscoverySource, source_id).enabled)
        self.assertIn('# preserve scan comment', self.path.read_text())

    def test_explicit_range_save_is_bounded_and_does_not_enable_scanning(self):
        """Only the saved choices grant permission; oversized permission is refused."""
        data = self.form(operation='configure', allowed_cidrs=['192.0.2.0/30'], manual_ranges='198.51.100.10\n192.0.2.0/30')
        response = self.client.post('/settings/discovery/save', data=data)
        self.assertEqual(response.status_code, 302)
        config = read_settings(self.path)[1]
        self.assertEqual(config['scan']['ranges'], ['192.0.2.0/30', '198.51.100.10/32'])
        self.assertFalse(config['discovery']['network'])
        before = self.path.read_bytes()
        self.assertEqual(self.save(operation='configure', manual_ranges='192.0.2.0/16').status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_nut_and_mndp_catalog_configuration_and_method_toggles(self):
        """Save bounded numeric settings and retain them through method disablement."""
        self.seed()
        self.assertEqual(self.save(method='nut', operation='configure', port='3494', rescan_interval_seconds='600', ups_interval_seconds='0').status_code, 302)
        self.assertEqual(read_settings(self.path)[1]['scan']['nut']['port'], 3494)
        self.assertEqual(self.save(method='nut', enabled='').status_code, 302)
        config = read_settings(self.path)[1]
        self.assertEqual(config['scan']['probes'], [])
        self.assertEqual(config['scan']['nut']['port'], 3494)
        self.assertEqual(self.save(method='mndp', operation='configure', listen_seconds='65').status_code, 302)
        self.assertEqual(self.save(method='mndp').status_code, 302)
        config = read_settings(self.path)[1]
        self.assertEqual(config['mndp']['listen_seconds'], 65)
        self.assertIn('default', effective_sources(config)['mndp'])
        self.assertEqual(self.save(method='mndp', enabled='').status_code, 302)
        self.assertEqual(effective_sources(read_settings(self.path)[1])['mndp'], {})
        before = self.path.read_bytes()
        self.assertEqual(self.save(method='nut', operation='configure', port='3493', rescan_interval_seconds='600', ups_interval_seconds='5').status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_snmp_is_scoped_and_disabled_without_disabling_proxmox(self):
        """Keep credentials private and avoid replacing unrelated API/advanced settings."""
        self.seed()
        self.assertEqual(self.save(method='snmp', operation='configure', target='default', nodes='example-node\nother-node', username='reader', auth_passphrase='', priv_passphrase='').status_code, 302)
        config = read_settings(self.path)[1]
        self.assertEqual(config['proxmox']['snmp']['auth_passphrase'], 'test-auth-secret')
        self.assertEqual(config['proxmox']['snmp']['nodes'], ['example-node', 'other-node'])
        self.assertEqual((config['proxmox']['snmp']['auth_protocol'], config['proxmox']['snmp']['priv_protocol'],
                          config['proxmox']['snmp']['port']), ('sha', 'aes', 161), 'blank options take the defaults')
        self.assertEqual(self.save(method='snmp', operation='configure', target='default', nodes='example-node',
                                   username='reader', auth_passphrase='', priv_passphrase='', auth_protocol='sha512',
                                   priv_protocol='aes256c', port='1161').status_code, 302)
        config = read_settings(self.path)[1]
        self.assertEqual((config['proxmox']['snmp']['auth_protocol'], config['proxmox']['snmp']['priv_protocol'],
                          config['proxmox']['snmp']['port']), ('sha512', 'aes256c', 1161))
        self.assertNotEqual(self.save(method='snmp', operation='configure', target='default', nodes='example-node',
                                      username='reader', auth_passphrase='', priv_passphrase='',
                                      priv_protocol='des').status_code, 302, 'an unknown protocol is refused')
        self.assertEqual(read_settings(self.path)[1]['proxmox']['snmp']['priv_protocol'], 'aes256c')
        self.assertEqual(self.save(method='snmp', enabled='').status_code, 302)
        config = read_settings(self.path)[1]
        self.assertIn('snmp', configured_sources(config)['proxmox']['default'])
        self.assertNotIn('snmp', effective_sources(config)['proxmox']['default'])
        with self.factory() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == 'proxmox'))
            self.assertTrue(source.enabled)
            audits = list(db.scalars(select(AuditEvent).where(AuditEvent.action == 'discovery.settings.updated')))
            self.assertTrue(audits)
            self.assertNotIn('test-auth-secret', str([a.details_json for a in audits]))

    def test_named_snmp_target_preserves_sibling_credentials(self):
        """Edit a named connection without moving or altering the default section."""
        self.seed()
        self.path.write_text(self.path.read_text() + '\n[integrations.proxmox.secondary]\nurl = "https://second.example"\n'
                             'token_id = "reader@pve!test"\ntoken_secret = "second-api-secret"\n')
        before = read_settings(self.path)[1]['proxmox']
        response = self.save(method='snmp', operation='configure', target='secondary', nodes='second-node',
                             username='reader', auth_passphrase='new-test-auth', priv_passphrase='new-test-privacy')
        self.assertEqual(response.status_code, 302)
        config = read_settings(self.path)[1]
        self.assertEqual(config['proxmox'], before)
        self.assertEqual(config['integrations']['proxmox']['secondary']['snmp']['nodes'], ['second-node'])
        html = self.client.get('/settings?panel=discovery&method=snmp').get_data(as_text=True)
        self.assertIn('target=secondary', html)
        self.assertNotIn('new-test-auth', html)

    def test_stale_csrf_role_and_atomic_rollback(self):
        """Refuse stale/unauthorized forms and roll back DB state on failed file writes."""
        self.seed()
        before = self.path.read_bytes()
        self.assertEqual(self.save(revision='old').status_code, 400)
        self.assertEqual(self.save(_csrf_token='bad').status_code, 400)
        with patch('labrat.integrations.os.replace', side_effect=OSError('private-path')):
            response = self.save(enabled='')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('private-path', response.get_data(as_text=True))
        self.assertEqual(self.path.read_bytes(), before)
        with self.factory() as db:
            self.assertTrue(db.scalar(select(DiscoverySource).where(DiscoverySource.kind == 'nut')).enabled)
        with self.factory.begin() as db:
            db.scalar(select(User)).is_admin = False
        self.assertEqual(self.client.get('/settings?panel=discovery').status_code, 403)
        self.assertEqual(self.save().status_code, 403)

    def test_cached_candidates_never_grant_permission_and_stale_verdicts_hide(self):
        """Display provenance and details without selecting newly discovered subnets."""
        self.seed()
        config = read_settings(self.path)[1]
        with self.factory() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == 'unifi'))
            source_id = source.id
        now = datetime.now(timezone.utc)
        fingerprint = hashlib.sha256(json.dumps(config['unifi'], sort_keys=True).encode()).hexdigest()
        path = Path(config['database']['path'] + '.access-evidence') / f'source-{source_id}.json'
        snapshot = {'version': 1, 'available': True, 'fingerprint': fingerprint, 'expires_at': (now + timedelta(minutes=10)).isoformat(),
                    'collected_at': now.isoformat(), 'sites': [{'site_id': 'site-example', 'candidates': [
                        {'network_id': 'network-example', 'name': 'Example subnet', 'cidr': '198.51.100.0/24',
                         'profiles': {'nut': {'access': 'blocked', 'reason': 'Synthetic policy block'}, 'snmp': {'access': 'blocked'}}}]}]}
        _write_snapshot(path, snapshot)
        html = self.client.get('/settings?panel=discovery&method=network').get_data(as_text=True)
        self.assertIn('Example subnet', html)
        self.assertIn('access: blocked', html)
        self.assertNotIn('value="198.51.100.0/24" checked', html)
        snapshot['expires_at'] = (now - timedelta(minutes=1)).isoformat()
        _write_snapshot(path, snapshot)
        html = self.client.get('/settings?panel=discovery&method=network').get_data(as_text=True)
        self.assertIn('access: unknown', html)
        self.assertNotIn('Synthetic policy block', html)


if __name__ == '__main__':
    unittest.main()
