/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Exercise actual Equipment SVG rendering with measured stand-in elements. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
    constructor(x=0,y=0,width=10,height=10) {
        this.rect={left:x,top:y,right:x+width,bottom:y+height,width,height};
        this.children=[]; this.dataset={}; this.style={}; this.attrs={};
        this.classes=new Set();
        this.classList={contains:n=>this.classes.has(n),add:n=>this.classes.add(n),
            toggle:(n,on)=>on?this.classes.add(n):this.classes.delete(n)};
    }
    setAttribute(k,v) { this.attrs[k]=v; }
    getBoundingClientRect() { return this.rect; }
    append(e) { this.children.push(e); }
    get childElementCount() { return this.children.length; }
    remove() {}
    querySelector(selector) {
        const pin=selector.match(/data-pin="([^"]+)"/);
        if(pin) return pins.get(pin[1])||null;
        const card=selector.match(/data-node="([^"]+)"/);
        if(card) return cards.get(card[1])||null;
        return null;
    }
    querySelectorAll(selector) {
        const ups=selector.match(/data-ups="([^"]+)"/);
        return ups ? upsLamps.filter(lamp=>lamp.dataset.ups===ups[1]) : [];
    }
}
const upsLamps=[];
const root=new Element(0,0,1000,1000);
root.clientWidth=1000; root.scrollHeight=1000;
const container=new Element(0,100,1000,900);
const frame=new Element(0,200,500,700);
const block=new Element(0,0,1000,1000);
const pins=new Map(),cards=new Map();
function addPin(id,name,x,y) {
    const pin=new Element(x,y,10,10),card=new Element(x-10,y-10,100,30);
    pin.closest=selector=>selector==='.rack-frame'?frame:card;
    card.closest=()=>block;
    pins.set(`${id}::${name}`,pin);
    return pin;
}
for (const [id,x] of [['ap',100],['wan1',250],['wan2',400]]) {
    const card=new Element(x,100,100,40);
    card.classes.add('rack-top-item'); card.closest=()=>block;
    cards.set(id,card);
}
addPin('gw','WAN1',100,300); addPin('gw','WAN2',130,300);
addPin('gw','LAN',160,300); addPin('sw','AP',190,400); addPin('sw','LAN',220,400);
const context=vm.createContext({document:{getElementById:id=>id==='rack-room-racks'?container:null,
    querySelector:selector=>selector==='.rack-main-column'?root:null,
    createElementNS:()=>new Element()},window:{}, CSS:{escape:x=>x},performance:{now:()=>0}});
