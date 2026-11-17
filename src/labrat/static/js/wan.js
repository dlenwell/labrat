/*
 * Copyright 2026 LabRat contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * WAN status widgets. Reads /api/v1/wan (LabRat's own stored readings; the
 * page never contacts a controller) and draws every element marked
 * data-wan-widget: "panel" is the uplink boxes (Equipment view's Internet),
 * "card" Home's summary (a column per uplink and the VPN). An element with data-nodes (a JSON list
 * of topology node IDs) shows only gateways among those nodes. Other scripts
 * read window.labratWan.gateways and subscribe() to updates.
 *
 * Used by: Home, Observations, Equipment and Diagram.
 */

"use strict";

/** How often the widgets ask for new readings; the collector polls the WAN every ~30 s. */
const WAN_POLL_MILLISECONDS = 30000;

/** Meters run on a log scale from this rate up to the link speed. */
const WAN_METER_FLOOR_BPS = 1000;

/** History buckets are five minutes; the sparkline spans a day. */
const WAN_BUCKET_SECONDS = 300;
const WAN_HISTORY_SECONDS = 86400;

/**
 * Create a text-only element.
 *
 * @param {string} tag - The element's tag name.
 * @param {string} className - Its class attribute; empty for none.
 * @param {*} [text=""] - Its text; empty leaves it empty.
 * @returns {HTMLElement} The element.
 */
function wanElement(tag, className, text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== "") element.textContent = String(text);
    return element;
}

/**
 * Format a bit rate, such as "3.2 Mb/s".
 *
 * @param {number|null} bps - Bits per second.
 * @returns {string} Readout text, or "--" when unknown.
 */
function wanRate(bps) {
    if (typeof bps !== "number" || !Number.isFinite(bps)) return "--";
    for (const [unit, size] of [["Gb/s", 1e9], ["Mb/s", 1e6], ["kb/s", 1e3]]) {
        if (bps >= size) {
            const value = bps / size;
            return `${value < 100 ? value.toFixed(1) : value.toFixed(0)} ${unit}`;
        }
    }
    return `${Math.round(bps)} b/s`;
}

/**
 * Format a link speed in Mb/s, such as "2.5 Gb/s".
 *
 * @param {number|null} mbps - Megabits per second.
 * @returns {string} Readout text.
 */
function wanLinkSpeed(mbps) {
    if (typeof mbps !== "number" || mbps <= 0) return "speed unknown";
    return mbps >= 1000 ? `${(mbps / 1000).toString().replace(/\.0$/, "")} Gb/s` : `${mbps} Mb/s`;
}

/**
 * Format a duration in seconds, such as "3 d 4 h" or "1 h 05 m".
 *
 * @param {number|null} seconds - Duration.
 * @returns {string} Readout text.
 */
function wanDuration(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "--";
    const s = Math.max(0, Math.floor(seconds));
    if (s >= 86400) return `${Math.floor(s / 86400)} d ${Math.floor(s / 3600) % 24} h`;
    if (s >= 3600) return `${Math.floor(s / 3600)} h ${String(Math.floor(s / 60) % 60).padStart(2, "0")} m`;
    return `${Math.floor(s / 60)} m`;
}

/**
 * Classify a gateway's internet state from its enabled uplinks.
 *
 * @param {object} gateway - Entry from /api/v1/wan.
 * @returns {{state: "online"|"degraded"|"down", label: string, active: object|null,
 *     backup: boolean, downCount: number}}
 */
function wanStatus(gateway) {
    const uplinks = (gateway.wan?.uplinks || []).filter((item) => item.enabled);
    const up = uplinks.filter((item) => item.up);
    const active = uplinks.find((item) => item.active) || up[0] || null;
    const backup = Boolean(active && uplinks.length && active !== uplinks[0]);
    const downCount = uplinks.length - up.length;
    if (uplinks.length && !up.length) return { state: "down", label: "Internet down", active: null, backup, downCount };
    if (downCount || backup) {
        return { state: "degraded", label: backup ? "On backup uplink" : "Uplink down", active, backup, downCount };
    }
    return { state: "online", label: "Internet online", active, backup, downCount };
}

/** Coordinate polling, state, and every WAN widget on the page. */
class LabratWanMonitor {
    /** Start empty; start() begins polling. */
    constructor() {
        this.gateways = null;
        this.listeners = new Set();
        this.shown = new Map();
    }

