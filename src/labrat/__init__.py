# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""LabRat: a local-first homelab inventory and telemetry application.

This package provides the backend components for LabRat, including
the Flask web application, infrastructure inventory, and eventually
the background discovery and telemetry collectors.

Used by: app, collector.__main__, pages.settings_backup, startup.
"""

from importlib.metadata import PackageNotFoundError, version as _package_version

__version__ = "0.5.4"


def app_version() -> str:
    """Return the installed LabRat version, as the footer and exports show it.

    The installed package's metadata, not ``__version__``, so a checkout
    shows what pip installed; ``dev`` from a bare source tree.

    Returns:
        str: The version, or ``dev``.
    """
    try:
        return _package_version("labrat")
    except PackageNotFoundError:
        return "dev"
