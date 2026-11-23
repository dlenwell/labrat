// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
/**
 * @file The Networks page (templates/private/networks.html).
 *
 * Reads /api/v1/network once, using each source's shared `network` form
 * (api/network_shape.py: source-scoped IDs and normalized words, so any
 * router source, UniFi or OPNsense, draws the same way; a picker chooses
 * the router when there are several, and a tab whose section the router
 * does not report says so), and draws one of six tabs: Overview (health,
 * tiles, donuts, DNS sinkholes such as Pi-hole), Zones (a firewall zone
 * graph where a zone shows where it can and cannot route, with every
 * exception; a zone matrix; the rules), Networks (VLANs, subnets, DHCP,
 * DNS), Wi-Fi (SSIDs, bands, signal), Devices (uplink tree, ports, load),
 * and Clients. Drawing helpers come from proxmox-ui.js; subtabs from
 * view-frame.js. Early and full of variants on purpose (owner).
 */
(() => {
    "use strict";

    const { readPreference, writePreference } = window.LabratProxmox;
    const {
        el, svg, bytes, uptime, ago, sum, tally, lamp, tip, meter, severity, statTile, stats, row, panel,
        donut, rate, splitBars, sinkholes, columnGroups,
    } = window.LabratWidgets;
    const {
        accessPoints, channelGroups, sectionState, gatewayView, ruleGroups, readoutState, peerState,
    } = window.LabratNetworkModel;
    const app = document.getElementById("networks-app");
    if (!app) {
        return;
    }
    const summaryLine = document.getElementById("networks-summary");
    const notice = document.getElementById("networks-notice");
    let data = null;
    const params = new URLSearchParams(window.location.search);
    const state = {
        view: app.dataset.view || "overview",
        zoneStyle: params.get("sub") || readPreference("labrat.networks.zoneStyle", "graph"),
        zone: params.get("zone") || null,
        direction: "out",
        clients: params.get("sub") || "all",
        source: params.get("source") || readPreference("labrat.networks.source", null),
    };
    const sourcePicker = document.getElementById("networks-source");

    /** The shared section each tab draws; a source without it shows a note. */
    const TAB_SECTIONS = { zones: "zones", networks: "networks", wifi: "wifi", devices: "devices", clients: "clients" };

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /**
     * The chosen source with a network in the shared form, else the first.
     * @returns {object|null} The source entry.
     */
    function current() {
        const ready = data ? data.sources.filter((source) => source.network) : [];
        return ready.find((source) => String(source.id) === String(state.source)) || ready[0] || null;
    }

    /**
     * A source's name for people: its display name without the kind prefix.
     * @param {object} source Source entry.
     * @returns {string} Such as "Edge firewall".
     */
    function sourceName(source) {
        return (source.network && source.network.site) || String(source.name || "").replace(/^[a-z_]+:/, "");
    }

    /** Offer each source with a network, when there is more than one. */
    function fillSourcePicker() {
        const ready = data.sources.filter((source) => source.network);
        const chosen = current();
        sourcePicker.replaceChildren(...ready.map((source) => el("option", {
            value: String(source.id), text: `${sourceName(source)} (${source.network.source.kind === "opnsense" ? "OPNsense" : "UniFi"})`,
        })));
        if (chosen) {
            sourcePicker.value = String(chosen.id);
        }
        document.getElementById("networks-source-field").hidden = ready.length < 2;
    }

    /**
     * A normalized word as the source reported it, for tooltips.
     * @param {string|null} word Such as "online" or "allow".
     * @returns {string|null} The word in capitals.
     */
    function shout(word) {
        return word ? word.toUpperCase() : word;
    }

    /**
     * A readable Wi-Fi generation from the classic radio protocol.
     * @param {string|null} radio Such as "ax", "ac", "ng", "be".
     * @returns {string} Display name.
     */
    function wifiGeneration(radio) {
        return ({
            be: "Wi-Fi 7", ax: "Wi-Fi 6/6E", ac: "Wi-Fi 5", na: "Wi-Fi 4 (5 GHz)", ng: "Wi-Fi 4 (2.4 GHz)",
            g: "Wi-Fi 3", b: "802.11b"
        })[radio] || (radio || "Unknown");
    }

    /**
     * A device's kind for display.
     * @param {object} device Shared-form device.
     * @returns {string} Gateway, access point, switch, or other.
     */
    function deviceKind(device) {
        return { gateway: "Gateway", access_point: "Access point", switch: "Switch" }[device.kind] || "Other";
    }

    /**
     * Join zone, network, gateway, and device names for lookups.
     * @param {object} snapshot Shared-form network.
     * @returns {object} Maps by ID.
     */
    function indexes(snapshot) {
        const zones = new Map(snapshot.zones.map((zone) => [zone.id, zone]));
        const networks = new Map(snapshot.networks.map((network) => [network.id, network]));
        const devices = new Map(snapshot.devices.map((device) => [device.id, device]));
        const gateways = new Map((snapshot.gateways || []).map((gateway) => [gateway.id, gateway]));
        return { zones, networks, devices, gateways };
    }

    // ------------------------------------------------------------------
    // Overview
    // ------------------------------------------------------------------

    /**
     * Draw the Overview tab.
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source entry.
     */
    function drawOverview(root, source) {
        const snapshot = source.network;
        const clients = snapshot.clients || [];
        const wired = clients.filter((client) => client.connection === "wired");
        const wireless = clients.filter((client) => client.connection === "wireless");
        const online = snapshot.devices.filter((device) => device.state === "online");
        const userPolicies = snapshot.policies.filter((policy) => policy.origin === "user");
        const health = el("div", { class: "net-health" }, (snapshot.health || []).map((item) => {
            const tone = item.status === "ok"
                ? "on"
                : item.status === "warning"
                ? "warn"
                : item.status === "error"
                ? "alarm"
                : "off";
            const chip = el("span", { class: `net-health-chip is-${tone}`, tabindex: 0 }, [
                lamp(tone, item.status || "unknown"), el("strong", { text: (item.subsystem || "?").toUpperCase() }),
                el(
                    "span",
                    {
                        class: "cluster-muted",
                        text: item.users !== null
                            && item.users !== undefined
                            ? `${item.users} users`
                            : item.status
                            || ""
                    }
                ),
            ]);
            tip(chip, [
                `${item.subsystem}: ${item.status}`,
                item.adopted !== null
                    && item.adopted !== undefined
                    ? `${item.adopted} adopted, ${item.disconnected || 0} disconnected`
                    : "",
                item.tx_bps !== null ? `${rate(item.tx_bps)} out · ${rate(item.rx_bps)} in` : ""].filter(Boolean));
            return chip;
        }));
        const tiles = stats([
            statTile({
                label: "Clients", value: String(clients.length),
                caption: `${wired.length} wired · ${wireless.length} Wi-Fi`, hue: "network",
                meter: {
                    total: clients.length, used: wired.length, label: `${wired.length} wired of ${clients.length}`
                } }),
            statTile({ label: "Devices online", value: `${online.length} of ${snapshot.devices.length}`, hue: "network",
                caption: `${snapshot.devices.filter((device) => device.updatable).length} firmware updates`,
                meter: {
                    total: snapshot.devices.length, used: online.length, label: `${online.length} online`,
                    tone: online.length < snapshot.devices.length ? "is-warning" : ""
                } }),
            statTile({
                label: "Networks", value: String(snapshot.networks.length), hue: "network",
                caption: `${new Set(snapshot.networks.map((network) => network.vlan)).size} VLANs`
            }),
            statTile({ label: "Zones", value: String(snapshot.zones.length), hue: "network", caption: `${snapshot.zones.filter((zone) => zone.origin === "user").length} your own` }),
            statTile({
                label: "Firewall policies", value: String(snapshot.policies.length), hue: "network",
                caption: `${userPolicies.length} yours · ${snapshot.policies.length - userPolicies.length} built in`
            }),
            statTile({
                label: "Wi-Fi networks", value: String((snapshot.wifi || []).length), hue: "network",
                caption: `${(snapshot.wifi || []).filter((item) => item.enabled).length} broadcasting`
            }),
        ]);
        const kinds = tally(snapshot.devices, deviceKind);
        const donuts = row([
            panel(
                "Clients by network",
                [donut(
                    tally(clients, (client) => client.network),
                    { label: "Clients by network", centre: String(clients.length), caption: "clients" }
                )],
                { hue: "network" }
            ),
            panel("How clients connect", [donut([
                { label: "Wired", value: wired.length },
                ...tally(wireless, (client) => wifiGeneration(client.radio)),
            ], { label: "Clients by connection", pie: true })], { hue: "network" }),
            panel(
                "Devices",
                [donut(
                    kinds, { label: "Devices by kind", centre: String(snapshot.devices.length), caption: "devices" }
                )],
                { hue: "network" }
            ),
            panel("Policies", [donut([
                { label: "Your allows", value: userPolicies.filter((policy) => policy.action === "allow").length },
                { label: "Your blocks", value: userPolicies.filter((policy) => policy.action !== "allow").length },
                { label: "Built in", value: snapshot.policies.length - userPolicies.length, tone: "muted" },
            ], {
                label: "Firewall policies by origin and action", centre: String(snapshot.policies.length),
                caption: "policies"
            })], { hue: "network" }),
        ]);
        root.replaceChildren(...[
            health, gatewayPanel(snapshot), vpnPanel(snapshot), sinkholePanel(snapshot.sinkholes),
            servicesPanel(snapshot), carpPanel(snapshot), tiles, donuts, topClients(clients),
        ].filter(Boolean));
    }

    /**
     * A readout's header note when it is not current: why, and from when.
     * @param {object} state readoutState's result.
     * @returns {string} Such as "last refresh failed; last known from 5 min ago".
     */
    function staleNote(state) {
        return `${state.note}${state.since ? `; last known from ${ago(state.since)}` : ""}`;
    }

    /**
     * A panel saying a readout could not be read at all (a first failure),
     * so a failed read never looks like an empty one.
     * @param {string} title Panel title.
     * @returns {HTMLElement} The panel.
     */
    function unreadPanel(title) {
        return panel(title, [el("p", { class: "cluster-empty", text: "This could not be read." })], { hue: "network" });
    }

    /**
     * The WireGuard peers panel: last handshake, transfer, and keepalive.
     * A reading that is not current is last known: grey lamps, standing as
     * of the reading's own time, and no live count.
     * @param {object} snapshot Shared-form network.
     * @returns {HTMLElement|null} The panel, or null without peers.
     */
    function vpnPanel(snapshot) {
        const peers = snapshot.vpn || [];
        const state = readoutState(snapshot, "vpn");
        if (!peers.length) {
            return state.unread ? unreadPanel("WireGuard") : null;
        }
        const at = state.current ? Date.now() : Date.parse(state.since || "") || Date.now();
        const table = el("table", { class: "cluster-list" });
        table.append(el("thead", {}, [el("tr", {}, ["Peer", "Endpoint", "Last handshake", "Transfer", "Keepalive"]
            .map((name) => el("th", { scope: "col", text: name })))]));
        table.append(el("tbody", {}, peers.map((peer) => {
            const standing = peerState(peer, at);
            return el("tr", {}, [
                el("td", { class: "cluster-list-name" }, [lamp(state.current ? standing.tone : "off",
                    `${state.current ? "" : "last known: "}${standing.label}`), el("strong", { text: peer.name || "unnamed" })]),
                el("td", { class: "cluster-list-ip", text: peer.endpoint || "—" }),
                el("td", { text: peer.handshake ? ago(new Date(peer.handshake * 1000).toISOString()) : "never" }),
                el("td", { text: `${bytes(peer.rx_bytes)} in · ${bytes(peer.tx_bytes)} out` }),
                el("td", { text: peer.keepalive ? `${peer.keepalive} s` : "off" }),
            ]);
        })));
        return panel("WireGuard", [el("div", { class: "cluster-list-scroll", tabindex: 0 }, [table])], {
            hue: "network", aside: state.current
                ? `${peers.filter((peer) => peerState(peer, at).key === "recent").length} of ${peers.length} recently connected`
                : staleNote(state),
        });
    }

    /**
     * The services panel: which of the router's services run (last known,
     * without warnings, when the reading is not current).
     * @param {object} snapshot Shared-form network.
     * @returns {HTMLElement|null} The panel, or null when none are reported.
     */
    function servicesPanel(snapshot) {
        const services = snapshot.services || [];
        const state = readoutState(snapshot, "services");
        if (!services.length) {
            return state.unread ? unreadPanel("Services") : null;
        }
        const stopped = services.filter((service) => !service.running);
        return panel("Services", [el("p", { class: "net-flags" }, services.map((service) => el("span", {
            class: `cluster-pill${service.running || !state.current ? "" : " is-warning"}`,
            text: service.description || service.name,
            title: `${state.current ? "" : "last known: "}${service.running ? "running" : "not running"}`,
        })))], {
            hue: "network",
            aside: !state.current ? staleNote(state) : stopped.length ? `${stopped.length} not running` : `all ${services.length} running`,
        });
    }

    /**
     * The CARP panel: each virtual IP's state on this node, with the IP
     * aliases on its VHID (last known, without warnings, when not current).
     * @param {object} snapshot Shared-form network.
     * @returns {HTMLElement|null} The panel, or null without virtual IPs.
     */
    function carpPanel(snapshot) {
        const carp = snapshot.carp;
        const state = readoutState(snapshot, "carp");
        if (!carp || !(carp.vips || []).length) {
            return state.unread ? unreadPanel("High availability (CARP)") : null;
        }
        return panel("High availability (CARP)", [el("p", { class: "net-flags" }, carp.vips.map((vip) => el("span", {
            class: `cluster-pill${vip.state === "master" || !state.current ? "" : " is-warning"}`,
            text: `${vip.address || vip.interface} · VHID ${vip.vhid ?? "?"} · ${state.current ? "" : "last known "}${vip.state || "unknown"}`,
            title: (vip.aliases || []).length ? `Aliases on this VHID: ${vip.aliases.join(", ")}` : "",
        })))], {
            hue: "network",
            aside: !state.current ? staleNote(state) : carp.maintenance ? "maintenance mode" : `demotion ${carp.demotion ?? 0}`,
        });
    }

    /**
     * The WAN gateways panel, for a source that reports gateway status
     * (OPNsense's dpinger), from gatewayView: state, latency, and loss; an
     * unmonitored gateway shows its status without readings, never zero,
     * and is not counted as up; stale rows are last known and greyed. A
     * first failure says the status could not be read.
     * @param {object} snapshot Shared-form network.
     * @returns {HTMLElement|null} The panel, or null when the source reports
     *     no gateway status.
     */
    function gatewayPanel(snapshot) {
        const view = gatewayView(snapshot, ago);
        const section = (snapshot.sections || {}).gateways;
        if (!view.rows.length) {
            return snapshot.source.kind === "opnsense" && section && section.status === "failed"
                ? panel("Gateways", [el("p", { class: "cluster-empty", text: "Gateway status could not be read." })], { hue: "network" })
                : null;
        }
        const table = el("table", { class: "cluster-list" });
        table.append(el("thead", {}, [el("tr", {}, ["Gateway", "Address", "Interface", "Latency", "Loss"]
            .map((name) => el("th", { scope: "col", text: name })))]));
        table.append(el("tbody", {}, view.rows.map(({ gateway, tone, label, showDefault }) => el("tr", {}, [
            el("td", { class: "cluster-list-name" }, [lamp(tone, label), el("strong", { text: gateway.name }),
                showDefault ? el("span", { class: "cluster-pill", text: "default" }) : null].filter(Boolean)),
            el("td", { class: "cluster-list-ip", text: gateway.address || "—" }),
            el("td", { text: gateway.interface || "—" }),
            el("td", { text: gateway.monitored && typeof gateway.delay_ms === "number" ? `${gateway.delay_ms} ms` : "not monitored" }),
            el("td", { text: gateway.monitored && typeof gateway.loss_pct === "number" ? `${gateway.loss_pct}%` : "—" }),
        ]))));
        return panel("Gateways", [el("div", { class: "cluster-list-scroll", tabindex: 0 }, [table])], { hue: "network", aside: view.aside });
    }

    /**
     * The DNS sinkholes panel: custom DNS servers and likely Pi-holes.
     * @param {Array<object>} items Sinkhole entries.
     * @returns {HTMLElement} The panel.
     */
    function sinkholePanel(items) {
        return panel("DNS sinkholes", [sinkholes(items)],
            {
                hue: "network",
                aside: items.length ? `${items.filter((item) => item.likely_pihole).length} likely Pi-hole` : ""
            });
    }

    /**
     * Clients that moved the most data since they connected.
     * @param {Array<object>} clients Clients.
     * @returns {HTMLElement} The panel.
     */
    function topClients(clients) {
        // The classic API counts from the client's side of the AP: rx is
        // what the client received.
        return panel("Top clients by traffic", [splitBars(clients.map((client) => ({
            label: client.name || client.mac, sub: client.network || "", values: [
                client.rx_bytes || 0, client.tx_bytes || 0
            ],
            tip: [
                client.name || client.mac, `${bytes(client.rx_bytes)} received · ${bytes(client.tx_bytes)} sent`,
                `connected ${uptime(client.uptime)}`
            ],
        })), {
            parts: ["received", "sent"], format: bytes, limit: 10
        })], { hue: "network", aside: "since each connected; received then sent" });
    }

    // ------------------------------------------------------------------
    // Zones
    // ------------------------------------------------------------------

    /**
     * The Zones tab's subtab for a source: a router without verdicts
     * (OPNsense) has Zones and Rules, never a graph or matrix of paths it
     * cannot know; others have Graph, Matrix, and Rules.
     * @param {object} source Source entry.
     * @returns {{verdicts: boolean, styles: Array<string>, style: string}} The choices and the current one.
     */
    function zoneStyle(source) {
        const verdicts = !(source.network.capabilities && source.network.capabilities.verdicts === false);
        const styles = verdicts ? ["graph", "matrix", "rules"] : ["zones", "rules"];
        return { verdicts, styles, style: styles.includes(state.zoneStyle) ? state.zoneStyle : styles[verdicts ? 0 : 1] };
    }

    /**
     * Draw the Zones tab: Graph, Matrix, and Rules subtabs, or, for a router
     * without verdicts, Zones and Rules.
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source entry.
     */
    function drawZones(root, source) {
        const { verdicts, style } = zoneStyle(source);
        const strip = window.LabratViewFrame.placeSubtabs({
            label: "Zone views", tab: app.querySelector('[data-tab="zones"]'), current: style,
            items: verdicts ? [
                { key: "graph", label: "Graph" }, { key: "matrix", label: "Matrix" },
                { key: "rules", label: "Rules", detail: String(source.network.policies.length) }
            ] : [
                { key: "zones", label: "Zones", detail: String(source.network.zones.length) },
                { key: "rules", label: "Rules", detail: String(source.network.policies.length) }
            ],
            onSelect: (key) => { state.zoneStyle = key; writePreference("labrat.networks.zoneStyle", key); render(); },
        });
        let body;
        const shown = (source.network.sections || {})[style === "rules" ? "policies" : "zones"];
        if (shown && shown.status === "not_available") {
            body = el("p", { class: "cluster-empty", text: `${sourceName(source)} does not report this. Choose the other subtab, another router, or another tab.` });
        } else if (!verdicts) {
            body = style === "zones" ? zoneMembers(source) : reportedRules(source);
        } else {
            body = style === "matrix" ? zoneMatrix(source) : style === "rules" ? rulesList(source) : zoneGraph(source);
        }
        // OPNsense evaluates rules in an order the shared form does not
        // model, so it carries no verdicts: say so instead of guessing.
        const note = verdicts ? null : el("p", { class: "cluster-muted", text: "Effective reachability is not available for this router: its rules are listed as reported, in evaluation order, without verdicts." });
        root.replaceChildren(...(strip ? [strip] : []), ...(note ? [note] : []), body);
    }

    /**
     * A verdict-less router's zones: interface groups and lone interfaces,
     * each with its networks (an interface in two groups is in both).
     * @param {object} source Source entry.
     * @returns {HTMLElement} The panel.
     */
    function zoneMembers(source) {
        const snapshot = source.network;
        const { networks } = indexes(snapshot);
        const cards = snapshot.zones.map((zone) => el("article", { class: "cluster-card net-network" }, [
            el("header", { class: "net-network-heading" }, [
                el("h3", { text: zone.name }),
                el("span", { class: "cluster-pill", text: zone.kind === "group" ? "group" : "interface" }),
            ]),
            el("p", { class: "cluster-muted", text: zone.networks.map((id) => (networks.get(id) || {}).name
                || id.split(":").pop()).join(", ") || "no members" }),
        ]));
        return panel("Zones", [el("div", { class: "net-network-grid" }, cards)], { hue: "network", aside: "interface groups and interfaces" });
    }

    /**
     * A verdict-less router's rules, grouped by evaluation stage (ruleGroups).
     * @param {object} source Source entry.
     * @returns {HTMLElement} The panel.
     */
    function reportedRules(source) {
        const groups = ruleGroups(source.network);
        const table = el("table", { class: "cluster-list net-rules" });
        table.append(el("thead", {}, [el("tr", {}, ["Rule", "On", "Direction", "Reported match", "Notes"]
            .map((name) => el("th", { scope: "col", text: name })))]));
        const body = el("tbody");
        for (const group of groups) {
            body.append(el("tr", { class: "net-rules-stage" }, [el("th", { scope: "colgroup", colspan: 5, text: `${group.label} (${group.rules.length})` })]));
            for (const rule of group.rules) {
                body.append(el("tr", { class: rule.enabled ? "" : "is-stopped" }, [
                    el("td", {}, [lamp(rule.enabled ? (rule.action === "pass" ? "on" : "alarm") : "off", rule.action || "unknown"),
                        el("span", { text: ` ${rule.name || "unnamed"}` })]),
                    el("td", { text: (rule.interface_names || []).join(", ") || "every interface" }),
                    el("td", { text: rule.direction || "—" }),
                    el("td", { class: "net-rule-match", text: rule.match, title: rule.match }),
                    el("td", {}, rule.marks.map((mark) => el("span", { class: "cluster-pill is-warning", text: mark }))),
                ]));
            }
        }
        table.append(body);
        const own = groups.filter((group) => group.scope !== "system").reduce((total, group) => total + group.rules.length, 0);
        return panel("Rules", [el("div", { class: "cluster-list-scroll", tabindex: 0 }, [table])], { hue: "network", aside: `${own} yours, in evaluation order` });
    }

    /**
     * Verdict class for a path: allow or block, and whether exceptions
     * change it for some traffic.
     * @param {object|undefined} path Zone path.
     * @returns {string} CSS modifier.
     */
    function pathClass(path) {
        if (!path) {
            return "is-none";
        }
        const flips = path.exceptions.some((item) => item.action !== path.verdict);
        return `is-${path.verdict}${flips ? " has-exceptions" : ""}`;
    }

    /**
     * The zone graph: zones on a ring, each showing its networks; the
     * selected zone's paths drawn to every other zone, allowed solid and
     * blocked dashed, with an amber badge counting exceptions. A side
     * panel lists where it can and cannot route and each exception.
     * @param {object} source Source entry.
     * @returns {HTMLElement} The view.
     */
    function zoneGraph(source) {
        const snapshot = source.network;
        const { networks, gateways } = indexes(snapshot);
        const zones = [...snapshot.zones].sort((a, b) => (b.networks.length - a.networks.length)
            || a.name.localeCompare(b.name));
        if (!state.zone || !zones.some((zone) => zone.id === state.zone)) {
            state.zone = (zones.find((zone) => zone.name === "Internal") || zones[0] || {}).id || null;
        }
        const paths = new Map(source.network.paths.map((path) => [`${path.source}>${path.destination}`, path]));
        const width = 1040;
        const height = 640;
        const cx = width / 2;
        const cy = height / 2;
        const box = { w: 176, h: 72 };
        // An ellipse leaves room for the wide zone boxes at left and right.
        const rx = width / 2 - box.w / 2 - 12;
        const ry = height / 2 - box.h / 2 - 12;
        const position = new Map(zones.map((zone, index) => {
            const angle = (index / zones.length) * 2 * Math.PI - Math.PI / 2;
            return [zone.id, { x: cx + rx * Math.cos(angle), y: cy + ry * Math.sin(angle) }];
        }));
        const chart = svg(
            "svg",
            {
                class: "net-zone-svg", viewBox: `0 0 ${width} ${height}`, role: "group",
                "aria-label": "Firewall zones and the paths between them"
            }
        );
        const defs = svg("defs");
        for (const kind of ["allow", "block", "unknown"]) {
            const marker = svg(
                "marker",
                {
                    id: `net-arrow-${kind}`, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7,
                    orient: "auto-start-reverse"
                }
            );
            marker.append(svg("path", { class: `net-arrow is-${kind}`, d: "M0,0L10,5L0,10Z" }));
            defs.append(marker);
        }
        chart.append(defs);
        const edges = svg("g", { class: "net-edges" });
        const nodes = svg("g", { class: "net-nodes" });
        chart.append(edges, nodes);
        const selected = state.zone;
        const outward = state.direction === "out";
        /**
         * Point on a zone box's edge towards another point.
         * @param {object} from Box centre.
         * @param {object} to Target.
         * @returns {object} Edge point.
         */
        const edgePoint = (from, to) => {
            const dx = to.x - from.x;
            const dy = to.y - from.y;
            const scale = Math.min((box.w / 2 + 6) / Math.abs(dx || 1e-6), (box.h / 2 + 6) / Math.abs(dy || 1e-6));
            return { x: from.x + dx * scale, y: from.y + dy * scale };
        };
        for (const zone of zones) {
            if (!selected || zone.id === selected) {
                continue;
            }
            const path = outward ? paths.get(`${selected}>${zone.id}`) : paths.get(`${zone.id}>${selected}`);
            const a = position.get(outward ? selected : zone.id);
            const b = position.get(outward ? zone.id : selected);
            const p = edgePoint(a, b);
            const q = edgePoint(b, a);
            const mx = (p.x + q.x) / 2 + (q.y - p.y) * 0.12;
            const my = (p.y + q.y) / 2 - (q.x - p.x) * 0.12;
            const verdict = path ? path.verdict : "unknown";
            const line = svg(
                "path",
                {
                    class: `net-edge ${pathClass(path)}`, d: `M${p.x},${p.y}Q${mx},${my} ${q.x},${q.y}`,
                    "marker-end": `url(#net-arrow-${verdict})`, tabindex: 0
                }
            );
            edges.append(line);
            const lines = path ? [
                `${zoneName(snapshot, path.source)} → ${zoneName(snapshot, path.destination)}: ${path.verdict}`,
                path.decided_by ? `by “${path.decided_by}”` : "no broad rule",
                ...path.exceptions.slice(0, 6).map((item) => `${item.action === "allow" ? "allows" : "blocks"}: ${item.name}`)] : ["No policy between these zones"];
            tip(line, lines);
            if (path && path.exceptions.length) {
                const tx = 0.25 * p.x + 0.5 * mx + 0.25 * q.x;
                const ty = 0.25 * p.y + 0.5 * my + 0.25 * q.y;
                const badge = svg("g", { class: `net-badge${path.exceptions.some((item) => item.action !== path.verdict) ? " is-flip" : ""}` });
                badge.append(svg("circle", { cx: tx, cy: ty, r: 11 }));
                const count = svg("text", { x: tx, y: ty + 4, "text-anchor": "middle" });
                count.textContent = String(path.exceptions.length);
                badge.append(count);
                edges.append(badge);
                tip(badge, lines);
            }
        }
        for (const zone of zones) {
            const { x, y } = position.get(zone.id);
            const member = zone.networks.map((id) => networks.get(id) || (gateways.get(id) ? {
                name: gateways.get(id).name, vlan: null
            } : null)).filter(Boolean);
            const self = paths.get(`${zone.id}>${zone.id}`);
            const group = svg("g", { class: `net-zone${zone.id === selected ? " is-selected" : ""}${zone.origin === "user" ? " is-user" : ""}`, tabindex: 0, role: "button", "aria-pressed": String(zone.id === selected), "aria-label": `${zone.name}: ${member.length} networks` });
            group.append(svg("rect", { x: x - box.w / 2, y: y - box.h / 2, width: box.w, height: box.h, rx: 10 }));
            const title = svg("text", { class: "net-zone-name", x: x - box.w / 2 + 12, y: y - box.h / 2 + 22 });
            title.textContent = zone.name;
            group.append(title);
            const list = svg("text", { class: "net-zone-networks", x: x - box.w / 2 + 12, y: y - box.h / 2 + 42 });
            list.textContent = member.length
                ? member.slice(0, 2).map((network) => (network.vlan ? `${network.name} ·${network.vlan}` : network.name)).join(", ")
                : "no networks";
            group.append(list);
            if (member.length > 2) {
                const more = svg("text", { class: "net-zone-networks", x: x - box.w / 2 + 12, y: y - box.h / 2 + 58 });
                more.textContent = `+${member.length - 2} more`;
                group.append(more);
            }
            if (self) {
                const dot = svg(
                    "circle",
                    { class: `net-self ${pathClass(self)}`, cx: x + box.w / 2 - 14, cy: y - box.h / 2 + 16, r: 5 }
                );
                group.append(dot);
            }
            tip(group, [zone.name, `${zone.origin === "user" ? "Your zone" : "Built-in zone"}`, ...member.map((network) => `${network.name}${network.vlan ? ` (VLAN ${network.vlan})` : ""}`),
                self ? `Inside the zone: ${self.verdict}${self.exceptions.length ? `, ${self.exceptions.length} exceptions` : ""}` : ""].filter(Boolean));
            const pick = () => {
                if (state.zone !== zone.id) {
                    state.zone = zone.id;
                    render();
                }
            };
            // Hovering previews a zone's routes; the last one hovered or
            // clicked stays shown.
            group.addEventListener("click", pick);
            group.addEventListener("pointerenter", pick);
            group.addEventListener("keydown", (event) => {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    pick();
                }
            });
            nodes.append(group);
        }
        const toggle = el("button", {
            class: "button button-quiet net-direction", type: "button", "aria-pressed": String(!outward),
            text: outward
                ? "Showing where it can go · show what can reach it"
                : "Showing what can reach it · show where it can go" });
        toggle.addEventListener("click", () => { state.direction = outward ? "in" : "out"; render(); });
        const legend = el("p", { class: "cluster-legend net-legend" }, [
            el("span", { class: "net-key is-allow" }), el("span", { text: "allowed" }),
            el("span", { class: "net-key is-block" }), el("span", { text: "blocked" }),
            el(
                "span", { class: "net-key-badge" }
            ), el("span", { text: "exceptions (amber when some traffic is treated the other way)" }),
        ]);
        return el("div", { class: "net-zone-layout" }, [
            panel(
                "Zones", [toggle, el("div", { class: "net-zone-wrap" }, [chart]), legend],
                { hue: "network", aside: "pick a zone; arrows show the default for all its traffic" }
            ),
            zoneDetail(source, selected, outward),
        ]);
    }

    /**
     * A zone's name by ID.
     * @param {object} snapshot Curated snapshot.
     * @param {string} id Zone ID.
     * @returns {string} Name.
     */
    function zoneName(snapshot, id) {
        return (snapshot.zones.find((zone) => zone.id === id) || { name: "?" }).name;
    }

    /**
     * The selected zone's routes, split into can and cannot, each with its
     * deciding rule and exceptions (who, what ports, which way).
     * @param {object} source Source entry.
     * @param {string} selected Zone ID.
     * @param {boolean} outward Its outgoing paths (true) or incoming.
     * @returns {HTMLElement} The panel.
     */
    function zoneDetail(source, selected, outward) {
        const snapshot = source.network;
        const rows = source.network.paths.filter((path) => (outward ? path.source : path.destination) === selected)
            .sort((a, b) => (a.verdict === b.verdict ? 0 : a.verdict === "allow" ? -1 : 1));
        const describe = (path) => {
            const other = zoneName(snapshot, outward ? path.destination : path.source);
            const label = path.source === path.destination ? `${other} (inside the zone)` : other;
            return el("li", { class: `net-route is-${path.verdict}` }, [
                el("div", { class: "net-route-head" }, [lamp(
                    path.verdict === "allow" ? "on" : path.verdict === "block" ? "alarm" : "off", path.verdict
                ),
                    el(
                        "strong", { text: label }
                    ), el(
                        "span",
                        { class: "cluster-muted", text: path.decided_by ? `by “${path.decided_by}”` : "no broad rule" }
                    )]),
                path.exceptions.length
                    ? el("ul", { class: "net-exceptions" }, path.exceptions.map((item) => el("li", { class: `is-${item.action}` }, [
                    el("span", { class: "net-exception-action", text: item.action === "allow" ? "allows" : "blocks" }),
                    el("span", { class: "net-exception-name", text: item.name || "unnamed rule" }),
                    el("span", { class: "cluster-muted", text: [
                        item.source.length ? `from ${item.negate ? "not " : ""}${item.source.join(", ")}` : "",
                        item.destination.length ? `to ${item.destination.join(", ")}` : "",
                        item.ports.length ? `ports ${item.ports.join(", ")}` : "",
                        item.protocol ? item.protocol : "",
                        item.states ? `${item.states.join("/").toLowerCase()} only` : "",
                    ].filter(Boolean).join(" · ") }),
                ]))) : null,
            ]);
        };
        const can = rows.filter((path) => path.verdict === "allow");
        const cannot = rows.filter((path) => path.verdict !== "allow");
        return panel(`${zoneName(snapshot, selected)} · ${outward ? "can reach" : "reached by"}`, [
            el("h3", { class: "cluster-subheading", text: `${outward ? "Can route to" : "Can be reached from"} (${can.length})` }),
            el("ul", { class: "net-routes" }, can.map(describe)),
            el("h3", { class: "cluster-subheading", text: `${outward ? "Cannot route to" : "Cannot be reached from"} (${cannot.length})` }),
            el("ul", { class: "net-routes" }, cannot.map(describe)),
        ], { hue: "network", className: "net-zone-detail" });
    }

    /**
     * The zone matrix: source zones down, destinations across, each cell
     * the default verdict with its exception count.
     * @param {object} source Source entry.
     * @returns {HTMLElement} The panel.
     */
    function zoneMatrix(source) {
        const zones = source.network.zones;
        const paths = new Map(source.network.paths.map((path) => [`${path.source}>${path.destination}`, path]));
        const table = el("table", { class: "cluster-matrix net-matrix" });
        table.append(el(
            "thead", {},
            [el(
                "tr", {},
                [
                    el("th", { scope: "col", text: "from ↓ to →" }),
                    ...zones.map((zone) => el("th", { scope: "col", class: "cluster-matrix-node", text: zone.name }))
                ]
            )]
        ));
        const body = el("tbody");
        for (const from of zones) {
            const row = el("tr", {}, [el("th", { scope: "row", text: from.name })]);
            for (const to of zones) {
                const path = paths.get(`${from.id}>${to.id}`);
                const cell = el("td", { class: `net-cell ${pathClass(path)}`, tabindex: 0 }, [
                    el(
                        "span",
                        {
                            class: "net-cell-mark",
                            text: path ? (path.verdict === "allow" ? "✓" : path.verdict === "block" ? "✕" : "?") : "·"
                        }
                    ),
                    path && path.exceptions.length ? el(
                        "span", { class: "net-cell-count", text: String(path.exceptions.length) }
                    ) : null,
                ]);
                tip(cell, path ? [
                    `${from.name} → ${to.name}: ${path.verdict}`,
                    path.decided_by ? `by “${path.decided_by}”` : "no broad rule",
                    ...path.exceptions.slice(0, 8).map((item) => `${item.action === "allow" ? "allows" : "blocks"}: ${item.name}`)] : [`${from.name} → ${to.name}`, "No policy"]);
                row.append(cell);
            }
            body.append(row);
        }
        table.append(body);
        return panel("Zone matrix", [el("div", { class: "cluster-matrix-scroll", tabindex: 0 }, [table]),
            el(
                "p",
                {
                    class: "cluster-legend",
                    text: "✓ allowed by default · ✕ blocked by default · the number counts exceptions; amber cells have exceptions that go the other way."
                }
            )],
        { hue: "network", aside: "rows are the source zone" });
    }

    /**
     * Every firewall policy in a sortable ledger, yours first.
     * @param {object} source Source entry.
     * @returns {HTMLElement} The panel.
     */
    function rulesList(source) {
        const snapshot = source.network;
        const rules = [...snapshot.policies].sort((a, b) => ((a.origin === "user") === (b.origin === "user")
            ? (a.order || 0) - (b.order || 0)
            : a.origin === "user"
            ? -1
            : 1));
        const table = el("table", { class: "cluster-list net-rules" });
        table.append(el(
            "thead", {},
            [el(
                "tr", {},
                [
                    "Rule", "From", "To", "Action", "Matches", "Order", "Origin"
                ].map((name) => el("th", { scope: "col", text: name }))
            )]
        ));
        const body = el("tbody");
        for (const rule of rules) {
            const matches = [
                rule.source.values && rule.source.values.length ? `src ${rule.source.negate ? "not " : ""}${rule.source.values.join(", ")}` : "",
                rule.destination.values && rule.destination.values.length ? `dst ${rule.destination.negate ? "not " : ""}${rule.destination.values.join(", ")}` : "",
                (rule.destination.ports || []).length ? `ports ${rule.destination.ports.join(", ")}` : "",
                rule.protocol || "", rule.states ? rule.states.join("/").toLowerCase() : "",
            ].filter(Boolean).join(" · ");
            body.append(el("tr", { class: rule.enabled ? "" : "is-stopped" }, [
                el("td", {}, [lamp(rule.enabled ? (rule.action === "allow" ? "on" : "alarm") : "off", rule.enabled ? shout(rule.action) : "disabled"), el("span", { text: ` ${rule.name || "unnamed"}` })]),
                el(
                    "td", { text: zoneName(snapshot, rule.source.zone) }
                ), el("td", { text: zoneName(snapshot, rule.destination.zone) }),
                el(
                    "td", { text: (rule.action || "").toLowerCase() }
                ), el("td", { class: "net-rule-match", text: matches || "everything", title: matches }),
                el(
                    "td", { class: "is-number", text: String(rule.order ?? "") }
                ), el("td", { text: rule.origin === "user" ? "yours" : "built in" }),
            ]));
        }
        table.append(body);
        return panel("Rules", [el("div", { class: "cluster-list-scroll", tabindex: 0 }, [table])], { hue: "network", aside: `${rules.filter((rule) => rule.origin === "user").length} of yours first` });
    }

    // ------------------------------------------------------------------
    // Networks, Wi-Fi, devices, clients
    // ------------------------------------------------------------------

    /**
     * Draw the Networks tab: one card per network.
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source entry.
     */
    function drawNetworks(root, source) {
        const snapshot = source.network;
        const sinkholes = new Map((snapshot.sinkholes || []).map((item) => [item.ip, item]));
        const ipNumber = (ip) => (ip || "").split(".").reduce((total, part) => total * 256 + Number(part), 0);
        const cards = [...snapshot.networks].sort((a, b) => (a.vlan || 0) - (b.vlan || 0)).map((network) => {
            const clients = snapshot.clients.filter((client) => client.network === network.name);
            const dhcp = network.dhcp || {};
            const size = dhcp.start && dhcp.stop ? ipNumber(dhcp.stop) - ipNumber(dhcp.start) + 1 : null;
            return el("article", { class: "cluster-card net-network" }, [
                el("header", { class: "net-network-heading" }, [
                    el("span", { class: "net-vlan", text: network.vlan ? `VLAN ${network.vlan}` : "no VLAN" }),
                    el("h3", { text: network.name }),
                    el("span", { class: "cluster-pill", text: zoneName(snapshot, network.zone) }),
                ]),
                el(
                    "p",
                    {
                        class: "net-cidr",
                        text: network.cidr || (network.management === "unmanaged" ? "unmanaged" : "no subnet")
                    }
                ),
                size ? el("div", { class: "cluster-capacity-row net-capacity" }, [
                    el("span", { class: "cluster-node-key", text: "Clients" }),
                    meter({
                        total: size, used: clients.length,
                        label: `${clients.length} clients in a ${size}-address DHCP range`
                    }),
                    el("span", { class: "cluster-node-figure", text: `${clients.length}/${size}` }),
                ]) : el("p", { class: "cluster-muted", text: `${clients.length} client${clients.length === 1 ? "" : "s"}` }),
                el("dl", { class: "widget-facts" }, [
                    el("dt", { text: "Gateway" }), el("dd", { text: network.gateway || "—" }),
                    el("dt", { text: "DHCP" }), el("dd", { text: dhcp.mode ? `${dhcp.mode.toLowerCase()}${dhcp.start ? ` ${dhcp.start}–${dhcp.stop}` : ""}${dhcp.serving === false ? " (configured, no leases seen)" : ""}` : "—" }),
                    el(
                        "dt", { text: "DNS" }
                    ), el("dd", {}, (dhcp.dns || []).length ? dhcp.dns.map((server) => el("span", {
                        class: `net-dns${sinkholes.get(server) && sinkholes.get(server).likely_pihole ? " is-pihole" : ""}`,
                        text: sinkholes.get(server) && sinkholes.get(server).names[0] ? `${server} (${sinkholes.get(server).names[0]})` : server })) : [el("span", { text: "gateway" })]),
                    el("dt", { text: "Domain" }), el("dd", { text: dhcp.domain || "—" }),
                ]),
                el("p", { class: "net-flags" }, [
                    network.isolated ? el("span", { class: "cluster-pill is-warning", text: "isolated" }) : null,
                    network.internet === false ? el(
                        "span", { class: "cluster-pill is-warning", text: "no internet" }
                    ) : null,
                    network.mdns ? el("span", { class: "cluster-pill", text: "mDNS" }) : null,
                    network.default ? el("span", { class: "cluster-pill", text: "default" }) : null,
                    network.enabled ? null : el("span", { class: "cluster-pill", text: "disabled" }),
                ]),
            ]);
        });
        root.replaceChildren(panel(
            "Networks", [el("div", { class: "net-network-grid" }, cards)],
            { hue: "network", aside: `${snapshot.networks.length} networks by VLAN` }
        ));
    }

    /**
     * Draw the Wi-Fi tab: SSIDs, bands, generations, signal, satisfaction, APs.
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source entry.
     */
    function drawWifi(root, source) {
        const snapshot = source.network;
        const wireless = snapshot.clients.filter((client) => client.connection === "wireless");
        const aps = accessPoints(snapshot);
        const ssids = el("div", { class: "net-ssids" }, (snapshot.wifi || []).map((item) => {
            const count = wireless.filter((client) => client.essid === item.name).length;
            return el("article", { class: `cluster-card net-ssid${item.enabled ? "" : " is-stopped"}` }, [
                el("header", { class: "net-network-heading" }, [lamp(
                    item.enabled ? "on" : "off", item.enabled ? "Broadcasting" : "Off"
                ), el("h3", { text: item.name }),
                    el("span", { class: "cluster-pill", text: `${count} client${count === 1 ? "" : "s"}` })]),
                el("p", { class: "net-bands" }, (item.bands.length ? item.bands : [null]).map((band) => el("span", { class: `net-band is-${String(band).replace(".", "")}`, text: band ? `${band} GHz` : "bands not reported" }))),
                el("p", { class: "cluster-muted", text: `${(item.security || "").replace(/_/g, " ").toLowerCase()} · ${item.type ? item.type.replace(/_/g, " ").toLowerCase() : ""}${item.devices ? ` · on ${item.devices} APs` : ""}` }),
            ]);
        }));
        const buckets = [
            [-50, "≥ −50 dBm"], [-60, "−50 to −60"], [-67, "−60 to −67"], [-75, "−67 to −75"], [-200, "< −75"]
        ];
        const signal = buckets.map(([floor, label], index) => ({
            label, value: wireless.filter((client) => typeof client.signal === "number"
                && client.signal >= floor
                && (index === 0 || client.signal < buckets[index - 1][0])).length,
            tone: index >= 3 ? (index === 4 ? "alarm" : "warning") : null,
        }));
        const apCards = el("div", { class: "net-network-grid" }, aps.map((device) => {
            const count = snapshot.clients.filter((client) => client.uplink === device.id
                && client.connection === "wireless").length;
            return el("article", { class: `cluster-card net-device${device.state === "online" ? "" : " is-stopped"}` }, [
                el(
                    "header", { class: "net-network-heading" },
                    [
                        lamp(device.state === "online" ? "on" : "alarm", shout(device.state)), el("h3", { text: device.name }),
                        el("span", { class: "cluster-pill", text: `${count} clients` })
                    ]
                ),
                el("p", { class: "cluster-muted", text: device.model || "" }),
                el("p", { class: "net-bands" }, (device.radios || []).map((radio) => el("span", { class: "net-band", text: `${radio.band} GHz${radio.channel ? ` · ch ${radio.channel}` : ""}${radio.width ? ` · ${radio.width} MHz` : ""}` }))),
            ]);
        }));
        root.replaceChildren(
            panel("Wi-Fi networks", [ssids], { hue: "network", aside: `${wireless.length} wireless clients` }),
            row([
                panel(
                    "Clients by SSID",
                    [donut(
                        tally(wireless, (client) => client.essid),
                        { label: "Wireless clients by SSID", centre: String(wireless.length), caption: "on Wi-Fi" }
                    )],
                    { hue: "network" }
                ),
                panel(
                    "Wi-Fi generation",
                    [donut(
                        tally(wireless, (client) => wifiGeneration(client.radio)),
                        { label: "Wireless clients by Wi-Fi generation", pie: true }
                    )],
                    { hue: "network" }
                ),
                panel(
                    "Signal",
                    [donut(
                        signal,
                        {
                            label: "Wireless clients by signal strength",
                            centre: wireless.length
                                ? `${Math.round(sum(wireless, (client) => client.signal || 0) / wireless.length)}`
                                : "—",
                            caption: "avg dBm"
                        }
                    )],
                    { hue: "network", aside: "stronger first" }
                ),
                panel(
                    "Clients by access point",
                    [donut(
                        tally(
                            wireless,
                            (client) => (snapshot.devices.find((device) => device.id === client.uplink)
                                || { name: "unknown" }).name
                        ),
                        { label: "Wireless clients by access point", pie: true }
                    )],
                    { hue: "network" }
                ),
            ]),
            panel("Access points", [apCards], { hue: "network", aside: `${aps.filter((device) => device.state === "online").length} of ${aps.length} online` }),
            neighbourhood(snapshot),
        );
    }

    /**
     * The Wi-Fi neighbourhood: neighbouring networks per channel, one chart
     * per band on one scale, with your own APs' channels lit, so a crowded
     * channel stands out. Only counts are kept; neighbours' names are not.
     * @param {object} snapshot Shared-form network.
     * @returns {HTMLElement} The panel.
     */
    function neighbourhood(snapshot) {
        const rows = snapshot.neighbours || [];
        return panel("Wi-Fi neighbourhood", [rows.length
            ? columnGroups(
                channelGroups(snapshot),
                {
                    label: "Neighbouring networks per channel",
                    note: "Neighbouring networks per channel; brighter columns are channels your own APs use."
                }
            )
            : el("p", { class: "cluster-muted", text: "No neighbouring networks reported." })],
            {
                hue: "network", aside: rows.length ? `${sum(rows, (row) => row.count)} neighbouring networks heard` : ""
            });
    }

    /**
     * Draw the Devices tab: the uplink tree and a card per device with its
     * ports as lamps and its load.
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source entry.
     */
    function drawDevices(root, source) {
        const snapshot = source.network;
        const { devices } = indexes(snapshot);
        const children = new Map();
        for (const device of snapshot.devices) {
            const parent = device.uplink && devices.has(device.uplink) ? device.uplink : "root";
            children.set(parent, (children.get(parent) || []).concat(device));
        }
        const tree = el("ul", { class: "net-tree" });
        const walk = (parent, depth, list) => {
            for (const device of (children.get(parent) || []).sort((a, b) => a.name.localeCompare(b.name))) {
                const clients = snapshot.clients.filter((client) => client.uplink === device.id).length;
                const item = el("li", { class: `net-tree-row depth-${Math.min(depth, 5)}`, tabindex: 0 }, [
                    lamp(device.state === "online" ? "on" : "alarm", shout(device.state)), el("strong", { text: device.name }),
                    el("span", { class: "cluster-muted", text: ` ${deviceKind(device).toLowerCase()} · ${device.model || ""}` }),
                    el("span", { class: "net-tree-clients", text: `${clients} clients` }),
                ]);
                tip(item, [device.name, device.model || "", `${device.ip || ""} · firmware ${device.firmware || "?"}${device.updatable ? " (update available)" : ""}`,
                    device.uptime
                        ? `up ${uptime(device.uptime)}`
                        : "", typeof device.cpu === "number"
                        ? `CPU ${device.cpu}% · memory ${device.memory}%`
                        : ""].filter(Boolean));
                list.append(item);
                walk(device.id, depth + 1, list);
            }
        };
        walk("root", 0, tree);
        const cards = el("div", { class: "net-network-grid" }, snapshot.devices.filter((device) => (device.ports
            || []).length).map((device) => {
            const up = device.ports.filter((port) => port.state === "up").length;
            const strip = el("div", {
                class: "net-ports", role: "img", "aria-label": `${up} of ${device.ports.length} ports up`
            }, device.ports.map((port) => {
                const pip = el("span", { class: `net-port is-${(port.state || "unknown").toLowerCase()} speed-${port.speed >= 10000 ? "10g" : port.speed >= 2500 ? "2g" : port.speed >= 1000 ? "1g" : port.speed ? "slow" : "none"}`, tabindex: 0 });
                tip(pip, [`Port ${port.idx}`, `${shout(port.state) || "unknown"}${port.speed ? ` · ${port.speed >= 1000 ? `${port.speed / 1000} Gb/s` : `${port.speed} Mb/s`}` : ""}`, port.connector || "", port.poe ? `PoE ${port.poe.toLowerCase()}` : ""].filter(Boolean));
                return pip;
            }));
            return el("article", { class: `cluster-card net-device${device.state === "online" ? "" : " is-stopped"}` }, [
                el(
                    "header", { class: "net-network-heading" },
                    [
                        lamp(device.state === "online" ? "on" : "alarm", shout(device.state)), el("h3", { text: device.name }),
                        el("span", { class: "cluster-pill", text: `${up}/${device.ports.length} up` })
                    ]
                ),
                el("p", { class: "cluster-muted", text: `${device.model || ""}${device.updatable ? " · firmware update" : ""}` }),
                strip,
                typeof device.cpu === "number" ? el("div", { class: "cluster-guest-meters" }, [
                    el(
                        "span", { class: "cluster-node-key", text: "CPU" }
                    ), meter({
                        total: 100, used: device.cpu, label: `CPU ${device.cpu}%`, tone: severity(device.cpu / 100)
                    }),
                    el(
                        "span", { class: "cluster-node-key", text: "Mem" }
                    ), meter({
                        total: 100, used: device.memory || 0, label: `Memory ${device.memory}%`,
                        tone: severity((device.memory || 0) / 100)
                    }),
                ]) : null,
                el(
                    "p",
                    {
                        class: "cluster-muted",
                        text: device.uptime
                            ? `up ${uptime(device.uptime)} · uplink ${rate(device.tx_bps)} out, ${rate(device.rx_bps)} in`
                            : "no statistics"
                    }
                ),
            ]);
        }));
        root.replaceChildren(
            row([
                panel("Uplink tree", [tree], { hue: "network", aside: "each device under the one it uplinks to" }),
                panel("Firmware and state", [donut([
                    {
                        label: "Online, current",
                        value: snapshot.devices.filter((device) => device.state === "online"
                            && !device.updatable).length,
                        tone: "ok"
                    },
                    {
                        label: "Online, update available",
                        value: snapshot.devices.filter((device) => device.state === "online"
                            && device.updatable).length,
                        tone: "warning"
                    },
                    {
                        label: "Offline", value: snapshot.devices.filter((device) => device.state !== "online").length,
                        tone: "alarm",
                      detail: snapshot.devices.filter((device) => device.state !== "online").map((device) => device.name).join(", ") },
                ], {
                    label: "Devices by state and firmware", centre: String(snapshot.devices.length), caption: "devices"
                })], { hue: "network" }),
            ]),
            panel("Ports", [cards], { hue: "network", aside: "lamps by link speed: brightest 10 Gb/s" }),
        );
    }

    /**
     * Draw the Clients tab: a ledger with All, Wired, Wi-Fi, and Proxmox
     * guest subtabs.
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source entry.
     */
    function drawClients(root, source) {
        const snapshot = source.network;
        const { devices } = indexes(snapshot);
        const all = snapshot.clients;
        const filters = {
            all: () => true, wired: (client) => client.connection === "wired",
            wifi: (client) => client.connection === "wireless", guests: (client) => Boolean(client.guest),
        };
        if (!(state.clients in filters)) {
            state.clients = "all";
        }
        const strip = window.LabratViewFrame.placeSubtabs({
            label: "Client filters", tab: app.querySelector('[data-tab="clients"]'), current: state.clients,
            items: [{
                key: "all", label: "All", detail: String(all.length)
            }, { key: "wired", label: "Wired", detail: String(all.filter(filters.wired).length) },
                {
                    key: "wifi", label: "Wi-Fi", detail: String(all.filter(filters.wifi).length)
                }, { key: "guests", label: "Proxmox guests", detail: String(all.filter(filters.guests).length) }],
            onSelect: (key) => { state.clients = key; render(); },
        });
        const rows = all.filter(filters[state.clients]).sort((a, b) => (a.network || "").localeCompare(b.network || "")
            || (a.name || "").localeCompare(b.name || ""));
        const table = el("table", { class: "cluster-list" });
        table.append(el(
            "thead", {},
            [el(
                "tr", {},
                [
                    "Client", "Address", "Network", "Connection", "Via", "Quality", "Proxmox guest"
                ].map((name) => el("th", { scope: "col", text: name }))
            )]
        ));
        const body = el("tbody");
        for (const client of rows) {
            const via = devices.get(client.uplink);
            body.append(el("tr", {}, [
                el(
                    "td", { class: "cluster-list-name" },
                    [client.state === "lease" ? lamp("off", "lease only")
                        : lamp("on", client.state === "seen" ? "seen by router" : "connected"),
                    el("strong", { text: client.name || client.hostname || client.mac })]
                ),
                el("td", { class: "cluster-list-ip", text: client.ip || "—" }),
                el("td", { text: client.network || "—" }),
                el("td", { text: client.connection === "other"
                    ? (client.state === "lease" ? "lease only" : "seen by router")
                    : client.connection === "wireless" ? `${client.essid || "Wi-Fi"} · ${wifiGeneration(client.radio)}` : `wired${client.wired_rate ? ` · ${client.wired_rate >= 1000 ? `${client.wired_rate / 1000} Gb/s` : `${client.wired_rate} Mb/s`}` : ""}` }),
                el("td", { text: via ? via.name : "—" }),
                el("td", { class: "cluster-list-meter" }, typeof client.satisfaction === "number" ? [meter({
                    total: 100, used: client.satisfaction, label: `Satisfaction ${client.satisfaction}%`,
                    tone: client.satisfaction < 50 ? "is-alarm" : client.satisfaction < 80 ? "is-warning" : ""
                }),
                    el(
                        "span",
                        { text: client.connection === "wireless"
                            && typeof client.signal === "number"
                            ? `${client.signal} dBm`
                            : `${client.satisfaction}%` }
                    )] : []),
                el("td", { text: client.guest ? `${client.guest.name} on ${client.guest.node}` : "" }),
            ]));
        }
        table.append(body);
        root.replaceChildren(
            ...(strip ? [strip] : []),
            panel(
                "Clients", [el("div", { class: "cluster-list-scroll", tabindex: 0 }, [table])],
                { hue: "network", aside: `${rows.length} shown` }
            )
        );
    }

    // ------------------------------------------------------------------
    // Page wiring
    // ------------------------------------------------------------------

    const drawers = {
        overview: drawOverview, zones: drawZones, networks: drawNetworks, wifi: drawWifi, devices: drawDevices,
        clients: drawClients
    };

    /** Draw the selected tab. */
    function render() {
        for (const panelNode of app.querySelectorAll("[data-view-panel]")) {
            panelNode.hidden = panelNode.dataset.viewPanel !== state.view;
        }
        for (const tab of app.querySelectorAll("[data-tab]")) {
            if (tab.dataset.tab === state.view) {
                tab.setAttribute("aria-current", "page");
            } else {
                tab.removeAttribute("aria-current");
            }
        }
        if (state.view !== "zones" && state.view !== "clients") {
            window.LabratViewFrame.placeSubtabs({ label: "Subtabs", items: [] });
        }
        const source = current();
        if (!source) {
            summaryLine.textContent = data ? "No UniFi network snapshot yet" : "Loading…";
            if (data) {
                const note = el("p", {
                    class: "cluster-empty",
                    text: "The collector has not stored a UniFi network snapshot yet. It refreshes every five minutes."
                });
                // Pi-holes set up in Settings need no UniFi: show them anyway.
                const piholes = state.view === "overview" && (data.piholes || []).length
                    ? sinkholePanel(data.piholes) : null;
                document.getElementById(`networks-view-${state.view}`).replaceChildren(...[note, piholes].filter(Boolean));
            }
            return;
        }
        const snapshot = source.network;
        summaryLine.textContent = `${sourceName(source)} · ${snapshot.networks.length} networks · ${snapshot.clients.length} clients · sampled ${ago(snapshot.sampled_at)}${snapshot.available ? "" : " (stale)"}`;
        // The Rules subtab shows the rules (policies), not the zones.
        const sectionName = state.view === "zones" && zoneStyle(source).style === "rules" ? "policies" : TAB_SECTIONS[state.view];
        const section = (snapshot.sections || {})[sectionName];
        // The Zones tab keeps its subtabs and says so inside the one that
        // is not reported (drawZones), so the other stays reachable.
        if (section && section.status === "not_available" && state.view !== "zones") {
            window.LabratViewFrame.placeSubtabs({ label: "Subtabs", items: [] });
            document.getElementById(`networks-view-${state.view}`).replaceChildren(el("p", {
                class: "cluster-empty", text: `${sourceName(source)} does not report this. Choose another router, or another tab.`,
            }));
            return;
        }
        const root = document.getElementById(`networks-view-${state.view}`);
        drawers[state.view](root, source);
        // A failed refresh keeps the last reading: say so, with its time.
        const freshness = sectionState(section);
        if (section && !freshness.current && section.status !== "not_available") {
            root.prepend(el("p", { class: "view-notice", role: "status", text: freshness.since
                ? `The last refresh of this part failed: showing the reading from ${ago(freshness.since)}.`
                : "This part could not be read." }));
        }
    }

    /**
     * Fetch the data and redraw.
     * @returns {Promise<void>} Resolves after drawing.
     */
    async function load() {
        notice.textContent = "";
        try {
            data = await labratJson(app.dataset.api);
        } catch (error) {
            notice.textContent = "The network data could not be loaded. Try Refresh.";
            return;
        }
        fillSourcePicker();
        render();
    }

    for (const tab of app.querySelectorAll("[data-tab]")) {
        tab.addEventListener("click", (event) => {
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
                return;
            }
            event.preventDefault();
            state.view = tab.dataset.tab;
            window.history.pushState({ view: state.view }, "", tab.href);
            render();
        });
    }
    window.addEventListener("popstate", () => {
        const parts = window.location.pathname.replace(/\/$/, "").split("/");
        state.view = parts[parts.indexOf("networks") + 1] in drawers
            ? parts[parts.indexOf("networks") + 1]
            : "overview";
        render();
    });
    let wide = window.innerWidth > 640;
    window.addEventListener("resize", () => {
        if ((window.innerWidth > 640) !== wide) {
            wide = window.innerWidth > 640;
            render();
        }
    });
    sourcePicker.addEventListener("change", () => {
        state.source = sourcePicker.value;
        writePreference("labrat.networks.source", state.source);
        const url = new URL(window.location.href);
        url.searchParams.set("source", state.source);
        window.history.replaceState(window.history.state, "", url);
        render();
    });
    document.getElementById("networks-refresh").addEventListener("click", load);
    render();
    load();
})();
