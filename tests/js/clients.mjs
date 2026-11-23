/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Check endpoint classification, freshness, search and placement filters. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const context=vm.createContext({assert,document:{getElementById:()=>null}});
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/clients.js',import.meta.url),'utf8'),context);
const checks=String.raw`
const current={state:'connected',source_enabled:true,collection_error:false,sampled_at:new Date().toISOString(),connection:'wireless',manufacturer:'Example maker',device_category:'Printer',ip_address:'192.0.2.4',ssid:'Example Wi-Fi'};
const node={name:'User name',placement:null,observations:[{id:1,reported_name:'Controller name',client:current}]};
assert.equal(clientIcon({virtualization:{kind:'vm'}}),'vm');
assert.equal(clientIcon({virtualization:{kind:'container'}}),'container');
assert.equal(filterClients([{...node, observations:[{client:{...current,server_interfaces:['Example host']}}]}]).length,0);
assert.equal(filterClients([{...node, observations:[{client:{...current,server_interfaces:['Example host']}}]}],{showInterfaces:true}).length,1);
const high={...node,name:'Unit 10'},low={...node,name:'Unit 2'};
assert.equal(sortClients([high,low],'name','asc')[0].name,'Unit 2');
assert.equal(sortClients([high,low],'name','desc')[0].name,'Unit 10');
for (const key of ['type','name','address','connection','status','placement']) assert.equal(sortClients([high,low],key,'asc').length,2);
const guest={kind:'vm',vmid:101,name:'Example VM',host:'host-a',bridge:'vmbr0',fresh:true};
const vmNode={name:'example-vm 0a:0b',placement:null,observations:[{id:2,client:{...current,virtualization:guest}}]};
assert.equal(clientDisplayName(vmNode),'Example VM');
assert.equal(clientDisplayName({...vmNode,device_id:7}),'example-vm 0a:0b');
assert.equal(clientColumns(vmNode).type,'VM');
assert.equal(clientColumns(vmNode).placement,'Virtual');
assert.equal(filterClients([vmNode],{unplaced:true}).length,0);
assert.equal(clientColumns(vmNode).connection,'host-a - vmbr0');
assert.equal(clientColumns({...vmNode,observations:[{id:2,client:{...current,virtualization:{...guest,bridge:null,fresh:false}}}]}).connection,'host-a · cached');
assert.equal(clientIcon(current),'printer');
// Several clients of one guest (one per card) fold into one row.
const card=(id,ip,bridge,state='connected')=>({id,name:'vm 0'+id,placement:null,observations:[{id,client:{...current,state,ip_address:ip,mac_address:'02:00:00:00:00:0'+id,virtualization:{...guest,source_id:9,bridge}}}]});
const merged=mergeClientNodes([card(1,'192.0.2.10','vmbr0'),card(2,'192.0.2.11','vmbr1','not_seen'),card(3,null,'vmbr0'),{...node,id:4}]);
assert.equal(merged.length,2);
const vmRow=merged.find((row)=>row.merged_ids);
assert.deepEqual(vmRow.merged_ids,[1,2,3]);
assert.equal(clientAddresses(vmRow).length,3);
assert.equal(clientAddresses(vmRow)[2],'02:00:00:00:00:03');
assert.equal(clientObservation(vmRow).client.state,'connected');
assert.equal(clientAttachment(vmRow).detail,'vmbr0, vmbr1');
assert.equal(filterClients([vmRow],{query:'192.0.2.11'}).length,1);
assert.equal(mergeClientNodes([card(1,'192.0.2.10','vmbr0'),{...card(2,'192.0.2.11','vmbr0'),device_id:8}]).find((r)=>r.merged_ids).device_id,8);
const ap={name:'Example AP',observations:[{source_id:3,remote_id:'site:ap'}]};
const hv={name:'Example host',observations:[{source_id:9,remote_id:'host-a'}]};
const wifi={...node,observations:[{id:5,client:{...current,parent_source_id:3,uplink_remote_id:'site:ap'}}]};
assert.equal(clientAttachment(wifi,[ap]).device,ap);
assert.equal(clientAttachment(wifi,[ap]).detail,'Example Wi-Fi');
assert.equal(clientAttachment({...node,observations:[{id:6,client:{...current,connection:'wired',switch_port:4}}]}).detail,'Port 4');
const hosted={...node,observations:[{id:7,client:{...current,virtualization:{...guest,source_id:9}}}]};
assert.equal(clientAttachment(hosted,[hv]).device,hv);
assert.equal(clientAttachment(hosted,[hv]).detail,'vmbr0');
assert.equal(clientColumns(hosted,[hv]).connection,'Example host - vmbr0');
assert.equal(clientIcon({device_category:'Unknown',name:'Printer'}),'client');
assert.equal(clientIcon({device_model:'Smartphone'}),'phone');
assert.equal(clientIcon({operating_system:'Windows'}),'computer');
assert.equal(clientCondition(current).label,'Connected');
assert.equal(clientCondition({...current,is_blocked:true}).label,'Blocked');
assert.equal(clientCondition({...current,collection_error:true}).label,'Unavailable');
assert.equal(clientCondition({...current,source_enabled:false}).label,'Unavailable');
assert.equal(clientCondition({...current,sampled_at:'invalid'}).label,'Stale');
assert.equal(clientCondition({...current,sampled_at:'2000-01-01T00:00:00Z'}).label,'Stale');
assert.equal(clientCondition({...current,state:'not_seen'}).label,'Not seen');
assert.equal(filterClients([node],{query:'controller'}).length,1);
assert.equal(filterClients([node],{query:'192.0.2.4'}).length,1);
assert.equal(filterClients([node],{query:'maker'}).length,1);
assert.equal(filterClients([node],{kinds:['wired']}).length,0);
assert.equal(filterClients([node],{kinds:['wired','wireless']}).length,1);
assert.equal(filterClients([node],{kinds:[]}).length,0);
assert.equal(filterClients([node]).length,1);
assert.equal(clientKind(current),'wireless');
assert.equal(clientKind({...current,virtualization:{kind:'container'}}),'vm');
assert.equal(clientKind({connection:'wired'}),'wired');
assert.equal(filterClients([node],{unplaced:true}).length,1);
assert.equal(filterClients([node],{attention:true}).length,0);
assert.equal(filterClients([{...node,observations:[{id:1,client:{...current,state:'not_seen'}}]}],{attention:true}).length,1);
assert.equal(filterClients([node],{query:'missing'}).length,0);
assert.equal(filterClients([{...node,placement:{room_id:1}}],{unplaced:true}).length,0);
assert.equal(clientObservation({observations:[]}),undefined);
// Router sightings: evidence of an address in use, never "connected".
const seenAt=new Date().toISOString();
const sighting=(id,scope,extra={})=>({id:'observed:'+id,name:'workstation',placement:null,observations:[{id,source_id:20,client:{
  evidence:'router-sighting',state:'seen',seen_at:seenAt,sampled_at:seenAt,source_enabled:true,collection_error:false,
  mac_address:'02:00:00:00:10:01',ip_address:'203.0.113.12',ipv6:['2001:db8:1::a'],scope,network_name:scope.toUpperCase(),
  connection:'unknown',...extra}}]});
assert.equal(clientCondition(sighting(30,'opt1').observations[0].client).label,'Seen by router');
assert.equal(clientCondition(sighting(30,'opt1',{state:'lease'}).observations[0].client).label,'Lease only');
assert.equal(clientCondition(sighting(30,'opt1',{seen_at:'2000-01-01T00:00:00Z'}).observations[0].client).label,'Stale');
assert.equal(clientCondition(sighting(30,'opt1',{state:'not_seen'}).observations[0].client).label,'Not seen');
assert.equal(clientKind(sighting(30,'opt1').observations[0].client),'sighted');
assert.equal(filterClients([sighting(30,'opt1')],{kinds:['wired','wireless','vm']}).length,0);
assert.equal(filterClients([sighting(30,'opt1')],{query:'2001:db8:1::a'}).length,1);
// One MAC on two networks folds into one row naming both.
const twice=mergeClientNodes([sighting(31,'opt1'),sighting(32,'opt3')]);
assert.equal(twice.length,1);
assert.equal(clientAttachment(twice[0]).detail,'OPT1, OPT3');
assert.equal(clientColumns(twice[0]).connection,'Seen by router · OPT1, OPT3');
// A switch's attachment of the same MAC wins the row; the sighting joins it.
const wired={id:'observed:40',name:'Desk PC',placement:null,observations:[{id:40,client:{...current,connection:'wired',
  mac_address:'02:00:00:00:10:01',ip_address:'203.0.113.12',switch_port:7}}]};
const joined=mergeClientNodes([sighting(33,'opt1',{seen_at:'2000-01-01T00:00:00Z'}),wired]);
assert.equal(joined.length,1);
assert.equal(clientObservation(joined[0]).id,40);
assert.equal(clientColumns(joined[0]).status,'Connected');
assert.equal(clientAttachment(joined[0]).detail,'Port 7');
// A fresh sighting outranks a stale or disabled attachment, which stays as history.
const oldPort={...wired,observations:[{id:42,client:{...wired.observations[0].client,sampled_at:'2000-01-01T00:00:00Z'}}]};
const offPort={...wired,observations:[{id:43,client:{...wired.observations[0].client,source_enabled:false}}]};
for (const attachment of [oldPort,offPort]) {
  const row=mergeClientNodes([attachment,sighting(35,'opt1')])[0];
  assert.equal(clientObservation(row).id,35);
  assert.equal(clientColumns(row).status,'Seen by router');
  assert.equal(filterClients([row],{kinds:['sighted']}).length,1);
  assert.equal(clientObservations(row).length,2);
}
// Addresses: seen IPv6 when there is no IPv4, else a lease's.
assert.deepEqual(clientAddresses(sighting(36,'opt1',{ip_address:null})),['2001:db8:1::a']);
assert.deepEqual(clientAddresses(sighting(37,'opt1',{ip_address:null,ipv6:[],state:'lease',lease:{ip:'203.0.113.13'}})),['203.0.113.13']);
// A stale attachment that is no longer reported yields to a current sighting.
const gone={...wired,observations:[{id:41,client:{...wired.observations[0].client,state:'not_seen'}}]};
assert.equal(clientObservation(mergeClientNodes([gone,sighting(34,'opt1')])[0]).id,34);
`;
vm.runInContext(checks,context);
console.log('clients checks passed');
