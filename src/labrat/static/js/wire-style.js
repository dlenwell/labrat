/* Used by: Discovery, Equipment and Diagram. */
/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */

/** Read a browser-only style, falling back when storage is absent or invalid.
 * @param {string} key - Page-specific storage key.
 * @param {string} fallback - Default style, square or curved.
 * @returns {string} Validated wire style.
 */
function labratReadWireStyle(key, fallback) {
    try {
        const value = localStorage.getItem(key);
        return ["square", "curved"].includes(value) ? value : fallback;
    } catch { return fallback; }
}

/** Save an optional preference without requiring browser storage.
 * @param {string} key - Page-specific storage key.
 * @param {string} style - Current wire style.
 * @returns {void}
 */
function labratSaveWireStyle(key, style) {
    try { localStorage.setItem(key, style); } catch { /* Optional preference. */ }
}

/** Keep the shared control's icon, tooltip and accessible state synchronized.
 * @param {HTMLElement} button - Wire style button.
 * @param {string} style - Current wire style.
 * @returns {void}
 */
function labratUpdateWireStyleButton(button, style) {
    if (!button) return;
    const curved = style === "curved";
    const label = `Wire style: ${curved ? 'Curved' : '90°'}. Switch to ${curved ? '90°' : 'Curved'}`;
    button.setAttribute("aria-pressed", String(curved));
    button.setAttribute("aria-label", label);
    button.title = label;
    button.dataset.tooltip = label;
    button.dataset.style = style;
}
