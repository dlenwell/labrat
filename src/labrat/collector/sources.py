# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Register discovery sources from local config without storing credentials.

The initial developer config may use ``[unifi]`` and ``[proxmox]`` directly.
For multiple controllers, use ``[integrations.unifi.NAME]`` or
``[integrations.proxmox.NAME]``. Existing source rows are never deleted,
re-enabled, or reset when the file changes; disabling a source in the database
is an intentional operator decision.

Used by: api.clients, collector.__main__, collector.access_collection,
collector.proxmox_live, collector.runner, discovery_settings, integrations.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from labrat.audit import record_audit_event
from labrat.discovery.models import DiscoverySource
from labrat.integration_catalog import KINDS

# "mndp" is an opt-in passive listener for MikroTik announcements.
_KINDS = ("mndp", *KINDS)

#: Service-discovery probes that ``[scan] probes`` may enable, each a source
#: kind of its own. They contact only hosts inside ``[scan] ranges``.
SCAN_PROBES = ("nut",)


def configured_sources(config: Mapping[str, Any]) -> dict[str, dict[str, Mapping[str, Any]]]:
    """Normalize legacy single-controller and named-controller configuration.

    Args:
        config: Local TOML mapping with optional top-level ``[unifi]`` and
            ``[proxmox]`` or ``[integrations.KIND.NAME]`` sections.

    Returns:
        dict[str, dict[str, Mapping[str, Any]]]: Vendor -> name -> settings.
            Only supported vendor types are returned.

    Raises:
        ValueError: The configuration structure is ambiguous or malformed.
    """
    result: dict[str, dict[str, Mapping[str, Any]]] = {kind: {} for kind in _KINDS}
    integrations = config.get("integrations", {})
    if not isinstance(integrations, dict):
        raise ValueError("[integrations] must be a TOML table")
    for kind in _KINDS:
        family = integrations.get(kind, {})
        if not isinstance(family, dict):
            raise ValueError(f"[integrations.{kind}] must be a TOML table")
        for name, settings in family.items():
            if not isinstance(name, str) or not name or len(name) > 80 or not isinstance(settings, dict):
                raise ValueError(f"Invalid named {kind} source configuration")
            result[kind][name] = settings
        legacy = config.get(kind)
        if legacy is not None:
            if not isinstance(legacy, dict):
                raise ValueError(f"[{kind}] must be a TOML table")
            if "default" in result[kind]:
                raise ValueError(f"[{kind}] conflicts with [integrations.{kind}.default]")
            result[kind]["default"] = legacy
    result.update(_scan_probe_sources(config))
    controls = config.get('discovery', {})
    if not isinstance(controls, dict) or any(type(controls[key]) is not bool for key in ('network', 'nut', 'snmp', 'mndp') if key in controls):
        raise ValueError('Discovery controls must be boolean flags')
    return result


def effective_sources(config: Mapping[str, Any]) -> dict:
    """Apply discovery method gates without altering stored integration configuration.

    Scan permission and method choice are separate. SNMP supplements known
    Proxmox hosts; disabling it must not disable the Proxmox API collector.

    Args:
        config: The private configuration.

    Returns:
        dict: The configured sources per kind, with those the Discovery
            settings turn off removed.
    """
    result = configured_sources(config)
    flags = config.get('discovery', {})
    if not flags.get('network', True) or not flags.get('nut', True) or not config.get('scan', {}).get('ranges'):
        result['nut'] = {}
    if not flags.get('mndp', True):
        result['mndp'] = {}
    if not flags.get('snmp', True):
        result['proxmox'] = {name: {key: value for key, value in settings.items() if key != 'snmp'}
                             for name, settings in result['proxmox'].items()}
    return result


def _scan_probe_sources(config: Mapping[str, Any]) -> dict[str, dict[str, Mapping[str, Any]]]:
    """Turn ``[scan]`` into one source per enabled service-discovery probe.

    ``[scan]`` is the single place that grants LabRat permission to scan:
    ``ranges`` lists the only networks or addresses it may contact, and
    ``probes`` the protocols it may try there. A probe's own options live in a
    sub-table such as ``[scan.nut]``.

    Args:
        config: Local TOML mapping.

    Returns:
        dict[str, dict[str, Mapping[str, Any]]]: Probe kind -> ``default`` ->
            settings (``ranges`` plus the probe's options); empty kinds when
            scanning is not configured.

    Raises:
        ValueError: Malformed ``[scan]`` or an unknown probe.
    """
    result: dict[str, dict[str, Mapping[str, Any]]] = {kind: {} for kind in SCAN_PROBES}
    scan = config.get("scan")
    if scan is None:
        return result
    if not isinstance(scan, dict):
        raise ValueError("[scan] must be a TOML table")
    probes = scan.get("probes", [])
    if not isinstance(probes, list) or not all(isinstance(probe, str) for probe in probes):
        raise ValueError("[scan] probes must be a list of probe names")
    unknown = sorted(set(probes) - set(SCAN_PROBES))
    if unknown:
        raise ValueError(f"[scan] probes has unknown probe(s): {', '.join(unknown)}")
    for probe in probes:
        options = scan.get(probe, {})
        if not isinstance(options, dict):
            raise ValueError(f"[scan.{probe}] must be a TOML table")
        result[probe]["default"] = {**options, "ranges": scan.get("ranges")}
    return result


def sync_configured_sources(sessions: sessionmaker, config: Mapping[str, Any]) -> int:
    """Create missing database discovery sources for locally configured APIs.

    Args:
        sessions: Existing SQLAlchemy session factory.
        config: Validated source sections (secrets remain in this mapping).

    Returns:
        int: Number of new source rows registered.

    Note:
        Database rows contain a source name, adapter kind and config reference
        only. No URL, token, secret, or credential value is persisted or audited.
    """
    settings = configured_sources(config)
    added = 0
    with sessions.begin() as db:
        for kind, family in settings.items():
            for name, source_settings in family.items():
                current = db.scalar(select(DiscoverySource).where(
                    DiscoverySource.kind == kind, DiscoverySource.config_name == name,
                ))
                if current is not None:
                    continue
                display_name = source_settings.get("display_name")
                label = display_name.strip() if isinstance(display_name, str) and 0 < len(display_name.strip()) <= 80 else name
                source = DiscoverySource(
                    name=f"{kind}:{label}", kind=kind, config_name=name, enabled=True,
                )
                db.add(source)
                db.flush()
                record_audit_event(
                    db, actor_kind="system", actor_name="collector-bootstrap",
                    action="discovery.source.created",
                    subject_kind="discovery_source", subject_id=source.id,
                    details={"kind": kind, "config_name": name},
                )
                added += 1
    return added
