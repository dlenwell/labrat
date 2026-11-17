# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Declare the canonical SQLAlchemy models for LabRat's initial schema.

This module describes *inventory*, not the current operational state of a lab.
Locations contain equipment; equipment owns interfaces; interface association
and link tables describe network membership and physical cabling. User accounts
and authentication methods are modeled separately from the equipment inventory.

Relationship guide:
    Site -> Room -> Rack is the optional physical containment hierarchy.
    Device can refer to a Room, a Rack, another Device, or remain unplaced.
    Device -> Interface describes ownership of physical or logical ports.
    Device -> DeviceGroup -> Group supplies multi-valued capability labels
        while tracking manual and collector-originated claims separately.
    InterfaceLink connects two inventoried physical cable endpoints.
    InterfaceMembership groups ports into a logical bond or bridge.
    InterfaceNetwork assigns one or more VLANs to an interface.
    DeviceManagementInterface identifies management endpoints, including BMCs.
    User -> AuthIdentity and User -> LocalCredential keep login providers modular.

These models use explicit foreign keys and association tables, *not* SQLAlchemy
``relationship()`` properties. Callers must use joins or explicit queries to
traverse associations. Constraints enforce some basic invariants; higher-level
rules (such as rack collisions, physical port types, and coherent placement)
are reserved for the inventory service.

The schema originates in migration ``0001_initial``. Schema changes must be
introduced through Alembic, never by implicit ``create_all()`` at startup.
See ``docs/database-schema.md`` for an entity-relationship diagram and examples.

Used by: api.clients, api.locations, api.placement_rules, api.rack_devices,
api.topology, app, auth.providers, auth.session, collector.__main__,
integrations, inventory_backup, pages.activity, pages.navigation,
pages.private, pages.public.
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


