# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Track a router's watchlist conditions across polls: gateway down, VPN peer stale, CARP change.

A condition needs history: it may only enter after a known-good sample of
the same item on the previous, unbroken poll, so the first sample after
setup, a new item, or the first sample after a gap only sets the baseline
(``planning/specs/opnsense-integration.md``, "Watchlist conditions"). This
module keeps that history as the ``watchlist`` telemetry value of the
router's observation and advances it with each successful poll's reading
(:func:`advance`, called through the adapter's ``merge_telemetry``).

No data is not a condition: a section that was not read in full this poll
(failed, partial, not available), or an item whose evidence is missing or
malformed, leaves those items exactly as they were; a poll that fails or
is held leaves the state as it was but marks it interrupted
(:func:`interrupt`), so the next poll cannot open a condition across it.
A condition clears on any fresh, complete sample that shows the item good
again, or that shows the owner opted out (monitoring or keepalive off).

Items, by kind:

- ``gateway``: a monitored gateway. Good when dpinger reports it up
  (``none``), bad when ``down``. An unmonitored gateway is never either.
- ``wireguard``: a peer with persistent keepalive. Good with a handshake
  within :data:`STALE_SECONDS` of the sample, bad beyond. A peer without
  keepalive, or one that never handshook, is never either.
- ``carp``: a CARP virtual IP. Its baseline is its state when first seen
  (or after a gap with no condition); a fresh sample in another state
  enters a change, which clears when the state returns to the baseline.

Used by: api.topology, collector.adapters.opnsense.
"""

from datetime import datetime
from typing import Any

#: A keepalive peer rehandshakes about every two minutes; five without one is stale.
STALE_SECONDS = 300

#: Two samples further apart than this are not one unbroken watch.
GAP_SECONDS = 900

#: Condition words by item kind (also the ignore rule's ``condition``).
CONDITIONS = {"gateway": "router-gateway-down", "wireguard": "router-peer-stale", "carp": "router-carp-changed"}


def _time(value: Any) -> float | None:
    """A stored ISO time as a Unix timestamp.

    Args:
        value: Such as ``2026-11-21T17:00:00+00:00``.

    Returns:
        float | None: The timestamp, or None when it is not a time.
    """
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


#: CARP states a sample may report; anything else is missing evidence.
CARP_STATES = frozenset({"master", "backup", "init", "disabled"})

#: A handshake this far in the future of its sample is not evidence (clock skew allowed).
FUTURE_SECONDS = 60


def _health(kind: str, item: dict, at: float) -> tuple[bool, bool, bool] | None:
    """An item's standing in one sample.

    Args:
        kind: ``gateway`` or ``wireguard``.
        item: The item as read.
        at: The sample's time.

    Returns:
        tuple | None: ``(applies, good, bad)``: ``applies`` is false when
            the owner opted out (monitoring ``~``, keepalive ``off``), which ends
            any condition; good and bad are both false when neither is
            known (a degraded gateway, a peer that never handshook). None
            when the evidence is missing or malformed: it then changes
            nothing.
    """
    if kind == "gateway":
        if not isinstance(item.get("status"), str) or not isinstance(item.get("monitored"), bool):
            return None
        if not item["monitored"]:
            return False, False, False
        return True, item["status"] == "none", item["status"] == "down"
    keepalive, handshake = item.get("keepalive"), item.get("handshake")
    if not isinstance(keepalive, int) or isinstance(keepalive, bool):
        return None  # Not reported: unknown, not "off".
    if keepalive == 0:
        return False, False, False
    if handshake is None:
        return True, False, False  # Never connected (or reset): neither.
    if not isinstance(handshake, (int, float)) or isinstance(handshake, bool) or handshake > at + FUTURE_SECONDS:
        return None
    return True, at - handshake <= STALE_SECONDS, at - handshake > STALE_SECONDS


def interrupt(previous: Any) -> Any:
    """Mark a stored watchlist as interrupted by a failed or held poll.

    Its items and conditions stay as they were, but the next successful
    poll no longer follows the last one, so it cannot open a condition
    (it can still clear one).

    Args:
        previous: The stored watchlist, or None.

    Returns:
        Any: The watchlist marked interrupted (None stays None).
    """
    return {**previous, "interrupted": True} if isinstance(previous, dict) else previous


def advance(previous: Any, reading: dict) -> dict:
    """The watchlist after one successful poll.

    Args:
        previous: The stored watchlist (``sampled_at``, ``items`` by key,
            and ``interrupted`` after a failed or held poll), or None
            before the first poll.
        reading: This poll's ``sampled_at``, ``sections`` (``gateway``,
            ``wireguard``, ``carp``: whether each was read in full), and
            ``items`` (``key``, ``kind``, ``label``, and the kind's fields:
            ``monitored`` and ``status``; ``keepalive`` and ``handshake``;
            ``state``).

    Returns:
        dict: ``sampled_at`` and ``items``: each with ``kind``, ``label``,
            ``sampled_at`` (its last valid complete sample), ``state``,
            ``good``, ``baseline`` (CARP), and ``condition`` (None, or
            ``condition``, ``entered_at``, and for CARP ``from`` and ``to``).
            An item whose evidence was missing or malformed keeps its
            previous entry, so its next sample does not follow on.
    """
    previous = previous if isinstance(previous, dict) else {}
    now = reading["sampled_at"]
    at, before = _time(now), _time(previous.get("sampled_at"))
    unbroken = (at is not None and before is not None and 0 <= at - before <= GAP_SECONDS
                and not previous.get("interrupted"))
    items = {key: dict(value) for key, value in (previous.get("items") or {}).items() if isinstance(value, dict)}
    for kind in CONDITIONS:
        if not reading["sections"].get(kind):
            continue  # Not read in full: no data, so nothing changes.
        seen = set()
        for item in (row for row in reading["items"] if row["kind"] == kind):
            key = item["key"]
            seen.add(key)
            last = items.get(key) or {}
            followed = unbroken and last.get("sampled_at") == previous.get("sampled_at")
            entry = {"kind": kind, "label": item.get("label"), "sampled_at": now, "condition": last.get("condition"),
                     "state": item.get("state") or item.get("status"), "baseline": last.get("baseline")}
            if kind == "carp":
                state = item.get("state")
                if state not in CARP_STATES:
                    continue  # Missing evidence: the entry stays, and the next sample does not follow on.
                entry["good"] = None
                if entry["condition"]:
                    if state == entry["baseline"]:
                        entry["condition"] = None
                    elif state != entry["condition"].get("to"):
                        entry["condition"] = {**entry["condition"], "to": state}
                elif not followed or entry["baseline"] is None:
                    entry["baseline"] = state
                elif state != entry["baseline"]:
                    entry["condition"] = {"condition": CONDITIONS[kind], "entered_at": now,
                                          "from": entry["baseline"], "to": state}
            else:
                standing = _health(kind, item, at or 0)
                if standing is None:
                    continue  # Missing or malformed evidence: as for CARP.
                applies, good, bad = standing
                entry["good"] = good
                if not applies:
                    entry["condition"] = None  # Opted out: the condition no longer applies.
                elif entry["condition"] and good:
                    entry["condition"] = None
                elif not entry["condition"] and bad and followed and last.get("good"):
                    entry["condition"] = {"condition": CONDITIONS[kind], "entered_at": now}
            items[key] = entry
        for key in [key for key, value in items.items() if value.get("kind") == kind and key not in seen]:
            del items[key]  # No longer reported by a complete read: gone.
    return {"sampled_at": now, "items": items}


def conditions(state: Any) -> list[dict]:
    """The active conditions in a stored watchlist, for the Observations page.

    Args:
        state: The stored ``watchlist`` value.

    Returns:
        list[dict]: ``condition``, ``item`` (the item's key), ``kind``,
            ``label``, ``entered_at``, ``state`` (now), ``from`` and ``to``
            (CARP), and ``sampled_at`` (the item's last complete sample:
            older than the watchlist's own when its section was not read
            since, which is "no data since").
    """
    if not isinstance(state, dict):
        return []
    found = []
    for key, item in sorted((state.get("items") or {}).items()):
        condition = item.get("condition") if isinstance(item, dict) else None
        if isinstance(condition, dict):
            found.append({"condition": condition.get("condition"), "item": key, "kind": item.get("kind"),
                          "label": item.get("label"), "entered_at": condition.get("entered_at"),
                          "state": item.get("state"), "from": condition.get("from"), "to": condition.get("to"),
                          "sampled_at": item.get("sampled_at"), "watch_sampled_at": state.get("sampled_at")})
    return found
