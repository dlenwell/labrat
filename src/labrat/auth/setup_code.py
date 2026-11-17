# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Guard first-run setup with a one-time code only the operator can read.

Until the first administrator exists, anyone who reaches the setup page
could create that account. The setup form therefore also asks for a code
that the web app writes beside the database (readable only by the service
account) and prints to its log at start, so only someone with access to the
server, its logs, or its data folder can finish setup. The file is removed
once the administrator exists.

Every web worker reads the same file, so they all accept the same code.

Used by: app, pages.public.
"""

import hmac
import os
from pathlib import Path
import secrets
import sys


def setup_code_path(database: Path) -> Path:
    """Place the code file beside the database it guards.

    Args:
        database: The SQLite database's path.

    Returns:
        Path: ``<database>.setup-code``.
    """
    return database.with_name(database.name + ".setup-code")


def new_code() -> str:
    """Make a code that is easy to type and impractical to guess.

    Returns:
        str: Twelve hex digits in three groups, such as ``3f9a-0c1e-b27d``.
    """
    digits = secrets.token_hex(6)
    return "-".join(digits[index:index + 4] for index in range(0, 12, 4))


def ensure_setup_code(path: Path) -> str:
    """Return the current code, creating it (and logging it) if needed.

    Args:
        path: The code file.

    Returns:
        str: The code the setup form must be given.
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pass
    code = new_code()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Another worker wrote it first; use theirs.
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(code + "\n")
    announce(code, path)
    return code


def announce(code: str, path: Path) -> None:
    """Print the code where the operator will look for it.

    Args:
        code: The setup code.
        path: The file that also holds it.
    """
    # Straight to stderr: the web app configures no logging of its own, and
    # this must reach the terminal, journal, or container log regardless.
    print(f"LabRat first-time setup code: {code} (also in {path})", file=sys.stderr, flush=True)


def setup_code_matches(path: Path, submitted: object) -> bool:
    """Compare a submitted code with the saved one in constant time.

    Spaces and letter case are ignored, so a code read aloud or copied from
    a log still matches.

    Args:
        path: The code file.
        submitted: The form value.

    Returns:
        bool: True when the code is right.
    """
    if not isinstance(submitted, str):
        return False
    try:
        expected = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False
    given = "".join(submitted.split()).lower()
    return bool(expected) and hmac.compare_digest(expected.encode(), given.encode())


def remove_setup_code(path: Path) -> None:
    """Delete the code once setup is done.

    Args:
        path: The code file.
    """
    path.unlink(missing_ok=True)
