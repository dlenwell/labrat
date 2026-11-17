/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Check header disclosure animation races, dismissal, and reduced motion. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const checks = String.raw`
class Target {
  handlers = {};
  addEventListener(type, fn) { this.handlers[type] = fn; }
  fire(type, extra = {}) { this.handlers[type]?.({preventDefault() {}, ...extra}); }
}
const animations = [];
const makeMenu = (name) => {
  const menu = new Target();
  menu.classList = { contains: (value) => value === name };
  menu.open = false;
  menu.button = new Target();
  menu.button.focus = () => { focused = menu.button; };
  menu.panel = { inert: false, offsetHeight: 140, animate(frames, options) {
    let finish, reject;
    const animation = { frames, options, finished: new Promise((a,b) => {finish=a; reject=b;}),
      finish() { finish(); }, cancel() { reject(); } };
    animations.push(animation);
    return animation;
  }};
  menu.querySelector = selector => selector === 'summary' ? menu.button : menu.panel;
  menu.contains = target => [menu, menu.button, menu.panel].includes(target);
  return menu;
};
let focused, reduced = false, wide = false; // Start folded: panels spring.
// Fake timers and clock for the hover-open delay.
let now = 0, timerId = 0;
const timers = new Map();
const setTimeout = (fn, ms) => { timers.set(++timerId, {fn, at: now + ms}); return timerId; };
const clearTimeout = (id) => { timers.delete(id); };
const advance = (ms) => { now += ms; for (const [id, t] of [...timers]) if (t.at <= now) { timers.delete(id); t.fn(); } };
const Date = { now: () => now };
const menus = [makeMenu('locations-menu'), makeMenu('account-menu')];
const document = new Target();
document.querySelectorAll = () => menus;
document.getElementById = () => null;
document.querySelector = () => null;
const matchMedia = (query) => ({matches: query.includes('reduced-motion') ? reduced : wide, addEventListener() {}});
const getComputedStyle = (element) => ({opacity:'0.5',transform:'matrix(1, 0, 0, 1, 0, -4)',
  getPropertyValue: (name) => name === '--slide-offset' ? (element.slideOffset || '') : ''});
`;
const context = vm.createContext({assert});
vm.runInContext(checks, context);
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/main.js',import.meta.url),'utf8'),context);
await vm.runInContext(`(async () => {
const [locations, account] = menus;
locations.button.fire('click');
assert.equal(locations.open,true);
assert.equal(locations.panel.inert,false);
assert.equal(animations.at(-1).options.duration,320);
assert.ok(animations.at(-1).frames[1].transform); // Folded: Locations springs.
const firstOpening = animations.at(-1);
document.fire('click',{target:{}});
assert.equal(locations.open,true); // Keep the panel rendered for its retreat.
assert.equal(locations.panel.inert,true);
const closing = animations.at(-1);
locations.button.fire('click'); // Reopen before exit finishes.
closing.finish(); firstOpening.finish();
await Promise.resolve();
assert.equal(locations.open,true);
assert.equal(locations.panel.inert,false);
animations.at(-1).finish(); await Promise.resolve();
wide = true; // Above the ☰ fold both panels slide.
account.button.fire('click');
assert.equal(locations.panel.inert,true);
assert.equal(account.open,true);
// On wider screens the account panel slides down from behind its button.
// Its top moves by its height in step with an equal clip, so it never shows above.
assert.deepEqual({...animations.at(-1).frames[0]},{top:'calc(100% - 140px)',clipPath:'inset(140px -2rem -2rem -2rem)'});
assert.deepEqual({...animations.at(-1).frames[1]},{top:'calc(100% + 0px)',clipPath:'inset(0px -2rem -2rem -2rem)'});
const locationExit = animations.at(-2);
locationExit.finish(); await Promise.resolve();
assert.equal(locations.open,false);
account.fire('keydown',{key:'Escape'});
assert.equal(focused,account.button);
assert.equal(account.panel.inert,true);
animations.at(-1).finish(); await Promise.resolve();
assert.equal(account.open,false);
// Locations rests 16 px above its tab's bottom (the nav pill's middle) and slides the same way.
locations.panel.slideOffset = '-16px';
locations.button.fire('click');
assert.deepEqual({...animations.at(-1).frames[0]},{top:'calc(100% - 156px)',clipPath:'inset(140px -2rem -2rem -2rem)'});
assert.deepEqual({...animations.at(-1).frames[1]},{top:'calc(100% - 16px)',clipPath:'inset(0px -2rem -2rem -2rem)'});
animations.at(-1).finish(); await Promise.resolve();
locations.button.fire('click');
animations.at(-1).finish(); await Promise.resolve();
assert.equal(locations.open,false);
// Resting the mouse on a button for half a second opens it; leaving first cancels.
reduced = true;
locations.button.fire('pointerenter',{pointerType:'mouse'});
advance(499);
assert.equal(locations.open,false);
locations.button.fire('pointerleave');
advance(500);
assert.equal(locations.open,false);
locations.button.fire('pointerenter',{pointerType:'touch'}); // Touch never hover-opens.
advance(500);
assert.equal(locations.open,false);
locations.button.fire('pointerenter',{pointerType:'mouse'});
advance(500);
assert.equal(locations.open,true);
advance(100);
locations.button.fire('click'); // A click right after the hover opened it keeps it open.
assert.equal(locations.open,true);
account.button.fire('pointerenter',{pointerType:'mouse'});
advance(500);
assert.equal(account.open,true);
assert.equal(locations.open,false); // Opening one closes the other.
advance(700); // Past the 600 ms grace window.
account.button.fire('click'); // A later click closes it as usual.
assert.equal(account.open,false);
const count = animations.length;
locations.button.fire('click');
assert.equal(locations.open,true);
locations.fire('focusout',{relatedTarget:{}});
assert.equal(locations.open,false);
assert.equal(animations.length,count);
locations.button.fire('click');
locations.button.fire('click');
assert.equal(locations.open,false);
assert.equal(animations.length,count);
})()`,context);
console.log('navigation-menus checks passed');

// Phone ☰ header menu: opens, closes on Escape and outside clicks.
await vm.runInContext(`(async () => {
const header = new Target(), button = new Target(), attributes = new Set();
header.toggleAttribute = (name, on) => on ? attributes.add(name) : attributes.delete(name);
header.hasAttribute = (name) => attributes.has(name);
header.contains = (target) => target === button;
button.attributes = {};
button.setAttribute = (name, value) => { button.attributes[name] = value; };
button.focus = () => { focused = button; };
document.querySelector = () => header;
document.getElementById = () => button;
bindSiteMenu();
button.fire('click');
assert.equal(header.hasAttribute('data-menu-open'), true);
assert.equal(button.attributes['aria-expanded'], 'true');
header.fire('keydown', { key: 'Escape', target: { closest: () => null } });
assert.equal(header.hasAttribute('data-menu-open'), false);
assert.equal(focused, button);
button.fire('click');
document.fire('click', { target: { closest: () => null } });
assert.equal(header.hasAttribute('data-menu-open'), false);
})()`, context);
console.log('site menu checks passed');

// Device links (main.js labratDeviceSegment): inventory by number, observed
// devices prefixed, anything else none.
vm.runInContext(`
assert.equal(labratDeviceSegment("device:35"), "35");
assert.equal(labratDeviceSegment("observed:110"), "observed-110");
assert.equal(labratDeviceSegment("wan:1"), null);
assert.equal(labratDeviceSegment(null), null);
`, context);
console.log('device link checks passed');
