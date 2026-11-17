/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * Test connection forms without navigating or returning secrets to the page.
 * Values the administrator typed stay in the form so a successful test can
 * be followed by Save. Without JavaScript the normal POST remains available.
 */

/**
 * Bind one integration form to a read-only, explicitly requested API test.
 * @param {HTMLFormElement} form - Connection editor and its CSRF field.
 * @param {object} [dependencies] - Injectable browser services for focused tests.
 * @returns {void}
 */
function bindIntegrationTest(form, dependencies = {}) {
    const send = dependencies.fetch || window.fetch.bind(window);
    const makeData = dependencies.makeData || ((element) => new FormData(element));
    const result = form.querySelector("[data-integration-result]");
    const controls = Array.from(form.querySelectorAll('button[type="submit"]'));
    let busy = false;
    let editVersion = 0;
    form.addEventListener("input", () => {
        editVersion += 1;
        result.hidden = true;
        result.textContent = "";
    });
    form.addEventListener("submit", async (event) => {
        if (busy) {
            event.preventDefault();
            return;
        }
        if (!event.submitter?.hasAttribute("data-integration-test")) return;
        event.preventDefault();
        busy = true;
        const payload = makeData(form);
        const testedVersion = editVersion;
        controls.forEach((control) => { control.disabled = true; });
        result.hidden = false;
        result.textContent = "Testing connection…";
        result.dataset.state = "pending";
        try {
            const response = await send(form.dataset.testUrl, {
                method: "POST", body: payload, credentials: "same-origin",
                headers: { Accept: "application/json" },
            });
            const data = await response.json();
            if (typeof data.message !== "string") throw new Error("Invalid test response");
            result.hidden = false;
            if (testedVersion !== editVersion) {
                result.textContent = "The form changed during the test. Test the current settings again.";
                result.dataset.state = "pending";
            } else {
                result.textContent = data.message;
                result.dataset.state = response.ok && data.ok ? "success" : "error";
            }
        } catch {
            result.textContent = "The test could not complete. Check your connection or reload the page and try again.";
            result.dataset.state = "error";
        } finally {
            busy = false;
            controls.forEach((control) => { control.disabled = false; });
        }
    });
}


/**
 * Match catalog entries without making case or accents affect discovery.
 * @param {string} label - Public catalog label/category/description.
 * @param {string} query - User's search text.
 * @returns {boolean} Whether every query word occurs in the entry.
 */
function integrationSearchMatches(label, query) {
    const normalize = (value) => value.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
    const text = normalize(label);
    return normalize(query).trim().split(/\s+/).filter(Boolean).every((word) => text.includes(word));
}

/** Shared drawer controller for the Settings catalog and reusable form. */
class IntegrationSettingsDrawer {
    /**
     * @param {HTMLElement} host - Settings section with catalog and drawer markup.
     * @param {Document} [doc] - Owning document.
     */
    constructor(host, doc = document) {
        this.host = host;
        this.doc = doc;
        this.slot = host.querySelector("[data-integration-editor-slot]");
        this.title = doc.getElementById("integration-connection-title");
        this.add = host.querySelector("[data-integration-add]");
        this.opener = this.add;
        this.pending = null;
        this.drawer = new DeviceDrawer({
            host, doc, rootView: "closed", closeAtRoot: true,
            keepsOpen: (target) => Boolean(target.closest("[data-integration-launch], [data-integration-add]")),
            onActivate: (view) => {
                if (view !== "connection") {
                    this.pending?.abort();
                    this.slot.replaceChildren();
                    history.replaceState(null, "", host.dataset.overviewUrl + "&catalog=1");
                }
                if (this.drawer?.isOpen) doc.getElementById("device-drawer-title").focus({preventScroll:true});
            },
            onClose: () => {
                this.pending?.abort();
                this.slot.replaceChildren();
                history.replaceState(null, "", host.dataset.overviewUrl);
                this.opener?.focus({preventScroll:true});
            },
        });
        this.drawer.element.setAttribute("role", "dialog");
        this.bindNavigation();
        this.bindSearch();
        this.bindFilter();
        const initial = this.slot.querySelector("[data-integration-editor]");
        if (initial) {
            this.bindEditor(initial);
            const stack = initial.dataset.editorNew === "1" ? [
                "add", "category-" + initial.dataset.editorCategory
            ] : [];
            this.drawer.activate("connection");
            this.drawer.reset("connection", stack);
            this.drawer.open("device-drawer-title");
        } else if (host.dataset.openCatalog === "1") {
            this.openCatalog();
        }
    }

