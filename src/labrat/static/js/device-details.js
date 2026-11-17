/*
 * Copyright 2026 LabRat contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Used by: Discovery, Equipment and Diagram, Discovery clients, Floor plan.
 */

/**
 * @fileoverview Shared device details, rename, and neighbor-match panels.
 *
 * Equipment and Discovery show the same details for a selected device inside
 * the shared drawer (device-drawer.js): identity, placement, ports facing,
 * sources, UPS power, the inferred Wi-Fi interface, physical ports with
 * their cables and bonds, logical bridges and VLANs, and LLDP neighbors
 * (unresolved, LACP secondary, and saved matches). Rename and neighbor
 * matching are panels of the same drawer. Panel markup comes from the
 * macros in templates/private/_device_drawer.html; styles are
 * static/css/device-details.css. Every name and value is inserted as text.
 *
 * Needs main.js (labratFormatSpeed, LABRAT_PORT_LIST_LIMIT) and ups.js
 * (upsReading, upsCondition, upsRuntime) on the page.
 */

"use strict";

/** Readable names for cable media (see discovered_ports.medium). */
const DEVICE_MEDIUM_LABELS = {
    copper: "copper Ethernet",
    dac: "DAC twinax",
    fiber_multimode: "multimode fiber",
    fiber_singlemode: "single-mode fiber",
    fiber: "fiber",
};

/**
 * Describe a cable medium for people.
 *
 * @param {string|null} medium - Stored medium value.
 * @returns {string} Readable label, or "unknown cable".
 */
function deviceMediumLabel(medium) {
    return DEVICE_MEDIUM_LABELS[medium] || "unknown cable";
}

/**
 * Describe a duration as a short clock readout: "mm:ss" under an hour,
 * then "h:mm h", then days.
 *
 * @param {number} milliseconds - Elapsed time.
 * @returns {string} Readout text.
 */
function deviceAgeReadout(milliseconds) {
    if (!Number.isFinite(milliseconds)) return "never";
    const seconds = Math.max(0, Math.floor(milliseconds / 1000));
    if (seconds < 3600) {
        return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    }
    if (seconds < 86400) {
        return `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")} h`;
    }
    return `${Math.floor(seconds / 86400)} d`;
}

/**
 * Create a text-only element.
 *
 * @param {string} tag - Element name.
 * @param {string} className - Class attribute.
 * @param {*} value - Text content.
 * @returns {HTMLElement} The element.
 */
function deviceText(tag, className, value) {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = String(value);
    return element;
}

/**
 * Readable category label without changing identity.
 *
 * @param {string} value - API category or class name.
 * @returns {string} Label.
 */
function deviceCategory(value) {
    return String(value || "equipment").replaceAll("_", " ");
}

/**
 * Out-of-band management (a BMC) on a device, inferred (owner): when a host
 * source reports the device's own NICs (Proxmox, which never reports a BMC),
 * a port that only a controller's client record adds is the management
 * interface. Records matched to host NICs add no port, so they do not count.
 *
 * @param {object} node - Device record with `ports` and `observations`.
 * @returns {{ports: Set<string>, observations: object[]}} Management port
 *     names and the client records that give them.
 */
function deviceManagement(node) {
    const observations = node?.observations || [];
    const clientIds = new Set(observations.filter((item) => item.client).map((item) => item.id));
    const ports = node?.ports || [];
    const hostReported = ports.some((port) => !clientIds.has(port.observation_id));
    if (!hostReported) return { ports: new Set(), observations: [] };
    const managed = ports.filter((port) => clientIds.has(port.observation_id));
    const ids = new Set(managed.map((port) => port.observation_id));
    return {
        ports: new Set(managed.map((port) => port.name)),
        observations: observations.filter((item) => ids.has(item.id)),
    };
}

/**
 * The host's own power state, apart from its BMC: offline when a host
 * source reports it offline, and whether the BMC still answers then.
 *
 * @param {object} node - Device record.
 * @returns {{hostOffline: boolean, bmcReachable: boolean}} State.
 */
function deviceHostPower(node) {
    const management = deviceManagement(node);
    const managedIds = new Set(management.observations.map((item) => item.id));
    const host = (node?.observations || []).filter((item) => !managedIds.has(item.id));
    return {
        hostOffline: host.some((item) => item.status === "offline"),
        bmcReachable: management.observations.some((item) => item.status === "online"),
    };
}

/**
 * VPN kinds by the controller's type ("openvpn-client", "wireguard-server",
 * "ipsec-vpn"): a readable name and a simple glyph each (not vendor logos).
 * OpenVPN is a padlock (TLS), WireGuard a key, IPsec a shield, and
 * anything else a tunnel.
 */
const DEVICE_VPN_KINDS = [
    { match: "openvpn", name: "OpenVPN", glyph: "M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5zM12 15v3" },
    {
        match: "wireguard", name: "WireGuard",
        glyph: "M14 10a4 4 0 1 0-3.5 4L7 17.5V20h2.5v-2h2v-2h2l.5-.5M15.5 8.5h.01"
    },
    { match: "ipsec", name: "IPsec", glyph: "M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6zM9 12l2 2 4-4" },
    { match: "l2tp", name: "L2TP", glyph: "M3 20V12a9 9 0 0 1 18 0v8M7 20v-8a5 5 0 0 1 10 0v8" },
    { match: "pptp", name: "PPTP", glyph: "M3 20V12a9 9 0 0 1 18 0v8M7 20v-8a5 5 0 0 1 10 0v8" },
];

/**
 * Describe a VPN connection's type for people: just its kind, the first
 * half of the controller's type ("openvpn-client" reads "OpenVPN"; no
 * "-client", "-server", or "-vpn", owner).
 *
 * @param {string} protocol - Controller type, such as "openvpn-client".
 * @returns {{name: string, glyph: string}} "OpenVPN" and its glyph.
 */
function deviceVpnKind(protocol) {
    const text = String(protocol || "").toLowerCase();
    const kind = DEVICE_VPN_KINDS.find((item) => text.includes(item.match));
    return kind
        ? { name: kind.name, glyph: kind.glyph }
        : { name: String(protocol || "VPN").split("-")[0] || "VPN",
            glyph: "M3 20V12a9 9 0 0 1 18 0v8M7 20v-8a5 5 0 0 1 10 0v8" };
}

/**
 * Short source name for people: the part before the colon, as in the
 * header's source strip ("unifi:default" reads "unifi").
 *
 * @param {string} source - Observation source.
 * @returns {string} Short name.
 */
function deviceSourceName(source) {
    // A derived source names its parent last ("Clients 2 · unifi:default").
    const text = String(source || "").split(" · ").at(-1);
    return text.split(":")[0] || text;
}

/**
 * Distinct, non-empty values in first-seen order.
 *
 * @param {Array<*>} values - Candidate values.
 * @returns {string[]} Distinct strings.
 */
function deviceDistinct(values) {
    return [...new Set(values.filter((value) => value !== undefined && value !== null && value !== "")
        .map((value) => String(value)))];
}

/**
 * Name an icon tool: its accessible name, title, and tooltip together, as
 * the tool button rules ask (planning/STYLEGUIDE.md).
 *
 * @param {HTMLElement} button - Icon tool.
 * @param {string} label - Its name.
 * @returns {void}
 */
function deviceToolLabel(button, label) {
    button.setAttribute("aria-label", label);
    button.title = label;
    button.dataset.tooltip = label;
}

/** Details, rename, and neighbor-match panels for one page's drawer. */
class DeviceDetails {
    /**
     * Bind the shared panels' controls once.
     *
     * @param {object} options - Page integration.
     * @param {DeviceDrawer} options.drawer - The page's shared drawer.
     * @param {string} options.csrfToken - Session CSRF token for writes.
     * @param {() => object[]} options.nodes - Current device records
     *     (topology API nodes, plus inventory-only devices where the page has
     *     them).
     * @param {() => object[]} options.links - Current physical links.
     * @param {() => object[]} options.neighborMatches - Saved neighbor matches.
     * @param {() => Promise<void>} options.reload - Reload the page's data
     *     after a write; it should call `render` for the selected device.
     * @param {(message: string, ok?: boolean) => void} options.notify - Show
     *     a page notice.
     * @param {(node: object) => void} options.onPlace - Open placement for
     *     the device (the details footer's main button).
     * @param {() => object|null} [options.locations] - Latest locations API
     *     document, used to name the rack behind a rack-top or shared-shelf
     *     placement.
     * @param {boolean} [options.ignorable] - Offer Ignore device for an
     *     observation not in inventory (Discovery and its clients view).
     */
    constructor({ drawer, csrfToken, nodes, links, neighborMatches, reload, notify, onPlace,
        locations = () => null, ignorable = false }) {
        this.drawer = drawer;
        this.csrfToken = csrfToken;
        this.nodes = nodes;
        this.links = links;
        this.neighborMatches = neighborMatches;
        this.reload = reload;
        this.notify = notify;
        this.onPlace = onPlace;
        this.locations = locations;
        this.ignorable = ignorable;
        this.node = null;
        this.networkTab = "physical";
        this.neighborDraft = null;
        this.cables = new Map();
        this.bindEvents();
    }

