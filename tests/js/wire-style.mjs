/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Check shared controls and Discovery geometry, persistence and redraw invariants. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const storage = new Map();
const button = { dataset: {}, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } };
const context = vm.createContext({
    assert, button,
    localStorage: { getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v) },
    document: { getElementById: id => id === 'topology-wire-style' ? button : null }, window: {},
});
for (const file of ['wiring.js', 'wire-style.js', 'topology-layout.js', 'topology.js']) {
    vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/' + file, import.meta.url), 'utf8'), context);
}
const checks = `
assert.equal(labratReadWireStyle('labrat.topology.wireStyle', 'curved'), 'curved');
assert.equal(labratReadWireStyle('labrat.equipment.wireStyle', 'square'), 'square');
for (const kind of ['tree', 'stack', 'side', 'cross']) {
    for (const b of [{x:500,y:400}, {x:500,y:40}, {x:100,y:400}, {x:100,y:100}]) {
        const a = {x:100,y:100};
        const square = labratRoutePath(kind, a, b, -6, false);
        const curved = labratRoutePath(kind, a, b, -6, true);
        const numbers = d => d.match(/-?\\d+(?:\\.\\d+)?/g).map(Number);
        const sq = numbers(square), cu = numbers(curved);
        assert.deepEqual(cu.slice(0,2), sq.slice(0,2), kind + ' start stays on the same card edge');
        assert.deepEqual(cu.slice(-2), sq.slice(-2), kind + ' end stays on the same card edge');
        assert.ok(!/[CQ]/.test(square), kind + ' square has no curves');
        assert.ok(!/NaN|undefined/.test(curved), kind + ' handles coincident coordinates');
        if (b.x === 500 && b.y === 400) assert.ok(curved.includes(' Q '), kind + ' bends round');
    }
}
const layout = { positions: new Map([['a', {x:100,y:100}]]) };
labratGraph.layout = layout;
labratGraph.selectedId = 'a';
labratGraph.nodes.set('a', {pinned:true});
let paints = 0;
labratPaint = () => { paints++; };
labratNotice = () => {};
labratToggleWireStyle();
assert.equal(button.attrs['aria-pressed'], 'false');
assert.equal(button.dataset.style, 'square');
assert.equal(button.attrs['aria-label'], button.dataset.tooltip);
assert.equal(labratReadWireStyle('labrat.topology.wireStyle', 'curved'), 'square');
assert.equal(labratReadWireStyle('labrat.equipment.wireStyle', 'square'), 'square');
labratToggleWireStyle();
assert.equal(button.attrs['aria-pressed'], 'true');
assert.equal(button.dataset.style, 'curved');
assert.equal(paints, 2);
assert.equal(labratGraph.layout, layout);
assert.equal(labratGraph.selectedId, 'a');
assert.equal(labratGraph.nodes.get('a').pinned, true);
`;
vm.runInContext(checks, context);
storage.set('labrat.topology.wireStyle', 'invalid');
assert.equal(vm.runInContext("labratReadWireStyle('labrat.topology.wireStyle', 'curved')", context), 'curved');
vm.runInContext(`localStorage.getItem = () => { throw Error('blocked'); };
localStorage.setItem = () => { throw Error('blocked'); };
assert.equal(labratReadWireStyle('test', 'curved'), 'curved');
labratToggleWireStyle();
assert.equal(labratGraph.wireStyle, 'square');`, context);
console.log('Shared wire style and Discovery all-route, persistence, storage-failure and redraw checks passed');
