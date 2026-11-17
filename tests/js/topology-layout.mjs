/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Run with `node tests/js/topology-layout.mjs`; no browser or network required. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({});
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/topology-layout.js', import.meta.url), 'utf8'), context);
const layout = (nodes, links, wireless = [], power = []) => {
    context.input = { nodes, links, wireless, power };
    return vm.runInContext('labratLayeredLayout(input.nodes, input.links, input.wireless, input.power)', context);
};
const node = (id, category, name = id) => ({ id, category, name });
const cable = (from, to, confidence = 'confirmed') => ({ from, to, confidence });

const nodes = [
    node('gw', 'router'), node('agg', 'switch'), node('edge', 'switch'), node('mgmt', 'switch'),
    node('s1', 'server'), node('s2', 'server'), node('ap', 'access_point'),
    node('nut', 'server'), node('ups', 'ups'),
];
const links = [
    cable('gw', 'agg'), cable('agg', 'edge'), cable('agg', 'mgmt'),
    // s1 is bonded to edge (two cables) and has a management link to mgmt.
    cable('edge', 's1'), cable('edge', 's1'), cable('mgmt', 's1', 'inferred'),
    cable('edge', 's2'), cable('edge', 'ap'),
];
const result = layout(nodes, links, [{ from: 'nut', to: 'ap' }]);

// The router is the root; switches hang by hop; a server never parents a switch.
assert.deepEqual([...result.roots], ['gw']);
assert.equal(result.parent.get('agg'), 'gw');
assert.equal(result.parent.get('edge'), 'agg');
assert.equal(result.parent.get('mgmt'), 'agg');
// Two confirmed cables beat one inferred management link.
assert.equal(result.parent.get('s1'), 'edge');
// The Wi-Fi client hangs under its AP, so the AP becomes a branch.
assert.equal(result.parent.get('nut'), 'ap');
assert.ok(result.stacked.has('s1') && result.stacked.has('s2') && result.stacked.has('nut'));
assert.ok(!result.stacked.has('ap'));
// mgmt draws nothing under it (s1 hangs under edge) but carries s1's second
// cable, so it keeps a row slot instead of stacking like an end device.
assert.ok(!result.stacked.has('mgmt'));
assert.equal(result.positions.get('mgmt').y, result.positions.get('edge').y);
// Unconnected equipment goes to the shelf, not into a tree.
assert.deepEqual([...result.orphans], ['ups']);
assert.ok(result.orphanTop > result.positions.get('nut').y);

// Tiers go down the page and no two cards overlap.
const at = (id) => result.positions.get(id);
assert.ok(at('gw').y < at('agg').y && at('agg').y < at('edge').y && at('edge').y < at('s1').y);
const cards = [...result.positions.values()];
for (let i = 0; i < cards.length; i += 1) {
    for (let j = i + 1; j < cards.length; j += 1) {
        const apart = Math.abs(cards[i].x - cards[j].x) >= 148 || Math.abs(cards[i].y - cards[j].y) >= 50;
        assert.ok(apart, `cards ${i} and ${j} overlap`);
    }
}
// Bonded cables are one drawn pair carrying both links.
const bonded = [...result.pairs.values()].find((pair) => pair.key === 'edge|s1');
assert.equal(bonded.links.length, 2);

// USB and relayed UPS units both hang under their NUT server, on separate kinds.
const powered = layout([...nodes, node('relay', 'ups'), node('loose', 'ups')], links,
    [{ from: 'nut', to: 'ap' }],
    [{ from: 'nut', to: 'ups', kind: 'usb' }, { from: 'nut', to: 'relay', kind: 'relay' }]);
assert.equal(powered.parent.get('ups'), 'nut');
assert.equal(powered.parent.get('relay'), 'nut');
assert.ok(powered.stacked.has('ups') && powered.stacked.has('relay') && !powered.stacked.has('nut'));
assert.deepEqual([...powered.orphans], ['loose']);
const pairOf = (key) => [...powered.pairs.values()].find((pair) => pair.key === key);
assert.equal(pairOf('nut|ups').usb.length, 1);
assert.equal(pairOf('nut|relay').relay.length, 1);
assert.equal(pairOf('nut|relay').usb.length, 0);

// Same input, same picture.
const again = layout(nodes, links, [{ from: 'nut', to: 'ap' }]);
assert.deepEqual([...again.positions.entries()], [...result.positions.entries()]);

// Without a router, the best-connected switch starts the tree.
const island = layout([node('a', 'switch'), node('b', 'switch'), node('c', 'switch'), node('h', 'server')],
    [cable('a', 'b'), cable('b', 'c'), cable('c', 'h')]);
assert.deepEqual([...island.roots], ['b']);
assert.equal(island.parent.get('h'), 'c');

// A side route enters the stacked card's right edge; a stack route its left.
context.route = (kind) => vm.runInContext(`labratRoutePath('${kind}', {x: 100, y: 50}, {x: 300, y: 300})`, context);
assert.match(context.route('side'), /H374$/);
assert.match(context.route('stack'), /H226$/);

// Path tracing walks to the root.
context.parentMap = result.parent;
assert.deepEqual([...vm.runInContext("labratPathToRoot(parentMap, 's1')", context)], ['s1', 'edge', 'agg', 'gw']);
console.log('topology layout checks passed');

// Both WAN modems stay above their shared gateway, regardless of link direction.
const wanNodes = [node('gw', 'router'), node('wan1', 'modem'), node('wan2', 'modem'), node('sw', 'switch')];
for (const reverse of [false, true]) {
    const wanLinks = ['wan1', 'wan2'].map(id => cable(reverse ? 'gw' : id, reverse ? id : 'gw', 'inferred'));
    const wanLayout = layout(wanNodes, [...wanLinks, cable('gw', 'sw')]);
    const gateway = wanLayout.positions.get('gw');
    assert.equal(wanLayout.positions.get('wan1').y, wanLayout.positions.get('wan2').y);
    assert.ok(wanLayout.positions.get('wan1').y < gateway.y);
    assert.ok(wanLayout.positions.get('sw').y > gateway.y);
    assert.notEqual(wanLayout.positions.get('wan1').x, wanLayout.positions.get('wan2').x);
}
