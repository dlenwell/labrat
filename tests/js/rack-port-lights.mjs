/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Check which port lamps each Port lights variant draws, and the tool's cycle. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const store = new Map();
const localStorage = { getItem: (key) => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) };
const context = vm.createContext({ assert, localStorage });
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/rack-port-lights.js', import.meta.url), 'utf8'), context);
const checks = String.raw`
const range = (from, to) => Array.from({ length: to - from }, (_, i) => from + i);
// Few ports, the Diagram's unlimited call, and All: every port.
assert.deepEqual(rackPortLightSlots(8, 24, 'capped'), range(0, 8));
assert.deepEqual(rackPortLightSlots(52, Infinity, 'capped'), range(0, 52));
assert.deepEqual(rackPortLightSlots(52, 24, 'all'), range(0, 52));
// Capped: the first 24, then one marker for the rest.
assert.deepEqual(JSON.stringify(rackPortLightSlots(52, 24, 'capped')), JSON.stringify([...range(0, 24), { more: 28 }]));
// Ends: the first 20 and the last 4, with the hidden middle marked.
assert.deepEqual(JSON.stringify(rackPortLightSlots(52, 24, 'ends')), JSON.stringify([...range(0, 20), { more: 28 }, ...range(48, 52)]));
// A cabled port is never hidden, and still counts toward the limit.
const kept = rackPortLightSlots(52, 24, 'capped', new Set([30]));
assert.ok(kept.includes(30));
assert.equal(kept.filter((slot) => typeof slot === 'number').length, 24);
assert.deepEqual(JSON.stringify(kept), JSON.stringify([...range(0, 23), { more: 7 }, 30, { more: 21 }]));
// More cabled ports than the limit: all of them show.
const many = new Set(range(10, 40));
assert.equal(rackPortLightSlots(52, 24, 'ends', many).filter((slot) => typeof slot === 'number').length, 30);
// The tool cycles All -> Ends -> Capped and remembers the choice.
assert.equal(rackReadPortLightMode(), 'all');
assert.equal(rackNextPortLightMode('all'), 'ends');
assert.equal(rackNextPortLightMode('capped'), 'all');
rackSavePortLightMode('ends');
assert.equal(rackReadPortLightMode(), 'ends');
localStorage.setItem(RACK_PORT_LIGHT_MODE_KEY, 'bogus');
assert.equal(rackReadPortLightMode(), 'all');
const button = { dataset: {}, attrs: {}, setAttribute(name, value) { this.attrs[name] = value; } };
rackUpdatePortLightButton(button, 'ends');
assert.equal(button.title, 'Port lights: First and last ports. Switch to First 24 ports');
assert.equal(button.attrs['aria-label'], button.title);
assert.equal(button.dataset.mode, 'ends');
`;
vm.runInContext(checks, context);
console.log('rack-port-lights: ok');
