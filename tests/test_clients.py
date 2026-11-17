# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Test client collection, identity boundaries, placement, and scan authorization."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select

import test_step4c as fixture
from labrat.api.clients import COMMON_PORTS, _SCAN_TIMES, scan_permission
from labrat.collector.clients import client_snapshot, reconcile_clients, poll_clients, text
from labrat.collector.contracts import DeviceObservation, DiscoveryBatch, PortObservation
from labrat.collector.store import apply_batch
from labrat.discovery.models import DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource
from labrat.models import Device, User

CONFIG = {'unifi': {'url': 'https://controller.example.test', 'api_key': 'test-only-key'},
          'scan': {'ranges': ['192.0.2.0/28']}, 'discovery': {'network': True}}
MAC = '02:00:00:00:00:44'
SWITCH = '02:00:00:00:00:01'


class ClientTests(unittest.TestCase):
    """Use migrated disposable storage and mocked controller/transport calls."""
    setUp = fixture.Step4CTests.setUp
    post = fixture.Step4CTests.post
    create_locations = fixture.Step4CTests.create_locations

    def sources(self):
        """Create one infrastructure source and its derived endpoint source."""
        with self.factory.begin() as db:
            parent = DiscoverySource(name='UniFi example', kind='unifi', config_name='default')
            source = DiscoverySource(name='Clients example', kind='unifi_clients', config_name='default')
            db.add_all([parent, source]); db.flush()
            self.parent_id, self.source_id = parent.id, source.id
        _SCAN_TIMES.clear()

    def record(self, **changes):
        """Make one sanitized wired client record with explicit controller evidence.

        Args:
            **changes: Fields to override.

        Returns:
            dict: The client record.
        """
        return {'remote_id':'site:client:one', 'site_id':'site', 'site_name':'Example site',
                'controller_id':'one', 'name':'Example client', 'ip_address':'192.0.2.4',
                'mac_address':MAC, 'connection':'wired', 'state':'connected',
                'connected_at':None, 'uplink_remote_id':'site:switch', 'classic_available':True,
                'switch_mac':SWITCH.replace(':',''), 'switch_port':1, **changes}

    def collect(self, records=None, **kwargs):
        """Apply a complete client snapshot, returning its retained observation ID.

        Args:
            records: The snapshot's clients; one default record when None.
            **kwargs: Passed to the collector.

        Returns:
            int: The retained observation's ID.
        """
        with self.factory.begin() as db:
            reconcile_clients(db, db.get(DiscoverySource,self.source_id), db.get(DiscoverySource,self.parent_id),
                              records if records is not None else [self.record()], **kwargs)
            row = db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.source_id==self.source_id))
            return row.id if row else None

    def switch(self):
        """Insert a real observed switch and port for attachment reconciliation."""
        with self.factory.begin() as db:
            apply_batch(db,db.get(DiscoverySource,self.parent_id),DiscoveryBatch(
                devices=(DeviceObservation('site:switch','Example switch','switch',
                    identifiers={'mac':SWITCH},details={'site_id':'site'}),),
                ports=(PortObservation('site:switch','1','Port 1',link_up=True),)))

    def adapter(self, clients=None, classic=None, sites=None):
        """Fake the supported overview and optional classic response independently.

        Args:
            clients: The overview's clients.
            classic: The classic stat/sta records.
            sites: The controller's sites.

        Returns:
            Mock: The fake adapter.
        """
        adapter=Mock()
        adapter._page.side_effect=lambda base,path,*args,**kwargs: sites if path=='sites' else clients
        adapter.client.get_json.return_value={'data':classic or []}
        return adapter

    def test_snapshot_whitelist_and_optional_telemetry(self):
        """Keep supported fields and units; reject raw blobs, secrets and stale detail."""
        overview=[{'id':'one','name':'Printer','macAddress':MAC,'ipAddress':'192.0.2.4',
                   'type':'WIRED','uplinkDeviceId':'switch','connectedAt':'2026-01-01T00:00:00Z'}]
        classic=[{'mac':MAC,'last_seen':datetime.now(timezone.utc).timestamp(), 'is_wired':True,
                  'sw_mac':SWITCH,'sw_port':1,'oui':'Example manufacturer','dev_cat':'Printer',
                  'signal':-42,'rx_bytes':123,'is_guest':False,'password':'not-persisted', 'secret_blob':{'token':'not-persisted'}}]
        adapter=self.adapter(overview,classic,[{'id':'site','name':'Example site','internalReference':'default'}])
        result=client_snapshot(adapter,CONFIG['unifi'])[0]
        self.assertEqual(result['device_category'],'Printer')
        self.assertEqual(result['switch_port'],1)
        self.assertEqual(result['signal_dbm'],-42)
        self.assertFalse(result['is_guest'])
        self.assertNotIn('not-persisted',json.dumps(result))
        classic[0]['last_seen']=1
        self.assertFalse(client_snapshot(adapter,CONFIG['unifi'])[0]['classic_available'])
        adapter.client.get_json.side_effect=ValueError('unsupported')
        self.assertEqual(client_snapshot(adapter,CONFIG['unifi'])[0]['connection'],'wired')
        self.assertIsNone(text('bad\ntext'))
        self.assertIsNone(text({}))

    def test_snapshot_bounds_missing_identity_and_duplicate(self):
        """Incomplete collections cannot establish absence or unbounded work."""
        sites=[{'id':'site'}]
        adapter=self.adapter([{'id':'a','type':'WIRELESS','ipAddress':'invalid'}],sites=sites)
        record=client_snapshot(adapter,CONFIG['unifi'])[0]
        self.assertIsNone(record['ip_address']); self.assertIsNone(record['mac_address'])
        self.assertEqual(record['connection'],'wireless')
        adapter=self.adapter([{}],sites=sites)
        self.assertEqual(client_snapshot(adapter,CONFIG['unifi']),[])
        for rows,s in [([{'id':'a'},{'id':'a'}],sites),([{'id':str(i)} for i in range(2001)],sites),([], [{}]),([],sites*17)]:
            with self.subTest(rows=len(rows),sites=len(s)),self.assertRaises(ValueError):
                client_snapshot(self.adapter(rows,sites=s),CONFIG['unifi'])

    def test_wired_reconciliation_roaming_absence_and_name_persistence(self):
        """Infer only eligible cables and preserve user inventory on later scans."""
        self.sources();self.switch();observed_id=self.collect()
        with self.factory.begin() as db:
            link=db.scalar(select(DiscoveredLink));self.assertEqual(link.evidence,'unifi-client-attachment')
            device=Device(name='User label');db.add(device);db.flush()
            db.get(DiscoveredDevice,observed_id).device_id=device.id
            inventory_id=device.id
        self.collect([self.record(name='Changed reported name')])
        with self.factory() as db:self.assertEqual(db.get(Device,inventory_id).name,'User label')
        self.collect([self.record(connection='wireless')])
        with self.factory() as db:
            self.assertEqual(list(db.scalars(select(DiscoveredLink))),[])
            self.assertEqual(list(db.scalars(select(DiscoveredPort).where(DiscoveredPort.discovered_device_id==observed_id))),[])
        self.collect([])
        with self.factory() as db:
            row=db.get(DiscoveredDevice,observed_id)
            self.assertEqual(json.loads(row.details_json)['client']['state'],'not_seen')
            self.assertEqual(row.device_id,inventory_id)

    def test_down_occupied_or_unknown_port_never_invents_cable(self):
        """A contradictory or absent upstream port cannot support client wiring."""
        self.sources();self.switch()
        with self.factory.begin() as db:db.scalar(select(DiscoveredPort)).link_up=False
        self.collect()
        with self.factory() as db:self.assertIsNone(db.scalar(select(DiscoveredLink)))
        self.collect([self.record(switch_port=99)])
        with self.factory() as db:self.assertIsNone(db.scalar(select(DiscoveredLink)))
        self.collect([self.record(switch_mac='aabbccddeeff')])
        with self.factory() as db:self.assertIsNone(db.scalar(select(DiscoveredLink)))

    def test_exact_mac_alias_does_not_merge_inventory_or_flood_diagram(self):
        """An exact physical MAC can deduplicate display, with explicit identity writes."""
        self.sources();self.switch()
        with self.factory.begin() as db:
            host=DiscoveredDevice(source_id=self.parent_id,remote_id='existing',name='Existing server',category='server',
                                  identifiers_json=json.dumps({'mac':MAC}))
            db.add(host);db.flush();host_id=host.id
        self.collect()
        graph=self.client.get('/api/v1/topology').json
        node=next(n for n in graph['nodes'] if any(o.get('client') for o in n['observations']))
        self.assertFalse(node['client_only']);self.assertEqual(node['discovery_id'],host_id)
        self.assertEqual(len(graph['nodes']),2)
        with self.factory() as db:self.assertEqual(list(db.scalars(select(Device))),[])

    def test_independent_poll_health_and_retention(self):
        """Optional client failure retains evidence and the last successful timestamp."""
        self.sources()
        with self.factory() as db:parent=db.get(DiscoverySource,self.parent_id)
        with patch('labrat.collector.clients.client_snapshot',return_value=[self.record()]):
            poll_clients(self.factory,parent,Mock(),CONFIG['unifi'],60)
        with self.factory() as db:before=db.get(DiscoverySource,self.source_id).last_success_at
        with patch('labrat.collector.clients.client_snapshot',side_effect=ValueError('private-error')):
            poll_clients(self.factory,parent,Mock(),CONFIG['unifi'])
        with self.factory() as db:
            source=db.get(DiscoverySource,self.source_id)
            self.assertEqual(source.last_success_at,before)
            self.assertEqual(source.last_error,'Client collection unavailable')
            self.assertEqual(len(list(db.scalars(select(DiscoveredDevice)))),1)

    def test_client_page_data_inventory_and_placement(self):
        """Browse, explicitly create inventory, rename, and place through shared APIs."""
        self.sources();observed=self.collect()
        page=self.client.get('/topology/clients').get_data(as_text=True)
        self.assertIn('aria-label="Discovery views"',page)
        self.assertIn('client-row-template',page)
        with patch('labrat.api.clients._config',return_value=CONFIG):
            payload=self.client.get('/api/v1/clients').json
        self.assertEqual(len(payload['clients']),1)
        self.assertTrue(payload['clients'][0]['client_only'])
        self.assertTrue(payload['clients'][0]['observations'][0]['client']['scan_allowed'])
        response=self.post(f'/api/v1/clients/{observed}/inventory',{})
        self.assertEqual(response.status_code,200);inventory=response.json['device_id']
        self.assertEqual(self.post(f'/api/v1/clients/{observed}/inventory',{}).json['device_id'],inventory)
        self.assertEqual(self.post(f'/api/v1/rack-devices/{inventory}/name',{'name':'Personal display label'}).status_code,200)
        _,room,_=self.create_locations()
        locations=self.client.get('/api/v1/locations').json
        floor=next(c for c in locations['containers'] if c['room_id']==room and c['kind']=='floor')
        response=self.post('/api/v1/placements',{'discovery_id':observed,'container_id':floor['id']})
        self.assertEqual(response.status_code,200,response.json)
        self.collect([self.record(name='Collector label')])
        graph=self.client.get('/api/v1/topology').json
        node=next(n for n in graph['nodes'] if n['device_id']==inventory)
        self.assertEqual(node['name'],'Personal display label');self.assertEqual(node['placement']['container_room_id'],room)

    def test_scan_exact_scope_results_and_throttle(self):
        """Only the saved numeric target and fixed TCP set reach the socket helper."""
        self.sources();observed=self.collect()
        with patch('labrat.api.clients._config',return_value=CONFIG),patch('labrat.api.clients.port_open',side_effect=lambda host,port,timeout:port in {22,443}) as check:
            response=self.post(f'/api/v1/clients/{observed}/scan',{})
            self.assertEqual(response.status_code,200,response.json)
            self.assertEqual([p['port'] for p in response.json['open_ports']],[22,443])
            self.assertEqual(check.call_count,len(COMMON_PORTS))
            self.assertTrue(all(call.args[0]=='192.0.2.4' for call in check.call_args_list))
            self.assertEqual(self.post(f'/api/v1/clients/{observed}/scan',{}).status_code,429)
            self.assertEqual(self.post(f'/api/v1/clients/{observed}/scan',{'target':'192.0.2.5'}).status_code,400)
        with patch('labrat.api.clients._config',return_value=CONFIG),patch('labrat.api.clients._SCAN_SLOTS.acquire',return_value=False):
            _SCAN_TIMES.clear()
            self.assertEqual(self.post(f'/api/v1/clients/{observed}/scan',{}).status_code,429)

    def test_scan_permission_failures_make_no_connections(self):
        """Disabled scanning, out-of-range IPs, stale data and source failures deny."""
        self.sources();observed=self.collect()
        configurations=[{**CONFIG,'discovery':{'network':False}}, {**CONFIG,'scan':{'ranges':[]}},
                        {**CONFIG,'scan':{'ranges':['invalid']}}]
        for config in configurations:
            with self.subTest(config=config),patch('labrat.api.clients._config',return_value=config),patch('labrat.api.clients.port_open') as check:
                self.assertEqual(self.post(f'/api/v1/clients/{observed}/scan',{}).status_code,403)
                check.assert_not_called()
        for changes in [{'ip_address':'127.0.0.1'}, {'ip_address':'invalid'}, {'state':'not_seen'}, {'sampled_at':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()}]:
            with self.factory.begin() as db:
                row=db.get(DiscoveredDevice,observed);details=json.loads(row.details_json)
                details['client'].update(changes);row.details_json=json.dumps(details)
            with patch('labrat.api.clients._config',return_value=CONFIG),patch('labrat.api.clients.port_open') as check:
                self.assertEqual(self.post(f'/api/v1/clients/{observed}/scan',{}).status_code,403);check.assert_not_called()
        self.collect()
        with self.factory.begin() as db:db.get(DiscoverySource,self.parent_id).enabled=False
        with patch('labrat.api.clients._config',return_value=CONFIG):
            self.assertEqual(self.post(f'/api/v1/clients/{observed}/scan',{}).status_code,403)
            self.assertFalse(self.client.get('/api/v1/clients').json['clients'][0]['observations'][0]['client']['source_enabled'])

    def test_scan_and_inventory_auth_csrf_and_missing(self):
        """Client writes require both administrator privileges and session CSRF."""
        self.sources();observed=self.collect()
        for suffix in ('scan','inventory'):
            self.assertEqual(self.client.post(f'/api/v1/clients/{observed}/{suffix}',json={}).status_code,403)
            self.assertEqual(self.post(f'/api/v1/clients/99999/{suffix}',{}).status_code,404)
        with self.factory.begin() as db:db.scalar(select(User)).is_admin=False
        for suffix in ('scan','inventory'):
            self.assertEqual(self.post(f'/api/v1/clients/{observed}/{suffix}',{}).status_code,403)
        self.client.post('/signout',data={'_csrf_token':self.csrf})
        self.assertEqual(self.client.get('/api/v1/clients').status_code,401)


