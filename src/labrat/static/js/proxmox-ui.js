// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
// Used by: Cluster, Networks.
/**
 * @file Page helpers the Cluster and Networks pages share that are not
 * widgets: remembered per-viewer preferences. Exposed as
 * window.LabratProxmox. The charts, bars, tiles, and panels these pages
 * draw are widgets (widgets.js, window.LabratWidgets).
 */
(() => {
    "use strict";

    /**
     * Read a remembered per-viewer preference.
     * @param {string} key Storage key.
     * @param {string} fallback Value when nothing is stored or storage fails.
     * @returns {string} The stored or fallback value.
     */
    function readPreference(key, fallback) {
        try {
            return window.localStorage.getItem(key) || fallback;
        } catch (error) {
            return fallback;
        }
    }

    /**
     * Remember a per-viewer preference, ignoring storage failures.
     * @param {string} key Storage key.
     * @param {string} value Value to store.
     */
    function writePreference(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (error) {
            // Private windows may refuse storage; the view still works.
        }
    }

    window.LabratProxmox = { readPreference, writePreference };
})();