    /** @param {string} id - Element ID. @returns {HTMLElement} Required element. */
    element(id) {
        const found = document.getElementById(id);
        if (!found) throw new Error(`Device drawer element is missing: ${id}`);
        return found;
    }

    /** Attach the shared panels' buttons and forms. */
    bindEvents() {
        this.element("device-details-place").addEventListener("click", () => {
            if (this.node) this.onPlace(this.node);
        });
        this.element("device-details-rename").addEventListener("click", () => this.openRename());
        this.element("device-details-ignore").addEventListener("click", () => this.ignoreDevice());
        this.element("device-rename-form").addEventListener("submit", (event) => this.saveName(event));
        this.element("device-rename-cancel").addEventListener("click", () => this.cancelRename());
        this.element("device-neighbor-form").addEventListener("submit", (event) => this.saveNeighborMatch(event));
        this.element("device-neighbor-cancel").addEventListener("click", () => this.drawer.back());
        this.element("device-neighbor-server").addEventListener("change", () => this.updateNeighborPorts());
    }

    // ------------------------------------------------------------------
    // Cables and port state (also used by Equipment's port lamps)
    // ------------------------------------------------------------------

    /** Rebuild the port-to-cable index after the page loads new data. */
    index() {
        const names = new Map(this.nodes().map((node) => [node.id, node.name]));
        this.cables = new Map();
        for (const link of this.links()) {
            const ends = [
                [link.from, link.from_port, link.to, link.to_port], [link.to, link.to_port, link.from, link.from_port]
            ];
            for (const [nodeId, port, otherId, otherPort] of ends) {
                this.cables.set(`${nodeId}::${port}`, {
                    link, otherId, otherPort, otherName: names.get(otherId) || "another device",
                });
            }
        }
    }

    /**
     * Return the cable plugged into one port, if a link was observed.
     *
     * @param {string} nodeId - Device node ID.
     * @param {string} portName - Port name on that device.
     * @returns {{link: object, otherId: string, otherPort: string, otherName: string}|undefined}
     */
    cableAt(nodeId, portName) {
        return this.cables.get(`${nodeId}::${portName}`);
    }

    /**
     * Whether a port appears in a trusted discovery link.
     *
     * @param {string} nodeId - The device.
     * @param {string} portName - The port.
     * @returns {boolean} True when a confirmed or inferred link uses it.
     */
    isPortLinked(nodeId, portName) {
        return this.links().some((link) => link.confidence !== "ambiguous" && (
            (link.from === nodeId && link.from_port === portName)
            || (link.to === nodeId && link.to_port === portName)
        ));
    }

    /**
     * Whether a port counts as up: unless explicitly down, a positive speed or
     * a trusted link counts.
     *
     * @param {object} port - The port record.
     * @param {string} nodeId - Its device.
     * @returns {boolean} True when it counts as up.
     */
    portIsUp(port, nodeId) {
        if (port.link_up === false) return false;
        if (port.link_up === true || Number(port.speed_mbps) > 0) return true;
        return this.isPortLinked(nodeId, port.name);
    }

    /**
     * Describe a port's state from negotiated speed and trusted evidence.
     *
     * @param {object} port - The port record.
     * @param {string} nodeId - Its device.
     * @returns {string} "up", "down", the link's confidence and evidence
     *     (such as "inferred connection · lldp"), or "unknown".
     */
    portStatus(port, nodeId) {
        if (port.link_up === false) return "down";
        if (port.link_up === true) return "up";
        if (Number(port.speed_mbps) > 0) return "up";
        const link = this.links().find((item) => (
            (item.from === nodeId && item.from_port === port.name)
            || (item.to === nodeId && item.to_port === port.name)
        ));
        if (link?.confidence === "ambiguous") return `tentative connection · ${link.evidence}`;
        if (link) return `${link.confidence} connection · ${link.evidence}`;
        return "unknown";
    }

    /**
     * What is known about one port's physical link, from the port itself
     * or, when it does not report it (a server NIC seen by Proxmox), from
     * the port at the other end of its cable: the cable medium, the
     * connector (from the medium first: copper is RJ45; DAC and fiber are
     * SFP), and the negotiated speed.
     *
     * @param {object} port - Port record.
     * @param {string} nodeId - Its device's node ID.
     * @returns {{medium: string|null, connector: string, speedMbps: number|null}} Facts.
     */
    portFacts(port, nodeId) {
        const cable = this.cableAt(nodeId, port.name);
        const far = cable
            ? this.nodes().find((node) => node.id === cable.otherId)?.ports
                ?.find((item) => item.name === cable.otherPort) || null
            : null;
        const medium = port.medium || far?.medium || null;
        const known = (type) => (type && type !== "unknown" ? type : null);
        const fromMedium = medium === "copper" ? "rj45"
            : ["dac", "fiber", "fiber_multimode", "fiber_singlemode"].includes(medium) ? "sfp_plus" : null;
        const connector = known(port.connector_type) || fromMedium
            || (known(far?.connector_type) === "combo" ? null : known(far?.connector_type)) || "unknown";
        const up = this.portIsUp(port, nodeId);
        // A down port has no link speed, whatever it last negotiated.
        const speed = !up ? null : Number(port.speed_mbps) > 0 ? Number(port.speed_mbps)
            : Number(far?.speed_mbps) > 0 ? Number(far.speed_mbps) : null;
        return { medium, connector, speedMbps: speed };
    }

    /**
     * Build an accessible connector glyph without inserting SVG markup strings.
     *
     * @param {string|null} connectorType - Port connector type.
     * @param {?string} [label=null] - Text beside the glyph (the drawer passes
     *     the link speed); the connector's name when null.
     * @returns {HTMLElement} Glyph and label.
     */
    portConnectorIcon(connectorType, label = null) {
        const names = {
            rj45: "RJ45", sfp: "SFP", sfp_plus: "SFP+", sfp28: "SFP28",
            combo: "RJ45/SFP+", wifi: "Wi-Fi", usb: "USB", relay: "Relayed reading", unknown: "Port",
        };
        const type = names[connectorType] ? connectorType : "unknown";
        const holder = deviceText("span", `device-port-connector is-${type}`, "");
        holder.title = names[type];
        holder.setAttribute("aria-label", names[type]);
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 28 28");
        svg.setAttribute("aria-hidden", "true");
        const shape = (tag, attributes) => {
            const element = document.createElementNS(svg.namespaceURI, tag);
            for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
            svg.append(element);
        };
        if (type === "rj45") {
            shape("path", { d: "M6 4h16l2 5v13H4V9l2-5Z", class: "device-port-glyph-shell" });
            shape("path", { d: "M9 10v5m3-5v5m4-5v5m3-5v5M8 19h12", class: "device-port-glyph-detail" });
        } else if (type === "sfp" || type === "sfp_plus" || type === "sfp28") {
            shape("rect", { x: "3", y: "6", width: "22", height: "16", rx: "2", class: "device-port-glyph-shell" });
            shape("path", { d: "M7 11h14M8 16h3m3 0h3m3 0h1", class: "device-port-glyph-detail" });
        } else if (type === "wifi") {
            shape("path", { d: "M4 11a14 14 0 0 1 20 0M7.5 14.5a9 9 0 0 1 13 0M11 18a4 4 0 0 1 6 0M14 21.5h.01",
                class: "device-port-glyph-detail" });
        } else if (type === "relay") {
            // Readings passed along: a dotted arrow, like the relayed line.
            shape("path", { d: "M4 14h.01M8 14h.01M12 14h.01M16 14h.01M19 10l4 4-4 4",
                class: "device-port-glyph-detail" });
        } else if (type === "usb") {
            shape("path", { d: "M14 25V5M11 8l3-3 3 3M14 18l-5-3v-3M14 15l5-3V9M8 11h2v2H8zM18.5 8a1 1 0 1 0 0 .01",
                class: "device-port-glyph-detail" });
            shape("circle", { cx: "14", cy: "24", r: "1.6", class: "device-port-glyph-detail" });
        } else {
            // Type unknown: a plain dashed outline, so it is not read as SFP.
            shape("rect", { x: "5", y: "7", width: "18", height: "14", rx: "3",
                class: "device-port-glyph-shell is-unknown" });
        }
        // The label beside the glyph: the link speed when given (the drawer's
        // port cards), else the connector's name.
        const text = label ?? names[type];
        holder.append(svg);
        if (text) holder.append(deviceText("span", "device-port-connector-label", text));
        return holder;
    }

