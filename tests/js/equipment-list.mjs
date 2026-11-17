/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/equipment-list.mjs`; no browser or network required.
 *
 * Checks the shared Add equipment list's filtering (equipment-list.js
 * EquipmentList.shown): devices already in the room, VMs and containers
 * (they run on their host and cannot be placed), the Infrastructure /
 * Clients / Unplaced-only toggles, and the search.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const elements = {
    'equipment-search': { value: '', addEventListener() {} },
    'equipment-manual-open': { addEventListener() {} },
};
const toggles = { infrastructure: true, clients: true, unplaced: false };
const doc = {
    getElementById: (id) => elements[id] || null,
    querySelectorAll: () => [],
    querySelector: (selector) => {
        const key = selector.match(/data-equipment-filter="([^"]+)"/)?.[1];
        return key ? { checked: toggles[key] } : null;
    },
};
const context = vm.createContext({ window: {}, document: doc });
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/equipment-list.js', import.meta.url), 'utf8'), context);

const items = [
    { id: 'here', name: 'In this room', category: 'switch', placement: { room: 1 } },
    { id: 'switch', name: 'Spare switch', category: 'switch', placement: null },
    { id: 'placed', name: 'Placed elsewhere', category: 'server', placement: { room: 2 } },
    { id: 'laptop', name: 'Laptop', category: 'other_physical', client_only: true, placement: null },
    { id: 'guest', name: 'Guest VM', category: 'other_physical', client_only: true, placement: null,
        observations: [{ client: { virtual_endpoint: true } }] },
    { id: 'ct', name: 'Container', category: 'other_physical', client_only: true, placement: null,
        observations: [{ client: { virtualization: { kind: 'container' } } }] },
];
context.items = items;
const list = vm.runInContext(`new EquipmentList({ items: () => items, inRoom: (item) => item.placement?.room === 1,
    where: () => 'somewhere', onSelect() {}, onManual() {}, doc: document })`, context);
const ids = () => Array.from(list.shown(), (item) => item.id);

assert.deepEqual(ids(), ['laptop', 'placed', 'switch'],
    'Everything not in this room, by name; VMs and containers are never listed');
toggles.clients = false;
assert.deepEqual(ids(), ['placed', 'switch'], 'Clients off hides client-only devices');
toggles.unplaced = true;
assert.deepEqual(ids(), ['switch'], 'Unplaced only hides placed devices');
toggles.clients = true; toggles.unplaced = false;
elements['equipment-search'].value = 'lap';
assert.deepEqual(ids(), ['laptop'], 'Search matches the name');
elements['equipment-search'].value = 'guest';
assert.deepEqual(ids(), [], 'A search never brings back a guest');
console.log('Equipment list: room, guest, toggle and search filtering passed');
