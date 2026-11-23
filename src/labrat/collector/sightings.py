# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Keep a router's client sightings as client observations of their own source.

A router (OPNsense) knows which MACs used which addresses on which of its
networks: ARP and NDP entries, and DHCP leases. That is a **sighting**,
never an attachment: it never creates a wired NIC, a cable, or online
status (``planning/specs/opnsense-integration.md``, "Clients"). Switches
and access points still say where a client is plugged in.

Sightings live in a derived source (``<kind>_clients``, such as
``opnsense_clients``) beside their router's, as UniFi clients do, so the
Discovery clients list, its port check, and Add to inventory treat both
alike. One observation per MAC per scope (the router interface): the same
MAC on two networks is two sightings, and the clients list folds them
into one row by MAC for display only.

Each sighting's ``client`` details hold ``state``: ``seen`` (in ARP or
NDP), ``lease`` (a DHCP lease, but not seen: an endpoint, never online),
or ``not_seen`` (no longer reported); ``seen_at``, when it was last seen,
with the addresses seen then; and the feeds that reported it. It ages
only when those feeds were read in full again
(:func:`reconcile_sightings`).

Used by: api.clients, collector.runner.
"""

from datetime import datetime
import json
from typing import Any

from sqlalchemy import select

from labrat.coerce import json_object, mac_address
from labrat.collector.clients import endpoint_owners
from labrat.collector.contracts import DeviceObservation, DiscoveryBatch
from labrat.collector.store import apply_batch
from labrat.discovery.mac_ownership import EXACT, interface_owners
from labrat.discovery.models import DiscoveredDevice, DiscoverySource

#: The evidence word on every router sighting.
EVIDENCE = "router-sighting"

#: The neighbour tables, which see a client (its addresses in use now).
NEIGHBOUR_FEEDS = ("arp", "ndp")

#: Every feed that can report a sighting.
ALL_FEEDS = (*NEIGHBOUR_FEEDS, "dnsmasq_leases", "kea_leases")


def derived_source(db: Any, parent: DiscoverySource) -> DiscoverySource:
    """The router's clients source, created on first use.

    Args:
        db: Open transaction.
        parent: The router's own source.

    Returns:
        DiscoverySource: ``<kind>_clients`` with the parent's config name.
    """
    kind = f"{parent.kind}_clients"
    source = db.scalar(select(DiscoverySource).where(
        DiscoverySource.kind == kind, DiscoverySource.config_name == parent.config_name))
    if source is None:
        source = DiscoverySource(kind=kind, config_name=parent.config_name, name=f"Clients {parent.id}")
        db.add(source)
    source.name = f"Clients {parent.id} · {parent.name[:100]}"
    source.enabled = True
    db.flush()
    return source


def _remote_id(record: dict) -> str:
    """A sighting's observation ID: its scope and MAC.

    Args:
        record: A sighting from the adapter.

    Returns:
        str: Such as ``opt1:020000001001``.
    """
    return f"{record['scope']}:{mac_address(record['mac'])}"


def reconcile_sightings(db: Any, parent: DiscoverySource, report: dict, now: datetime) -> DiscoverySource:
    """Store one poll's sightings, aging one only when the feeds that reported it finished.

    Each sighting keeps ``feeds``, the sections that reported it (``arp``,
    ``ndp``, ``dnsmasq_leases``, ``kea_leases``), and ``seen_feeds``, the
    neighbour tables that saw it. A sighting this poll did not see stays
    ``seen``, with its old addresses and ``seen_at``, unless every table
    that saw it finished; one this poll did not report at all becomes
    ``not_seen`` only when every feed that reported it finished. So a
    refused, failed, or capped read never ages what it reported, while a
    feed that never reported a sighting cannot keep it alive.

    Observed addresses (``ip_address``, ``ipv4``, ``ipv6``) come only from
    ARP and NDP, with ``seen_at``; a lease's address stays in ``lease``.

    Args:
        db: Open transaction (the router's own poll).
        parent: The router's source.
        report: The adapter's ``last_sightings``: ``records`` and
            ``finished`` (the feeds whose answer was the whole picture).
        now: This poll's time.

    Returns:
        DiscoverySource: The derived clients source.
    """
    source = derived_source(db, parent)
    source.last_attempt_at = now
    finished = set(report["finished"])
    stored = {device.remote_id: device for device in db.scalars(
        select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id))}
    others = list(db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id != source.id)))
    by_mac, guest_macs = endpoint_owners(db, others)
    devices = []
    for record in report["records"]:
        remote_id = _remote_id(record)
        before = json_object(stored[remote_id].details_json).get("client", {}) if remote_id in stored else {}
        feeds = list(record["feeds"])
        if record["seen"]:
            state, seen_at, ipv4, ipv6 = "seen", now.isoformat(), record["ipv4"], record["ipv6"]
            seen_feeds = [feed for feed in feeds if feed in NEIGHBOUR_FEEDS]
        elif before.get("state") == "seen" and not set(before.get("seen_feeds") or NEIGHBOUR_FEEDS) <= finished:
            # The tables that saw it were not read in full: keep that
            # sighting as it was (its addresses and time), but no newer one.
            state, seen_at, ipv4, ipv6 = "seen", before.get("seen_at"), before.get("ipv4") or [], before.get("ipv6") or []
            seen_feeds = before.get("seen_feeds") or list(NEIGHBOUR_FEEDS)
            feeds = sorted(set(feeds) | set(seen_feeds))
        else:
            state, seen_at, ipv4, ipv6, seen_feeds = "lease", before.get("seen_at"), [], [], []
        lease = record["lease"]
        address = ipv4[0] if ipv4 else None
        mac = mac_address(record["mac"])
        client = {
            "remote_id": remote_id, "scope": record["scope"], "network_name": record["network"],
            "mac_address": record["mac"], "ip_address": address, "ipv4": ipv4, "ipv6": ipv6,
            "hostname": record["hostname"], "manufacturer": record["manufacturer"], "lease": lease,
            "state": state, "seen_at": seen_at, "feeds": feeds, "seen_feeds": seen_feeds,
            # Which poll read this lease, to tell a current lease from history.
            "lease_sampled_at": report["sampled_at"] if lease else None,
            "connection": "unknown", "evidence": EVIDENCE,
            "parent_source_id": parent.id, "site_id": None, "sampled_at": now.isoformat(),
            "virtual_endpoint": mac in guest_macs,
        }
        if not client["virtual_endpoint"]:
            owners, match = interface_owners(mac, by_mac, adapter_block=not (address or ipv6 or lease)
                                             and not record["hostname"])
            if len(owners) == 1:
                client["represented_by"] = next(iter(owners))
                if match != EXACT:
                    client["represented_by_match"] = match
        devices.append(DeviceObservation(
            remote_id=remote_id, name=record["hostname"] or record["mac"], category="other_physical",
            identifiers={"mac": record["mac"], "management_ips": [address] if address else []},
            # No ``status``: a router sighting is never "online".
            details={"role": "client", "client": client, "ports_complete": True},
        ))
    apply_batch(db, source, DiscoveryBatch(devices=tuple(devices)), seen_at=now)
    current = {device.remote_id for device in devices}
    for remote_id, device in stored.items():
        details = json_object(device.details_json)
        client = details.get("client")
        if remote_id in current or not isinstance(client, dict) or client.get("state") == "not_seen":
            continue
        if set(client.get("feeds") or ALL_FEEDS) <= finished:
            client["state"] = "not_seen"
            device.details_json = json.dumps(details)
    db.flush()
    return source
