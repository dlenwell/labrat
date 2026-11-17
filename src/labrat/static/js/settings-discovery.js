/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @fileoverview Discovery controls use the shared drawer and safe server forms.
 * The browser never interprets firewall rules or grants permission from an
 * indicator. Only the submitted allowlist changes scan permission.
 */
class DiscoverySettingsDrawer {
    /**
     * Bind activity navigation, explicit toggles, and nested evidence details.
     *
     * @param {HTMLElement} host - The Discovery panel.
     * @param {Document} [doc=document] - The document (tests pass a stand-in).
     */
    constructor(host, doc = document) {
        this.host = host;
        this.doc = doc;
        this.pending = null;
        this.opener = null;
        this.urls = {};
        this.slots = Object.fromEntries([
            "menu", "editor", "detail"
        ].map((view) => [view, host.querySelector(`[data-discovery-slot="${view}"]`)]));
        this.drawer = new DeviceDrawer({host, doc, rootView:"closed", closeAtRoot:true,
            keepsOpen: (target) => Boolean(target.closest("[data-discovery-open]")),
            onActivate: (view) => {
                if (view === "menu") this.slots.editor.replaceChildren();
                if (view !== "detail") this.slots.detail.replaceChildren();
                if (this.urls[view]) history.replaceState(null, "", this.urls[view]);
            },
            onClose: () => {
                this.pending?.abort();
                Object.values(this.slots).forEach((slot) => slot.replaceChildren());
                history.replaceState(null, "", host.dataset.overviewUrl);
                this.opener?.focus({preventScroll:true});
            },
        });
        this.drawer.element.setAttribute("role", "dialog");
        host.querySelectorAll(".discovery-toggle-form").forEach((form) => {
            form.querySelector(".discovery-toggle-save").hidden = true;
            form.querySelector('[role="switch"]').addEventListener("change", () => form.requestSubmit());
        });
        host.addEventListener("click", (event) => {
            const link = event.target.closest("[data-discovery-open]");
            if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            if (!this.drawer.element.contains(link)) this.opener = link;
            this.load(link.href);
        });
        const initial = host.querySelector("[data-discovery-editor]");
        if (initial) {
            const view = initial.dataset.viewName;
            this.urls[view] = location.href;
            this.bindEditor(initial);
            this.drawer.activate(view);
            this.drawer.reset(view);
            this.drawer.open("device-drawer-title");
        }
    }

    /**
     * Fetch a same-origin Settings fragment into the existing drawer shell.
     *
     * @param {string} href - The fragment's address; only the panel's own page
     *     is loaded.
     * @returns {Promise<void>} Resolves once shown, or ignored.
     */
    async load(href) {
        const url = new URL(href, location.href);
        const overview = new URL(this.host.dataset.overviewUrl, location.href);
        if (url.origin !== overview.origin || url.pathname !== overview.pathname) return;
        this.pending?.abort();
        const request = new AbortController();
        this.pending = request;
        const wasOpen = this.drawer.isOpen;
        const previous = this.drawer.currentView;
        try {
            const response = await fetch(url.href, {credentials:"same-origin", signal:request.signal});
            if (!response.ok) throw new Error("Unavailable editor");
            const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
            const editor = parsed.querySelector("[data-discovery-editor]");
            if (!editor) throw new Error("Unavailable editor");
            if (request.signal.aborted || this.pending !== request) return;
            const view = editor.dataset.viewName;
            if (!this.slots[view]) return;
            const stack = wasOpen
                && (view === "detail" && previous === "editor" || view === "editor" && previous === "menu")
                ? [previous]
                : [];
            const imported = this.doc.importNode(editor, true);
            this.slots[view].replaceChildren(imported);
            this.host.querySelector(`[data-discovery-title="${view}"]`).textContent = imported.dataset.title;
            this.urls[view] = url.href;
            this.drawer.activate(view, wasOpen ? "forward" : "none");
            this.drawer.reset(view, stack);
            this.drawer.open("device-drawer-title");
            this.bindEditor(imported);
        } catch {
            if (request.signal.aborted || this.pending !== request) return;
            const note = this.doc.createElement("p");
            note.className = "form-error";
            note.textContent = "Could not open discovery settings. Close the panel and try again.";
            this.slots.editor.replaceChildren(note);
            this.drawer.activate("editor");
            this.drawer.reset("editor");
            this.drawer.open("device-drawer-title");
        } finally {
            if (this.pending === request) this.pending = null;
        }
    }

    /**
     * Keep duplicate CIDRs synchronized and preserve drafts through evidence drill-down.
     *
     * @param {HTMLElement} editor - The loaded editor fragment.
     */
    bindEditor(editor) {
        editor.querySelector("[data-discovery-cancel]")?.addEventListener("click", () => {
            if (this.drawer.viewStack.length) this.drawer.back();
            else this.drawer.close();
        });
        const permissions = [...editor.querySelectorAll('input[name="allowed_cidrs"]')];
        permissions.forEach((input) => input.addEventListener("change", () => {
            permissions.filter((peer) => peer.value === input.value).forEach((peer) => { peer.checked = input.checked; });
        }));
        editor.querySelector("[data-subnet-filter]")?.addEventListener("input", (event) => {
            const query = event.target.value.trim().toLowerCase();
            editor.querySelectorAll("[data-subnet-row]").forEach((row) => {
                row.hidden = !query.split(/\s+/).every((word) => row.dataset.subnetRow.toLowerCase().includes(word));
            });
        });
    }
}
if (typeof document !== "undefined") {
    document.querySelectorAll("[data-discovery-settings]").forEach((host) => new DiscoverySettingsDrawer(host));
}
