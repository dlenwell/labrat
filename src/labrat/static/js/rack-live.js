/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @fileoverview Equipment's live layer: telemetry ticks, source and UPS polling, change
 * flashes, ignore rules, port alarms, and the annunciator. Reads only the
 * local API; open edit forms are never rebuilt under the user.
 *
 * Part of the Equipment page's RackViewApp (racks.js): these methods join
 * its prototype through rackAddMethods, so `this` is the page's app. Load
 * after racks.js.
 */

/** LiveUpdates methods of RackViewApp; never constructed itself. */
class RackLiveUpdates {
    /**
     * Draw one telemetry cell per discovery source and start the one-second
     * tick that advances the clock, each source's collection age, and the
     * countdown to its next poll. The gauge fills toward the next poll when
     * the collector recorded its interval, and otherwise drains over the
     * fresh window.
     *
     * @returns {void}
     */
    renderTelemetry() {
        const target = document.getElementById("rack-telemetry-sources");
        if (!target) return;
        target.replaceChildren();
        this.telemetryCells = [];
        for (const source of this.sources) {
            const cell = rackText("div", "rack-telemetry-cell rack-telemetry-source", "");
            const head = rackText("span", "rack-telemetry-label", "");
            const lamp = rackText("span", "rack-status-lamp", "");
            lamp.setAttribute("aria-hidden", "true");
            head.append(lamp, document.createTextNode(source.name));
            const readings = rackText("span", "rack-telemetry-readings", "");
            const age = rackText("span", "rack-telemetry-value", "--:--");
            const next = rackText("span", "rack-telemetry-next", "");
            readings.append(age, next);
            const gauge = rackText("span", "rack-telemetry-gauge", "");
            const fill = rackText("span", "rack-telemetry-gauge-fill", "");
            gauge.append(fill);
            gauge.setAttribute("aria-hidden", "true");
            cell.append(head, readings, gauge);
            target.append(cell);
            this.telemetryCells.push({ source, cell, lamp, age, next, fill });
        }
        this.tickTelemetry();
        if (!this.telemetryTimer) {
            this.telemetryTimer = setInterval(() => {
                if (!document.hidden) this.tickTelemetry();
            }, 1000);
        }
    }