    // ------------------------------------------------------------------
    // Details panel
    // ------------------------------------------------------------------

    /**
     * The inferred Wi-Fi client sample, if any.
     *
     * @param {?object} node - The device record.
     * @returns {object|null} Its fresh UniFi client evidence, or null.
     */
    wirelessFor(node) {
        return node?.observations?.map((item) => item.wireless)
            .find((item) => item && [
                "unifi-connected-client-ip", "unifi-client-attachment"
            ].includes(item.evidence)) || null;
    }

    /**
     * Append one label/value row to a definition list.
     *
     * @param {HTMLElement} list - The definition list.
     * @param {string} label - The term.
     * @param {string} value - Its value, as text.
     * @returns {HTMLElement} The row.
     */
    detailRow(list, label, value) {
        const row = deviceText("div", "device-detail-row", "");
        row.append(deviceText("dt", "", label), deviceText("dd", "", value));
        list.append(row);
        return row;
    }

    /**
     * Append a row whose value may be several: one line with the first,
     * then a "+N" pill, every value in the tooltip and the rest in visually
     * hidden text (the list rules in planning/STYLEGUIDE.md). Nothing is
     * added when there are no values.
     *
     * @param {HTMLElement} list - Definition list.
     * @param {string} label - Row label.
     * @param {string[]} values - Distinct values, the primary first.
     * @returns {void}
     */
    detailRows(list, label, values) {
        if (!values.length) return;
        const row = this.detailRow(list, label, values[0]);
        if (values.length < 2) return;
        const value = row.querySelector("dd");
        value.title = values.join("\n");
        value.append(deviceText("span", "device-more", `+${values.length - 1}`),
            deviceText("span", "visually-hidden", `, also ${values.slice(1).join(", ")}`));
    }

    /**
     * Describe where a device is placed.
     *
     * @param {object} node - Device record.
     * @returns {string} Placement summary.
     */
    placementLabel(node) {
        const place = node.placement;
        if (!place) return "Not placed";
        // Rack tops and shared shelves are named for their rack, when known.
        const containers = this.locations()?.containers || [];
        const parentId = containers.find((item) => item.id === place.container_id)?.parent_id;
        const rackName = containers.find((item) => item.id === parentId)?.name;
        if (place.container_kind === "rack_top") return rackName ? `On top of ${rackName}` : "On top of a rack";
        if (place.container_kind === "rack_shelf") {
            return `${rackName ? `${rackName} · ` : ""}U${place.rack_u} shared shelf · ${place.face || "front"}`;
        }
        if (place.rack_u) return `${place.container_name} · U${place.rack_u} · ${(place.face || "front").replace("_", " ")}`;
        return place.container_name || "Placed";
    }

    /**
     * Show a device in the details panel, or a prompt when none is selected.
     *
     * Sets the panel title (shown in the drawer bar) to the device's name and
     * the footer's Place and Rename buttons for it. The page may adjust the
     * Place button afterwards (Equipment disables it without a rack).
     *
     * @param {object|null} node - Selected device record.
     * @returns {void}
     */
    render(node) {
        if (node?.id !== this.node?.id) this.networkTab = null;
        this.node = node;
        const target = this.element("device-details-body");
        target.replaceChildren();
        this.element("device-details-title").textContent = node ? node.name : "Equipment details";
        const place = this.element("device-details-place");
        const rename = this.element("device-details-rename");
        place.disabled = !node;
        // Icon tools in the drawer bar: the label is the name and tooltip.
        deviceToolLabel(place, node?.placement ? "Edit placement" : "Place device");
        rename.disabled = !node?.device_id;
        deviceToolLabel(rename, node?.device_id ? "Rename"
            : "Rename: add this observation to inventory first");
        // Ignore hides an observation outright; inventory items are never hidden.
        const ignore = this.element("device-details-ignore");
        ignore.hidden = !this.ignorable || !node || Boolean(node.device_id);
        ignore.disabled = false;
        if (this.drawer.currentView === "details") this.drawer.updateBar();
        if (!node) {
            target.append(deviceText("p", "device-muted", "Select a device to see its details."));
            return;
        }
        target.append(this.renderSummary(node));
        const hasWifi = Boolean(this.wifiPort(node));
        // Only a client with no port of its own, wired or Wi-Fi, lists how
        // the network sees it; otherwise the Network box says it (owner).
        if (!node.ports?.length && !hasWifi) {
            const presence = this.renderNetworkPresence(node);
            if (presence) target.append(presence);
        }
        const ups = typeof upsReading === "function" ? upsReading(node) : null;
        if (ups) target.append(this.renderUps(ups.reading));
        // A Proxmox node or guest adds its cluster details, drawn with
        // widgets, when the page loads device-cluster.js (it fills in once
        // the cluster API answers and stays hidden for anything else).
        if (window.LabratDeviceCluster) target.append(window.LabratDeviceCluster.section(node));
        // One Network box holds every kind of link as a pill tab: ports
        // (or a gateway's Switch and WAN), Bridges, VLANs, VPN, USB, and
        // relayed readings. No box when there is nothing (owner).
        const network = this.renderNetworkTabs(node, this.networkInterfacesFor(node));
        if (network) target.append(network);
        // A Proxmox node's drives as hot-swap bays, last (owner: under
        // networking, like the rack view's faceplates).
        if (window.LabratDeviceCluster?.drives) target.append(window.LabratDeviceCluster.drives(node));
    }

    /**
     * Every address the device is known to have, with the interface it is
     * on, for the IP tab (owner): bridge and VLAN addresses, client records
     * (on the host NIC cabled to their switch port, the BMC, or Wi-Fi), the
     * Wi-Fi match, and a gateway's WAN uplinks; host-reported management
     * addresses fill in only what those do not cover. One row per address.
     *
     * @param {object} node - Device record.
     * @param {object[]} networkInterfaces - Its bond, bridge, and VLAN records.
     * @returns {Array<{ip: string, iface: string}>} Addresses, first source wins.
     */
    deviceAddresses(node, networkInterfaces) {
        const seen = new Map();
        const add = (ip, iface) => {
            if (!ip) return;
            const bare = String(ip).split("/")[0];
            if (!seen.has(bare)) seen.set(bare, { ip: String(ip), iface });
        };
        for (const item of networkInterfaces) {
            const details = item.details || {};
            add(details.cidr && String(details.cidr).includes("/") ? details.cidr
                : details.address && details.cidr ? `${details.address}/${details.cidr}` : details.address, item.name);
        }
        const management = deviceManagement(node);
        const managedIds = new Set(management.observations.map((item) => item.id));
        for (const observation of node.observations || []) {
            const record = observation.client;
            if (!record?.ip_address) continue;
            let iface = "network card";
            if (managedIds.has(observation.id)) iface = "BMC";
            else if (record.ssid || record.connection === "wireless") iface = "Wi-Fi";
            else if (record.switch_port != null) {
                // The host NIC cabled to the switch port the record reports.
                const link = this.links().find((item) => {
                    const local = item.from === node.id ? "from" : item.to === node.id ? "to" : null;
                    if (!local) return false;
                    const remote = local === "from" ? item.to_port : item.from_port;
                    return String(remote).replace(/^Port\s*/i, "") === String(record.switch_port);
                });
                if (link) iface = link.from === node.id ? link.from_port : link.to_port;
            }
            add(record.ip_address, iface);
        }
        add(this.wifiPort(node) && this.wirelessFor(node)?.ip_address, "Wi-Fi");
        for (const uplink of this.wanGateway(node)?.wan?.uplinks || []) {
            add(uplink.ip, [uplink.name, uplink.port && `port ${uplink.port}`].filter(Boolean).join(" · "));
        }
        for (const observation of node.observations || []) {
            if (managedIds.has(observation.id)) continue;
            for (const ip of observation.identifiers?.management_ips || []) add(ip, "management address");
        }
        return [...seen.values()];
    }

