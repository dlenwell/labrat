# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Vendor-specific physical-infrastructure discovery implementations.

Adapters produce source-scoped observations without direct database access.
All collectors use GET-only vendor API calls and exclude guest workloads and
ordinary network clients from initial physical inventory discovery.
"""
