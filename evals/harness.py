"""Report structures shared by every suite.

The shapes here are the contract between the harness and the evaluation
dashboard, so they are deliberately flat and JSON-native: a report is a list of
suites, a suite is a list of metrics plus a list of cases, and a metric is a
label, a value, and a unit. The frontend renders whatever it is given without
knowing which suite produced it, which means adding a suite requires no frontend
change.

One field is load-bearing for honesty: ``Metric.value`` may be ``None``. A suite
that could not measure something reports ``None`` and explains itself in
``detail``, and the dashboard renders that as "not measured". Nothing in this
harness substitutes a plausible number for a missing one.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


@dataclass(slots=True)
class Metric:
    """One measured quantity. ``value=None`` means "not measured", never "zero"."""

    label: str
    value: float | int | str | bool | None
    unit: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Case:
    """One labelled case and what the system actually did with it."""

    id: str
    title: str
    passed: bool
    category: str | None = None
    expected: str | None = None
    observed: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Suite:
    id: str
    title: str
    description: str
    status: str = STATUS_PASSED
    duration_ms: int = 0
    metrics: list[Metric] = field(default_factory=list)
    cases: list[Case] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def cases_passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "metrics": [m.to_dict() for m in self.metrics],
            "cases": [c.to_dict() for c in self.cases],
            "cases_total": len(self.cases),
            "cases_passed": self.cases_passed,
            "notes": list(self.notes),
            "error": self.error,
        }


@dataclass(slots=True)
class Report:
    generated_at: str
    git_commit: str | None
    git_dirty: bool
    methodology_version: str
    environment: dict[str, Any]
    suites: list[Suite] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(s.status == STATUS_ERROR for s in self.suites):
            return STATUS_ERROR
        if any(s.status == STATUS_FAILED for s in self.suites):
            return STATUS_FAILED
        return STATUS_PASSED

    def to_dict(self) -> dict:
        cases_total = sum(len(s.cases) for s in self.suites)
        cases_passed = sum(s.cases_passed for s in self.suites)
        return {
            "generated_at": self.generated_at,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "methodology_version": self.methodology_version,
            "status": self.status,
            "environment": self.environment,
            "totals": {
                "suites_total": len(self.suites),
                "suites_passed": sum(1 for s in self.suites if s.status == STATUS_PASSED),
                "cases_total": cases_total,
                "cases_passed": cases_passed,
                "duration_ms": sum(s.duration_ms for s in self.suites),
            },
            "suites": [s.to_dict() for s in self.suites],
            "limitations": list(self.limitations),
        }


class Timer:
    """Context manager yielding elapsed milliseconds."""

    def __init__(self) -> None:
        self.started = 0.0
        self.elapsed_ms = 0

    def __enter__(self) -> Timer:
        self.started = time.perf_counter()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.elapsed_ms = int((time.perf_counter() - self.started) * 1000)


def git_state(repo_root) -> tuple[str | None, bool]:
    """``(short commit, has uncommitted changes)``, or ``(None, False)`` outside git.

    Recorded so a report can always be traced back to the code that produced it,
    and so a report generated from a dirty tree is visibly marked as such.
    """
    def run(args: list[str]) -> str | None:
        try:
            # Fixed argv, no shell, no interpolation of anything external.
            out = subprocess.run(
                args,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    commit = run(["git", "rev-parse", "--short", "HEAD"])
    if commit is None:
        return None, False
    status = run(["git", "status", "--porcelain"])
    return commit, bool(status)


def describe_environment(*, weather_provider: str, llm_mode: str) -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "weather_provider": weather_provider,
        "llm_mode": llm_mode,
        "database": "sqlite (in-memory scratch)",
    }


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ratio(numerator: int, denominator: int) -> float | None:
    """A rate, or ``None`` when there is nothing to divide."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)
