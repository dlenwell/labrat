# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Provide reusable LabRat authentication providers and session utilities.

Authentication providers validate credentials and return local user IDs; Flask
sessions and authorization rules remain independent of the provider in use.
"""
