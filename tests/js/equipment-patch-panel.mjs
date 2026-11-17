/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/equipment-patch-panel.mjs`; no browser or network required.
 *
 * Checks Equipment's copy of the room patch panel (racks.js renderPatchBox
 * and drawPatchCables with room-wiring.js RoomPatchPanel): one socket per
 * cable leaving the room, on the first rack's top (leftmost) when the room
 * has racks, in the first side panel otherwise, never in the Diagram, and
 * a routed cable from the socket to its local port that crosses no card.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

/** A measured stand-in for a DOM element with just enough tree behavior. */
class Element {
    constructor(tag = 'div', rect = null) {
        this.tagName = tag; this.children = []; this.parentElement = null;
        this.dataset = {}; this.attrs = {}; this.handlers = {}; this.hidden = false; this.textContent = '';
        this.style = { setProperty: (k, v) => { this.style[k] = v; } };
        this.classes = new Set();
        this.classList = { contains: (n) => this.classes.has(n), add: (...n) => n.forEach((x) => this.classes.add(x)),
            remove: (...n) => n.forEach((x) => this.classes.delete(x)),
            toggle: (n, on) => (on ? this.classes.add(n) : this.classes.delete(n)) };
        this.rect = rect;
        this.namespaceURI = 'http://www.w3.org/2000/svg';
    }
    set className(value) { this.classes = new Set(String(value).split(/\s+/).filter(Boolean)); }
    get className() { return [...this.classes].join(' '); }
    setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'class') this.className = v; }
    getAttribute(k) { return this.attrs[k]; }
    addEventListener(type, handler) { this.handlers[type] = handler; }
    append(...children) { for (const c of children) { c.remove?.(); c.parentElement = this; this.children.push(c); } }
    prepend(child) { child.remove?.(); child.parentElement = this; this.children.unshift(child); }
    replaceChildren(...children) { for (const c of this.children) c.parentElement = null; this.children = []; this.append(...children); }
    remove() {
        if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((c) => c !== this);
        this.parentElement = null;
    }
    get isConnected() { return this === page || Boolean(this.parentElement?.isConnected); }
    get childElementCount() { return this.children.length; }
    contains(element) { return this === element || this.children.some((c) => c.contains(element)); }
    /** Simple selectors only: `#id`, `tag.class`, descendants, `[data-pin]`, and `.class[data-node]`, comma-separated. */
    matches(selector) {
        return selector.split(',').some((part) => {
            const parts = part.trim().split(/\s+/);
            const last = parts.at(-1);
            // A descendant selector needs a matching ancestor too.
            if (parts.length > 1 && !this.parentElement?.closest(parts.slice(0, -1).join(' '))) return false;
            const pin = last.match(/data-pin="([^"]+)"/);
            if (pin) return this.dataset.pin === pin[1];
            const node = last.match(/^\.([\w-]+)\[data-node="([^"]+)"\]$/);
            if (node) return this.classes.has(node[1]) && this.dataset.node === node[2];
            if (last.startsWith('#')) return this.id === last.slice(1);
            const [tag, ...classes] = last.split('.');
            return (!tag || tag === this.tagName) && classes.every((c) => this.classes.has(c.replace(/:.*$/, '')));
        });
    }
    closest(selector) { for (let e = this; e; e = e.parentElement) if (e.matches(selector)) return e; return null; }
    querySelectorAll(selector) {
        const found = [];
        const walk = (e) => { for (const c of e.children) { if (c.matches(selector)) found.push(c); walk(c); } };
        walk(this);
        return found;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    getClientRects() { return [this.getBoundingClientRect()]; }
    getBoundingClientRect() {
        if (this.rect) return { ...this.rect, width: this.rect.right - this.rect.left, height: this.rect.bottom - this.rect.top };
        // Sockets sit 22 px apart in rows inside their panel; lamps inside sockets.
        if (this.classes.has('room-patch-socket')) {
            const face = this.parentElement.getBoundingClientRect();
            const i = this.parentElement.children.indexOf(this), columns = Number(this.parentElement.style['--patch-columns']);
            const left = face.left + 8 + (i % columns) * 22, top = face.top + 8 + Math.floor(i / columns) * 22;
            return { left, top, right: left + 20, bottom: top + 20, width: 20, height: 20 };
        }
        if (this.classes.has('room-patch-lamp')) {
            const s = this.parentElement.getBoundingClientRect();
            return { left: s.left + 5, top: s.top + 3, right: s.left + 15, bottom: s.top + 17, width: 10, height: 14 };
        }
        if (this.classes.has('room-patch-panel')) {
            const holder = this.parentElement.getBoundingClientRect();
            return { left: holder.left, top: holder.top, right: holder.left + 192, bottom: holder.top + 38, width: 192, height: 38 };
        }
        return this.parentElement?.getBoundingClientRect() || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
    }
}

