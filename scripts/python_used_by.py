# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Work out which LabRat modules each Python module is imported by.

A module that two or more others import names them in its docstring, in a
paragraph starting ``Used by:`` (``Used by: api.clients, collector.store.``),
so whoever changes it sees what else to check. ``tests/test_python_used_by.py``
holds every module to its real importers; run this script with ``--write``
to rewrite the paragraphs after imports change.

Usage::

    .venv/bin/python scripts/python_used_by.py           # list mismatches
    .venv/bin/python scripts/python_used_by.py --write   # fix them
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "labrat"
_USED_BY = re.compile(r"^Used by: (.*?)\.?$", re.S)


def module_name(path: Path) -> str:
    """Name a source file as a module relative to the package.

    Args:
        path: A file under src/labrat.

    Returns:
        str: Such as ``api.clients``; ``labrat`` for the package itself.
    """
    parts = path.relative_to(PACKAGE).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "labrat"


def importers() -> dict[str, set[str]]:
    """Map each module to the modules that import it.

    Returns:
        dict[str, set[str]]: Importing module names by imported module.
    """
    modules = {module_name(path): path for path in PACKAGE.rglob("*.py")}
    used: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets = []
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("labrat"):
                targets = [node.module]
                targets += [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names if alias.name.startswith("labrat")]
            for target in targets:
                short = target.removeprefix("labrat.") if target != "labrat" else "labrat"
                if short in used and short != name:
                    used[short].add(name)
    return used


def paths() -> dict[str, Path]:
    """Map module names to their files.

    Returns:
        dict[str, Path]: Each module's source file.
    """
    return {module_name(path): path for path in PACKAGE.rglob("*.py")}


def documented(path: Path) -> set[str] | None:
    """Read the modules a file's docstring says use it.

    Args:
        path: The module's source file.

    Returns:
        set[str] | None: The names in its ``Used by:`` paragraph, or None
            when it has none.
    """
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    for paragraph in doc.split("\n\n"):
        found = _USED_BY.match(paragraph.strip())
        if found:
            return {name.strip() for name in re.split(r",\s*|\s+and\s+", " ".join(found.group(1).split())) if name.strip()}
    return None


def mismatches() -> list[str]:
    """List modules whose ``Used by:`` paragraph is missing, wrong, or stale.

    Returns:
        list[str]: One message per module, with the line it should carry.
    """
    used, files, problems = importers(), paths(), []
    for name in sorted(files):
        want = used.get(name, set())
        have = documented(files[name])
        if len(want) >= 2 and have != want:
            problems.append(f"{files[name].relative_to(ROOT)}: Used by: {', '.join(sorted(want))}.")
        elif len(want) < 2 and have is not None:
            problems.append(f"{files[name].relative_to(ROOT)}: drop its Used by: paragraph ({len(want)} importer)")
    return problems


def write() -> None:
    """Rewrite every module's ``Used by:`` paragraph to match its importers."""
    used, files = importers(), paths()
    for name, path in files.items():
        want = sorted(used.get(name, set()))
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        node = tree.body[0] if tree.body and isinstance(tree.body[0], ast.Expr) else None
        if node is None or not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        doc = ast.get_docstring(tree)
        paragraphs = [p for p in doc.split("\n\n") if not _USED_BY.match(p.strip())]
        if len(want) >= 2:
            line = "Used by: " + ", ".join(want) + "."
            paragraphs.append("\n".join(textwrap.wrap(line, 76, break_on_hyphens=False)))
        new_doc = "\n\n".join(paragraphs)
        quote = '"""'
        body = quote + new_doc + ("\n" + quote if "\n" in new_doc else quote)
        lines = source.split("\n")
        lines[node.lineno - 1:node.end_lineno] = body.split("\n")
        path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
    found = mismatches()
    print("\n".join(found))
    sys.exit(1 if found else 0)
