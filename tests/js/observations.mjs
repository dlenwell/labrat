/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Run with `node tests/js/observations.mjs`; no browser or network required. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({
    document: { getElementById: () => null, querySelector: () => null },
    window: {}, location: { href: 'http://localhost/observations?room=2' }, URL,
});
for (const file of ['wan.js', 'observations.js']) {
    vm.runInContext(fs.readFileSync(new URL(`../../src/labrat/static/js/${file}`, import.meta.url), 'utf8'), context);
}
const evaluate = code => vm.runInContext(code, context);
assert.equal(evaluate('observationPortState({link_up: false, speed_mbps: 10000})'), 'down');
assert.equal(evaluate('observationPortState({link_up: null, speed_mbps: 10000})'), 'unknown');
assert.equal(evaluate('observationPortState({link_up: true})'), 'up');
assert.equal(evaluate('observationNodeState({observations: []})'), 'unknown');
assert.equal(evaluate(`observationNodeState({observations: [{status:'offline',last_seen_at:'2000-01-01T00:00:00Z'}]})`), 'stale');
assert.equal(evaluate(`observationNodeState({observations: [{status:'offline',last_seen_at:new Date().toISOString()}]})`), 'offline');
assert.equal(evaluate(`observationSourceState({last_success_at:null}).overdue`), true);
assert.equal(evaluate(`observationSourceState({last_success_at:'2026-01-01T00:00:00Z', last_attempt_at:'2026-01-01T00:00:00Z',poll_interval_seconds:60},Date.parse('2026-01-01T00:02:59Z')).overdue`), false);
assert.equal(evaluate(`observationSourceState({last_success_at:'2026-01-01T00:00:00Z', last_attempt_at:'2026-01-01T00:00:00Z',poll_interval_seconds:60},Date.parse('2026-01-01T00:03:01Z')).overdue`), true);
evaluate(`
    const monitor = new ObservationConsole();
    monitor.data = {sources: [],links: [],nodes: [
        {id:'a', name:'Room two server', category:'server', placement:{container_room_id:2}, ports:[], observations:[{status:'online', last_seen_at:new Date().toISOString()}]},
        {id:'b', name:'Other room server', category:'server', placement:{container_room_id:1}, ports:[], observations:[{status:'offline', last_seen_at:new Date().toISOString()}]},
        {id:'c', name:'Unplaced server', category:'server', placement:null, ports:[], observations:[]}
    ]};
    monitor.gateways = [{node_id:'b', name:'Other room gateway',wan:{sampled_at:new Date().toISOString(),uplinks:[{enabled:true,up:false}]}}];
`);
assert.equal(evaluate('monitor.scopedNodes().length'), 1);
assert.equal(evaluate('monitor.scopedGateways().length'), 0);
assert.equal(evaluate('monitor.warnings().length'), 0);
evaluate(`monitor.room='';`);
assert.equal(evaluate('monitor.scopedNodes().length'), 3);
assert.equal(evaluate('monitor.warnings().filter(row=>row.severity === "alarm").length'), 2);
evaluate(`monitor.recordChanges(monitor.data, monitor.gateways); monitor.recordChanges(monitor.data, monitor.gateways);`);
assert.equal(evaluate('monitor.events.length'), 1, 'Identical snapshots do not invent events');
evaluate(`monitor.data.nodes[0].observations[0].status='offline'; monitor.recordChanges(monitor.data, monitor.gateways);`);
assert.equal(evaluate('monitor.events.length'), 2, 'A real status transition produces one event');
console.log('Observations state, scope, stale-data, and event checks passed.');

// A client that left is ordinary churn until it is in inventory.
evaluate(`
    monitor.room=''; monitor.events=[]; monitor.gateways=[]; monitor.ignores=[];
    const gone = {status:null, last_seen_at:'2000-01-01T00:00:00Z', client:{state:'not_seen'}};
    monitor.data = {sources: [], links: [], nodes: [
        {id:'phone', name:'Phone', category:'other_physical', client_only:true, device_id:null, placement:null, ports:[], observations:[gone]},
        {id:'nas', name:'NAS', category:'other_physical', client_only:true, device_id:7, placement:null, ports:[], observations:[gone]},
        {id:'switch', name:'Switch', category:'switch', placement:null, ports:[], observations:[{status:'online', last_seen_at:'2000-01-01T00:00:00Z'}]}
    ]};
`);
assert.deepEqual(evaluate('JSON.stringify(monitor.warnings().map(row => [row.title, row.detail]))'),
    JSON.stringify([['Not observed for an hour', 'NAS'], ['Not observed for an hour', 'Switch']]));
evaluate('monitor.recordChanges(monitor.data, monitor.gateways); monitor.data.nodes[0].observations[0].last_seen_at = new Date().toISOString(); monitor.recordChanges(monitor.data, monitor.gateways);');
assert.equal(evaluate('monitor.events.some(event => event.message.startsWith("Phone"))'), false, 'A passing client adds no events');
console.log('Passing-client checks passed.');