    /**
     * Relayed UPS readings as cards, for the "Other" tab (owner): the UPSes
     * a NUT server reports for (a dummy-ups relay, not a cable), or the
     * server a relayed UPS reports through.
     *
     * @param {object} node - Device record.
     * @returns {object[]} Card entries for `portCard`.
     */
    relayedPorts(node) {
        const relayOf = (item) => item.observations?.map((observation) => observation.ups)
            .find((reading) => reading?.attachment === "relayed");
        const card = (name, reading, verb) => ({ connector: "relay", name, up: true, medium: "relay",
            cable: `${verb}${reading?.driver ? ` · ${reading.driver}` : ""}` });
        const own = relayOf(node);
        if (own) {
            const server = this.nodes().find((item) => item.id === own.server_node_id);
            return [card(server?.name || own.server_name || "NUT server", own, "reports through")];
        }
        return this.nodes().filter((item) => relayOf(item)?.server_node_id === node.id)
            .map((ups) => card(ups.name, relayOf(ups), "reports for"));
    }

    /**
     * The device's Wi-Fi link as a port card's facts, when it is connected
     * (or matched ambiguously); null when never seen on Wi-Fi.
     *
     * @param {object} node - Device record.
     * @returns {object|null} Card entry for `portCard`.
     */
    wifiPort(node) {
        const wireless = this.wirelessFor(node);
        if (!wireless || !["connected", "ambiguous"].includes(wireless.state)) return null;
        const up = wireless.state === "connected" && wireless.fresh !== false;
        return {
            connector: "wifi", name: "Wi-Fi", up, medium: "wifi",
            cable: [
                wireless.ap_name ? `Wi-Fi → ${wireless.ap_name}` : "Wi-Fi", wireless.ip_address
            ].filter(Boolean).join(" · "),
            title: wireless.state === "ambiguous" ? "Ambiguous match" : up ? "Connected" : "Previously seen",
        };
    }

    /**
     * USB links as port cards: a UPS's to the NUT server that reads it, or a
     * NUT server's to each UPS on its USB. Relayed UPSes only report
     * through the server, so they are not listed.
     *
     * @param {object} node - Device record.
     * @returns {object[]} Card entries for `portCard`.
     */
    usbPorts(node) {
        const usbOf = (item) => item.observations?.map((observation) => observation.ups)
            .find((reading) => reading?.attachment === "usb");
        const own = usbOf(node);
        if (own) {
            const server = this.nodes().find((item) => item.id === own.server_node_id);
            return [{ connector: "usb", name: "USB", up: true, medium: "usb",
                cable: `USB → ${server?.name || own.server_name || "NUT server"}` }];
        }
        return this.nodes().filter((item) => usbOf(item)?.server_node_id === node.id)
            .map((ups) => ({ connector: "usb", name: "USB", up: true, medium: "usb", cable: `USB → ${ups.name}` }));
    }

    /**
     * One port card for a link that is not a switch port (Wi-Fi, USB):
     * the same look as physical ports (renderPhysicalNetwork).
     *
     * @param {object} entry - `{connector, name, up, medium, cable, title}`.
     * @returns {HTMLElement} The card.
     */
    portCard(entry) {
        const item = deviceText("li", "device-detail-port", "");
        item.dataset.linked = String(entry.up);
        const icon = this.portConnectorIcon(entry.connector, "");
        icon.title = `${icon.title} · ${entry.title || (entry.up ? "up" : "down")}`;
        const identity = deviceText("span", "device-detail-port-identity", "");
        identity.append(icon, deviceText("span", "device-detail-port-name", entry.name),
            deviceText("span", "visually-hidden", entry.up ? ", up" : ", down"));
        if (entry.cable) {
            const where = deviceText("span", "device-detail-port-cable", entry.cable);
            where.dataset.medium = entry.medium || "unknown";
            identity.append(where);
        }
        item.append(identity);
        return item;
    }

    /**
     * The summary at the top of the details: type, placement, ports facing,
     * and what the network reports about the device (host name, OS, model),
     * each once. No sources, maker, or other reported names (owner). Backend identifiers,
     * sample times, and evidence codes stay out (owner).
     *
     * @param {object} node - Device record.
     * @returns {HTMLElement} Definition list.
     */
    renderSummary(node) {
        const list = deviceText("dl", "device-detail-list device-summary", "");
        // The type is the summary's first row, not a line of its own (owner).
        this.detailRow(list, "Type", deviceCategory(node.category));
        if (!node.device_id) this.detailRow(list, "Inventory", "Not in inventory");
        this.detailRow(list, "Placement", this.placementLabel(node));
        const portFacing = node.port_facing ?? node.placement?.port_facing ?? null;
        const inferredFacing = portFacing
            || (node.placement?.face === "full_depth" ? null : node.placement?.face || null);
        if (inferredFacing) {
            // Taken from the rack side when not set; no "(assumed)" (owner).
            this.detailRow(list, "Ports face", inferredFacing);
        }
        const clients = node.observations.map((item) => item.client).filter(Boolean);
        const pick = (...keys) => deviceDistinct(clients.flatMap((record) => keys.map((key) => record[key])));
        // A Proxmox host is known to run Proxmox VE, whatever a controller's
        // fingerprint guessed (owner). Its client records also include its
        // guests' cards, so their host names and models are not the host's.
        // A host that is off while its BMC answers says so (owner).
        const power = deviceHostPower(node);
        if (power.hostOffline && power.bmcReachable) this.detailRow(list, "Power", "Off · BMC reachable");
        const pveHost = node.observations.some((item) => deviceSourceName(item.source) === "proxmox")
            && !clients.some((record) => record.virtualization);
        if (pveHost) {
            this.detailRow(list, "OS", "linux:pve");
        } else {
            this.detailRows(list, "Host name", pick("hostname"));
            // A controller's catch-all guess ("Others") says nothing.
            this.detailRows(list, "OS", pick("operating_system").filter((os) => !/^others?$/i.test(os)));
            this.detailRows(list, "Model", pick("device_model", "device_family"));
        }
        return list;
    }

    /**
     * Where the network sees this device: one row per network address (MAC),
     * merging the controller's repeated client records for it (a bond's
     * members, a VM's cards), with its address, how it connects, and its
     * network. Replaces the per-observation dumps (owner).
     *
     * @param {object} node - Device record.
     * @returns {HTMLElement|null} The section, or null without client records.
     */
    renderNetworkPresence(node) {
        const records = node.observations.map((item) => item.client).filter(Boolean);
        if (!records.length) return null;
        const byMac = new Map();
        for (const record of records) {
            const key = record.mac_address || record.ip_address || `record-${byMac.size}`;
            if (!byMac.has(key)) byMac.set(key, []);
            byMac.get(key).push(record);
        }
        const section = deviceText("section", "device-client-details", "");
        section.append(deviceText("h4", "", `On the network (${byMac.size})`));
        const list = deviceText("ul", "device-ledger", "");
        for (const [key, group] of byMac) {
            const item = deviceText("li", "device-ledger-row", "");
            const values = (field) => deviceDistinct(group.map((record) => record[field]));
            const head = deviceText("span", "device-ledger-main", "");
            head.append(deviceText("span", "device-ledger-name", group[0].mac_address || key));
            const ips = values("ip_address");
            if (ips.length) head.append(deviceText("span", "device-ledger-value", ips.join(", ")));
            item.append(head);
            const facts = [];
            const wifi = group.find((record) => record.ssid || record.connection === "wireless");
            if (wifi) {
                facts.push(["Wi-Fi", wifi.ssid].filter(Boolean).join(" · "));
                if (wifi.radio || wifi.channel) facts.push([
                    wifi.radio, wifi.channel && `ch ${wifi.channel}`
                ].filter(Boolean).join(" "));
                if (typeof wifi.signal_dbm === "number") facts.push(`${wifi.signal_dbm} dBm`);
                const rate = Math.max(wifi.receive_rate_kbps || 0, wifi.transmit_rate_kbps || 0);
                if (rate) facts.push(labratFormatSpeed(Math.round(rate / 1000)));
            } else {
                const ports = values("switch_port");
                facts.push(ports.length ? `Wired · ${this.switchPortLabel(node, ports)}` : "Wired");
            }
            const networks = deviceDistinct(group.flatMap((record) => [
                record.network_name
                    && (record.vlan ? `${record.network_name} (VLAN ${record.vlan})` : record.network_name),
            ]));
            facts.push(...networks);
            const guest = group.find((record) => record.virtualization)?.virtualization;
            if (guest) facts.push(`${guest.kind === "vm" ? "VM" : "Container"} ${guest.vmid} on ${guest.host}`);
            if (group.every((record) => record.state && record.state !== "connected")) facts.push("not connected now");
            const line = facts.filter(Boolean).join(" · ");
            const detail = deviceText("span", "device-ledger-detail", line);
            detail.title = line;
            item.append(detail);
            list.append(item);
        }
        section.append(list);
        return section;
    }

