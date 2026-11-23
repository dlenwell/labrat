/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Check source-driven WAN labels and shared-gateway link routing. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const layer={children:[],replaceChildren(){this.children=[];},append(e){this.children.push(e);}};
const element=()=>({attrs:{},setAttribute(k,v){this.attrs[k]=v;},classList:{add(){}},append(){}});
const context=vm.createContext({document:{getElementById:id=>id==='topology-lines'?layer:null,createElementNS:element},window:{}});
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/topology.js',import.meta.url),'utf8'),context);
const run=code=>vm.runInContext(code,context);
run(`
const canonical={name:'My modem',category:'modem',observations:[
    {wan_upstream:true,reported_name:'Primary ISP WAN1 upstream'}]};
`);
assert.equal(run('labratNodeDisplayName(canonical)'),'Primary ISP WAN1 upstream');
assert.equal(run('canonical.name'),'My modem','Display names never overwrite canonical inventory');
assert.equal(run("labratNodeDisplayName({name:'Manual modem'})"),'Manual modem');
assert.equal(run("labratNodeDisplayName({name:'Custom',observations:[{reported_name:'Generic controller name'}]})"),'Custom');
assert.equal(run("labratNodeDisplayName({name:'Custom',observations:[{wan_upstream:true,reported_name:''}]})"),'Custom');
run(`
// Style and paint are independent concerns; exercise the real route selection.
labratPairStyle=()=>({classes:[],title:'WAN'});
labratPaint=()=>{};
const pair={from:'gateway',to:'wan2',links:[{evidence:'unifi-wan-upstream'}]};
labratGraph.layout={parent:new Map([['gateway','wan1']]),stacked:new Set(),
    positions:new Map([['wan1',{x:100,y:50}],['wan2',{x:300,y:50}],['gateway',{x:200,y:170}]]),
    pairs:new Map([['wan',pair]])};
labratDrawLinks();
`);
assert.equal(run('pair.route.kind'),'tree');
assert.equal(run('pair.route.upper'),'wan2');
assert.equal(run('pair.route.lower'),'gateway');
run(`pair.from='wan2';pair.to='gateway';labratDrawLinks();`);
assert.equal(run('pair.route.upper'),'wan2');
assert.equal(layer.children.length,1);
console.log('Discovery ISP labels and shared-gateway WAN routing checks passed');

// Adopted but unplugged devices with no link stay off the map.
assert.equal(run(`JSON.stringify([...labratDisconnectedNodes({
    nodes:[
        {id:'ap-off', observations:[{controller_state:'offline'}]},
        {id:'switch-off-cabled', observations:[{controller_state:'offline'}]},
        {id:'ap-on', observations:[{controller_state:'online'}]},
        {id:'merged', observations:[{controller_state:'offline'},{controller_state:null,status:'online'}]},
        {id:'manual', observations:[]}],
    links:[{from:'switch-off-cabled',to:'core'}], wireless_links:[]})])`), JSON.stringify(['ap-off']));
