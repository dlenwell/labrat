/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/room-wiring.mjs`; no browser or network required.
 *
 * The room Diagram (room-wiring.js): its left-to-right tree columns, top
 * alignment, side ports (only wired ones), fixed lane wiring, sibling
 * reordering, zoom and pan, the patch panel and its popups, and the shared
 * router Equipment still uses.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
    constructor(tag='div') {
        this.tagName=tag; this.children=[]; this.dataset={}; this.attrs={}; this.handlers={};
        this.style={setProperty(k,v){this[k]=v;}};
        this.classes=new Set();
        this.classList={add:(...n)=>n.forEach(c=>this.classes.add(c)),remove:(...n)=>n.forEach(c=>this.classes.delete(c)),
            contains:n=>this.classes.has(n),toggle:(n,on)=>on?this.classes.add(n):this.classes.delete(n)};
        this.namespaceURI='http://www.w3.org/2000/svg';
        this.scrollLeft=0; this.scrollTop=0; this.clientWidth=1400;this.clientHeight=700;
    }
    get className(){return this._className||'';}
    set className(value){this._className=value;this.classes=new Set(String(value).split(/\s+/).filter(Boolean));}
    setAttribute(k,v){this.attrs[k]=String(v);if(k==='class')this.className=v;}
    getAttribute(k){return this.attrs[k];}
    append(...children){for(const c of children){if(c.parentElement)c.parentElement.children=c.parentElement.children.filter(x=>x!==c);c.parentElement=this;this.children.push(c);}}
    replaceChildren(...children){for(const c of this.children)c.parentElement=null;this.children=[];this.append(...children);}
    addEventListener(type,handler){this.handlers[type]=handler;}
    removeEventListener(type,handler){if(this.handlers[type]===handler)delete this.handlers[type];}
    contains(element){return this===element||this.children.some(child=>child.contains(element));}
    closest(){return null;}
    cloneNode(){const copy=new Element(this.tagName);copy.className=this.className;copy.title=this.title;
        Object.assign(copy.dataset,this.dataset);Object.assign(copy.attrs,this.attrs);
        for(const [k,v] of Object.entries(this.style))if(typeof v!=='function')copy.style[k]=v;return copy;}
    focus(){context.document.activeElement=this;this.handlers.focus?.();}
    setPointerCapture(id){this.capture=id;}
    hasPointerCapture(id){return this.capture===id;}
    releasePointerCapture(){this.capture=null;}
    remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(c=>c!==this);this.parentElement=null;}
    querySelector(){return null;}
    get isConnected(){return this===canvas||this===diagram||this===body||Boolean(this.parentElement?.isConnected);}
    getBoundingClientRect(){
        const parent=this.parentElement?.getBoundingClientRect()||{left:0,top:0};
        const left=parent.left+(parseFloat(this.style.left)||0),top=parent.top+(parseFloat(this.style.top)||0);
        const width=parseFloat(this.style.width)||20,height=parseFloat(this.style.height)||20;
        return {left,top,right:left+width,bottom:top+height,width,height};
    }
}
const canvas=new Element(), summary=new Element(), body=new Element(), diagram=new Element(), rackContainer=new Element();
diagram.append(canvas);
const wireStyleButton=new Element('button');
const zoomValue=new Element(),zoomIn=new Element('button'),zoomOut=new Element('button');
const storage=new Map();
const docEvents=new Map();
const context=vm.createContext({document:{getElementById:id=>({'room-wiring-canvas':canvas,'room-wiring-summary':summary,
    'room-wiring-diagram':diagram,'room-wiring-style':wireStyleButton,'room-wiring-zoom-value':zoomValue,
    'room-wiring-zoom-in':zoomIn,'room-wiring-zoom-out':zoomOut,'rack-room-racks':rackContainer}[id]||null),querySelector:()=>null,
    body,activeElement:null,addEventListener:(type,handler)=>docEvents.set(type,handler),
    removeEventListener:(type,handler)=>{if(docEvents.get(type)===handler)docEvents.delete(type);},
    createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),createTextNode:text=>Object.assign(new Element('text'),{textContent:text})},
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    setTimeout,clearTimeout,window:{innerWidth:1400,innerHeight:900},CSS:{escape:s=>s},performance:{now:()=>0}});
for(const file of ['wiring.js', 'wire-style.js','topology-layout.js','device-details.js','rack-port-lights.js', 'racks.js', 'rack-cables.js', 'rack-arrange-mode.js', 'rack-live.js','room-wiring.js']){
    vm.runInContext(fs.readFileSync(new URL(`../../src/labrat/static/js/${file}`,import.meta.url),'utf8'),context);
}
const run=code=>vm.runInContext(code,context);
storage.set('labrat.diagram.wireStyle','square');
run(`
var node=(id,category='switch',room=1,ports=1)=>({id,name:id,category,placement:room===null?null:
    {container_room_id:room,container_name:'Example shelf',container_kind:'shelf'},observations:[],
    ports:Array.from({length:ports},(_,i)=>({name:'Port '+i,link_up:true,medium:'copper',speed_mbps:1000}))});
