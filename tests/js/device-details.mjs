/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Run with `node tests/js/device-details.mjs`; no browser or network required.
 *
 * Checks the shared details module (static/js/device-details.js): port
 * state, the port-to-cable index, placement labels, and neighbor records.
 * Equipment's rack lamps use the same port functions. The checks are the
 * `checks` string, so they can also run in a browser console.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({ assert, labratFormatSpeed: (mbps) => `${mbps} Mb/s`, LABRAT_PORT_LIST_LIMIT: 16 });
vm.runInContext(fs.readFileSync(new URL('../../src/labrat/static/js/device-details.js', import.meta.url), 'utf8'), context);

const checks = String.raw`
const details = Object.create(DeviceDetails.prototype);
const nodes = [
    { id: "sw", name: "Switch", observations: [{ source: "unifi:test", last_seen_at: "2026-01-01T00:00:00Z",
        neighbors: [{ local_port: "1", chassis_id: "aa", port_id: "", resolved: false }] }], ports: [] },
    { id: "srv", name: "Server", observations: [], ports: [] },
];
let links = [
    { from: "sw", from_port: "Port 1", to: "srv", to_port: "nic0", confidence: "inferred", evidence: "unifi-client-mac" },
    { from: "sw", from_port: "Port 2", to: "srv", to_port: "nic1", confidence: "ambiguous", evidence: "lldp-name" },
];
const locations = { containers: [
    { id: 1, kind: "rack", name: "Main rack", parent_id: null },
    { id: 2, kind: "rack_top", name: "On top", parent_id: 1 },
    { id: 3, kind: "rack_shelf", name: "U4 Front shared shelf", parent_id: 1 },
] };
details.nodes = () => nodes;
details.links = () => links;
details.locations = () => locations;
details.cables = new Map();
details.index();

// The cable index names the far end from either side.
assert.equal(details.cableAt("sw", "Port 1").otherName, "Server");
assert.equal(details.cableAt("srv", "nic0").otherPort, "Port 1");
assert.equal(details.cableAt("sw", "Port 9"), undefined);

// Port state: explicit state wins; then speed; then a trusted (not ambiguous) link.
assert.equal(details.portIsUp({ name: "Port 1", link_up: false, speed_mbps: 1000 }, "sw"), false, "Explicitly down stays down");
assert.equal(details.portIsUp({ name: "Port 3", link_up: null, speed_mbps: 1000 }, "sw"), true, "Positive speed counts");
assert.equal(details.portIsUp({ name: "Port 1", link_up: null }, "sw"), true, "An inferred cable counts");
assert.equal(details.portIsUp({ name: "Port 2", link_up: null }, "sw"), false, "An ambiguous cable does not");
assert.equal(details.portStatus({ name: "Port 1", link_up: null }, "sw"), "inferred connection · unifi-client-mac");
assert.equal(details.portStatus({ name: "Port 2", link_up: null }, "sw"), "tentative connection · lldp-name");
assert.equal(details.portStatus({ name: "Port 9", link_up: null }, "sw"), "unknown");

// Placement labels name the rack behind a rack top or shared shelf.
assert.equal(details.placementLabel({ placement: null }), "Not placed");
assert.equal(details.placementLabel({ placement: { container_kind: "rack", container_id: 1, container_name: "Main rack", rack_u: 7, face: "rear" } }), "Main rack · U7 · rear");
assert.equal(details.placementLabel({ placement: { container_kind: "rack_top", container_id: 2, container_name: "On top" } }), "On top of Main rack");
assert.equal(details.placementLabel({ placement: { container_kind: "rack_shelf", container_id: 3, rack_u: 4, face: "front" } }), "Main rack · U4 shared shelf · front");
assert.equal(details.placementLabel({ placement: { container_kind: "floor", container_id: 9, container_name: "Floor" } }), "Floor");
details.locations = () => null;
assert.equal(details.placementLabel({ placement: { container_kind: "rack_top", container_id: 2 } }), "On top of a rack", "Works without locations");

// Neighbor records carry their observation's source and time.
const neighbor = details.neighborsFor(nodes[0])[0];
assert.equal(neighbor.source, "unifi:test");
assert.equal(neighbor.seenAt, "2026-01-01T00:00:00Z");

// Wi-Fi: only the IP-matched connected-client evidence counts.
assert.equal(details.wirelessFor({ observations: [{ wireless: { evidence: "other" } }] }), null);
assert.equal(details.wirelessFor({ observations: [{ wireless: { evidence: "unifi-client-attachment", state: "connected" } }] }).state, "connected");
assert.equal(details.wirelessFor({ observations: [{ wireless: { evidence: "unifi-connected-client-ip", state: "connected" } }] }).state, "connected");

// Bonds: a member NIC finds its bond; others find none.
const bonds = [{ name: "bond0", members: ["nic0", "nic1"] }];
assert.equal(details.bondForPort({ name: "nic1" }, bonds).name, "bond0");
assert.equal(details.bondForPort({ name: "nic9" }, bonds), null);
// Port facts: a bare server NIC takes medium, connector, and speed from the
// switch port at the other end of its cable; copper means RJ45.
nodes[0].ports = [{ name: "Port 1", medium: "copper", speed_mbps: 2500, connector_type: null }];
nodes[1].ports = [{ name: "nic0", link_up: true }, { name: "nic9", link_up: false }];
details.locations = () => null;
details.cables = new Map(); details.index();
assert.deepEqual({ ...details.portFacts(nodes[1].ports[0], "srv") }, { medium: "copper", connector: "rj45", speedMbps: 2500 });
assert.deepEqual({ ...details.portFacts(nodes[1].ports[1], "srv") }, { medium: null, connector: "unknown", speedMbps: null });

// USB and relayed UPS links, from both ends.
{
    const ups = (id, attachment) => ({ id, name: id, ports: [], observations: [{ ups: { attachment, server_node_id: "nut" } }] });
    const fleet = [{ id: "nut", name: "Nut", ports: [], observations: [] }, ups("usb-ups", "usb"), ups("relay-ups", "relayed")];
    const view = Object.create(DeviceDetails.prototype);
    view.nodes = () => fleet;
    assert.deepEqual(view.usbPorts(fleet[0]).map((card) => card.cable), ["USB → usb-ups"]);
    assert.deepEqual(view.usbPorts(fleet[1]).map((card) => card.cable), ["USB → Nut"]);
    assert.deepEqual(view.relayedPorts(fleet[0]).map((card) => card.name), ["relay-ups"]);
    assert.deepEqual(view.relayedPorts(fleet[2]).map((card) => card.cable), ["reports through"]);
    assert.equal(view.relayedPorts(fleet[1]).length, 0, "A USB UPS is not relayed");
}

// A port only a client record adds, on a device whose host source reports
// its NICs, is out-of-band management; the host's power is its own.
{
    const server = {
        ports: [{ name: "nic0", observation_id: 1 }, { name: "Wired NIC", observation_id: 3 }],
        observations: [
            { id: 1, source: "proxmox:default", status: "offline" },
            { id: 2, source: "Clients · unifi", status: "online", client: {} },
            { id: 3, source: "Clients · unifi", status: "online", client: {} },
        ],
    };
    const management = deviceManagement(server);
    assert.deepEqual([...management.ports], ["Wired NIC"]);
    assert.deepEqual(management.observations.map((item) => item.id), [3], "A record matched to a host NIC adds no port");
    assert.deepEqual({ ...deviceHostPower(server) }, { hostOffline: true, bmcReachable: true });
    const client = { ports: [{ name: "Wired NIC", observation_id: 3 }], observations: [server.observations[2]] };
    assert.equal(deviceManagement(client).ports.size, 0, "A plain client's own port is not management");
}

// Addresses: interfaces first, one row per address, BMC records named.
{
    const host = {
        id: "h", ports: [{ name: "nic0", observation_id: 1 }, { name: "Wired NIC", observation_id: 3 }],
        observations: [
            { id: 1, source: "proxmox", identifiers: { management_ips: ["192.0.2.5"] } },
            { id: 3, source: "Clients · unifi", client: { ip_address: "192.0.2.9" } },
        ],
    };
    const view = Object.create(DeviceDetails.prototype);
    view.nodes = () => [host]; view.links = () => []; view.wanGateways = [];
    view.wifiPort = () => null;
    const rows = view.deviceAddresses(host, [{ name: "vmbr0", kind: "bridge", details: { cidr: "192.0.2.5/24" } }]);
    assert.deepEqual(rows.map((row) => row.ip + " " + row.iface), ["192.0.2.5/24 vmbr0", "192.0.2.9 BMC"]);
}

// VPN kinds read for people.
assert.equal(deviceVpnKind("openvpn-client").name, "OpenVPN");
assert.equal(deviceVpnKind("wireguard-server").name, "WireGuard");
assert.equal(deviceVpnKind("ipsec-vpn").name, "IPsec");
assert.equal(deviceVpnKind("site-magic").name, "site");

// Short source names, as in the header's source strip.
assert.equal(deviceSourceName("unifi:default"), "unifi");
assert.equal(deviceSourceName("Clients 2 · unifi:default"), "unifi", "A derived source names its parent");
assert.equal(deviceSourceName("proxmox"), "proxmox");
assert.deepEqual([...deviceDistinct(["a", null, "", "a", "b", undefined])], ["a", "b"]);
"device details checks passed";
`;
console.log(vm.runInContext(checks, context));
