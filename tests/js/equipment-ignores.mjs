/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Run with `node tests/js/equipment-ignores.mjs`; no network or browser required. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({
    // main.js binds page-wide menus at load; with no elements it binds none.
    document: { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, hidden: false },
    window: {}, AbortSignal,
});
for (const file of ['main.js', 'wan.js', 'device-details.js', 'rack-port-lights.js', 'racks.js', 'rack-cables.js', 'rack-arrange-mode.js', 'rack-live.js']) {
    vm.runInContext(fs.readFileSync(new URL(`../../src/labrat/static/js/${file}`, import.meta.url), 'utf8'), context);
}
const evaluate = code => vm.runInContext(code, context);
evaluate(`
    const rack = Object.create(RackViewApp.prototype);
    rack.roomId=1; rack.ignoreTargets=new Set(); rack.sources=[];
    // Port state and neighbors live in the shared DeviceDetails; build it
    // without its constructor (no drawer DOM here) over the rack's data.
    rack.details=Object.create(DeviceDetails.prototype);
    rack.details.nodes=()=>rack.nodes; rack.details.links=()=>rack.links; rack.details.cables=new Map();
    rack.nodes=[{id:'switch',name:'Switch',category:'switch',placement:{container_room_id:1},
        observations:[{id:1,source_id:1,remote_id:'switch-remote',source:'unifi:test',last_seen_at:new Date().toISOString(),
            neighbors:[{switch_observation_id:1,local_port:'1',chassis_id:'peer',port_id:'a',resolved:false},
                       {switch_observation_id:1,local_port:'2',chassis_id:'peer',port_id:'b',resolved:false}]}],
        ports:[{id:10,observation_id:1,remote_id:'1',name:'Port 1',link_up:false},
               {id:11,observation_id:1,remote_id:'2',name:'Port 2',link_up:false}]}];
    rack.links=[{from:'switch',from_port:'Port 1',to:'far',to_port:'1'},
                {from:'switch',from_port:'Port 2',to:'far',to_port:'2'}];
    rack.indexCables();
    const rule=(condition,extra={})=>({target:{condition,source_id:1,device_remote_id:'switch-remote',...extra}});
    const count=id=>rack.roomAlarms().find(alarm=>alarm.id===id).count;
`);
assert.equal(evaluate(`count('link-down')`), 1);
assert.equal(evaluate(`rack.setIgnoreRules([rule('port-down',{port_remote_id:'1'})])`), true);
assert.equal(evaluate(`rack.portAlarmState(rack.nodes[0].ports[0],'switch')`), 'off');
assert.equal(evaluate(`rack.nodes[0].ports[0].link_up`), false, 'Ignoring never changes actual port state');
assert.equal(evaluate(`rack.portAlarmState(rack.nodes[0].ports[1],'switch')`), 'fault');
assert.equal(evaluate(`count('link-down')`), 1, 'The other port still warns');
evaluate(`rack.nodes[0].ports[0].id=999;`);
assert.equal(evaluate(`rack.portAlarmState(rack.nodes[0].ports[0],'switch')`), 'off', 'Regenerated row IDs do not invalidate rules');
evaluate(`rack.setIgnoreRules([rule('port-down',{port_remote_id:'1'}),rule('port-down',{port_remote_id:'2'})]);`);
assert.equal(evaluate(`count('link-down')`), 0);
assert.equal(evaluate(`count('neighbor')`), 2, 'Port-down rules do not hide neighbor warnings');
evaluate(`rack.setIgnoreRules([rule('neighbor-unresolved',{port_remote_id:'1',chassis_id:'peer',remote_port_id:'a'})]);`);
assert.equal(evaluate(`count('neighbor')`), 1, 'Only the exact neighbor is ignored');
assert.equal(evaluate(`rack.neighborsFor(rack.nodes[0]).length`), 2, 'Inspector evidence remains available');
assert.equal(evaluate(`count('link-down')`), 1, 'Restored port rules show the warnings again');
evaluate(`
    rack.sources=[{id:1,name:'unifi:test',last_error:'Collection failed: Timeout',last_success_at:'2000-01-01T00:00:00Z'}];
    rack.setIgnoreRules([{target:{condition:'source-fault',source_id:1}}]);
`);
assert.equal(evaluate(`count('source-fault')`), 0);
assert.equal(evaluate(`count('scan-overdue')`), 1, 'Source fault ignore does not hide overdue collection');
evaluate(`
    rack.nodes.push({id:'server',name:'Server',category:'server',placement:{container_room_id:1},ports:[],
        observations:[{id:2,source_id:2,remote_id:'server-remote',source:'proxmox:test',status:'offline',last_seen_at:'2000-01-01T00:00:00Z'}]});
    rack.setIgnoreRules([{target:{condition:'server-offline',source_id:2,device_remote_id:'server-remote'}}]);
`);
assert.equal(evaluate(`count('server-down')`), 0);
assert.equal(evaluate(`count('not-seen')`), 1, 'Offline ignore does not hide stale observations');
evaluate(`rack.setIgnoreRules([{target:{condition:'device-stale',source_id:2,device_remote_id:'server-remote'}}]);`);
assert.equal(evaluate(`count('not-seen')`), 0);
assert.equal(evaluate(`count('server-down')`), 1);
evaluate(`
    rack.nodes.push({id:'gateway',name:'Gateway',category:'router',placement:{container_room_id:1},ports:[],
        observations:[{id:3,source_id:3,remote_id:'gateway-remote',source:'unifi:wan',last_seen_at:new Date().toISOString()}]});
    window.labratWan.gateways=[{node_id:'gateway',name:'Gateway',source:'unifi:wan',wan:{uplinks:[{enabled:true,up:false}]}},
        {node_id:'outside-room',name:'Other room gateway',source:'unifi:wan',wan:{uplinks:[{enabled:true,up:false}]}}];
    rack.setIgnoreRules([{target:{condition:'wan-down',source_id:3,device_remote_id:'gateway-remote'}}]);
`);
assert.equal(evaluate(`count('internet-down')`), 0, 'Exact WAN policy applies and other rooms stay out of scope');
evaluate(`rack.setIgnoreRules([]);`);
assert.equal(evaluate(`count('internet-down')`), 1);

// A policy change must propagate even when the source signature is unchanged.
evaluate(`
    rack.sourceStamp=rack.sourceSignature(rack.sources);
    rack.render=(options)=>{rack.lastRenderOptions=options;};
    rack.renderEventLine=()=>{};
    globalThis.fetch=async url=>({ok:true,status:200,json:async()=>url.endsWith('/sources')
        ? {sources:rack.sources} : {ignores:[rule('port-down',{port_remote_id:'1'})]}});
`);
await evaluate('rack.pollSources()');
assert.equal(evaluate('rack.lastRenderOptions.preserveForms'), true);
assert.equal(evaluate(`rack.portAlarmState(rack.nodes[0].ports[0],'switch')`), 'off');
console.log('Equipment exact-ignore, restore, raw-evidence, room-scope, and live-policy checks passed.');
