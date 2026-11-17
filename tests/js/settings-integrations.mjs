/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Verify read-only connection test behavior without a browser or live controller. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({ assert });
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/settings-integrations.js', import.meta.url), 'utf8'), context);
const checks = String.raw`
assert.equal(integrationSearchMatches('Proxmox VE compute clusters', 'PROXMOX cluster'), true);
assert.equal(integrationSearchMatches('UniFi Network wireless', 'compute'), false);
assert.equal(integrationSearchMatches('Réseau Network', 'reseau'), true);
const filterEvents = {};
const filter = {value:'', addEventListener:(name, fn) => { filterEvents[name] = fn; }};
const empty = {hidden:true};
const rows = [{dataset:{connectionRow:'Main UniFi network https://network.example'}, hidden:false},
              {dataset:{connectionRow:'Cluster Proxmox https://compute.example'}, hidden:false}];
IntegrationSettingsDrawer.prototype.bindFilter.call({host:{
    querySelector:(selector) => selector === '[data-integration-filter]' ? filter : empty,
    querySelectorAll:() => rows,
}});
filter.value = 'proxmox'; filterEvents.input();
assert.equal(rows[0].hidden, true); assert.equal(rows[1].hidden, false);
filter.value = 'missing'; filterEvents.input(); assert.equal(empty.hidden, false);
filter.value = ''; filterEvents.input(); assert.ok(rows.every(row => !row.hidden));
assert.equal(empty.hidden, true);
let cancelDelete;
let deleteClosed = false;
IntegrationSettingsDrawer.prototype.bindEditor.call({drawer:{viewStack:[], close:() => {deleteClosed = true;}}}, {
    querySelector:(selector) => selector === '[data-integration-form]' ? null : {addEventListener:(name, fn) => {cancelDelete = fn;}},
});
cancelDelete(); assert.equal(deleteClosed, true, 'Delete preview can be cancelled without a credential form');
const events = {};
const output = {hidden:true, textContent:'', dataset:{}};
const controls = [{disabled:false}, {disabled:false}];
const form = {
    dataset:{testUrl:'/settings/integrations/test'},
    querySelector: () => output,
    querySelectorAll: () => controls,
    addEventListener: (name, callback) => { events[name] = callback; },
};
let calls = [];
let finish;
let fail;
const fetch = (url, options) => {
    calls.push({url, options});
    return new Promise((resolve, reject) => { finish = resolve; fail = reject; });
};
const data = {csrf:'opaque', secret:'test-typed-secret'};
bindIntegrationTest(form, {fetch, makeData: () => data});
let prevented = 0;
const event = {submitter:{hasAttribute: () => true}, preventDefault: () => { prevented += 1; }};
let pending = events.submit(event);
assert.equal(prevented, 1);
assert.equal(calls.length, 1);
assert.equal(calls[0].url, '/settings/integrations/test');
assert.equal(calls[0].options.method, 'POST');
assert.equal(calls[0].options.body, data);
assert.equal(calls[0].options.credentials, 'same-origin');
assert.equal(calls[0].options.headers.Accept, 'application/json');
assert.ok(controls.every(item => item.disabled));
await events.submit(event);
assert.equal(calls.length, 1, 'Repeated test/Save is blocked while a test is pending');
finish({ok:true, json:async () => ({ok:true, message:'Connection successful.'})});
await pending;
assert.equal(output.textContent, 'Connection successful.');
assert.equal(output.dataset.state, 'success');
assert.ok(controls.every(item => !item.disabled));
assert.equal(data.secret, 'test-typed-secret', 'Test never resets the typed form values');
events.input();
assert.equal(output.hidden, true);
pending = events.submit(event);
events.input();
finish({ok:true, json:async () => ({ok:true, message:'Stale success'})});
await pending;
assert.match(output.textContent, /form changed/);
assert.notEqual(output.dataset.state, 'success');
pending = events.submit(event);
finish({ok:false, json:async () => ({ok:false, message:'Access denied'})});
await pending;
assert.equal(output.textContent, 'Access denied');
assert.equal(output.dataset.state, 'error');
pending = events.submit(event);
fail(new Error('private network error'));
await pending;
assert.match(output.textContent, /could not complete/);
assert.ok(!output.textContent.includes('private network error'));
assert.ok(controls.every(item => !item.disabled));
pending = events.submit(event);
finish({ok:true, json:async () => ({unexpected:'shape'})});
await pending;
assert.equal(output.dataset.state, 'error');
const oldCount = calls.length;
await events.submit({...event, submitter:{hasAttribute: () => false}});
assert.equal(calls.length, oldCount, 'Save remains a normal form submission');
`;
await vm.runInContext(`(async () => { ${checks} })()`, context);
console.log('settings-integrations checks passed');
