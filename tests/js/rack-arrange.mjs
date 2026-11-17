/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/rack-arrange.mjs`; no browser or network required.
 *
 * Checks the Equipment arrange-mode draft (static/js/rack-arrange.js): free
 * moves, same-height swaps, refused overlaps, faces, shared shelves, moves
 * between racks, keyboard nudges, and the API payload. The checks are the
 * `checks` string, so they can also run in a browser console.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({ assert });
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/rack-arrange.js', import.meta.url), 'utf8'), context);

const checks = String.raw`
// Rack 70 (10U, rear): A at U1-2 front, B at U5-6 front, C at U8 full depth,
// R at U5 rear, and an occupied front shelf at U10. Rack 80 (4U) is front only.
const build = () => new RackArrangement({
    racks: [
        { containerId: 70, name: "Main", height_u: 10, has_rear: true,
          shelves: [{ rack_u: 10, rack_face: "front", occupied: true }] },
        { containerId: 80, name: "Small", height_u: 4, has_rear: false, shelves: [] },
    ],
    devices: [
        { id: 1, name: "A", height: 2, face: "front", containerId: 70, rack_u: 1 },
        { id: 2, name: "B", height: 2, face: "front", containerId: 70, rack_u: 5 },
        { id: 3, name: "C", height: 1, face: "full_depth", containerId: 70, rack_u: 8 },
        { id: 4, name: "R", height: 1, face: "rear", containerId: 70, rack_u: 5 },
    ],
});

// Free move, and moving back to the start leaves no move to save.
let draft = build();
let plan = draft.plan(1, 70, 3);
assert.equal(plan.kind, "move");
assert.ok(draft.apply(plan));
assert.deepEqual(draft.position(1), { containerId: 70, rack_u: 3 });
assert.equal(draft.moves().length, 1);
assert.deepEqual(JSON.parse(JSON.stringify(draft.moves()[0])), {
    device_id: 1, from: { container_id: 70, rack_u: 1 }, to: { container_id: 70, rack_u: 3 },
});
draft.apply(draft.plan(1, 70, 1));
assert.equal(draft.moves().length, 0);
assert.equal(draft.plan(1, 70, 1).kind, "none");

// Dropping onto a same-height device swaps, snapping to its unit even when
// the drop overlaps it only partly.
draft = build();
plan = draft.plan(1, 70, 4);
assert.equal(plan.kind, "swap");
assert.equal(plan.partner.name, "B");
assert.equal(plan.unit, 5);
draft.apply(plan);
assert.deepEqual(draft.position(1), { containerId: 70, rack_u: 5 });
assert.deepEqual(draft.position(2), { containerId: 70, rack_u: 1 });
assert.equal(draft.moves().length, 2);

// Different heights, several devices, shelves, and bounds are refused.
draft = build();
assert.match(draft.plan(1, 70, 7).reason, /C is a different height/);
assert.equal(draft.plan(1, 70, 9).kind, "refused");          // shelf at U10
assert.match(draft.plan(1, 70, 9).reason, /shared shelf/);
assert.match(draft.plan(1, 70, 0).reason, /does not fit/);
assert.match(draft.plan(3, 70, 6).reason, /B is a different height/); // full depth hits B
assert.equal(draft.plan(3, 70, 4).kind, "move");             // U4 is free on both faces
assert.match(draft.plan(3, 70, 5).reason, /Several devices/);
assert.equal(draft.plan(99, 70, 1).kind, "refused");
assert.equal(draft.plan(1, 999, 1).kind, "refused");

// Faces: front and rear devices share units; rear cannot go to a front-only rack.
assert.equal(draft.plan(4, 70, 1).kind, "move");             // behind A
assert.match(draft.plan(4, 80, 1).reason, /Small has no rear/);

// Between racks: free move, and a swap whose partner cannot fit back is refused.
plan = draft.plan(1, 80, 3);
assert.equal(plan.kind, "move");
draft.apply(plan);
assert.deepEqual(JSON.parse(JSON.stringify(draft.moves())), [{
    device_id: 1, from: { container_id: 70, rack_u: 1 }, to: { container_id: 80, rack_u: 3 },
}]);
draft = new RackArrangement({
    racks: [
        { containerId: 70, name: "Main", height_u: 10, has_rear: true, shelves: [] },
        { containerId: 80, name: "Small", height_u: 4, has_rear: false, shelves: [] },
    ],
    devices: [
        { id: 1, name: "Front", height: 1, face: "front", containerId: 80, rack_u: 2 },
        { id: 2, name: "Deep", height: 1, face: "full_depth", containerId: 70, rack_u: 4 },
    ],
});
assert.match(draft.plan(1, 70, 4).reason, /Cannot swap: Small has no rear/);

