# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Independent, read-only infrastructure discovery collector for LabRat.

The daemon never imports Flask and never handles browser requests. Adapters
collect facts; the persistence layer writes observed facts to SQLite. Proxmox and UniFi adapters are loaded by the command entry point.

Used by: collector.adapters.opnsense, collector.runner, integrations.
"""