const page = new Element('body');
const byId = new Map();
/** Make an element with classes, optional ID, and optional fixed rectangle. */
function el(classes, { id, rect, parent } = {}) {
    const e = new Element('div', rect ? { left: rect[0], top: rect[1], right: rect[0] + rect[2], bottom: rect[1] + rect[3] } : null);
    e.className = classes;
    if (id) { e.id = id; byId.set(id, e); }
    (parent || page).append(e);
    return e;
}
const column = el('rack-main-column', { rect: [0, 0, 1400, 1200] });
column.clientWidth = 1400; column.scrollHeight = 1200;
const racks = el('rack-room-racks', { id: 'rack-room-racks', parent: column, rect: [0, 0, 1000, 1200] });
const side = el('rack-side-column', { parent: column, rect: [1040, 0, 360, 1200] });
const box = el('rack-patch-box', { id: 'rack-patch', parent: side, rect: [1040, 0, 360, 120] });
el('rack-patch-face', { id: 'rack-patch-face', parent: box, rect: [1056, 40, 300, 60] });
const floor = el('rack-room-group', { parent: side, rect: [1040, 300, 360, 400] });

/** A room card with one port lamp, like "Around the room". */
function roomCard(id, x, y) {
    const card = el('rack-room-item', { parent: floor, rect: [x, y, 150, 50] });
    card.dataset.node = id;
    const lamp = el('rack-port-light', { parent: card, rect: [x + 40, y + 40, 10, 8] });
    lamp.dataset.pin = `${id}::p1`;
    return card;
}
const server = roomCard('server', 1060, 400);
const blocker = roomCard('blocker', 1060, 200);

const docEvents = new Map();
const context = vm.createContext({
    document: {
        getElementById: (id) => byId.get(id) || null,
        querySelector: (s) => (s === '.rack-main-column' ? column : page.querySelector(s)),
        querySelectorAll: (s) => page.querySelectorAll(s),
        createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag),
        addEventListener: (t, h) => docEvents.set(t, h), removeEventListener: (t) => docEvents.delete(t),
        body: page, activeElement: null,
    },
    window: { innerWidth: 1400, innerHeight: 900 }, Node: { DOCUMENT_POSITION_FOLLOWING: 4 },
    CSS: { escape: (x) => x }, performance: { now: () => 0 }, setTimeout, clearTimeout,
});
for (const file of ['wiring.js', 'wire-style.js', 'topology-layout.js', 'room-wiring.js', 'device-details.js', 'rack-port-lights.js', 'racks.js', 'rack-cables.js', 'rack-arrange-mode.js', 'rack-live.js']) {
    vm.runInContext(fs.readFileSync(new URL(`../../src/labrat/static/js/${file}`, import.meta.url), 'utf8'), context);
}
const run = (code) => vm.runInContext(code, context);
run(`
const app = Object.create(RackViewApp.prototype);
const node = (id, room) => ({ id, name: id, category: 'switch', placement: room ? { container_room_id: room } : null,
    ports: [{ name: 'p1', medium: 'copper', speed_mbps: 1000, link_up: true }] });
app.roomId = 1; app.selectedId = null; app.racks = [];
app.root = { dataset: { roomView: 'racks' } };
app.locationsData = { rooms: [{ id: 1, name: 'Here' }, { id: 2, name: 'Closet' }] };
app.nodes = [node('server', 1), node('blocker', 1), node('core', 2), node('loose', null)];
app.links = [
    { from: 'core', from_port: 'p1', to: 'server', to_port: 'p1', evidence: 'lldp', confidence: 'confirmed' },
    { from: 'server', from_port: 'p1', to: 'loose', to_port: 'p1', evidence: 'lldp', confidence: 'ambiguous' },
    { from: 'server', from_port: 'p1', to: 'blocker', to_port: 'p1', evidence: 'lldp', confidence: 'confirmed' },
];
app.portIsUp = (port) => port.link_up === true;
app.portAlarmState = () => 'up';
app.queueCables = () => {};
app.select = (id) => { app.selectedId = id; };
globalThis.app = app;
`);

// Only cables with exactly one end in this room become sockets.
assert.deepEqual(Array.from(run('roomPatchExits(app)'), (exit) => exit.remote.id).sort(), ['core', 'loose']);

