/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/device-drawer.mjs`; no browser or network required.
 *
 * Checks the shared drawer shell (static/js/device-drawer.js): the panel
 * stack, Back and Close, closeAtRoot, Escape, and which clicks may close it.
 * The drawer runs against a small stand-in document. The checks themselves
 * are the `checks` string, so they can also run in a browser console.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({ assert, setTimeout, clearTimeout });
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/device-drawer.js', import.meta.url), 'utf8'), context);

const checks = String.raw`
// ---- A stand-in document: just what DeviceDrawer touches. -------------
class FakeElement {
    constructor(doc, id, { cls = "", view = null, parent = null, text = "" } = {}) {
        Object.assign(this, { doc, id, cls, parent, textContent: text, hidden: false, inert: false });
        this.dataset = view ? { view } : {};
        this.children = []; this.listeners = {}; this.attributes = {}; this.offsetWidth = 1;
        const names = new Set();
        this.classList = { add: (...c) => c.forEach((x) => names.add(x)), remove: (...c) => c.forEach((x) => names.delete(x)), contains: (c) => names.has(c) };
        if (parent) parent.children.push(this);
        if (id) doc.byId.set(id, this);
    }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
    matches(selector) { return selector.split(",").some((part) => part.trim() === "." + this.cls || part.trim() === "#" + this.id); }
    closest(selector) { for (let at = this; at; at = at.parent) if (at.matches(selector)) return at; return null; }
    contains(other) { for (let at = other; at; at = at.parent) if (at === this) return true; return false; }
    descendants() { return this.children.flatMap((child) => [child, ...child.descendants()]); }
    querySelectorAll(selector) { return this.descendants().filter((item) => item.matches(selector)); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    focus() { this.doc.activeElement = this; }
}
class FakeDocument {
    constructor() { this.byId = new Map(); this.capture = {}; this.bubble = {}; this.activeElement = null; }
    getElementById(id) { return this.byId.get(id) || null; }
    addEventListener(type, listener, capture = false) { ((capture ? this.capture : this.bubble)[type] ||= []).push(listener); }
    /** Capture on the document, then the target and its ancestors, then bubble on the document. */
    dispatch(type, target, extra = {}) {
        const event = { type, target, button: 0, ...extra };
        for (const listener of this.capture[type] || []) listener(event);
        for (let at = target; at && at !== this; at = at.parent) for (const listener of at.listeners[type] || []) listener(event);
        for (const listener of this.bubble[type] || []) listener(event);
    }
    /** A mouse click: press, then click. */
    tap(target) { this.dispatch("pointerdown", target); this.dispatch("click", target); }
}

function page({ closeAtRoot = false, panTo = () => false } = {}) {
    const doc = new FakeDocument();
    const host = new FakeElement(doc, "app");
    const drawer = new FakeElement(doc, "device-drawer", { cls: "device-drawer", view: "shelf", parent: host });
    new FakeElement(doc, "device-drawer-back", { parent: drawer });
    new FakeElement(doc, "device-drawer-title", { parent: drawer });
    new FakeElement(doc, "device-drawer-close", { parent: drawer });
    for (const [view, title] of [["shelf", "Equipment"], ["details", "Switch A"], ["placement", "Place or move device"]]) {
        const panel = new FakeElement(doc, "panel-" + view, { cls: "device-drawer-view", view, parent: drawer });
        new FakeElement(doc, "", { cls: "device-drawer-panel-title", parent: panel, text: title });
    }
    const inside = new FakeElement(doc, "inside-button", { parent: doc.getElementById("panel-details") });
    const map = new FakeElement(doc, "map", { parent: host });
    const card = new FakeElement(doc, "card", { cls: "card", parent: map });
    let closed = 0;
    const shell = new DeviceDrawer({
        host, doc, closeAtRoot, rootView: "shelf",
        onClose: () => { closed += 1; },
        keepsOpen: (target) => Boolean(target.closest(".card")),
        ignoreClick: panTo,
    });
    // A card opens the drawer on details, as both pages do.
    card.addEventListener("click", () => { shell.reset("details", [], "forward"); shell.open(); });
    return { doc, host, shell, map, card, inside, closedCount: () => closed };
}

// ---- Panel stack, bar title, Back. ------------------------------------
{
    const { doc, host, shell, card } = page();
    assert.equal(host.dataset.drawer, undefined);
    doc.tap(card);
    assert.equal(host.dataset.drawer, "open", "A card opens the drawer");
    assert.equal(shell.currentView, "details");
    assert.equal(doc.getElementById("device-drawer-title").textContent, "Switch A", "The bar shows the panel title");
    assert.equal(doc.getElementById("device-drawer-back").hidden, true, "No Back on the first panel");
    shell.push("placement");
    assert.deepEqual([...shell.viewStack], ["details"]);
    assert.equal(doc.getElementById("device-drawer-title").textContent, "Place or move device");
    assert.equal(doc.getElementById("device-drawer-back").hidden, false);
    assert.equal(doc.getElementById("panel-details").inert, true, "Inactive panels are inert");
    assert.equal(doc.getElementById("panel-placement").inert, false);
    doc.tap(doc.getElementById("device-drawer-back"));
    assert.equal(shell.currentView, "details", "Back steps one panel");
    assert.equal(host.dataset.drawer, "open", "Back inside the drawer never closes it");
    shell.back();
    assert.equal(shell.currentView, "shelf", "Back past the first panel shows the root");
    assert.equal(host.dataset.drawer, "open", "Without closeAtRoot the root is a real panel");
}
// ---- closeAtRoot: the root means closed (Discovery). -------------------
{
    const { doc, host, shell, card, closedCount } = page({ closeAtRoot: true });
    doc.tap(card);
    shell.back();
    assert.equal(host.dataset.drawer, "closed");
    assert.equal(closedCount(), 1, "onClose runs once");
}
// ---- Which clicks close it. -------------------------------------------
{
    const { doc, host, shell, map, card, inside, closedCount } = page();
    doc.tap(card);
    assert.equal(host.dataset.drawer, "open", "The click that opened it does not close it");
    doc.tap(card);
    assert.equal(host.dataset.drawer, "open", "A card (keepsOpen) switches instead of closing");
    doc.dispatch("pointerdown", inside);
    doc.dispatch("click", map);
    assert.equal(host.dataset.drawer, "open", "A press that began inside never closes it");
    doc.dispatch("click", map);
    assert.equal(host.dataset.drawer, "open", "A keyboard click (no press) never closes it");
    doc.tap(map);
    assert.equal(host.dataset.drawer, "closed", "A press and click outside close it");
    assert.equal(closedCount(), 1);
    doc.tap(map);
    assert.equal(closedCount(), 1, "Closing twice runs onClose once");
    doc.tap(card);
    doc.dispatch("keydown", map, { key: "Escape" });
    assert.equal(host.dataset.drawer, "closed", "Escape closes it");
    doc.tap(card);
    doc.tap(doc.getElementById("device-drawer-close"));
    assert.equal(host.dataset.drawer, "closed", "Close closes it");
}
{
    let panned = true;
    const { doc, host, map, card } = page({ panTo: () => panned });
    doc.tap(card);
    doc.tap(map);
    assert.equal(host.dataset.drawer, "open", "ignoreClick (the end of a pan) keeps it open");
    panned = false;
    doc.tap(map);
    assert.equal(host.dataset.drawer, "closed");
}
"device drawer checks passed";
`;
console.log(vm.runInContext(checks, context));