    /**
     * Name the switch ports a client record reports, with the switch when
     * this device's cable to that port is known ("Core switch · ports 9,
     * 10"); otherwise just the ports.
     *
     * @param {object} node - Device record.
     * @param {string[]} ports - Reported switch port numbers.
     * @returns {string} Readable location.
     */
    switchPortLabel(node, ports) {
        const nodes = this.nodes();
        const switches = deviceDistinct(this.links().filter((link) => link.from === node.id || link.to === node.id)
            .filter((link) => {
                const remote = link.from === node.id ? link.to_port : link.from_port;
                return ports.some((port) => String(remote).replace(/^Port\s*/i, "") === String(port));
            })
            .map((link) => nodes.find((entry) => entry.id === (link.from === node.id ? link.to : link.from))?.name));
        const where = `port${ports.length > 1 ? "s" : ""} ${ports.join(", ")}`;
        return switches.length ? `${switches.join(", ")} · ${where}` : where;
    }

    /**
     * Describe a UPS's latest NUT readings: state, battery, runtime, load,
     * power, voltages, alerts, and where the reading came from.
     *
     * @param {object} reading - `observations[].ups` record.
     * @returns {HTMLElement} The power section.
     */
    renderUps(reading) {
        const section = deviceText("section", "device-ups-details", "");
        const condition = upsCondition(reading);
        section.dataset.level = condition.level;
        section.append(deviceText("h4", "", "Power"));
        const list = deviceText("dl", "device-detail-list", "");
        const value = (number, unit) => (typeof number === "number"
            ? `${Math.round(number * 10) / 10}${unit}`
            : "Not reported");
        const widgets = window.LabratWidgets;
        if (widgets) {
            // Battery, runtime, and load as tiles with meters (widgets.js);
            // the list below keeps the rest. Battery reads amber under half
            // and red under a quarter.
            const charge = reading.battery_charge;
            const load = reading.load_percent;
            section.append(widgets.stats([
                {
                    label: "Battery", value: value(charge, "%"),
                    caption: typeof reading.battery_runtime_s === "number"
                        ? `${upsRuntime(reading.battery_runtime_s)} runtime`
                        : "",
                    meter: typeof charge === "number" ? { total: 100, used: charge, label: `Battery ${value(charge, "%")}`,
                        tone: charge < 25 ? "is-alarm" : charge < 50 ? "is-warning" : "" } : null },
                { label: "Load", value: value(load, "%"),
                    meter: typeof load === "number" ? { total: 100, used: load, label: `Load ${value(load, "%")}`, tone: "auto" } : null },
            ], { hue: "power" }));
            this.detailRow(list, "State", condition.label);
        } else {
            this.detailRow(list, "State", condition.label);
            this.detailRow(list, "Battery", value(reading.battery_charge, "%"));
            this.detailRow(list, "Runtime", upsRuntime(reading.battery_runtime_s));
            this.detailRow(list, "Load", value(reading.load_percent, "%"));
        }
        this.detailRow(list, "Power", typeof reading.real_power_w === "number"
            ? value(reading.real_power_w, " W") : value(reading.apparent_power_va, " VA"));
        this.detailRow(list, "Input / output", `${value(reading.input_voltage, " V")} / ${value(reading.output_voltage, " V")}`);
        if (condition.alerts.length) this.detailRow(list, "Alerts", condition.alerts.join(", "));
        // USB and relayed links show in their own boxes; only others here.
        if (!["usb", "relayed"].includes(reading.attachment)) {
            this.detailRow(list, "Connection", `Via ${reading.server_name || "the NUT server"}`);
        }
        section.append(list);
        if (reading.attachment === "relayed") {
            section.append(deviceText("p", "device-field-help",
                "Relayed data repeats another source; LabRat does not assert a hardware fault or a physical connection from it."));
        }
        return section;
    }

    /**
     * Source-scoped bond, bridge, and VLAN interfaces of a device.
     *
     * @param {object} node - The device record.
     * @returns {object[]} Its logical interfaces.
     */
    networkInterfacesFor(node) {
        return node.observations.flatMap((observation) => observation.network_interfaces || [])
            .filter((item) => ["bond", "bridge", "vlan"].includes(item.kind));
    }

    /**
     * The logical bond that owns one physical NIC.
     *
     * @param {object} port - The physical port.
     * @param {object[]} bonds - The device's bonds.
     * @returns {object|null} The bond, or null.
     */
    bondForPort(port, bonds) {
        return bonds.find((bond) => bond.members?.includes(port.name)) || null;
    }

    /**
     * Physical ports with their cables and bonds, discovered connections, and
     * LLDP neighbor evidence.
     *
     * @param {object} node - Device record.
     * @param {object[]} bonds - The device's bond interfaces.
     * @param {object[]|null} [subset] - Only these ports (a gateway's WAN
     *     or switch ports), without the LLDP neighbor lists; all ports and
     *     the neighbors when omitted.
     * @returns {HTMLElement} The physical network section.
     */
    renderPhysicalNetwork(node, bonds, subset = null) {
        const panel = deviceText("section", "device-network-panel", "");
        const all = subset || node.ports;
        // The bond and connection lists this used to repeat below the ports
        // are gone; the port cards already say it (owner).
        // No "Physical ports (n)" heading: the tab already says it (owner).
        // Port cards (owner): the connector glyph, lit when the port is up
        // and grey when it is down, with the link speed beside it; then the
        // name, bond, and cable. A down port's card is greyed out. Up ports
        // come first. No up/down word, "Speed unknown", bond mode, or
        // evidence codes.
        const ports = deviceText("ul", "device-detail-items device-port-list", "");
        const management = deviceManagement(node);
        const ordered = [...all].sort((left, right) => (
            Number(!this.portIsUp(left, node.id)) - Number(!this.portIsUp(right, node.id))));
        for (const port of ordered.slice(0, LABRAT_PORT_LIST_LIMIT)) {
            const up = this.portIsUp(port, node.id);
            const facts = this.portFacts(port, node.id);
            const item = deviceText("li", "device-detail-port", "");
            item.dataset.linked = String(up);
            const icon = this.portConnectorIcon(facts.connector,
                facts.speedMbps ? labratFormatSpeed(facts.speedMbps) : "");
            icon.title = `${icon.title} · ${up ? "up" : "down"}`;
            const identity = deviceText("span", "device-detail-port-identity", "");
            // "Port 7" reads "7": the card already says it is a port (owner);
            // the full name stays in its tooltip and for screen readers. An
            // inferred management port reads "BMC" with a management tag.
            const managed = management.ports.has(port.name);
            const shown = deviceText("span", "device-detail-port-name",
                managed ? "BMC" : String(port.name).replace(/^Port\s+(?=\S)/i, ""));
            if (shown.textContent !== port.name) {
                shown.title = managed ? `${port.name} · out-of-band management` : port.name;
                shown.setAttribute("aria-label", managed ? `${port.name}, out-of-band management` : port.name);
            }
            identity.append(icon, shown, deviceText("span", "visually-hidden", up ? ", up" : ", down"));
            if (managed) identity.append(deviceText("span", "device-bond-label device-mgmt-label", "management"));
            const bond = this.bondForPort(port, bonds);
            if (bond) identity.append(deviceText("span", "device-bond-label", bond.name));
            const cable = this.cableAt(node.id, port.name);
            if (facts.medium || cable) {
                const where = deviceText("span", "device-detail-port-cable", "");
                where.dataset.medium = facts.medium || "unknown";
                where.textContent = (facts.medium ? deviceMediumLabel(facts.medium) : "cable")
                    + (cable ? ` → ${cable.otherName} · ${cable.otherPort}` : "");
                identity.append(where);
            }
            item.append(identity);
            ports.append(item);
        }
        // A Wi-Fi link is a port too, on the main port list (owner).
        const wifi = !subset || !subset.some((port) => this.wanPortNames(node).has(port.name))
            ? this.wifiPort(node) : null;
        if (wifi) {
            const card = this.portCard(wifi);
            const firstDown = ports.querySelector('[data-linked="false"]');
            if (wifi.up && firstDown) firstDown.before(card);
            else ports.append(card);
        }
        if (!all.length && !wifi) ports.append(deviceText("li", "", "No physical ports recorded."));
        if (all.length > LABRAT_PORT_LIST_LIMIT) {
            ports.append(deviceText("li", "", `And ${all.length - LABRAT_PORT_LIST_LIMIT} more ports.`));
        }
        panel.append(ports);
        if (!subset
            || !subset.some((port) => this.wanPortNames(node).has(port.name))) this.renderNeighbors(node, panel);
        return panel;
    }