var link=(from,to,fromPort='Port 0',toPort='Port 0')=>({from,to,from_port:fromPort,to_port:toPort,confidence:'confirmed',evidence:'unifi-uplink'});
var app=Object.create(RackViewApp.prototype);
app.roomId=1; app.racks=[]; app.selectedId=null;
app.locationsData={rooms:[{id:1,name:'Current room'},{id:2,name:'Other room'}]};
app.nodes=[node('wan','modem'),node('root','router',1,4),node('a','switch',1,6),node('b','switch',1,2),
    node('deep','server',1,2),node('manual','other_physical',1,0),node('outside','switch',2),node('far','switch',2),node('unplaced','switch',null)];
app.links=[link('wan','root'),link('root','a','Port 1'),link('root','b','Port 2'),
    link('a','deep','Port 1'),link('root','deep','Port 3','Port 1'),
    link('root','outside'),link('a','far','Port 2'),link('a','unplaced','Port 3')];
app.wirelessLinks=[];
app.details={networkInterfacesFor:()=>[],bondForPort:()=>null};
app.statusLamp=()=>document.createElement('span');
app.portIsUp=port=>port.link_up===true;
app.portAlarmState=port=>port.link_up===true?'up':'off';
app.portStatus=()=> 'up'; app.cableAt=()=>null;
app.select=id=>{app.selectedId=id;};
var tree=new RoomWiring();
app.roomWiring=tree;
app.queueCables=()=>{app.redraws=(app.redraws||0)+1;tree.draw();};
var asApp=o=>Object.assign(Object.create(RackViewApp.prototype),o);
`);

// Columns: WAN in 0, routers in 1, switches by hops, hosts right of their deepest neighbor.
const col=id=>run(`RoomWiring.model(app).column.get(${JSON.stringify(id)})`);
assert.deepEqual(Array.from(run('RoomWiring.model(app).nodes'),n=>n.id),['wan','root','a','b','deep','manual']);
assert.equal(col('wan'),0,'WAN modems are column 0');
assert.equal(col('room-patch'),undefined,'The patch panel is its own row, not in a column');
assert.equal(col('root'),1,'Routers are column 1');
assert.equal(col('a'),2); assert.equal(col('b'),2);
assert.equal(col('deep'),3,'A host sits right of its deepest neighbor (a), not its router');
assert.equal(col('manual'),4,'A device with no neighbor goes in a last column');
assert.equal(run(`RoomWiring.model(app).parent.get('deep')`),'a');

run('tree.render(app); tree.draw();');
assert.equal(summary.textContent,'6 devices · 5 area cables · 2 external');
assert.equal(run(`tree.data.exits.some(e=>e.remote.id==='unplaced')`),false,
    'A cable to an unplaced device gets no socket or wire (owner)');
const pos=id=>run(`tree.positions.get(${JSON.stringify(id)})`);
// Every column starts at one top line (owner), the patch panel's row north of them all.
const firstY=pos('root').y;
assert.equal(pos('wan').y,firstY,'The WAN starts level with the following columns');
assert.equal(pos('b').y,firstY);
assert.ok(pos('room-patch').y+pos('room-patch').height<firstY,'The patch panel is north of everything');
assert.ok(pos('a').y>pos('b').y,'Among siblings, fewer wires and children come first (b has none, a has deep)');
assert.equal(pos('deep').y,pos('a').y,'A device lines up with the one it hangs from (owner)');
assert.ok(pos('root').x>pos('wan').x && pos('a').x>pos('root').x && pos('deep').x>pos('a').x,'Left to right');

// Only wired ports show, on the side facing their wire (owner).
const ports=id=>Array.from(run(`tree.cards.get(${JSON.stringify(id)})?.children||[]`)).filter(c=>c.classes.has('room-wiring-port'));
assert.equal(ports('a').length,3,'a: its uplink, deep, and the cable leaving the room; unwired ports are hidden');
assert.equal(ports('a').filter(p=>p.dataset.side==='left').length,2,
    'The uplink and the cable to the patch panel (column 0) enter on the west side');
assert.equal(ports('root').filter(p=>p.dataset.side==='left').length,2,'WAN and the patch panel arrive from the west');
assert.equal(ports('root').filter(p=>p.dataset.side==='right').length,3,'a, b, and deep leave to the east');
assert.equal(ports('manual').length,0,'A device with nothing wired shows no ports');

// Fixed lane paths: out sideways, along a channel lane, into the facing side.
const paths=()=>Array.from(run('tree.svg.children')).filter(p=>p.dataset.from);
const numbers=d=>d.replace(/[MLQC]/g,' ').trim().split(/\s+/).map(Number);
assert.equal(paths().length,7,'Five room cables and two leaving the room');
for(const p of paths()){
    const n=numbers(p.attrs.d);
    for(let i=0;i+3<n.length;i+=2) assert.ok(n[i]===n[i+2]||n[i+1]===n[i+3],'Every segment is orthogonal');
}
const pathOf=(from,to)=>paths().find(p=>p.dataset.from===from&&p.dataset.to===to);
const ra=numbers(pathOf('root','a').attrs.d), rb=numbers(pathOf('root','b').attrs.d);
assert.equal(ra[0],pos('root').x+pos('root').width,'Leaves the router from its east side');
assert.equal(ra.at(-2),pos('a').x,'Enters the switch on its west side');
assert.notEqual(ra[2],rb[2],'Two wires in one channel take their own lanes');
// A wire that skips a column runs along the top.
const skip=numbers(pathOf('root','deep').attrs.d);
assert.ok(Math.min(...skip.filter((_,i)=>i%2))<firstY,'The router-to-server run goes over the top');
assert.equal(skip.at(-2),pos('deep').x);

// Lane order picks the arrangement with the fewest crossings.
context.nested=[{id:'outer',left:[0],right:[400]},{id:'inner',left:[100],right:[300]}];
const crossings=order=>{const [l,r]=order.map(id=>context.nested.find(i=>i.id===id));
    const span=i=>[Math.min(...i.left,...i.right),Math.max(...i.left,...i.right)],inside=(y,[t,b])=>y>t&&y<b;
    return r.left.filter(y=>inside(y,span(l))).length+l.right.filter(y=>inside(y,span(r))).length;};
assert.equal(crossings(Array.from(run('roomWiringLaneOrder(nested).map(i=>i.id)'))),
    Math.min(crossings(['outer','inner']),crossings(['inner','outer'])));

// Background pan changes only the viewport.
canvas.scrollLeft=150;canvas.scrollTop=250;
canvas.handlers.pointerdown({button:0,pointerType:'mouse',pointerId:10,clientX:300,clientY:300,target:{closest:()=>null}});
canvas.handlers.pointermove({pointerId:10,clientX:220,clientY:180,preventDefault(){}});
assert.equal(canvas.scrollLeft,230);assert.equal(canvas.scrollTop,370);
assert.equal(run('app.isEditing()'),true,'Source redraws wait during background pans');
canvas.handlers.pointercancel({pointerId:10});
assert.equal(run('tree.panning'),false);
for(const ignored of [{pointerType:'touch',target:{closest:()=>null}},
    {pointerType:'mouse',target:{closest:()=>({tagName:'button'})}}]){
    const before=canvas.scrollLeft;
    canvas.handlers.pointerdown({button:0,pointerId:11,clientX:300,clientY:300,...ignored});
    canvas.handlers.pointermove({pointerId:11,clientX:100,clientY:100,preventDefault(){}});
    assert.equal(canvas.scrollLeft,before,'Native touch and card interactions do not start background pans');
}
assert.equal(canvas.handlers.scroll,undefined,'Scrolling and panning never redraw wires');

// Zoom keeps the anchor point, never redraws wires, and has usable limits.
run('tree.zoomScale=1;tree.fit();');canvas.scrollLeft=1000;canvas.scrollTop=750;
assert.deepEqual({...run('tree.slack')},{x:700,y:350},'Half a canvas of pan room on every side');
run('tree.svg.children[0].marker=true;');
run('tree.zoom(1.25);');
assert.equal(run('tree.scale'),1.25);
assert.equal(canvas.scrollLeft,(1000+700-700)*1.25+700-700);
assert.equal(run('tree.svg.children[0].marker'),true,'Zooming scales the wires with the stage; it never redraws them');
assert.equal(zoomValue.textContent,'125%');
run('tree.zoom(100);');assert.equal(run('tree.scale'),2);assert.equal(zoomIn.disabled,true);
run('tree.zoom(0.001);');assert.equal(run('tree.scale'),0.1);assert.equal(zoomOut.disabled,true);
run('tree.zoomToFit();');
assert.ok(run('tree.width*tree.scale <= tree.canvas.clientWidth'));
assert.equal(canvas.scrollLeft,700,'Fit scrolls to the drawing, past the pan room');
run('tree.zoomScale=1;tree.fit();');canvas.scrollLeft=700;canvas.scrollTop=350;
let wheelPrevented=false;
canvas.handlers.wheel({deltaY:-100,deltaMode:0,clientX:200,clientY:100,preventDefault(){wheelPrevented=true;}});
assert.equal(wheelPrevented,true,'The wheel zooms instead of scrolling the page');
assert.ok(run('tree.scale')>1,'Wheel up zooms in');
assert.ok(Math.abs((canvas.scrollLeft+200-700)/run('tree.scale')-200)<1e-6,'The point under the pointer stays put');
run('tree.zoomScale=undefined;tree.fit();');

// Cards never move by hand (owner): no drag or arrow-key handlers, a click opens details.
assert.equal(run(`tree.cards.get('a').handlers.pointerdown`),undefined,'Cards cannot be dragged');
assert.equal(run(`tree.cards.get('a').handlers.keydown`),undefined,'or moved with arrow keys');
assert.equal(run(`tree.patchCard.handlers.pointerdown`),undefined,'Neither can the patch panel');
run(`tree.cards.get('a').handlers.click();`);
assert.equal(run('app.selectedId'),'a','Cards open the shared Equipment selection flow');
run('app.selectedId=null;tree.render(app);tree.draw();');

// The patch panel: one socket per live external cable, facing east, with destination popups.
assert.equal(run('tree.exitSockets.size'),2);
assert.equal(run('tree.data.panel.rows'),1,'A row of sockets facing south');
const patchWire=numbers(pathOf('root','outside')?.attrs.d||pathOf('outside','root')?.attrs.d||'');
assert.ok(patchWire.length>=10,'A patch cable drops into the band, runs along it, and down to its device');
assert.ok(patchWire[1]<firstY && patchWire.at(-1)>=firstY);
assert.equal(run('tree.patchPanel.parentElement===tree.patchCard && tree.patchCard.parentElement===tree.stage'),true);
run(`var destination=[...tree.exitSockets.values()].find(r=>r.exit.remote.id==='outside');`);
assert.equal(run(`destination.details.children.at(-1).href`),'/areas/2?inspect=outside');
assert.equal(run(`destination.socket.children.some(c=>Boolean(c.textContent))`),false,'Destination labels stay out of the panel');
assert.equal(body.children.length,0,'Destination details start hidden');
run(`destination.socket.handlers.pointerenter({pointerType:'mouse'});`);
assert.equal(body.children.length,1,'Hover opens an interactive destination popup');
assert.equal(run(`tree.cards.get('root').classList.contains('is-patch-highlighted')`),true,
    'A hovered patch socket highlights its local device');
assert.equal(run(`tree.highlightedPort?.dataset.pin`),'root::Port 0','and its port');
assert.equal(run(`tree.svg.children.filter(p=>p.classList.contains('is-patch-highlighted')).length`),1,
    'Highlight exactly the hovered cable');
run(`destination.socket.handlers.pointerleave();destination.details.handlers.pointerenter();`);
await new Promise(resolve=>setTimeout(resolve,210));
assert.equal(body.children.length,1,'The popup stays open while the pointer moves to its room link');
run(`destination.details.handlers.pointerleave();`);
await new Promise(resolve=>setTimeout(resolve,210));
assert.equal(body.children.length,0);
run('destination.socket.focus();');
assert.equal(body.children.length,1,'Keyboard focus exposes the same destination and link');
run(`destination.socket.handlers.keydown({key:'Tab',preventDefault(){}});`);
assert.equal(context.document.activeElement,run('destination.follow'),'Tab reaches the portaled popup link directly');
docEvents.get('keydown')({key:'Escape',preventDefault(){},stopPropagation(){}});
assert.equal(body.children.length,0);
assert.equal(docEvents.size,0,'Closing removes temporary document listeners');
context.document.activeElement=null;

// Patch cables stack by the columns' order: nearest column first, lowest card first
// within a column, so none crosses another (owner).
run(`app.links.push(link('b','outside','Port 1','Port 0'));tree.render(app);tree.draw();`);
const startX=(from,to)=>numbers(pathOf(from,to).attrs.d)[0];
assert.ok(startX('root','outside')<startX('a','far'),'Column 1 before column 2');
assert.ok(pos('a').y>pos('b').y);
assert.ok(startX('a','far')<startX('b','outside'),'In one column the lower card\'s socket comes first');
const segments=Array.from(['root|outside','a|far','b|outside'],k=>{const [f,t]=k.split('|');const n=numbers(pathOf(f,t).attrs.d);
    const out=[];for(let i=0;i+3<n.length;i+=2)out.push([n[i],n[i+1],n[i+2],n[i+3]]);return out;});
const cross=(s,t)=>{const h=s[1]===s[3]?s:t,v=s[1]===s[3]?t:s;if(h===v||(h[1]!==h[3])||(v[0]!==v[2]))return false;
    return v[0]>Math.min(h[0],h[2])+0.5&&v[0]<Math.max(h[0],h[2])-0.5&&h[1]>Math.min(v[1],v[3])+0.5&&h[1]<Math.max(v[1],v[3])-0.5;};
let patchCrossings=0;for(let i=0;i<3;i++)for(let j=i+1;j<3;j++)for(const p of segments[i])for(const q of segments[j])if(cross(p,q))patchCrossings++;
assert.equal(patchCrossings,0,"Patch cables never cross each other");
run(`app.links.pop();tree.render(app);tree.draw();`);

// Selecting a device lights its own wires and its path up to the gateway (owner).
run(`app.selectedId='deep';tree.draw();`);
const lit=(from,to)=>!pathOf(from,to).classes.has('is-dim');
assert.equal(lit('a','deep'),true,'Its parent link');
assert.equal(lit('root','deep'),true,'Its other links');
assert.equal(lit('root','a'),true,'Its parent\'s link up to the gateway');
assert.equal(lit('root','b'),false,'Unrelated wires dim');
run(`app.selectedId='a';tree.draw();`);
assert.equal(lit('a','deep'),true,'Selecting a parent lights its children\'s links');
assert.equal(lit('root','deep'),false,'but not their other routes');
// Every route toward the gateway lights, not just one (owner).
run(`app.links.push(link('b','deep','Port 1','Port 0'));app.selectedId='deep';tree.render(app);tree.draw();`);
assert.equal(lit('b','deep'),true,'A second uplink');
assert.equal(lit('root','b'),true,'and its switch\'s route to the gateway');
run(`app.links.pop();tree.render(app);`);
run(`app.selectedId=null;tree.draw();`);

// Wire style, activity, and Equipment's down rules.
const flows=()=>Array.from(run('tree.svg.children')).filter(p=>p.attrs.class==='rack-cable-flow');
const squarePaths=paths().map(p=>p.attrs.d);
assert.ok(squarePaths.every(d=>!d.includes(' C ')),'Square mode keeps 90° bends');
run('tree.toggleWireStyle();');
assert.equal(wireStyleButton.attrs['aria-pressed'],'true');
assert.ok(paths().filter(p=>!p.classes.has('is-usb')).every(p=>!p.attrs.d.includes(' L ')),
    'Curved mode swoops: no straight 90° corners at all');
const swoop=numbers(pathOf('root','a').attrs.d);
assert.equal(swoop[0],pos('root').x+pos('root').width,'A swoop still starts at its port');
assert.equal(swoop.at(-2),pos('a').x,'and ends at the far port');
assert.ok(flows().every(flow=>paths().some(path=>path.attrs.d===flow.attrs.d)),'Pulses follow the same paths');
run('tree.toggleWireStyle();');
assert.deepEqual(paths().map(p=>p.attrs.d),squarePaths,'Toggling back restores the same paths');
storage.delete('labrat.diagram.wireStyle');
assert.equal(run('tree.readWireStyle()'),'curved','Swoopy curves are the default (owner)');
storage.set('labrat.diagram.wireStyle','square');
assert.equal(flows().length,7,'Up ports carry activity pulses');
run(`app.nodes.find(n=>n.id==='b').ports[0].link_up=false;tree.render(app);tree.draw();`);
assert.equal(pathOf('root','b'),undefined,'A cable whose port is reported down is not drawn');
assert.equal(ports('b').length,0,'and its port is not shown');
run(`app.nodes.find(n=>n.id==='b').ports[0].link_up=true;
app.nodes.find(n=>n.id==='root').observations=[{id:1,status:'offline',last_seen_at:new Date().toISOString()}];
tree.render(app);tree.draw();`);
assert.equal(paths().filter(p=>p.dataset.from==='root'||p.dataset.to==='root').length,0,'Cables to an offline device are not drawn');
assert.equal(run('tree.data.exits.filter(e=>e.local.id==="root").length'),0,'and its external cables get no socket');
run(`app.nodes.find(n=>n.id==='root').observations=[];app.links[1].confidence='ambiguous';tree.render(app);tree.draw();`);
assert.ok(pathOf('root','a').classes.has('is-tentative'),'Tentative cables are marked');

// USB and relayed UPS lines, and Wi-Fi, each with a dot instead of a port.
run(`app.nodes=[node('server','server'),{...node('ups','ups',1,0),observations:[{ups:{attachment:'usb',server_node_id:'server'}}]},
    {...node('relay','ups',1,0),observations:[{ups:{attachment:'relayed',server_node_id:'server'}}]}];
