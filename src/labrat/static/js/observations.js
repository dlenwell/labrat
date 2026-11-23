/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */

/**
 * Live operations screen backed entirely by cached discovery and WAN APIs.
 * Source state is lab-wide; room filtering only limits devices and gateways.
 * The activity log exists in memory for this visit, never as invented history.
 */

/**
 * Local-storage key for this browser's panel arrangement. It is a viewer
 * preference only: never sent to the server or stored in the database.
 */
const OBSERVATION_LAYOUT_KEY = "labrat.observations.layout";

/** A press on a panel grip becomes a drag after the pointer moves this far. */
const OBSERVATION_DRAG_THRESHOLD = 4;

/** Return a source's age, expected next scan, and overdue state.
 * @param {object} source - Cached source record.
 * @param {number} now - Current epoch milliseconds.
 * @returns {object} Collection timing and health.
 */
function observationSourceState(source, now = Date.now()) {
    const success = Date.parse(source.last_success_at);
    const attempt = Date.parse(source.last_attempt_at);
    const interval = Number(source.poll_interval_seconds) * 1000;
    const age = Number.isFinite(success) ? Math.max(0, now - success) : Infinity;
    const next = interval > 0 && Number.isFinite(attempt) ? attempt + interval - now : null;
    return { age, next, interval, overdue: next === null ? age > 1800000 : next < -(interval + 60000) };
}

/** Preserve the difference between an explicit link state and missing data.
 * @param {object} port - Physical port observation.
 * @returns {string} up, down, or unknown; speed alone never proves a link.
 */
function observationPortState(port) {
    return port.link_up === true ? "up" : port.link_up === false ? "down" : "unknown";
}

/** Classify a server without treating a cached online record as current.
 * @param {object} node - Topology node.
 * @param {number} now - Current epoch milliseconds.
 * @returns {string} offline, stale, online, or unknown.
 */
function observationNodeState(node, now = Date.now()) {
    const observations = node.observations || [];
    const fresh = observations.filter(item => now - Date.parse(item.last_seen_at) <= 3600000);
    if (!fresh.length) return observations.length ? "stale" : "unknown";
    if (fresh.some(item => item.status === "offline")) return "offline";
    return fresh.some(item => item.status === "online") ? "online" : "unknown";
}

/**
 * Describe a router watchlist condition (collector/watchlist.py) for the
 * watchlist: a monitored gateway down, a keepalive VPN peer stale, or a
 * CARP virtual IP in another state than its baseline.
 * @param {object} watch - One served condition (`condition`, `label`,
 *     `state`, `from`, `to`).
 * @returns {{severity: string, title: string, facts: Array<Array<string>>}} Its row.
 */
function routerCondition(watch) {
    if (watch.condition === "router-gateway-down") {
        return { severity: "alarm", title: "Gateway down", facts: [["Gateway", watch.label || watch.item],
            ["Reported", "down (monitored)"]] };
    }
    if (watch.condition === "router-peer-stale") {
        return { severity: "warning", title: "VPN peer stale", facts: [["Peer", watch.label || watch.item],
            ["Reported", "no handshake for over 5 minutes, with keepalive set"]] };
    }
    return { severity: "warning", title: "CARP state changed", facts: [["Virtual IP", watch.label || watch.item],
        ["Change", `${watch.from || "unknown"} → ${watch.to || "unknown"}`]] };
}

/** Tell a passing network client from equipment whose silence matters.
 * Phones, laptops, and guests join and leave all day; the collector keeps
 * the ones it no longer sees, marked not seen. Until one is added to
 * inventory, its coming and going is not a condition or an event.
 * @param {object} node - Topology node.
 * @returns {boolean} Whether the node is a client not in inventory.
 */
function observationPassingClient(node) {
    return Boolean(node.client_only) && !node.device_id;
}

/** Format an elapsed time, preserving never-observed state.
 * @param {number} milliseconds - Age in milliseconds.
 * @returns {string} Compact human-readable duration.
 */
