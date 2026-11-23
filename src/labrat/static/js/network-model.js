// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
// Used by: Home, Networks, Widgets.
/**
 * @file Facts derived from /api/v1/network (one source's network in the
 * shared form, api/network_shape.py). Pure functions, shared by the Networks page (networks.js) and
 * the widget feeds (widget-feeds.js), and exposed as
 * window.LabratNetworkModel.
 */
(() => {
    "use strict";

    /**
     * The band a radio's reported frequency band falls in, as the
     * neighbour rows name it ("2.4", "5", or "6").
     * @param {number} band Radio band in GHz.
     * @returns {string} Band name.
     */
    function bandName(band) {
        return band === 2.4 ? "2.4" : band >= 5.9 ? "6" : "5";
    }

    /**
     * Your access points: devices with the access point role (a console
     * that also routes is still one).
     * @param {object} snapshot Shared-form network.
     * @returns {Array<object>} Access points.
     */
    function accessPoints(snapshot) {
        return (snapshot.devices || []).filter((device) => (device.roles || []).includes("access_point"));
    }

    /**
     * Neighbouring Wi-Fi networks per channel, one group per band (2.4,
     * 5, then 6 GHz), with the channels your own APs use marked: input for
     * the columnGroups widget.
     * @param {object} snapshot Shared-form network with `neighbours` and
     *     `devices`.
     * @returns {Array<{label: string, rows: Array<object>}>} Groups.
     */
    function channelGroups(snapshot) {
        const rows = snapshot.neighbours || [];
        const mine = new Set(accessPoints(snapshot).flatMap((device) => (device.radios
            || []).map((radio) => `${bandName(radio.band)}:${radio.channel}`)));
        return ["2.4", "5", "6"].map((band) => ({
            label: `${band} GHz`,
            rows: rows.filter((row) => row.band === band).map((row) => {
                const own = mine.has(`${row.band}:${row.channel}`);
                return {
                    label: String(row.channel), value: row.count, mark: own,
                    tip: [`Channel ${row.channel} (${row.band} GHz)`, `${row.count} neighbouring network${row.count === 1 ? "" : "s"}`, own ? "One of your APs uses this channel" : ""].filter(Boolean),
                };
            }),
        })).filter((group) => group.rows.length);
    }

    /**
     * Whether a shared section's data is current, and what to say if not.
     * A failed refresh keeps the last good reading (with its own sample
     * time), which must not pass for a current one; a first failure has
     * nothing to show.
     * @param {object|undefined} section `{status, sampled_at}` from `sections`.
     * @returns {{current: boolean, label: string|null, since: string|null}}
     *     current is false for failed and not available sections; label
     *     says why; since is the time of the reading still shown.
     */
    function sectionState(section) {
        const status = section ? section.status : "not_available";
        if (status === "complete") {
            return { current: true, label: null, since: null };
        }
        if (status === "partial") {
            return { current: true, label: "partly read", since: null };
        }
        if (status === "failed") {
            return section.sampled_at
                ? { current: false, label: "last refresh failed", since: section.sampled_at }
                : { current: false, label: "could not be read", since: null };
        }
        return { current: false, label: "not reported", since: null };
    }

    /**
     * The Gateways panel's facts. Status is current only when the gateway
     * section is and the firewall's latest poll succeeded (`available`);
     * otherwise rows are last known, greyed, without the default badge. An
     * unmonitored gateway (dpinger off) shows its reported status but is
     * never counted as up.
     * @param {object} snapshot Shared-form network.
     * @param {Function} [when] Formats a sample time (the page passes `ago`).
     * @returns {{current: boolean, aside: string, rows: Array<object>}} rows
     *     hold `gateway`, `tone` (a lamp tone), `label`, and `showDefault`.
     */
    function gatewayView(snapshot, when = (time) => time) {
        const reported = (snapshot.gateways || []).filter((gateway) => gateway.status !== undefined);
        const section = (snapshot.sections || {}).gateways;
        const state = sectionState(section);
        const current = state.current && snapshot.available !== false;
        const monitored = reported.filter((gateway) => gateway.monitored);
        const up = monitored.filter((gateway) => gateway.status === "none").length;
        const unmonitored = reported.length - monitored.length;
        let aside;
        if (!current) {
            const since = state.since || (section && section.sampled_at) || null;
            aside = `${state.current ? "latest poll failed" : state.label}${since ? `; last known from ${when(since)}` : ""}`;
        } else if (monitored.length) {
            aside = `${up} of ${monitored.length} monitored up${unmonitored ? ` · ${unmonitored} not monitored` : ""}`;
        } else {
            aside = `${unmonitored} not monitored`;
        }
        return {
            current, aside,
            rows: reported.map((gateway) => {
                const word = gateway.label || gateway.status || "unknown";
                return {
                    gateway, showDefault: current && Boolean(gateway.default),
                    tone: !current || !gateway.monitored ? "off"
                        : { none: "on", down: "alarm", force_down: "off" }[gateway.status] || "warn",
                    label: !current ? `last known: ${word}` : gateway.monitored ? word : `${word} (not monitored)`,
                };
            }),
        };
    }

    /** Headings of rule evaluation stages. */
    const RULE_STAGES = {
        floating: "Floating", group: "Interface groups", interface: "Interfaces", system: "Automatic system rules",
    };

    /** Address family words for a rule's reported IP version. */
    const FAMILIES = { inet: "IPv4", inet6: "IPv6", inet46: "IPv4+IPv6" };

    /**
     * A router's reported rules (no verdicts) in their reported evaluation
     * order, split into consecutive runs of one stage (floating, interface
     * groups, interfaces, automatic system rules): automatic rules sit at
     * both ends of the ruleset, so a stage can appear more than once and
     * nothing is moved. A rule's marks say what changes how it reads: not
     * quick (a later match can override it), not yet migrated (an
     * old-format rule), disabled. Its match line lists the reported fields
     * LabRat keeps (address family, protocol, source, destination, ports);
     * other criteria a rule may have are not shown.
     * @param {object} snapshot Shared-form network.
     * @returns {Array<{scope: string, label: string, rules: Array<object>}>}
     *     Runs in order; each rule adds `marks` (words) and `match`.
     */
    function ruleGroups(snapshot) {
        const side = (endpoint, word) => {
            const values = (endpoint.values || []).join(", ") || "any";
            const ports = (endpoint.ports || []).length ? ` port ${endpoint.ports.join(", ")}` : "";
            return `${word} ${endpoint.negate ? "not " : ""}${values}${ports}`;
        };
        const runs = [];
        for (const rule of [...(snapshot.policies || [])].sort((a, b) => (a.order || 0) - (b.order || 0))) {
            const scope = rule.scope || "system";
            const shown = {
                ...rule,
                marks: [
                    rule.quick === false ? "not quick" : null, rule.legacy ? "not yet migrated" : null,
                    rule.enabled ? null : "disabled",
                ].filter(Boolean),
                match: [FAMILIES[rule.ip_version] || rule.ip_version || "any family",
                    rule.protocol && rule.protocol !== "any" ? rule.protocol : "any protocol",
                    side(rule.source || {}, "from"), side(rule.destination || {}, "to")].join(" "),
            };
            const last = runs[runs.length - 1];
            if (last && last.scope === scope) {
                last.rules.push(shown);
            } else {
                runs.push({ scope, label: RULE_STAGES[scope] || scope, rules: [shown] });
            }
        }
        return runs;
    }

    /**
     * Whether an Overview readout is current: its section complete or
     * partial, and the source's latest poll successful. Otherwise it is the
     * last known reading (with its time), or nothing could be read yet.
     * @param {object} snapshot Shared-form network.
     * @param {string} name The section (such as `vpn`, `services`, `carp`).
     * @returns {{current: boolean, note: string|null, since: string|null,
     *     unread: boolean}} note says why it is not current; unread is true
     *     when nothing was ever read (a first failure).
     */
    function readoutState(snapshot, name) {
        const section = (snapshot.sections || {})[name];
        const state = sectionState(section);
        if (state.current && snapshot.available !== false) {
            return { current: true, note: null, since: null, unread: false };
        }
        const since = state.since || (section && section.status !== "failed" && section.sampled_at) || null;
        return {
            current: false, since, unread: !since && Boolean(section) && section.status === "failed",
            note: state.current ? "latest poll failed" : state.label,
        };
    }

    /**
     * How a WireGuard peer stands, by its last handshake: a peer with
     * persistent keepalive handshakes about every two minutes, so five
     * minutes without one is stale; a peer without keepalive legitimately
     * goes quiet (idle); one that never handshook is "never connected".
     * @param {object} peer Shared `vpn` entry (`handshake` in Unix seconds,
     *     `keepalive` in seconds or null).
     * @param {number} [now] Current epoch milliseconds, injectable in tests.
     * @returns {{key: string, label: string, tone: string}} key is
     *     `never`, `recent`, `stale`, or `idle`; tone is a lamp tone.
     */
    function peerState(peer, now = Date.now()) {
        // Callers pass the reading's own time for a last-known reading.
        if (!peer.handshake) {
            return { key: "never", label: "never connected", tone: "off" };
        }
        const age = now / 1000 - peer.handshake;
        if (age <= 300) {
            return { key: "recent", label: "handshake recent", tone: "on" };
        }
        return peer.keepalive
            ? { key: "stale", label: "no handshake for over 5 minutes", tone: "warn" }
            : { key: "idle", label: "idle (no keepalive)", tone: "off" };
    }

    window.LabratNetworkModel = {
        bandName, accessPoints, channelGroups, sectionState, gatewayView, ruleGroups, readoutState, peerState,
    };
})();
