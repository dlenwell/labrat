/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/device-placement.mjs`; no browser or network required.
 *
 * Checks the shared placement editor (static/js/device-placement.js): the
 * rack model, free-span rules (faces, full depth, shared shelves, moving
 * devices), U suggestions, face options, and the API payload for each
 * placement mode. Equipment's manual form uses the same rack functions. The
 * checks are the `checks` string, so they can also run in a browser console.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({ assert });
for (const file of ['device-details.js', 'device-placement.js']) {
    vm.runInContext(fs.readFileSync(new URL(`../../src/labrat/static/js/${file}`, import.meta.url), 'utf8'), context);
}

const checks = String.raw`
const placement = Object.create(DevicePlacement.prototype);
// A 10U rack with a rear: device 1 at U1-2 front, device 2 at U5 full depth,
// device 3 on a front shared shelf at U8, and an empty rear shelf at U9.
const data = {
    racks: [{ id: 7, room_id: 1, name: "Main rack", height_u: 10, has_rear: true },
            { id: 8, room_id: 1, name: "Front only", height_u: 4, has_rear: false }],
    containers: [
        { id: 70, kind: "rack", rack_id: 7, room_id: 1, name: "Main rack" },
        { id: 71, kind: "rack_top", parent_id: 70, room_id: 1, name: "On top" },
        { id: 72, kind: "rack_shelf", parent_id: 70, rack_u: 8, rack_face: "front", room_id: 1, name: "U8 shelf" },
        { id: 73, kind: "rack_shelf", parent_id: 70, rack_u: 9, rack_face: "rear", room_id: 1, name: "U9 shelf" },
        { id: 80, kind: "rack", rack_id: 8, room_id: 1, name: "Front only" },
        { id: 90, kind: "floor", room_id: 1, name: "Floor" },
    ],
    devices: [
        { id: 1, height_u: 2, placement: { container_id: 70, rack_u: 1, face: "front" } },
        { id: 2, height_u: 1, placement: { container_id: 70, rack_u: 5, face: "full_depth" } },
        { id: 3, height_u: 1, placement: { container_id: 72, rack_u: 8, face: "front" } },
        { id: 4, height_u: 1, placement: null },
    ],
    sites: [], rooms: [],
};
placement.locations = () => data;

// Rack model.
const rack = placement.rackFor(70);
assert.equal(rack.containerId, 70);
assert.equal(rack.topContainerId, 71);
assert.deepEqual(rack.rackShelves.map((shelf) => shelf.id), [72, 73]);
assert.equal(placement.rackFor(90), null, "A floor is not a rack");
assert.equal(placement.rackFor(80).topContainerId, null, "No rack top recorded");

// Free spans.
assert.equal(placement.spanIsFree(rack, "front", 1, 1), false, "Device 1 holds U1 front");
assert.equal(placement.spanIsFree(rack, "front", 1, 2), false, "and U2, its second unit");
assert.equal(placement.spanIsFree(rack, "rear", 1, 1), true, "The rear of U1 is free");
assert.equal(placement.spanIsFree(rack, "rear", 1, 5), false, "Full depth blocks the rear too");
assert.equal(placement.spanIsFree(rack, "full_depth", 1, 2), false, "Full depth needs both faces");
assert.equal(placement.spanIsFree(rack, "front", 1, 8), false, "An occupied shared shelf blocks its U");
assert.equal(placement.spanIsFree(rack, "rear", 1, 9), true, "An empty shared shelf does not");
assert.equal(placement.spanIsFree(rack, "front", 2, 1, 1), true, "A device does not block its own move");
assert.equal(placement.spanIsFree(rack, "front", 2, 10), false, "Spans stay inside the rack");
assert.equal(placement.spanIsFree(rack, "front", 1, 0), false);

// Suggestions: lowest free span, keeping a still-valid preference.
assert.equal(placement.firstAvailableUnit(rack, "front", 1), 3);
assert.equal(placement.firstAvailableUnit(rack, "front", 1, null, 6), 6, "A valid entry is kept");
assert.equal(placement.firstAvailableUnit(rack, "front", 1, null, 5), 3, "An invalid entry is replaced");
assert.equal(placement.firstAvailableUnit(rack, "front", 2, 1), 1, "Moving device 1 can keep U1");
assert.equal(placement.firstAvailableUnit(rack, "front", 11), null, "Nothing taller than the rack fits");
assert.equal(placement.firstAvailableUnit(null, "front", 1), null);

// Face options: back and full depth only on racks with a rear.
const option = (value) => ({ value, hidden: false, disabled: false });
const faceSelect = { value: "full_depth", options: [option("front"), option("rear"), option("full_depth")],
    querySelector(selector) { return this.options.find((item) => selector.includes('"' + item.value + '"')); } };
placement.updateFaceOptions(faceSelect, placement.rackFor(80));
assert.equal(faceSelect.value, "front", "A front-only rack resets the face");
assert.equal(faceSelect.options[1].hidden && faceSelect.options[2].disabled, true);
placement.updateFaceOptions(faceSelect, rack);
assert.equal(faceSelect.options[1].hidden || faceSelect.options[2].disabled, false);

// Payloads for each mode (the form fields are stand-ins).
const fields = { "device-port-facing": { value: "rear" }, "device-placement-mode": { value: "rack" },
    "device-rack-u": { value: "3" }, "device-face": { value: "front" }, "device-height": { value: "2" } };
placement.element = (id) => fields[id];
placement.node = { device_id: 4, placement: null };
placement.selection = { roomId: 1, containerId: null };
assert.equal(placement.payload(), null, "Nothing chosen, nothing to save");
placement.selection.containerId = 70;
assert.deepEqual({ ...placement.payload() }, { port_facing: "rear", container_id: 70, rack_u: 3, face: "front",
    placement_mode: "rack", height_u: 2 });
fields["device-placement-mode"].value = "rack_shelf";
assert.deepEqual({ ...placement.payload() }, { port_facing: "rear", container_id: 70, rack_u: 3, face: "front",
    placement_mode: "rack_shelf", position: { x: 1 } }, "Joining a shelf goes to its right end");
placement.node = { device_id: 3, placement: { container_kind: "rack_shelf", position: { x: 0.25 } } };
assert.deepEqual({ ...placement.payload().position }, { x: 0.25 }, "Staying on a shelf keeps the position");
fields["device-placement-mode"].value = "on_top";
assert.deepEqual({ ...placement.payload() }, { port_facing: "rear", container_id: 71, position: { x: 0.5, y: 0.5 } },
    "On top uses the rack-top container");
placement.selection.containerId = 90;
fields["device-port-facing"].value = "";
assert.deepEqual({ ...placement.payload() }, { port_facing: null, container_id: 90 }, "Other places need only the container");
"device placement checks passed";
`;
console.log(vm.runInContext(checks, context));
