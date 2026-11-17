/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/landing-cables.mjs`; no browser or network required.
 *
 * Checks the landing page's sample cable routes (static/js/landing.js):
 * down from the upper lamp, along its run, down the channel lane, along the
 * lower run, and up into the lower lamp, with Equipment's rounded turns
 * and tidy one-decimal coordinates, and the speed steps for line width and
 * pulse rate.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({ assert });
for (const file of ['wiring.js', 'landing.js']) {
    vm.runInContext(fs.readFileSync(new URL(`../../src/labrat/static/js/${file}`, import.meta.url), 'utf8'), context);
}

const checks = String.raw`
/** Corner control points and the points joined by straight runs. */
const parse = (d) => {
    const controls = [...d.matchAll(/Q([\d.]+) ([\d.]+)/g)].map((m) => m[1] + ' ' + m[2]);
    const ends = [...d.matchAll(/[ML]([\d.]+) ([\d.]+)/g)].map((m) => ({ x: +m[1], y: +m[2] }));
    return { controls, ends };
};
// wirePath spaces its commands; compare the compact form.
const tidy = (d) => d.replace(/([MLQ]) /g, '$1').replace(/ ([MLQ])/g, '$1');
const path = tidy(landingCablePath({ x: 120, y: 24, exitY: 28.4 }, { x: 80, y: 54, exitY: 59.7 }, 300));
assert.ok(path.startsWith('M120 24') && path.endsWith('L80 54'), 'It starts and ends under the two lamps');
// The route is Equipment's: down under the device, out to the lane, down the
// channel, back under the lower device, up into its lamp. Each of the four
// turns is rounded, with the square corner as its control point.
assert.deepEqual(parse(path).controls, ['120 28.4', '300 28.4', '300 59.7', '80 59.7']);
// Between the turns, runs stay straight and axis-aligned.
const { ends } = parse(path);
const q = [...path.matchAll(/Q[\d.]+ [\d.]+ ([\d.]+) ([\d.]+)/g)].map((m) => ({ x: +m[1], y: +m[2] }));
for (let i = 1; i < ends.length; i++) {
    // Run 1 starts at the move; each later run starts where the previous turn ends.
    const a = i === 1 ? ends[0] : q[i - 2], b = ends[i];
    assert.ok(a.x === b.x || a.y === b.y, 'Runs between turns are horizontal or vertical');
}
// A turn is at most 6 px, and at most half its shorter neighbouring run
// (4.4 px down from the lamp here, so 2.2 px).
assert.ok(path.includes('L120 26.2Q120 28.4 122.2 28.4'), 'A short run limits its turn');
assert.ok(path.includes('L294 28.4Q300 28.4 300 34.4'), 'A long run turns at 6 px');
// Fractions round to one decimal so the markup stays small.
const small = tidy(landingCablePath({ x: 1.234, y: 2.25, exitY: 3.31 }, { x: 4.06, y: 5, exitY: 6.04 }, 7.55));
assert.deepEqual(parse(small).controls, ['1.2 3.3', '7.6 3.3', '7.6 6', '4.1 6']);
assert.ok(small.startsWith('M1.2 2.3') && small.endsWith('L4.1 5'));
assert.ok((small.match(/[\d.]+/g) || []).every((n) => !/\.\d\d/.test(n)), 'One decimal at most');

// Speed steps are the shared ones (wiring.js wireSpeedStyle): width and pulse cycle.
const step = (mbps) => { const { width, flow } = wireSpeedStyle(mbps); return { width, flow }; };
assert.deepEqual(JSON.parse(JSON.stringify(step(10000))), { width: 2.2, flow: 0.7 });
assert.deepEqual(JSON.parse(JSON.stringify(step(2500))), { width: 1.8, flow: 1.1 });
assert.deepEqual(JSON.parse(JSON.stringify(step(1000))), { width: 1.4, flow: 1.6 });
assert.deepEqual(JSON.parse(JSON.stringify(step(100))), { width: 1, flow: 3.2 });
`;

vm.runInContext(checks, context);
console.log('landing cable checks passed');
