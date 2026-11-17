/**
 * @fileoverview Shared helpers loaded on every page, and the landing-page
 * API status indicator.
 *
 * This frontend uses only LabRat's JSON API, not direct network-device APIs.
 * @copyright 2026 LabRat contributors
 * @license Apache-2.0
 * SPDX-License-Identifier: Apache-2.0
 *
 * Used by: every page.
 */

/**
 * Every JSON API address the pages use, in one place. Plain entries are
 * collections; the functions build one record's address, to which a page
 * may add an action such as `/delete` or `/layout`. Pages that are handed
 * an address by their template (Cluster, Networks, widget feeds) use that.
 *
 * Used by: every page.
 */
const LABRAT_API = Object.freeze({
  health: "/api/v1/health",
  topology: "/api/v1/topology",
  wan: "/api/v1/wan",
  sources: "/api/v1/sources",
  locations: "/api/v1/locations",
  cluster: "/api/v1/cluster",
  clients: "/api/v1/clients",
  placements: "/api/v1/placements",
  rackDevices: "/api/v1/rack-devices",
  rackArrangement: "/api/v1/rack-arrangement",
  neighborMatches: "/api/v1/neighbor-matches",
  observationIgnores: "/api/v1/observation-ignores",
  /** @param {string} kind - "sites" or "rooms". @param {number} id - Its ID. @returns {string} The address. */
  place: (kind, id) => `/api/v1/${kind}/${id}`,
  /** @param {number} id - Room ID. @returns {string} The room's address. */
  room: (id) => `/api/v1/rooms/${id}`,
  /** @param {number} id - Rack ID. @returns {string} The rack's address. */
  rack: (id) => `/api/v1/racks/${id}`,
  /** @param {number} id - Container ID. @returns {string} The container's address. */
  container: (id) => `/api/v1/containers/${id}`,
  /** @param {number} id - Inventory device ID. @returns {string} The device's address. */
  rackDevice: (id) => `/api/v1/rack-devices/${id}`,
  /** @param {number} id - Client observation ID. @returns {string} The client's address. */
  client: (id) => `/api/v1/clients/${id}`,
  /** @param {number} id - Match ID. @returns {string} The match's address. */
  neighborMatch: (id) => `/api/v1/neighbor-matches/${id}`,
  /** @param {number} id - Rule ID. @returns {string} The rule's address. */
  observationIgnore: (id) => `/api/v1/observation-ignores/${id}`,
});

/**
 * Read or write LabRat's JSON API through the signed browser session.
 *
 * Every page's API calls go through here, so a reply is read the same way
 * everywhere: a failed request throws the server's own `error` message.
 * With a payload it POSTs JSON with the session's CSRF token; without one
 * it GETs.
 *
 * @param {string} path - Same-origin API path, such as "/api/v1/topology".
 * @param {object} [options] - Request options.
 * @param {object|null} [options.payload=null] - JSON body; given, the
 *     request is a POST.
 * @param {string} [options.csrfToken=""] - The page's CSRF token, needed
 *     for every write.
 * @param {AbortSignal} [options.signal] - Cancels the request.
 * @param {string} [options.failure] - Message when the server gives none.
 * @returns {Promise<object>} The parsed reply.
 * @throws {Error} On a network error, an unreadable reply, or an HTTP
 *     error status (then with the code as `status`; 401 means signed out).
 */
