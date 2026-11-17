/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/** Check the widget kit's pure helpers and the cluster model's derived facts. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = (name) => fs.readFileSync(new URL(`../../src/labrat/static/js/${name}`, import.meta.url), 'utf8');
const window = { setTimeout: () => 0 };
const document = { readyState: 'complete', querySelectorAll: () => [], getElementById: () => null };
const context = vm.createContext({ window, document, setTimeout: () => 0, Date, Math, Number, JSON, Map, Set, assert });
vm.runInContext(source('widgets.js'), context);
vm.runInContext(source('cluster-model.js'), context);
vm.runInContext(source('network-model.js'), context);
const W = window.LabratWidgets;
const M = window.LabratClusterModel;
const { niceCeil, fractionCeil, timeTicks, foldSlices, segment, squarify } = W.internals;

// Formatters.
assert.equal(W.bytes(1536 * 1024 ** 3), '1.5 TiB');
assert.equal(W.bytes(null), '—');
assert.equal(W.rate(1250000), '10.0 Mb/s');
assert.equal(W.percent(0.426), '43%');
assert.equal(W.severity(0.85), 'is-warning');
assert.equal(W.severity(0.95), 'is-alarm');
assert.deepEqual(JSON.parse(JSON.stringify(W.tally([{ k: 'a' }, { k: 'b' }, { k: 'a' }], (row) => row.k))), [{ label: 'a', value: 2 }, { label: 'b', value: 1 }]);

// Scales.
assert.equal(niceCeil(3.2), 5);
assert.equal(niceCeil(0.21), 0.25);
assert.equal(niceCeil(80), 100);
assert.equal(niceCeil(0), 1);
assert.equal(fractionCeil(0.04), 0.1);
assert.equal(fractionCeil(0.97), 1);
const ticks = timeTicks(0, 86400);
assert.equal(ticks[ticks.length - 1].label, 'now');
assert.equal(ticks[0].label, '−24 h');
assert.equal(ticks.length, 5);
assert.equal(timeTicks(0, 3600)[0].label, '−1 h');
assert.equal(timeTicks(0, 1800)[0].label, '−30 min');

// Slices keep fixed colours by position; past five, the tail folds into Other.
const folded = foldSlices([...'abcdefg'].map((label, index) => ({ label, value: 10 - index })).concat([{ label: 'off', value: 2, tone: 'muted' }, { label: 'none', value: 0 }]));
assert.equal(folded.map((slice) => slice.slot).join(), '1,2,3,4,other,muted');
assert.equal(folded[4].value, 6 + 5 + 4);
assert.equal(foldSlices([{ label: 'x', value: 1 }])[0].slot, '1');

// A whole ring is drawn as two arcs; a wedge starts at the centre.
assert.equal((segment(100, 90, 60, 0, 1).match(/A/g) || []).length, 4);
assert.ok(segment(100, 90, 0, 0, 0.25).startsWith('M100,100'));

// The treemap fills its box exactly.
const placed = squarify([{ value: 6 }, { value: 3 }, { value: 1 }, { value: 0 }], { x: 0, y: 0, w: 100, h: 50 });
assert.equal(placed.length, 3);
assert.ok(Math.abs(placed.reduce((total, rect) => total + rect.w * rect.h, 0) - 5000) < 1e-6);
assert.equal(squarify([], { x: 0, y: 0, w: 10, h: 10 }).length, 0);

// Feeds register by name.
W.register('test.feed', () => ({ widget: 'donut', data: [] }));

// Cluster model: history is weighted by CPUs and installed memory.
const node = (id, extra = {}) => ({ remote_id: id, name: id, status: 'online', maxcpu: 4, maxmem: 100, mem: 50, cpu: 0.1, ...extra });
const cluster = {
  nodes: [
    node('a', { history: [{ t: 1, cpu: 0.5, mem: 0.5 }] }),
    node('b', { maxcpu: 12, maxmem: 300, history: [{ t: 1, cpu: 0.1, mem: 0.1 }], disks: [{ path: '/dev/sda', worn_percent: 85 }] }),
  ],
  guests: [
    { vmid: 1, name: 'one', node: 'a', status: 'running', kind: 'vm', vcpus: 8, memory: 10 },
    { vmid: 2, name: 'two', node: 'b', status: 'stopped', kind: 'container', memory: 5 },
    { vmid: 3, name: 'tpl', node: 'b', status: 'stopped', kind: 'vm', template: true },
  ],
  networks: [],
  cluster: { quorate: true, not_backed_up: [1, 2] },
};
const history = M.clusterHistory(cluster);
assert.equal(history.length, 1);
assert.ok(Math.abs(history[0].cpu - (0.5 * 4 + 0.1 * 12) / 16) < 1e-9);
assert.ok(Math.abs(history[0].mem - (50 + 30) / 400) < 1e-9);
assert.equal(M.runningGuests(cluster).length, 1);
assert.equal(M.runningGuests(cluster, 'b').length, 0);
assert.equal(M.osFamily({ ostype: 'l26' }), 'Linux');
assert.equal(M.guestOs({ ostype: 'win11', agent: { os: { name: 'Example OS' } } }), 'Example OS');

// Attention: worst first, linked under the given base.
const items = M.attentionItems(cluster, '/cluster');
assert.equal(items[0].tone, 'alarm');
assert.match(items[0].text, /85% worn/);
assert.equal(items[0].href, '/cluster/nodes/b');
assert.ok(items.some((item) => item.tone === 'info' && /8 vCPUs/.test(item.text)));
assert.ok(items.some((item) => /2 of 2 guests are in no backup job/.test(item.text)));
// Device drawer: a device matches a node by observation ID, a guest by source and VM ID.
vm.runInContext(source('device-cluster.js'), context);
const D = window.LabratDeviceCluster;
const data = { sources: [{ id: 9, ...cluster, nodes: cluster.nodes.map((item, index) => ({ ...item, id: 100 + index })) }] };
assert.equal(D.match(data, { observations: [{ id: 101 }] }).node.remote_id, 'b');
assert.equal(D.match(data, { observations: [{ id: 7, client: { virtualization: { source_id: 9, vmid: 2 } } }] }).guest.name, 'two');
assert.equal(D.match(data, { client: { virtualization: { source_id: 9, vmid: 1 } }, observations: [] }).guest.name, 'one');
assert.equal(D.match(data, { observations: [{ id: 5 }] }), null);
assert.equal(D.match(data, { observations: [{ id: 7, client: { virtualization: { source_id: 8, vmid: 1 } } }] }), null);

// Wi-Fi channels: one group per band in band order, your own channels marked.
const N = window.LabratNetworkModel;
const groups = N.channelGroups({
  neighbours: [{ band: '5', channel: 36, count: 2 }, { band: '2.4', channel: 6, count: 4 }, { band: '2.4', channel: 1, count: 1 }],
  devices: [{ features: ['accessPoint'], radios: [{ band: 2.4, channel: 6 }, { band: 5.8, channel: 149 }] }, { features: [], radios: [{ band: 2.4, channel: 1 }] }],
});
assert.equal(groups.map((group) => group.label).join(), '2.4 GHz,5 GHz');
assert.equal(groups[0].rows.map((row) => `${row.label}:${row.value}:${row.mark}`).join(), '6:4:true,1:1:false');
assert.equal(groups[1].rows[0].mark, false);
assert.equal(N.bandName(6.0), '6');
assert.equal(N.channelGroups({}).length, 0);

// Disk issues: SMART health first; sector counts warn only at replacement levels, else a note.
assert.equal(M.diskIssues({ health: 'FAILED', smart: { pending_sectors: 3 } }).map((item) => item.tone).join(), 'alarm,info');
const failing = M.diskIssues({ health: 'PASSED', smart: { reallocated_sectors: 10, pending_sectors: 728, uncorrectable_sectors: 64 } })[0];
assert.equal(failing.tone, 'warn');
assert.match(failing.text, /10 reallocated, 728 pending, 64 uncorrectable: plan a replacement/);
assert.equal(M.sectorIssue({ smart: { reallocated_sectors: 5 } }).tone, 'info');
assert.equal(M.sectorIssue({ smart: { reallocated_sectors: 1, pending_sectors: 8, uncorrectable_sectors: 3 } }).tone, 'info');
assert.equal(M.sectorIssue({ smart: { media_errors: 12 } }).tone, 'warn');
assert.equal(M.sectorIssue({ smart: { pending_sectors: 0 } }), null);
const noted = M.attentionItems({ ...cluster, nodes: [node('c', { disks: [{ path: '/dev/sdd', smart: { reallocated_sectors: 5 } }] })] }, '/cluster');
assert.ok(noted.some((item) => item.tone === 'info' && /sdd has 5 reallocated, below replacement levels/.test(item.text)));
assert.equal(M.diskIssues({ health: 'PASSED', type: 'hdd', smart: { temperature_c: 30 } }).length, 0);
assert.equal(M.tempTone({ type: 'hdd', smart: { temperature_c: 52 } }), 'warn');

// ZFS shelf: devices match SMART records by serial, grouped by vdev; scan lines read short.
vm.runInContext(source('widget-shelf.js'), context);
vm.runInContext(source('shelf-model.js'), context);
const Z = window.LabratShelfModel;
const bays = Z.members({ name: 'tank', vdevs: [
  { name: 'raidz1-0', depth: 0, leaf: false },
  { name: 'ata-EXAMPLE_SER1-part1', depth: 1, leaf: true, state: 'ONLINE' },
  { name: 'ata-EXAMPLE_SER2-part1', depth: 1, leaf: true, state: 'FAULTED' },
  { name: 'logs', depth: 0, leaf: false },
  { name: 'nvme0n1', depth: 1, leaf: true, state: 'ONLINE' },
] }, { disks: [{ path: '/dev/sdb', serial: 'SER2' }, { path: '/dev/sda', serial: 'SER1' }, { path: '/dev/nvme0n1', serial: null }] });
assert.equal(bays.map((bay) => `${bay.group}:${bay.disk && bay.disk.path}`).join(), 'raidz1-0:/dev/sda,raidz1-0:/dev/sdb,logs:/dev/nvme0n1');
assert.match(Z.scrub('scrub repaired 0B in 13:44:07 with 2 errors on Sun Nov  8 14:08:09 2026').text, /^Scrub .*: 0B repaired, 2 errors, took 13:44:07$/);
assert.equal(Z.scrub('scrub repaired 0B in 13:44:07 with 2 errors on Sun Nov  8 14:08:09 2026').tone, 'warn');
assert.equal(Z.scrub(null).tone, 'off');

// Ceph pool colours: fixed by raw use; an OSD carries the pools whose rule keeps to its class.
const cephData = {
  osd_list: [{ id: 1, device_class: 'hdd' }, { id: 2, device_class: 'nvme' }],
  pool_list: [{ name: 'small', raw_used: 1, rule: 'replicated_hdd' }, { name: 'big', raw_used: 9, rule: 'replicated_nvme' }, { name: 'any', raw_used: 5, rule: 'replicated_rule' }],
};
const slots = M.cephPoolSlots(cephData);
assert.equal(slots.map((entry) => `${entry.pool.name}:${entry.slot}:${entry.deviceClass}`).join(), 'big:1:nvme,any:2:null,small:3:hdd');
assert.equal(M.osdPools({ device_class: 'hdd' }, slots).map((entry) => entry.pool.name).join(), 'any,small');
assert.equal(M.osdPools({ device_class: 'nvme' }, slots).map((entry) => entry.pool.name).join(), 'big,any');
assert.equal(M.ruleClass('replicated_ssd', cephData.osd_list), null);

// Latency gauge: 10–150 ms scale, clamped by the gauge; yellow over 80, red over 100.
assert.equal(Z.latencySpec(10, '').value, 0);
assert.equal(Z.latencySpec(150, '').value, 1);
assert.equal(Z.latencySpec(80, '').tone, 'on');
assert.equal(Z.latencySpec(81, '').tone, 'warn');
assert.equal(Z.latencySpec(101, '').tone, 'alarm');
assert.equal(Z.latencySpec(null, '').tone, 'off');
assert.equal(Z.latencySpec(45, '').kind, 'dial');
assert.equal(Z.osdLatency({ commit_latency_ms: 12, apply_latency_ms: 40 }), 40);
assert.equal(Z.osdLatency({}), null);

// Every drive: one chassis per node, grouped boot, ZFS, Ceph by class, then the rest.
const drivesSource = {
  cluster: { ceph: { health: 'HEALTH_OK', osds: { up: 1, total: 1 }, osd_list: [{ id: 3, name: 'osd.3', host: 'node-a', device_class: 'hdd', status: 'up', in: true, percent_used: 10, commit_latency_ms: 20 }],
    pool_list: [{ name: 'rbd', raw_used: 5, rule: 'replicated_hdd' }] } },
  nodes: [{ name: 'node-a', rootfs_used: 1, rootfs_total: 4, history: [], disks: [
    { path: '/dev/nvme0n1', type: 'nvme', boot: true, size: 1, smart: {} },
    { path: '/dev/sda', type: 'hdd', serial: 'S1', size: 2, smart: {} },
    { path: '/dev/sdb', type: 'hdd', osds: ['3'], size: 3, smart: { pending_sectors: 70 } },
    { path: '/dev/sdc', type: 'ssd', size: 4, smart: {} },
  ], zfs: [{ name: 'tank', size: 10, alloc: 5, vdevs: [{ name: 'mirror-0', depth: 0, leaf: false }, { name: 'ata-X_S1-part1', depth: 1, leaf: true, state: 'ONLINE' }] }] }],
};
const everyDrive = Z.clusterDrives(drivesSource);
assert.equal(everyDrive.chassis.length, 1);
assert.equal(everyDrive.chassis[0].groups.map((group) => `${group.name}:${group.bays.map((bay) => bay.label).join('+')}`).join(), 'Boot:nvme0n1,ZFS tank:sda,Ceph hdd OSDs:OSD 3,Other drives:sdc');
assert.equal(everyDrive.chassis[0].groups[0].bays[0].gauges[0].value, 0.25);
assert.equal(everyDrive.health.tone, 'warn');
assert.equal(everyDrive.health.key.map((entry) => `${entry.label}:${entry.slot}`).join(), 'rbd:1,tank (node-a):2');
assert.ok(everyDrive.health.found.some((item) => /node-a sdb: 70 pending: plan a replacement/.test(item.text)));

// A node's drives as one flat list for its faceplate, same order as the overview.
assert.equal(Z.drivesByNode(drivesSource).get('node-a').map((bay) => bay.label).join(), 'nvme0n1,sda,OSD 3,sdc');

// Bay layout: rows and columns balanced (8 drives make two rows of 4), no empty slots.
assert.equal(W.shelfInternals.bayLayout(8, 4), 4);
assert.equal(W.shelfInternals.bayLayout(8, 9), 4);
assert.equal(W.shelfInternals.bayLayout(8, 3), 3);
assert.equal(W.shelfInternals.bayLayout(12, 4), 4);
assert.equal(W.shelfInternals.bayLayout(6, 5), 3);
assert.equal(W.shelfInternals.bayLayout(2, 4), 2);
assert.equal(W.shelfInternals.bayLayout(2, 1), 0);

// Ceph: OSDs find their drives through the nodes' disk lists; down is red, out and near full amber.
const drives = Z.osdDisks([{ name: 'node-a', disks: [{ path: '/dev/sdb', osds: ['3'] }] }]);
assert.equal(drives.get('3').node, 'node-a');
const osd = { name: 'osd.3', status: 'up', in: true, percent_used: 40, commit_latency_ms: 5 };
assert.equal(Z.osdIssues(osd, null).length, 0);
assert.equal(Z.osdIssues({ ...osd, status: 'down' }, null)[0].tone, 'alarm');
assert.equal(Z.osdIssues({ ...osd, in: false }, null)[0].tone, 'warn');
assert.equal(Z.osdIssues({ ...osd, percent_used: 87 }, null)[0].tone, 'warn');
assert.equal(Z.osdIssues({ ...osd, percent_used: 96 }, null)[0].tone, 'alarm');
assert.equal(Z.osdIssues({ ...osd, commit_latency_ms: 250 }, null)[0].tone, 'info');
assert.equal(Z.osdIssues(osd, { health: 'PASSED', smart: { pending_sectors: 80 } })[0].tone, 'warn');
console.log('widgets: ok');
