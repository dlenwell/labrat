#!/usr/bin/env python3
# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Run LabRat's validation gate with one line of output per part.

The full gate is four parts: the Python tests under coverage, the coverage
minimum, every Node check in ``tests/js``, and Alembic's model/migration
drift check. Each prints a single summary line; a failing part also prints
the output needed to fix it (trimmed unless ``--full-output``). The last
line is ``gate: PASS`` or ``gate: FAIL``, and the exit status matches, so
an agent can report the result without reading thousands of lines.

``--docs`` runs only the shared-code map test, the one test that reads the
Markdown docs, for changes that touch Markdown files only.

Usage::

    .venv/bin/python scripts/gate.py            # full gate
    .venv/bin/python scripts/gate.py --docs     # Markdown-only change
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_BIN = ROOT / ".venv" / "bin"
DETAIL_LINES = 60
"""Lines of failure output shown per part unless ``--full-output`` is given."""


def tool(name):
    """Return the virtualenv's copy of a tool, falling back to ``PATH``.

    Args:
        name: The tool's executable name, such as ``alembic``.

    Returns:
        str | None: Its path, or None when it is installed nowhere.
    """
    candidate = VENV_BIN / name
    return str(candidate) if candidate.exists() else shutil.which(name)


def run(command, env=None):
    """Run a command in the checkout and return the completed process.

    Args:
        command: The command and its arguments.
        env: The environment; this process's when None.

    Returns:
        subprocess.CompletedProcess: The finished process, with its output
            captured as text.
    """
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, env=env)


class Gate:
    """Collect one result per part and print them tersely."""

    def __init__(self, full_output):
        """Start with no results; ``full_output`` disables detail trimming.

        Args:
            full_output: Print complete failure output instead of its tail.
        """
        self.full_output = full_output
        self.failed = False

    def report(self, part, ok, summary, detail=""):
        """Print a part's summary line, and its detail when it failed.

        Args:
            part: The part's name, such as ``python``.
            ok: Whether it passed.
            summary: Its one-line result.
            detail: What to print when it failed.
        """
        print(f"gate: {part:<9} {'OK  ' if ok else 'FAIL'} {summary}", flush=True)
        if ok:
            return
        self.failed = True
        lines = detail.strip().splitlines()
        if not self.full_output and len(lines) > DETAIL_LINES:
            hidden = len(lines) - DETAIL_LINES
            lines = [f"... {hidden} earlier lines hidden (use --full-output) ..."] + lines[-DETAIL_LINES:]
        for line in lines:
            print(f"    {line}")

    def finish(self):
        """Print the overall verdict and return the exit status.

        Returns:
            int: 1 when any part failed, else 0.
        """
        print(f"gate: {'FAIL' if self.failed else 'PASS'}")
        return 1 if self.failed else 0


def unittest_summary(output):
    """Return (ok, summary, failure detail) from unittest's text output.

    Args:
        output: unittest's combined output.

    Returns:
        tuple[bool, str, str]: Whether every test passed, the count and time,
            and the failure messages.
    """
    ran = re.search(r"^Ran (\d+) tests? in ([\d.]+)s", output, re.MULTILINE)
    verdict = re.search(r"^(OK.*|FAILED.*)$", output, re.MULTILINE)
    ok = bool(verdict and verdict.group(1).startswith("OK"))
    count = f"{ran.group(1)} tests ({float(ran.group(2)):.1f} s)" if ran else "no test summary"
    if not verdict:
        extra = "did not finish"
    elif ok:
        extra = verdict.group(1).removeprefix("OK").strip().removeprefix("(").removesuffix(")")
    else:
        extra = verdict.group(1).removeprefix("FAILED").strip().removeprefix("(").removesuffix(")")
    summary = count + (f", {extra}" if extra else "")
    detail = ""
    if not ok:
        start = output.find("=" * 70)
        end = output.rfind("-" * 70)
        detail = output[start:end] if 0 <= start < end else output
    return ok, summary, detail