// The page loads wiring.js (wirePath, wireRoute), wire-style.js, room-wiring.js, and
// device-details.js (deviceManagement) first.
for(const file of ['wiring.js', 'wire-style.js', 'room-wiring.js', 'device-details.js', 'rack-port-lights.js', 'racks.js', 'rack-cables.js', 'rack-arrange-mode.js', 'rack-live.js']){
    vm.runInContext(fs.readFileSync(new URL(`../../src/labrat/static/js/${file}`,import.meta.url),'utf8'),context);
}
// Geometry checks read each cable's square route; its drawn path rounds the turns.
const route=p=>p.dataset.route??p.attrs.d;
const run=code=>vm.runInContext(code,context);
context.surfaceRoot=root; context.rackContainer=container; context.pinMap=pins;
run(`
const app=Object.create(RackViewApp.prototype);
app.roomId=1; app.selectedId=null;
app.portAlarmState=()=> 'up';
const node=(id,category,ports)=>({id,category,placement:{container_room_id:1},
    ports:ports.map(name=>({name,medium:'copper',speed_mbps:1000}))});
app.nodes=[node('gw','router',['WAN1','WAN2','LAN']),node('sw','switch',['AP','LAN']),
    node('ap','access_point',['eth0']),node('wan1','modem',['WAN']),node('wan2','modem',['WAN'])];
const link=(from,from_port,to,to_port,evidence,confidence='confirmed')=>({from,from_port,to,to_port,evidence,confidence});
app.links=[link('gw','WAN1','wan1','WAN','unifi-wan-upstream','inferred'),
    link('wan2','WAN','gw','WAN2','unifi-wan-upstream','inferred'),
    link('ap','eth0','sw','AP','unifi-uplink'), link('gw','LAN','sw','LAN','lldp')];
app.drawCables();
`);
const paths=()=>root.children.at(-1).children;
// The rack layer holds cables; their pulses have their own layer.
const layer=name=>container.children.filter(c=>c.attrs.class===name).at(-1);
const rackCables=()=>layer('rack-cable-layer')?.children||[];
const rackFlows=()=>layer('rack-flow-layer')?.children||[];
assert.equal(paths().length,3,'Both WAN cables and the AP cable must render');
assert.equal(paths().filter(p=>p.classes.has('is-inferred-wan')).length,2);
assert.ok(paths().every(p=>p.dataset.medium==='copper'&&!/NaN|undefined/.test(p.attrs.d)));
assert.ok(paths().some(p=>p.attrs.d.includes(' Q ')),'Equipment cables are drawn with rounded turns');
for(const p of paths()){
    const ends=d=>{const n=d.match(/-?[\d.]+/g).map(Number);return [n.slice(0,2),n.slice(-2)].map(xy=>xy.map(v=>v.toFixed(1)).join(','));};
    assert.deepEqual(ends(p.attrs.d),ends(route(p)),'Rounding keeps each cable\'s two ends under their lamps');
}
assert.equal(rackCables().length,1,'The regular rack cable survives surface rendering');
assert.equal(rackFlows().length,1,'and its live pulse, in the pulse layer');
// Every cable and pulse names its two end ports, so hovering a lamp can trace it.
for(const p of [...paths(),...rackCables(),...rackFlows()]){
    assert.ok(p.dataset.pinA&&p.dataset.pinB&&p.dataset.pinA.includes('::'),'Cable paths record both ends');
}
assert.ok(paths().some(p=>p.dataset.pinA==='ap::eth0'&&p.dataset.pinB==='sw::AP'));
// A device its source reports down draws no cables; up again, they return.
run(`app.nodes.find(n=>n.id==='ap').observations=[{status:'offline',last_seen_at:new Date().toISOString()}]; app.drawCables();`);
assert.equal(paths().length,2,"A down device's cable is not drawn");
assert.equal(rackCables().length+rackFlows().length,2,'Cables between up devices still render');
run(`delete app.nodes.find(n=>n.id==='ap').observations; app.drawCables();`);
assert.equal(paths().length,3);
// A port its source reports down drops its cable too; missing data does not.
run(`app.nodes.find(n=>n.id==='ap').ports[0].link_up=false; app.drawCables();`);
assert.equal(paths().length,2,'A cable on a port reported down is not drawn');
run(`delete app.nodes.find(n=>n.id==='ap').ports[0].link_up; app.drawCables();`);
assert.equal(paths().length,3,'A port with no reading keeps its cable');
run(`app.nodes.find(n=>n.id==='wan1').placement.container_room_id=2; app.drawSurfaceUplinks();`);
assert.equal(paths().length,2,'A cable never crosses room boundaries');
run(`app.nodes.find(n=>n.id==='wan2').placement=null; app.drawSurfaceUplinks();`);
assert.equal(paths().length,1,'An unplaced endpoint has no physical cable path');

