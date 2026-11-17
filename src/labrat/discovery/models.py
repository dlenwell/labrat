# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Store infrastructure discovery observations without treating them as fact.

A UniFi or Proxmox adapter produces observations from its own source namespace.
Observed devices, ports, and links are never silently promoted to authoritative
inventory. Explicit matches to existing devices and interfaces are optional.
Device objects remain available when absent from a later poll. Complete port
and link snapshots are reconciled so stale aliases and cable observations do
not continue to imply current physical state.

Used by: api.clients, api.cluster, api.common, api.network,
api.observation_actions, api.rack_devices, api.topology, collector.__main__,
collector.access_collection, collector.client_links, collector.clients,
collector.links, collector.nut_wired, collector.proxmox_clients,
collector.proxmox_cluster, collector.proxmox_live, collector.runner,
collector.sources, collector.store, collector.unifi_network,
collector.wireless_clients, discovery.observation_ignores,
discovery_settings, integrations, inventory_backup, pages.private,
pages.status.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from labrat.database import Base


class DiscoverySource(Base):
    """Identify a configured external collector without storing its secrets.

    ``config_name`` selects a local configuration entry. Credentials remain in
    the Git-ignored ``local_config.toml`` file, never in this table. ``kind``
    identifies an adapter (such as ``proxmox`` or ``unifi``); only registered
    adapters may poll a source.

    Last-attempt, success, and error fields are operational metadata, not
    evidence that every formerly seen asset is still reachable.
    """

    __tablename__ = "discovery_sources"
    __table_args__ = (UniqueConstraint("kind", "config_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    config_name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="1")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(255))
    #: Seconds between polls of the looping collector that last attempted
    #: this source; None until one records it (one-off runs leave it as is).
    poll_interval_seconds: Mapped[int | None] = mapped_column(Integer)


class DiscoveredDevice(Base):
    """Represent one source-scoped observed physical infrastructure device.

    ``remote_id`` is a stable identifier from the adapter, unique only within
    that source; the same server may be observed by Proxmox and UniFi and thus
    have two observations. ``device_id`` optionally associates an observation
    with a canonical inventory :class:`labrat.models.Device` after matching.

    Infrastructure and physical client endpoints live here, with clients in
    a separate derived source outside the infrastructure diagram. Virtual
    machines and containers are intentionally outside this physical graph. A fresh scan updates ``last_seen``
    without deleting earlier observations that temporarily disappear.
    """

    __tablename__ = "discovered_devices"
    __table_args__ = (UniqueConstraint("source_id", "remote_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("discovery_sources.id", ondelete="CASCADE"), index=True)
    remote_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40))
    identifiers_json: Mapped[str] = mapped_column(Text, server_default="{}")
    details_json: Mapped[str] = mapped_column(Text, server_default="{}")
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), index=True)
    visibility: Mapped[str] = mapped_column(String(24), server_default="recognized")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class DiscoveredPort(Base):
    """Describe a port observed on a discovered physical device.

    An observation has its own source-scoped identity and can optionally map
    to a canonical inventory interface via ``interface_id``. This is useful
    when adapter port names differ from the OS interface names.
    """

    __tablename__ = "discovered_ports"
    __table_args__ = (UniqueConstraint("discovered_device_id", "remote_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    discovered_device_id: Mapped[int] = mapped_column(
        ForeignKey("discovered_devices.id", ondelete="CASCADE"), index=True
    )
    remote_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(160))
    mac: Mapped[str | None] = mapped_column(String(24))
    speed_mbps: Mapped[int | None] = mapped_column(Integer)
    link_up: Mapped[bool | None] = mapped_column(Boolean)
    max_speed_mbps: Mapped[int | None] = mapped_column(Integer)
    connector_type: Mapped[str | None] = mapped_column(String(24))
    # Revision 0011: copper, dac, fiber_multimode, fiber_singlemode, or fiber.
    medium: Mapped[str | None] = mapped_column(String(24))
    interface_id: Mapped[int | None] = mapped_column(ForeignKey("interfaces.id", ondelete="SET NULL"), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class DiscoveredNetworkInterface(Base):
    """Store source-scoped logical network interfaces for one observed device.

    Bonds, bridges, and VLAN interfaces describe how the host's network stack
    is configured. They are not physical cable endpoints; cable observations
    continue to reference :class:`DiscoveredPort` only.

    Relationships:
        ``discovered_device_id`` -> the source-scoped observed device.
        ``parent_name`` and ``members_json`` refer to sibling interfaces or
        physical ports by the names reported by that same source.
    """

    __tablename__ = "discovered_network_interfaces"
    __table_args__ = (
        UniqueConstraint("discovered_device_id", "remote_id"),
        CheckConstraint("kind IN ('bond', 'bridge', 'vlan')"),
        CheckConstraint("vlan_id IS NULL OR vlan_id BETWEEN 1 AND 4094"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discovered_device_id: Mapped[int] = mapped_column(
        ForeignKey("discovered_devices.id", ondelete="CASCADE"), index=True,
    )
    remote_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(24), index=True)
    parent_name: Mapped[str | None] = mapped_column(String(160))
    vlan_id: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str | None] = mapped_column(String(80))
    members_json: Mapped[str] = mapped_column(Text, server_default="[]")
    active: Mapped[bool | None] = mapped_column(Boolean)
    details_json: Mapped[str] = mapped_column(Text, server_default="{}")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(),
    )


