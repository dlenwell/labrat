/**
 * @fileoverview The landing page's screenshot viewer. Selecting a
 * screenshot opens it full size in a dialog with its caption; the
 * previous and next buttons, the arrow keys, or a sideways swipe step
 * through the rest, and Escape, the close button, or a click on the
 * backdrop closes it. Without scripts the page still shows every
 * screenshot in the grid.
 *
 * @copyright 2026 LabRat contributors
 * @license Apache-2.0
 * SPDX-License-Identifier: Apache-2.0
 */

/** Fewest pixels a horizontal swipe must travel to change screenshots. */
const SITE_SWIPE_PIXELS = 40;

/**
 * Open and step through the page's screenshots.
 */
class SiteViewer {
    /**
     * Wire the viewer to the dialog and the grid's screenshots.
     *
     * @param {HTMLDialogElement} dialog - The viewer dialog.
     * @param {HTMLElement[]} figures - The screenshots, in page order.
     */
    constructor(dialog, figures) {
        this.dialog = dialog;
        this.figures = figures;
        this.index = 0;
        this.image = dialog.querySelector(".site-viewer-image");
        this.caption = dialog.querySelector(".site-viewer-caption");
        this.count = dialog.querySelector(".site-viewer-count");
        this.opener = null;
        figures.forEach((figure, index) => {
            const button = figure.querySelector(".site-shot-open");
            const label = figure.querySelector("img").alt;
            button.setAttribute("aria-label", `View larger: ${label}`);
            button.addEventListener("click", () => this.open(index, button));
        });
        dialog.querySelector(".site-viewer-prev").addEventListener("click", () => this.step(-1));
        dialog.querySelector(".site-viewer-next").addEventListener("click", () => this.step(1));
        dialog.querySelector(".site-viewer-close").addEventListener("click", () => dialog.close());
        dialog.addEventListener("click", (event) => {
            // A click on the backdrop lands on the dialog itself.
            if (event.target === dialog) dialog.close();
        });
        dialog.addEventListener("keydown", (event) => {
            if (event.key === "ArrowLeft") this.step(-1);
            if (event.key === "ArrowRight") this.step(1);
        });
        dialog.addEventListener("close", () => this.opener?.focus());
        this.watchSwipes();
    }

    /**
     * Show one screenshot in the viewer, opening it if needed.
     *
     * @param {number} index - Which screenshot.
     * @param {HTMLElement|null} [opener] - What to focus again on close.
     * @returns {void}
     */
    open(index, opener = null) {
        if (opener) this.opener = opener;
        this.show(index);
        if (!this.dialog.open) this.dialog.showModal();
    }

    /**
     * Put a screenshot and its caption in the viewer.
     *
     * @param {number} index - Which screenshot; wraps around at either end.
     * @returns {void}
     */
    show(index) {
        const total = this.figures.length;
        this.index = (index + total) % total;
        const figure = this.figures[this.index];
        const source = figure.querySelector("img");
        this.image.src = source.currentSrc || source.src;
        this.image.alt = source.alt;
        this.caption.replaceChildren(...figure.querySelector("figcaption").cloneNode(true).childNodes);
        this.count.textContent = `${this.index + 1} / ${total}`;
    }

    /**
     * Move to the previous or next screenshot.
     *
     * @param {number} delta - -1 for previous, 1 for next.
     * @returns {void}
     */
    step(delta) {
        this.show(this.index + delta);
    }

    /**
     * Change screenshots on a sideways swipe across the image.
     *
     * @returns {void}
     */
    watchSwipes() {
        let startX = null;
        this.image.addEventListener("pointerdown", (event) => { startX = event.clientX; });
        this.image.addEventListener("pointerup", (event) => {
            if (startX === null) return;
            const travel = event.clientX - startX;
            startX = null;
            if (Math.abs(travel) >= SITE_SWIPE_PIXELS) this.step(travel < 0 ? 1 : -1);
        });
    }
}

const siteViewerDialog = document.querySelector(".site-viewer");
if (siteViewerDialog && typeof siteViewerDialog.showModal === "function") {
    new SiteViewer(siteViewerDialog, [...document.querySelectorAll(".site-shot")]);
}
