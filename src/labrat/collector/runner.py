# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Run background discovery without blocking the Flask request workers.

One collector process polls enabled, registered sources and persists their
observations using short SQLite transactions. A failed source does not stop
other sources. Adapter credentials are read only from local configuration.

Proxmox and UniFi adapters are registered by the collector entry point. Run via
``python -m labrat.collector --once`` or the long-running daemon command.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from labrat.audit import record_audit_event
from labrat.collector.contracts import DiscoveryAdapter
from labrat.collector.adapters.unifi import UniFiAdapter
from labrat.collector.adapters.proxmox import ProxmoxAdapter
from labrat.collector.client_links import collect_client_links, reconcile_client_links
from labrat.collector.links import reconcile_proxmox_lldp_links
from labrat.collector.nut_wired import collect_nut_wired, reconcile_nut_wired
from labrat.collector import identity
from labrat.collector.store import apply_batch, record_telemetry, record_wan
from labrat.collector.sightings import reconcile_sightings
from labrat.collector.sources import DERIVED_SOURCE_PARENTS, effective_sources
from labrat.collector.wireless_clients import collect_nut_wireless, reconcile_nut_wireless
from labrat.coerce import json_object
from labrat.discovery.models import DiscoveredDevice, DiscoverySource
from labrat.instance import LeaseLost


_LOGGER = logging.getLogger(__name__)



def _stored_details(db: Any, source: DiscoverySource, remote_id: str) -> dict:
    """An observation's stored details, for adapters that merge telemetry.

    Args:
        db: Open session.
        source: The observation's source.
        remote_id: Its remote ID.

    Returns:
        dict: Its details, or empty when it is not stored yet.
    """
    device = db.scalar(select(DiscoveredDevice).where(
        DiscoveredDevice.source_id == source.id, DiscoveredDevice.remote_id == remote_id))
    return json_object(device.details_json) if device is not None else {}


def _interrupt(db: Any, source: DiscoverySource, adapter: Any) -> None:
    """Tell an adapter's stored history that a poll failed or was held.

    An adapter with ``interrupt_telemetry(key, stored)`` and
    ``INTERRUPTED`` (telemetry keys by remote ID) keeps history across
    polls (OPNsense's watchlist); it marks that history interrupted, so
    the next poll does not follow on from the last good one.

    Args:
        db: Open session.
        source: The source whose poll failed or was held.
        adapter: Its adapter.
    """
    hook, keys_by_remote = getattr(adapter, "interrupt_telemetry", None), getattr(adapter, "INTERRUPTED", None)
    if not callable(hook) or not isinstance(keys_by_remote, dict):
        return
    for remote_id, keys in keys_by_remote.items():
        stored = _stored_details(db, source, remote_id)
        for key in keys:
            if key in stored:
                record_telemetry(db, source, remote_id, key, hook(key, stored[key]))