async function labratJson(path, { payload = null, csrfToken = "", signal, failure } = {}) {
  const options = { credentials: "same-origin", headers: { Accept: "application/json" }, signal };
  if (payload !== null) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = csrfToken;
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error(`The server returned an unreadable response (${response.status}).`);
  }
  if (!response.ok) {
    const error = new Error(body.error || failure || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

/**
 * Format a port speed for display, using the same units on every page.
 *
 * @param {number|string|null|undefined} mbps - Speed in megabits per second.
 * @returns {string} For example "100 Mb/s", "2.5 Gb/s", or "10 Gb/s";
 *     an empty string when the speed is missing or not positive.
 */
function labratFormatSpeed(mbps) {
  const speed = Number(mbps);
  if (!Number.isFinite(speed) || speed <= 0) {
    return "";
  }
  return speed >= 1000 ? `${speed / 1000} Gb/s` : `${speed} Mb/s`;
}

/**
 * Name a device in a page address: an inventory device by its number
 * ("device:35" is "35"), one only discovery has seen as "observed-110".
 * Pages put it after their own path (/areas/1/equipment/35) so a refresh
 * or a shared link opens that device's details.
 *
 * @param {string|null} nodeId - Topology node ID.
 * @returns {string|null} Path segment, or null for any other ID.
 */
function labratDeviceSegment(nodeId) {
  const match = /^(device|observed):(\d+)$/.exec(String(nodeId || ""));
  if (!match) return null;
  return match[1] === "device" ? match[2] : `observed-${match[2]}`;
}

/**
 * Show a device link in the address bar without a new history entry, or
 * the page's own address when no device is open.
 *
 * @param {string} base - The page's path without a device.
 * @param {string|null} nodeId - Open device's node ID, or null.
 * @returns {void}
 */
function labratShowDeviceInUrl(base, nodeId) {
  const segment = labratDeviceSegment(nodeId);
  const path = segment ? `${base.replace(/\/$/, "")}/${segment}` : base;
  if (window.location.pathname === path && !window.location.search) return;
  window.history.replaceState(window.history.state, "", path + window.location.hash);
}

/** Most ports an inspector lists before summarizing the rest. */
const LABRAT_PORT_LIST_LIMIT = 16;

/**
 * Check the versioned LabRat health endpoint and display its result.
 *
 * If the status indicator is absent, the function does nothing, allowing the
 * script to be shared by future pages. Network and JSON errors are handled
 * locally so the landing page continues to render if the API is unavailable.
 *
 * @returns {Promise<void>} Resolves after the status indicator is updated.
 */
async function updateApiStatus() {
  const indicator = document.getElementById("api-status");
  if (!indicator) {
    return;
  }

  try {
    const response = await fetch(LABRAT_API.health, {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error(`Health endpoint returned HTTP ${response.status}`);
    }
    const health = await response.json();
    const healthy = health.service === "labrat" && health.status === "ok";
    indicator.textContent = healthy ? "API online" : "API returned an unexpected status";
    indicator.dataset.state = healthy ? "ok" : "error";
  } catch {
    indicator.textContent = "API unreachable";
    indicator.dataset.state = "error";
  }
}

void updateApiStatus();

/** How long the mouse rests on a header menu's button before it opens. */
const MENU_HOVER_OPEN_MS = 500;

/** A click this soon after a hover opened a menu keeps it open. */
const MENU_HOVER_GRACE_MS = 600;

/**
 * Make the header's dropdowns (the Locations and account menus, <details>
 * disclosures in base.html) close the way menus are expected to: on Escape
 * (focus returns to the menu's button), on a click outside, when keyboard
 * focus leaves, and when the other menu opens. Resting the mouse on a
 * menu's button for half a second opens it too, as a click would. Without this
 * script each still opens and closes from its button.
 *
 * @returns {void}
 */
function bindNavigationMenus() {
  const menus = [...document.querySelectorAll(".nav-menu")];
  const transitions = new WeakMap();
  /** Pending hover-open timers, and when a hover last opened each menu. */
  const hoverTimers = new WeakMap();
  const hoverOpenedAt = new WeakMap();

  /**
   * Animate one disclosure, retaining its panel until its exit finishes.
   * Cancelled transitions cannot close a menu that has since reopened.
   *
   * @param {HTMLDetailsElement} menu - Header disclosure to update.
   * @param {boolean} open - Desired disclosure state.
   * @returns {void}
   */
  const setOpen = (menu, open) => {
    const previous = transitions.get(menu);
    if (previous ? previous.open === open : menu.open === open) return;
    const panel = menu.querySelector(".nav-menu-panel");
    // Above the ☰ fold both header panels hang from their button (the
    // account pill, or the nav pill for Locations; main.css) and slide down
    // out from behind it. In the folded header they spring into place.
    const slides = matchMedia("(min-width: 861px)").matches;
    // Read where an interrupted animation is before cancelling it.
    const current = previous ? getComputedStyle(panel) : null;
    const start = current && (slides
      ? { top: current.top, clipPath: current.clipPath }
      : { opacity: current.opacity, transform: current.transform });
    previous?.animation.cancel();
    transitions.delete(menu);
    panel.inert = !open;
    if (open) menu.open = true;

    if (matchMedia("(prefers-reduced-motion: reduce)").matches || !panel.animate) {
      menu.open = open;
      return;
    }
    // The slide moves the panel's top by its own height while a clip of
    // the same height hides everything above its resting top (the button's
    // bottom edge, plus the CSS --slide-offset below it), so it never shows
    // above or beside the button. Both are main-thread properties (top, not
    // a composited transform), so they stay in step on every frame; the
    // clip's sides and bottom leave room for the shadow.
    const height = slides ? panel.offsetHeight : 0;
    const offset = slides ? parseFloat(getComputedStyle(panel).getPropertyValue("--slide-offset")) || 0 : 0;
    const resting = slides
      ? { top: `calc(100% ${offset < 0 ? "-" : "+"} ${Math.abs(offset)}px)`, clipPath: "inset(0px -2rem -2rem -2rem)" }
      : { opacity: 1, transform: "translateY(0) scale(1)" };
    const tucked = slides
      ? { top: `calc(100% - ${height - offset}px)`, clipPath: `inset(${height}px -2rem -2rem -2rem)` }
      : { opacity: 0, transform: "translateY(-8px) scale(0.96)" };
    const animation = panel.animate(
      [start || (open ? tucked : resting), open ? resting : tucked],
      {
        duration: open ? (slides ? 260 : 320) : (slides ? 180 : 160),
        easing: open
          ? (slides ? "cubic-bezier(0.2, 0.8, 0.2, 1)" : "cubic-bezier(0.34, 1.56, 0.64, 1)")
          : "cubic-bezier(0.4, 0, 1, 1)",
        fill: "both",
      },
    );
    transitions.set(menu, { open, animation });
    animation.finished.then(() => {
      if (transitions.get(menu)?.animation !== animation) return;
      transitions.delete(menu);
      menu.open = open;
      animation.cancel();
    }, () => {});
  };

  /**
   * Open one menu and close the others.
   *
   * @param {HTMLDetailsElement} menu - Header disclosure to open.
   * @returns {void}
   */
  const openOnly = (menu) => {
    for (const other of menus) if (other !== menu) setOpen(other, false);
    setOpen(menu, true);
  };

  /**
   * Cancel a menu's pending hover-open, if any.
   *
   * @param {HTMLDetailsElement} menu - Header disclosure.
   * @returns {void}
   */
  const cancelHover = (menu) => {
    clearTimeout(hoverTimers.get(menu));
    hoverTimers.delete(menu);
  };

  for (const menu of menus) {
    const button = menu.querySelector("summary");
    button.addEventListener("click", (event) => {
      event.preventDefault();
      cancelHover(menu);
      const open = !(transitions.get(menu)?.open ?? menu.open);
      // A click just after a hover opened the menu means "open", so it
      // must not snap the menu shut again.
      const hoveredAt = hoverOpenedAt.get(menu);
      if (!open && hoveredAt !== undefined && Date.now() - hoveredAt < MENU_HOVER_GRACE_MS) return;
      if (open) openOnly(menu);
      else setOpen(menu, false);
    });
    // Resting a mouse (not touch or pen) on the button opens it after a delay.
    button.addEventListener("pointerenter", (event) => {
      if (event.pointerType !== "mouse" || (transitions.get(menu)?.open ?? menu.open)) return;
      cancelHover(menu);
      hoverTimers.set(menu, setTimeout(() => {
        hoverTimers.delete(menu);
        hoverOpenedAt.set(menu, Date.now());
        openOnly(menu);
      }, MENU_HOVER_OPEN_MS));
    });
    button.addEventListener("pointerleave", () => cancelHover(menu));
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && menu.open) {
        event.preventDefault();
        setOpen(menu, false);
        button.focus();
      }
    });
    menu.addEventListener("focusout", (event) => {
      if (menu.open && event.relatedTarget && !menu.contains(event.relatedTarget)) {
        setOpen(menu, false);
      }
    });
  }
  document.addEventListener("click", (event) => {
    for (const menu of menus) {
      if (menu.open && !menu.contains(event.target)) setOpen(menu, false);
    }
  });
}