app.links=[];app.wirelessLinks=[{from:'outside',to:'server'}];tree.render(app);tree.draw();`);
assert.deepEqual(Array.from(run('RoomWiring.model(app).usb'),l=>l.to+':'+l.kind),['ups:usb','relay:relay']);
assert.deepEqual(paths().map(p=>[...p.classes].find(c=>c==='is-usb'||c==='is-relay')).sort(),['is-relay','is-usb'],
    'Both power relations draw in their own styles');
assert.equal(col('ups'),col('server'),'A UPS sits in its server\'s column (owner)');
assert.equal(col('relay'),col('server'));
assert.ok(pos('ups').y>pos('server').y && pos('relay').y>pos('server').y,'right below it');
assert.equal(pos('ups').x,pos('server').x);
const usbLine=numbers(paths().find(p=>p.classes.has('is-usb')).attrs.d);
assert.equal(usbLine[0],pos('server').x+pos('server').width,'A UPS line leaves the server\'s east side');
assert.equal(usbLine.at(-2),pos('ups').x+pos('ups').width,'and curves back into the UPS\'s east side (owner)');
assert.ok(Math.max(...usbLine.filter((_,i)=>i%2===0))>pos('server').x+pos('server').width,'through the channel beside them');
assert.equal(ports('server').filter(p=>p.dataset.side==='right').length,2);
assert.equal(ports('ups').filter(p=>p.dataset.side==='right').length,1);
assert.equal(ports('ups')[0].classes.has('room-wiring-dot'),true,'A line with no port ends on a dot');
assert.equal(flows().length,0,'Neither power relation carries activity pulses');
assert.equal(run('tree.patchCard'),null,'No external cable, no patch panel');
assert.equal(run('RoomWiring.model(app).wireless.length'),0,'Wireless links cannot introduce other rooms');
run(`app.nodes=[];tree.render(app);tree.draw();`);
assert.equal(canvas.children.length,0,'Empty rooms do not retain old devices or wire layers');
run(`app.root={dataset:{roomView:'wiring'}};app.racks=[{id:1}];
app.roomWiring={draw:()=>{app.treeDraws=(app.treeDraws||0)+1;}};
app.drawSurfaceUplinks=()=>{throw new Error('Surface overlay must not render over a room diagram');};
app.drawCables();`);
assert.equal(run('app.treeDraws'),1,'Explicit wiring draws room cables even when racks exist');
run(`app.root.dataset.roomView='racks';app.racks=[];`);
assert.equal(run('app.wiringView'),false,'Explicit Equipment remains the rack view without racks');

// Islands without a router start at column 1; edge-facing exits lead the patch panel.
run(`var island=RoomWiring.model(asApp({roomId:1,wirelessLinks:[],nodes:[node('sw','switch'),node('host','server')],
    links:[link('sw','host')]}));`);
assert.equal(run(`island.column.get('sw')`),1); assert.equal(run(`island.column.get('host')`),2);
run(`var orderedApp={roomId:1,wirelessLinks:[],nodes:[
    node('local','switch'),node('Z core','switch',2),node('A downstream','switch',2),
    node('edge','router',2),node('wan','modem',null),node('server shortcut','server',2)],
    links:[link('local','A downstream'),link('Z core','local'),link('edge','Z core'),
        link('wan','edge'),link('edge','server shortcut'),link('server shortcut','A downstream')]};`);
assert.equal(run(`RoomWiring.model(asApp(orderedApp)).exits[0].remote.id`),'Z core',
    'An edge-facing multi-hop cable comes before alphabetically earlier downstream exits');
run(`orderedApp.nodes=orderedApp.nodes.filter(n=>n.id!=='wan'&&n.id!=='edge');`);
assert.equal(run(`RoomWiring.model(asApp(orderedApp)).exits[0].remote.id`),'A downstream',
    'Without edge evidence keep the stable neutral order');

// The shared router (Equipment's surface cables): one bend in the open, never through a card.
const turns=points=>points.slice(2).filter((p,i)=>(points[i].x===points[i+1].x)!==(points[i+1].x===p.x)).length;
assert.equal(turns(run(`wireRoute({x:0,y:0},{x:300,y:200},[{left:100,right:120,top:300,bottom:320}])`)),1);
context.boxes=[{left:100,right:300,top:100,bottom:200}];
const route=run(`wireRoute({x:0,y:150},{x:400,y:150},boxes)`);
assert.ok(route.length>2,'A blocked straight run needs bends');
for(let i=1;i<route.length;i++){
    const a=route[i-1],b=route[i];
    assert.ok(a.x===b.x||a.y===b.y,'Every segment is orthogonal');
    const intersects=a.x===b.x
        ? a.x>100&&a.x<300&&Math.max(a.y,b.y)>100&&Math.min(a.y,b.y)<200
        : a.y>100&&a.y<200&&Math.max(a.x,b.x)>100&&Math.min(a.x,b.x)<300;
    assert.equal(intersects,false,'A cable cannot pass through an unrelated faceplate');
}
assert.equal(run(`wirePath([{x:0,y:0},{x:0,y:0},{x:0,y:20},{x:0,y:40},{x:40,y:40}],true)`),'M 0 0 L 0 28 Q 0 40 12 40 L 40 40');
assert.equal(run('wirePath([],true)'),'');
console.log('Room diagram columns, side ports, lane wiring, reordering, zoom and pan, and the patch panel passed');