// Two devices on one shared shelf: straight under the shelf, no side channel.
const shelfA=addPin('sa','P1',300,600), shelfB=addPin('sb','P2',360,600);
const shelf=new Element(250,590,200,30);
for (const p of [shelfA,shelfB]) {
    const card=new Element(p.rect.left-10,590,50,30);
    card.classes.add('rack-shared-device');
    card.closest=selector=>selector==='.rack-shared-shelf'?shelf:block;
    p.closest=selector=>selector==='.rack-frame'?frame:card;
}
// A switch on the rack top cabled to a rack pin, and a NUT server's USB UPS.
const topSwitch=new Element(600,100,100,40); topSwitch.classes.add('rack-top-item'); topSwitch.closest=()=>block;
cards.set('topsw',topSwitch);
const nut=new Element(700,100,80,40); nut.classes.add('rack-top-item'); nut.closest=()=>block; cards.set('nut',nut);
const ups=new Element(800,100,80,40); ups.classes.add('rack-top-item'); ups.closest=()=>block; cards.set('ups',ups);
const relayed=new Element(900,100,80,40); relayed.classes.add('rack-top-item'); relayed.closest=()=>block; cards.set('relayed',relayed);
run(`
app.nodes=[node('sa','switch',['P1']),node('sb','switch',['P2']),node('sw','switch',['AP','LAN']),
    node('topsw','switch',['Port 1']),node('nut','server',[]),
    {...node('ups','ups',[]),observations:[{ups:{attachment:'usb',server_node_id:'nut'}}]},
    {...node('relayed','ups',[]),observations:[{ups:{attachment:'relayed',server_node_id:'nut'}}]}];
app.links=[link('sa','P1','sb','P2','lldp'),link('topsw','Port 1','sw','LAN','unifi-uplink')];
app.drawCables();
`);
const rackPaths=rackCables().filter(p=>!p.attrs.class);
const shelfPath=rackPaths.find(p=>route(p).startsWith('M 305.0'));
assert.ok(shelfPath,'The shelf cable renders in the rack layer');
assert.ok(route(shelfPath).split(' ').filter(t=>/^[MVH]$/.test(t)).length===4
    && !route(shelfPath).includes('H 512'),'Shelf neighbours connect directly, not via the side channel');
assert.ok(paths().some(p=>!p.classes.has('is-usb')&&p.dataset.medium==='copper'),'A rack-top switch uplink renders');
const usb=paths().filter(p=>p.classes.has('is-usb'));
assert.equal(usb.length,1,'The NUT server draws one USB line to its UPS');
assert.ok(!/NaN|undefined/.test(usb[0].attrs.d));
// A full-depth UPS has a card on each face; its lamp is only on the rear
// one (portFacing), so the line must end there, not at the front card that
// a plain lookup finds first.
const upsRear=new Element(800,500,80,40); upsRear.classes.add('rack-top-item'); upsRear.closest=()=>block;
upsRear.dataset.node='ups';
const upsLamp=new Element(860,525,9,7); upsLamp.dataset.ups='ups';
upsLamp.closest=selector=>selector==='[data-node]'?upsRear:null;
upsLamps.push(upsLamp);
run('app.drawSurfaceUplinks();');
const usbToRear=paths().filter(p=>p.classes.has('is-usb'));
assert.equal(usbToRear.length,1);
const usbLastY=Number(route(usbToRear[0]).match(/V (-?[\d.]+)(?!.*V )/)[1]);
assert.ok(usbLastY>=upsRear.rect.top&&usbLastY<=upsRear.rect.bottom,
    'The USB line ends at the card holding its lamp (the rear face), not the front card');
upsLamps.length=0;
run('app.drawSurfaceUplinks();');
// A relayed (dummy-ups) UPS gets Discovery's "reports through" line to the
// same server, beside the USB line rather than on it, and never as a cable.
const relay=paths().filter(p=>p.classes.has('is-relay'));
assert.equal(relay.length,1,'A relayed UPS draws one reports-through line to its NUT server');
assert.ok(!relay[0].classes.has('is-usb'),'It is not drawn as USB');
assert.ok(!relay[0].dataset.medium,'It carries no cable medium');
assert.ok(!/NaN|undefined/.test(relay[0].attrs.d));
assert.notEqual(route(relay[0]).split(' V ')[0],route(usb[0]).split(' V ')[0],
    'It leaves the server beside the USB line, not on it');
console.log('Equipment AP/WAN, shelf, rack-top uplink, USB, relayed UPS, and regular rack cable checks passed');