/**
 * Fold the header's page links behind ☰ on phones (main.css section 08).
 * Escape, an outside click, following a link, or widening the window
 * closes the panel. Wider screens never show the button.
 *
 * @returns {void}
 */
function bindSiteMenu() {
  const header = document.querySelector(".site-header");
  const button = document.getElementById("site-menu-toggle");
  if (!header || !button) return;
  const setOpen = (open) => {
    header.toggleAttribute("data-menu-open", open);
    button.setAttribute("aria-expanded", String(open));
  };
  button.addEventListener("click", () => setOpen(!header.hasAttribute("data-menu-open")));
  document.addEventListener("click", (event) => {
    if (!header.hasAttribute("data-menu-open")) return;
    if (!header.contains(event.target) || event.target.closest(".site-navigation-links a")) setOpen(false);
  });
  header.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && header.hasAttribute("data-menu-open") && !event.target.closest("details[open]")) {
      setOpen(false);
      button.focus();
    }
  });
  matchMedia("(min-width: 861px)").addEventListener("change", (query) => {
    if (query.matches) setOpen(false);
  });
}

/** Ticker pace in CSS pixels per second: slow enough to read names as they pass. */
const SOURCE_TICKER_SPEED = 30;

/**
 * Turn the header's source strip into a slow ticker when its lights overflow.
 *
 * When every light fits, nothing moves. When they do not, the lights are
 * repeated once (aria-hidden copies, so screen readers hear each source
 * once) and the list slides left by exactly one set, which loops without a
 * seam; the CSS pauses it under the pointer, keyboard focus, or a tap, so a
 * tooltip stays put while it is read. The window becomes focusable while
 * it overflows. Reduced motion leaves the overflow as a sideways swipe.
 * Resizing the window re-measures.
 *
 * @returns {void}
 */
