# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Hold a source's polls when its device looks like a different machine.

A source whose observation has a fixed remote ID (an OPNsense integration
is one ``firewall``) would otherwise move that observation, its inventory
link, label, and placement, onto whatever machine now answers at the
address. Such an adapter reports ``identity_evidence`` (the hostname and
hardware MACs, only from a complete interface read) with its telemetry.
When the stored evidence names a different hostname *and* shares none of
the MACs, the poll is held: nothing is applied, the new evidence is kept
as ``identity_pending``, and the source shows :data:`HELD_MESSAGE` until
the owner answers in Settings (``planning/specs/opnsense-integration.md``,
"Identity"). A changed hostname alone, a NIC swap, or a new key never
holds a poll.

Used by: collector.runner, integrations.
"""

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from labrat.coerce import json_object
from labrat.collector.store import clear_telemetry, record_telemetry
from labrat.discovery.models import DiscoveredDevice, DiscoverySource

#: What a held source shows as its error.
HELD_MESSAGE = "This looks like a different device. Confirm it in Settings, Integrations."


def conflicts(stored: Mapping[str, Any], observed: Mapping[str, Any]) -> bool:
    """Whether new evidence names a different machine than the stored evidence.

    Args:
        stored: The observation's ``identity_evidence``.
        observed: This poll's ``identity_evidence``.

    Returns:
        bool: True when the hostname differs and no hardware MAC is shared.
    """
    same_name = str(stored.get("hostname") or "").lower() == str(observed.get("hostname") or "").lower()
    shared = set(stored.get("macs") or []) & set(observed.get("macs") or [])
    return not same_name and not shared


def _device(db: Session, source: DiscoverySource, remote_id: str) -> DiscoveredDevice | None:
    """A source's stored observation.

    Args:
        db: Open session.
        source: Its source.
        remote_id: Its remote ID.

    Returns:
        DiscoveredDevice | None: The observation, if stored.
    """
    return db.scalar(select(DiscoveredDevice).where(
        DiscoveredDevice.source_id == source.id, DiscoveredDevice.remote_id == remote_id))


def hold(db: Session, source: DiscoverySource, telemetry: Any) -> bool:
    """Decide whether this poll must be held, keeping the new evidence if so.

    A poll is held when a device's evidence conflicts with what is stored.
    A device already waiting on the owner stays held until a poll brings
    complete evidence of the known machine again: a poll without evidence
    (a failed, refused, or capped interface read) never writes through.

    Args:
        db: Open transaction session.
        source: The polled source.
        telemetry: The adapter's ``last_telemetry`` (remote ID to readings).

    Returns:
        bool: True when the poll must not be applied.
    """
    readings = telemetry if isinstance(telemetry, dict) else {}
    held = False
    devices = db.scalars(select(DiscoveredDevice).where(
        DiscoveredDevice.source_id == source.id, DiscoveredDevice.details_json.contains('"identity_evidence"')))
    for device in devices:
        details = json_object(device.details_json)
        stored = details.get("identity_evidence")
        if not isinstance(stored, dict):
            continue
        reading = readings.get(device.remote_id)
        observed = reading.get("identity_evidence") if isinstance(reading, dict) else None
        if isinstance(observed, dict):
            if conflicts(stored, observed):
                record_telemetry(db, source, device.remote_id, "identity_pending", observed)
                held = True
        elif isinstance(details.get("identity_pending"), dict):
            held = True
    return held


def fingerprint(evidence: Mapping[str, Any]) -> str:
    """A short, stable digest of identity evidence, ignoring when it was read.

    Settings puts it in the answer form, so an answer applies only to the
    evidence the owner saw; repeated readings of the same machine keep it.

    Args:
        evidence: Identity evidence (``hostname`` and ``macs``).

    Returns:
        str: Sixteen hexadecimal digits.
    """
    canonical = json.dumps({"hostname": str(evidence.get("hostname") or "").lower(),
                            "macs": sorted(str(mac) for mac in evidence.get("macs") or [])}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def settle(db: Session, source: DiscoverySource, remote_id: str) -> None:
    """Drop a pending identity once a poll matches the stored one again.

    Args:
        db: Open transaction session.
        source: The source.
        remote_id: The observation's remote ID.
    """
    clear_telemetry(db, source, remote_id, "identity_pending")


def pending(db: Session, source: DiscoverySource) -> dict | None:
    """The evidence a held source is waiting on, for Settings.

    Args:
        db: Open session.
        source: The source.

    Returns:
        dict | None: ``remote_id``, ``stored``, and ``observed`` evidence, or
            None when nothing is pending.
    """
    for device in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id)):
        details = json_object(device.details_json)
        if isinstance(details.get("identity_pending"), dict):
            return {"remote_id": device.remote_id, "stored": details.get("identity_evidence") or {},
                    "observed": details["identity_pending"]}
    return None


def accept(db: Session, source: DiscoverySource) -> bool:
    """The owner says it is the same machine: adopt the pending evidence.

    Args:
        db: Open transaction session.
        source: The held source.

    Returns:
        bool: True when there was pending evidence to adopt.
    """
    waiting = pending(db, source)
    if waiting is None:
        return False
    record_telemetry(db, source, waiting["remote_id"], "identity_evidence", waiting["observed"])
    settle(db, source, waiting["remote_id"])
    source.last_error = None
    return True