    /** Advance the clock and source ages by one tick. */
    tickTelemetry() {
        const now = new Date();
        const clock = document.getElementById("rack-clock");
        if (clock) {
            clock.textContent = now.toLocaleTimeString(
                [], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }
            );
            clock.dateTime = now.toISOString();
        }
        for (const { source, cell, lamp, age, next, fill } of this.telemetryCells || []) {
            const cycle = rackScanCycle(source, now.getTime());
            const state = source.last_error ? "fault" : cycle.overdue ? "stale" : "online";
            lamp.dataset.state = state === "online" ? "online" : "stale";
            cell.dataset.state = state;
            age.textContent = source.last_error ? "FAULT" : rackAgeReadout(cycle.sinceSuccess);
            let progress;
            if (cycle.interval) {
                if (cycle.overdue) next.textContent = `Overdue +${rackAgeReadout(-cycle.untilNext - cycle.interval)}`;
                else if (cycle.untilNext <= 0) next.textContent = "Due";
                else next.textContent = `Next ${rackAgeReadout(cycle.untilNext)}`;
                progress = 1 - Math.max(0, cycle.untilNext) / cycle.interval;
            } else {
                next.textContent = "";
                progress = Number.isFinite(cycle.sinceSuccess)
                    ? Math.max(0, 1 - cycle.sinceSuccess / RACK_FRESH_MILLISECONDS) : 0;
            }
            cell.dataset.due = String(Boolean(cycle.interval) && cycle.untilNext <= 0 && !cycle.overdue);
            fill.style.transform = `scaleX(${Math.min(1, Math.max(0, progress)).toFixed(4)})`;
            cell.title = source.last_error
                ? `${source.name}: last collection failed`
                : `${source.name}: last collected ${rackAgeReadout(cycle.sinceSuccess)} ago`
                    + (cycle.interval ? `, polled every ${rackAgeReadout(cycle.interval)}` : "");
        }
    }

    /**
     * Start watching for new collections. Polling pauses while the tab is
     * hidden and checks at once when it is shown again.
     *
     * @returns {void}
     */
    startLiveUpdates() {
        if (this.liveStarted) return;
        this.liveStarted = true;
        this.sourceStamp = this.sourceSignature(this.sources);
        const schedule = () => {
            clearTimeout(this.pollTimer);
            this.pollTimer = setTimeout(async () => {
                await this.pollSources();
                schedule();
            }, this.pollDelay());
        };
        document.addEventListener("visibilitychange", async () => {
            if (document.hidden) return;
            await this.pollSources();
            await this.pollPower();
            schedule();
        });
        schedule();
        // UPS readings are telemetry: they update between source scans, so
        // source signatures alone cannot refresh the Power panel.
        this.powerPollTimer = setInterval(() => this.pollPower(), 30000);
        this.renderEventLine();
        // New WAN readings arrive between collections: refresh the tiles.
        window.labratWan?.subscribe(() => this.renderAnnunciator());
    }

    /**
     * Fingerprint the collection state that a reload would change.
     *
     * @param {?object[]} sources - The sources API's records.
     * @returns {string} Their names, attempt and success times, and errors.
     */
    sourceSignature(sources) {
        return JSON.stringify((sources || []).map((source) => [
            source.name, source.last_attempt_at, source.last_success_at, source.last_error,
        ]));
    }

    /**
     * Poll sooner while any source's next collection is due.
     *
     * @returns {number} Milliseconds to the next poll.
     */
    pollDelay() {
        const now = Date.now();
        const due = this.sources.some((source) => {
            const cycle = rackScanCycle(source, now);
            return cycle.interval && cycle.untilNext <= 0;
        });
        return due ? RACK_POLL_DUE_MILLISECONDS : RACK_POLL_IDLE_MILLISECONDS;
    }

    /**
     * Ask for source state; reload the room when a collection happened.
     * A failed request marks the telemetry link as lost (or signed out)
     * without disturbing what is drawn.
     *
     * @returns {Promise<void>}
     */
    async pollSources() {
        if (document.hidden || this.polling) return;
        this.polling = true;
        try {
            const [{ sources }, policies] = await Promise.all([LABRAT_API.sources, LABRAT_API.observationIgnores].map(
                (url) => labratJson(url, { signal: AbortSignal.timeout(10000) }),
            ));
            if (this.setIgnoreRules(policies.ignores || [])) this.render({ preserveForms: true });
            this.linkState = "live";
            const stamp = this.sourceSignature(sources);
            if (stamp === this.sourceStamp) {
                this.sources = sources;
                return;
            }
            await this.applyUpdate();
        } catch (error) {
            this.linkState = error.status === 401 ? "signed-out" : "lost";
        } finally {
            this.polling = false;
            this.renderEventLine();
        }
    }

    /**
     * Refresh UPS telemetry without rebuilding the room or touching open edit
     * forms. A source collection still performs the full topology reload.
     *
     * @returns {Promise<void>}
     */
    async pollPower() {
        if (document.hidden || this.powerPolling) return;
        const roomUps = this.nodes.filter((node) => node.category === "ups"
            && node.placement?.container_room_id === this.roomId);
        if (!roomUps.length) return;
        this.powerPolling = true;
        const currentNodes = this.nodes;
        try {
            const topology = await labratJson(LABRAT_API.topology, { signal: AbortSignal.timeout(10000) });
            if (this.nodes !== currentNodes) return;
            const fresh = new Map((topology.nodes || []).map((node) => [node.id, node]));
            let changed = false;
            for (const node of roomUps) {
                const update = fresh.get(node.id);
                if (!update) continue;
                const oldReadings = node.observations.filter((item) => item.ups).map((item) => item.ups);
                const newReadings = update.observations.filter((item) => item.ups).map((item) => item.ups);
                if (JSON.stringify(oldReadings) === JSON.stringify(newReadings)) continue;
                node.observations = update.observations;
                changed = true;
            }
            this.renderRoomPower();
            this.refreshUpsBadges();
            if (changed) {
                this.renderAnnunciator();
                if (this.currentView === "details" && roomUps.some((node) => node.id === this.selectedId)) {
                    this.renderInspector();
                }
            }
        } catch {
            // Keep the last reading; its age and stale label still advance.
            this.renderRoomPower();
            this.refreshUpsBadges();
        } finally {
            this.powerPolling = false;
        }
    }

    /**
     * True while a drawer form with unsaved input is open.
     *
     * @returns {boolean} Whether a refresh should wait.
     */
    isEditing() {
        if (this.roomWiring?.dragging || this.roomWiring?.panning || this.roomWiring?.patchDetails) return true;
        if (this.patchBox?.open) return true;
        if (this.arrangement || this.renaming) return true;
        return Boolean(this.drawerOpen && RACK_EDITING_VIEWS.has(this.currentView));
    }

    /**
     * Reload the room after a new collection, unless a form is open: then
     * hold the update and apply it when the form closes.
     *
     * @param {string} [trigger] - Event-log label when no source delivered
     *     new data (for example "Refresh").
     * @returns {Promise<void>}
     */
    async applyUpdate(trigger = "Collection") {
        if (this.applying) return;
        if (this.isEditing()) {
            this.updateHeld = true;
            this.heldTrigger = this.heldTrigger || trigger;
            this.renderEventLine();
            return;
        }
        this.updateHeld = false;
        this.applying = true;
        const before = this.snapshotState();
        const sourcesBefore = new Map(this.sources.map((source) => [source.name, source.last_success_at]));
        try {
            await this.load({ quiet: true });
            this.sourceStamp = this.sourceSignature(this.sources);
            this.recordChanges(before, sourcesBefore, this.heldTrigger || trigger);
            this.heldTrigger = null;
        } catch {
            this.linkState = "lost";
        } finally {
            this.applying = false;
            this.renderEventLine();
        }
    }

    /** Refresh button: reload now, even with no new collection. */
    async refreshNow() {
        try {
            await this.applyUpdate("Refresh");
        } catch { /* load() already showed the error. */ }
    }

    /**
     * Record what a refresh can change: each device's lamp state, each port's
     * lamp state, and the set of cables, keyed by stable identifiers.
     *
     * @returns {{devices: Map<string, string>, ports: Map<string, string>, links: Set<string>}}
     */
    snapshotState() {
        const devices = new Map();
        const ports = new Map();
        for (const node of this.nodes) {
            devices.set(node.id, this.deviceState(node));
            for (const port of node.ports) ports.set(`${node.id}::${port.name}`, this.portAlarmState(port, node.id));
        }
        return { devices, ports, links: new Set(this.links.map(rackLinkKey)) };
    }

    /**
     * Compare the room before and after a refresh, remember what changed so
     * the drawing can highlight it, and add a line to the event log.
     *
     * @param {object} before - snapshotState() from before the reload.
     * @param {Map<string, string|null>} sourcesBefore - Last success per source before.
     * @param {string} trigger - Label used when no source delivered new data.
     * @returns {void}
     */
    recordChanges(before, sourcesBefore, trigger) {
        const after = this.snapshotState();
        const devices = new Set([...after.devices].filter(([id, state]) => before.devices.get(id) !== state).map(([id]) => id));
        const ports = new Set([...after.ports].filter(([key, state]) => before.ports.has(key)
            && before.ports.get(key) !== state).map(([key]) => key));
        const added = new Set([...after.links].filter((key) => !before.links.has(key)));
        const removed = [...before.links].filter((key) => !after.links.has(key)).length;
        const received = new Set(this.sources.filter((source) => (
            source.last_success_at && source.last_success_at !== sourcesBefore.get(source.name)
        )).map((source) => source.name));
        this.recentChanges = {
            devices, ports, links: added, received, until: performance.now() + RACK_CHANGE_FLASH_MILLISECONDS
        };
        const parts = [];
        if (devices.size) parts.push(`${devices.size} device${devices.size === 1 ? "" : "s"} changed`);
        if (ports.size) parts.push(`${ports.size} port${ports.size === 1 ? "" : "s"} changed`);
        if (added.size) parts.push(`${added.size} cable${added.size === 1 ? "" : "s"} added`);
        if (removed) parts.push(`${removed} cable${removed === 1 ? "" : "s"} removed`);
        const failed = this.sources.filter((source) => source.last_error).map((source) => source.name);
        if (failed.length) parts.push(`${failed.join(", ")} failed`);
        const what = parts.length ? parts.join(" · ") : "no changes";
        const from = received.size ? [...received].join(", ") : trigger;
        this.events.unshift({ at: new Date(), text: `${from}: ${what}`, changed: parts.length > 0 });
        this.events.length = Math.min(this.events.length, 8);
        this.markChanges();
        this.queueCables();
        setTimeout(() => this.queueCables(), RACK_CHANGE_FLASH_MILLISECONDS + 50);
    }

    /** Highlight devices, port lamps, and telemetry cells changed by the last refresh. */
    markChanges() {
        const changes = this.recentChanges;
        if (!changes || performance.now() >= changes.until) return;
        const racks = document.getElementById("rack-room-racks");
        for (const item of document.querySelectorAll("[data-node]")) {
            if (changes.devices.has(item.dataset.node)) item.classList.add("is-changed");
        }
        for (const item of racks?.querySelectorAll("[data-pin]") || []) {
            if (changes.ports.has(item.dataset.pin)) item.classList.add("is-changed");
        }
        for (const { source, cell } of this.telemetryCells || []) {
            if (changes.received.has(source.name)) cell.classList.add("is-received");
        }
    }

    /**
     * Show the newest event and the live-link state at the end of the
     * telemetry strip; earlier events are in its tooltip.
     *
     * @returns {void}
     */
    renderEventLine() {
        const strip = document.getElementById("rack-telemetry");
        if (!strip) return;
        let cell = document.getElementById("rack-event-line");
        if (!cell) {
            cell = rackText("div", "rack-telemetry-cell rack-telemetry-events", "");
            cell.id = "rack-event-line";
            cell.setAttribute("role", "status");
            const label = rackText("span", "rack-telemetry-label", "");
            label.append(rackText("span", "rack-status-lamp", ""), rackText("span", "rack-event-state", ""));
            cell.append(label, rackText("span", "rack-telemetry-event", ""));
            strip.append(cell);
        }
        const state = this.updateHeld ? "held" : this.linkState || "live";
        cell.dataset.state = state;
        // Red is kept for servers down; every other state here is amber.
        cell.querySelector(".rack-status-lamp").dataset.state = state === "live" ? "online" : "stale";
        cell.querySelector(".rack-event-state").textContent = {
            live: "Live", held: "Update held", lost: "Link lost", "signed-out": "Signed out",
        }[state];
        const latest = this.events?.[0];
        const time = (date) => date.toLocaleTimeString(
            [], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }
        );
        let text = latest ? `${time(latest.at)} ${latest.text}` : "Watching for collections";
        if (state === "held") text = "New collection waits until you close the form";
        if (state === "lost") text = "Cannot reach LabRat; retrying";
        if (state === "signed-out") text = "Sign in again to resume live updates";
        cell.querySelector(".rack-telemetry-event").textContent = text;
        cell.classList.toggle(
            "is-fresh", Boolean(latest?.changed) && Date.now() - latest.at < RACK_CHANGE_FLASH_MILLISECONDS
        );
        cell.title = (this.events || []).map((event) => `${time(event.at)} ${event.text}`).join("\n")
            || "No collections received yet";
    }

    /** Compare policy targets using source-scoped identities, not row IDs.
     * @param {object} target - Exact condition target from the policy API.
     * @returns {string} Canonical key matching the Observations screen.
     */
    ignoreKey(target) {
        return JSON.stringify(Object.fromEntries(Object.entries(target).sort(([a], [b]) => a.localeCompare(b))));
    }

    /** Update shared policy state without waiting for a new collection.
     * @param {Array<object>} rules - Current rules, including cleared conditions.
     * @returns {boolean} Whether effective policy targets changed.
     */
    setIgnoreRules(rules) {
        const targets = new Set(rules.map((rule) => this.ignoreKey(rule.target)));
        const changed = targets.size !== this.ignoreTargets.size
            || [...targets].some((key) => !this.ignoreTargets.has(key));
        this.ignoreTargets = targets;
        return changed;
    }

    /** Test an observation's exact condition; missing identity fails open.
     * @param {string} condition - Shared policy condition kind.
     * @param {object|undefined} observation - Source-scoped device observation.
     * @param {object} extra - Physical port or neighbor identity, when applicable.
     * @returns {boolean} Whether this condition is suppressed.
     */
    observationIgnored(condition, observation, extra = {}) {
        if (!observation) return false;
        return this.ignoreTargets.has(this.ignoreKey({ condition, source_id: observation.source_id,
            device_remote_id: observation.remote_id, ...extra }));
    }

    /** Match an exact switch-side neighbor policy, retaining raw inspector evidence.
     * @param {object} node - Switch or gateway topology node.
     * @param {object} neighbor - One reported LLDP neighbor.
     * @returns {boolean} Whether its unresolved warning is ignored.
     */
    neighborIgnored(node, neighbor) {
        const observation = node.observations.find((item) => item.id === neighbor.switch_observation_id);
        return this.observationIgnored("neighbor-unresolved", observation, {
            port_remote_id: neighbor.local_port, chassis_id: neighbor.chassis_id, remote_port_id: neighbor.port_id,
        });
    }

    /**
     * Classify a port for alarm lighting: a known cable on a port reported
     * down is a fault; otherwise its plain link state applies.
     *
     * @param {object} port - Port record.
     * @param {string} nodeId - Owning device node.
     * @returns {"fault"|"up"|"off"} Lighting state.
     */
    portAlarmState(port, nodeId) {
        const node = this.nodes.find((item) => item.id === nodeId);
        const observation = node?.observations.find((item) => item.id === port.observation_id);
        if (port.link_up === false && this.cableAt(nodeId, port.name)
                && !this.observationIgnored(
                    "port-down", observation, { port_remote_id: port.remote_id }
                )) return "fault";
        return this.portIsUp(port, nodeId) ? "up" : "off";
    }

    /**
     * Evaluate the room's alarm conditions from loaded data.
     *
     * Every condition comes from real collector state. Only a server its
     * source reports offline is an alarm (red, fast flash). Source errors,
     * overdue collection (see rackScanCycle), cabled ports reported down (often just a moved
     * cable), devices not seen recently, and unresolved LLDP neighbors are
     * warnings; live devices without an observed cable are advisory. Each
     * returns the affected device IDs so the racks can show them.
     *
     * @returns {Array<{id: string, label: string, severity: string, count: number,
     *     nodes: string[], detail: string}>} One entry per annunciator tile.
     */
    roomAlarms() {
        const now = Date.now();
        const age = (value) => (value ? now - new Date(value).getTime() : Infinity);
        const inRoom = this.nodes.filter((node) => node.placement?.container_room_id === this.roomId);
        // A BMC record going offline is not the server going offline.
        const offline = inRoom.filter((node) => {
            const managed = new Set(deviceManagement(node).observations.map((item) => item.id));
            return node.observations.some((item) => item.status === "offline" && !managed.has(item.id)
                && !this.observationIgnored("server-offline", item));
        });
        const faulted = this.sources.filter((source) => source.last_error
            && !this.ignoreTargets.has(this.ignoreKey({ condition: "source-fault", source_id: source.id })));
        const stale = this.sources.filter((source) => rackScanCycle(source, now).overdue
            && !this.ignoreTargets.has(this.ignoreKey({ condition: "source-overdue", source_id: source.id })));
        const down = inRoom.filter((node) => node.ports.some((port) => this.portAlarmState(port, node.id) === "fault"));
        const lost = inRoom.filter((node) => node.observations.length
            && Math.min(...node.observations.map((item) => age(item.last_seen_at))) > 60 * 60000
            && !this.observationIgnored(
                "device-stale", node.observations.find((item) => item.status === "offline") || node.observations[0]
            ));
        const unresolvedNeighbors = (node) => this.neighborsFor(node).filter((item) => !item.resolved
            && !this.neighborIgnored(node, item));
        const unresolved = inRoom.filter((node) => unresolvedNeighbors(node).length);
        const neighborCount = unresolved.reduce((sum, node) => (
            sum + unresolvedNeighbors(node).length), 0);
        const uncabled = inRoom.filter((node) => (
            node.ports.some((port) => this.portIsUp(port, node.id))
            && !node.ports.some((port) => this.cableAt(node.id, port.name))
        ));
        const names = (items) => items.map((item) => item.name).join(", ");
        // Internet uplinks (wan.js): red only when a gateway has no working
        // uplink; one down or traffic on the backup is a warning.
        const nodeIds = new Set(inRoom.map((node) => node.id));
        const gateways = (window.labratWan?.gateways || []).filter((gateway) => nodeIds.has(gateway.node_id));
        const wanState = (gateway) => (typeof wanStatus === "function" ? wanStatus(gateway).state : "online");
        const wanIgnored = (condition, gateway) => this.observationIgnored(condition,
            inRoom.find((node) => node.id === gateway.node_id)?.observations.find((item) => item.source === gateway.source));
        const noInternet = gateways.filter((gateway) => wanState(gateway) === "down"
            && !wanIgnored("wan-down", gateway));
        const failover = gateways.filter((gateway) => wanState(gateway) === "degraded" && !wanIgnored(
            "wan-degraded", gateway
        ));
        const wanAlarms = gateways.length ? [
            { id: "internet-down", label: "Internet down", severity: "alarm", count: noInternet.length,
                nodes: noInternet.map((gateway) => gateway.node_id),
                detail: noInternet.length
                    ? `Every uplink is down: ${names(noInternet)}`
                    : "No unignored internet-down warnings" },
            { id: "wan-failover", label: "WAN failover", severity: "warning", count: failover.length,
                nodes: failover.map((gateway) => gateway.node_id),
                detail: failover.length
                    ? `An uplink is down or traffic is on the backup: ${names(failover)}`
                    : "No unignored WAN failover warnings" },
        ] : [];
        // UPS units (NUT): red only for low battery; on battery or a reported
        // problem is amber. Each condition honours its own ignore rule.
        const upsLow = [];
        const upsWarn = [];
        for (const node of inRoom.filter((item) => item.category === "ups")) {
            const ups = typeof upsReading === "function" ? upsReading(node) : null;
            if (!ups) continue;
            const condition = upsCondition(ups.reading);
            if (ups.reading.state === "low_battery" && !this.observationIgnored(
                "ups-low-battery", ups.observation
            )) upsLow.push(node);
            else if ((ups.reading.state === "on_battery" && !this.observationIgnored("ups-on-battery", ups.observation))
                || (condition.alerts.length && !this.observationIgnored(
                    "ups-alert", ups.observation
                ))) upsWarn.push(node);
        }
        const upsAlarms = inRoom.some((item) => item.category === "ups" && item.observations.some((observation) => observation.ups))
            ? [
            { id: "ups-low-battery", label: "UPS low battery", severity: "alarm", count: upsLow.length,
                nodes: upsLow.map((node) => node.id), detail: upsLow.length
                    ? `Battery low: ${names(upsLow)}`
                    : "No unignored low-battery warnings" },
            { id: "ups-warning", label: "UPS warning", severity: "warning", count: upsWarn.length,
                nodes: upsWarn.map((node) => node.id), detail: upsWarn.length
                    ? `On battery or reporting a problem: ${names(upsWarn)}`
                    : "No unignored UPS warnings" },
        ] : [];
        return [
            { id: "server-down", label: "Server down", severity: "alarm", count: offline.length,
                nodes: offline.map((node) => node.id), detail: offline.length
                    ? `Reported offline: ${names(offline)}`
                    : "No unignored server-down warnings" },
            ...wanAlarms,
            ...upsAlarms,
            { id: "source-fault", label: "Source fault", severity: "warning", count: faulted.length,
                nodes: [], detail: faulted.length
                    ? `Last collection failed: ${names(faulted)}`
                    : "No unignored source faults" },
            { id: "link-down", label: "Link down", severity: "warning", count: down.length,
                nodes: down.map((node) => node.id), detail: down.length
                    ? `Cabled port reported down: ${names(down)}`
                    : "No unignored cabled-port warnings" },
            { id: "scan-overdue", label: "Scan overdue", severity: "warning", count: stale.length,
                nodes: [], detail: stale.length
                    ? `Collection overdue: ${names(stale)}`
                    : "No unignored overdue collections" },
            { id: "not-seen", label: "Not seen 1 h", severity: "warning", count: lost.length,
                nodes: lost.map((node) => node.id), detail: lost.length
                    ? `Not observed for an hour: ${names(lost)}`
                    : "No unignored stale-device warnings" },
            { id: "neighbor", label: "Nbr unresolved", severity: "warning", count: neighborCount,
                nodes: unresolved.map((node) => node.id), detail: neighborCount
                    ? `${neighborCount} LLDP neighbor(s) not matched to a device`
                    : "No unignored unresolved neighbors" },
            { id: "uncabled", label: "No cable data", severity: "advisory", count: uncabled.length,
                nodes: uncabled.map((node) => node.id), detail: uncabled.length
                    ? `Live ports but no observed cable: ${names(uncabled)}`
                    : "Every live device has a cable" },
        ];
    }

    /**
     * Read acknowledged alarm signatures for this room (per browser).
     *
     * @returns {object} Acknowledged signatures by alarm; empty without storage.
     */
    readAcknowledged() {
        try {
            return JSON.parse(localStorage.getItem(`labrat.annunciator.room.${this.roomId}`) || "{}") || {};
        } catch { return {}; }
    }

    /**
     * Save acknowledged alarm signatures for this room.
     *
     * @param {object} value - Acknowledged signatures by alarm.
     */
    writeAcknowledged(value) {
        try {
            localStorage.setItem(`labrat.annunciator.room.${this.roomId}`, JSON.stringify(value));
        } catch { /* Acknowledgement then lasts for this page only. */ }
    }

    /**
     * Draw the annunciator: one tile per active alarm condition, flashing
     * when unacknowledged and lit steady once acknowledged. Clear conditions
     * are not drawn, and a room with none active hides the whole bar. A change in what is affected flashes the tile again.
     * Selecting a tile acknowledges it and marks its devices in the racks.
     *
     * @returns {void}
     */
    renderAnnunciator() {
        const target = document.getElementById("rack-annunciator");
        if (!target) return;
        const alarms = this.roomAlarms();
        this.alarms = alarms;
        const acknowledged = { ...this.readAcknowledged(), ...(this.sessionAcks || {}) };
        const signature = (alarm) => `${alarm.count}|${[...alarm.nodes].sort().join(",")}`;
        const tiles = rackText("div", "rack-annunciator-tiles", "");
        let flashing = 0;
        let active = 0;
        for (const alarm of alarms) {
            if (alarm.count <= 0) continue;
            const tile = document.createElement("button");
            tile.type = "button";
            tile.className = "rack-annunciator-tile";
            tile.dataset.severity = alarm.severity;
            const lit = alarm.count > 0;
            const unacked = lit && acknowledged[alarm.id] !== signature(alarm);
            if (lit && alarm.severity !== "advisory") active += 1;
            if (unacked) flashing += 1;
            tile.dataset.state = !lit ? "clear" : unacked ? "flashing" : "lit";
            tile.setAttribute("aria-pressed", String(this.alarmFocus === alarm.id));
            tile.title = alarm.detail;
            tile.setAttribute("aria-label", `${alarm.label}: ${lit ? `${alarm.count} active${unacked ? ", not acknowledged" : ""}` : "clear"}. ${alarm.detail}`);
            tile.append(
                rackText("span", "rack-annunciator-label", alarm.label),
                rackText("span", "rack-annunciator-count", lit ? String(alarm.count).padStart(2, "0") : "--"),
            );
            tile.addEventListener("click", () => {
                const acks = this.readAcknowledged();
                acks[alarm.id] = signature(alarm);
                this.writeAcknowledged(acks);
                this.sessionAcks = { ...(this.sessionAcks || {}), [alarm.id]: signature(alarm) };
                this.alarmFocus = this.alarmFocus === alarm.id || !lit ? null : alarm.id;
                this.render({ preserveForms: true });
            });
            tiles.append(tile);
        }
        const master = rackText("div", "rack-annunciator-master", "");
        // Red only when a red alarm is lit; warnings alone light it amber.
        const alarmLit = alarms.some((alarm) => alarm.count && alarm.severity === "alarm");
        master.dataset.state = alarmLit ? "alarm" : active ? "warning" : "normal";
        master.append(
            rackText("span", "rack-annunciator-lamp", ""),
            rackText("span", "rack-annunciator-master-label", active ? `${active} condition${active === 1 ? "" : "s"}` : "All normal"),
        );
        const ack = rackText("button", "rack-annunciator-ack", "Ack");
        ack.type = "button";
        ack.disabled = flashing === 0;
        ack.setAttribute("aria-label", "Acknowledge all alarms");
        ack.addEventListener("click", () => {
            const acks = this.readAcknowledged();
            for (const alarm of alarms) if (alarm.count) acks[alarm.id] = signature(alarm);
            this.writeAcknowledged(acks);
            this.sessionAcks = { ...acks };
            this.render({ preserveForms: true });
        });
        master.append(ack);
        // An empty bar is hidden by `.rack-annunciator:empty`.
        if (tiles.childElementCount) target.replaceChildren(master, tiles);
        else target.replaceChildren();
        // Mark the devices behind the focused tile; everything else recedes.
        const focused = alarms.find((alarm) => alarm.id === this.alarmFocus && alarm.count);
        if (!focused) this.alarmFocus = null;
        const marked = new Set(focused?.nodes || []);
        const racks = document.getElementById("rack-room-racks");
        racks?.classList.toggle("has-alarm-focus", Boolean(focused?.nodes.length));
        for (const item of racks?.querySelectorAll("[data-node]") || []) {
            item.classList.toggle("is-alarm-marked", marked.has(item.dataset.node));
        }
    }
}

rackAddMethods(RackLiveUpdates);
