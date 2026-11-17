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
