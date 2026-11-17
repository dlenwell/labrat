# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Hold each shared Python module's ``Used by:`` line to its real importers."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import python_used_by  # noqa: E402  (scripts/ is not a package)


class PythonUsedByTests(unittest.TestCase):
    """A module two or more others import names them; others name none."""

    def test_used_by_lines_match_imports(self) -> None:
        """Every module's docstring lists exactly the modules importing it."""
        problems = python_used_by.mismatches()
        self.assertEqual(problems, [], "Fix with scripts/python_used_by.py --write:\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