def poll_once(
    sessions: sessionmaker,
    config: Mapping[str, Any],
    adapters: Mapping[str, DiscoveryAdapter],
    interval_seconds: int | None = None,
) -> int:
    """Poll each enabled source with a matching registered adapter.

    Args:
        sessions: Factory for short-lived database transactions.
        config: Parsed local TOML configuration (never log this mapping).
        adapters: Map of source kinds to read-only adapter instances.
        interval_seconds: The looping collector's interval, recorded on each
            attempted source so pages can show when the next poll is due.
            None (a one-off run) leaves the recorded interval unchanged.

    Returns:
        int: Number of sources polled successfully.

    Raises:
        ValueError: Invalid integration configuration structure.
    """
    sources_config = effective_sources(config)
    completed = 0
    successful_sources: list[DiscoverySource] = []
    with sessions.begin() as db:
        for derived in db.scalars(select(DiscoverySource).where(DiscoverySource.kind.in_(DERIVED_SOURCE_PARENTS))):
            kind = DERIVED_SOURCE_PARENTS[derived.kind]
            parent = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == kind, DiscoverySource.config_name == derived.config_name))
            derived.enabled = bool(parent and parent.enabled and parent.config_name in sources_config.get(kind, {}))
        # Derived client sources are filled by their parent's poll, and
        # pushed sources by machines over the API (api/machine.py).
        sources = list(db.scalars(select(DiscoverySource).where(
            DiscoverySource.enabled.is_(True), DiscoverySource.kind.not_in((*DERIVED_SOURCE_PARENTS, "mndp_push")),
        )))
    for source_record in sources:
        adapter = adapters.get(source_record.kind)
        if adapter is None:
            _LOGGER.info("No registered adapter for source kind %s", source_record.kind)
            continue
        settings = sources_config.get(source_record.kind, {}).get(source_record.config_name)
        if not isinstance(settings, dict):
            _LOGGER.warning("Missing configuration for source %s", source_record.name)
            continue
        try:
            batch = adapter.poll(settings)
            now = datetime.now(timezone.utc)
            with sessions.begin() as db:
                source = db.get(DiscoverySource, source_record.id)
                if source is None or not source.enabled:
                    # Disabled, retired, or deleted while the adapter read it
                    # (such as a replacement answered in Settings): the
                    # in-flight result belongs to no active source.
                    _LOGGER.info("Source %s changed during its poll; result discarded", source_record.name)
                    continue
                source.last_attempt_at = now
                if interval_seconds is not None:
                    source.poll_interval_seconds = interval_seconds
                telemetry = getattr(adapter, "last_telemetry", None)
                if identity.hold(db, source, telemetry):
                    # A different machine answers: apply nothing until the
                    # owner says which it is (collector/identity.py).
                    source.last_error = identity.HELD_MESSAGE
                    _interrupt(db, source, adapter)
                    _LOGGER.warning("Source %s held: its device looks different", source_record.name)
                    continue
                apply_batch(db, source, batch, seen_at=now)
                # Readings an adapter took during this poll (such as Pi-hole
                # statistics) are telemetry: kept by scans, never audited.
                # An adapter may merge a reading with the stored one (to keep
                # the last good value of a part it could not read).
                merge = getattr(adapter, "merge_telemetry", None)
                for remote_id, readings in (telemetry.items() if isinstance(telemetry, dict) else ()):
                    stored = _stored_details(db, source, remote_id) if merge else {}
                    for key, value in readings.items():
                        record_telemetry(db, source, remote_id, key, merge(key, stored.get(key), value) if merge else value)
                    if "identity_evidence" in readings:
                        identity.settle(db, source, remote_id)
                # A router's client sightings go to its derived clients
                # source in the same transaction (collector/sightings.py).
                report = getattr(adapter, "last_sightings", None)
                if isinstance(report, dict):
                    reconcile_sightings(db, source, report, now)
            completed += 1
            successful_sources.append(source_record)
        except LeaseLost:
            raise  # Another collector took over: stop, record nothing.
        except Exception as exc:
            # Never store or log raw exception text: HTTP clients may embed API
            # tokens, URLs, or sensitive device details in their exceptions.
            error_name = type(exc).__name__
            _LOGGER.warning("Source %s failed (%s)", source_record.name, error_name)
            with sessions.begin() as db:
                source = db.get(DiscoverySource, source_record.id)
                if source is None or not source.enabled:
                    continue  # Disabled or deleted during the poll: record nothing.
                source.last_attempt_at = datetime.now(timezone.utc)
                if interval_seconds is not None:
                    source.poll_interval_seconds = interval_seconds
                source.last_error = f"Collection failed: {error_name}"
                _interrupt(db, source, adapter)
                record_audit_event(
                    db, actor_kind="collector", actor_id=source.id,
                    actor_name=source.name, action="discovery.collection.failed",
                    subject_kind="discovery_source", subject_id=source.id,
                    details={"error_type": error_name},
                )
    proxmox = [source for source in successful_sources if source.kind == "proxmox"]
    unifi = [source for source in successful_sources if source.kind == "unifi"]
    from labrat.collector.proxmox_clients import collect_guest_identities
    from labrat.collector.proxmox_cluster import collect_cluster_telemetry
    proxmox_adapter = adapters.get("proxmox")
    if isinstance(proxmox_adapter, ProxmoxAdapter):
        for parent in proxmox:
            settings = sources_config["proxmox"][parent.config_name]
            collect_guest_identities(sessions, parent, proxmox_adapter, settings)
            collect_cluster_telemetry(sessions, parent, proxmox_adapter, settings)
    from labrat.collector.unifi_network import collect_network_snapshot
    unifi_adapter = adapters.get("unifi")
    if isinstance(unifi_adapter, UniFiAdapter):
        for source in unifi:
            settings = sources_config["unifi"].get(source.config_name)
            if isinstance(settings, dict):
                collect_network_snapshot(sessions, source, unifi_adapter, settings)
    if proxmox and unifi:
        with sessions.begin() as db:
            proxmox_records = [db.get(DiscoverySource, source.id) for source in proxmox]
            for source in unifi:
                unifi_record = db.get(DiscoverySource, source.id)
                count = reconcile_proxmox_lldp_links(
                    db, unifi_record, proxmox_records,
                )
                if count:
                    _LOGGER.info("Reconciled %s Proxmox physical link(s) from LLDP", count)
        adapter = adapters.get("unifi")
        if isinstance(adapter, UniFiAdapter):
            for source in unifi:
                settings = sources_config["unifi"].get(source.config_name)
                if not isinstance(settings, dict):
                    continue
                with sessions() as db:
                    pairs = collect_client_links(db, source, proxmox, adapter, settings)
                if pairs is None:
                    _LOGGER.warning("UniFi client link lookup unavailable; retaining previous observations")
                    continue
                with sessions.begin() as db:
                    count = reconcile_client_links(db, db.get(DiscoverySource, source.id), pairs)
                    if count:
                        _LOGGER.info("Reconciled %s Proxmox physical link(s) from UniFi clients", count)
    # NUT has no host NIC inventory. Match current UniFi clients by the
    # server's exact address: a wireless match is telemetry (no cable), a
    # wired match is an inferred NIC and cable to the reported switch port.
    if unifi and any(source.kind == "nut" for source in sources):
        adapter = adapters.get("unifi")
        if isinstance(adapter, UniFiAdapter):
            candidates: dict[int, list[dict[str, Any]]] = {}
            wired: dict[int, list[dict[str, Any]]] = {}
            complete = True
            for source in unifi:
                settings = sources_config["unifi"].get(source.config_name)
                if not isinstance(settings, dict):
                    complete = False
                    break
                with sessions() as db:
                    found = collect_nut_wireless(db, source, adapter, settings)
                    found_wired = collect_nut_wired(db, source, adapter, settings)
                if found is None or found_wired is None:
                    complete = False
                    break
                for server_id, records in found.items():
                    candidates.setdefault(server_id, []).extend(records)
                for server_id, records in found_wired.items():
                    wired.setdefault(server_id, []).extend(records)
            # One failed or skipped controller cannot establish absence. Keep
            # the previous sample and let the page show it as stale instead.
            if len(unifi) != len([source for source in sources if source.kind == "unifi"]):
                complete = False
            if complete:
                with sessions.begin() as db:
                    reconcile_nut_wireless(db, candidates)
                    if reconcile_nut_wired(db, wired):
                        _LOGGER.info("Inferred NUT server wired link(s) from UniFi clients")
            else:
                _LOGGER.warning("UniFi NUT client lookup unavailable; retaining previous observations")
    # Endpoints have independent health and snapshot reconciliation. Their
    # optional collection cannot invalidate a successful infrastructure poll.
    from labrat.collector.clients import poll_clients
    adapter = adapters.get("unifi")
    if isinstance(adapter, UniFiAdapter):
        for parent in unifi:
            poll_clients(sessions, parent, adapter, sources_config["unifi"][parent.config_name], interval_seconds)
    return completed


