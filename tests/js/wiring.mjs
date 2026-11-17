/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/wiring.mjs`; no browser or network required.
 *
 * Checks the shared cable geometry (static/js/wiring.js): rounded and
 * square paths from corner points, rounding to a precision, reading a
 * square path back into points, the speed steps, and the obstacle router.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({ assert });
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/wiring.js', import.meta.url), 'utf8'), context);

const checks = String.raw`
// Rounded turns stay within half the shorter run; square keeps the corner.
assert.equal(wirePath([{x:0,y:0},{x:0,y:40},{x:40,y:40}]), 'M 0 0 L 0 28 Q 0 40 12 40 L 40 40');
assert.equal(wirePath([{x:0,y:0},{x:0,y:40},{x:40,y:40}], false), 'M 0 0 L 0 40 L 40 40');
assert.equal(wirePath([{x:0,y:0},{x:0,y:10},{x:40,y:10}], true, 12), 'M 0 0 L 0 5 Q 0 10 5 10 L 40 10');
// Duplicate and straight-through points drop out; empty input draws nothing.
assert.equal(wirePath([{x:0,y:0},{x:0,y:0},{x:0,y:20},{x:0,y:40}]), 'M 0 0 L 0 40');
assert.equal(wirePath([]), '');
// A precision rounds the points and the turn ends.
assert.equal(wirePath([{x:0.04,y:0},{x:0.04,y:10.06},{x:3.33,y:10.06}], true, 6, 1), 'M 0 0 L 0 8.5 Q 0 10.1 1.7 10.1 L 3.3 10.1');

// Square paths read back into points; anything else is refused.
assert.equal(JSON.stringify(wireSquarePoints('M 1 2 V 10 H 20 L 30 40')), JSON.stringify([{x:1,y:2},{x:1,y:10},{x:20,y:10},{x:30,y:40}]));
assert.equal(wireSquarePoints('M 0 0 C 1 1 2 2 3 3'), null);
assert.equal(wireSquarePoints('M 0 0 L 1 1 M 5 5 L 6 6'), null);

// Speed steps: faster links draw wider and pulse faster.
const widths = [0, 100, 1000, 2500, 10000].map((mbps) => wireSpeedStyle(mbps).width);
assert.equal(JSON.stringify(widths), JSON.stringify([1, 1, 1.4, 1.8, 2.2]));
assert.ok(wireSpeedStyle(10000).flow < wireSpeedStyle(1000).flow);

// Lanes count per key, independently.
const lanes = wireLaneCounter();
assert.equal(JSON.stringify([lanes('a'), lanes('a'), lanes('b'), lanes('a')]), JSON.stringify([0, 1, 0, 2]));

// The router goes round an obstacle in the way, never through it.
const box = {left: 40, right: 60, top: -20, bottom: 20};
const route = wireRoute({x: 0, y: 0}, {x: 100, y: 0}, [box]);
assert.ok(route.length > 2, 'It bends to avoid the box');
for (let i = 1; i < route.length; i++) {
    const a = route[i - 1], b = route[i];
    assert.ok(a.x === b.x || a.y === b.y, 'Runs are horizontal or vertical');
    const crosses = a.y === b.y && a.y > box.top && a.y < box.bottom && Math.max(a.x, b.x) > box.left && Math.min(a.x, b.x) < box.right;
    assert.ok(!crosses, 'No run crosses the box');
}
`;

vm.runInContext(checks, context);
console.log('wiring geometry checks passed');
