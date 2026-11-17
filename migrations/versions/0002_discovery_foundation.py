# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Add attributes, flexible placement, discovery and change auditing.

Revision 0002 follows the initial 15-table schema. Existing rack and room
placements are copied into the new container model without deleting the legacy
fields. Registry triggers make attribute ownership work for new and old rows.

Data in new Step 4A tables is lost on downgrade. Make a backup before downgrading.
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_discovery_foundation"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OWNER_TABLES = (("sites", "site_id"), ("rooms", "room_id"), ("racks", "rack_id"), ("devices", "device_id"))


def upgrade() -> None:
    """Create the Step 4A schema and preserve existing equipment placements."""
    op.create_table(
        "inventory_objects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", ondelete="CASCADE"), unique=True),
        sa.Column("room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="CASCADE"), unique=True),
        sa.Column("rack_id", sa.Integer(), sa.ForeignKey("racks.id", ondelete="CASCADE"), unique=True),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), unique=True),
        sa.CheckConstraint(
            "(site_id IS NOT NULL) + (room_id IS NOT NULL) + "
            "(rack_id IS NOT NULL) + (device_id IS NOT NULL) = 1",
            name="exactly_one_entity",
        ),
    )
    op.create_table(
        "attributes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("inventory_objects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("attributes.id", ondelete="SET NULL")),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.CheckConstraint("json_valid(value)", name="valid_json"),
    )
    op.create_index("ix_attributes_owner_id", "attributes", ["owner_id"])
    op.create_index("ix_attributes_parent_id", "attributes", ["parent_id"])
    op.create_index("ix_attributes_key", "attributes", ["key"])

    for table, column in _OWNER_TABLES:
        # These SQLite triggers automatically register future owner rows. They
        # create no reverse FK cycle because the registry references the owner.
        op.execute(
            f"CREATE TRIGGER labrat_register_{table} AFTER INSERT ON {table} "
            f"BEGIN INSERT INTO inventory_objects ({column}) VALUES (NEW.id); END"
        )
        op.execute(
            f"INSERT INTO inventory_objects ({column}) SELECT id FROM {table}"
        )

    op.create_table(
        "placement_containers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("room_id", sa.Integer(), sa.ForeignKey("rooms.id"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("placement_containers.id")),
        sa.Column("rack_id", sa.Integer(), sa.ForeignKey("racks.id"), unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.CheckConstraint("parent_id IS NULL OR parent_id != id", name="no_self_parent"),
        sa.CheckConstraint(
            "(kind = 'rack' AND rack_id IS NOT NULL) OR "
            "(kind != 'rack' AND rack_id IS NULL)",
            name="rack_has_reference",
        ),
    )
    op.create_index("ix_placement_containers_room_id", "placement_containers", ["room_id"])
    op.create_index("ix_placement_containers_parent_id", "placement_containers", ["parent_id"])
    op.create_table(
        "device_placements",
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("container_id", sa.Integer(), sa.ForeignKey("placement_containers.id"), nullable=False),
        sa.Column("rack_u", sa.Integer()),
        sa.Column("face", sa.String(8)),
        sa.Column("position_json", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("rack_u IS NULL OR rack_u > 0", name="valid_rack_u"),
        sa.CheckConstraint("face IS NULL OR face IN ('front', 'rear')", name="valid_face"),
    )
    op.create_index("ix_device_placements_container_id", "device_placements", ["container_id"])

    # A rack has exactly one backing rack-type container. New racks get one
    # automatically; inventory UI can create other kinds of container freely.
    op.execute(
        "INSERT INTO placement_containers (room_id, rack_id, name, kind) "
        "SELECT room_id, id, name, 'rack' FROM racks"
    )
    op.execute(
        "CREATE TRIGGER labrat_new_rack_container AFTER INSERT ON racks "
        "BEGIN INSERT INTO placement_containers (room_id, rack_id, name, kind) "
        "VALUES (NEW.room_id, NEW.id, NEW.name, 'rack'); END"
    )
    op.execute(
        "INSERT INTO device_placements (device_id, container_id, rack_u) "
        "SELECT d.id, pc.id, d.rack_u FROM devices d "
        "JOIN placement_containers pc ON pc.rack_id = d.rack_id "
        "WHERE d.rack_id IS NOT NULL"
    )
    op.execute(
        "INSERT INTO placement_containers (room_id, name, kind) "
        "SELECT DISTINCT d.room_id, 'Unspecified room area', 'area' "
        "FROM devices d WHERE d.room_id IS NOT NULL AND d.rack_id IS NULL"
    )
    op.execute(
        "INSERT INTO device_placements (device_id, container_id) "
        "SELECT d.id, pc.id FROM devices d "
        "JOIN placement_containers pc ON pc.room_id = d.room_id AND pc.kind = 'area' "
        "WHERE d.rack_id IS NULL AND d.room_id IS NOT NULL"
    )

    op.create_table(
        "discovery_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("config_name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(255)),
        sa.UniqueConstraint("kind", "config_name"),
    )
    op.create_table(
        "discovered_devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("discovery_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("remote_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("identifiers_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("details_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="SET NULL")),
        sa.Column("visibility", sa.String(24), server_default="recognized", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("source_id", "remote_id"),
    )
    op.create_index("ix_discovered_devices_source_id", "discovered_devices", ["source_id"])
    op.create_index("ix_discovered_devices_device_id", "discovered_devices", ["device_id"])
    op.create_table(
        "discovered_ports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("discovered_device_id", sa.Integer(), sa.ForeignKey("discovered_devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("remote_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("mac", sa.String(24)),
        sa.Column("speed_mbps", sa.Integer()),
        sa.Column("interface_id", sa.Integer(), sa.ForeignKey("interfaces.id", ondelete="SET NULL")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("discovered_device_id", "remote_id"),
    )
    op.create_index("ix_discovered_ports_discovered_device_id", "discovered_ports", ["discovered_device_id"])
    op.create_index("ix_discovered_ports_interface_id", "discovered_ports", ["interface_id"])
    op.create_table(
        "discovered_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("discovery_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("a_port_id", sa.Integer(), sa.ForeignKey("discovered_ports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("b_port_id", sa.Integer(), sa.ForeignKey("discovered_ports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence", sa.String(80), nullable=False),
        sa.Column("confidence", sa.String(24), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("a_port_id < b_port_id", name="ordered_discovered_ports"),
        sa.UniqueConstraint("source_id", "a_port_id", "b_port_id"),
    )
    op.create_index("ix_discovered_links_source_id", "discovered_links", ["source_id"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Integer()),
        sa.Column("actor_name", sa.String(120)),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("subject_kind", sa.String(80), nullable=False),
        sa.Column("subject_id", sa.Integer()),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.CheckConstraint("actor_kind IN ('user', 'collector', 'system')", name="valid_actor_kind"),
        sa.CheckConstraint("json_valid(details_json)", name="valid_details_json"),
    )
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])
    op.create_index("ix_audit_events_subject", "audit_events", ["subject_kind", "subject_id"])
    op.create_index("ix_audit_events_actor", "audit_events", ["actor_kind", "actor_id"])
    # Append-only enforcement is also at the database level, not just API code.
    op.execute(
        "CREATE TRIGGER labrat_audit_no_update BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER labrat_audit_no_delete BEFORE DELETE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END"
    )

    op.create_table(
        "interface_link_policies",
        sa.Column("interface_link_id", sa.Integer(), sa.ForeignKey("interface_links.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("manual_locked", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("suppress_conflicts", sa.Boolean(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    """Remove Step 4A tables and triggers, preserving the Step 2 schema.

    Warning:
        Any Step 4A attributes, observations, and placements will be lost.
    """
    op.drop_table("interface_link_policies")
    op.execute("DROP TRIGGER IF EXISTS labrat_audit_no_update")
    op.execute("DROP TRIGGER IF EXISTS labrat_audit_no_delete")
    op.drop_index("ix_audit_events_actor", table_name="audit_events")
    op.drop_index("ix_audit_events_subject", table_name="audit_events")
    op.drop_index("ix_audit_events_occurred_at", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_discovered_links_source_id", table_name="discovered_links")
    op.drop_table("discovered_links")
    op.drop_index("ix_discovered_ports_interface_id", table_name="discovered_ports")
    op.drop_index("ix_discovered_ports_discovered_device_id", table_name="discovered_ports")
    op.drop_table("discovered_ports")
    op.drop_index("ix_discovered_devices_device_id", table_name="discovered_devices")
    op.drop_index("ix_discovered_devices_source_id", table_name="discovered_devices")
    op.drop_table("discovered_devices")
    op.drop_table("discovery_sources")
    op.execute("DROP TRIGGER IF EXISTS labrat_new_rack_container")
    op.drop_index("ix_device_placements_container_id", table_name="device_placements")
    op.drop_table("device_placements")
    op.drop_index("ix_placement_containers_parent_id", table_name="placement_containers")
    op.drop_index("ix_placement_containers_room_id", table_name="placement_containers")
    op.drop_table("placement_containers")
    for table, _ in _OWNER_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS labrat_register_{table}")
    op.drop_index("ix_attributes_key", table_name="attributes")
    op.drop_index("ix_attributes_parent_id", table_name="attributes")
    op.drop_index("ix_attributes_owner_id", table_name="attributes")
    op.drop_table("attributes")
    op.drop_table("inventory_objects")
