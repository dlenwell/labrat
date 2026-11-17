# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise collector scheduling and recovery using isolated storage and fake IO."""

from contextlib import ExitStack
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select

from labrat.audit import AuditEvent
from labrat.collector import __main__ as cli, runner
from labrat.collector.adapters.unifi import UniFiAdapter
from labrat.collector.contracts import DeviceObservation, DiscoveryBatch
from labrat.database import Base, make_engine, make_session_factory
from labrat.discovery.models import DiscoverySource, DiscoveredDevice


class CollectorOperationsTests(unittest.TestCase):
    """Keep failed sources and fast telemetry isolated from full scan health."""

    def setUp(self):
        """Create disposable model storage with no production configuration."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = make_engine({'database': {'path': str(Path(temporary.name) / 'db.sqlite')}})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = make_session_factory(self.engine)
        # Alembic's logging setup in other fixtures disables existing loggers.
        logging_state = patch.object(runner._LOGGER, 'disabled', False)
        logging_state.start()
        self.addCleanup(logging_state.stop)

    def source(self, kind, name='default', enabled=True):
        """Return the identity of a configured test source.

        Args:
            kind: The source kind.
            name: The connection name.
            enabled: Whether it is enabled.

        Returns:
            int: The source's ID.
        """
        with self.sessions.begin() as db:
            source = DiscoverySource(name=f'{kind}:{name}', kind=kind, config_name=name, enabled=enabled)
            db.add(source)
            db.flush()
            return source.id

    def test_failure_is_sanitized_and_other_sources_recover(self):
        """A failed adapter neither leaks its exception nor rolls back its peer."""
        failed = self.source('unifi')
        success = self.source('proxmox')
        self.source('mndp')  # No adapter: skipped.
        self.source('nut')  # No configuration: skipped.
        self.source('proxmox', 'disabled', False)
        bad = Mock()
        bad.poll.side_effect = RuntimeError('secret-token-private-url')
        good = Mock()
        good.poll.return_value = DiscoveryBatch(devices=(DeviceObservation('host', 'Test host', 'server'),))
        config = {'unifi': {}, 'proxmox': {}}
        with self.assertLogs(runner.__name__, level='INFO') as logs:
            self.assertEqual(runner.poll_once(self.sessions, config, {'unifi': bad, 'proxmox': good, 'nut': Mock()}, 75), 1)
        self.assertNotIn('secret-token', str(logs.output))
        with self.sessions() as db:
            source = db.get(DiscoverySource, failed)
            self.assertEqual(source.last_error, 'Collection failed: RuntimeError')
            self.assertEqual(source.poll_interval_seconds, 75)
            self.assertIsNotNone(source.last_attempt_at)
            self.assertIsNone(source.last_success_at)
            self.assertEqual(db.get(DiscoverySource, success).poll_interval_seconds, 75)
            event = db.scalar(select(AuditEvent).where(AuditEvent.action == 'discovery.collection.failed'))
            self.assertNotIn('secret-token', event.details_json)
        bad.poll.return_value = DiscoveryBatch()
        bad.poll.side_effect = None
        runner.poll_once(self.sessions, config, {'unifi': bad})
        with self.sessions() as db:
            self.assertIsNone(db.get(DiscoverySource, failed).last_error)
            self.assertEqual(db.get(DiscoverySource, failed).poll_interval_seconds, 75)

    def test_cross_source_reconciliation_and_incomplete_wireless(self):
        """Missing client snapshots preserve old observations instead of deleting them."""
        self.source('unifi')
        self.source('proxmox')
        self.source('nut')
        adapter = Mock(spec=UniFiAdapter)
        adapter.poll.return_value = DiscoveryBatch()
        peer = Mock()
        peer.poll.return_value = DiscoveryBatch()
        config = {'unifi': {}, 'proxmox': {}}
        for clients, wireless in ((None, None), ([], {1: [{'interface': 'wifi'}]})):
            with self.subTest(clients=clients), ExitStack() as stack:
                lldp = stack.enter_context(patch.object(runner, 'reconcile_proxmox_lldp_links', return_value=1))
                stack.enter_context(patch.object(runner, 'collect_client_links', return_value=clients))
                reconcile = stack.enter_context(patch.object(runner, 'reconcile_client_links', return_value=1))
                stack.enter_context(patch.object(runner, 'collect_nut_wireless', return_value=wireless))
                stack.enter_context(patch.object(runner, 'collect_nut_wired', return_value=wireless))
                save_wifi = stack.enter_context(patch.object(runner, 'reconcile_nut_wireless'))
                save_wired = stack.enter_context(patch.object(runner, 'reconcile_nut_wired', return_value=1))
                self.assertEqual(runner.poll_once(self.sessions, config, {'unifi': adapter, 'proxmox': peer}), 2)
                lldp.assert_called_once()
                self.assertEqual(reconcile.call_count, int(clients is not None))
                self.assertEqual(save_wifi.call_count, int(wireless is not None))
                self.assertEqual(save_wired.call_count, int(wireless is not None))
        # A failed wired lookup alone also keeps both previous samples.
        with patch.object(runner, 'collect_nut_wireless', return_value={}), \
                patch.object(runner, 'collect_nut_wired', return_value=None), \
                patch.object(runner, 'reconcile_nut_wireless') as save_wifi, \
                patch.object(runner, 'reconcile_nut_wired') as save_wired:
            runner.poll_once(self.sessions, config, {'unifi': adapter, 'proxmox': peer})
            save_wifi.assert_not_called()
            save_wired.assert_not_called()
        self.source('unifi', 'missing')
        with patch.object(runner, 'collect_nut_wireless', return_value={}), patch.object(runner, 'reconcile_nut_wireless') as save:
            runner.poll_once(self.sessions, config, {'unifi': adapter})
            save.assert_not_called()

    def test_fast_poll_source_selection(self):
        """Disabled, missing, invalid and zero intervals never schedule telemetry."""
        unifi = self.source('unifi')
        nut = self.source('nut')
        self.source('unifi', 'missing')
        self.source('nut', 'missing')
        self.source('unifi', 'disabled', False)
        for value, count in ((30, 1), (0, 0), (True, 0), (9, 0), (301, 0)):
            with self.subTest(interval=value):
                config = {'unifi': {'wan_interval_seconds': value},
                          'scan': {'ranges': ['192.0.2.0/30'], 'probes': ['nut'], 'nut': {'ups_interval_seconds': value}}}
                wan = runner.wan_poll_sources(self.sessions, config)
                ups = runner.ups_poll_sources(self.sessions, config)
                self.assertEqual(len(wan), count)
                self.assertEqual(len(ups), count)
                if count:
                    self.assertEqual((wan[0][0], ups[0][0]), (unifi, nut))

    def test_fast_poll_failures_and_missing_sources(self):
        """Telemetry errors stay sanitized and do not become scan errors."""
        source_id = self.source('unifi')
        for function, method in ((runner.poll_wan_once, 'poll_wan'), (runner.poll_ups_once, 'poll_ups')):
            adapter = Mock()
            getattr(adapter, method).side_effect = ValueError('private-token')
            with self.assertLogs(runner.__name__) as logs:
                self.assertEqual(function(self.sessions, source_id, {}, adapter), 0)
            self.assertNotIn('private-token', str(logs.output))
            getattr(adapter, method).side_effect = None
            getattr(adapter, method).return_value = []
            self.assertEqual(function(self.sessions, 999, {}, adapter), 0)
        with self.sessions() as db:
            source = db.get(DiscoverySource, source_id)
            self.assertIsNone(source.last_error)
            self.assertIsNone(source.last_attempt_at)

    def run_cli(self, args, config, **replacements):
        """Run the entry point with fake configuration, adapters and clock.

        Args:
            args: Command-line arguments.
            config: The configuration to load.
            **replacements: Fake adapters and readers, by name.

        Returns:
            dict: The mocks it patched in, by name.
        """
        with ExitStack() as stack:
            stack.enter_context(patch('sys.argv', ['collector', *args]))
            stack.enter_context(patch.object(cli, 'load_local_config', return_value=config))
            stack.enter_context(patch.object(cli, 'make_engine', return_value=self.engine))
            stack.enter_context(patch.object(cli, 'logging'))
            for name in ('MndpAdapter', 'NutAdapter', 'ProxmoxAdapter', 'UniFiAdapter', 'AccessCollector'):
                stack.enter_context(patch.object(cli, name))
            mocks = {name: stack.enter_context(patch.object(cli, name, **options))
                     for name, options in replacements.items()}
            cli.main()
            return mocks

    def test_cli_list_does_not_poll(self):
        """Listing observations never initializes a scan or contacts an adapter."""
        source_id = self.source('unifi')
        with self.sessions.begin() as db:
            db.add(DiscoveredDevice(source_id=source_id, remote_id='test', name='Test router', category='router'))
        with patch('sys.stdout', new_callable=StringIO) as output:
            mocks = self.run_cli(['--list'], {}, poll_once={}, sync_configured_sources={})
        self.assertIn('unifi:default: 1 physical device(s)', output.getvalue())
        self.assertIn('[router] Test router', output.getvalue())
        mocks['poll_once'].assert_not_called()
        mocks['sync_configured_sources'].assert_not_called()

    def test_cli_once_records_no_poll_interval(self):
        """One-off scans finish after one iteration and leave daemon cadence alone."""
        mocks = self.run_cli(['--once'], {}, sync_configured_sources={'return_value': 1}, poll_once={'return_value': 2})
        self.assertIsNone(mocks['poll_once'].call_args.args[3])
        mocks['poll_once'].assert_called_once()

    def test_cli_access_collection_is_separate_and_closed(self):
        """Run optional evidence collection after discovery, and close its session."""
        client = Mock()
        mocks = self.run_cli(['--once'], {}, sync_configured_sources={'return_value': 0},
                             poll_once={'return_value': 0}, AccessCollector={'return_value': client})
        mocks['AccessCollector'].assert_called_once()
        client.poll.assert_called_once()
        self.assertIs(client.poll.call_args.args[0].kw['bind'], self.engine)
        self.assertEqual(client.poll.call_args.args[1], {})
        client.close.assert_called_once()
        client.reset_mock()
        mocks = self.run_cli(['--list'], {}, AccessCollector={'return_value': client})
        mocks['AccessCollector'].assert_not_called()
        client.poll.assert_not_called()

    def test_cli_rejects_invalid_configuration(self):
        """Reject malformed tables and out-of-range or boolean intervals."""
        for config in ({'collector': []}, {'collector': {'interval_seconds': True}},
                       {'collector': {'interval_seconds': 4}}, {'collector': {'interval_seconds': 86401}}):
            with self.subTest(config=config), patch('sys.stderr', new_callable=StringIO), self.assertRaises(SystemExit) as error:
                self.run_cli([], config)
            self.assertEqual(error.exception.code, 2)

    def test_cli_schedules_fast_polls_between_full_scans(self):
        """A fake clock proves fast readings run at their own configured cadence."""
        clock = [0.0]
        with patch.object(cli.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(
            cli.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        ):
            mocks = self.run_cli(['--interval', '30'], {},
                sync_configured_sources={'return_value': 0},
                poll_once={'side_effect': [1, KeyboardInterrupt]},
                wan_poll_sources={'return_value': [(1, {}, 10)]},
                ups_poll_sources={'return_value': [(2, {}, 15)]},
                live_poll_sources={'return_value': [(3, {}, 20)]},
                poll_wan_once={}, poll_ups_once={}, poll_proxmox_live_once={})
        self.assertEqual(mocks['poll_once'].call_count, 2)
        self.assertEqual(mocks['poll_wan_once'].call_count, 2)
        self.assertEqual(mocks['poll_ups_once'].call_count, 1)
        self.assertEqual(mocks['poll_proxmox_live_once'].call_count, 1)
        self.assertEqual(clock[0], 30)
