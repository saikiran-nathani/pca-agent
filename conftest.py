"""Repo-root pytest configuration — the Linux-only guard, and its own anchor check.

Exists for one reason: `[tool.pytest.ini_options] testpaths = ["src"]` means a
bare `pytest` now collects every suite under src/ — src/pca/sandbox/tests (the
adversarial suite) and src/train/tests (monitor.py's degradation contract).
src/trainwatch/tests was the third and left with trainwatch in Phase B step 6.
One case in the sandbox suite must not run on macOS, and skipping it here
rather than in the test file keeps that file runnable standalone
(`python src/pca/sandbox/tests/test_adversarial.py`), which it is designed to be —
it imports no pytest and predates it being installed.

That design constraint is why the guard matches on a **test name** and not on a
marker. A file that cannot import pytest cannot carry `@pytest.mark.skipif`, so
name-matching is the correct mechanism here, not a shortcut. What was missing is
that a name-keyed guard has an expiry date it does not announce: rename or
parametrize the test and the guard goes on reporting nothing, which is
indistinguishable from the guard working. `HARDENING-PATTERNS.md` §1 — absence
of evidence must never render as success — and §2's rule that an anchor matching
nothing "reports CAUGHT for whatever else is failing". So the guard now verifies
its own anchor, and fails collection loudly when it has expired.

> **Added 2026-09-18 — this hardening was written for Platform, where it can
> never fire, and was missing from here, where it is the only thing standing
> between a rename and a fork bomb.** `_anchor_name`, `_guarded_suite_is_collected`
> and the `UsageError` were added to Platform's copy of this file on 2026-09-18
> and not to ULTRON's. Platform has no `src/sandbox/` — its own docstring says
> so and labels its guard inert — so the protection landed in the repository
> that cannot use it and was absent from the one that can. **This** repository
> holds `src/pca/sandbox/tests/test_adversarial.py::test_fork_bomb_is_contained`,
> so this is the live copy of the guard and the un-hardened one was this one.
>
> Nothing was broken on the day it was found: the test is un-parametrised, the
> bare name matched, and the macOS skip fired. That is the point. The failure
> mode is a rename or a `@pytest.mark.parametrize`, after which the guard
> silently matches nothing, `test_fork_bomb_is_contained` runs on the Mac,
> sizes `RLIMIT_NPROC` from a `/proc` that does not exist, and permits roughly
> 4,100 forked processes on the machine running it. A guard that has expired
> looks exactly like a guard with nothing to do.
>
> Left as a dated note rather than quietly rewritten, per this repo's
> convention.
"""

from __future__ import annotations

import sys

import pytest

# test_fork_bomb_is_contained sizes RLIMIT_NPROC from current_user_threads(),
# which reads /proc. On macOS /proc is absent, the helper falls back to 4096,
# and the test would permit ~4,100 forked processes -- see BUILDING-ULTRON.md
# Part 3.5, "Do not run the fork-bomb test on the Mac".
LINUX_ONLY_TESTS = frozenset({"test_fork_bomb_is_contained"})

# The guard's precondition. It can only demand that a name resolve while the
# suite holding that name is actually being collected; requiring a match
# unconditionally would turn a `pytest src/train` run red over a directory it
# was never asked to look at.
_GUARDED_SUITE = "src/pca/sandbox"


def _guarded_suite_is_collected(items: list[pytest.Item]) -> bool:
    return any(_GUARDED_SUITE in item.nodeid.replace("\\", "/") for item in items)


def _anchor_name(item: pytest.Item) -> str:
    """The test's function name, with any parametrisation suffix removed.

    `item.name` for a parametrised case is `test_x[4096]`, which would not equal
    the bare name — so matching on `item.name` alone means parametrising the
    fork-bomb test disarms this guard as silently as renaming it would.
    """
    return getattr(item, "originalname", None) or item.name


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    matched = {_anchor_name(item) for item in items} & LINUX_ONLY_TESTS

    # Anchor check, deliberately before the platform test and not inside it. On
    # Linux the guard is *meant* to do nothing, so an early `return` there would
    # mean a rename landing on Linux CI disarms the macOS protection without
    # anything noticing until someone runs the suite on a Mac.
    if _guarded_suite_is_collected(items) and not matched:
        missing = ", ".join(sorted(LINUX_ONLY_TESTS))
        raise pytest.UsageError(
            f"{_GUARDED_SUITE} is being collected, but none of its tests are named "
            f"{{{missing}}}. This guard keeps that test off non-Linux hosts, where it "
            "sizes RLIMIT_NPROC from a /proc that does not exist and would permit "
            "~4,100 forked processes. A rename or a parametrisation silently disarms "
            "it, so collection fails here instead. Fix LINUX_ONLY_TESTS in "
            "conftest.py to match the new name."
        )

    if sys.platform == "linux":
        return

    skip = pytest.mark.skip(reason="Linux only: sizes RLIMIT_NPROC from /proc, which macOS lacks")
    for item in items:
        if _anchor_name(item) in LINUX_ONLY_TESTS:
            item.add_marker(skip)