function bindSourceTicker() {
  const windowEl = document.querySelector(".source-strip-window");
  const list = windowEl && windowEl.querySelector(".source-strip-list");
  if (!list) return;
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  let width = -1;

  const update = () => {
    list.querySelectorAll(".is-ticker-copy").forEach((copy) => copy.remove());
    windowEl.classList.remove("is-ticking");
    const overflows = list.scrollWidth > windowEl.clientWidth + 1;
    if (overflows) windowEl.tabIndex = 0;
    else windowEl.removeAttribute("tabindex");
    if (!overflows || reducedMotion.matches) return;
    const originals = [...list.children];
    originals.forEach((item) => {
      const copy = item.cloneNode(true);
      copy.classList.add("is-ticker-copy");
      copy.setAttribute("aria-hidden", "true");
      list.append(copy);
    });
    const distance = list.children[originals.length].offsetLeft - originals[0].offsetLeft;
    list.style.setProperty("--ticker-distance", `${distance}px`);
    list.style.setProperty("--ticker-duration", `${distance / SOURCE_TICKER_SPEED}s`);
    windowEl.classList.add("is-ticking");
  };

  // Re-measure only when the window's width changes, not on our own edits.
  new ResizeObserver(([entry]) => {
    const next = Math.round(entry.contentRect.width);
    if (next === width) return;
    width = next;
    update();
  }).observe(windowEl);
  reducedMotion.addEventListener("change", update);
}

bindNavigationMenus();
bindSiteMenu();
bindSourceTicker();