class DiscoveredLink(Base):
    """Record an observed candidate connection between two physical ports.

    ``evidence`` describes how the adapter obtained this observation (for
    example LLDP). ``confidence`` distinguishes direct verified neighbor data
    from mere inference. A learned MAC address is **not** proof of a cable.

    These rows are *not* the authoritative ``interface_links`` table. Manual
    mappings, locks, and discrepancies will be reconciled in Step 4B; changing
    a switch port must never silently alter a manually locked cable.
    """

    __tablename__ = "discovered_links"
    __table_args__ = (
        UniqueConstraint("source_id", "a_port_id", "b_port_id"),
        CheckConstraint("a_port_id < b_port_id", name="ordered_discovered_ports"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("discovery_sources.id", ondelete="CASCADE"), index=True)
    a_port_id: Mapped[int] = mapped_column(ForeignKey("discovered_ports.id", ondelete="CASCADE"))
    b_port_id: Mapped[int] = mapped_column(ForeignKey("discovered_ports.id", ondelete="CASCADE"))
    evidence: Mapped[str] = mapped_column(String(80))
    confidence: Mapped[str] = mapped_column(String(24))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class InterfaceLinkPolicy(Base):
    """Preserve an operator's authority over one canonical network cable.

    By default, links without a policy can be automatically reconciled after
    suitable evidence. ``manual_locked`` prevents an adapter changing the
    canonical endpoints. A later conflicting observation should be presented
    for review; ``suppress_conflicts`` hides repetitive alerts for this *link*
    only, without suppressing collection or other discrepancies.
    """

    __tablename__ = "interface_link_policies"

    interface_link_id: Mapped[int] = mapped_column(
        ForeignKey("interface_links.id", ondelete="CASCADE"), primary_key=True
    )
    manual_locked: Mapped[bool] = mapped_column(Boolean, server_default="0")
    suppress_conflicts: Mapped[bool] = mapped_column(Boolean, server_default="0")


class ObservationIgnore(Base):
    """Suppress one operations condition without changing discovery evidence.

    The canonical JSON target uses source-scoped remote identities, never a
    transient discovered port or cable ID. A source deletion removes its rules;
    deleting the creating user preserves the policy and its audit history.
    """

    __tablename__ = "observation_ignores"
    __table_args__ = (CheckConstraint("json_valid(target_json)", name="valid_ignore_target"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("discovery_sources.id", ondelete="CASCADE"), index=True)
    target_json: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())