#: Default and allowed seconds between WAN-only polls of a UniFi source.
DEFAULT_WAN_INTERVAL_SECONDS = 30
MIN_WAN_INTERVAL_SECONDS = 10
MAX_WAN_INTERVAL_SECONDS = 300


def wan_interval(settings: Mapping[str, Any]) -> int:
    """Read a UniFi source's WAN poll interval; 0 turns WAN polling off.

    Args:
        settings: The source's private settings (``wan_interval_seconds``).

    Returns:
        int: Seconds between WAN-only polls, or 0 when disabled.

    Raises:
        ValueError: The value is not 0 or an integer from 10 to 300.
    """
    value = settings.get("wan_interval_seconds", DEFAULT_WAN_INTERVAL_SECONDS)
    if type(value) is not int or not (
        value == 0 or MIN_WAN_INTERVAL_SECONDS <= value <= MAX_WAN_INTERVAL_SECONDS
    ):
        raise ValueError("UniFi wan_interval_seconds must be 0 or an integer from 10 to 300")
    return value


def wan_poll_sources(
    sessions: sessionmaker, config: Mapping[str, Any],
) -> list[tuple[int, Mapping[str, Any], int]]:
    """List enabled UniFi sources that want WAN-only polling.

    Args:
        sessions: Factory for short-lived database transactions.
        config: Parsed local TOML configuration (never log this mapping).

    Returns:
        list[tuple[int, Mapping[str, Any], int]]: Source ID, settings, and
            WAN interval for each source with polling enabled.
    """
    sources_config = effective_sources(config)
    with sessions() as db:
        sources = list(db.scalars(select(DiscoverySource).where(
            DiscoverySource.enabled.is_(True), DiscoverySource.kind == "unifi",
        )))
    found = []
    for source in sources:
        settings = sources_config.get("unifi", {}).get(source.config_name)
        if not isinstance(settings, dict):
            continue
        try:
            interval = wan_interval(settings)
        except ValueError:
            _LOGGER.warning("Invalid wan_interval_seconds for %s; WAN polling is off", source.name)
            continue
        if interval:
            found.append((source.id, settings, interval))
    return found