    /**
     * The WAN readings for a gateway (/api/v1/wan; wan.js's copy when the
     * page has it, else one request made here, after which the open device
     * redraws).
     *
     * @param {object} node - Device record.
     * @returns {object|null} Its `/api/v1/wan` gateway entry, if any.
     */
    wanGateway(node) {
        if (!["router", "gateway", "firewall"].includes(node?.category)) return null;
        const gateways = window.labratWan?.gateways || this.wanGateways;
        if (!gateways) {
            if (!this.wanLoading) {
                this.wanLoading = true;
                labratJson(LABRAT_API.wan).then((body) => {
                    this.wanGateways = body.gateways || [];
                    if (this.node && [
                        "router", "gateway", "firewall"
                    ].includes(this.node.category)) this.render(this.node);
                }).catch(() => { this.wanGateways = []; });
            }
            return null;
        }
        return gateways.find((gateway) => gateway.node_id === node.id) || null;
    }

    /**
     * A gateway's active VPN tunnels: one ledger row per connected tunnel
     * (name, then protocol dim and italic; traffic and how long it has been
     * up beneath), and open remote-access sessions when there are any.
     *
     * @param {object} vpn - The gateway's `wan.vpn` reading.
     * @returns {HTMLElement} The VPN tab's panel.
     */
    renderVpn(vpn) {
        const panel = deviceText("section", "device-network-panel", "");
        const active = (vpn.connections || []).filter((connection) => connection.state === "connected");
        const list = deviceText("ul", "device-ledger", "");
        const rate = (bps) => (typeof bps === "number"
            ? bps >= 1e6 ? `${(bps / 1e6).toFixed(1)} Mb/s` : `${Math.round(bps / 1e3)} kb/s` : "--");
        for (const connection of active) {
            const item = deviceText("li", "device-ledger-row", "");
            const head = deviceText("span", "device-ledger-main", "");
            // The VPN kind's glyph, then the name, then the kind (owner).
            const kind = deviceVpnKind(connection.protocol);
            const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
            icon.setAttribute("viewBox", "0 0 24 24");
            icon.setAttribute("class", "device-vpn-icon");
            icon.setAttribute("aria-hidden", "true");
            const glyph = document.createElementNS(icon.namespaceURI, "path");
            glyph.setAttribute("d", kind.glyph);
            icon.append(glyph);
            const name = deviceText("span", "device-ledger-name", connection.name);
            name.append(deviceText("span", "device-ledger-on", ` · ${kind.name}`));
            head.title = kind.name;
            head.append(icon, name);
            item.append(head);
            const since = Number(connection.connected_at) > 0
                ? `up ${deviceAgeReadout(Date.now() - Number(connection.connected_at) * 1000)}` : "";
            const line = [
                `↓ ${rate(connection.rx_bps)}`, `↑ ${rate(connection.tx_bps)}`, since
            ].filter(Boolean).join(" · ");
            const detail = deviceText("span", "device-ledger-detail", "");
            detail.title = line;
            // The arrows take the theme's highlight color (owner).
            const arrow = (glyph) => deviceText("span", "device-ledger-arrow", glyph);
            detail.append(arrow("↓"), ` ${rate(connection.rx_bps)} · `, arrow("↑"), ` ${rate(connection.tx_bps)}`,
                since ? ` · ${since}` : "");
            item.append(detail);
            list.append(item);
        }
        if (vpn.remote_access_sessions > 0) {
            const item = deviceText("li", "device-ledger-row", "");
            item.append(deviceText("span", "device-ledger-name", "Remote access"),
                deviceText("span", "device-ledger-detail",
                    `${vpn.remote_access_sessions} session${vpn.remote_access_sessions === 1 ? "" : "s"}`));
            list.append(item);
        }
        if (list.children.length) panel.append(list);
        else panel.append(deviceText("p", "device-muted", "No active VPN tunnels."));
        return panel;
    }

    /**
     * Name a gateway's WAN ports, from the WAN readings' uplink port
     * numbers (/api/v1/wan; wan.js's copy when the page has it, else one
     * request made here, after which the open device redraws).
     *
     * @param {object} node - Device record.
     * @returns {Set<string>} WAN port names; empty for other devices.
     */
    wanPortNames(node) {
        const uplinks = this.wanGateway(node)?.wan?.uplinks || [];
        const numbers = new Set(uplinks.map((uplink) => String(uplink.port)).filter((port) => port
            && port !== "undefined"));
        return new Set((node.ports || []).filter((port) => numbers.has(String(port.remote_id))
            || numbers.has(String(port.name).replace(/^Port\s*/i, ""))).map((port) => port.name));
    }

    /**
     * Switch-side LLDP observations with their source.
     *
     * @param {object} node - The device record.
     * @returns {object[]} Each neighbor, with the source and time it was seen.
     */
    neighborsFor(node) {
        return node.observations.flatMap((observation) => (observation.neighbors || []).map(
            (neighbor) => ({ ...neighbor, source: observation.source, seenAt: observation.last_seen_at }),
        ));
    }

    /**
     * Unresolved LLDP neighbors (with a match action), LACP secondary
     * identities (evidence only), and reversible saved matches.
     *
     * @param {object} node - Device record.
     * @param {HTMLElement} panel - Section to append to.
     * @returns {void}
     */
    renderNeighbors(node, panel) {
        const neighbors = this.neighborsFor(node);
        const unresolved = neighbors.filter((neighbor) => !neighbor.resolved);
        if (unresolved.length) {
            panel.append(deviceText("h4", "", `Unresolved neighbors (${unresolved.length})`));
            const list = deviceText("ul", "device-detail-items device-logical-list", "");
            for (const neighbor of unresolved) {
                const item = deviceText("li", "device-logical-card", "");
                item.append(deviceText("p", "", `${neighbor.local_port_name} → chassis ${neighbor.chassis_id}`));
                item.append(deviceText("p", "device-muted", `Remote port: ${neighbor.port_id || "not reported"}`));
                if (neighbor.management_ips?.length) {
                    item.append(deviceText("p", "device-muted", `Announced IPs: ${neighbor.management_ips.join(", ")}`));
                }
                const match = deviceText("button", "button button-secondary", "Match to server NIC");
                match.type = "button";
                match.disabled = neighbor.occupied || Boolean(neighbor.match_id);
                match.addEventListener("click", () => this.openNeighborMatch(node, neighbor));
                item.append(match);
                if (neighbor.occupied) item.append(deviceText(
                    "p", "device-muted", "This switch port already has a mapped cable."
                ));
                else if (neighbor.match_id) item.append(deviceText(
                    "p", "device-muted", "A saved identity exists. Remove its match below to change it."
                ));
                list.append(item);
            }
            panel.append(list);
        }
        // LACP secondary identities (a bond member's extra LLDP address) are
        // evidence for drawing, not something to act on, so they are not
        // listed (owner).
        const nodes = this.nodes();
        const saved = (this.neighborMatches() || []).filter((match) => (
            match.device_id === node.device_id || neighbors.some((neighbor) => neighbor.match_id === match.id)
        ));
        if (saved.length) {
            panel.append(deviceText("h4", "", "Saved neighbor matches"));
            for (const match of saved) {
                const server = nodes.find((candidate) => candidate.device_id === match.device_id);
                const card = deviceText("article", "device-logical-card", "");
                card.append(deviceText("p", "", `${match.chassis_id} / ${match.port_id || "port not reported"} → ${server?.name || "existing server"} / ${match.target_port_remote_id}`));
                const remove = deviceText("button", "button button-quiet", "Remove neighbor match");
                remove.type = "button";
                remove.addEventListener("click", async () => {
                    try {
                        await labratJson(
                            `${LABRAT_API.neighborMatch(match.id)}/remove`, { payload: {}, csrfToken: this.csrfToken }
                        );
                        await this.reload();
                        this.notify("Neighbor identity match removed.", true);
                    } catch (error) {
                        this.notify(error.message);
                    }
                });
                card.append(remove);
                panel.append(card);
            }
        }
    }

