"""Runs the corpus against an executor, under repetition, and reports what held.

Deliberately executor-agnostic. `run_corpus` takes a callable, not an import, so the same corpus
can be pointed at someone else's sandbox in phase 4 without being rewritten. The adapter for this
project's own executor is `pca_executor`; it is the only part of this file that knows PCA exists.

**Repetition is the point, not a quality setting.** Containment that holds once and not on the
four-thousandth call is not containment, and an agent repair loop is the second kind. A case is
reported as held only when it held on every repetition; the accept count is reported alongside,
because "blocked 9 times out of 10" is a different and worse finding than "never blocked".
"""

from __future__ import annotations

import resource
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field

from pca.audit.corpus import CASES, TASK_CHECKS, TASK_TESTS, Case

# ru_maxrss is KiB on Linux and bytes on Darwin. Normalising here rather than at the call site
# keeps the unit out of the report format, where a wrong one is invisible.
_RSS_DIVISOR = 1024 if sys.platform == "darwin" else 1


@dataclass(frozen=True)
class Outcome:
    """What the scorer needs from any executor. A subset of PCA's ExecResult on purpose."""

    status: str
    passed: int
    total: int
    fraction: float
    flags: tuple[str, ...] = ()
    wall_ms: int = 0


Executor = Callable[[str, list], Outcome]

SNIPPET = "snippet"
CHECK = "check"


def pca_executor(mode: str = SNIPPET, **kwargs) -> Executor:
    """Adapter over this project's own sandbox. Keyword arguments pass through to `run`.

    `mode` picks which of the executor's two test forms the corpus is scored against.
    Running the corpus in both is the point: snippet mode is what phase 2's `verify()`
    has to use against a real repository's own suite, and it is the weaker of the two.
    """
    from pca.sandbox.executor import run

    def execute(code: str, tests: list) -> Outcome:
        result = run(code, checks=tests, **kwargs) if mode == CHECK else run(code, tests, **kwargs)
        return Outcome(
            status=result.status,
            passed=result.passed,
            total=result.total,
            fraction=result.fraction,
            flags=tuple(result.flags),
            wall_ms=result.wall_ms,
        )

    return execute


@dataclass
class CaseReport:
    """One case, every repetition of it, and the verdict."""

    id: str
    name: str
    kind: str
    expect: str
    reps: int
    fractions: list[float] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    wall_ms: list[int] = field(default_factory=list)
    parent_peak_rss_kb: int = 0

    @property
    def target(self) -> float:
        return 0.0 if self.expect == "zero" else 1.0

    @property
    def held_count(self) -> int:
        return sum(1 for f in self.fractions if f == self.target)

    @property
    def held(self) -> bool:
        """Held on every repetition. Anything less is a hole with a duty cycle."""
        return bool(self.fractions) and self.held_count == len(self.fractions)

    @property
    def worst(self) -> float:
        """The fraction furthest from what was expected -- the number to quote."""
        if not self.fractions:
            return float("nan")
        return max(self.fractions) if self.expect == "zero" else min(self.fractions)

    @property
    def median_wall_ms(self) -> int:
        return int(statistics.median(self.wall_ms)) if self.wall_ms else 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(
            held=self.held,
            held_count=self.held_count,
            worst=self.worst,
            median_wall_ms=self.median_wall_ms,
        )
        return data


def run_case(
    execute: Executor, case: Case, reps: int, tests: Sequence = (), mode: str = SNIPPET
) -> CaseReport:
    tests = list(tests) or list(TASK_CHECKS if mode == CHECK else TASK_TESTS)
    report = CaseReport(id=case.id, name=case.name, kind=case.kind, expect=case.expect, reps=reps)
    seen: set[str] = set()
    for _ in range(reps):
        started = time.monotonic()
        outcome = execute(case.source, tests)
        elapsed = int((time.monotonic() - started) * 1000)
        report.fractions.append(outcome.fraction)
        report.statuses.append(outcome.status)
        report.wall_ms.append(outcome.wall_ms or elapsed)
        seen.update(outcome.flags)
    report.flags = sorted(seen)
    # Monotonic high-water mark for this process, sampled after the case ran. Only an increase
    # is readable, and only the first case to reach a given level gets credit for it -- which is
    # all that is needed to see a 16 MiB flood land in the grader rather than in the sandbox.
    report.parent_peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // _RSS_DIVISOR
    return report


def run_corpus(
    execute: Executor,
    *,
    reps: int = 10,
    cases: Sequence[Case] = (),
    tests: Sequence = (),
    mode: str = SNIPPET,
) -> list[CaseReport]:
    return [run_case(execute, case, reps, tests, mode) for case in (list(cases) or CASES)]


def controls_are_sound(reports: Sequence[CaseReport]) -> tuple[bool, str]:
    """Did the controls behave? Read this before reading any exploit result.

    An all-zero corpus is the expected shape of total success and the expected shape of a
    harness that cannot run anything. The controls are what tell those two apart, so their
    verdict is returned separately rather than mixed into the exploit count.
    """
    controls = [r for r in reports if r.kind == "control"]
    if not controls:
        return False, "no controls were run; exploit results are unreadable"
    broken = [r for r in controls if not r.held]
    if broken:
        detail = "; ".join(f"{r.id} {r.name}: worst {r.worst:.3f}, wanted {r.target:.1f}" for r in broken)
        return False, f"controls did not behave ({detail}); exploit results are unreadable"
    return True, f"{len(controls)} controls behaved"


def accepted(reports: Sequence[CaseReport]) -> list[CaseReport]:
    """Exploits that scored above zero at least once."""
    return [r for r in reports if r.kind == "exploit" and not r.held]


def format_table(reports: Sequence[CaseReport]) -> str:
    head = f"{'id':<5} {'name':<16} {'kind':<8} {'want':<5} {'held':>7}  {'worst':>6}  {'ms':>5}  {'rss_kb':>8}  statuses / flags"
    lines = [head, "-" * len(head)]
    for r in reports:
        verdict = f"{r.held_count}/{len(r.fractions)}"
        statuses = ",".join(sorted(set(r.statuses)))
        flags = (" [" + ",".join(r.flags) + "]") if r.flags else ""
        mark = " " if r.held else "<"
        lines.append(
            f"{r.id:<5} {r.name:<16} {r.kind:<8} {r.expect:<5} {verdict:>7}{mark} {r.worst:>6.3f}  "
            f"{r.median_wall_ms:>5}  {r.parent_peak_rss_kb:>8}  {statuses}{flags}"
        )
    return "\n".join(lines)
