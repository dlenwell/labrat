# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Verify private integration saves, sanitized tests, and collector reloads."""

import copy
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
import unittest
from unittest.mock import Mock, patch

import requests
from sqlalchemy import select

import test_step4c as fixture
from labrat.audit import AuditEvent
from labrat.collector import __main__ as cli
from labrat.discovery.models import DiscoverySource, DiscoveredDevice
from labrat.integration_catalog import DEFINITIONS, load_catalog
from labrat.integrations import IntegrationError, candidate, read_settings, test_connection, connection_health
from labrat.models import User, Device, Room, Site
from labrat.placement import DevicePlacement, PlacementContainer


class IntegrationTests(unittest.TestCase):
    """Exercise authenticated forms with temporary files and mocked controllers."""

    setUp = fixture.Step4CTests.setUp

    @property
    def path(self):
        """Return the test app's pinned configuration path.

        Returns:
            Path: The configuration file.
        """
        return self.app.extensions['labrat_config_path']

    def form(self, **changes):
        """Build a valid add-connection submission with its current revision.

        Args:
            **changes: Fields added to or overriding the submission.

        Returns:
            dict: The form.
        """
        _raw, _config, revision = read_settings(self.path)
        fields = {'_csrf_token': self.csrf, 'revision': revision, 'kind': 'unifi',
                  'name': 'primary', 'is_new': '1', 'url': 'https://controller.example',
                  'api_key': 'test-api-key', 'tls_mode': 'verify', 'ca_bundle': '', 'enabled': '1'}
        return {**fields, **changes}

    def save(self, form):
        """Submit settings without invoking a live controller.

        Args:
            form: The submission.

        Returns:
            The response.
        """
        return self.client.post('/settings/integrations/save', data=form)

    def seed_legacy(self):
        """Add commented legacy credentials plus untouched advanced settings."""
        self.path.write_text(self.path.read_text() + '\n# Preserve this integration comment\n[unifi]\n'
                             'url = "https://controller.example"\napi_key = "test-legacy-key" # keep this comment\n'
                             'wan_interval_seconds = 45\n'
                             '\n[proxmox]\nurl = "https://compute.example"\n'
                             'token_id = "reader@pve!inventory"\ntoken_secret = "test-compute-secret"\n'
                             'report_interval_seconds = 1800\n[proxmox.snmp]\n'
                             'nodes = ["example-node"]\nauth_passphrase = "test-snmp-passphrase"\n')

    def test_new_named_source_private_file_and_curated_audit(self):
        """Add a named source with restrictive file permissions and no DB credentials."""
        response = self.save(self.form())
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        _raw, config, _revision = read_settings(self.path)
        self.assertEqual(config['integrations']['unifi']['primary']['api_key'], 'test-api-key')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.factory() as db:
            source = db.scalar(select(DiscoverySource))
            self.assertEqual((source.kind, source.config_name, source.enabled), ('unifi', 'primary', True))
            event = db.scalar(select(AuditEvent).where(AuditEvent.action == 'integration.created'))
            self.assertEqual(json.loads(event.details_json), {'kind': 'unifi', 'config_name': 'primary', 'enabled': True})
            self.assertNotIn('test-api-key', event.details_json)
        self.assertFalse(list(self.path.parent.glob('.*.tmp')))

    def test_legacy_save_preserves_comments_advanced_settings_and_secret(self):
        """Editing a legacy connection keeps its section, extras, and blank secret."""
        self.seed_legacy()
        before = read_settings(self.path)[1]
        response = self.save(self.form(name='default', is_new='0', api_key='', enabled=''))
        self.assertEqual(response.status_code, 302)
        raw, config, _revision = read_settings(self.path)
        self.assertIn(b'Preserve this integration comment', raw)
        self.assertIn(b'# keep this comment', raw)
        self.assertEqual(config['unifi']['api_key'], 'test-legacy-key')
        self.assertEqual(config['unifi']['wan_interval_seconds'], 45)
        self.assertEqual(config['proxmox'], before['proxmox'])
        self.assertNotIn('integrations', config)
        with self.factory() as db:
            self.assertFalse(db.scalar(select(DiscoverySource.enabled)))

    def test_editor_and_backup_never_return_saved_secrets(self):
        """Admin HTML and inventory exports expose only credential-presence flags."""
        self.seed_legacy()
        for endpoint in ('/settings?panel=integrations', '/settings?panel=integrations&kind=unifi&name=default',
                         '/settings?panel=integrations&kind=proxmox&name=default'):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            for secret in (b'test-legacy-key', b'test-compute-secret', b'test-snmp-passphrase'):
                self.assertNotIn(secret, response.data)
        backup = self.client.post('/settings/backup/export', data={'_csrf_token': self.csrf})
        self.assertNotIn(b'test-legacy-key', backup.data)
        self.assertIn(b'Integrations', self.client.get('/settings').data)

    def test_changed_address_requires_secret_before_any_network_request(self):
        """Blank saved credentials cannot be forwarded to a new controller address."""
        self.seed_legacy()
        form = self.form(name='default', is_new='0', api_key='', url='https://replacement.example')
        with patch('labrat.integrations.JsonGetClient.get_json') as get:
            response = self.client.post('/settings/integrations/test', data=form, headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('requires the secret again', response.json['message'])
        get.assert_not_called()
        self.assertNotIn('test-legacy-key', response.get_data(as_text=True))
        form['api_key'] = 'test-replacement-key'
        self.assertEqual(self.save(form).status_code, 302)

    def test_connection_test_is_read_only_and_uses_header_auth(self):
        """An explicit test uses one API GET and saves neither credentials nor state."""
        original = self.path.read_bytes()
        with self.factory() as db:
            before = list(db.scalars(select(AuditEvent.id)))
        with patch('labrat.integrations.JsonGetClient.get_json', return_value={'data': []}) as get:
            response = self.client.post('/settings/integrations/test', data=self.form(), headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['ok'])
        get.assert_called_once_with('https://controller.example/proxy/network/integration/v1/sites?offset=0&limit=1',
                                    {'Accept': 'application/json', 'X-API-Key': 'test-api-key'}, True)
        self.assertEqual(self.path.read_bytes(), original)
        with self.factory() as db:
            self.assertIsNone(db.scalar(select(DiscoverySource)))
            self.assertEqual(list(db.scalars(select(AuditEvent.id))), before)

    def test_connection_test_follows_the_controller_type(self):
        """A self-hosted Network application is tested without the console prefix."""
        with patch('labrat.integrations.JsonGetClient.get_json', return_value={'data': []}) as get:
            response = self.client.post('/settings/integrations/test', headers={'Accept': 'application/json'},
                                        data=self.form(url='https://controller.example:8443',
                                                       controller_type='network_application'))
        self.assertTrue(response.json['ok'])
        self.assertEqual(get.call_args.args[0], 'https://controller.example:8443/integration/v1/sites?offset=0&limit=1')

    def test_proxmox_save_preserves_nested_options_and_tests_token(self):
        """Proxmox token headers and existing SNMP settings stay independent."""
        self.seed_legacy()
        form = self.form(kind='proxmox', name='default', is_new='0', url='https://compute.example',
                         api_key='', token_id='reader@pve!inventory', token_secret='')
        with patch('labrat.integrations.JsonGetClient.get_json', return_value={'data': []}) as get:
            response = self.client.post('/settings/integrations/test', data=form, headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_args.args[1]['Authorization'], 'PVEAPIToken=reader@pve!inventory=test-compute-secret')
        self.assertEqual(self.save(form).status_code, 302)
        self.assertEqual(read_settings(self.path)[1]['proxmox']['snmp']['nodes'], ['example-node'])

    def test_stale_form_and_atomic_write_failure_preserve_state(self):
        """Concurrent file edits and failed replacements cannot partially save a source."""
        form = self.form()
        self.path.write_text(self.path.read_text() + '\n# external edit\n')
        response = self.save(form)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Configuration changed', response.data)
        original = self.path.read_bytes()
        with patch('labrat.integrations.os.replace', side_effect=OSError('private-test-value')):
            response = self.save(self.form())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'private-test-value', response.data)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob('.*.tmp')))
        with self.factory() as db:
            self.assertIsNone(db.scalar(select(DiscoverySource)))

    def test_commit_failure_rolls_back_file_and_source(self):
        """A failed database commit restores the original private config bytes."""
        from sqlalchemy import event
        original = self.path.read_bytes()
        def reject_commit(session):
            """Simulate a persistence error after the private atomic replacement."""
            from sqlalchemy.exc import OperationalError
            raise OperationalError('test commit', {}, Exception('private-test-value'))
        event.listen(self.factory.class_, 'before_commit', reject_commit)
        try:
            response = self.save(self.form())
        finally:
            event.remove(self.factory.class_, 'before_commit', reject_commit)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.path.read_bytes(), original)
        with self.factory() as db:
            self.assertIsNone(db.scalar(select(DiscoverySource)))

    def test_validation_duplicate_names_and_permissions(self):
        """Reject unsafe roots, names, tokens, forged CSRF, and non-admin writes."""
        cases = ({'name': 'bad.name'}, {'url': 'http://controller.example'}, {'url': 'https://user:password@controller.example'},
                 {'api_key': 'test\r\nheader'}, {'api_key': ''}, {'kind': 'unsupported'},
                 {'kind': 'proxmox', 'token_id': 'missing-token-format', 'token_secret': 'test-secret'},
                 {'ca_bundle': 'relative-ca.pem'}, {'tls_mode': 'existing'})
        for changes in cases:
            with self.subTest(fields=list(changes)):
                self.assertEqual(self.save(self.form(**changes)).status_code, 400)
        self.assertEqual(self.save(self.form(_csrf_token='forged')).status_code, 400)
        self.assertEqual(self.save(self.form()).status_code, 302)
        self.assertEqual(self.save(self.form()).status_code, 400)
        with self.factory.begin() as db:
            db.scalar(select(User)).is_admin = False
        self.assertEqual(self.client.get('/settings?panel=integrations').status_code, 403)
        for action in ('save', 'test', 'delete'):
            self.assertEqual(self.client.post('/settings/integrations/' + action, data=self.form()).status_code, 403)
        self.assertNotIn(b'Discovery connections', self.client.get('/settings').data)

    def test_existing_disabled_tls_can_be_preserved_or_upgraded_only(self):
        """A legacy diagnosis setting is retained only at its existing destination."""
        self.seed_legacy()
        self.path.write_text(self.path.read_text().replace('wan_interval_seconds = 45', 'wan_interval_seconds = 45\nverify_tls = false'))
        form = self.form(name='default', is_new='0', api_key='', tls_mode='existing')
        self.assertEqual(self.save(form).status_code, 302)
        self.assertFalse(read_settings(self.path)[1]['unifi']['verify_tls'])
        form = self.form(name='default', is_new='0', api_key='', tls_mode='verify')
        self.assertEqual(self.save(form).status_code, 302)
        self.assertTrue(read_settings(self.path)[1]['unifi']['verify_tls'])
        form = self.form(name='default', is_new='0', api_key='', tls_mode='existing')
        self.assertEqual(self.save(form).status_code, 400)

    def test_network_errors_are_sanitized(self):
        """No HTTP exception, token, address, or response body is returned verbatim."""
        settings = {'url': 'https://controller.example', 'api_key': 'test-sensitive-key'}
        denied = requests.Response()
        denied.status_code = 403
        errors = [requests.exceptions.SSLError('private-test-value'), requests.exceptions.Timeout('private-test-value'),
                  requests.exceptions.ConnectionError('private-test-value'),
                  requests.exceptions.HTTPError('private-test-value', response=denied), ValueError('private-test-value')]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch('labrat.integrations.JsonGetClient.get_json', side_effect=error):
                ok, message = test_connection('unifi', settings)
                self.assertFalse(ok)
                self.assertNotIn('private-test-value', message)
                self.assertNotIn('test-sensitive-key', message)
        with patch('labrat.integrations.JsonGetClient.get_json', return_value={'unexpected': []}):
            self.assertFalse(test_connection('unifi', settings)[0])

    def test_disabled_cached_fast_polls_skip_network(self):
        """Disabled sources in a cached telemetry schedule cannot issue requests."""
        from labrat.collector.runner import poll_wan_once, poll_ups_once
        with self.factory.begin() as db:
            source = DiscoverySource(kind='unifi', config_name='primary', name='unifi:primary', enabled=False)
            db.add(source)
            db.flush()
            source_id = source.id
        adapter = Mock()
        self.assertEqual(poll_wan_once(self.factory, source_id, {}, adapter), 0)
        self.assertEqual(poll_ups_once(self.factory, source_id, {}, adapter), 0)
        adapter.poll_wan.assert_not_called()
        adapter.poll_ups.assert_not_called()

    def test_unsafe_saved_url_is_not_reflected_in_editor(self):
        """Malformed legacy roots with embedded credentials do not leak into HTML."""
        self.path.write_text(self.path.read_text() + '\n[unifi]\nurl = "https://user:test-sensitive-key@controller.example"\napi_key = "test-other-key"\n')
        response = self.client.get('/settings?panel=integrations&kind=unifi&name=default')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'test-sensitive-key', response.data)
        self.assertNotIn(b'test-other-key', response.data)

    def test_rename_default_preserves_namespace_and_observations(self):
        """Changing a friendly name keeps legacy config keys and discovery identities."""
        self.seed_legacy()
        self.assertEqual(self.save(self.form(name='default', is_new='0', api_key='')).status_code, 302)
        with self.factory.begin() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == 'unifi'))
            source_id = source.id
            observation = DiscoveredDevice(source_id=source.id, remote_id='stable-device', name='Reported label', category='switch')
            db.add(observation)
            db.flush()
            observation_id = observation.id
        response = self.save(self.form(name='default', is_new='0', api_key='', display_name='Main network'))
        self.assertEqual(response.status_code, 302)
        config = read_settings(self.path)[1]
        self.assertEqual(config['unifi']['display_name'], 'Main network')
        self.assertNotIn('integrations', config)
        with self.factory() as db:
            source = db.get(DiscoverySource, source_id)
            self.assertEqual((source.config_name, source.name), ('default', 'unifi:Main network'))
            self.assertEqual(db.get(DiscoveredDevice, observation_id).source_id, source_id)
        page = self.client.get('/settings?panel=integrations').get_data(as_text=True)
        self.assertIn('Main network', page)

    def test_catalog_fields_render_validate_and_save_generically(self):
        """Extra catalog fields require no vendor-specific template or save branch."""
        definition = copy.deepcopy(DEFINITIONS['unifi'])
        definition['fields'] += [
            {'key': 'sample_interval', 'label': 'Sample interval', 'type': 'number', 'required': True, 'min': 5, 'max': 60},
            {'key': 'sample_mode', 'label': 'Sample mode', 'type': 'select', 'required': True, 'choices': [{'value': 'read', 'label': 'Read only'}]},
            {'key': 'sample_flag', 'label': 'Sample flag', 'type': 'checkbox', 'required': False},
        ]
        with patch.dict(DEFINITIONS, {'unifi': definition}):
            page = self.client.get('/settings?panel=integrations&kind=unifi&is_new=1').get_data(as_text=True)
            self.assertIn('Sample interval', page)
            self.assertIn('Sample mode', page)
            form = self.form(sample_interval='12', sample_mode='read', sample_flag='1')
            self.assertEqual(self.save(form).status_code, 302)
            settings = read_settings(self.path)[1]['integrations']['unifi']['primary']
            self.assertEqual((settings['sample_interval'], settings['sample_mode'], settings['sample_flag']), (12, 'read', True))
            for fields in ({'sample_interval': '2', 'sample_mode': 'read'}, {'sample_interval': '12', 'sample_mode': 'write'}, {'sample_interval': 'text', 'sample_mode': 'read'}):
                response = self.save(self.form(name='other', **fields))
                self.assertEqual(response.status_code, 400)

    def test_friendly_names_generate_stable_keys_and_reject_duplicate_labels(self):
        """New human-readable names are distinct from immutable collector references."""
        response = self.save(self.form(name='', display_name='Main network'))
        self.assertEqual(response.status_code, 302)
        config = read_settings(self.path)[1]
        self.assertIn('main-network', config['integrations']['unifi'])
        response = self.save(self.form(name='', display_name='Main network'))
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'already uses that name', response.data)
        config = read_settings(self.path)[1]
        self.assertEqual(list(config['integrations']['unifi']), ['main-network'])

    def test_catalog_is_packaged_and_drives_required_vendor_fields(self):
        """Public TOML contains the connection schemas, categories, and test rules."""
        categories, definitions = load_catalog()
        self.assertEqual({entry['key'] for entry in categories}, {'network', 'compute'})
        vendors = {entry['kind']: entry for entry in definitions}
        self.assertEqual({field['key'] for field in vendors['unifi']['fields'] if field['required']}, {'url', 'api_key'})
        self.assertEqual({field['key'] for field in vendors['proxmox']['fields'] if field['required']}, {'url', 'token_id', 'token_secret'})

    def test_new_catalog_entry_gets_form_test_and_save_without_vendor_code(self):
        """A TOML-only setup definition uses the generic fields and test transport."""
        from labrat.collector import sources
        catalog_text = files('labrat').joinpath('integration_catalog.toml').read_text()
        catalog_text += '\n[integrations.example]\nlabel = "Example API"\ncategory = "network"\nicon = "api"\ndescription = "An example API connection"\n'
        catalog_text += '[integrations.example.test]\npath = "/status"\nauth_header = "Authorization"\nauth_template = "Bearer {access_token}"\nresponse_key = "data"\ncollection_label = "API status"\n'
        catalog_text += '[[integrations.example.fields]]\nkey = "url"\nlabel = "Endpoint"\ntype = "url"\nrequired = true\n'
        catalog_text += '[[integrations.example.fields]]\nkey = "access_token"\nlabel = "Access token"\ntype = "password"\nrequired = true\n'
        with patch('labrat.integration_catalog.files') as resource:
            resource.return_value.joinpath.return_value.read_text.return_value = catalog_text
            _categories, definitions = load_catalog()
        example = next(entry for entry in definitions if entry['kind'] == 'example')
        with patch.dict(DEFINITIONS, {'example': example}), patch.dict(sources.KINDS, {'example': 'Example API'}), \
             patch.object(sources, '_KINDS', (*sources._KINDS, 'example')), patch('labrat.integrations.CATALOG', definitions):
            page = self.client.get('/settings?panel=integrations&kind=example&is_new=1').get_data(as_text=True)
            self.assertIn('Access token', page)
            fields = self.form(kind='example', name='', display_name='Example connection', access_token='test-access-token')
            with patch('labrat.integrations.JsonGetClient.get_json', return_value={'data': []}) as get:
                result = self.client.post('/settings/integrations/test', data=fields, headers={'Accept': 'application/json'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(get.call_args.args[1]['Authorization'], 'Bearer test-access-token')
            self.assertEqual(self.save(fields).status_code, 302)
            self.assertEqual(read_settings(self.path)[1]['integrations']['example']['example-connection']['access_token'], 'test-access-token')

    def deletion_fields(self, name='default'):
        """Build an explicit source-removal confirmation without credential fields.

        Args:
            name: The connection to remove.

        Returns:
            dict: The confirmation form.
        """
        return {'_csrf_token': self.csrf, 'revision': read_settings(self.path)[2],
                'kind': 'unifi', 'name': name, 'action': 'delete', 'confirm': 'delete'}

    def test_delete_preview_and_confirm_preserve_inventory_and_observations(self):
        """Remove credentials and stop collection while retaining all existing data."""
        self.seed_legacy()
        self.assertEqual(self.save(self.form(name='default', is_new='0', api_key='')).status_code, 302)
        with self.factory.begin() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == 'unifi'))
            source_id = source.id
            site = Site(name='Example location')
            db.add(site)
            db.flush()
            room = Room(site_id=site.id, name='Example room')
            db.add(room)
            db.flush()
            place = PlacementContainer(room_id=room.id, name='Example surface', kind='floor')
            device = Device(name='Curated equipment', room_id=room.id, placement='floor')
            db.add_all([place, device])
            db.flush()
            device_id = device.id
            observed = DiscoveredDevice(source_id=source.id, remote_id='device-a', name='Reported equipment', category='switch', device_id=device.id)
            db.add(observed)
            db.add(DevicePlacement(device_id=device.id, container_id=place.id, position_json='{"x":0.5,"y":0.5}'))
            db.flush()
            observation_id = observed.id
            before_audit = list(db.scalars(select(AuditEvent.id)))
        original = self.path.read_bytes()
        preview = self.client.get('/settings?panel=integrations&kind=unifi&name=default&action=delete')
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b'ghost', preview.data)
        self.assertIn(b'Inventory devices kept</dt><dd>1', preview.data)
        self.assertNotIn(b'test-legacy-key', preview.data)
        self.assertEqual(self.path.read_bytes(), original)
        with self.factory() as db:
            self.assertEqual(list(db.scalars(select(AuditEvent.id))), before_audit)
        response = self.client.post('/settings/integrations/delete', data=self.deletion_fields())
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        config = read_settings(self.path)[1]
        self.assertNotIn('unifi', config)
        self.assertEqual(config['proxmox']['token_secret'], 'test-compute-secret')
        self.assertNotIn(b'test-legacy-key', self.path.read_bytes())
        with self.factory() as db:
            self.assertFalse(db.get(DiscoverySource, source_id).enabled)
            self.assertIsNotNone(db.get(DiscoveredDevice, observation_id))
            self.assertIsNotNone(db.get(Device, device_id))
            self.assertIsNotNone(db.get(DevicePlacement, device_id))
            event = db.scalar(select(AuditEvent).where(AuditEvent.action == 'integration.deleted'))
            self.assertEqual(json.loads(event.details_json)['retained'], {'observations': 1, 'inventory': 1, 'placements': 1})
        from labrat.collector.sources import sync_configured_sources
        sync_configured_sources(self.factory, {'unifi': {'url': 'https://controller.example', 'api_key': 'test-old-key'}})
        with self.factory() as db:
            self.assertFalse(db.get(DiscoverySource, source_id).enabled, 'A cached configuration cannot revive the removed source')

    def test_named_delete_keeps_siblings_and_requires_confirmation_and_revision(self):
        """Deleting one named connection leaves other integrations and settings intact."""
        self.assertEqual(self.save(self.form(name='primary')).status_code, 302)
        self.assertEqual(self.save(self.form(name='secondary')).status_code, 302)
        fields = self.deletion_fields('primary')
        for changes in ({'confirm': ''}, {'revision': 'stale'}, {'_csrf_token': 'forged'}):
            with self.subTest(fields=list(changes)):
                self.assertEqual(self.client.post('/settings/integrations/delete', data={**fields, **changes}).status_code, 400)
        response = self.client.post('/settings/integrations/delete', data=fields)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(read_settings(self.path)[1]['integrations']['unifi']), ['secondary'])
        self.assertEqual(self.client.post('/settings/integrations/delete', data=self.deletion_fields('secondary')).status_code, 302)
        self.assertNotIn('integrations', read_settings(self.path)[1])

    def test_delete_failure_rolls_back_file_source_and_audit(self):
        """A file replacement or database commit failure leaves the connection intact."""
        from sqlalchemy import event
        from sqlalchemy.exc import OperationalError
        self.assertEqual(self.save(self.form(name='primary')).status_code, 302)
        original = self.path.read_bytes()
        def reject_commit(session):
            """Simulate an audit persistence failure after the config write."""
            raise OperationalError('test commit', {}, Exception('private-test-value'))
        with patch('labrat.integrations.os.replace', side_effect=OSError('private-test-value')):
            response = self.client.post('/settings/integrations/delete', data=self.deletion_fields('primary'))
            self.assertEqual(response.status_code, 503)
        event.listen(self.factory.class_, 'before_commit', reject_commit)
        try:
            response = self.client.post('/settings/integrations/delete', data=self.deletion_fields('primary'))
            self.assertEqual(response.status_code, 503)
        finally:
            event.remove(self.factory.class_, 'before_commit', reject_commit)
        self.assertEqual(self.path.read_bytes(), original)
        with self.factory() as db:
            self.assertTrue(db.scalar(select(DiscoverySource.enabled)))
            self.assertIsNone(db.scalar(select(AuditEvent).where(AuditEvent.action == 'integration.deleted')))

    def test_bad_configuration_returns_safe_editor_message(self):
        """Broken TOML is shown generically without reflecting its private contents."""
        self.path.write_text('broken = "test-sensitive-key')
        response = self.client.get('/settings?panel=integrations')
        self.assertIn(b'configuration file could not be read', response.data)
        self.assertNotIn(b'test-sensitive-key', response.data)