// No racks: the patch panel is the first side panel, sized to its cables
// (at most eight a row, spread evenly) with no unused positions.
run('app.renderPatchBox(false);');
assert.equal(box.hidden, false, 'A room without racks shows the patch box');
assert.equal(run('app.patchBox.sockets.size'), 2, 'One socket per cable leaving the room');
assert.equal(run('app.patchBox.face.style["--patch-columns"]'), 2);
assert.equal(run('app.patchBox.face.children.length'), 2, 'No unused positions');
// A down cable gets no socket or popup at all, not an empty one.
run(`app.nodes.find((n) => n.id === 'core').observations = [{ status: 'offline', last_seen_at: new Date().toISOString() }];
app.renderPatchBox(false);`);
assert.equal(run('app.patchBox.sockets.size'), 1, 'A cable to a down device has no socket');
assert.equal(run('app.patchBox.face.children.length'), 1);
run(`delete app.nodes.find((n) => n.id === 'core').observations; app.renderPatchBox(false);`);
assert.equal(run('app.patchBox.sockets.size'), 2);
assert.equal(run('app.patchBox.face.parentElement.id'), 'rack-patch-face');

// Its cable leaves straight down under the panel and reaches the local lamp
// without crossing the card in between.
run('app.drawSurfaceUplinks();');
const layer = column.children.find((child) => child.classes.has('rack-surface-uplink-layer'));
const patchPaths = layer.children.filter((path) => path.dataset.patch);
assert.equal(patchPaths.length, 2, 'Each socket draws its own cable');
/** Parse an SVG path of M/L/H/V commands into its corner points. */
function corners(d) {
    const tokens = d.match(/[MLHV]|-?[\d.]+/g);
    const points = []; let command = 'M', at = { x: 0, y: 0 };
    for (let i = 0; i < tokens.length;) {
        if (/[MLHV]/.test(tokens[i])) { command = tokens[i++]; continue; }
        if (command === 'H') at = { x: +tokens[i++], y: at.y };
        else if (command === 'V') at = { x: at.x, y: +tokens[i++] };
        else at = { x: +tokens[i++], y: +tokens[i++] };
        points.push(at);
    }
    return points;
}
const crosses = (a, b, card) => {
    const r = card.getBoundingClientRect();
    for (let k = 1; k < 40; k++) {
        const x = a.x + (b.x - a.x) * k / 40, y = a.y + (b.y - a.y) * k / 40;
        if (x > r.left + 1 && x < r.right - 1 && y > r.top + 1 && y < r.bottom - 1) return true;
    }
    return false;
};
for (const path of patchPaths) {
    const points = corners(path.dataset.route);
    assert.equal(points[1].x, points[0].x, 'A side-box cable first drops straight down');
    assert.ok(points[1].y > run('app.patchBox.face.getBoundingClientRect().bottom'), 'out under the panel');
    assert.deepEqual(points.at(-1), { x: 1105, y: 450 }, 'and ends under the local port lamp');
    for (let i = 1; i < points.length; i++) {
        assert.ok(points[i].x === points[i - 1].x || points[i].y === points[i - 1].y, 'Square bends only');
        assert.ok(!crosses(points[i - 1], points[i], blocker), 'It never crosses another card');
    }
}
assert.ok(patchPaths.some((path) => path.classes.has('is-tentative')), 'Ambiguous cables stay tentative');

// Hovering a socket highlights exactly its cable and port; the device card
// is not outlined, since the outline crossed the bright cable.
run('[...app.patchBox.sockets.values()][0].socket.handlers.pointerenter({ pointerType: "mouse" });');
run('app.markPatchHighlight();');
assert.equal(server.classes.has('is-patch-highlighted'), false, 'The device card gets no outline');
assert.equal(server.children[0].classes.has('is-patch-port'), true);
assert.equal(layer.children.filter((p) => p.classes.has('is-patch-highlighted')).length, 1);
assert.ok(layer.children.filter((p) => !p.classes.has('is-patch-highlighted')).every((p) => p.classes.has('is-dim')));
assert.equal(run('app.isEditing()'), true, 'Live updates wait while a destination is open');
run('app.patchBox.close(); app.markPatchHighlight();');
assert.equal(server.children[0].classes.has('is-patch-port'), false);

