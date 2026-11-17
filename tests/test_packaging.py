# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check that a regular (non-editable) install ships every app file.

An editable install reads files straight from ``src/``, so a missing
``package-data`` pattern only shows up in a wheel or container install,
as a broken page or image. This test reads ``pyproject.toml`` and checks
every non-Python file in the package against its patterns.
"""

from fnmatch import fnmatchcase
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "labrat"


class PackageDataTests(unittest.TestCase):
    """Every shipped asset matches a ``package-data`` pattern."""

    def test_every_asset_is_packaged(self) -> None:
        """List app files a wheel would leave out (placeholders excepted)."""
        settings = tomllib.loads((ROOT / "pyproject.toml").read_text())
        patterns = settings["tool"]["setuptools"]["package-data"]["labrat"]
        missing = []
        for path in PACKAGE.rglob("*"):
            if (not path.is_file() or path.suffix in {".py", ".pyc"} or "__pycache__" in path.parts
                    or path.name.startswith(".")):
                continue
            relative = path.relative_to(PACKAGE).as_posix()
            # A pattern's "*" stops at a slash, as setuptools' globs do.
            if not any(fnmatchcase(relative, pattern) and relative.count("/") == pattern.count("/")
                       for pattern in patterns):
                missing.append(relative)
        self.assertEqual(missing, [], "add these to [tool.setuptools.package-data] in pyproject.toml")


if __name__ == "__main__":
    unittest.main()