// A rule identifies one remote port, not its regenerated database row ID.
evaluate(`
    monitor.data = {sources: [], nodes: [{id:'switch', name:'Switch', category:'switch', placement:null,
        observations:[{id:5,source_id:1,remote_id:'remote-switch',source:'unifi:test',last_seen_at:new Date().toISOString()}],
        ports:[{id:10,observation_id:5,remote_id:'1',name:'Port 1',link_up:false},
               {id:11,observation_id:5,remote_id:'2',name:'Port 2',link_up:false}]}],
        links:[{from:'switch',from_port:'Port 1',to:'far',to_port:'1',confidence:'confirmed',evidence:'lldp'},
               {from:'switch',from_port:'Port 2',to:'far',to_port:'2',confidence:'confirmed',evidence:'lldp'}]};
    monitor.gateways=[];
    monitor.ignores=[{id:1,target:{condition:'port-down',source_id:1,device_remote_id:'remote-switch',port_remote_id:'1'}}];
`);
assert.equal(evaluate('monitor.warnings().length'), 1);
assert.equal(evaluate('monitor.warnings()[0].target.port_remote_id'), '2');
evaluate('monitor.data.nodes[0].ports[0].id=999;');
assert.equal(evaluate('monitor.warnings().length'), 1, 'A new database ID does not un-ignore the same physical port');
evaluate('monitor.ignores=[];');
assert.equal(evaluate('monitor.warnings().length'), 2, 'Restore reveals the exact condition again');
console.log('Exact-port ignore and restore checks passed.');

// Router watchlist conditions: one row per item, ignored by its exact item
// (and, for CARP, the state it changed to).
evaluate(`
    const now = new Date().toISOString();
    monitor.gateways = []; monitor.ignores = [];
    monitor.data = {sources: [], links: [], nodes: [{id:'fw', name:'Edge', category:'firewall', placement:null, ports:[],
        observations:[{id:9, source_id:3, remote_id:'firewall', source:'opnsense:Edge', status:null, last_seen_at:now,
            watch_conditions:[
                {condition:'router-gateway-down', item:'gateway:PEER_GW', kind:'gateway', label:'PEER_GW', entered_at:now, sampled_at:now, watch_sampled_at:now},
                {condition:'router-peer-stale', item:'wireguard:wg0:abc', kind:'wireguard', label:'peer-firewall', entered_at:now, sampled_at:'t0', watch_sampled_at:now},
                {condition:'router-carp-changed', item:'carp:opt4:1', kind:'carp', label:'203.0.113.198', entered_at:now, from:'master', to:'backup', sampled_at:now, watch_sampled_at:now}]}]}]};
`);
assert.equal(evaluate('JSON.stringify(monitor.warnings().map(row => [row.severity, row.title]))'),
    JSON.stringify([['alarm', 'Gateway down'], ['warning', 'VPN peer stale'], ['warning', 'CARP state changed']]));
assert.equal(evaluate('monitor.warnings().find(row => row.title === "VPN peer stale").facts.some(([key]) => key === "No data since")'), true);
assert.equal(evaluate('monitor.warnings().find(row => row.title === "CARP state changed").target.state'), 'backup');
evaluate(`monitor.ignores = [{id:2, target:{condition:'router-carp-changed', source_id:3, device_remote_id:'firewall', item:'carp:opt4:1', state:'backup', since:now}}];`);
assert.equal(evaluate('monitor.warnings().length'), 2, 'Acknowledging a CARP change hides only that change');
evaluate(`monitor.data.nodes[0].observations[0].watch_conditions[2].to = 'init';`);
assert.equal(evaluate('monitor.warnings().length'), 3, 'A change to another state shows again');
evaluate(`monitor.data.nodes[0].observations[0].watch_conditions[2].to = 'backup'; monitor.data.nodes[0].observations[0].watch_conditions[2].entered_at = 'later';`);
assert.equal(evaluate('monitor.warnings().length'), 3, 'A later change to the same state shows again');
// A failing or overdue source: the condition stays, as last known.
evaluate(`monitor.data.sources = [{id:3, name:'opnsense:Edge', last_error:'Collection failed: Timeout', last_success_at:now, last_attempt_at:now, poll_interval_seconds:300}]; monitor.ignores = [];`);
assert.equal(evaluate('monitor.warnings().filter(row => row.title === "Gateway down").length'), 1);
assert.equal(evaluate('monitor.warnings().find(row => row.title === "Gateway down").facts.some(([key]) => key === "No data since")'), true);
assert.equal(evaluate('monitor.warnings().find(row => row.title === "Gateway down").detail.endsWith("last known")'), true);
evaluate(`monitor.data.sources[0].last_error = null; monitor.data.sources[0].last_success_at = monitor.data.sources[0].last_attempt_at = '2000-01-01T00:00:00Z';`);
assert.equal(evaluate('monitor.warnings().find(row => row.title === "Gateway down").facts.some(([key]) => key === "No data since")'), true, 'overdue');
console.log('Router watchlist checks passed.');
