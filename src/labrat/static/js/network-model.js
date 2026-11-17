// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
// Used by: Home, Networks, Widgets.
/**
 * @file Facts derived from /api/v1/network (one source's curated
 * snapshot). Pure functions, shared by the Networks page (networks.js) and
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
     * Your access points: devices with the access point feature.
     * @param {object} snapshot Curated snapshot.
     * @returns {Array<object>} Access points.
     */
    function accessPoints(snapshot) {
        return (snapshot.devices || []).filter((device) => (device.features || []).includes("accessPoint"));
    }

    /**
     * Neighbouring Wi-Fi networks per channel, one group per band (2.4,
     * 5, then 6 GHz), with the channels your own APs use marked: input for
     * the columnGroups widget.
     * @param {object} snapshot Curated snapshot with `neighbours` and
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

    window.LabratNetworkModel = { bandName, accessPoints, channelGroups };
})();