// With a rack: it rests leftmost on the first rack's top, and the box hides.
const block = el('rack-block', { parent: racks, rect: [0, 0, 1000, 900] });
const top = el('rack-top-display', { parent: block, rect: [20, 60, 900, 60] });
const items = el('rack-top-items', { parent: top, rect: [20, 60, 900, 40] });
el('rack-top-item', { parent: items, rect: [300, 60, 150, 40] });
el('rack-top-plank', { parent: top, rect: [20, 100, 900, 16] });
el('rack-frame', { parent: block, rect: [20, 140, 900, 700] });
run('app.racks = [{ id: 1 }]; app.renderPatchBox(false);');
assert.equal(box.hidden, true, 'A room with racks hides the side box');
assert.equal(items.children[0].classes.has('rack-top-patch'), true, 'The panel is leftmost on the rack top');
assert.equal(items.children[0].children[0], run('app.patchBox.face'));
assert.equal(items.querySelectorAll('.rack-top-item').length, 1, 'It is not a rack-top item that arrange mode moves');
run('app.drawSurfaceUplinks();');
const rackLayer = column.children.find((child) => child.classes.has('rack-surface-uplink-layer'));
for (const path of rackLayer.children.filter((p) => p.dataset.patch)) {
    const points = corners(path.dataset.route);
    assert.ok(points[1].y > 116, 'A rack-top cable drops over the front of the plank');
    assert.ok(points[2].x > 920, 'then runs out to the channel right of the rack');
}

// Cables to devices on the panel's own rack leave the rack's left side:
// lanes in depth order (shallowest nearest the face), no lane crossings,
// and two cables to one device meeting it in one tight bundle.
const frame = block.children.find((child) => child.classes.has('rack-frame'));
/** A rack device on the front face with port lamps at its bottom edge. */
function rackDevice(id, y, ports) {
    const device = el('rack-device', { parent: frame, rect: [40, y, 860, 40] });
    device.dataset.node = id;
    ports.forEach((port, index) => {
        const lamp = el('rack-port-light', { parent: device, rect: [400 + index * 20, y + 28, 10, 8] });
        lamp.dataset.pin = `${id}::${port}`;
    });
}
rackDevice('shallow', 200, ['p1', 'p2']);
rackDevice('deep', 500, ['p1']);
run(`
app.nodes.push(node('shallow', 1), node('deep', 1));
app.links.push(
    { from: 'core', from_port: 'a', to: 'deep', to_port: 'p1', evidence: 'lldp', confidence: 'confirmed' },
    { from: 'core', from_port: 'b', to: 'shallow', to_port: 'p1', evidence: 'lldp', confidence: 'confirmed' },
    { from: 'core', from_port: 'c', to: 'shallow', to_port: 'p2', evidence: 'lldp', confidence: 'confirmed' },
);
app.renderPatchBox(false); app.drawSurfaceUplinks();
`);
const sideLayer = column.children.find((child) => child.classes.has('rack-surface-uplink-layer'));
const sidePath = (port) => corners(sideLayer.children.find((p) => p.dataset.patch?.includes(port)).dataset.route);
const runs = ['deep::p1', 'shallow::p1', 'shallow::p2'].map(sidePath);
for (const points of runs) {
    assert.equal(points.length, 6, 'A fixed run: drop, along the plank, down the lane, in, up');
    assert.ok(points[1].y > 116 && points[1].y < 140, 'It runs along under the plank, above the frame');
    assert.ok(points[2].x < 20, 'and leaves the rack on its left side');
    assert.equal(points[3].x, points[2].x);
}
assert.ok(sideLayer.children.filter((p) => p.dataset.patch).every((p) => p.attrs.d.includes(' Q ')),
    'Patch cables are drawn with rounded turns');
const [deep, first, second] = runs;
assert.ok(deep[2].x < first[2].x && deep[2].x < second[2].x, 'The deepest device takes the outermost lane');
assert.ok(deep[1].y < first[1].y && deep[1].y < second[1].y, 'and the highest run, so no run crosses a lane');
assert.notEqual(first[2].x, second[2].x, 'Two cables to one device get their own lanes');
assert.equal(first[3].y, second[3].y, 'and they meet the device at one height, a tight bundle');
// No run under a device crosses a lane nearer the face that goes deeper.
for (const a of runs) {
    for (const b of runs) {
        if (b[2].x <= a[2].x) continue;
        assert.ok(!(b[3].y > a[3].y && b[1].y < a[3].y), 'No lane crosses a run under a device');
    }
}

// The Diagram has its own wall panel: neither copy shows in Equipment.
run('app.renderPatchBox(true);');
assert.equal(box.hidden, true);
assert.equal(run('app.patchBox.sockets.size'), 0);

console.log('Equipment patch panel checks passed');
