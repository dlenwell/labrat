# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Typed data contract shared by all physical-infrastructure adapters.

The contract omits virtual machines and containers. Client endpoints use
``other_physical`` observations in their own derived source, outside the
infrastructure diagram. Adapter-returned IDs are scoped to their source configuration;
port references are scoped to the remote IDs in the same batch.

Used by: collector.adapters.mndp, collector.adapters.nut,
collector.adapters.pihole, collector.adapters.proxmox,
collector.adapters.unifi, collector.clients, collector.runner,
collector.store.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


PHYSICAL_CATEGORIES = frozenset({
    "server", "switch", "router", "firewall", "access_point", "ups",
    "pdu", "modem", "management_controller", "other_physical",
})


@dataclass(frozen=True)
class DeviceObservation:
    """Identify a physical device and optionally include source metadata.

    Attributes:
        remote_id: Stable source-specific device identifier.
        name: Human-readable observed name.
        category: Supported physical equipment classification.
        identifiers: Identity clues such as serial number or stable MAC address.
        details: Additional source-provided details for future reconciliation.
    """

    remote_id: str
    name: str
    category: str
    identifiers: Mapping[str, Any] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PortObservation:
    """Identify one physical network port on a device in this batch.

    Attributes:
        device_remote_id: Remote ID of the owning observed device.
        remote_id: Port ID unique within the observed device.
        name: User-visible port label.
        mac: Optional reported port MAC address.
        speed_mbps: Optional reported physical link capacity.
        max_speed_mbps: Maximum reported physical port speed.
        link_up: Explicit current physical-link state when the source provides it.
        connector_type: Physical connector kind such as ``rj45`` or ``sfp_plus``.
        medium: Cable medium in use: ``copper`` (RJ45 Ethernet), ``dac``
            (direct-attach twinax), ``fiber_multimode``, ``fiber_singlemode``,
            or ``fiber`` when the optical mode is unknown.
    """

    device_remote_id: str
    remote_id: str
    name: str
    mac: str | None = None
    speed_mbps: int | None = None
    link_up: bool | None = None
    max_speed_mbps: int | None = None
    connector_type: str | None = None
    medium: str | None = None


@dataclass(frozen=True)
class NetworkInterfaceObservation:
    """Describe a logical network interface without treating it as a cable.

    Attributes:
        device_remote_id: Source device that owns the interface.
        remote_id: Stable interface name reported by the source.
        name: Human-readable interface name.
        kind: Logical interface kind: ``bond``, ``bridge``, or ``vlan``.
        parent_name: Lower interface for a VLAN, when reported.
        vlan_id: 802.1Q tag for a VLAN interface, when reported.
        mode: Bonding mode, when reported.
        members: Physical or logical interface names used by a bond or bridge.
        active: Explicit interface state, or ``None`` when unknown.
        details: Curated configuration metadata such as addresses and bridge
            VLAN filtering. Never include an unfiltered controller response.
    """

    device_remote_id: str
    remote_id: str
    name: str
    kind: str
    parent_name: str | None = None
    vlan_id: int | None = None
    mode: str | None = None
    members: tuple[str, ...] = ()
    active: bool | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


#: Cable media a port observation may report (see PortObservation.medium).
CABLE_MEDIA = frozenset({"copper", "dac", "fiber_multimode", "fiber_singlemode", "fiber"})


@dataclass(frozen=True)
class LinkObservation:
    """Describe evidence for a candidate physical cable, not canonical wiring.

    Attributes:
        a_device_remote_id: Source device at one end of the observation.
        a_port_remote_id: Port at that endpoint.
        b_device_remote_id: Source device at the other end.
        b_port_remote_id: Port at the other endpoint.
        evidence: Discovery method, such as LLDP or controller neighbor data.
        confidence: ``confirmed``, ``inferred``, or ``ambiguous``.
    """

    a_device_remote_id: str
    a_port_remote_id: str
    b_device_remote_id: str
    b_port_remote_id: str
    evidence: str
    confidence: str


@dataclass(frozen=True)
class WanObservation:
    """Live internet-uplink state of one gateway, as its source reports it.

    This is telemetry, not inventory: it is stored on the gateway's device
    observation (see ``store.record_wan``) and never counts as a change.

    Attributes:
        gateway_mac: MAC of the gateway device observation it belongs to.
        snapshot: ``internet`` and ``uplinks`` mappings, with optional nested
            ``vpn`` telemetry containing only whitelisted status fields.
    """

    gateway_mac: str
    snapshot: Mapping[str, Any]


@dataclass(frozen=True)
class DiscoveryBatch:
    """Hold one atomic batch of observations from a single integration source.

    Attributes:
        devices: Recognized physical infrastructure devices only.
        ports: Physical ports belonging to devices in this batch.
        network_interfaces: Logical bonds, bridges, and VLAN interfaces.
        links: Evidence-backed candidate connections between those ports.
        wan: Live uplink state of gateways in this batch.
    """

    devices: tuple[DeviceObservation, ...] = ()
    ports: tuple[PortObservation, ...] = ()
    network_interfaces: tuple[NetworkInterfaceObservation, ...] = ()
    links: tuple[LinkObservation, ...] = ()
    wan: tuple[WanObservation, ...] = ()


class DiscoveryAdapter(Protocol):
    """Define the read-only interface implemented by integration adapters."""

    def poll(self, settings: Mapping[str, Any]) -> DiscoveryBatch:
        """Collect one batch using local configuration, without writing to SQLite.

        Args:
            settings: Credentials and options loaded strictly in the backend.

        Returns:
            DiscoveryBatch: Observed physical equipment and confirmed or
                candidate links, with logical network configuration kept
                separate from physical cable endpoints. Missing prior devices
                must not be treated as proof of removal.
        """
        ...