    /**
     * Source-reported logical interfaces of one kind.
     *
     * @param {object} node - Device record.
     * @param {"bridge"|"vlan"} kind - Interface kind.
     * @param {object[]} interfaces - Interfaces of that kind.
     * @returns {HTMLElement} The section.
     */
    renderLogicalNetwork(node, kind, interfaces) {
        const panel = deviceText("section", "device-network-panel", "");
        // The tab names the list, so no heading repeats it (owner).
        const heading = { bridge: "Bridges", vlan: "VLAN interfaces" }[kind];
        if (!interfaces.length) {
            panel.append(deviceText(
                "p", "device-muted", `No ${heading.toLowerCase()} reported by the discovery source.`
            ));
            return panel;
        }
        // One ledger row each: name (and Down when it is), then what it
        // carries and its address. Address methods and other settings are
        // not listed (owner).
        const list = deviceText("ul", "device-ledger", "");
        for (const network of interfaces) {
            const item = deviceText("li", "device-ledger-row", "");
            const head = deviceText("span", "device-ledger-main", "");
            const details = network.details || {};
            const address = details.cidr && details.address && !String(details.cidr).includes("/")
                ? `${details.address}/${details.cidr}` : details.cidr || details.address;
            // "br5 → vlan5" or "vlan100 → enp6s0": the interface it sits on
            // dimmer and italic, then its address on the next line; no
            // gateway (owner). A VLAN whose name hides its ID adds it.
            const on = kind === "bridge" ? network.members || [] : [network.parent_name].filter(Boolean);
            const name = deviceText("span", "device-ledger-name", network.name);
            if (on.length) name.append(deviceText("span", "device-ledger-on", ` → ${on.join(", ")}`));
            head.append(name);
            const facts = [
                kind === "vlan" && network.vlan_id && !String(network.name).includes(String(network.vlan_id))
                    && `VLAN ${network.vlan_id}`,
                address,
            ];
            if (network.active === false) head.append(deviceText("span", "device-port-status", "down"));
            item.append(head);
            const line = facts.filter(Boolean).join(" · ");
            if (line) {
                const detail = deviceText("span", "device-ledger-detail", line);
                detail.title = line;
                item.append(detail);
            }
            list.append(item);
        }
        panel.append(list);
        return panel;
    }

    /**
     * Accessible Physical, Bridges, and VLANs tabs (arrow keys, Home, End).
     *
     * @param {object} node - Device record.
     * @param {object[]} networkInterfaces - The device's logical interfaces.
     * @returns {HTMLElement} Tabs and their panels.
     */
    renderNetworkTabs(node, networkInterfaces) {
        const wrapper = deviceText("div", "device-network-tabs-wrap", "");
        const tabs = deviceText("div", "device-network-tabs", "");
        tabs.setAttribute("role", "tablist");
        tabs.setAttribute("aria-label", `${node.name} network interfaces`);
        const panels = new Map();
        // A gateway splits its ports into Switch and WAN tabs, Switch first
        // and selected (owner); Bridges and VLANs show when it reports any.
        const wan = this.wanPortNames(node);
        const bonds = networkInterfaces.filter((item) => item.kind === "bond");
        // One box, a tab for each kind of link the device has; tabs with
        // nothing in them are left out (owner).
        const views = wan.size
            ? [
                { id: "physical", label: "Switch", ports: node.ports.filter((port) => !wan.has(port.name)) },
                { id: "wan", label: "WAN", ports: node.ports.filter((port) => wan.has(port.name)) },
            ]
            : node.ports?.length || this.wifiPort(node) ? [{ id: "physical", label: "Physical", ports: null }] : [];
        if (networkInterfaces.some((item) => item.kind !== "bond")) {
            views.push({ id: "bridge", label: "Bridges" }, { id: "vlan", label: "VLANs" });
        }
        // Addresses and the interface each is on; none listed, no tab (owner).
        const addresses = this.deviceAddresses(node, networkInterfaces);
        if (addresses.length) views.push({ id: "ip", label: "IP", addresses });
        // A gateway with VPN readings lists its active tunnels (owner).
        const vpn = this.wanGateway(node)?.wan?.vpn;
        if (vpn) views.push({ id: "vpn", label: "VPN" });
        // USB links, then "Other": relayed UPS readings (owner).
        const usb = this.usbPorts(node);
        if (usb.length) views.push({ id: "usb", label: "USB", cards: usb });
        const relayed = this.relayedPorts(node);
        if (relayed.length) views.push({ id: "relay", label: "Other", cards: relayed });
        if (!views.length) return null;
        if (!views.some((view) => view.id === this.networkTab)) this.networkTab = views[0].id;
        for (const view of views) {
            const button = deviceText("button", "device-network-tab", view.label);
            button.type = "button";
            button.id = `device-network-tab-${view.id}`;
            button.setAttribute("role", "tab");
            button.setAttribute("aria-controls", `device-network-panel-${view.id}`);
            button.setAttribute("aria-selected", String(view.id === this.networkTab));
            button.tabIndex = view.id === this.networkTab ? 0 : -1;
            button.addEventListener("click", () => this.setNetworkTab(view.id, tabs, panels));
            button.addEventListener("keydown", (event) => {
                if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                event.preventDefault();
                const buttons = [...tabs.querySelectorAll('[role="tab"]')];
                const index = buttons.indexOf(button);
                const next = event.key === "Home" ? 0
                    : event.key === "End" ? buttons.length - 1
                        : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
                buttons[next].focus();
                buttons[next].click();
            });
            tabs.append(button);
            const panel = view.id === "physical" || view.id === "wan"
                ? this.renderPhysicalNetwork(node, bonds, view.ports)
                : view.id === "vpn" ? this.renderVpn(vpn)
                    : view.cards ? this.renderCards(view.cards)
                        : view.addresses ? this.renderAddresses(view.addresses)
                        : this.renderLogicalNetwork(
                            node, view.id, networkInterfaces.filter((item) => item.kind === view.id)
                        );
            panel.id = `device-network-panel-${view.id}`;
            panel.setAttribute("role", "tabpanel");
            panel.setAttribute("aria-labelledby", button.id);
            panel.tabIndex = 0;
            panel.hidden = view.id !== this.networkTab;
            panels.set(view.id, panel);
            wrapper.append(panel);
        }
        // A header strip, "Network" then the pill tabs, sits flush on the
        // content well below it (owner).
        const head = deviceText("div", "device-network-head", "");
        head.append(deviceText("h4", "", "Network"), tabs);
        wrapper.prepend(head);
        return wrapper;
    }

    /**
     * The IP tab: one ledger row per address, the interface after it dim
     * and italic, like bridges and VLANs.
     *
     * @param {Array<{ip: string, iface: string}>} addresses - From `deviceAddresses`.
     * @returns {HTMLElement} The panel.
     */
    renderAddresses(addresses) {
        const panel = deviceText("section", "device-network-panel", "");
        const list = deviceText("ul", "device-ledger", "");
        for (const { ip, iface } of addresses) {
            const item = deviceText("li", "device-ledger-row", "");
            const head = deviceText("span", "device-ledger-main", "");
            // Just the address: the prefix length says nothing here (owner).
            const name = deviceText("span", "device-ledger-name", String(ip).split("/")[0]);
            name.append(deviceText("span", "device-ledger-on", ` · ${iface}`));
            head.append(name);
            item.append(head);
            list.append(item);
        }
        panel.append(list);
        return panel;
    }