class ConnectionHealthTests(unittest.TestCase):
    """Only recent collection evidence can light a connection green."""

    def test_active_failed_stale_disabled_and_waiting_states(self):
        """Failed and overdue collections are red; disabled or new sources are neutral."""
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        settings = {'url': 'https://controller.example', 'api_key': 'test-key'}
        source = DiscoverySource(kind='unifi', name='unifi:example', config_name='example', enabled=True,
                                 last_success_at=now - timedelta(seconds=20), last_attempt_at=now - timedelta(seconds=20), poll_interval_seconds=60)
        self.assertEqual(connection_health(settings, source, now)['state'], 'ready')
        source.last_error = 'Collection failed: HTTPError'
        state = connection_health(settings, source, now)
        self.assertEqual(state['state'], 'error')
        self.assertIn('credentials', state['message'])
        source.last_error = 'test-sensitive-key'
        self.assertNotIn('test-sensitive-key', connection_health(settings, source, now)['message'])
        source.enabled = False
        self.assertEqual(connection_health(settings, source, now)['label'], 'Collection disabled')
        source.enabled, source.last_error = True, None
        source.last_success_at = now - timedelta(minutes=10)
        self.assertEqual(connection_health(settings, source, now)['label'], 'Collection overdue')
        self.assertEqual(connection_health(settings, None, now)['state'], 'waiting')
        self.assertEqual(connection_health({'url': 'invalid'}, source, now)['state'], 'error')


