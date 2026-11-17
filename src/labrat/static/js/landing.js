/**
 * @fileoverview Sample cables on the landing page's rack preview.
 *
 * The preview (templates/public/index.html) is static sample data, not
 * inventory. Each cabled port lamp carries data-cable (a link name shared by
 * its two ends), data-medium, and data-speed (Mb/s). This draws each link
 * exactly as the Equipment view draws a same-face rack cable (racks.js
 * drawCables): out of the lamp's bottom, down below its device, out to a
 * lane in the channel just past the rack frame's right edge, down that
 * channel, and back in under the other lamp, with Equipment's rounded
 * turns (racks.js rackRoundedPath). Line width and the speed of the moving
 * pulses follow link speed, with the same steps as Equipment.
 * Colors come from the cable-medium tokens (landing.css).
 *
 * @copyright 2026 LabRat contributors
 * @license Apache-2.0
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Largest bend radius, as in Equipment (racks.js RACK_CABLE_BEND_RADIUS).
 * Each turn also stays within half of its shorter neighbouring run.
 */
const LANDING_BEND_RADIUS = 6;

/**
 * Build one cable's SVG path from the upper lamp to the lower one: the
 * square route Equipment builds, drawn with Equipment's rounded turns (a
 * quadratic curve at each corner, at most LANDING_BEND_RADIUS and at most
 * half of either neighbouring run: the shared `wirePath` in wiring.js).
 * Coordinates round to one decimal. DOM-free, so tests/js/landing-cables.mjs
 * checks it in Node.
 *
 * @param {{x: number, y: number, exitY: number}} from - Upper end: just
 *     under the lamp, and just under its device.
 * @param {{x: number, y: number, exitY: number}} to - Lower end, likewise.
 * @param {number} laneX - The cable's lane in the channel past the frame.
 * @returns {string} SVG path data with rounded turns.
 */
function landingCablePath(from, to, laneX) {
    return wirePath([
        { x: from.x, y: from.y }, { x: from.x, y: from.exitY }, { x: laneX, y: from.exitY },
        { x: laneX, y: to.exitY }, { x: to.x, y: to.exitY }, { x: to.x, y: to.y },
    ], true, LANDING_BEND_RADIUS, 1);
}

/**
 * Draw every sample cable over the preview, replacing any earlier drawing
 * (after a resize, for example). Lanes step outward from the frame in link
 * name order, 3 px apart, starting 12 px past its edge.
 *
 * @returns {void}
 */
function drawLandingCables() {
    const preview = document.querySelector(".rack-preview");
    const frame = preview?.querySelector(".rack-frame");
    if (!frame) return;
    preview.querySelector(".landing-cables")?.remove();
    const box = preview.getBoundingClientRect();
    const edge = frame.getBoundingClientRect().right - box.left;
    const lamps = [...preview.querySelectorAll("[data-cable]")];
    const names = [...new Set(lamps.map((lamp) => lamp.dataset.cable))].sort();
    const end = (lamp) => {
        const rect = lamp.getBoundingClientRect();
        const device = lamp.closest(".rack-device").getBoundingClientRect();
        return {
            x: rect.left + rect.width / 2 - box.left,
            y: rect.bottom + 2 - box.top,
            exitY: device.bottom + 1 - box.top,
        };
    };
    const namespace = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(namespace, "svg");
    svg.setAttribute("class", "landing-cables");
    svg.setAttribute("width", String(box.width));
    svg.setAttribute("height", String(box.height));
    svg.setAttribute("aria-hidden", "true");
    names.forEach((name, lane) => {
        const pair = lamps.filter((lamp) => lamp.dataset.cable === name)
            .sort((left, right) => left.getBoundingClientRect().top - right.getBoundingClientRect().top);
        if (pair.length !== 2) return;
        const d = landingCablePath(end(pair[0]), end(pair[1]), edge + 12 + lane * 3);
        const medium = pair[0].dataset.medium || "unknown";
        const style = wireSpeedStyle(Number(pair[0].dataset.speed) || 0);
        // The cable, then a dashed copy whose dashes move: the pulses.
        for (const flow of [false, true]) {
            const path = document.createElementNS(namespace, "path");
            path.setAttribute("d", d);
            path.dataset.medium = medium;
            if (flow) path.setAttribute("class", "landing-cable-flow");
            path.style.strokeWidth = String(style.width);
            if (flow) path.style.animationDuration = `${style.flow}s`;
            svg.append(path);
        }
    });
    preview.append(svg);
}

if (typeof document !== "undefined") {
    const preview = document.querySelector(".rack-preview");
    if (preview) {
        let pending = false;
        const queue = () => {
            if (pending) return;
            pending = true;
            requestAnimationFrame(() => {
                pending = false;
                drawLandingCables();
            });
        };
        if ("ResizeObserver" in window) new ResizeObserver(queue).observe(preview);
        window.addEventListener("resize", queue);
        document.fonts?.ready.then(queue);
        queue();
    }
}