    /**
     * A well of port cards for links that are not switch ports.
     *
     * @param {object[]} cards - Entries for `portCard`.
     * @returns {HTMLElement} The panel.
     */
    renderCards(cards) {
        const panel = deviceText("section", "device-network-panel", "");
        const list = deviceText("ul", "device-detail-items device-port-list", "");
        for (const entry of cards) list.append(this.portCard(entry));
        panel.append(list);
        return panel;
    }

    /**
     * Switch network tabs without rebuilding the rest of the panel.
     *
     * @param {string} id - The tab to show.
     * @param {HTMLElement} tabs - The tab list.
     * @param {HTMLElement} panels - The panels' container.
     */
    setNetworkTab(id, tabs, panels) {
        this.networkTab = id;
        for (const button of tabs.querySelectorAll('[role="tab"]')) {
            const active = button.id === `device-network-tab-${id}`;
            button.setAttribute("aria-selected", String(active));
            button.tabIndex = active ? 0 : -1;
        }
        for (const [panelId, panel] of panels) panel.hidden = panelId !== id;
    }

    // ------------------------------------------------------------------
    // Rename panel
    // ------------------------------------------------------------------

    /**
     * Show feedback beside a form's own actions (empty hides it).
     *
     * @param {string} id - The feedback element's ID.
     * @param {string} message - The message.
     */
    feedback(id, message) {
        const element = this.element(id);
        element.hidden = !message;
        element.textContent = message;
    }

    /** Open the inventory display-name editor for the shown device. */
    openRename() {
        const node = this.node;
        if (!node?.device_id) return;
        this.element("device-rename-name").value = node.name;
        const reported = [...new Set(node.observations.map((item) => item.reported_name).filter(Boolean))];
        this.element("device-rename-reported").textContent = reported.length
            ? `Source reported: ${reported.join(" · ")}` : "Manual inventory item; no source-reported name.";
        this.feedback("device-rename-feedback", "");
        this.drawer.push("rename");
        this.element("device-rename-name").focus();
    }

    /**
     * Ignore the selected device (owner: an Ignore button whose rule
     * discovery honors on later scans): one shared `device` rule per
     * observation, so it stays hidden however often its sources report it.
     * Observations › Ignored items restores it.
     *
     * @returns {Promise<void>} Resolves once saved and reloaded.
     */
    async ignoreDevice() {
        const node = this.node;
        if (!node || node.device_id) return;
        const button = this.element("device-details-ignore");
        button.disabled = true;
        try {
            for (const observation of node.observations) {
                await labratJson(LABRAT_API.observationIgnores, { payload: { target: {
                    condition: "device", source_id: observation.source_id, device_remote_id: observation.remote_id,
                } }, csrfToken: this.csrfToken });
            }
            this.drawer.close();
            await this.reload();
            this.notify(`${node.name} ignored. Restore it from Observations › Ignored items.`, true);
        } catch (error) {
            this.notify(error.message);
        } finally {
            button.disabled = false;
        }
    }

    /** Discard a draft name and return to the details. */
    cancelRename() {
        this.element("device-rename-form").reset();
        this.feedback("device-rename-feedback", "");
        this.drawer.back("device-details-rename");
    }

    /**
     * Save a user-owned display name; discovery keeps its reported name.
     *
     * @param {SubmitEvent} event - The rename form's submit.
     * @returns {Promise<void>} Resolves once saved.
     */
    async saveName(event) {
        event.preventDefault();
        const node = this.node;
        if (!node?.device_id) return;
        try {
            await labratJson(`${LABRAT_API.rackDevice(node.device_id)}/name`, { payload: {
                name: this.element("device-rename-name").value,
            }, csrfToken: this.csrfToken });
            await this.reload();
            this.drawer.back("device-details-rename");
            this.notify("Display name saved.", true);
        } catch (error) {
            this.feedback("device-rename-feedback", error.message);
        }
    }

    // ------------------------------------------------------------------
    // Neighbor-match panel
    // ------------------------------------------------------------------

    /**
     * Open the editor for an explicit neighbor-to-NIC identity choice.
     *
     * @param {object} node - The switch whose port saw the neighbor.
     * @param {object} neighbor - The LLDP neighbor.
     */
    openNeighborMatch(node, neighbor) {
        this.neighborDraft = neighbor;
        this.element("device-neighbor-summary").textContent = `${node.name} / ${neighbor.local_port_name} → chassis ${neighbor.chassis_id} / ${neighbor.port_id || "remote port not reported"}`;
        const selector = this.element("device-neighbor-server");
        selector.replaceChildren(new Option("Choose an existing server", ""));
        for (const server of this.nodes().filter((candidate) => candidate.category === "server"
            && candidate.device_id
            && candidate.ports.length)) {
            selector.append(new Option(server.name, server.id));
        }
        this.feedback("device-neighbor-feedback", selector.options.length > 1
            ? "" : "Link a discovered server to inventory before matching its physical NICs.");
        this.updateNeighborPorts();
        this.drawer.push("neighbor-match");
        selector.focus();
    }

    /** Limit NIC choices to physical observations on the chosen server. */
    updateNeighborPorts() {
        const server = this.nodes().find((node) => node.id === this.element("device-neighbor-server").value);
        const selector = this.element("device-neighbor-port");
        selector.replaceChildren(new Option("Choose a physical NIC", ""));
        for (const port of server?.ports || []) {
            selector.append(new Option(`${port.name} · ${port.source}`, String(port.id)));
        }
    }

    /**
     * Save the reviewed identity choice and return to the details.
     *
     * @param {SubmitEvent} event - The match form's submit.
     * @returns {Promise<void>} Resolves once saved.
     */
    async saveNeighborMatch(event) {
        event.preventDefault();
        try {
            await labratJson(LABRAT_API.neighborMatches, { payload: {
                ...this.neighborDraft,
                target_port_id: Number(this.element("device-neighbor-port").value),
            }, csrfToken: this.csrfToken });
            await this.reload();
            this.drawer.back();
            this.notify("Neighbor matched to the selected server NIC.", true);
        } catch (error) {
            this.feedback("device-neighbor-feedback", error.message);
        }
    }
}

/**
 * Return the icon path for a physical equipment category: the line icons
 * Discovery's map and Equipment's rack-top cards share (24-unit, stroked).
 * Unknown categories get a generic box.
 *
 * @param {string} category - Physical equipment classification.
 * @returns {string} Static SVG path data for the category.
 */
function labratIconPath(category) {
    const paths = {
        server: "M4 3h16v5H4zM4 10h16v5H4zM4 17h16v4H4zM7 5.5h.01M7 12.5h.01M7 19h.01M11 5.5h6M11 12.5h6M11 19h6",
        switch: "M4 7h16v10H4zM7 10h2M11 10h2M15 10h2M7 14h2M11 14h2M15 14h2",
        router: "M4 16h16v5H4zM7 16V9m10 7V9M7 7c2-2 8-2 10 0M9 10c1-1 5-1 6 0",
        firewall: "M12 3 20 6v6c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6zM7 9h10M6 13h12M9 9v4m6-4v4",
        access_point: "M12 17v4m-4 0h8M5 13a10 10 0 0 1 14 0M8 16a6 6 0 0 1 8 0M11 19a2 2 0 0 1 2 0M12 9v4",
        ups: "M8 3h8v3h3v15H5V6h3zM10 11l-1 4h3l-1 4 4-6h-3l1-2z",
        // A cloud: a modem is the way out to the internet (owner).
        modem: "M7 19h10.5a4.5 4.5 0 0 0 .6-8.96A6 6 0 0 0 6.4 9.2 5 5 0 0 0 7 19z",
    };
    return paths[category] || "M12 3 21 8v8l-9 5-9-5V8zM3 8l9 5 9-5M12 13v8";
}

/**
 * Create a small, decorative SVG icon for a physical equipment category.
 *
 * @param {string} category - Physical equipment classification.
 * @param {string} [className] - Class for the icon (each page sizes its own).
 * @returns {SVGSVGElement} Accessible-hidden line icon.
 */
function labratDeviceIcon(category, className = "device-type-icon") {
    const namespace = "http://www.w3.org/2000/svg";
    const icon = document.createElementNS(namespace, "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");
    icon.classList.add(className);
    const path = document.createElementNS(namespace, "path");
    path.setAttribute("d", labratIconPath(category));
    icon.append(path);
    return icon;
}
