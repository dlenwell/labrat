/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/equipment-room-cables.mjs`; no browser or network required.
 *
 * Checks Equipment cables between "Around the room" cards (racks.js
 * drawSurfaceUplinks with wiring.js wireRoute): a cable leaves
 * its own port lamp, never crosses another card, keeps to the channel on
 * the right like rack cables, and two cables never share a run.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

/** A measured stand-in for a DOM element. */
class Element {
    constructor(x = 0, y = 0, width = 10, height = 10, classes = []) {
        this.rect = { left: x, top: y, right: x + width, bottom: y + height, width, height };
        this.children = []; this.dataset = {}; this.style = {}; this.attrs = {}; this.lamps = new Map();
        this.classes = new Set(classes);
        this.classList = { contains: (n) => this.classes.has(n), add: (n) => this.classes.add(n),
            toggle: (n, on) => (on ? this.classes.add(n) : this.classes.delete(n)) };
    }
    setAttribute(k, v) { this.attrs[k] = v; }
    getBoundingClientRect() { return this.rect; }
    append(e) { this.children.push(e); }
    get childElementCount() { return this.children.length; }
    remove() {}
    closest() { return null; }
    matches(selector) { return selector.split(',').some((s) => this.classes.has(s.trim().replace(/^\./, ''))); }
    querySelector(selector) {
        const pin = selector.match(/data-pin="([^"]+)"/);
        return pin ? this.lamps.get(pin[1]) || null : null;
    }
}

const cards = new Map();
const root = new Element(0, 0, 1000, 1000);
root.clientWidth = 1000; root.scrollHeight = 1000;
// Only room cards here: no rack, so no rack-face pins.
root.querySelector = (selector) => {
    const node = selector.match(/\.rack-room-item\[data-node="([^"]+)"\]/);
    return node ? cards.get(node[1]) || null : null;
};
root.querySelectorAll = () => [...cards.values()];

/** A room card with one port lamp at its bottom edge, 16 px apart like racks.css. */
function roomCard(id, x, y, port) {
    const card = new Element(x, y, 150, 50, ['rack-room-item']);
    const lamp = new Element(x + 40, y + 40, 10, 8);
    card.lamps.set(`${id}::${port}`, lamp);
    cards.set(id, card);
    return card;
}
const top = roomCard('a', 0, 100, 'p1');
const middle = roomCard('b', 0, 166, 'p1');
const bottom = roomCard('c', 0, 232, 'p1');

const context = vm.createContext({
    document: { getElementById: () => null, querySelector: (s) => (s === '.rack-main-column' ? root : null),
        createElementNS: () => new Element() },
    window: {}, CSS: { escape: (x) => x }, performance: { now: () => 0 },
});
for (const file of ['wiring.js', 'wire-style.js', 'room-wiring.js', 'device-details.js', 'rack-port-lights.js', 'racks.js', 'rack-cables.js', 'rack-arrange-mode.js', 'rack-live.js']) {
    vm.runInContext(fs.readFileSync(new URL(`../../src/labrat/static/js/${file}`, import.meta.url), 'utf8'), context);
}
vm.runInContext(`
const app = Object.create(RackViewApp.prototype);
app.roomId = 1; app.selectedId = null;
const node = (id) => ({ id, category: 'switch', placement: { container_room_id: 1 },
    ports: [{ name: 'p1', medium: 'copper', speed_mbps: 1000 }] });
app.nodes = [node('a'), node('b'), node('c')];
app.links = [{ from: 'a', from_port: 'p1', to: 'c', to_port: 'p1', evidence: 'lldp', confidence: 'confirmed' }];
app.drawSurfaceUplinks();
globalThis.app = app;
`, context);

/** Parse an SVG path of M/L/H/V commands into its corner points. */
function corners(d) {
    const tokens = d.match(/[MLHV]|-?[\d.]+/g);
    const points = [];
    let command = 'M', at = { x: 0, y: 0 };
    for (let i = 0; i < tokens.length;) {
        if (/[MLHV]/.test(tokens[i])) { command = tokens[i++]; continue; }
        if (command === 'H') at = { x: +tokens[i++], y: at.y };
        else if (command === 'V') at = { x: at.x, y: +tokens[i++] };
        else at = { x: +tokens[i++], y: +tokens[i++] };
        points.push(at);
    }
    return points;
}
/** Whether a straight run passes through a card's inside. */
function crosses(a, b, card) {
    const r = card.rect;
    for (let k = 1; k < 40; k++) {
        const x = a.x + (b.x - a.x) * k / 40, y = a.y + (b.y - a.y) * k / 40;
        if (x > r.left + 1 && x < r.right - 1 && y > r.top + 1 && y < r.bottom - 1) return true;
    }
    return false;
}
const paths = () => root.children.at(-1).children;

let points = corners(paths()[0].dataset.route);
assert.deepEqual(points[0], { x: 45, y: 150 }, 'A cable leaves under its own port lamp');
assert.deepEqual(points.at(-1), { x: 45, y: 282 }, 'and ends under the other card\'s lamp');
for (let i = 1; i < points.length; i++) {
    assert.ok(points[i].x === points[i - 1].x || points[i].y === points[i - 1].y, 'Square bends only');
    assert.ok(!crosses(points[i - 1], points[i], middle), 'It never crosses the card in between');
}
assert.ok(Math.max(...points.map((p) => p.x)) > middle.rect.right,
    'It runs down the channel on the right, like rack cables');
assert.ok(paths()[0].attrs.d.includes(' Q '), 'Its turns are drawn rounded');
assert.ok(!paths()[0].dataset.route.includes(' Q '), 'while its square route stays on data-route');

// A second cable between the same cards gets its own lane.
vm.runInContext(`
app.links.push({ from: 'a', from_port: 'p1', to: 'c', to_port: 'p1', evidence: 'lldp', confidence: 'confirmed' });
app.drawSurfaceUplinks();
`, context);
const lanes = [...paths()].map((path) => Math.max(...corners(path.dataset.route).map((p) => p.x)));
assert.equal(lanes.length, 2);
assert.notEqual(lanes[0], lanes[1], 'Two cables never share a vertical run');
for (const path of paths()) {
    const pts = corners(path.dataset.route);
    for (let i = 1; i < pts.length; i++) assert.ok(!crosses(pts[i - 1], pts[i], middle));
}

// Every staggered exit stays at least a pixel clear of the router's 4 px
// margin round the card below (16 px gap): an exit exactly on that margin
// is blocked or not by sub-pixel rounding, and the cable vanished in the
// owner's browser. Four cables from one card use every drop step.
vm.runInContext(`
app.links.push(
    { from: 'a', from_port: 'p1', to: 'c', to_port: 'p1', evidence: 'lldp', confidence: 'confirmed' },
    { from: 'a', from_port: 'p1', to: 'c', to_port: 'p1', evidence: 'lldp', confidence: 'confirmed' },
);
app.drawSurfaceUplinks();
`, context);
assert.equal(paths().length, 4, 'Four cables from one card are all drawn');
const gapBelow = middle.rect.top - top.rect.bottom;
for (const path of paths()) {
    const drop = corners(path.dataset.route)[1].y - top.rect.bottom;
    assert.ok(drop > 0 && drop <= gapBelow - 4 - 1, `A ${drop} px exit drop stays clear of the next card's margin`);
}

console.log('Equipment room-card cable checks passed');
