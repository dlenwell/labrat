# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Attribute an endpoint MAC to the host that owns the interface behind it.

Hypervisor hosts leak more MACs onto a switch than their network API reports.
A bond, bridge, or macvlan can transmit from a locally administered copy of a
member NIC's burned-in address (the U/L bit flipped, every other bit equal).
Multi-function adapters also expose storage and offload functions whose
addresses sit next to the port's own in the vendor's per-adapter block.

These rules only fold such addresses under the owning host for display. They
never create an inventory identity, and every rule refuses ambiguous owners.

Used by: api.clients, collector.clients, collector.sightings.
"""

from collections.abc import Iterable, Mapping

#: Match kinds, strongest first, as exposed to the UI for provenance.
EXACT = "exact"
LOCALLY_ADMINISTERED = "locally_administered"
ADAPTER_BLOCK = "adapter_block"

# Vendors assign each multi-port adapter a small consecutive block. Eight
# addresses cover two ports with their storage/offload functions while
# keeping separately allocated neighbouring adapters apart.
_BLOCK_MASK = 0xF8


def _first_octet(mac: str) -> int:
    """Return the first octet of a normalized twelve-digit MAC.

    Args:
        mac: Twelve lowercase hexadecimal digits.

    Returns:
        int: The first octet's value.
    """
    return int(mac[:2], 16)


def _universal(mac: str) -> bool:
    """Return whether a normalized MAC is a globally unique unicast address.

    Args:
        mac: Twelve lowercase hexadecimal digits.

    Returns:
        bool: True when neither the local nor the multicast bit is set.
    """
    return not _first_octet(mac) & 0x03


def _block(mac: str) -> tuple[str, int]:
    """Return the adapter-block key of a normalized MAC.

    Args:
        mac: Twelve lowercase hexadecimal digits.

    Returns:
        tuple[str, int]: The first five octets, and the last octet with its
            low three bits cleared: equal for MACs in one aligned block of
            eight.
    """
    return mac[:10], int(mac[10:], 16) & _BLOCK_MASK


def interface_owners(
    mac: str | None,
    index: Mapping[str, Iterable],
    *,
    adapter_block: bool = False,
) -> tuple[set, str | None]:
    """Find the owners of one MAC through exact or derived host interfaces.

    Args:
        mac: Normalized twelve-digit lowercase MAC of the endpoint.
        index: Every known non-endpoint MAC mapped to its owners. Derived
            rules check ambiguity against all of it, so pass the full index.
        adapter_block: Also accept a universal MAC in the same adapter block
            as known addresses. Callers allow this only for endpoints with no
            IP or hostname of their own, which a real device would report.

    Returns:
        tuple[set, str | None]: The candidate owners and the match kind, or
            an empty set and ``None``. More than one owner means ambiguous;
            callers must leave the endpoint unassociated.
    """
    if not mac:
        return set(), None
    if index.get(mac):
        return set(index[mac]), EXACT
    first = _first_octet(mac)
    if first & 0x01:
        return set(), None
    if first & 0x02:
        twin = f"{first ^ 0x02:02x}{mac[2:]}"
        if index.get(twin):
            return set(index[twin]), LOCALLY_ADMINISTERED
        return set(), None
    if adapter_block:
        key = _block(mac)
        owners = set()
        for known, known_owners in index.items():
            if known_owners and _universal(known) and _block(known) == key:
                owners.update(known_owners)
        if owners:
            return owners, ADAPTER_BLOCK
    return set(), None