class ClientEvidenceTests(unittest.TestCase):
    """Additional checks for snapshot freshness and controller fingerprint labels."""
    setUp = ClientTests.setUp
    sources = ClientTests.sources
    record = ClientTests.record
    collect = ClientTests.collect
    switch = ClientTests.switch

    def test_fingerprint_ids_are_resolved_without_remote_artwork(self):
        """Use only bounded labels from the reporting controller's catalogue."""
        from labrat.collector.clients import fingerprint_catalog, fingerprint_labels
        catalogue={'dev_ids':{'10':{'name':'Example notebook','dev_type_id':'1','family_id':'2','vendor_id':'3','os_name_id':'4'}},
                   'dev_type_ids':{'1':'Laptop'},'family_ids':{'2':'Computer'},'vendor_ids':{'3':'Example maker'},'os_name_ids':{'4':'Example OS'}}
        adapter=Mock();adapter.client.get_json.return_value=catalogue
        first=fingerprint_catalog(adapter,'https://controller.example.test',{},True)
        self.assertEqual(fingerprint_catalog(adapter,'https://controller.example.test',{},True),first)
        self.assertEqual(adapter.client.get_json.call_count,1)
        labels=fingerprint_labels({'dev_id':10},first)
        self.assertEqual(labels['device_category'],'Laptop')
        self.assertEqual(labels['device_vendor'],'Example maker')
        self.assertEqual(labels['device_model'],'Example notebook')
        self.assertEqual(fingerprint_labels({'dev_cat':999},first)['device_category_id'],'999')
        self.assertEqual(fingerprint_labels({'dev_cat':'Printer'},{}),{})

    def test_client_page_bounds_and_changing_totals(self):
        """Stop incomplete or excessive paginated data before reconciling absence."""
        from labrat.collector.adapters.unifi import UniFiAdapter
        client = Mock()
        client.get_json.return_value = {'data': [], 'totalCount': 2001}
        adapter = UniFiAdapter(client)
        with self.assertRaises(ValueError):
            adapter._page('https://controller.example.test', 'clients', {}, True, max_records=2000)
        client.get_json.side_effect = [
            {'data': [{'id':'one'}], 'totalCount': 3, 'offset':0, 'count':1},
            {'data': [{'id':'two'}], 'totalCount':2, 'offset':1, 'count':1},
        ]
        with self.assertRaises(ValueError):
            adapter._page('https://controller.example.test', 'clients', {}, True, max_records=2000)

    def test_failed_optional_detail_retains_aged_wired_evidence(self):
        """A failed classic request cannot manufacture a fresh attachment timestamp."""
        self.sources();self.switch()
        earlier=datetime.now(timezone.utc)-timedelta(minutes=3)
        self.collect(sampled_at=earlier)
        self.collect([{k:v for k,v in self.record(classic_failed=True).items() if k not in {'switch_mac','switch_port'}}])
        with self.factory() as db:
            link=db.scalar(select(DiscoveredLink))
            self.assertIsNotNone(link)
            self.assertEqual(link.last_seen_at.replace(tzinfo=timezone.utc),earlier)
        self.collect([self.record(connection='wireless',classic_failed=True)])
        with self.factory() as db:self.assertIsNone(db.scalar(select(DiscoveredLink)))

    def test_multiple_clients_on_one_port_are_ambiguous(self):
        """Never pick an arbitrary endpoint when multiple clients share one port."""
        self.sources();self.switch()
        self.collect([self.record(),self.record(remote_id='site:client:two',mac_address='02:00:00:00:00:55')])
        with self.factory() as db:self.assertIsNone(db.scalar(select(DiscoveredLink)))

    def test_wireless_edges_are_separate_from_physical_ports(self):
        """A current Wi-Fi client has an AP association, with no physical cable."""
        self.sources()
        with self.factory.begin() as db:
            apply_batch(db,db.get(DiscoverySource,self.parent_id),DiscoveryBatch(devices=(DeviceObservation('site:ap','Example AP','access_point'),)))
        self.collect([self.record(connection='wireless',uplink_remote_id='site:ap')])
        graph=self.client.get('/api/v1/topology').json
        self.assertEqual(len(graph['wireless_links']),1)
        self.assertEqual(graph['links'],[])
        client=next(n for n in graph['nodes'] if n['client_only'])
        self.assertEqual(client['ports'],[])
        self.collect([])
        self.assertEqual(self.client.get('/api/v1/topology').json['wireless_links'],[])


if __name__ == '__main__':
    unittest.main()
