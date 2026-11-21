"""Level 1 sandbox — subprocess + resource.setrlimit.

The trust boundary of the whole project. In the GRPO phase this exact code becomes the
reward function, so every weakness here turns into a thing the policy is optimised to find.

Level 1 protects you from accidents. Only Level 2 (Docker, --network none) protects you
from a model that is actively searching. Residual Level 1 gaps are marked RESIDUAL below.

Design rules, from the field guide:
  * Two timeouts, always both. RLIMIT_CPU catches busy loops; wall-clock catches sleeps
    and blocking I/O. Neither alone is sufficient.
  * os.setsid() + os.killpg() — kill the process *group*. subprocess timeout kills the
    child and cheerfully leaves its grandchildren running.
  * Scrub the environment to an explicit allowlist. Inherited env leaks HF_TOKEN.
  * Tests never live in cwd. The model will open them.
  * Return fraction-of-tests-passed, not a boolean. Binary reward is sparse and GRPO
    barely moves on it.
  * Capture the traceback. The self-repair phase is built entirely from it.
  * Never trust exit code 0. sys.exit(0) defeats it.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import resource
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field

_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_runner.py")

# Wall clock must sit above the CPU limit so that RLIMIT_CPU fires first when the
# program is genuinely burning CPU -- that path gives us a diagnosable signal instead
# of an opaque "timeout".
DEFAULT_CPU_S = 10
DEFAULT_WALL_S = 15
DEFAULT_MEM_MB = 512
DEFAULT_FSIZE_MB = 16

# RLIMIT_NPROC is per-UID and, on Linux, counts THREADS -- not processes, and not just
# this process group. Measured on this box: 175 processes but 2069 threads, because the
# desktop session and the IDE each hold hundreds. So the field guide's example value of
# 64 makes *every* fork fail instantly with BlockingIOError. That is the worst possible
# outcome, because it looks like containment: the fork-bomb test goes green while really
# nothing could ever have spawned, and legitimate subprocess use is broken too.
#
# Size the cap as "current thread usage + headroom" so the ceiling is real but only a
# bomb can reach it.
#
# RESIDUAL: because the limit is per-UID it is coupled to whatever else you happen to be
# running. A bomb also eats into the desktop's own budget while it climbs. The clean fix
# is Level 2's `--pids-limit`, which is per-container. Use Level 2 for unattended runs.
NPROC_HEADROOM = 512
_nproc_default: int | None = None


@dataclass
class ExecResult:
    """The result schema. Everything downstream reads this, so it is deliberately wide."""

    status: str  # pass | fail | timeout | error | crash
    passed: int = 0
    total: int = 0
    fraction: float = 0.0  # passed/total -> DENSE REWARD
    stdout: str = ""
    stderr: str = ""
    traceback: str = ""  # the self-repair phase depends on this
    exit_code: int | None = None
    wall_ms: int = 0
    cpu_ms: int = 0
    flags: list[str] = field(default_factory=list)  # 'read_test_file', 'net_attempt', ...
    fraud: str = ""  # non-empty means the score was zeroed, and names why

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict:
        return asdict(self)


def current_user_threads() -> int:
    """Threads owned by this UID -- the unit RLIMIT_NPROC actually counts."""
    uid = os.getuid()
    total = 0
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                if os.stat(f"/proc/{pid}").st_uid != uid:
                    continue
                with open(f"/proc/{pid}/status") as fh:
                    for line in fh:
                        if line.startswith("Threads:"):
                            total += int(line.split()[1])
                            break
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return 4096
    return total or 4096


def default_nproc() -> int:
    """Cached, because run() is called millions of times and this walks /proc.

    Bulk callers on a busy desktop should pass `nproc` explicitly rather than rely on a
    value measured once at first use.
    """
    global _nproc_default
    if _nproc_default is None:
        _nproc_default = current_user_threads() + NPROC_HEADROOM
    return _nproc_default


def _limits(mem_mb: int, cpu_s: int, nproc: int, fsize_mb: int):
    """Return the preexec_fn applied between fork and exec.

    preexec_fn is NOT thread-safe -- parallelise with multiprocessing, never threads.
    """

    def apply() -> None:
        os.setsid()  # NEW SESSION + PROCESS GROUP  <-- critical for killpg

        # Address space. Note RLIMIT_AS caps *virtual* address space and some
        # allocators over-reserve, so this is a blunt instrument; cgroups (Level 2)
        # are stricter. It is still what turns a 40 GB allocation into a MemoryError.
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb << 20, mem_mb << 20))
        except (ValueError, OSError):
            # macOS/Darwin does not support RLIMIT_AS -- it raises "current limit
            # exceeds maximum limit" and every run() fails in preexec_fn. RLIMIT_DATA
            # is the nearest equivalent there. On Linux this branch never fires.
            # NOTE: RLIMIT_DATA does NOT actually cap allocation on Darwin, so memory
            # containment is Linux-only. Run untrusted bulk execution on the TUF.
            #
            # SIM105 is suppressed below rather than satisfied: this is the ONE site
            # in this file that runs between fork and exec, where the docstring above
            # says preexec_fn is not thread-safe and the rule is to do as little as
            # possible. Entering a context manager here buys nothing behavioural and
            # adds machinery to a post-fork child. The three other SIM105 sites in
            # this module are in the parent and were converted to
            # contextlib.suppress.
            #
            # (This comment used to open with the four letters of the directive
            # itself, which ruff read as a blanket suppression for the whole line and
            # then flagged as unused. Worth knowing: prose about a directive is a
            # directive.)
            try:  # noqa: SIM105
                resource.setrlimit(resource.RLIMIT_DATA, (mem_mb << 20, mem_mb << 20))
            except (ValueError, OSError):
                pass

        # CPU seconds. Soft limit raises SIGXCPU, which the program is allowed to
        # catch and ignore; the higher hard limit guarantees a following SIGKILL.
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 2))

        # Anti fork-bomb.
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))

        # Max bytes any single write may extend a file to. Stops a solution filling
        # the disk with a log file; exceeding it raises SIGXFSZ.
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_mb << 20, fsize_mb << 20))

        # Core dumps from a killed memory bomb are pure noise on a 4 GB budget.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def _scrubbed_env(workdir: str) -> dict[str, str]:
    """Explicit allowlist. Never pass os.environ through.

    An inherited environment is an information leak straight into generated code:
    HF_TOKEN, WANDB_API_KEY, AWS creds. It is also how proxy variables let a
    "network isolated" Level 1 sandbox reach the internet anyway.
    """
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": workdir,
        "TMPDIR": workdir,
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        # Deliberately absent: HF_*, WANDB_*, *_PROXY, PYTHONPATH, SSH_AUTH_SOCK.
    }


# Flags that mean the result is fraudulent rather than merely suspicious, and what the
# score becomes when one is present.
#
# Phase 1.2 measured E03 `always_equal` scoring 1.0 on 25 runs out of 25 with
# `eq_override` raised on every one of them. The detection worked; nothing consulted it.
# _runner.py had said for months that "the reward function must treat eq_override as a
# hard zero" -- a rule living in a comment, waiting on a reward function twelve weeks
# away, while the executor returned 1.0 for a result it had already called fake.
#
# DECISIONS.md X1 settled it here rather than in the caller, on the grounds that
# returning 1.0 for a result you have flagged as fraudulent is itself a policy, and a
# worse one than the primitive declining to. The flag stays -- the evidence is worth
# keeping. What changes is that the number stops contradicting it.
FRAUD_FLAGS = ("eq_override",)


def run(
    code: str,
    tests: str | list[str] | None = None,
    *,
    checks: list[tuple[str, object]] | None = None,
    wall_s: int = DEFAULT_WALL_S,
    cpu_s: int = DEFAULT_CPU_S,
    mem_mb: int = DEFAULT_MEM_MB,
    fsize_mb: int = DEFAULT_FSIZE_MB,
    nproc: int | None = None,
    python: str | None = None,
) -> ExecResult:
    """Execute `code` against `tests` and/or `checks` under Level 1 isolation.

    Two test forms, and the difference between them is a security property rather than
    a convenience. DECISIONS.md X2 settled on carrying both.

    `tests` is a list of independent source snippets, each counted separately so the
    result carries passed/total -- the dense reward. A bare string is treated as one
    test. Each snippet is exec'd with the solution module's namespace in scope, so it
    can call the solution's functions directly. **The child evaluating a snippet is
    told what passing looks like**, because the assertion is the snippet. It therefore
    decides its own pass/fail, and a solution willing to forge one message and exit can
    take that one test. It cannot reach any other test or the report. This form exists
    because a real repository's pytest suite is arbitrary Python and phase 2's
    `verify()` has to run it.

    `checks` is a list of `(call_source, expected)`. **The expected value never enters
    the sandbox.** The child evaluates the call, serialises what the solution returned,
    and the comparison happens here. `os._exit(0)` then produces a missing value rather
    than a pass; forging one means computing the right answer, which is the task. Values
    must be JSON-serialisable, and comparison is on canonical JSON text, so an object
    with a custom `__eq__` has nothing to override. Use this form for training rewards,
    where the task set is ours and the shape is free.

    Both may be given; they are scored into one fraction, snippets first.
    """
    if isinstance(tests, str):
        tests = [tests]
    specs: list[dict] = [{"k": "snippet", "src": t} for t in (tests or [])]
    specs += [{"k": "check", "src": c, "expected": e} for c, e in (checks or [])]
    if not specs:
        raise ValueError("run() needs at least one of tests= or checks=")
    total = len(specs)
    python = python or sys.executable
    if nproc is None:
        nproc = default_nproc()

    # Fail fast on syntax: ~0.1 ms of ast.parse rejects a meaningful fraction of
    # generations instead of paying ~50 ms to spawn a process that cannot run.
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return ExecResult(
            status="error",
            total=total,
            traceback=f"SyntaxError: {exc.msg} (line {exc.lineno})",
            flags=["syntax_error"],
        )

    # work/ and ctl/ are separate top-level temp dirs with independent random names.
    # If they were siblings, '../ctl/tests.json' would be a one-line read from the
    # solution. The runner additionally unlinks the test payload before importing the
    # solution, so by the time untrusted code executes there is nothing left on disk.
    workdir = tempfile.mkdtemp(prefix="ultron-work-")
    ctldir = tempfile.mkdtemp(prefix="ultron-ctl-")
    proc: subprocess.Popen | None = None
    try:
        with open(os.path.join(workdir, "solution.py"), "w") as fh:
            fh.write(code)

        payload = os.path.join(ctldir, "payload.json")
        report = os.path.join(ctldir, "report.json")

        # The report is still a file, and a solution that scans /tmp can still find and
        # overwrite it -- E05 and E08 both did. What it cannot do is produce this token.
        # It is generated here, travels in a payload that is unlinked before anything is
        # compiled, and lives from then on only in the runner's scoring process, which
        # never executes untrusted code; every forked child clears it before the
        # solution runs. A report without it is a forgery and scores zero.
        token = secrets.token_hex(16)
        with open(payload, "w") as fh:
            json.dump({"specs": specs, "report": report, "workdir": workdir, "token": token}, fh)

        started = time.monotonic()
        proc = subprocess.Popen(
            [python, "-I", "-B", _RUNNER, payload],
            cwd=workdir,
            preexec_fn=_limits(mem_mb, cpu_s, nproc, fsize_mb),
            env=_scrubbed_env(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )

        timed_out = False
        try:
            out, err = proc.communicate(timeout=wall_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            out, err, abandoned = _drain(proc)

        wall_ms = int((time.monotonic() - started) * 1000)

        if timed_out:
            return ExecResult(
                status="timeout",
                total=total,
                stdout=_clip(out),
                stderr=_clip(err),
                traceback=f"Wall-clock timeout after {wall_s}s"
                + (" (output abandoned: a process outlived the group kill)"
                   if abandoned else ""),
                exit_code=proc.returncode,
                wall_ms=wall_ms,
                flags=["group_escape"] if abandoned else [],
            )

        # Never trust the exit code. sys.exit(0) inside the solution produces a clean
        # 0 while nothing was verified, so scoring reads only the runner's report --
        # which is written to a path the solution was never shown.
        return _read_report(report, total, out, err, proc.returncode, wall_ms, token)
    finally:
        # Belt and braces: if we left by any path other than a clean communicate()
        # -- an exception in our own code included -- the group must still die, or we
        # leak the orphans this whole module exists to prevent.
        if proc is not None and proc.poll() is None:
            _kill_group(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(ctldir, ignore_errors=True)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group.

    This is the bug almost every hand-rolled sandbox ships. subprocess's own timeout
    kills the direct child only; anything it spawned survives as an orphan and they
    accumulate all night. os.setsid() in preexec_fn is what makes the group killable.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def _drain(proc: subprocess.Popen, grace: float = 2.0) -> tuple[str, str, bool]:
    """Collect whatever output survives the kill, without ever blocking forever.

    A plain communicate() here is a deadlock waiting to happen. Our pipes were
    inherited by every descendant, so any process that outlives the group kill still
    holds their write end open and EOF never arrives -- the harness hangs on a random
    step with no error, which is the single most expensive failure mode in the trap
    index. A solution can arrange this deliberately: call os.setsid() in a child and it
    leaves the process group that killpg targets.

    Verified by negative control: with the group kill replaced by a plain proc.kill(),
    an unbounded communicate() here hangs indefinitely.

    Returns (stdout, stderr, abandoned).
    """
    try:
        out, err = proc.communicate(timeout=grace)
        return out or "", err or "", False
    except subprocess.TimeoutExpired:
        # Something escaped. Drop the pipes rather than wait on a writer that will
        # never close, and flag it -- at Level 1 this is detectable, not preventable.
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        return "", "", True


def _clip(text: str | None, limit: int = 8000) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [clipped {len(text) - limit} chars]"


def _read_report(
    report: str, total: int, out: str, err: str, rc: int | None, wall_ms: int, token: str
) -> ExecResult:
    try:
        with open(report) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        # No structured report: the process died hard before it could write one --
        # SIGKILL from RLIMIT_CPU, SIGXFSZ from RLIMIT_FSIZE, an OOM at import, or a
        # crash in the interpreter itself. Distinct from 'error', which means the
        # runner survived and told us the solution blew up.
        return ExecResult(
            status="crash",
            total=total,
            stdout=_clip(out),
            stderr=_clip(err),
            traceback=_crash_reason(rc, err),
            exit_code=rc,
            wall_ms=wall_ms,
        )

    # Two things must hold before a well-formed report is believed, and both exist
    # because phase 1.2 measured solutions writing their own.
    #
    # The token proves the report came from the runner's scoring process. A solution
    # that finds the file in /tmp can overwrite it, and E05 and E08 both did -- but it
    # was cleared out of every child before the solution ran, so a forgery cannot carry
    # it. A report without the right token is not a failed run; it is evidence of one.
    #
    # rc == 0 closes the other half. To be the only writer, a forger has to stop the
    # runner from writing -- and the cheapest way to do that is to kill it, which it can
    # do, same UID. A killed runner cannot also exit 0.
    if data.get("token") != token:
        return ExecResult(
            status="crash",
            total=total,
            stdout=_clip(out),
            stderr=_clip(err),
            traceback="Report carried no valid token: it was not written by the runner.",
            exit_code=rc,
            wall_ms=wall_ms,
            flags=["forged_report"],
            fraud="forged_report",
        )
    if rc != 0:
        return ExecResult(
            status="crash",
            total=total,
            stdout=_clip(out),
            stderr=_clip(err),
            traceback=f"Runner exited {rc} but a report was present; not trusted. "
            + _crash_reason(rc, err),
            exit_code=rc,
            wall_ms=wall_ms,
            flags=["runner_died"],
        )

    passed = int(data.get("passed", 0))
    reported_total = int(data.get("total", total)) or total
    flags = list(data.get("flags", []))
    import_error = data.get("import_error") or ""

    # A flagged fraud is a zero, not a 1.0 with a note attached. See FRAUD_FLAGS.
    fraud = next((f for f in FRAUD_FLAGS if f in flags), "")

    if fraud:
        status = "fraud"
        passed = 0
    elif import_error:
        status = "error"
    elif passed == reported_total and reported_total > 0:
        status = "pass"
    else:
        status = "fail"

    return ExecResult(
        status=status,
        passed=passed,
        total=reported_total,
        fraction=0.0 if fraud else (passed / reported_total if reported_total else 0.0),
        stdout=_clip(out),
        stderr=_clip(err),
        traceback=_clip(import_error or "\n".join(data.get("tracebacks", [])), 4000),
        exit_code=rc,
        wall_ms=wall_ms,
        cpu_ms=int(data.get("cpu_ms", 0)),
        flags=flags,
        fraud=fraud,
    )


def _crash_reason(rc: int | None, err: str) -> str:
    if rc is not None and rc < 0:
        sig = -rc
        name = signal.Signals(sig).name if sig in {s.value for s in signal.Signals} else str(sig)
        hint = {
            "SIGKILL": "hard RLIMIT_CPU limit, or the OOM killer",
            "SIGXCPU": "RLIMIT_CPU soft limit",
            "SIGXFSZ": "RLIMIT_FSIZE exceeded",
            "SIGSEGV": "segfault",
        }.get(name, "")
        return f"Killed by {name}" + (f" ({hint})" if hint else "")
    tail = (err or "").strip().splitlines()
    return tail[-1] if tail else f"No report written; exit code {rc}"