def python_tests(gate, pattern=None):
    """Run the unittest suite under coverage (or one test file, uncovered).

    Args:
        gate: The gate collecting results.
        pattern: Run only test files matching this name, without coverage.
    """
    python = tool("python") or sys.executable
    if pattern:
        command = [python, "-m", "unittest", "discover", "-s", "tests", "-p", pattern]
    else:
        command = [python, "-m", "coverage", "run", "-m", "unittest", "discover", "-s", "tests", "-q"]
    result = run(command)
    ok, summary, detail = unittest_summary(result.stderr + result.stdout)
    gate.report("python", ok and result.returncode == 0, summary, detail)


def coverage(gate):
    """Check combined coverage against ``fail_under`` in pyproject.toml.

    Args:
        gate: The gate collecting results.
    """
    minimum = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["coverage"]["report"]["fail_under"]
    result = run([tool("python") or sys.executable, "-m", "coverage", "report"])
    total = re.search(r"^TOTAL\s.*?([\d.]+)%\s*$", result.stdout, re.MULTILINE)
    if not total:
        gate.report("coverage", False, "no TOTAL line", result.stdout + result.stderr)
        return
    gate.report(
        "coverage",
        result.returncode == 0,
        f"{total.group(1)}% (minimum {minimum}%)",
        "Lowest-covered files:\n" + lowest_covered(result.stdout),
    )


def lowest_covered(report):
    """Return the ten least-covered rows of a coverage report.

    Args:
        report: ``coverage report``'s text.

    Returns:
        str: Its ten rows with the lowest coverage.
    """
    rows = []
    for line in report.splitlines():
        match = re.match(r"^(\S+\.py)\s.*?([\d.]+)%", line)
        if match:
            rows.append((float(match.group(2)), match.group(1)))
    return "\n".join(f"{percent:5.1f}%  {name}" for percent, name in sorted(rows)[:10])


def node_checks(gate):
    """Run every ``tests/js/*.mjs`` check with Node.

    Args:
        gate: The gate collecting results.
    """
    node = shutil.which("node")
    checks = sorted((ROOT / "tests" / "js").glob("*.mjs"))
    if node is None:
        gate.report("js", False, "node not found; this part did not run")
        return
    failures = []
    for check in checks:
        result = run([node, str(check)])
        if result.returncode != 0:
            failures.append(f"{check.name}:\n{(result.stdout + result.stderr).strip()}")
    passed = len(checks) - len(failures)
    gate.report("js", not failures, f"{passed}/{len(checks)} checks", "\n\n".join(failures))


def alembic_check(gate):
    """Fail on drift between the SQLAlchemy models and the migrations.

    Args:
        gate: The gate collecting results.
    """
    alembic = tool("alembic")
    if alembic is None:
        gate.report("alembic", False, "alembic not found; this part did not run")
        return
    # A throwaway database, migrated to head first, so the check needs no
    # local configuration and never touches the development database.
    with tempfile.TemporaryDirectory(prefix="labrat-gate-") as temp:
        config = Path(temp) / "local_config.toml"
        config.write_text(f'[database]\npath = "{Path(temp) / "check.sqlite3"}"\n')
        env = dict(os.environ, LABRAT_CONFIG=str(config))
        result = run([alembic, "-c", "alembic.ini", "upgrade", "head"], env=env)
        if result.returncode == 0:
            result = run([alembic, "-c", "alembic.ini", "check"], env=env)
    output = (result.stdout + result.stderr).strip()
    summary = "no model/migration drift" if result.returncode == 0 else "model/migration drift"
    gate.report("alembic", result.returncode == 0, summary, output)


def main(argv=None):
    """Parse arguments, run the requested parts, and return the exit status.

    Args:
        argv: Command-line arguments; ``sys.argv`` when None.

    Returns:
        int: The exit status: 0 when every part passed.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--docs", action="store_true", help="Markdown-only change: run just the shared-code map test")
    parser.add_argument("--full-output", action="store_true", help="show complete failure output")
    args = parser.parse_args(argv)
    gate = Gate(args.full_output)
    if args.docs:
        python_tests(gate, pattern="test_shared_code.py")
    else:
        python_tests(gate)
        coverage(gate)
        node_checks(gate)
        alembic_check(gate)
    return gate.finish()


if __name__ == "__main__":
    sys.exit(main())