function observationAge(milliseconds) {
    if (!Number.isFinite(milliseconds)) return "never";
    const seconds = Math.max(0, Math.floor(milliseconds / 1000));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds / 60) % 60}m`;
}

/** Extend the existing WAN instruments with whole-lab operations telemetry. */
class ObservationConsole extends LabratWanMonitor {
    /** Keep snapshots and activity in memory; remember only the scope in URL. */
    constructor() {
        super();
        this.data = { nodes: [], links: [], sources: [] };
        this.gateways = [];
        this.events = [];
        this.previous = null;
        this.room = new URL(location.href).searchParams.get("room") || "";
        this.updated = null;
        this.failed = false;
        this.ignores = [];
        this.ignoreRevision = 0;
    }

    /** Bind controls, refresh immediately, and suspend polling in hidden tabs. */
    start() {
        this.admin = document.getElementById("observations-app").dataset.admin === "true";
        this.csrf = document.getElementById("observations-app").dataset.csrf;
        const drawer = document.getElementById("noc-drawer");
        document.getElementById("noc-drawer-close").addEventListener("click", () => this.closeDrawer());
        drawer.addEventListener("cancel", event => {
            event.preventDefault();
            this.closeDrawer();
        });
        // Native dialog backdrop events target the dialog itself. Check the
        // pointer bounds so clicks in drawer padding and drags out of it stay open.
        const outsideDrawer = event => {
            const bounds = drawer.getBoundingClientRect();
            return event.clientX < bounds.left || event.clientX > bounds.right
                || event.clientY < bounds.top || event.clientY > bounds.bottom;
        };
        let startedOutside = false;
        drawer.addEventListener("pointerdown", event => { startedOutside = outsideDrawer(event); });
        drawer.addEventListener("click", event => {
            if (startedOutside && outsideDrawer(event)) this.closeDrawer();
            startedOutside = false;
        });
        document.getElementById("noc-ignored").addEventListener("click", () => this.openIgnored());
        document.getElementById("noc-wallboard").addEventListener(
            "click", () => this.setWallboard(!document.body.classList.contains("noc-wallboard"))
        );
        document.addEventListener("keydown", event => {
            // Escape first cancels a panel drag; only then does it leave wallboard.
            if (event.key === "Escape" && this.drag) {
                this.dragEnd(false);
                return;
            }
            if (event.key === "Escape" && !drawer.open) this.setWallboard(false);
        });
        this.setWallboard(new URL(location.href).searchParams.get("wallboard") === "1", false);
        this.masonryObserver = new ResizeObserver(() => this.layoutMasonry());
        document.querySelectorAll(".noc-panel").forEach(panel => this.masonryObserver.observe(panel));
        this.setupArrange();
        const select = document.getElementById("noc-room");
        select.value = this.room;
        if (select.value !== this.room) this.room = "";
        select.addEventListener("change", () => {
            this.room = select.value;
            const url = new URL(location.href);
            if (this.room) url.searchParams.set("room", this.room);
            else url.searchParams.delete("room");
            history.pushState({}, "", url);
            this.render();
        });
        window.addEventListener("popstate", () => {
            this.room = new URL(location.href).searchParams.get("room") || "";
            select.value = this.room;
            this.setWallboard(new URL(location.href).searchParams.get("wallboard") === "1", false);
            this.render();
        });
        document.getElementById("noc-refresh").addEventListener("click", () => this.refresh());
        const tick = async () => {
            if (!document.hidden) await this.refresh();
            this.timer = setTimeout(tick, 15000);
        };
        document.addEventListener("visibilitychange", () => { if (!document.hidden) this.refresh(); });
        // Source ages tick every second (the source heartbeat panel shows
        // the cache's health; there is no separate live bar, owner).
        setInterval(() => this.renderSources(), 1000);
        tick();
    }

    /** Read both caches atomically for display; retain the last screen on failure. */
    async refresh() {
        if (this.busy || this.writeBusy) return;
        this.busy = true;
        const button = document.getElementById("noc-refresh");
        button.disabled = true;
        try {
            const revision = this.ignoreRevision;
            const [data, wan, policies] = await Promise.all([
                LABRAT_API.topology, LABRAT_API.wan, LABRAT_API.observationIgnores
            ].map(url => labratJson(url, {
                signal: AbortSignal.timeout(10000),
            }).catch(error => {
                throw new Error(error.status === 401 ? "Session expired. Sign in again to resume observations."
                    : "The local cache could not be read. Keeping the last readings.");
            })));
            this.recordChanges(data, wan.gateways || []);
            if (revision === this.ignoreRevision) this.ignores = policies.ignores || [];
            this.data = data;
            this.gateways = wan.gateways || [];
            this.updated = Date.now();
            this.failed = false;
            document.getElementById("noc-error").hidden = true;
            this.render();
        } catch (error) {
            this.failed = true;
            this.render();
            const message = document.getElementById("noc-error");
            message.hidden = false;
            message.textContent = error.message.includes("Session expired")
                ? error.message
                : "Connection interrupted. Last readings are retained; retrying every 15 seconds.";
        } finally {
            this.busy = false;
            button.disabled = false;
        }
    }

    /**
     * Return nodes in the chosen room; unplaced observations remain in Whole lab.
     *
     * @returns {object[]} The nodes in scope.
     */
    scopedNodes() {
        return this.data.nodes.filter(node => !this.room || String(node.placement?.container_room_id) === this.room);
    }

    /**
     * Return only gateways in scope, never leaking another room into its alarms.
     *
     * @returns {object[]} The gateways in scope.
     */
    scopedGateways() {
        const ids = new Set(this.scopedNodes().map(node => node.id));
        return this.gateways.filter(gateway => !this.room || ids.has(gateway.node_id));
    }

    /** Link an observation to its room's Equipment screen, or Discovery if unplaced.
     * @param {object} node - Topology node.
     * @returns {string} Local application path.
     */
    deviceUrl(node) {
        const room = node.placement?.container_room_id;
        return room ? `/areas/${room}` : "/topology";
    }

    /** Serialize an exact rule identity independently of object key order.
     * @param {object} target - Source-scoped condition target.
     * @returns {string} Stable comparison key.
     */
    targetKey(target) {
        return JSON.stringify(Object.fromEntries(Object.entries(target || {}).sort(([a], [b]) => a.localeCompare(b))));
    }

    /** Build one condition per port or neighbor; ignores never mask a whole device.
     * @param {boolean} includeIgnored - Include suppressed conditions for inspection.
     * @returns {Array<object>} Conditions, evidence, and stable policy targets.
     */
    warnings(includeIgnored = false) {
        const rows = [];
        const add = (target, severity, title, detail, facts, node = null) => rows.push({
            id: this.targetKey(target), target, severity, title, detail, facts, node,
        });
        const deviceTarget = (condition, observation) => ({ condition, source_id: observation?.source_id,
            device_remote_id: observation?.remote_id });
        for (const source of this.data.sources) {
            const facts = [["Source", source.name], ["Last attempt", source.last_attempt_at || "Never"],
                ["Last success", source.last_success_at || "Never"], ["Poll interval", `${source.poll_interval_seconds || 'Unknown'} seconds`],
                ["Stored error", source.last_error || "None"]];
            if (source.last_error) add(
                { condition: "source-fault", source_id: source.id }, "warning", "Collection failed", source.name, facts
            );
            else if (observationSourceState(source).overdue) add(
                { condition: "source-overdue", source_id: source.id }, "warning", "Collection overdue", source.name,
                facts
            );
        }
        for (const gateway of this.scopedGateways()) {
            const state = wanStatus(gateway);
            const node = this.data.nodes.find(item => item.id === gateway.node_id);
            const observation = node?.observations.find(item => item.source === gateway.source)
                || node?.observations[0];
            const facts = [[
                "Gateway", gateway.name
            ], ["Source", gateway.source], ["Sampled at", gateway.wan?.sampled_at || "Unknown"],
                ...((gateway.wan?.uplinks || []).map(link => [link.name, `${link.up ? 'Link up' : 'Link down'} · ${link.active ? 'active' : 'standby'} · ${link.isp || 'ISP not reported'}`]))];
            const age = Date.now() - Date.parse(gateway.wan?.sampled_at);
            if (!Number.isFinite(age) || age > 120000) add(
                deviceTarget("wan-stale", observation), "warning", "WAN reading stale", gateway.name, facts, node
            );
            else if (state.state !== "online") add(
                deviceTarget(state.state === "down" ? "wan-down" : "wan-degraded", observation),
                state.state === "down" ? "alarm" : "warning", state.label, gateway.name, facts, node
            );
        }
        for (const node of this.scopedNodes()) {
            if (observationPassingClient(node)) continue;
            const state = observationNodeState(node);
            const observation = node.observations.find(item => item.status === "offline") || node.observations[0];
            const facts = [["Device", node.name], ["Type", node.category], ...node.observations.flatMap(item => [
                [
                    "Source", item.source
                ], ["Reported state", item.status || "Not reported"], ["Last observed", item.last_seen_at || "Unknown"],
            ])];
            if (node.category === "server" && state === "offline") add(
                deviceTarget("server-offline", observation), "alarm", "Server reported offline", node.name, facts, node
            );
            const ups = node.category === "ups" ? upsReading(node) : null;
            if (ups) {
                const { reading, observation: upsObservation } = ups;
                const condition = upsCondition(reading);
                const upsFacts = [["UPS", node.name], [
                    "State", condition.label
                ], ["Reported status", reading.status_raw || "Not reported"],
                    ["Battery charge", reading.battery_charge === null ? "Not reported" : `${reading.battery_charge}%`],
                    [
                        "Runtime", upsRuntime(reading.battery_runtime_s)
                    ], ["Load", reading.load_percent === null ? "Not reported" : `${reading.load_percent}%`],
                    ["Driver", reading.driver || "Not reported"],
                    ["Connection", reading.attachment === "usb" ? `USB on ${reading.server_name || 'the NUT server'}` : reading.attachment === "relayed" ? "Relayed by another source (dummy-ups)" : "Not reported"],
                    [
                        "Read through", reading.server_name || "Unknown NUT server"
                    ], ["Sampled at", reading.sampled_at || "Unknown"]];
                if (reading.state === "low_battery") add(
                    deviceTarget("ups-low-battery", upsObservation), "alarm", "UPS battery low", node.name, upsFacts,
                    node
                );
                else if (reading.state === "on_battery") add(
                    deviceTarget("ups-on-battery", upsObservation), "warning", "UPS on battery", node.name, upsFacts,
                    node
                );
                if (condition.alerts.length) add(deviceTarget("ups-alert", upsObservation), "warning", "UPS reports a problem", `${node.name} · ${condition.alerts.join(', ')}`, upsFacts, node);
            }
            if (state === "stale") add(
                deviceTarget("device-stale", observation), "warning", "Not observed for an hour", node.name, facts, node
            );
            for (const port of node.ports.filter(item => item.link_up === false)) {
                const links = this.data.links.filter(link => link.from === node.id
                    && link.from_port === port.name
                    || link.to === node.id
                    && link.to_port === port.name);
                if (!links.length) continue;
                const observed = node.observations.find(item => item.id === port.observation_id)
                    || node.observations[0];
                const portFacts = [["Device", node.name], ["Physical port", port.name], ["Reported link", "Down"],
                    [
                        "Source", port.source
                    ], [
                        "Last observed", observed?.last_seen_at || "Unknown"
                    ], ["Connector", port.connector_type || "Not reported"]];
                for (const link of links) {
                    const otherId = link.from === node.id ? link.to : link.from;
                    const other = this.data.nodes.find(item => item.id === otherId);
                    portFacts.push(["Observed far end", `${other?.name || 'Unknown device'} / ${link.from === node.id ? link.to_port : link.from_port}`],
                        [
                            "Cable evidence", `${link.evidence} · ${link.confidence}`
                        ], ["Cable last observed", link.last_seen_at || "Unknown"]);
                }
                add(
                    { ...deviceTarget("port-down", observed), port_remote_id: port.remote_id }, "warning",
                    "Cabled port reported down", `${node.name} · ${port.name}`, portFacts, node
                );
            }
            for (const observed of node.observations) {
                for (const watch of observed.watch_conditions || []) {
                    const row = routerCondition(watch);
                    const target = { ...deviceTarget(watch.condition, observed), item: watch.item };
                    // A CARP change is acknowledged per occurrence: a later
                    // change to the same state shows again.
                    if (watch.condition === "router-carp-changed") Object.assign(target, { state: watch.to, since: watch.entered_at });
                    // Last known when its section was not read since, or the
                    // source's polls are failing or overdue.
                    const source = this.data.sources.find(item => item.id === observed.source_id);
                    const lastKnown = watch.sampled_at !== watch.watch_sampled_at
                        || Boolean(source && (source.last_error || observationSourceState(source).overdue));
                    add(target, row.severity, row.title, `${node.name} · ${watch.label || watch.item}${lastKnown ? " · last known" : ""}`, [
                        ["Router", node.name], ["Source", observed.source], ...row.facts,
                        ["Condition since", watch.entered_at || "Unknown"],
                        [lastKnown ? "No data since" : "Last sample", watch.sampled_at || "Unknown"],
                    ], node);
                }
                for (const neighbor of (observed.neighbors || []).filter(item => !item.resolved)) {
                    add({ ...deviceTarget("neighbor-unresolved", observed), port_remote_id: neighbor.local_port,
                        chassis_id: neighbor.chassis_id, remote_port_id: neighbor.port_id }, "advisory", "Unresolved LLDP neighbor", `${node.name} · ${neighbor.local_port_name}`,
                    [[
                        "Source", observed.source
                    ], ["Switch", node.name], ["Local physical port", neighbor.local_port_name],
                        [
                            "Neighbor chassis ID", neighbor.chassis_id || "Not reported"
                        ], ["Neighbor port ID", neighbor.port_id || "Not reported"],
                        [
                            "Management addresses", (neighbor.management_ips || []).join(", ") || "Not reported"
                        ], ["Last observed", observed.last_seen_at || "Unknown"]], node);
                }
            }
        }
        const ignored = new Set(this.ignores.map(rule => this.targetKey(rule.target)));
        return rows.filter(row => includeIgnored || !ignored.has(row.id)).sort((a, b) => [
            "alarm", "warning", "advisory"
        ].indexOf(a.severity) - ["alarm", "warning", "advisory"].indexOf(b.severity));
    }

    /** Redraw data panels after a successful reading or a scope change. */
    render() {
        const nodes = this.scopedNodes();
        const ports = nodes.flatMap(node => node.ports);
        const servers = nodes.filter(node => node.category === "server");
        const ids = new Set(nodes.map(node => node.id));
        const links = this.data.links.filter(link => ids.has(link.from) || ids.has(link.to));
        const warnings = this.warnings();
        const metrics = document.getElementById("noc-metrics");
        metrics.replaceChildren();
        const online = servers.filter(node => observationNodeState(node) === "online").length;
        const uncertain = links.filter(link => link.confidence === "inferred"
            || link.confidence === "ambiguous").length;
        const placed = nodes.filter(node => node.placement).length;
        const portsUp = ports.filter(port => port.link_up === true).length;
        // Each card has a signal hue and a gauge of a real ratio: placed devices,
        // servers online, links with direct evidence, and ports with link.
        // Only the server card changes state: amber when some are not online,
        // red when none are.
        const ratio = (part, whole) => (whole ? part / whole : 0);
        const metricsData = [
            [
                "Devices observed", nodes.length, `${nodes.length - placed} unplaced`, "neutral",
                ratio(placed, nodes.length), null
            ],
            ["Servers online", online, `of ${servers.length} servers in scope`, "compute", ratio(
                online, servers.length
            ),
                !servers.length || online === servers.length ? null : online === 0 ? "alarm" : "warning"],
            ["Physical links", links.length, `${links.filter(link => link.confidence === 'inferred').length} inferred · ${links.filter(link => link.confidence === 'ambiguous').length} tentative`,
                "network", ratio(links.length - uncertain, links.length), null],
            [
                "Ports with link", portsUp, `${ports.length} physical ports observed`, "network",
                ratio(portsUp, ports.length), null
            ],
        ];
        for (const [label, value, detail, signal, fraction, state] of metricsData) {
            const card = wanElement("article", "noc-metric");
            card.dataset.signal = signal;
            if (state) card.dataset.state = state;
            const gauge = wanElement("span", "noc-gauge");
            gauge.setAttribute("aria-hidden", "true");
            const fill = wanElement("span", "noc-gauge-fill");
            fill.style.transform = `scaleX(${Math.min(1, Math.max(0, fraction)).toFixed(4)})`;
            gauge.append(fill);
            card.append(
                wanElement("span", "noc-label", label), wanElement("strong", "", String(value).padStart(2, "0")),
                wanElement("span", "noc-note", detail), gauge
            );
            metrics.append(card);
        }
        document.getElementById("noc-warning-count").textContent = `${warnings.length} condition${warnings.length === 1 ? '' : 's'}`;
        const target = document.getElementById("noc-warnings");
        target.replaceChildren();
        for (const warning of warnings) {
            const row = wanElement("button", "noc-warning");
            row.type = "button";
            row.addEventListener("click", () => this.openWarning(warning));
            row.dataset.severity = warning.severity;
            row.append(
                wanElement(
                    "span", "noc-warning-icon",
                    warning.severity === "alarm" ? "!" : warning.severity === "warning" ? "△" : "i"
                ),
                wanElement("div", "")
            );
            row.lastChild.append(wanElement("strong", "", warning.title), wanElement("span", "", warning.detail));
            target.append(row);
        }
        // A clear watchlist is one calm line; the panel grows when conditions appear.
        if (!warnings.length) {
            target.append(nodes.length
                ? wanElement("p", "noc-empty noc-all-clear", "All clear · no active conditions in this scope")
                : wanElement("p", "noc-empty", "No discovered equipment in this scope yet."));
        }
        this.renderSources();
        this.renderServers(servers);
        this.renderPower(nodes);
        this.renderPorts(nodes);
        this.renderWan();
        this.renderVpn();
        this.renderEvents();
        // An icon tool (owner): the count is a small badge and in its label.
        const ignored = document.getElementById("noc-ignored");
        const label = `Ignored items (${this.ignores.length})`;
        ignored.setAttribute("aria-label", label);
        ignored.title = label;
        let badge = ignored.querySelector(".noc-tool-count");
        if (!badge) {
            badge = document.createElement("span");
            badge.className = "noc-tool-count";
            badge.setAttribute("aria-hidden", "true");
            ignored.append(badge);
        }
        badge.textContent = String(this.ignores.length);
        badge.hidden = this.ignores.length === 0;
        this.layoutMasonry();
    }

    /** Update source ages and scan progress without rebuilding the rest of the page. */
    renderSources() {
        const target = document.getElementById("noc-sources");
        target.replaceChildren();
        for (const source of this.data.sources) {
            const timing = observationSourceState(source);
            const state = source.last_error
                || timing.overdue
                ? "warning"
                : !source.last_success_at
                ? "unknown"
                : "online";
            const card = wanElement("article", "noc-source");
            card.dataset.state = state;
            const title = wanElement("div", "noc-source-title");
            title.append(
                wanElement("strong", "", source.name),
                wanElement(
                    "span", "",
                    source.last_error
                        ? "Collection failed"
                        : !source.last_success_at
                        ? "Never collected"
                        : timing.overdue
                        ? "Overdue"
                        : "Collecting normally"
                )
            );
            const track = wanElement("div", "noc-progress");
            const fill = wanElement("span", "");
            fill.style.width = `${timing.next === null ? 0 : Math.min(100, Math.max(0, 100 * (1 - timing.next / timing.interval)))}%`;
            track.append(fill);
            card.append(title, track, wanElement("p", "noc-note", `Last success ${observationAge(timing.age)} ago · ${timing.next === null ? 'schedule unknown' : timing.next > 0 ? `next scan in ${observationAge(timing.next)}` : 'awaiting collection'}`));
            target.append(card);
        }
        if (!this.data.sources.length) target.append(wanElement("p", "noc-empty", "No discovery sources configured."));
    }

    /** Render server status tiles with explicit unknown and stale states.
     * @param {Array<object>} servers - Scoped server observations.
     */
    renderServers(servers) {
        const target = document.getElementById("noc-servers");
        target.replaceChildren();
        for (const server of servers) {
            const state = observationNodeState(server);
            const tile = wanElement("a", "noc-server");
            tile.href = this.deviceUrl(server);
            tile.dataset.state = state;
            tile.append(
                wanElement("span", "noc-status-dot"), wanElement("strong", "", server.name),
                wanElement("span", "noc-server-state", state)
            );
            // One lamp per physical NIC, so a server with a dead link shows it.
            const nics = server.ports || [];
            const linked = nics.filter(port => port.link_up === true).length;
            const lamps = wanElement("span", "noc-server-nics");
            lamps.setAttribute("aria-label", `${linked} of ${nics.length} network ports linked`);
            for (const port of nics) {
                const lamp = wanElement("span", "noc-port");
                lamp.dataset.state = observationPortState(port);
                lamp.title = `${port.name}: ${observationPortState(port)}`;
                lamps.append(lamp);
            }
            if (nics.length) tile.append(
                lamps, wanElement("span", "noc-server-count", `${linked}/${nics.length} NICs`)
            );
            const last = Math.max(...server.observations.map(item => Date.parse(item.last_seen_at)).filter(Number.isFinite));
            tile.append(wanElement("small", "", `Observed ${observationAge(Date.now() - last)} ago`));
            target.append(tile);
        }
        if (!servers.length) target.append(wanElement("p", "noc-empty", "No servers observed in this scope."));
    }

    /**
     * Draw one tile per UPS in scope: its state, battery charge as a gauge,
     * runtime, load, power, voltages, any reported alerts, and where the
     * reading came from (USB on a NUT server, or relayed).
     * @param {Array<object>} nodes - Scoped equipment.
     */
    renderPower(nodes) {
        const target = document.getElementById("noc-power");
        target.replaceChildren();
        for (const node of nodes.filter(item => item.category === "ups")) {
            const found = upsReading(node);
            if (!found) continue;
            const { reading } = found;
            const condition = upsCondition(reading);
            const age = Date.now() - Date.parse(reading.sampled_at);
            const stale = !Number.isFinite(age) || age > UPS_STALE_MILLISECONDS;
            const tile = wanElement("a", "noc-ups");
            tile.href = this.deviceUrl(node);
            tile.dataset.level = stale ? "stale" : condition.level;
            const head = wanElement("div", "noc-ups-head");
            head.append(wanElement("span", "noc-status-dot"), wanElement("strong", "", node.name),
                wanElement("span", "noc-ups-state", stale ? "Reading stale" : condition.label));
            const charge = typeof reading.battery_charge === "number" ? reading.battery_charge : null;
            const battery = wanElement("div", "noc-ups-battery");
            const gauge = wanElement("span", "noc-ups-gauge");
            gauge.setAttribute("aria-hidden", "true");
            const fill = wanElement("span", "noc-ups-gauge-fill");
            fill.style.transform = `scaleX(${charge === null ? 0 : Math.min(1, Math.max(0, charge / 100)).toFixed(3)})`;
            gauge.append(fill);
            battery.append(wanElement(
                "span", "noc-ups-charge", charge === null ? "—" : `${Math.round(charge)}%`
            ), gauge,
                wanElement("span", "noc-ups-caption", "Battery"));
            const facts = wanElement("dl", "noc-ups-facts");
            const fact = (label, value) => {
                const cell = wanElement("div", "");
                cell.append(wanElement("dt", "", label), wanElement("dd", "", value));
                facts.append(cell);
            };
            const watts = reading.real_power_w ?? null;
            fact("Runtime", upsRuntime(reading.battery_runtime_s));
            fact("Load", typeof reading.load_percent === "number" ? `${Math.round(reading.load_percent)}%` : "—");
            fact(
                "Power",
                typeof watts === "number"
                    ? `${Math.round(watts)} W`
                    : typeof reading.apparent_power_va === "number"
                    ? `${Math.round(reading.apparent_power_va)} VA`
                    : "—"
            );
            fact(
                "In / Out",
                [
                    reading.input_voltage, reading.output_voltage
                ].map(v => (typeof v === "number" ? `${Math.round(v)} V` : "—")).join(" / ")
            );
            tile.append(head, battery, facts);
            if (condition.alerts.length) tile.append(wanElement("p", "noc-ups-alerts", condition.alerts.join(" · ")));
            const source = reading.attachment === "usb" ? `USB on ${reading.server_name || 'NUT server'}`
                : reading.attachment === "relayed" ? `Relayed via ${reading.driver || 'dummy-ups'} on ${reading.server_name || 'NUT server'}`
                    : `Via ${reading.server_name || 'NUT server'}`;
            tile.append(wanElement("small", "noc-ups-source", `${source} · read ${observationAge(age)} ago`));
            tile.setAttribute("aria-label", `${node.name}: ${stale ? 'reading stale' : condition.label}, battery ${charge === null ? 'unknown' : `${Math.round(charge)}%`}${condition.alerts.length ? `, ${condition.alerts.join(", ")}` : ''}`);
            target.append(tile);
        }
        if (!target.children.length) target.append(wanElement(
            "p", "noc-empty", "No UPS observed in this scope. UPS units appear once the NUT probe is enabled in [scan]."
        ));
    }

    /** Draw one lamp per physical port, retaining connector and speed tooltips.
     * @param {Array<object>} nodes - Scoped equipment.
     */
    renderPorts(nodes) {
        const target = document.getElementById("noc-ports");
        target.replaceChildren();
        const ports = nodes.flatMap(node => node.ports);
        const up = ports.filter(port => port.link_up === true).length;
        const down = ports.filter(port => port.link_up === false).length;
        const unknown = ports.length - up - down;
        const mix = document.getElementById("noc-port-mix");
        const ring = wanElement("div", "noc-port-ring");
        ring.style.setProperty("--up", `${ports.length ? up / ports.length * 100 : 0}%`);
        ring.style.setProperty("--known", `${ports.length ? (up + down) / ports.length * 100 : 0}%`);
        ring.setAttribute("role", "img");
        ring.setAttribute("aria-label", `${up} ports up, ${down} down, ${unknown} unknown`);
        ring.append(wanElement("strong", "", String(up)));
        const legend = wanElement("div", "noc-port-legend");
        legend.append(
            wanElement("strong", "", `${up} linked / ${ports.length} observed`),
            wanElement("span", "", `${down} down · ${unknown} unknown`)
        );
        mix.replaceChildren(ring, legend);
        for (const node of nodes.filter(node => node.ports.length)) {
            const row = wanElement("div", "noc-port-row");
            const name = wanElement("a", "", node.name);
            name.href = this.deviceUrl(node);
            const lamps = wanElement("div", "noc-port-lamps");
            lamps.setAttribute(
                "aria-label",
                `${node.ports.filter(port => port.link_up === true).length} up of ${node.ports.length} ports`
            );
            for (const port of node.ports) {
                const lamp = wanElement("span", "noc-port");
                lamp.dataset.state = observationPortState(port);
                const description = `${node.name} / ${port.name}: ${observationPortState(port)} · ${port.connector_type || 'connector unknown'} · ${wanLinkSpeed(port.speed_mbps)}`;
                lamp.title = description;
                lamp.setAttribute("aria-label", description);
                lamps.append(lamp);
            }
            row.append(
                name, lamps,
                wanElement(
                    "span", "readout", `${node.ports.filter(port => port.link_up === true).length}/${node.ports.length}`
                )
            );
            target.append(row);
        }
        if (!target.children.length) target.append(wanElement(
            "p", "noc-empty", "No physical port observations in this scope."
        ));
    }

    /** Use the shared real WAN history chart and rate instruments. */
    renderWan() {
        const target = document.getElementById("noc-wan-body");
        target.replaceChildren();
        for (const gateway of this.scopedGateways()) {
            const card = wanElement("article", "noc-gateway");
            const state = wanStatus(gateway);
            const age = Date.now() - Date.parse(gateway.wan?.sampled_at);
            const stale = !Number.isFinite(age) || age > 120000;
            card.dataset.state = stale ? "stale" : state.state;
            // Hero: the internet's state in large type, with the three readings that
            // matter at a glance. The gateway and active provider sit beneath it.
            const head = wanElement("div", "noc-gateway-title noc-wan-hero");
            const status = wanElement("div", "noc-wan-status");
            status.append(
                wanElement("span", "noc-wan-lamp"), wanElement("strong", "", stale ? "Reading stale" : state.label)
            );
            const facts = wanElement("dl", "noc-wan-facts");
            const fact = (label, value) => {
                const cell = wanElement("div", "");
                cell.append(wanElement("dt", "", label), wanElement("dd", "", value));
                facts.append(cell);
            };
            const latency = gateway.wan?.internet?.latency_ms;
            fact("Latency", typeof latency === "number" ? `${Math.round(latency)} ms` : "—");
            fact("Uptime", wanDuration(gateway.wan?.internet?.uptime_s));
            fact("Read", `${observationAge(age)} ago`);
            head.append(status, facts);
            const activeIsp = state.active?.isp;
            card.append(
                head, wanElement("p", "noc-wan-identity", [gateway.name, activeIsp].filter(Boolean).join(" · "))
            );
            for (const uplink of gateway.wan?.uplinks || []) {
                if (!uplink.enabled) continue;
                const row = wanElement("div", "noc-uplink");
                row.dataset.state = !uplink.up ? "down" : uplink.active ? "active" : "standby";
                row.append(wanElement("strong", "", `${uplink.name} · ${uplink.active ? 'active' : uplink.up ? 'standby' : 'down'}`), wanElement("span", "noc-uplink-isp", uplink.isp || "ISP not reported"), this.meter(`${gateway.node_id}:${uplink.id}:rx`, "Down", uplink.rx_bps, uplink.speed_mbps), this.meter(`${gateway.node_id}:${uplink.id}:tx`, "Up", uplink.tx_bps, uplink.speed_mbps));
                card.append(row);
            }
            const chart = this.sparkline(gateway.history, "noc-history", { detailed: true });
            chart.setAttribute("role", "img");
            chart.setAttribute(
                "aria-label",
                `${gateway.name}: 24-hour receive and transmit history; ${gateway.history.length} five-minute samples. Receive is the filled area, transmit the line; amber ticks mark degraded uplinks.`
            );
            const legend = wanElement("p", "noc-chart-legend");
            legend.append(
                wanElement("span", "is-rx", "Receive"), wanElement("span", "is-tx", "Transmit"),
                wanElement("span", "is-degraded", "Degraded uplink")
            );
            card.append(
                chart, legend,
                wanElement(
                    "p", "noc-note",
                    `Five-minute averages · ${gateway.history.length} stored buckets. History builds as the collector runs.`
                )
            );
            target.append(card);
        }
        if (!target.children.length) target.append(wanElement(
            "p", "noc-empty", "No WAN telemetry in this scope. Gateway readings appear after a successful UniFi scan."
        ));
    }

    /** Render gateway VPN telemetry within the same room scope as WAN status.
     * Missing or stale samples never become green, and zero traffic never
     * overrides the controller's explicit CONNECTED state.
     */
    renderVpn() {
        const target = document.getElementById("noc-vpn-body");
        target.replaceChildren();
        for (const gateway of this.scopedGateways()) {
            const vpn = gateway.wan?.vpn;
            const group = wanElement("section", "noc-vpn-group");
            group.append(wanElement("h3", "", gateway.name));
            if (!vpn) {
                group.append(wanElement("p", "noc-empty", "VPN telemetry has not been collected yet."));
                target.append(group);
                continue;
            }
            const sampled = Date.parse(vpn.sampled_at);
            const fresh = vpn.available && Number.isFinite(sampled) && Date.now() - sampled <= 120000;
            if (!fresh) group.append(wanElement("p", "noc-vpn-unavailable", vpn.available
                ? "VPN readings are stale. Showing the last known state."
                : "VPN status is unavailable. Last known readings are retained when available."));
            const sampleAge = Number.isFinite(sampled)
                ? `read ${observationAge(Date.now() - sampled)} ago`
                : "no successful sample";
            const summary = wanElement("p", "noc-note", `Remote-access sessions: ${vpn.remote_access_sessions ?? 'not reported'} · ${sampleAge}`);
            group.append(summary);
            for (const connection of vpn.connections || []) {
                const row = wanElement("button", "noc-vpn-connection");
                row.type = "button";
                row.dataset.state = fresh ? connection.state : "stale";
                const heading = wanElement("div", "noc-vpn-heading");
                const label = fresh ? {
                    connected: "Connected", disconnected: "Disconnected", connecting: "Connecting",
                    disabled: "Disabled", unknown: "State unknown"
                }[connection.state] : "Last known / stale";
                heading.append(wanElement("strong", "", connection.name), wanElement("span", "noc-tag", label));
                const elapsed = connection.connected_at ? Math.max(
                    0, ((fresh ? Date.now() : sampled) / 1000) - connection.connected_at
                ) : null;
                row.append(heading, wanElement("span", "noc-note", connection.protocol));
                const traffic = wanElement("div", "noc-vpn-traffic");
                for (const [name, rate] of [["Receive", connection.rx_bps], ["Transmit", connection.tx_bps]]) {
                    const cell = wanElement("span", "");
                    cell.append(
                        wanElement("small", "", name),
                        this.readout(`vpn:${gateway.node_id}:${connection.id}:${name}`, rate, wanRate, "")
                    );
                    traffic.append(cell);
                }
                const active = (connection.rx_bps || 0) > 0 || (connection.tx_bps || 0) > 0;
                const activity = !fresh
                    ? "Last sample"
                    : active
                    ? "Traffic observed"
                    : connection.state === "connected"
                    ? "Connected / idle"
                    : "No traffic reported";
                // One-day traffic history for this tunnel (collector vpn_history),
                // drawn with the shared WAN chart in the VPN hue.
                const history = gateway.vpn_history?.[connection.id] || [];
                row.append(traffic, this.sparkline(history, "noc-vpn-spark"),
                    wanElement("span", "noc-note", `${activity} · connection uptime ${elapsed === null ? 'not reported' : wanDuration(elapsed)}`));
                row.addEventListener("click", () => this.openVpn(gateway, connection, vpn));
                group.append(row);
            }
            if (!vpn.connections?.length) group.append(wanElement(
                "p", "noc-empty",
                fresh ? "No client or site-to-site VPN tunnels reported." : "No previous tunnel readings available."
            ));
            target.append(group);
        }
        if (!target.children.length) target.append(wanElement(
            "p", "noc-empty", "No VPN-capable gateway readings in this scope."
        ));
    }

    /** Inspect a sanitized tunnel snapshot; this drawer never exposes VPN secrets.
     * @param {object} gateway - Gateway owning the reading.
     * @param {object} connection - Whitelisted tunnel telemetry.
     * @param {object} vpn - VPN snapshot with its own freshness timestamp.
     */
    openVpn(gateway, connection, vpn) {
        this.openDrawer(connection.name);
        const body = document.getElementById("noc-drawer-body");
        body.append(wanElement(
            "p", "noc-note",
            "Explicit UniFi connection state is authoritative. Traffic and uptime provide supporting activity evidence; an idle connection is not a failure."
        ));
        const facts = wanElement("dl", "noc-facts");
        for (const [label, value] of [["Gateway", gateway.name], ["Source", gateway.source],
            [
                "Protocol", connection.protocol
            ], ["Reported connection state", connection.reported_status || "Not reported"],
            ["Profile enabled", connection.enabled === null ? "Not reported" : connection.enabled ? "Yes" : "No"],
            [
                "Connected since",
                connection.connected_at ? new Date(connection.connected_at * 1000).toLocaleString() : "Not reported"
            ],
            ["Receive", wanRate(connection.rx_bps)], ["Transmit", wanRate(connection.tx_bps)],
            [
                "VPN sample time", vpn.sampled_at || "Not available"
            ], ["Reading available", vpn.available ? "Yes" : "No"]]) {
            const row = wanElement("div", "");
            row.append(wanElement("dt", "", label), wanElement("dd", "", value));
            facts.append(row);
        }
        body.append(
            facts,
            wanElement(
                "p", "noc-note",
                "Evidence snapshot at opening. VPN connections are logical tunnels, not physical cables."
            )
        );
    }

    /**
     * Let the viewer rearrange panels by dragging the grip in each panel's
     * header (mouse or touch), or by focusing a grip and pressing arrow keys.
     * The arrangement is saved in this browser's local storage only.
     *
     * On a desk the page order is the layout. In wallboard's three columns a
     * panel also keeps the column it was dropped in (`data-column`), which
     * overrides its role column; the page order still decides the order
     * within a column.
     */
    setupArrange() {
        const grid = document.querySelector(".noc-grid");
        this.defaultOrder = this.panelsInOrder().map(panel => panel.dataset.signal);
        const ns = "http://www.w3.org/2000/svg";
        for (const panel of this.panelsInOrder()) {
            const grip = document.createElement("button");
            grip.type = "button";
            grip.className = "noc-grip";
            grip.title = "Drag to move this panel (or focus and use the arrow keys)";
            grip.setAttribute("aria-label", `Move the ${panel.querySelector('h2').textContent} panel`);
            const icon = document.createElementNS(ns, "svg");
            icon.setAttribute("viewBox", "0 0 10 16");
            icon.setAttribute("aria-hidden", "true");
            for (const [cx, cy] of [[2, 2], [8, 2], [2, 8], [8, 8], [2, 14], [8, 14]]) {
                const dot = document.createElementNS(ns, "circle");
                dot.setAttribute("cx", String(cx));
                dot.setAttribute("cy", String(cy));
                dot.setAttribute("r", "1.4");
                icon.append(dot);
            }
            grip.append(icon);
            panel.querySelector(":scope > header").append(grip);
            grip.addEventListener("pointerdown", event => this.dragStart(event, panel, grip));
            grip.addEventListener("keydown", event => this.keyMove(event, panel, grip));
        }
        document.getElementById("noc-layout-reset").addEventListener("click", () => this.resetLayout());
        const saved = this.readLayout();
        if (saved) this.applyLayout(saved.order, saved.columns);
        this.updateResetButton(Boolean(saved));
        grid.classList.add("is-arrangeable");
    }

    /** @returns {Array<HTMLElement>} The panels in page order. */
    panelsInOrder() {
        return [...document.querySelectorAll(".noc-grid > .noc-panel")];
    }

    /** @returns {number} Wallboard columns in use (3 or 4), or 0 outside the wallboard's column layout. */
    columnCount() {
        if (!document.body.classList.contains("noc-wallboard")) return 0;
        const count = getComputedStyle(document.querySelector(".noc-grid")).gridTemplateColumns.split(" ").length;
        return count >= 3 ? count : 0;
    }

    /** @returns {boolean} True when panels sit in wallboard's role columns. */
    columnLayout() {
        return this.columnCount() > 0;
    }

    /**
     * @param {HTMLElement} panel - A grid panel.
     * @returns {number} The grid column it is drawn in (1 when placed automatically).
     */
    columnOf(panel) {
        return Number.parseInt(getComputedStyle(panel).gridColumnStart, 10) || 1;
    }

    /**
     * Read the saved arrangement, ignoring anything malformed.
     * @returns {{order: Array<string>, columns: object}|null} Saved layout, or null.
     */
    readLayout() {
        try {
            const value = JSON.parse(localStorage.getItem(OBSERVATION_LAYOUT_KEY) || "null");
            if (!value || !Array.isArray(value.order)) return null;
            const columns = value.columns && typeof value.columns === "object" ? value.columns : {};
            return { order: value.order.filter(id => typeof id === "string"), columns };
        } catch {
            return null;
        }
    }

    /**
     * Put panels in a given order and wallboard columns. Panels missing from a
     * saved order (added in a later version) keep their default place after it.
     * @param {Array<string>} order - Panel ids (`data-signal`) in order.
     * @param {object} columns - Wallboard column (1 to 4) by panel id.
     */
    applyLayout(order, columns) {
        const grid = document.querySelector(".noc-grid");
        const panels = new Map(this.panelsInOrder().map(panel => [panel.dataset.signal, panel]));
        const known = order.filter(id => panels.has(id));
        for (const id of [
            ...known, ...this.defaultOrder.filter(id => !known.includes(id))
        ]) grid.append(panels.get(id));
        for (const [id, panel] of panels) {
            if ([1, 2, 3, 4].includes(columns[id])) panel.dataset.column = String(columns[id]);
            else delete panel.dataset.column;
        }
        this.layoutMasonry();
    }

    /** Save the current order and wallboard columns in this browser. */
    saveLayout() {
        const panels = this.panelsInOrder();
        const columns = Object.fromEntries(panels.filter(panel => panel.dataset.column)
            .map(panel => [panel.dataset.signal, Number(panel.dataset.column)]));
        try {
            localStorage.setItem(OBSERVATION_LAYOUT_KEY, JSON.stringify({
                version: 1, order: panels.map(panel => panel.dataset.signal), columns,
            }));
        } catch {
            // Private windows can refuse storage; the layout then lasts for this visit.
        }
        this.updateResetButton(true);
    }

    /** Forget the saved arrangement and restore the default layout. */
    resetLayout() {
        try { localStorage.removeItem(OBSERVATION_LAYOUT_KEY); } catch { /* Nothing saved. */ }
        this.applyLayout(this.defaultOrder, {});
        this.updateResetButton(false);
        this.announce("Panels restored to the default layout.");
    }

    /** @param {boolean} customized - Whether a saved arrangement is in use. */
    updateResetButton(customized) {
        document.getElementById("noc-layout-reset").hidden = !customized;
    }

    /** @param {string} message - Text for screen readers. */
    announce(message) {
        document.getElementById("noc-arrange-status").textContent = message;
    }

    /**
     * Move a panel next to another, or to the end of a column or the page,
     * then relayout and save.
     * @param {HTMLElement} panel - The panel being moved.
     * @param {{panel: HTMLElement|null, place: string, column: number|null}} target - Drop target.
     */
    placePanel(panel, target) {
        const grid = document.querySelector(".noc-grid");
        if (target.panel) {
            target.panel[target.place === "before" ? "before" : "after"](panel);
        } else if (target.column) {
            const last = this.panelsInOrder().filter(item => item !== panel
                && this.columnOf(item) === target.column).at(-1);
            if (last) last.after(panel);
            else grid.append(panel);
        } else {
            grid.append(panel);
        }
        if (target.column) panel.dataset.column = String(target.column);
        this.layoutMasonry();
        this.saveLayout();
    }

    /**
     * Begin tracking a press on a grip; it becomes a drag once the pointer moves.
     * @param {PointerEvent} event - The press.
     * @param {HTMLElement} panel - The grip's panel.
     * @param {HTMLElement} grip - The grip pressed.
     */
    dragStart(event, panel, grip) {
        if (event.button !== 0 || this.drag) return;
        event.preventDefault();
        grip.setPointerCapture(event.pointerId);
        const drag = {
            panel, grip, pointer: event.pointerId, active: false, target: null,
            startX: event.clientX, startY: event.clientY, startScroll: window.scrollY,
            x: event.clientX, y: event.clientY,
        };
        const move = moveEvent => {
            if (moveEvent.pointerId !== drag.pointer) return;
            drag.x = moveEvent.clientX;
            drag.y = moveEvent.clientY;
            if (!drag.active) {
                if (Math.hypot(drag.x - drag.startX, drag.y - drag.startY) < OBSERVATION_DRAG_THRESHOLD) return;
                this.dragBegin();
            }
            this.dragUpdate();
        };
        const up = upEvent => { if (upEvent.pointerId === drag.pointer) this.dragEnd(true); };
        const cancel = () => this.dragEnd(false);
        grip.addEventListener("pointermove", move);
        grip.addEventListener("pointerup", up);
        grip.addEventListener("pointercancel", cancel);
        grip.addEventListener("lostpointercapture", cancel);
        drag.cleanup = () => {
            grip.removeEventListener("pointermove", move);
            grip.removeEventListener("pointerup", up);
            grip.removeEventListener("pointercancel", cancel);
            grip.removeEventListener("lostpointercapture", cancel);
        };
        this.drag = drag;
    }

    /** Lift the panel, show the drop marker, and start edge auto-scrolling. */
    dragBegin() {
        const drag = this.drag;
        drag.active = true;
        drag.panel.classList.add("is-dragging");
        document.body.classList.add("noc-arranging");
        this.marker = wanElement("div", "noc-drop-marker");
        this.marker.hidden = true;
        document.body.append(this.marker);
        const scroll = () => {
            if (this.drag !== drag) return;
            const edge = 70;
            let delta = 0;
            if (drag.y < edge) delta = -Math.ceil((edge - drag.y) / 5);
            else if (drag.y > window.innerHeight - edge) delta = Math.ceil((drag.y - (window.innerHeight - edge)) / 5);
            if (delta) {
                window.scrollBy(0, delta);
                this.dragUpdate();
            }
            drag.frame = requestAnimationFrame(scroll);
        };
        drag.frame = requestAnimationFrame(scroll);
    }

    /** Follow the pointer with the panel and update the drop target and marker. */
    dragUpdate() {
        const drag = this.drag;
        drag.panel.style.transform = `translate(${drag.x - drag.startX}px, ${drag.y - drag.startY + window.scrollY - drag.startScroll}px)`;
        drag.target = this.dropTarget(drag.x, drag.y, drag.panel);
        this.showMarker(drag.target, drag.panel);
    }

    /**
     * Find where a panel would land at a pointer position: before or after the
     * panel under it (by its vertical midline), the end of the column under it
     * in wallboard, or the end of the page; null outside the grid.
     *
     * @param {number} x - The pointer's x, in viewport pixels.
     * @param {number} y - The pointer's y.
     * @param {HTMLElement} dragged - The panel being dragged.
     * @returns {{panel: HTMLElement|null, place: string, column: number|null}|null} The landing spot.
     */
    dropTarget(x, y, dragged) {
        const columns = this.columnLayout();
        const over = document.elementsFromPoint(x, y)
            .map(element => element.closest(".noc-grid > .noc-panel"))
            .find(panel => panel && panel !== dragged);
        if (over) {
            const box = over.getBoundingClientRect();
            return {
                panel: over, place: y < box.top + box.height / 2 ? "before" : "after",
                column: columns ? this.columnOf(over) : null,
            };
        }
        const grid = document.querySelector(".noc-grid");
        const box = grid.getBoundingClientRect();
        if (x < box.left || x > box.right || y < box.top) return null;
        if (!columns) return { panel: null, place: "end", column: null };
        const gap = Number.parseFloat(getComputedStyle(grid).columnGap) || 0;
        const count = this.columnCount();
        const width = (box.width - (count - 1) * gap) / count;
        const column = Math.min(count, Math.max(1, Math.floor((x - box.left) / (width + gap)) + 1));
        return { panel: null, place: "end", column };
    }

    /**
     * Draw the drop marker where the dragged panel would land.
     *
     * @param {?object} target - The landing spot from dropTarget; null hides it.
     * @param {HTMLElement} dragged - The panel being dragged.
     */
    showMarker(target, dragged) {
        if (!target) {
            this.marker.hidden = true;
            return;
        }
        const grid = document.querySelector(".noc-grid");
        const box = grid.getBoundingClientRect();
        let left = box.left;
        let width = box.width;
        let top;
        if (target.panel) {
            const panel = target.panel.getBoundingClientRect();
            left = panel.left;
            width = panel.width;
            top = target.place === "before" ? panel.top - 8 : panel.bottom + 6;
        } else if (target.column) {
            const gap = Number.parseFloat(getComputedStyle(grid).columnGap) || 0;
            const count = this.columnCount();
            width = (box.width - (count - 1) * gap) / count;
            left = box.left + (target.column - 1) * (width + gap);
            const bottoms = this.panelsInOrder().filter(item => item !== dragged
                && this.columnOf(item) === target.column)
                .map(item => item.getBoundingClientRect().bottom);
            top = bottoms.length ? Math.max(...bottoms) + 6 : box.top;
        } else {
            const bottoms = this.panelsInOrder().filter(item => item !== dragged).map(item => item.getBoundingClientRect().bottom);
            top = Math.max(box.top, ...bottoms) + 6;
        }
        Object.assign(this.marker.style, { left: `${left}px`, top: `${top}px`, width: `${width}px` });
        this.marker.hidden = false;
    }

    /**
     * Finish a drag: drop the panel at its target, or put it back on cancel.
     * @param {boolean} drop - Whether to move the panel.
     */
    dragEnd(drop) {
        const drag = this.drag;
        if (!drag) return;
        this.drag = null;
        drag.cleanup();
        cancelAnimationFrame(drag.frame);
        if (drag.grip.hasPointerCapture(drag.pointer)) drag.grip.releasePointerCapture(drag.pointer);
        drag.panel.classList.remove("is-dragging");
        drag.panel.style.transform = "";
        document.body.classList.remove("noc-arranging");
        this.marker?.remove();
        this.marker = null;
        if (drop && drag.active && drag.target) {
            this.placePanel(drag.panel, drag.target);
            this.announce(this.positionText(drag.panel));
        }
    }

    /**
     * Move a panel with the arrow keys on its focused grip: up and down step
     * past the neighbouring panel (within its column in wallboard); left and
     * right change wallboard column.
     * @param {KeyboardEvent} event - The key press.
     * @param {HTMLElement} panel - The grip's panel.
     * @param {HTMLElement} grip - The focused grip.
     */
    keyMove(event, panel, grip) {
        if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault();
        const columns = this.columnLayout();
        const column = columns ? this.columnOf(panel) : null;
        const peers = this.panelsInOrder().filter(item => !columns || this.columnOf(item) === column);
        const index = peers.indexOf(panel);
        let target = null;
        if (event.key === "ArrowUp" && index > 0) target = { panel: peers[index - 1], place: "before", column };
        if (event.key === "ArrowDown" && index < peers.length - 1) target = {
            panel: peers[index + 1], place: "after", column
        };
        if (columns && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
            const next = column + (event.key === "ArrowLeft" ? -1 : 1);
            if (next >= 1 && next <= this.columnCount()) target = { panel: null, place: "end", column: next };
        }
        if (!target) return;
        this.placePanel(panel, target);
        grip.focus();
        this.announce(this.positionText(panel));
    }

    /**
     * @param {HTMLElement} panel - A panel just moved.
     * @returns {string} Its new position, for screen readers.
     */
    positionText(panel) {
        const name = panel.querySelector("h2").textContent;
        if (this.columnLayout()) {
            const column = this.columnOf(panel);
            const peers = this.panelsInOrder().filter(item => this.columnOf(item) === column);
            return `${name} moved to column ${column}, position ${peers.indexOf(panel) + 1} of ${peers.length}.`;
        }
        const panels = this.panelsInOrder();
        return `${name} moved to position ${panels.indexOf(panel) + 1} of ${panels.length}.`;
    }

    /** Measure independent panel heights for a masonry grid without reordering DOM. */
    layoutMasonry() {
        const grid = document.querySelector(".noc-grid");
        if (!grid) return;
        grid.classList.add("is-masonry");
        for (const panel of grid.children) {
            panel.style.gridRowEnd = `span ${Math.ceil((panel.getBoundingClientRect().height + 24) / 4)}`;
        }
    }

    /** Fill the browser viewport; this intentionally does not use OS fullscreen.
     * @param {boolean} enabled - Desired wallboard state.
     * @param {boolean} updateUrl - Record state in browser history.
     */
    setWallboard(enabled, updateUrl = true) {
        document.body.classList.toggle("noc-wallboard", enabled);
        const button = document.getElementById("noc-wallboard");
        button.setAttribute("aria-pressed", String(enabled));
        button.setAttribute("aria-label", enabled ? "Exit wallboard view" : "Enter wallboard view");
        button.querySelector("span").textContent = enabled ? "Exit wallboard" : "Wallboard";
        if (updateUrl) {
            const url = new URL(location.href);
            if (enabled) url.searchParams.set("wallboard", "1");
            else url.searchParams.delete("wallboard");
            if (url.href !== location.href) history.pushState({}, "", url);
        }
        this.layoutMasonry();
    }

    /** Slide out before releasing the modal and returning focus to its opener.
     *
     * Waiting for the actual animation also handles reduced motion (no
     * animation). A transition token prevents a pending close from hiding a
     * drawer that was reopened while its closing animation was in progress.
     */
    closeDrawer() {
        const drawer = document.getElementById("noc-drawer");
        if (!drawer.open || drawer.classList.contains("is-closing")) return;
        const transition = this.drawerTransition = (this.drawerTransition || 0) + 1;
        drawer.style.setProperty("--noc-close-start", getComputedStyle(drawer).transform);
        drawer.classList.add("is-closing");
        Promise.allSettled(drawer.getAnimations().map(animation => animation.finished)).then(() => {
            if (this.drawerTransition !== transition) return;
            drawer.close();
            drawer.classList.remove("is-closing");
            drawer.style.removeProperty("--noc-close-start");
        });
    }

    /** Reset and open the one accessible right-hand drawer.
     * @param {string} title - Drawer heading, assigned as text.
     */
    openDrawer(title) {
        document.getElementById("noc-drawer-title").textContent = title;
        document.getElementById("noc-drawer-body").replaceChildren();
        document.getElementById("noc-drawer-actions").replaceChildren();
        document.getElementById("noc-drawer-error").hidden = true;
        const drawer = document.getElementById("noc-drawer");
        this.drawerTransition = (this.drawerTransition || 0) + 1;
        drawer.classList.remove("is-closing");
        drawer.style.removeProperty("--noc-close-start");
        if (!drawer.open) drawer.showModal();
    }

    /** Show condition-specific evidence and a precisely scoped ignore action.
     * @param {object} warning - Warning and its cached evidence snapshot.
     */
    openWarning(warning) {
        this.openDrawer(warning.title);
        const body = document.getElementById("noc-drawer-body");
        body.append(wanElement("h3", "", warning.detail));
        const explanation = warning.target.condition === "port-down"
            ? "The source reports this physical port down, but a cable observation still names a far end. A wall socket or an unused patch connection can legitimately be down. This is a warning about conflicting observations, not proof of an equipment failure."
            : warning.target.condition === "neighbor-unresolved"
                ? "The switch reports this neighbor, but its identity has not been matched to a discovered device. The identifiers below are what the source actually reported."
                : warning.target.condition.startsWith("ups-")

                        ? "Read from the NUT server listed below. A relayed (dummy-ups) unit repeats another source's data, so its alarm comes from that source, not from a hardware check. Ignoring this suppresses only this condition for this UPS."
                    : "This condition is derived from the cached source readings below. Ignoring it suppresses only this condition; it does not change collection, device status, or link evidence.";
        body.append(wanElement("p", "noc-note", explanation));
        const facts = wanElement("dl", "noc-facts");
        for (const [label, value] of warning.facts) {
            const row = wanElement("div", "");
            row.append(wanElement("dt", "", label), wanElement("dd", "", String(value)));
            facts.append(row);
        }
        body.append(
            facts,
            wanElement(
                "p", "noc-note", "Evidence snapshot at opening. Close and reopen to read the latest observations."
            )
        );
        if (warning.node) {
            const link = wanElement("a", "", "Open equipment");
            link.href = this.deviceUrl(warning.node);
            body.append(link);
        }
        body.append(wanElement(
            "p", "noc-note",
            "Ignore is shared across the lab and lasts until restored from Ignored items. A port rule covers only this port reporting down, not other ports or warnings."
        ));
        if (this.admin) {
            const ignore = wanElement("button", "button", "Ignore");
            ignore.type = "button";
            ignore.addEventListener(
                "click", () => this.writeIgnore(LABRAT_API.observationIgnores, { target: warning.target }, ignore)
            );
            document.getElementById("noc-drawer-actions").append(ignore);
        } else body.append(wanElement("p", "noc-note", "An administrator can manage ignore rules."));
    }

    /** List every saved rule, even when its condition is no longer active. */
    openIgnored() {
        this.openDrawer("Ignored observations");
        const body = document.getElementById("noc-drawer-body");
        body.append(wanElement(
            "p", "noc-note",
            "Shared rules for the whole lab. Restore a rule to put its condition back on the watchlist if it is still active, or an ignored device back in Discovery. Collection and port indicators continue normally."
        ));
        if (!this.ignores.length) body.append(wanElement("p", "noc-empty", "No ignored observations."));
        for (const rule of this.ignores) {
            const row = wanElement("article", "noc-ignore-row");
            // A device rule hides the whole device from Discovery.
            const kind = rule.target.condition === "device"
                ? "device hidden from Discovery"
                : rule.target.condition.replaceAll("-", " ");
            row.append(wanElement("strong", "", rule.label), wanElement("small", "", kind),
                wanElement("small", "", `Ignored ${new Date(rule.created_at).toLocaleString()}`));
            if (this.admin) {
                const restore = wanElement("button", "button button-secondary", "Restore");
                restore.type = "button";
                restore.setAttribute("aria-label", `Restore ${rule.label}: ${rule.target.condition}`);
                restore.addEventListener(
                    "click",
                    () => this.writeIgnore(`${LABRAT_API.observationIgnore(rule.id)}/remove`, {}, restore, rule.id)
                );
                row.append(restore);
            }
            body.append(row);
        }
    }

    /** Save or restore a policy with CSRF and keep failed writes reviewable.
     * @param {string} url - Same-origin policy endpoint.
     * @param {object} payload - Validated again by the server.
     * @param {HTMLElement} button - Pending action control.
     * @param {number|null} removedId - Rule being restored, or null for a new rule.
     */
    async writeIgnore(url, payload, button, removedId = null) {
        if (this.writeBusy) return;
        this.writeBusy = true;
        this.ignoreRevision += 1;
        button.disabled = true;
        try {
            const result = await labratJson(url, { payload, csrfToken: this.csrf,
                signal: AbortSignal.timeout(10000), failure: "The rule could not be saved." });
            if (removedId !== null) this.ignores = this.ignores.filter(rule => rule.id !== removedId);
            else this.ignores = [...this.ignores.filter(rule => rule.id !== result.ignore.id), result.ignore];
            this.render();
            this.openIgnored();
        } catch (error) {
            const message = document.getElementById("noc-drawer-error");
            message.textContent = error.message;
            message.hidden = false;
        } finally {
            this.writeBusy = false;
            button.disabled = false;
        }
    }

    /** Compare stable identities rather than regenerated cable IDs.
     * @param {object} data - Latest topology cache.
     * @param {Array<object>} gateways - Latest WAN readings.
     */
    recordChanges(data, gateways) {
        const state = new Map();
        const add = (key, value, message, room = null) => state.set(key, { value, message, room });
        for (const source of data.sources) {
            add(`source:${source.name}`, source.last_success_at, `${source.name} completed a collection`);
            add(`error:${source.name}`, source.last_error, `${source.name}: ${source.last_error ? 'collection failed' : 'collection recovered'}`);
        }
        for (const node of data.nodes) {
            if (observationPassingClient(node)) continue;
            const room = node.placement?.container_room_id || "unplaced";
            add(`node:${node.id}`, observationNodeState(node), `${node.name}: ${observationNodeState(node)}`, room);
            for (const port of node.ports) add(
                `port:${node.id}:${port.source}:${port.name}`, observationPortState(port),
                `${node.name} / ${port.name}: link ${observationPortState(port)}`, room
            );
        }
        for (const gateway of gateways) {
            const node = data.nodes.find(item => item.id === gateway.node_id);
            add(
                `wan:${gateway.node_id}`, wanStatus(gateway).state, `${gateway.name}: ${wanStatus(gateway).label}`,
                node?.placement?.container_room_id || "unplaced"
            );
            for (const connection of gateway.wan?.vpn?.connections || []) {
                add(
                    `vpn:${gateway.node_id}:${connection.id}`, connection.state,
                    `${gateway.name} / ${connection.name}: VPN ${connection.state}`,
                    node?.placement?.container_room_id || "unplaced"
                );
            }
        }
        for (const link of data.links) {
            const endpoints = [`${link.from}:${link.from_port}`, `${link.to}:${link.to_port}`].sort().join("|");
            const node = data.nodes.find(item => item.id === link.from);
            const other = data.nodes.find(item => item.id === link.to);
            add(`link:${endpoints}`, link.confidence, `Physical link observed: ${node?.name || 'device'} / ${link.from_port} ↔ ${other?.name || 'device'} / ${link.to_port} (${link.confidence})`, node?.placement?.container_room_id || "unplaced");
        }
        if (this.previous) {
            for (const [key, item] of state) {
                const before = this.previous.get(key);
                if (before && before.value !== item.value) this.events.unshift({ ...item, time: Date.now() });
                else if (!before
                    && !key.startsWith("source:")
                    && !key.startsWith("error:")) this.events.unshift({ ...item, time: Date.now() });
            }
            for (const [key, item] of this.previous) {
                if (!state.has(key) && key.startsWith("link:")) this.events.unshift({
                    ...item, message: "Physical link no longer in the current observations", time: Date.now()
                });
            }
        } else this.events.push({ message: "Watch started. Current cache loaded.", time: Date.now(), room: null });
        this.previous = state;
        this.events = this.events.slice(0, 60);
    }

    /** Display a bounded session log, with source events visible in every scope. */
    renderEvents() {
        const target = document.getElementById("noc-events");
        target.replaceChildren();
        for (const event of this.events.filter(item => !this.room
            || item.room === null
            || String(item.room) === this.room).slice(0, 20)) {
            const row = wanElement("li", "");
            row.append(
                wanElement("time", "", new Date(event.time).toLocaleTimeString()), wanElement("span", "", event.message)
            );
            target.append(row);
        }
        if (!target.children.length) target.append(wanElement(
            "li", "noc-empty", "No changes observed in this scope during this visit."
        ));
    }
}

if (document.getElementById("observations-app")) new ObservationConsole().start();