// Keyboard nudges: step into free space, swap with a same-height neighbour,
// jump past a different-height one, and stop at the rack's edge.
draft = build();
plan = draft.nudge(1, 1);
assert.equal(plan.kind, "move");
assert.equal(plan.unit, 2);
draft.apply(plan);
draft.apply(draft.nudge(1, 1));                              // U3
plan = draft.nudge(1, 1);                                    // U4-5 meets B: swap
assert.equal(plan.kind, "swap");
draft = new RackArrangement({
    racks: [{ containerId: 70, name: "Main", height_u: 6, has_rear: false, shelves: [] }],
    devices: [
        { id: 1, name: "Patch", height: 1, face: "front", containerId: 70, rack_u: 1 },
        { id: 2, name: "Server", height: 2, face: "front", containerId: 70, rack_u: 2 },
        { id: 3, name: "Tall", height: 2, face: "front", containerId: 70, rack_u: 5 },
    ],
});
plan = draft.nudge(1, 1);                                    // Server is 2U: jump to U4
assert.equal(plan.kind, "move");
assert.equal(plan.unit, 4);
draft.apply(plan);
assert.equal(draft.nudge(1, 1).kind, "refused");             // past Tall is off the rack
assert.equal(draft.nudge(3, 1).kind, "refused");             // already at the top
draft.apply(draft.nudge(1, -1));                             // jump back below Server
assert.deepEqual(draft.position(1), { containerId: 70, rack_u: 1 });
assert.equal(draft.nudge(1, -1).kind, "refused");            // already at U1

// Rack tops: shared ordering (saved x, then name) and even spacing.
assert.deepEqual(rackTopOrder([
    { name: "b" }, { name: "a", placement: { position: { x: 0.9 } } }, { name: "c", placement: { position: { x: 0.1 } } },
]).map((item) => item.name), ["c", "b", "a"]);
assert.equal(rackTopX(0, 3), 0.25);
assert.equal(rackTopX(1, 2), 0.666667);

// Rack-top reordering: drop between items, nudge, ends, and other racks.
draft = new RackArrangement({
    racks: [{ containerId: 70, name: "Main", height_u: 10, has_rear: false, shelves: [] }],
    devices: [{ id: 1, name: "A", height: 1, face: "front", containerId: 70, rack_u: 1 }],
    tops: [
        { containerId: 71, rackName: "Main", devices: [
            { id: 10, name: "AP" }, { id: 11, name: "NUT" }, { id: 12, name: "Modem" }] },
        { containerId: 81, rackName: "Other", devices: [{ id: 20, name: "Lone" }] },
    ],
});
assert.deepEqual(draft.changes(), { moved: 0, tops: 0, any: false });
plan = draft.reorderTop(12, 71, 0);
assert.equal(plan.kind, "reorder");
assert.deepEqual([...plan.order], [12, 10, 11]);
draft.apply(plan);
assert.deepEqual(draft.topOrder(71), [12, 10, 11]);
assert.deepEqual(JSON.parse(JSON.stringify(draft.topOrders())), [{ container_id: 71, device_ids: [12, 10, 11] }]);
assert.deepEqual(draft.changes(), { moved: 0, tops: 1, any: true });
assert.equal(draft.reorderTop(12, 71, 0).kind, "none");      // already first
assert.equal(draft.reorderTop(12, 71, 99).index, 2);         // clamped to the end
assert.match(draft.reorderTop(12, 81, 0).reason, /stays on its own rack/);
assert.match(draft.reorderTop(1, 71, 0).reason, /Only equipment on top/);
assert.match(draft.nudgeTop(12, -1).reason, /left end/);
draft.apply(draft.nudgeTop(12, 1));
assert.deepEqual(draft.topOrder(71), [10, 12, 11]);
draft.apply(draft.nudgeTop(12, -1));                         // back where it started this round
draft.apply(draft.nudgeTop(12, 1));
draft.apply(draft.nudgeTop(12, 1));
assert.deepEqual(draft.topOrder(71), [10, 11, 12]);          // the original order again
assert.equal(draft.topOrders().length, 0);
assert.match(draft.nudgeTop(20, 1).reason, /right end/);
// Rack moves and rack-top orders are counted together.
draft.apply(draft.plan(1, 70, 4));
draft.apply(draft.reorderTop(11, 71, 0));
assert.deepEqual(draft.changes(), { moved: 1, tops: 1, any: true });
`;

vm.runInContext(checks, context);
console.log('rack arrange checks passed');