    /** Poll now and then on a timer; pause while the tab is hidden. */
    start() {
        if (!document.querySelector("[data-wan-widget]") && !document.getElementById("rack-app")) return;
        const tick = async () => {
            if (!document.hidden) await this.refresh();
            this.timer = setTimeout(tick, WAN_POLL_MILLISECONDS);
        };
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) this.refresh();
        });
        tick();
    }

    /** Fetch readings, redraw widgets, and notify subscribers. */
    async refresh() {
        try {
            this.gateways = (await labratJson(LABRAT_API.wan)).gateways || [];
        } catch {
            return;
        }
        this.render();
        for (const listener of this.listeners) listener(this.gateways);
    }

    /**
     * Call a function after each update.
     *
     * @param {function(Array<object>): void} listener - Receives the gateway list.
     * @returns {void}
     */
    subscribe(listener) {
        this.listeners.add(listener);
        if (this.gateways) listener(this.gateways);
    }

    /** Draw every widget on the page from the latest readings. */
    render() {
        if (!this.gateways) return;
        for (const element of document.querySelectorAll("[data-wan-widget]")) {
            let allowed = null;
            if (element.dataset.nodes) {
                try { allowed = new Set(JSON.parse(element.dataset.nodes)); } catch { allowed = new Set(); }
            }
            const gateways = this.gateways.filter((item) => !allowed || allowed.has(item.node_id));
            element.hidden = gateways.length === 0;
            const body = element.querySelector("[data-wan-body]") || element;
            body.replaceChildren(...gateways.map((gateway) => (
                element.dataset.wanWidget === "card" ? this.card(gateway) : this.panel(gateway)
            )));
        }
    }

    /**
     * Show a number that eases from its previous value to the new one.
     *
     * @param {string} key - Stable identity of this readout.
     * @param {number|null} value - New value.
     * @param {function(number|null): string} format - Text for a value.
     * @param {string} className - Element class.
     * @returns {HTMLElement} The readout element.
     */
    readout(key, value, format, className) {
        const element = wanElement("span", className, format(value));
        const previous = this.shown.get(key);
        this.shown.set(key, value);
        const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (typeof previous === "number" && typeof value === "number" && previous !== value && !reduced) {
            const started = performance.now();
            const step = () => {
                const progress = Math.min(1, (performance.now() - started) / 700);
                const eased = 1 - (1 - progress) ** 3;
                element.textContent = format(previous + (value - previous) * eased);
                if (progress < 1) requestAnimationFrame(step);
            };
            requestAnimationFrame(step);
        }
        return element;
    }

    /**
     * Build a bar meter on a log scale from 1 kb/s to the link speed.
     *
     * @param {string} key - Readout identity.
     * @param {string} label - Direction label ("Down" or "Up").
     * @param {number|null} bps - Current rate.
     * @param {number|null} linkMbps - Link speed, the meter's full scale.
     * @returns {HTMLElement} Meter row.
     */
    meter(key, label, bps, linkMbps) {
        const row = wanElement("div", "wan-meter");
        const full = Math.max(WAN_METER_FLOOR_BPS * 10, (linkMbps || 1000) * 1e6);
        const fraction = typeof bps === "number" && bps > WAN_METER_FLOOR_BPS
            ? Math.min(1, Math.log10(bps / WAN_METER_FLOOR_BPS) / Math.log10(full / WAN_METER_FLOOR_BPS)) : 0;
        const track = wanElement("span", "wan-meter-track");
        track.setAttribute("aria-hidden", "true");
        const fill = wanElement("span", "wan-meter-fill");
        fill.style.transform = `scaleX(${fraction.toFixed(4)})`;
        track.append(fill);
        row.append(wanElement("span", "wan-meter-label", label), track,
            this.readout(key, bps, wanRate, "wan-meter-value"));
        row.title = `${label}: ${wanRate(bps)} of a ${wanLinkSpeed(linkMbps)} link (log scale)`;
        return row;
    }

    /**
     * Draw a day of history: receive as a filled area, transmit as a line,
     * and amber ticks where an uplink was down. Scaled to the busiest bucket.
     *
     * @param {Array<Array<number|boolean|null>>} history - API history rows.
     * @param {string} className - SVG class.
     * @param {{detailed?: boolean}} [options] - ``detailed`` adds a gradient
     *     fill, labelled gridlines (peak and half), hour ticks, and a marker
     *     on the newest sample; the compact chart stays unlabelled.
     * @returns {HTMLElement} Figure with the chart and its scale label.
     */
    sparkline(history, className, { detailed = false } = {}) {
        const figure = wanElement("figure", `wan-sparkline ${className}`);
        const ns = "http://www.w3.org/2000/svg";
        const width = WAN_HISTORY_SECONDS / WAN_BUCKET_SECONDS;
        const height = 48;
        const svg = document.createElementNS(ns, "svg");
        svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
        svg.setAttribute("preserveAspectRatio", "none");
        svg.setAttribute("aria-hidden", "true");
        const now = Date.now() / 1000;
        const rows = (history || []).filter((row) => Array.isArray(row) && row[0] > now - WAN_HISTORY_SECONDS);
        const peak = Math.max(1, ...rows.map((row) => Math.max(row[1] || 0, row[2] || 0)));
        const x = (start) => Math.max(0, Math.min(width, (start - (now - WAN_HISTORY_SECONDS)) / WAN_BUCKET_SECONDS));
        const y = (value) => height - 2 - ((value || 0) / peak) * (height - 6);
        if (rows.length > 1) {
            const line = (index) => rows.map((row, i) => `${i ? "L" : "M"} ${x(row[0]).toFixed(1)} ${y(row[index]).toFixed(1)}`).join(" ");
            const area = document.createElementNS(ns, "path");
            area.setAttribute(
                "d", `${line(1)} L ${x(rows.at(-1)[0]).toFixed(1)} ${height} L ${x(rows[0][0]).toFixed(1)} ${height} Z`
            );
            area.setAttribute("class", "wan-spark-rx-area");
            if (detailed) {
                // A fill that fades toward the baseline, in the hue the page gives rx.
                this.gradientCount = (this.gradientCount || 0) + 1;
                const id = `wan-spark-fill-${this.gradientCount}`;
                const defs = document.createElementNS(ns, "defs");
                const gradient = document.createElementNS(ns, "linearGradient");
                gradient.setAttribute("id", id);
                gradient.setAttribute("x1", "0");
                gradient.setAttribute("y1", "0");
                gradient.setAttribute("x2", "0");
                gradient.setAttribute("y2", "1");
                for (const [offset, cls] of [["0", "wan-spark-stop-top"], ["1", "wan-spark-stop-bottom"]]) {
                    const stop = document.createElementNS(ns, "stop");
                    stop.setAttribute("offset", offset);
                    stop.setAttribute("class", cls);
                    gradient.append(stop);
                }
                defs.append(gradient);
                svg.append(defs);
                area.style.fill = `url(#${id})`;
            }
            const rx = document.createElementNS(ns, "path");
            rx.setAttribute("d", line(1));
            rx.setAttribute("class", "wan-spark-rx");
            const tx = document.createElementNS(ns, "path");
            tx.setAttribute("d", line(2));
            tx.setAttribute("class", "wan-spark-tx");
            svg.append(area, rx, tx);
        }
        for (const row of rows.filter((item) => item[5])) {
            const tick = document.createElementNS(ns, "rect");
            tick.setAttribute("x", x(row[0]).toFixed(1));
            tick.setAttribute("y", String(height - 3));
            tick.setAttribute("width", "1");
            tick.setAttribute("height", "3");
            tick.setAttribute("class", "wan-spark-degraded");
            svg.append(tick);
        }
        const caption = wanElement("figcaption", "wan-sparkline-caption");
        if (detailed) {
            caption.classList.add("is-detailed");
            for (const label of [
                "−24 h", "−18 h", "−12 h", "−6 h", "now"
            ]) caption.append(wanElement("span", "", label));
            const plot = wanElement("div", "wan-spark-plot");
            plot.append(svg);
            if (rows.length > 1) {
                // Overlay HTML labels and the "now" marker: the SVG stretches to fit,
                // so text and circles drawn inside it would distort.
                const top = wanElement("span", "wan-spark-ylabel is-top", wanRate(peak));
                const middle = wanElement("span", "wan-spark-ylabel is-middle", wanRate(peak / 2));
                const last = rows.at(-1);
                const marker = wanElement("span", "wan-spark-now");
                marker.style.left = `${(x(last[0]) / width * 100).toFixed(2)}%`;
                marker.style.top = `${(y(last[1]) / height * 100).toFixed(2)}%`;
                plot.append(top, middle, marker);
            }
            figure.append(plot, caption);
        } else {
            caption.append(
                wanElement("span", "", "24 h"),
                wanElement("span", "", rows.length > 1 ? `peak ${wanRate(peak)}` : "collecting history"),
                wanElement("span", "", "now"),
            );
            figure.append(svg, caption);
        }
        figure.title = "Last 24 hours: receive (filled) and transmit (line), five-minute averages";
        return figure;
    }

    /**
     * Build the Equipment view's Internet panel for one gateway: a box per
     * uplink and the traffic chart (no read-age line, owner).
     *
     * @param {object} gateway - Entry from /api/v1/wan.
     * @returns {HTMLElement} Panel content.
     */
    panel(gateway) {
        const status = wanStatus(gateway);
        const root = wanElement("div", "wan-cluster");
        root.dataset.state = status.state;
        // No overall status line, gateway name, or summary table: each
        // uplink's box says what matters (owner).
        for (const uplink of gateway.wan?.uplinks || []) {
            const card = wanElement("section", "wan-uplink");
            card.dataset.state = !uplink.enabled
                ? "disabled"
                : !uplink.up
                ? "down"
                : uplink.active
                ? "active"
                : "standby";
            const top = wanElement("header", "wan-uplink-head");
            const name = wanElement("span", "wan-uplink-name");
            // Boxes are named by provider, the port name (WAN1) as fallback.
            name.append(wanElement("span", "wan-lamp"), document.createTextNode(uplink.isp || uplink.name));
            if (uplink.isp) name.title = uplink.name;
            top.append(
                name,
                wanElement(
                    "span", "wan-uplink-role",
                    { active: "Active", standby: "Standby", down: "Down", disabled: "Disabled" }[card.dataset.state]
                ),
                // Port and speed only: the media name (GE, 2.5GE) repeats the speed (owner).
                wanElement(
                    "span", "wan-uplink-link",
                    [uplink.interface, wanLinkSpeed(uplink.speed_mbps)].filter(Boolean).join(" · ")
                ),
            );
            // The controller supplies one provider per circuit, including
            // standby links; do not reuse the gateway's active ISP here. The
            // public IP sits under the name, where the ISP used to be.
            card.append(top);
            if (uplink.ip) card.append(wanElement("p", "wan-uplink-ip", uplink.ip));
            card.append(
                this.meter(`${gateway.node_id}:${uplink.id}:rx`, "Down", uplink.rx_bps, uplink.speed_mbps),
                this.meter(`${gateway.node_id}:${uplink.id}:tx`, "Up", uplink.tx_bps, uplink.speed_mbps));
            const facts = wanElement("p", "wan-uplink-facts");
            facts.append(
                wanElement("span", "", `Latency ${typeof uplink.latency_ms === "number" ? `${uplink.latency_ms} ms` : "--"}`),
                wanElement("span", "", `Avail ${typeof uplink.availability === "number" ? `${uplink.availability}%` : "--"}`),
                wanElement("span", "", `Up ${wanDuration(uplink.uptime_s)}`),
            );
            card.append(facts);
            root.append(card);
        }
        root.append(this.sparkline(gateway.history, "is-large"));
        return root;
    }

    /**
     * Build Home's summary card for one gateway (owner: the detail the wide
     * section has room for): the overall state, then a column per uplink
     * (WAN1, WAN2: provider, role, IP, down, up, latency, uptime) and one for
     * the VPN (each tunnel's state, down, up, and its day of traffic), side
     * by side, and the gateway's 24-hour chart across the bottom.
     *
     * @param {object} gateway - Entry from /api/v1/wan.
     * @returns {HTMLElement} Card content.
     */
    card(gateway) {
        const status = wanStatus(gateway);
        const root = wanElement("div", "wan-card");
        root.dataset.state = status.state;
        const head = wanElement("div", "wan-card-head");
        // Just the state (owner): each column names its provider, port, and latency.
        head.append(wanElement("span", "wan-lamp"), wanElement("strong", "", status.label));
        const columns = wanElement("div", "wan-card-columns");
        for (const uplink of gateway.wan?.uplinks || []) columns.append(this.cardUplink(gateway, uplink));
        const vpn = this.cardVpn(gateway);
        if (vpn) columns.append(vpn);
        root.append(head, columns, this.sparkline(gateway.history, "is-small"));
        return root;
    }

    /**
     * One uplink's column on Home's card.
     *
     * @param {object} gateway - Entry from /api/v1/wan.
     * @param {object} uplink - One of its `wan.uplinks`.
     * @returns {HTMLElement} The column.
     */
    cardUplink(gateway, uplink) {
        const column = wanElement("section", "wan-card-column wan-uplink");
        column.dataset.state = !uplink.enabled
            ? "disabled"
            : !uplink.up
            ? "down"
            : uplink.active
            ? "active"
            : "standby";
        const top = wanElement("header", "wan-card-column-head");
        const name = wanElement("span", "wan-card-column-name");
        name.append(wanElement("span", "wan-lamp"), document.createTextNode(uplink.isp || uplink.name));
        top.append(name, wanElement("span", "wan-card-column-role",
            { active: "Active", standby: "Standby", down: "Down", disabled: "Disabled" }[column.dataset.state]));
        column.append(top, wanElement("p", "wan-card-column-sub",
            [uplink.isp ? uplink.name : null, uplink.ip].filter(Boolean).join(" · ") || "\u00a0"));
        column.append(this.cardPair(`${gateway.node_id}:${uplink.id}`, uplink.rx_bps, uplink.tx_bps));
        column.append(wanElement("p", "wan-card-column-facts", [
            `Latency ${typeof uplink.latency_ms === "number" ? `${uplink.latency_ms} ms` : "--"}`,
            `Up ${wanDuration(uplink.uptime_s)}`,
        ].join(" · ")));
        return column;
    }

    /**
     * The VPN column on Home's card: each tunnel's state, rates, and day of
     * traffic (collector `vpn_history`), or why there is none.
     *
     * @param {object} gateway - Entry from /api/v1/wan.
     * @returns {HTMLElement|null} The column, or null without VPN telemetry.
     */
    cardVpn(gateway) {
        const vpn = gateway.wan?.vpn;
        if (!vpn) return null;
        const column = wanElement("section", "wan-card-column wan-card-vpn");
        const sampled = Date.parse(vpn.sampled_at);
        const fresh = vpn.available && Number.isFinite(sampled) && Date.now() - sampled <= 120000;
        const top = wanElement("header", "wan-card-column-head");
        const name = wanElement("span", "wan-card-column-name");
        name.append(wanElement("span", "wan-lamp"), document.createTextNode("VPN"));
        const connections = vpn.connections || [];
        const up = connections.filter((item) => item.state === "connected").length;
        column.dataset.state = !fresh ? "standby" : up ? "active" : connections.length ? "down" : "standby";
        top.append(name, wanElement("span", "wan-card-column-role", !fresh ? "Stale"
            : connections.length ? `${up} of ${connections.length} up` : "No tunnels"));
        column.append(top);
        for (const connection of connections.slice(0, 2)) {
            column.append(wanElement("p", "wan-card-column-sub", `${connection.name} · ${connection.protocol || "tunnel"}`));
            column.append(this.cardPair(
                `vpn:${gateway.node_id}:${connection.id}`, connection.rx_bps, connection.tx_bps
            ));
            column.append(this.sparkline(gateway.vpn_history?.[connection.id] || [], "is-vpn"));
        }
        if (!connections.length) column.append(wanElement(
            "p", "wan-card-column-sub", "No client or site-to-site tunnels reported."
        ));
        return column;
    }

    /**
     * Down and up readouts side by side, easing between readings.
     *
     * @param {string} key - What the pair shows (a gateway's WAN or VPN
     *     connection), keying its eased readouts across redraws.
     * @param {?number} rx - Download rate in bits per second.
     * @param {?number} tx - Upload rate.
     * @returns {HTMLElement} The pair of readouts.
     */
    cardPair(key, rx, tx) {
        const stats = wanElement("div", "wan-card-stats");
        for (const [label, value, kind] of [["Down", rx, "rx"], ["Up", tx, "tx"]]) {
            const cell = wanElement("div", "wan-card-stat");
            cell.dataset.kind = kind;
            cell.append(
                this.readout(`${key}:${kind}`, value ?? null, wanRate, "wan-card-value"), wanElement("span", "", label)
            );
            stats.append(cell);
        }
        return stats;
    }
}

window.labratWan = new LabratWanMonitor();
window.labratWan.start();