class CollectorReloadTests(unittest.TestCase):
    """Verify integration edits become visible without changing database targets."""

    def test_running_loop_reads_updated_settings_on_next_scan(self):
        """The real CLI loop feeds refreshed credentials to its next full scan."""
        before = {'unifi': {'api_key': 'test-before'}}
        after = {'unifi': {'api_key': 'test-after'}}
        clock = [0.0]
        engine = Mock()
        with patch('sys.argv', ['collector', '--interval', '5']), \
             patch.object(cli, 'load_local_config', side_effect=[before, before, after]), \
             patch.object(cli, 'make_engine', return_value=engine), \
             patch.object(cli, 'make_session_factory', return_value=Mock()), \
             patch.object(cli, 'AccessCollector'), \
             patch.object(cli, 'sync_configured_sources', return_value=0), \
             patch.object(cli, 'poll_once', side_effect=[0, KeyboardInterrupt]) as poll, \
             patch.object(cli, 'wan_poll_sources', return_value=[]), \
             patch.object(cli, 'ups_poll_sources', return_value=[]), \
             patch.object(cli, 'live_poll_sources', return_value=[]), \
             patch.object(cli.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(cli.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            cli.main()
        self.assertEqual(poll.call_args_list[0].args[1], before)
        self.assertEqual(poll.call_args_list[1].args[1], after)
        engine.dispose.assert_called_once()

    def test_reload_uses_new_values_and_keeps_last_valid_config(self):
        """Refresh valid credentials; reject malformed edits without leaking values."""
        before = {'database': {'path': 'original.db'}, 'unifi': {'api_key': 'test-old'}}
        after = copy.deepcopy(before)
        after['unifi']['api_key'] = 'test-new'
        with patch.object(cli, 'load_local_config', return_value=after):
            self.assertEqual(cli.refreshed_config(before), after)
        for config in ({'integrations': []}, {'database': {'path': 'other.db'}}):
            with patch.object(cli, 'load_local_config', return_value=config), self.assertLogs(level='WARNING'):
                self.assertEqual(cli.refreshed_config(before), before)
        with patch.object(cli, 'load_local_config', side_effect=ValueError('test-sensitive-key')), self.assertLogs(level='WARNING') as logs:
            self.assertEqual(cli.refreshed_config(before), before)
        self.assertNotIn('test-sensitive-key', str(logs.output))


if __name__ == '__main__':
    unittest.main()
