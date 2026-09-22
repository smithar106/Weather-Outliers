"""Suite 1 — run the backend test suite and report what it actually did.

The counts published on the dashboard come from pytest's own JUnit XML, parsed
here. Nothing is summarised by hand, which matters because the alternative —
reading pytest's terminal summary — is exactly the sort of number that rots
silently once a test is added.

The suite fails if any test fails, errors, or if pytest cannot be run at all. A
suite that cannot run pytest reports ``error`` rather than pretending zero
failures.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from evals.environment import BACKEND
from evals.harness import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    Case,
    Metric,
    Suite,
    Timer,
    ratio,
)

SUITE_ID = "unit_tests"
TITLE = "Backend test suite"
DESCRIPTION = (
    "Runs the backend pytest suite and parses pytest's own JUnit XML. Covers the "
    "statistical engine, the grounding guards, the pipeline, and the HTTP API."
)

#: Long enough for a cold run on a slow CI worker, short enough that a hung test
#: fails the report instead of hanging it.
TIMEOUT_SECONDS = 900


def _module_of(testcase: ET.Element) -> str:
    """The test file a case belongs to, from pytest's own ``file`` attribute."""
    path = testcase.get("file")
    if path:
        return path
    classname = testcase.get("classname", "")
    parts = classname.split(".")
    return "/".join(parts[:2]) + ".py" if len(parts) >= 2 else classname or "unknown"


def run() -> Suite:
    suite = Suite(id=SUITE_ID, title=TITLE, description=DESCRIPTION)

    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "junit.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--junitxml",
            str(report_path),
        ]
        with Timer() as timer:
            try:
                # Fixed argv, no shell. The interpreter is this process's own.
                completed = subprocess.run(
                    command,
                    cwd=BACKEND,
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                suite.status = STATUS_ERROR
                suite.error = f"pytest exceeded {TIMEOUT_SECONDS}s"
                suite.duration_ms = timer.elapsed_ms
                return suite
            except OSError as exc:
                suite.status = STATUS_ERROR
                suite.error = f"could not start pytest: {exc}"
                return suite
        suite.duration_ms = timer.elapsed_ms

        if not report_path.exists():
            suite.status = STATUS_ERROR
            suite.error = (
                f"pytest exited {completed.returncode} without writing a report; "
                f"last output: {completed.stdout.strip()[-400:] or completed.stderr.strip()[-400:]}"
            )
            return suite

        # The XML was written moments ago by the pytest run above, into a path this
        # function created. It is not untrusted input.
        tree = ET.parse(report_path)

    root = tree.getroot()
    # pytest wraps its testsuite in a <testsuites> element.
    element = root if root.tag == "testsuite" else root.find("testsuite")
    if element is None:
        suite.status = STATUS_ERROR
        suite.error = "JUnit report contained no testsuite element"
        return suite

    total = int(element.get("tests", 0))
    failures = int(element.get("failures", 0))
    errors = int(element.get("errors", 0))
    skipped = int(element.get("skipped", 0))
    pytest_seconds = float(element.get("time", 0.0))
    passed = total - failures - errors - skipped

    per_module: dict[str, dict[str, int]] = {}
    failing: list[str] = []
    for testcase in element.iter("testcase"):
        module = _module_of(testcase)
        bucket = per_module.setdefault(module, {"total": 0, "bad": 0, "skipped": 0})
        bucket["total"] += 1
        if testcase.find("failure") is not None or testcase.find("error") is not None:
            bucket["bad"] += 1
            failing.append(f"{testcase.get('classname')}::{testcase.get('name')}")
        elif testcase.find("skipped") is not None:
            bucket["skipped"] += 1

    for module in sorted(per_module):
        bucket = per_module[module]
        suite.cases.append(
            Case(
                id=module,
                title=module,
                passed=bucket["bad"] == 0,
                category="test_module",
                expected="all tests pass",
                observed=(
                    f"{bucket['total'] - bucket['bad'] - bucket['skipped']}/"
                    f"{bucket['total']} passed"
                    + (f", {bucket['skipped']} skipped" if bucket["skipped"] else "")
                ),
            )
        )

    suite.metrics = [
        Metric("Tests collected", total, "tests"),
        Metric("Tests passed", passed, "tests"),
        Metric("Failures", failures, "tests"),
        Metric("Errors", errors, "tests"),
        Metric("Skipped", skipped, "tests"),
        Metric("Pass rate", ratio(passed, total - skipped), "fraction"),
        Metric(
            "Test wall-clock",
            round(pytest_seconds, 2),
            "s",
            "as measured by pytest, excluding interpreter startup",
        ),
        Metric("Test modules", len(per_module), "files"),
        Metric(
            "Exit code",
            completed.returncode,
            None,
            "0 means pytest itself reported success",
        ),
    ]

    if failing:
        suite.status = STATUS_FAILED
        suite.notes.extend(f"failed: {name}" for name in failing[:20])
        if len(failing) > 20:
            suite.notes.append(f"...and {len(failing) - 20} more")
    elif completed.returncode != 0:
        # Collection errors and plugin failures can produce a clean XML but a
        # non-zero exit. Trusting the XML alone would hide them.
        suite.status = STATUS_FAILED
        suite.notes.append(
            f"pytest exited {completed.returncode} with no failing test recorded; "
            "likely a collection or plugin error"
        )
    else:
        suite.status = STATUS_PASSED

    return suite
