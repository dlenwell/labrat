# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check how scripts/gate.py turns tool output into one-line summaries."""

import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("gate", Path(__file__).resolve().parents[1] / "scripts" / "gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

RULE = "-" * 70


class UnittestSummaryTests(unittest.TestCase):
    """Summaries carry the count and verdict; failures keep only their detail."""

    def test_passing_run(self):
        """A clean run reports its count and time, with no detail."""
        ok, summary, detail = gate.unittest_summary(f"{RULE}\nRan 236 tests in 20.302s\n\nOK\n")
        self.assertEqual((ok, summary, detail), (True, "236 tests (20.3 s)", ""))

    def test_skips_are_reported(self):
        """Skips stay visible in the summary."""
        _, summary, _ = gate.unittest_summary(f"{RULE}\nRan 3 tests in 0.010s\n\nOK (skipped=1)\n")
        self.assertEqual(summary, "3 tests (0.0 s), skipped=1")

    def test_failing_run_keeps_failures_without_trailer(self):
        """The detail runs from the first failure to unittest's closing rule."""
        output = (
            "=" * 70 + "\nFAIL: test_x (m.T.test_x)\n" + RULE + "\nAssertionError: 1 != 2\n\n"
            + RULE + "\nRan 2 tests in 0.004s\n\nFAILED (failures=1, errors=1)\n"
        )
        ok, summary, detail = gate.unittest_summary(output)
        self.assertFalse(ok)
        self.assertEqual(summary, "2 tests (0.0 s), failures=1, errors=1")
        self.assertIn("AssertionError: 1 != 2", detail)
        self.assertNotIn("Ran 2 tests", detail)

    def test_crash_without_summary(self):
        """A run that never prints a verdict fails."""
        ok, summary, _ = gate.unittest_summary("Traceback (most recent call last):\nImportError\n")
        self.assertFalse(ok)
        self.assertEqual(summary, "no test summary, did not finish")


class CoverageDetailTests(unittest.TestCase):
    """The coverage failure detail lists the least-covered files briefly."""

    def test_lowest_covered_is_short_and_sorted(self):
        """Only percent and path, lowest first, without missed-line columns."""
        report = (
            "Name    Stmts   Miss Branch BrPart  Cover   Missing\n"
            "src/a.py   10   1   2   1   90.0%   4, 7->9\n"
            "src/b.py   10   5   2   1   50.0%   1-5\n"
            "TOTAL      20   6   4   2   70.0%\n"
        )
        self.assertEqual(gate.lowest_covered(report), " 50.0%  src/b.py\n 90.0%  src/a.py")


if __name__ == "__main__":
    unittest.main()
