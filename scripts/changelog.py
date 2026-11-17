#!/usr/bin/env python3
# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Draft a release's changelog entry from the commits since the last one.

The draft is a starting point, not the entry: ``CHANGELOG.md`` keeps each
release short and about what users get, so the draft is meant to be
compacted by hand (merge related lines, drop the minor ones) before it is
pasted in.

It reads the commits since the last ``v*`` tag (``scripts/publish.py``
tags each released commit), leaves out merges and commits that touch only
tests, docs, planning notes, CI, the website, or development scripts, and
sorts the rest into Added, Changed, and Fixed by each subject's first word.

Usage::

    .venv/bin/python scripts/changelog.py                 # since the last release
    .venv/bin/python scripts/changelog.py --since v0.5.1  # or since any commit
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
#: Commits that touch only these paths change nothing a user runs.
QUIET_PATHS = re.compile(
    r"^(tests/|docs/|planning/|scripts/|site/|\.github/|\.gitea/|AGENTS\.md$|[^/]+\.md$|\.git\w+$)"
)
#: Subjects that record work rather than change the app.
QUIET_SUBJECTS = re.compile(r"^(Backlog|Release spec|Record|Plan|Close|Document|Merge|Revert)\b")
#: The first word of a subject decides its section; anything else is Changed.
ADDED = {"Add", "Show", "Let", "Support", "Draw", "Read", "Detect", "Recognize", "Infer",
         "Collect", "Discover", "Give", "Track", "List", "Offer", "Allow", "Introduce"}
FIXED = {"Fix", "Keep", "Stop", "Correct", "Repair", "Handle", "Restore", "Avoid", "Prevent",
         "Limit", "Require", "End", "Spend", "Skip", "Ignore", "Refuse", "Guard"}
SECTIONS = ("Added", "Changed", "Fixed")


def git(*args: str) -> str:
    """Run Git in this checkout and return its output.

    Args:
        *args: Git's arguments.

    Returns:
        str: Standard output, stripped.
    """
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def last_release() -> str | None:
    """Find the most recent ``v*`` tag behind HEAD.

    Returns:
        str | None: The tag, or None before the first release.
    """
    try:
        return git("describe", "--tags", "--abbrev=0", "--match", "v*", "HEAD")
    except subprocess.CalledProcessError:
        return None


def commits(since: str | None) -> list[tuple[str, list[str]]]:
    """List non-merge commits after ``since``, oldest first, with their files.

    Args:
        since: A tag or commit; None means the whole history.

    Returns:
        list[tuple[str, list[str]]]: Each commit's subject and changed paths.
    """
    span = f"{since}..HEAD" if since else "HEAD"
    log = git("log", "--reverse", "--no-merges", "--format=%x1e%s", "--name-only", span)
    found = []
    for record in log.split("\x1e"):
        lines = [line for line in record.splitlines() if line.strip()]
        if lines:
            found.append((lines[0], lines[1:]))
    return found


def worth_listing(subject: str, paths: list[str]) -> bool:
    """Say whether a commit changes something users get.

    Args:
        subject: The commit's subject line.
        paths: The files it changed.

    Returns:
        bool: False for quiet commits (tests, docs, planning, CI, tooling).
    """
    if QUIET_SUBJECTS.match(subject):
        return False
    return any(not QUIET_PATHS.match(path) for path in paths)


def section_for(subject: str) -> str:
    """Pick a commit's section from its first word.

    Args:
        subject: The commit's subject line.

    Returns:
        str: ``Added``, ``Changed``, or ``Fixed``.
    """
    first = subject.split(maxsplit=1)[0] if subject.strip() else ""
    if first in ADDED:
        return "Added"
    if first in FIXED:
        return "Fixed"
    return "Changed"


def draft(version: str, since: str | None) -> str:
    """Build the draft entry.

    Args:
        version: The release's version.
        since: Where the previous release ended, or None.

    Returns:
        str: Markdown for one ``CHANGELOG.md`` entry, to compact by hand.
    """
    grouped: dict[str, list[str]] = {name: [] for name in SECTIONS}
    for subject, paths in commits(since):
        if worth_listing(subject, paths):
            line = subject.rstrip(".") + "."
            if line not in grouped[section_for(subject)]:
                grouped[section_for(subject)].append(line)
    parts = [f"## {version} ({date.today().isoformat()})"]
    for name in SECTIONS:
        if grouped[name]:
            parts.append(f"### {name}\n\n" + "\n".join(f"- {line}" for line in grouped[name]))
    return "\n\n".join(parts) + "\n"


def current_version() -> str:
    """Read the version this checkout will release.

    Returns:
        str: ``project.version`` from pyproject.toml.
    """
    match = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M)
    return match.group(1) if match else "unreleased"


def main(argv: list[str] | None = None) -> int:
    """Print the draft entry.

    Args:
        argv: Arguments without the program name; ``sys.argv`` by default.

    Returns:
        int: The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="tag or commit the previous release ended at (default: last v* tag)")
    parser.add_argument("--version", help="version for the heading (default: pyproject.toml's)")
    args = parser.parse_args(argv)
    since = args.since or last_release()
    print(draft(args.version or current_version(), since), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