class Site(Base):
    """Represent a separately managed physical site.

    A site might be a home, workshop, detached building, or a second property. It
    is the top of the physical location hierarchy, but need not imply a data center.

    Relationships:
        One site contains zero or more :class:`Room` records (``rooms.site_id``)
        and zero or more :class:`Network` records (``networks.site_id``). Networks
        are scoped to sites because a VLAN number may be reused at another site.

    Constraints:
        Names are unique across the entire inventory. A site has no required
        parent location.
    """

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class Room(Base):
    """Represent a physical room or space that belongs to a site.

    Rooms are optional organizational boundaries for equipment, not a requirement
    that every device be installed in a rack. A switch on a wall and a UPS on the
    floor can both belong directly to the same room.

    Relationships:
        Each room has exactly one :class:`Site` via ``site_id``. A room may
        contain many :class:`Rack` records and many directly placed :class:`Device`
        records. Devices mounted in a rack also derive a room through their rack.

    Constraints:
        Room names are unique only within their site, so different sites can both
        contain a room named ``Office``.
    """

    __tablename__ = "rooms"
    __table_args__ = (UniqueConstraint("site_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))


class Rack(Base):
    """Describe an optional equipment rack or cabinet within a room.

    Rack units are measured in ``height_u``. ``width_inches`` supports standard
    19-inch and compact 10-inch rack rails. ``has_rear`` controls whether the
    rear face can be used for placement; existing racks have both faces. A rack
    may contain many devices but has no direct device collection attribute;
    find its devices by querying their ``rack_id`` foreign key.

    Relationships:
        Each rack belongs to one :class:`Room`. A :class:`Device` may reference a
        rack using ``rack_id`` and its start unit using ``rack_u``.

    Constraints:
        Rack names are unique within a room and ``height_u`` must be positive.
        The database does not enforce device fit or prevent overlapping U positions;
        that belongs to inventory placement validation.
    """

    __tablename__ = "racks"
    __table_args__ = (
        UniqueConstraint("room_id", "name"),
        CheckConstraint("height_u > 0"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    height_u: Mapped[int] = mapped_column(Integer)
    width_inches: Mapped[int] = mapped_column(Integer, nullable=False, server_default="19")
    has_rear: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")


class DeviceClass(Base):
    """Describe an equipment category independently of product or adapter.

    Examples include server, switch, UPS, and management controller. This is a
    classification for inventory and UI presentation; discovery adapters are not
    identified by foreign keys to this table.

    Relationships:
        One device class may describe many :class:`Device` records via
        ``devices.device_class_id``. Classification is optional for a device,
        allowing new discoveries to remain unclassified until reviewed.

    Constraints:
        Class names are globally unique. This table does not dictate a device's
        ports, parent, or physical placement.
    """

    __tablename__ = "device_classes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str | None] = mapped_column(String(255))


class Device(Base):
    """Represent physical equipment or an independently modeled component.

    A device can be a whole server, switch, or UPS, or a component such as a BMC
    inside a server. ``parent_id`` links child equipment to another device; it is
    not the same as mounting the equipment in a rack. A device can also remain
    unplaced while discovery and operator review are in progress.

    Relationships:
        ``device_class_id`` -> optional :class:`DeviceClass` classification.
        ``parent_id`` -> optional parent :class:`Device` (self-reference); the
            relationship supports nesting, such as server -> management controller.
        ``room_id`` -> optional :class:`Room` for equipment on a shelf, wall, or
            floor. ``rack_id`` -> optional :class:`Rack` for rack-mounted equipment.
        ``id`` <- many :class:`Interface` owners through ``interfaces.device_id``.
        ``id`` <- zero or more :class:`DeviceManagementInterface` records that
            designate management interfaces, possibly owned by a child BMC device.
        ``id`` <- zero or more :class:`DeviceGroup` assertions linking to
            user-defined :class:`Group` labels with per-source provenance.

    Placement fields:
        ``rack_u`` is the starting rack unit if rack-mounted. ``height_u`` is the
        number of units occupied. ``placement`` describes out-of-rack positioning
        such as ``shelf``, ``wall``, or ``floor``; no enum is enforced yet.

    Constraints and limitations:
        Start unit and height must be positive when supplied, and ``rack_u``
        requires ``rack_id``. The database does *not* check whether ``room_id``
        agrees with the room containing ``rack_id``, detect rack-U collisions,
        prevent cyclic parent relationships, or automatically inherit placement
        from a parent. Those rules require inventory service validation.
    """

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("rack_u IS NULL OR rack_u > 0"),
        CheckConstraint("height_u IS NULL OR height_u > 0"),
        CheckConstraint("rack_u IS NULL OR rack_id IS NOT NULL"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    device_class_id: Mapped[int | None] = mapped_column(ForeignKey("device_classes.id"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"))
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id"))
    rack_id: Mapped[int | None] = mapped_column(ForeignKey("racks.id"))
    rack_u: Mapped[int | None] = mapped_column(Integer)
    height_u: Mapped[int | None] = mapped_column(Integer)
    placement: Mapped[str | None] = mapped_column(String(30))
    port_facing: Mapped[str | None] = mapped_column(String(8))
    manufacturer: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    asset_id: Mapped[str | None] = mapped_column(String(120))


class Interface(Base):
    """Represent one device-owned physical or logical network interface.

    Physical ports (for example ``enp3s0f0np0`` or a switch's numbered port)
    are distinct from logical interfaces (for example ``bond0`` or ``bond0.100``).
    The ``kind`` field classifies that distinction; ``speed_mbps`` is inventoried
    metadata, not proof of a currently negotiated live link speed.

    Relationships:
        ``device_id`` -> one owning :class:`Device`; a device may own many ports.
        ``parent_id`` -> an optional parent :class:`Interface`, useful for a VLAN
            subinterface on top of a bond or physical port.
        :class:`InterfaceMembership` associates the interface with a logical bond
            or bridge, without treating that association as a physical cable.
        :class:`InterfaceLink` references interfaces at each end of a physical
            cable. :class:`InterfaceNetwork` maps them to VLANs; :class:`IPAddress`
            stores one or more IP assignments.
        :class:`DeviceManagementInterface` may mark this port as a management
            endpoint for the owning device or a related chassis.

    Constraints and limitations:
        Interface names are unique per device. The schema does not yet restrict
        links to physical interfaces, require group members to share a device, or
        restrict allowed ``kind`` values. Validation belongs to the inventory API.
    """

    __tablename__ = "interfaces"
    __table_args__ = (UniqueConstraint("device_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("interfaces.id"))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(24), default="physical")
    mac: Mapped[str | None] = mapped_column(String(24))
    speed_mbps: Mapped[int | None] = mapped_column(Integer)


class InterfaceMembership(Base):
    """Associate a member interface with a logical interface group.

    Use this many-to-many table for physical ports participating in a bond or
    bridge. For example, one ``bond0`` :class:`Interface` may have memberships for
    two distinct physical NICs. A group membership does not imply a cable and does
    not mean that an aggregate can carry one flow at the sum of member speeds.

    Relationships:
        ``group_interface_id`` -> the logical group :class:`Interface`.
        ``member_interface_id`` -> a participating :class:`Interface`.
        Both references point to the same interface table; a composite primary
        key uniquely identifies each group/member pair.

    Constraints and limitations:
        Self-membership is prohibited. The database does not yet reject nested
        cycles, validate the ``kind`` of either endpoint, or restrict membership
        across devices. Higher-level validation will apply those rules.
    """

    __tablename__ = "interface_memberships"
    __table_args__ = (CheckConstraint("group_interface_id != member_interface_id"),)

    group_interface_id: Mapped[int] = mapped_column(
        ForeignKey("interfaces.id"), primary_key=True
    )
    member_interface_id: Mapped[int] = mapped_column(
        ForeignKey("interfaces.id"), primary_key=True
    )


class InterfaceLink(Base):
    """Describe an inventoried, undirected physical network cable.

    The link connects two concrete interface endpoints; it does not connect two
    whole devices or model a logical VLAN/bond membership. This separation makes
    physical traceability possible even when devices have many ports.

    Relationships:
        ``a_interface_id`` and ``b_interface_id`` each reference an :class:`Interface`
        on either end of the cable. Links are independent of VLAN assignments and
        other logical networking relationships.

    Constraints and limitations:
        Endpoints must be stored in ascending ID order (``a_interface_id`` less
        than ``b_interface_id``), and each ordered pair is unique. This prevents
        duplicate reversed links and self-links. It does not yet prevent one port
        from appearing in several distinct links, nor validate that both endpoints
        are physical. Discovery should propose links for review rather than
        overwriting confirmed cables.
    """

    __tablename__ = "interface_links"
    __table_args__ = (
        UniqueConstraint("a_interface_id", "b_interface_id"),
        CheckConstraint("a_interface_id < b_interface_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    a_interface_id: Mapped[int] = mapped_column(ForeignKey("interfaces.id"), index=True)
    b_interface_id: Mapped[int] = mapped_column(ForeignKey("interfaces.id"), index=True)


class Network(Base):
    """Represent a named VLAN scoped to a physical site.

    A network is distinct from a switch interface or physical cable. The same
    VLAN may be present on many ports, and different sites may reuse the same
    VLAN number without sharing one network record.

    Relationships:
        ``site_id`` -> one :class:`Site` that scopes the VLAN ID.
        :class:`InterfaceNetwork` creates a many-to-many association between
        this network and physical or logical :class:`Interface` records.

    Constraints and limitations:
        ``(site_id, vlan_id)`` must be unique; VLAN IDs must be 1 through 4094.
        A cross-site interface/network association is not currently rejected by
        the database and should be validated by the inventory service.
    """

    __tablename__ = "networks"
    __table_args__ = (
        UniqueConstraint("site_id", "vlan_id"),
        CheckConstraint("vlan_id BETWEEN 1 AND 4094"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    vlan_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))


class InterfaceNetwork(Base):
    """Associate one interface with one allowed VLAN.

    This is the many-to-many join table between :class:`Interface` and
    :class:`Network`: each interface may have several VLANs, and each VLAN may
    appear on several interfaces. A trunk can therefore represent multiple VLANs
    without duplicating physical interface records.

    Relationships:
        ``interface_id`` -> :class:`Interface`.
        ``network_id`` -> :class:`Network`.
        The two columns form the composite primary key, preventing duplicate
        assignments of the same network to the same interface.

    Limitations:
        No access-versus-trunk mode or tagging state is represented yet, and
        the database does not enforce that the interface belongs to the same
        site as the VLAN. This table represents configured inventory intent,
        not live forwarding-table observations.
    """

    __tablename__ = "interface_networks"

    interface_id: Mapped[int] = mapped_column(
        ForeignKey("interfaces.id"), primary_key=True
    )
    network_id: Mapped[int] = mapped_column(ForeignKey("networks.id"), primary_key=True)


class IPAddress(Base):
    """Record one IPv4 or IPv6 address assigned to an interface.

    IP addresses belong to network interfaces rather than directly to devices.
    This also supports BMC interfaces or a device with multiple management IPs.
    The optional prefix length stores network mask width.

    Relationships:
        ``interface_id`` -> one :class:`Interface`. An interface can have
        multiple :class:`IPAddress` rows for IPv4 and IPv6 or secondary addresses.

    Limitations:
        Addresses are stored as text to accommodate IPv4 and IPv6. This schema
        does not normalize their textual forms, validate prefix lengths, or
        guarantee global uniqueness; future API validation should handle those.
    """

    __tablename__ = "ip_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    interface_id: Mapped[int] = mapped_column(ForeignKey("interfaces.id"), index=True)
    address: Mapped[str] = mapped_column(String(45))
    prefix_length: Mapped[int | None] = mapped_column(Integer)


class DeviceManagementInterface(Base):
    """Identify one management endpoint for an inventoried device.

    The endpoint need not be an interface owned by the device itself. For example,
    a server can point at a network interface owned by its child BMC, allowing
    out-of-band management to remain independently identifiable.

    Relationships:
        ``device_id`` -> the managed :class:`Device`.
        ``interface_id`` -> its management :class:`Interface` endpoint (which may
        belong to a child :class:`Device`). A device may have several management
        endpoints; the composite primary key prevents identical associations.

    Design rationale:
        This join table avoids a direct circular devices/interfaces foreign-key
        dependency, which makes SQLite migrations simpler and keeps BMCs explicit.
        It does not select a single primary endpoint or verify that the referenced
        interface belongs to the managed device's component hierarchy.
    """

    __tablename__ = "device_management_interfaces"

    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    interface_id: Mapped[int] = mapped_column(ForeignKey("interfaces.id"), primary_key=True)


class User(Base):
    """Represent a LabRat account independently of how its owner logs in.

    ``User`` is the stable internal principal for authorization. ``is_admin`` and
    ``is_active`` are application flags rather than authentication-provider roles;
    this lets a local password and a future SSO identity resolve to one account.

    Relationships:
        One user can have zero or more :class:`AuthIdentity` records, one per
        external provider subject. A user can also have zero or one
        :class:`LocalCredential` record for password-based access.

    Constraints and limitations:
        Usernames are globally unique. ``created_at`` defaults to the database's
        current timestamp. Email, phone numbers, contact verification and alert
        preferences are intentionally not modeled in this initial schema.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    #: Copied into each signed-in session; bumping it ends every session of
    #: this account (signing out does), including copies of the cookie.
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class AuthIdentity(Base):
    """Map a provider-specific login identity to a LabRat user account.

    This table reserves a clean path to SSO (such as OIDC): after authenticating
    the external subject, the auth provider resolves the stable local user ID.
    The record does not hold a password and does not grant permissions itself.

    Relationships:
        ``user_id`` -> one :class:`User`. A user may have identities from more
        than one provider. ``provider`` identifies the authority and ``subject``
        is that authority's stable user identifier, not a display name.

    Constraints:
        ``(provider, subject)`` is globally unique to prevent one external
        identity from being attached to more than one LabRat account.
    """

    __tablename__ = "auth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(255))


class LocalCredential(Base):
    """Store the local password verifier for a LabRat user.

    Local password authentication is optional per user and independent of SSO.
    The password hash, not the plaintext password, is persisted. The authentication
    provider is responsible for Argon2id hashing and constant-time verification.

    Relationships:
        ``user_id`` is both the primary key and a foreign key to :class:`User`,
        giving each user at most one local password credential. A user may still
        have several :class:`AuthIdentity` records alongside this credential.

    Security:
        Never log, serialize, or expose ``password_hash`` in API responses.
        Local credentials must never contain a password recovery secret in clear
        text. Password policy is enforced by the authentication service.
    """

    __tablename__ = "local_credentials"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class Group(Base):
    """Name a reusable, user-defined capability or equipment grouping.

    A group is intentionally not restricted to a fixed enumeration or a
    single device class. For example, an inventory item can belong to
    ``gateway``, ``firewall``, ``switch``, and ``experimental`` simultaneously.
    Users may create additional names without changing the database schema.

    Relationships:
        :class:`DeviceGroup` attaches a group to any number of canonical
        :class:`Device` records. Membership is separate from the group's name
        and can retain independent manual and discovery-source assertions.

    Constraints:
        Names are globally unique using SQLite's case-insensitive NOCASE
        collation. The application should trim user input before creation.
        Deleting a group cascades to its memberships, not to devices.

    Note:
        Devices observed but not yet matched to canonical inventory remain in
        ``discovered_devices``; attaching groups to those observations will be
        handled by the later reconciliation and discovery UI work.
    """

    __tablename__ = "groups"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="nonempty_group_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80, collation="NOCASE"), unique=True)
    description: Mapped[str | None] = mapped_column(Text)


class DeviceGroup(Base):
    """Record one independently sourced group membership for a device.

    The primary key ``(device_id, group_id, source)`` permits multiple sources
    to assert the same classification without storing duplicate assertions
    from the *same* source. ``source='manual'`` denotes a user's assignment;
    collectors should use their stable discovery source name (for example,
    ``unifi:default``), never ``manual``.

    As a result, a future collector can add or remove its own assertion while
    preserving user-chosen group memberships. When displaying a device's
    capabilities, callers should select distinct groups across all sources.
    Removing a device or group cascades to corresponding memberships.

    Relationships:
        ``device_id`` references :class:`Device`; ``group_id`` references
        :class:`Group`. A device and a group can each have many memberships,
        and the ``source`` column preserves separate provenance.

    Note:
        Group changes made by the eventual user interface or collector must
        call ``labrat.audit.record_audit_event`` in the same transaction.
        Direct database writes are only a low-level storage mechanism.
    """

    __tablename__ = "device_groups"
    __table_args__ = (
        CheckConstraint("length(trim(source)) > 0", name="nonempty_group_source"),
    )

    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    source: Mapped[str] = mapped_column(
        String(160), primary_key=True, server_default="manual"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
