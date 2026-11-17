/*
 * Copyright 2026 LabRat contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * UPS helpers shared by the pages listed below. UPS readings
 * come from the NUT probe (collector/adapters/nut.py) as each observation's
 * ``ups`` record in /api/v1/topology. Load this before the page script.
 *
 * Used by: Observations, Discovery, Equipment and Diagram, Discovery clients, Floor plan.
 */

"use strict";

/** UPS readings older than this are shown as stale (fast reads arrive every ~30 s). */
const UPS_STALE_MILLISECONDS = 120000;

/**
 * Pick a UPS node's newest reading, with its observation.
 * @param {object} node - Topology node with ``observations[].ups``.
 * @returns {{reading: object, observation: object}|null} Newest reading, or null.
 */
function upsReading(node) {
    const found = (node.observations || []).filter(item => item.ups)
        .sort((a, b) => Date.parse(b.ups.sampled_at || 0) - Date.parse(a.ups.sampled_at || 0))[0];
    return found ? { reading: found.ups, observation: found } : null;
}

/**
 * Classify a UPS for lamps and alarms. Low battery is an alarm (red); running
 * on battery, overload, a battery needing replacement, or a reported ALARM is
 * a warning (amber). A relayed unit's ALARM comes from the relayed source.
 * @param {object} reading - ``observations[].ups`` record.
 * @returns {{level: string, label: string, alerts: Array<string>}} Lamp level
 *     (``alarm``, ``warning``, ``ok``, or ``unknown``), state label, and alert names.
 */
function upsCondition(reading) {
    const flags = new Set(reading.flags || []);
    const alerts = [
        flags.has("overloaded") && "Overloaded",
        flags.has("replace_battery") && "Replace battery",
        (flags.has("alarm") || reading.alarm) && (reading.alarm ? `Alarm: ${reading.alarm}` : "Alarm reported"),
        flags.has("forced_shutdown") && "Forced shutdown",
    ].filter(Boolean);
    const label = {
        low_battery: "Low battery", on_battery: "On battery", online: "Online", off: "Off", unknown: "State unknown"
    }[reading.state] || "State unknown";
    const level = reading.state === "low_battery" ? "alarm"
        : reading.state === "on_battery" || alerts.length ? "warning"
            : reading.state === "online" ? "ok" : "unknown";
    return { level, label, alerts };
}

/**
 * Format seconds of battery runtime, such as "1 h 40 m" or "12 m".
 * @param {number|null} seconds - Runtime.
 * @returns {string} Readout text.
 */
function upsRuntime(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "—";
    const minutes = Math.round(seconds / 60);
    return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')} m` : `${minutes} m`;
}
