/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/remove-dialog.mjs`; no browser or network required.
 *
 * Checks the delete confirmation's wording (static/js/remove-dialog.js):
 * what goes with a delete, the unplace offer, and blocked deletes.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({ assert });
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/remove-dialog.js', import.meta.url), 'utf8'), context);

const checks = String.raw`
const base = { kind: "room", name: "Office", rooms: 0, racks: 0, places: 0, unplaces: 0, blocked: null };

// Empty: a plain Delete, no unplace.
let words = removalWording(base);
assert.equal(words.title, "Delete room “Office”?");
assert.equal(words.confirm, "Delete");
assert.equal(words.unplace, false);
assert.deepEqual(words.lines, ["Nothing is placed inside.", "This cannot be undone."]);

// Equipment inside: offer to unplace it, and say it is kept.
words = removalWording({ ...base, racks: 2, places: 1, unplaces: 5 });
assert.equal(words.confirm, "Delete and unplace 5");
assert.equal(words.unplace, true);
assert.equal(words.lines[0], "This also deletes 2 racks and 1 shelf.");
assert.match(words.lines[1], /^5 pieces of equipment inside will be unplaced\. They stay in inventory/);

// One of each, three kinds listed with commas.
words = removalWording({ ...base, kind: "location", rooms: 1, racks: 1, places: 3, unplaces: 1 });
assert.equal(words.lines[0], "This also deletes 1 area, 1 rack and 3 shelves.");
assert.match(words.lines[1], /^1 piece of equipment inside will be unplaced\. It stays/);

// Blocked: the reason only, and no confirm button.
words = removalWording({ ...base, unplaces: 3, blocked: "This room has saved attributes; it cannot be deleted here" });
assert.equal(words.confirm, null);
assert.deepEqual(words.lines, ["This room has saved attributes; it cannot be deleted here"]);
`;

vm.runInContext(checks, context);
console.log('remove-dialog checks passed');