def poll_wan_once(
    sessions: sessionmaker,
    source_id: int,
    settings: Mapping[str, Any],
    adapter: Any,
) -> int:
    """Read and store one source's gateway uplink state.

    WAN polls do not change the source's attempt or success times (those
    describe full scans), and a failure is only logged: the next poll or
    full scan tries again.

    Args:
        sessions: Factory for short-lived database transactions.
        source_id: UniFi discovery source to poll.
        settings: That source's private settings.
        adapter: UniFi adapter providing ``poll_wan``.

    Returns:
        int: Number of gateways updated.
    """
    with sessions() as db:
        source = db.get(DiscoverySource, source_id)
        if source is None or not source.enabled:
            return 0
    try:
        readings = adapter.poll_wan(settings)
    except Exception as exc:
        # Never log raw exception text: it may contain URLs or tokens.
        _LOGGER.warning("WAN poll failed (%s)", type(exc).__name__)
        return 0
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        source = db.get(DiscoverySource, source_id)
        if source is None:
            return 0
        return sum(1 for reading in readings if record_wan(db, source, reading, now))


#: Default and allowed seconds between UPS readings of known NUT servers.
DEFAULT_UPS_INTERVAL_SECONDS = 30
MIN_UPS_INTERVAL_SECONDS = 10
MAX_UPS_INTERVAL_SECONDS = 300


def ups_poll_sources(
    sessions: sessionmaker, config: Mapping[str, Any],
) -> list[tuple[int, Mapping[str, Any], int]]:
    """List enabled NUT sources that want UPS readings between scans.

    ``[scan.nut] ups_interval_seconds`` sets the interval (default 30, ``0``
    turns fast readings off, otherwise 10 to 300).

    Args:
        sessions: Factory for short-lived database transactions.
        config: Parsed local TOML configuration (never log this mapping).

    Returns:
        list[tuple[int, Mapping[str, Any], int]]: Source ID, settings, and interval.
    """
    sources_config = effective_sources(config)
    with sessions() as db:
        sources = list(db.scalars(select(DiscoverySource).where(
            DiscoverySource.enabled.is_(True), DiscoverySource.kind == "nut",
        )))
    found = []
    for source in sources:
        settings = sources_config.get("nut", {}).get(source.config_name)
        if not isinstance(settings, dict):
            continue
        interval = settings.get("ups_interval_seconds", DEFAULT_UPS_INTERVAL_SECONDS)
        if type(interval) is not int or not (
            interval == 0 or MIN_UPS_INTERVAL_SECONDS <= interval <= MAX_UPS_INTERVAL_SECONDS
        ):
            _LOGGER.warning("Invalid ups_interval_seconds for %s; UPS readings wait for scans", source.name)
            continue
        if interval:
            found.append((source.id, settings, interval))
    return found


def poll_ups_once(
    sessions: sessionmaker,
    source_id: int,
    settings: Mapping[str, Any],
    adapter: Any,
) -> int:
    """Read and store current UPS readings from known NUT servers.

    Like WAN polls, these leave the source's attempt and success times alone
    and only log failures.

    Args:
        sessions: Factory for short-lived database transactions.
        source_id: NUT discovery source.
        settings: That source's settings.
        adapter: NUT adapter providing ``poll_ups``.

    Returns:
        int: Number of UPS observations updated.
    """
    with sessions() as db:
        source = db.get(DiscoverySource, source_id)
        if source is None or not source.enabled:
            return 0
    try:
        readings = adapter.poll_ups(settings)
    except Exception as exc:
        _LOGGER.warning("UPS poll failed (%s)", type(exc).__name__)
        return 0
    with sessions.begin() as db:
        source = db.get(DiscoverySource, source_id)
        if source is None:
            return 0
        return sum(1 for reading in readings
                   if record_telemetry(db, source, reading.remote_id, "ups", reading.telemetry))
