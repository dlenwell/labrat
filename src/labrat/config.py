
# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Load local application configuration for LabRat.

This module provides access to configuration stored in a local TOML
file, including credentials used by backend integrations.

By default, configuration is loaded from ``local_config.toml`` in the
current working directory. The ``LABRAT_CONFIG`` environment variable
can override this location.

If the default configuration file is absent, an empty dictionary is
returned. If an explicitly configured file is missing, an exception
is raised to prevent silently starting with unintended settings.

Configuration may contain sensitive credentials and must never be
exposed through API responses, logs, or version control.

Python 3.11 or newer is required for the standard library ``tomllib``
module.

Used by: app, collector.__main__.
"""

import os
from pathlib import Path
import tomllib


def local_config_path() -> Path:
    """Resolve the active private TOML file without reading or exposing it.

    Returns:
        Path: ``LABRAT_CONFIG`` when set, else ``local_config.toml`` in the
            working directory.
    """
    explicit = os.environ.get("LABRAT_CONFIG")
    return Path(explicit).expanduser() if explicit else Path.cwd() / "local_config.toml"


def load_local_config() -> dict:
    """Load and parse the application's local TOML configuration.

    Resolve the configuration file path using the ``LABRAT_CONFIG``
    environment variable when set, expanding a leading ``~`` to the
    user's home directory. Otherwise, use ``local_config.toml`` in
    the current working directory.

    Missing default configuration is permitted to support development
    and testing without requiring external service credentials.
    Explicitly configured paths must exist.

    Returns:
        dict: Parsed TOML configuration as a dictionary. Returns an
        empty dictionary when the default configuration file does
        not exist.

    Raises:
        FileNotFoundError: If ``LABRAT_CONFIG`` specifies a path that
            does not refer to an existing regular file.
        tomllib.TOMLDecodeError: If the configuration file contains
            invalid TOML syntax.
        OSError: If the configuration file cannot be opened or read.

    Note:
        This function reads configuration on every invocation and
        does not cache the result. Callers should avoid logging the
        returned dictionary because it may contain credentials.
    """
    explicit_path = os.environ.get("LABRAT_CONFIG")
    path = local_config_path()

    if not path.is_file():
        if explicit_path:
            raise FileNotFoundError(f"LABRAT_CONFIG file not found: {path}")
        return {}

    with path.open("rb") as file:
        return tomllib.load(file)