    /** Bind one plus control, category submenus, and add/edit links. */
    bindNavigation() {
        this.add?.addEventListener("click", (event) => {
            event.preventDefault();
            this.opener = this.add;
            this.openCatalog();
        });
        this.host.addEventListener("click", (event) => {
            const category = event.target.closest("[data-integration-category]");
            if (category) {
                this.drawer.push("category-" + category.dataset.integrationCategory);
                return;
            }
            const link = event.target.closest("[data-integration-launch]");
            if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            if (!this.drawer.element.contains(link)) this.opener = link;
            this.loadEditor(link.href);
        });
    }

    /** Open the root of the searchable integration catalog. */
    openCatalog() {
        this.pending?.abort();
        this.drawer.activate("add");
        this.drawer.reset("add");
        this.drawer.open("integration-search");
    }

    /** Filter the shared catalog while retaining category navigation when empty. */
    bindSearch() {
        const search = this.host.querySelector("[data-integration-search]");
        const categories = this.host.querySelector("[data-integration-categories]");
        const results = this.host.querySelector("[data-integration-search-results]");
        const entries = [...results.querySelectorAll("[data-integration-search-item]")];
        search.addEventListener("input", () => {
            const query = search.value.trim();
            categories.hidden = Boolean(query);
            results.hidden = !query;
            let count = 0;
            entries.forEach((entry) => {
                entry.hidden = !integrationSearchMatches(entry.dataset.integrationSearchItem, query);
                if (!entry.hidden) count += 1;
            });
            results.querySelector("[data-integration-no-results]").hidden = count !== 0;
        });
    }

    /** Filter existing connections by their public name, type, or endpoint. */
    bindFilter() {
        const filter = this.host.querySelector("[data-integration-filter]");
        if (!filter) return;
        const rows = [...this.host.querySelectorAll("[data-connection-row]")];
        const empty = this.host.querySelector("[data-integration-filter-empty]");
        filter.addEventListener("input", () => {
            let visible = 0;
            rows.forEach((row) => {
                row.hidden = !integrationSearchMatches(row.dataset.connectionRow, filter.value);
                if (!row.hidden) visible += 1;
            });
            empty.hidden = visible !== 0;
        });
    }

    /**
     * Load a server-rendered form into the same drawer; never populate a secret.
     * @param {string} href - Same-origin Settings editor link from the catalog.
     * @returns {Promise<void>} Completion of the bounded editor load.
     */
    async loadEditor(href) {
        const url = new URL(href, location.href);
        const overview = new URL(this.host.dataset.overviewUrl, location.href);
        if (url.origin !== location.origin || url.pathname !== overview.pathname) return;
        this.pending?.abort();
        const request = new AbortController();
        this.pending = request;
        const stack = url.searchParams.get("is_new") === "1" && this.drawer.isOpen
            ? [...this.drawer.viewStack, this.drawer.currentView] : [];
        const loading = this.doc.createElement("p");
        loading.className = "integration-note";
        loading.textContent = "Loading connection…";
        this.slot.replaceChildren(loading);
        this.slot.parentElement.scrollTop = 0;
        this.title.textContent = "Connection";
        this.drawer.activate("connection", this.drawer.isOpen ? "forward" : "none");
        this.drawer.reset("connection", stack);
        this.drawer.open("device-drawer-title");
        try {
            const response = await fetch(url.href, {credentials:"same-origin", signal:request.signal});
            if (!response.ok) throw new Error("Editor unavailable");
            const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
            const editor = parsed.querySelector("[data-integration-editor]");
            if (!editor) throw new Error("Editor unavailable");
            if (request.signal.aborted || this.pending !== request) return;
            const imported = this.doc.importNode(editor, true);
            this.slot.replaceChildren(imported);
            this.title.textContent = imported.dataset.editorTitle;
            this.drawer.updateBar();
            this.bindEditor(imported);
            history.replaceState(null, "", url.href);
            const focus = imported.querySelector('#integration-display-name, input[type="checkbox"]');
            focus?.focus({preventScroll:true});
        } catch {
            if (request.signal.aborted || this.pending !== request) return;
            loading.textContent = "Could not open this connection. Close the drawer and try again, or reload the page.";
        } finally {
            if (this.pending === request) this.pending = null;
        }
    }

    /**
     * Bind inline testing and the shared Back/Close behavior to an editor.
     * @param {HTMLElement} editor - Safe form markup returned by Settings.
     */
    bindEditor(editor) {
        const form = editor.querySelector("[data-integration-form]");
        if (form) bindIntegrationTest(form);
        editor.querySelector("[data-integration-cancel]").addEventListener("click", () => {
            if (this.drawer.viewStack.length) this.drawer.back();
            else this.drawer.close();
        });
    }
}

if (typeof document !== "undefined") {
    document.querySelectorAll("[data-integration-settings]").forEach((host) => new IntegrationSettingsDrawer(host));
}
