"""`verify(patch, repo, test_selector) -> Report` — the one call the agent loop makes.

Phase 1.4. Everything the agent executes goes through here: it copies the repository to a
scratch tree, applies the model's patch to the copy, runs the project's own tests, and returns
a structured result. The original is never written to, and nothing is promoted — 2.4 does that,
and only after this says pass.

**It refuses to run outside the container.** That is the phase 1 gate, and the refusal is a
raised `NotContained` rather than a flag on the result, because a caller that ignores a return
value is the failure mode. See `containment()` for what is actually checked and why one of the
two signals is worth more than the other.

Two things this module knows that a thinner wrapper would not:

* **A selector that matches nothing is not a pass.** pytest exits 5 with no failures when a
  node id is wrong, and "no failures" is what a naive wrapper reads. An agent writing its own
  selectors will hit this constantly, and a reward that counts it as success teaches the model
  to write selectors that match nothing. Status `no_tests`, loudly.
* **A patch that edits the tests is the cheapest hack there is.** `protect_tests` restores any
  touched test file from the original copy after applying, and the report says it happened. The
  signal is always there even when the restore is turned off, on the same reasoning as
  `FRAUD_FLAGS` in the executor: a primitive that can see a cheat should not hand back a number
  that hides it.

**The weaker of the two executor modes is the one this uses, and that is not fixable here.**
`executor.run(checks=...)` keeps the expected value out of the sandbox, which is strictly
stronger — see `results/02-redesign.md` §4. It cannot be used for a real repository, because a
project's pytest suite is arbitrary Python and there is no expected value to hold back. So
`verify()` runs the suite in-process in the scratch tree and trusts its report, and the
containment that matters is the container around the whole thing. Check mode is for training
rewards, where the task set is ours; do not read "PCA has check mode" as "the loop is covered
by it".
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

from pca.sandbox.executor import _clip, _drain, _kill_group, _limits, default_nproc

# Written by docker/Dockerfile at build time. See containment().
MARKER = "/etc/pca-sandbox"
NET_DIR = "/sys/class/net"

DEFAULT_WALL_S = 180
DEFAULT_CPU_S = 150
DEFAULT_MEM_MB = 2048
DEFAULT_FSIZE_MB = 64

# Not copied into the scratch tree. .git is excluded deliberately: `git apply` works perfectly
# well outside a repository, and copying history makes the per-attempt cost scale with the
# project's age rather than its size.
IGNORE = shutil.ignore_patterns(
    ".git", ".hg", ".svn",
    ".venv", "venv", "env",
    "__pycache__", "*.pyc", "*.pyo",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".nox",
    "node_modules", ".archify",
)

# What counts as a test file when deciding whether a patch touched the oracle.
TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|(^|/)[^/]*_test\.py$|(^|/)conftest\.py$")


class NotContained(RuntimeError):
    """Raised when verify() is called somewhere it refuses to run."""


@dataclass(frozen=True)
class Containment:
    ok: bool
    signals: dict[str, bool]
    detail: str


def containment() -> Containment:
    """Is this process somewhere verify() is willing to execute a model's patch?

    Two signals, and they are not equally good.

    `marker` is a file this project's own Dockerfile writes. It says "this is the image we
    built", which is the only way to know the run carries the flags we chose -- `--network
    none`, `--read-only`, `--cap-drop ALL`, a non-root uid. A generic "am I in a container"
    check answers a question nobody asked: being in *some* container tells you nothing about
    whether the network is reachable.

    `network_down` is the better of the two, because it tests the claim instead of trusting a
    file we wrote. With `--network none` the only interface is loopback. If someone runs this
    image with networking on, the marker is still there and this signal is what catches it.

    **This guards against accident, not against an adversary.** Anything that can already
    execute code in *this* process can create the marker file. That is not the threat: the
    threat is a developer running verify() on a laptop against a model's patch, which is
    exactly the mistake a raised exception prevents. The adversarial boundary is one level
    down, in executor.py, and one level up, in the container's own flags.
    """
    signals: dict[str, bool] = {}
    reasons: list[str] = []

    signals["marker"] = os.path.exists(MARKER)
    if not signals["marker"]:
        reasons.append(f"{MARKER} is absent, so this is not an image built from docker/Dockerfile")

    try:
        interfaces = sorted(os.listdir(NET_DIR))
    except OSError:
        interfaces = []
    signals["network_down"] = bool(interfaces) and set(interfaces) <= {"lo"}
    if not signals["network_down"]:
        if not interfaces:
            reasons.append(f"{NET_DIR} does not exist, so this is not Linux")
        else:
            reasons.append(f"{NET_DIR} lists {', '.join(interfaces)} — the network is reachable")

    ok = all(signals.values())
    return Containment(ok=ok, signals=signals, detail="; ".join(reasons) or "contained")


def require_containment() -> None:
    state = containment()
    if state.ok:
        return
    raise NotContained(
        "verify() will not run here: " + state.detail + ".\n"
        "It applies a model's patch and executes a repository's test suite, and the only "
        "thing standing between that and your machine is the container.\n"
        "Run it through the harness instead:\n"
        "    scripts/run-in-container.sh python -m pytest src/pca/sandbox/tests/test_verify.py\n"
        "or, for your own call:\n"
        "    scripts/run-in-container.sh python -c '...'\n"
        f"signals: {state.signals}"
    )


@dataclass
class Failure:
    test: str
    kind: str  # failure | error
    message: str
    text: str


@dataclass
class Report:
    """What the loop gets back. Deliberately wide: the repair step is built from `failures`."""

    status: str  # pass | fail | error | no_tests | patch_rejected | timeout | crash
    applied: bool
    passed: int = 0
    failed: int = 0
    errored: int = 0
    skipped: int = 0
    total: int = 0
    fraction: float = 0.0
    failures: list[Failure] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    touched_tests: list[str] = field(default_factory=list)
    tests_restored: bool = False
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    exit_code: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------------------ the patch


def _touched_paths(patch: str) -> list[str]:
    """Paths a unified diff claims to change, read from its +++ lines.

    Advisory, and it says so: this is what the patch *says*, parsed before `git apply` has had
    an opinion. It is used to decide whether the oracle was edited, so being generous is the
    right bias -- a path listed here that git then rejects costs nothing.
    """
    paths = []
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        target = line[4:].strip().split("\t")[0]
        if target == "/dev/null":
            continue
        if target.startswith(("a/", "b/")):
            target = target[2:]
        paths.append(target)
    return sorted(set(paths))


def _apply(patch: str, tree: str) -> tuple[bool, str]:
    """Apply with `git apply`, which refuses to write outside the tree by default.

    `--unsafe-paths` is deliberately NOT passed. Without it git rejects a patch whose paths
    climb out of the directory, which is the one thing a patch applier must not do and the
    reason this is not a hand-rolled diff parser.
    """
    base = ["git", "apply", "--whitespace=nowarn", "-p1"]
    for args in ([*base, "--check", "-"], [*base, "-"]):
        proc = subprocess.run(
            args, cwd=tree, input=patch, text=True, capture_output=True, timeout=60, check=False
        )
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or f"git apply exited {proc.returncode}").strip()
    return True, ""


# ------------------------------------------------------------------------------- the tests


def _env(home: str) -> dict[str, str]:
    """An allowlist, for the same reason executor._scrubbed_env is one.

    Wider than the executor's, because pytest on a real project legitimately needs a usable
    PATH and a writable HOME. Still an allowlist: HF_TOKEN, WANDB_API_KEY and AWS credentials
    are not things a model's patch should be able to read out of the environment and print.
    """
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "TMPDIR": home,
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONHASHSEED": "0",
    }


def _parse_junit(path: str) -> tuple[dict[str, int], list[Failure]]:
    """Counts come from the XML, never from the exit code.

    Same rule as the executor, for the same reason: pytest's exit status answers "did anything
    go wrong", and the loop needs "how many of which kind". A missing or unparseable file is a
    crash, not a zero -- scoring an absent result as 0/0 would make a harness failure look like
    an honest no-op.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))

    counts = {"total": 0, "failed": 0, "errored": 0, "skipped": 0}
    failures: list[Failure] = []
    for suite in suites:
        for case in suite.iter("testcase"):
            counts["total"] += 1
            name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
            for node, kind in (("failure", "failure"), ("error", "error")):
                found = case.find(node)
                if found is None:
                    continue
                counts["failed" if kind == "failure" else "errored"] += 1
                failures.append(
                    Failure(
                        test=name,
                        kind=kind,
                        message=_clip(found.get("message", ""), 500),
                        text=_clip(found.text or "", 2000),
                    )
                )
                break
            else:
                if case.find("skipped") is not None:
                    counts["skipped"] += 1
    counts["passed"] = counts["total"] - counts["failed"] - counts["errored"] - counts["skipped"]
    return counts, failures


def _run_pytest(tree: str, home: str, selector: list[str], xml: str, wall_s: int, cpu_s: int, mem_mb: int, fsize_mb: int) -> tuple[str, str, int | None, bool]:
    argv = [
        sys.executable, "-m", "pytest",
        "-p", "no:cacheprovider",
        "--junitxml", xml,
        "-o", "addopts=",  # the project's own addopts may carry -q, -x or coverage flags
        *selector,
    ]
    proc = subprocess.Popen(
        argv,
        cwd=tree,
        preexec_fn=_limits(mem_mb, cpu_s, default_nproc(), fsize_mb),
        env=_env(home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    try:
        out, err = proc.communicate(timeout=wall_s)
        return out, err, proc.returncode, False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err, _abandoned = _drain(proc)
        return out, err, proc.returncode, True
    finally:
        if proc.poll() is None:
            _kill_group(proc)


# ----------------------------------------------------------------------------------- verify


def verify(
    patch: str | None,
    repo: str,
    test_selector: str | list[str] | None = None,
    *,
    protect_tests: bool = True,
    wall_s: int = DEFAULT_WALL_S,
    cpu_s: int = DEFAULT_CPU_S,
    mem_mb: int = DEFAULT_MEM_MB,
    fsize_mb: int = DEFAULT_FSIZE_MB,
) -> Report:
    """Apply `patch` to a throwaway copy of `repo` and run `test_selector` against it.

    `patch` is a unified diff, or None to run the tests unchanged -- which is how the loop
    establishes that a test fails before it tries to fix it.

    `test_selector` is whatever pytest would take: node ids, paths, or nothing for the whole
    suite. Nothing is the expensive choice and usually the wrong one; the loop should name the
    test it is trying to fix.

    Raises `NotContained` outside the container. Never writes to `repo`.
    """
    require_containment()
    started = time.monotonic()

    repo = os.path.abspath(repo)
    if not os.path.isdir(repo):
        return Report(status="error", applied=False, detail=f"no such repository: {repo}")

    if isinstance(test_selector, str):
        test_selector = [test_selector]
    selector = list(test_selector or [])

    scratch = tempfile.mkdtemp(prefix="pca-verify-")
    tree = os.path.join(scratch, "repo")
    home = os.path.join(scratch, "home")
    xml = os.path.join(scratch, "report.xml")
    try:
        os.mkdir(home)
        # symlinks=True copies links as links. The default follows them, which would pull a
        # link's target into the tree -- turning a one-line symlink into an unbounded copy, and
        # a link pointing outside the repo into a file the patch can then edit "inside" it.
        shutil.copytree(repo, tree, symlinks=True, ignore=IGNORE)

        touched = _touched_paths(patch) if patch else []
        touched_tests = [p for p in touched if TEST_PATH.search(p)]
        report = Report(status="error", applied=False, touched=touched, touched_tests=touched_tests)

        if patch:
            applied, why = _apply(patch, tree)
            report.applied = applied
            if not applied:
                report.status = "patch_rejected"
                report.detail = _clip(why, 2000)
                report.duration_ms = int((time.monotonic() - started) * 1000)
                return report

            # The oracle is not the model's to edit. Restoring after the apply rather than
            # rejecting the whole patch is deliberate: a patch that fixes code AND deletes the
            # failing test should have its fix measured honestly, against the real test.
            if protect_tests and touched_tests:
                for rel in touched_tests:
                    source = os.path.join(repo, rel)
                    target = os.path.join(tree, rel)
                    if not os.path.exists(source):
                        # The patch created a test file that was not there. Not a restore --
                        # a removal, because the model wrote its own oracle.
                        if os.path.exists(target):
                            os.unlink(target)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(source, target)
                report.tests_restored = True
        else:
            report.applied = True

        out, err, rc, timed_out = _run_pytest(tree, home, selector, xml, wall_s, cpu_s, mem_mb, fsize_mb)
        report.stdout = _clip(out)
        report.stderr = _clip(err)
        report.exit_code = rc
        report.duration_ms = int((time.monotonic() - started) * 1000)

        if timed_out:
            report.status = "timeout"
            report.detail = f"the suite did not finish within {wall_s}s"
            return report

        try:
            counts, failures = _parse_junit(xml)
        except (OSError, ET.ParseError) as exc:
            report.status = "crash"
            report.detail = (
                f"no usable junit report ({type(exc).__name__}); pytest exited {rc}. "
                "Counts are read from the XML and never from the exit code, so a missing file "
                "is a harness failure, not a score."
            )
            return report

        report.passed = counts["passed"]
        report.failed = counts["failed"]
        report.errored = counts["errored"]
        report.skipped = counts["skipped"]
        report.total = counts["total"]
        report.failures = failures

        countable = report.total - report.skipped
        report.fraction = report.passed / countable if countable else 0.0

        if report.total == 0:
            # pytest exits 5 here and reports no failures. A wrapper that asks "did anything
            # fail" gets "no" and calls it a pass, which teaches an agent writing its own
            # selectors to write ones that match nothing.
            report.status = "no_tests"
            report.detail = (
                f"the selector matched no tests: {selector or '(whole suite)'}. "
                "This is not a pass."
            )
        elif report.errored:
            report.status = "error"
            report.detail = f"{report.errored} test(s) errored before running"
        elif report.failed:
            report.status = "fail"
        else:
            report.status = "pass"
        return report
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
