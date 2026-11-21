"""Runs inside the sandbox. Forks once per test; the forking parent scores and never
executes the solution.

This file lives OUTSIDE the child's cwd on purpose. It is invoked by absolute path with
`python -I` (isolated mode: no cwd on sys.path, no user site-packages, env ignored).

> **Rewritten 2026-11-21 by phase 1.3.** The previous version imported the solution into
> this process and then, in this same process, compiled and executed each test and
> computed the score. `results/01-exploit-corpus.md` measured six of ten exploits
> scoring a perfect 1.0 against it, 25 times out of 25, on both machines. Three of those
> six followed from nothing more than scorer and subject sharing an interpreter, and two
> more from the control data being reachable at `sys._getframe().f_back`. The ordering
> the old docstring called load-bearing was correct and insufficient: it got the tests
> off disk and out of argv, and left them in a local variable one frame up.

Ordering here is still load-bearing, and now it is ordering across processes:

    1. read the payload; unlink it; scrub argv
    2. COMPILE the solution and every test -- before any untrusted code exists
    3. for each test: fork. The child clears _CTL, arms the audit hook, imports the
       solution, runs its one test, and reports through a pipe
    4. the parent -- which has never executed untrusted code -- scores and writes the
       report, carrying a token the children were never shown

What each step buys, and the exploit it closes:

* **Pre-compilation (2)** means `builtins.compile` is never called after the solution
  has had a chance to rebind it. E04 patched `compile` so each test compiled to `pass`;
  there is now nothing left to patch. The child also binds `exec` to a local before the
  import, so rebinding the builtin afterwards reaches nothing.
* **The fork (3)** means a patched `_write`, a neutered `_flag`, a poisoned builtin or a
  planted `atexit` handler dies with the child that made it and cannot reach the next
  test or the score. E07 and E04 both depended on living in the scoring process.
* **`_CTL.clear()` in the child, before any untrusted code (3)** removes the report
  path, the token and every *other* test from the child's reach. E05 and E06 walked the
  stack for exactly those. In snippet mode the child still holds its own test, which it
  has to; see RESIDUAL 2.
* **Parent writes last, with a token (4)** means a report forged in `/tmp` is either
  overwritten or rejected. E05 and E08 both wrote a perfect report and relied on nobody
  writing after them.

RESIDUAL 1 -- a child can forge its own result. It cannot reach the other tests or the
report, but in snippet mode it decides its own pass/fail, and `os._exit(0)` after a
forged message is indistinguishable from an honest one. The answer is check mode, where
the expected value never enters the child: see `_CHECK` below and DECISIONS.md X2.

RESIDUAL 2 -- PEP 667. On Python 3.13+ `frame.f_locals` writes through to the real
locals, so a solution can reach into this module's frames rather than only read them.
That weakens snippet mode further and does not touch check mode, which does not depend
on the child being honest. This file targets 3.12, where `f_locals` is a snapshot.

RESIDUAL 3 -- same-UID memory. A child that can `ptrace` the parent can read the token.
Linux `yama.ptrace_scope` and the container's dropped `CAP_SYS_PTRACE` both prevent it;
macOS requires an entitlement. Not defended here, and Level 2 is the answer.
"""

from __future__ import annotations

import contextlib
import json
import os
import resource
import sys
import traceback

# Bound before any untrusted code can exist, so a rebinding of the builtin reaches
# nothing this module calls. The child re-binds these to its own locals as well; module
# globals are writable through sys.modules['__main__'] and locals are not.
_EXEC = exec
_COMPILE = compile

# Everything the scoring parent knows. The child clears this before running anything
# untrusted, so the report path, the token and the other tests leave with it.
_CTL: dict = {}

_STATE = {"armed": False, "flags": set(), "workdir": ""}

# A child's message is one length-prefixed JSON object. Two messages concatenate into
# something that does not parse, so a planted thread writing early fails the check
# closed rather than racing it.
_MAX_MSG = 256 * 1024

_SNIPPET = "snippet"
_CHECK = "check"

# Reading a stdlib source file is how an import is satisfied, not an escape attempt.
# Flagging it made `import ast` indistinguishable from reading /etc/passwd -- measured
# in results/01-exploit-corpus.md §5, and a reward that penalises the flag penalises
# importing. /etc/hosts is not under any of these, so a genuine read is still caught.
_LIB_ROOTS: tuple[str, ...] = tuple(
    sorted(
        {
            os.path.realpath(os.path.dirname(os.__file__)),
            os.path.realpath(sys.base_prefix),
            os.path.realpath(sys.prefix),
        }
    )
)


# How much of a child's own stdout/stderr travels back to the parent. The rest is
# dropped in the child, where RLIMIT_AS applies to it.
_MAX_CAPTURE = 16384


class _Capture:
    """A write sink that stops accumulating past a limit but never stops accepting.

    E10 writes 16 MiB to stdout. Before 1.3 that went straight down the executor's pipe
    and `communicate()` buffered all of it in the grader before `_clip()` saw a byte --
    measured at +57 MB of parent RSS. The per-test fork then made it worse rather than
    better: the solution is imported once per test now, so the same flood ran three
    times and the parent took 198 MB. Capturing in the child caps it at the source, and
    the memory it does use is inside a process RLIMIT_AS governs.

    Raising rather than truncating would be wrong: a solution whose print fails is a
    solution that can detect the sandbox, and the failure would surface as an exception
    from inside its own code.

    RESIDUAL: `os.write(1, ...)` bypasses `sys.stdout` entirely and still reaches the
    executor's pipe unbounded. Closing that needs a bounded reader in the parent, in
    place of `communicate()` -- see results/02-redesign.md.
    """

    __slots__ = ("_n", "_parts")

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._n = 0

    def write(self, text) -> int:
        text = str(text)
        if self._n < _MAX_CAPTURE:
            self._parts.append(text[: _MAX_CAPTURE - self._n])
        self._n += len(text)
        return len(text)

    def flush(self) -> None:
        pass

    def value(self) -> str:
        out = "".join(self._parts)
        if self._n > _MAX_CAPTURE:
            out += f"\n... [clipped {self._n - _MAX_CAPTURE} chars in the sandbox]"
        return out


def _flag(name: str) -> None:
    _STATE["flags"].add(name)


def _audit(event: str, args) -> None:
    """Flag suspicious behaviour. Level 1 detects; Level 2 prevents.

    Runs only in a forked child now, so flipping `_STATE['armed']` -- the residual the
    previous version recorded here -- costs a solution its own flags and nothing else.
    The parent collects flags from the child's message and the child cannot write the
    report.
    """
    if not _STATE["armed"]:
        return
    try:
        if event == "open":
            path = args[0]
            if not isinstance(path, (str, bytes, os.PathLike)):
                return  # fd-based open; nothing to classify
            p = os.fsdecode(path)
            resolved = os.path.realpath(p)
            if resolved.startswith(_LIB_ROOTS):
                return  # satisfying an import, not escaping
            base = os.path.basename(p).lower()
            if "test" in base or "payload" in base or "report" in base:
                _flag("read_test_file")
            if not resolved.startswith(os.path.realpath(_STATE["workdir"])):
                mode = args[1] if len(args) > 1 else "r"
                if isinstance(mode, str) and any(c in mode for c in "wax+"):
                    _flag("fs_escape_write")
                else:
                    _flag("fs_read_outside_cwd")
        elif event in ("socket.connect", "socket.getaddrinfo", "socket.create_connection") or event == "urllib.Request":
            _flag("net_attempt")
        elif event in ("os.fork", "os.forkpty", "os.posix_spawn"):
            _flag("fork_attempt")
        elif event == "subprocess.Popen":
            _flag("subprocess_spawn")
        elif event in ("os.system", "os.exec"):
            _flag("exec_attempt")
    except Exception:
        # An exception raised inside an audit hook propagates into whatever the
        # solution was doing and would be indistinguishable from a real failure.
        pass


def _cpu_ms() -> int:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    ch = resource.getrusage(resource.RUSAGE_CHILDREN)
    total = ru.ru_utime + ru.ru_stime + ch.ru_utime + ch.ru_stime
    return int(total * 1000)


def _write(report_path: str, data: dict) -> None:
    """Only ever called in the scoring parent, which has executed nothing untrusted."""
    data["cpu_ms"] = _cpu_ms()
    try:
        with open(report_path, "w") as fh:
            json.dump(data, fh)
    except OSError:
        pass


# --------------------------------------------------------------------------------- IPC


def _send(wfd: int, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8", "replace")[:_MAX_MSG]
    with contextlib.suppress(OSError):
        os.write(wfd, len(data).to_bytes(4, "big") + data)


def _recv(rfd: int) -> dict | None:
    """Exactly one well-formed message, or None.

    Fails closed on every other shape: nothing written (the child was killed, or called
    os._exit before reporting), a short write, or more bytes than one message -- which
    is what a second writer looks like.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            block = os.read(rfd, 65536)
        except OSError:
            return None
        if not block:
            break
        chunks.append(block)
        total += len(block)
        if total > _MAX_MSG + 4:
            return None
    buf = b"".join(chunks)
    if len(buf) < 4:
        return None
    size = int.from_bytes(buf[:4], "big")
    if len(buf) != 4 + size:
        return None
    try:
        obj = json.loads(buf[4:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# ------------------------------------------------------------------------------- child


def _child(index: int, wfd: int) -> None:
    """Runs ONLY in a forked child, and never returns.

    The first four statements are the whole security argument: take this child's own
    work, then erase everything else this process inherited, before a single line of the
    solution has run.
    """
    _exec = _EXEC  # a local; sys.modules['__main__'] cannot reach it
    spec = _CTL["specs"][index]
    solution_code = _CTL["solution"]
    _STATE["workdir"] = _CTL["workdir"]
    _CTL.clear()

    msg: dict = {"r": "fail", "tb": "", "flags": [], "v": None, "out": "", "err": ""}
    ns: dict = {"__name__": "solution", "__file__": "solution.py"}

    # The capture covers the solution's import as well as its tests. It used to start
    # after the import, which let import-time output reach the executor's pipe
    # unbounded -- and import-time is exactly where a flood is cheapest to write.
    cap_out, cap_err = _Capture(), _Capture()
    sys.stdout, sys.stderr = cap_out, cap_err

    sys.addaudithook(_audit)
    _STATE["armed"] = True
    try:
        _exec(solution_code, ns)
    except BaseException:
        _STATE["armed"] = False
        msg["r"] = "import_error"
        msg["tb"] = traceback.format_exc(limit=6)
        msg["flags"] = sorted(_STATE["flags"])
        msg["out"], msg["err"] = cap_out.value(), cap_err.value()
        _send(wfd, msg)
        _exit_child()
    _STATE["armed"] = False

    # The equality oracle. A type defining __eq__ or __ne__ makes `assert f(x) == y`
    # true for any return value, and the assertion itself cannot tell. Detected here,
    # reported to the parent, and -- since 1.3 -- zeroed by the executor rather than
    # left as a note attached to a 1.0.
    for obj in ns.values():
        if isinstance(obj, type) and ("__eq__" in vars(obj) or "__ne__" in vars(obj)):
            _flag("eq_override")
            break

    try:
        _STATE["armed"] = True
        if spec["k"] == _CHECK:
            # Check mode. The child evaluates the call and returns what the solution
            # produced; the expected value stayed in the parent and was cleared out of
            # this process before the solution ran. os._exit(0) now yields no value
            # rather than a pass, and forging one means computing the right answer --
            # which is the task.
            value = eval(spec["code"], dict(ns))
            msg["r"] = "value"
            msg["v"] = json.dumps(value, sort_keys=True, default=_unserialisable)
        else:
            _exec(spec["code"], dict(ns))
            msg["r"] = "pass"
    except BaseException:
        # BaseException, not Exception: a test must still count as failed when the
        # solution raises SystemExit or KeyboardInterrupt to escape it.
        # Disarm FIRST: traceback.format_exc() opens source files to render frames,
        # which would otherwise raise a spurious fs_read_outside_cwd on every ordinary
        # failure -- poisoning the GRPO reward with a phantom violation.
        _STATE["armed"] = False
        msg["r"] = "raise" if spec["k"] == _CHECK else "fail"
        msg["tb"] = traceback.format_exc(limit=6)
    finally:
        _STATE["armed"] = False

    msg["out"], msg["err"] = cap_out.value(), cap_err.value()
    msg["flags"] = sorted(_STATE["flags"])
    _send(wfd, msg)
    _exit_child()


def _exit_child() -> int:
    """Leave a child without running anything the parent registered.

    `os._exit` is the point -- it skips atexit handlers, `finally` blocks and
    interpreter shutdown, so a solution cannot act after its test is scored. It also
    skips flushing, which is not the point and cost six tests in the inherited suite:
    the solution's own import-time `print` sat in a buffer that was discarded, and
    `r.stdout` came back empty on every run. Flush first, then leave.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
    os._exit(0)


def _unserialisable(obj):
    """json.dumps default for check mode.

    A value that will not serialise is not comparable to the expected one, so it is
    turned into something that can never equal it rather than raising -- the check
    fails on the comparison, which is the honest outcome, instead of on the encoding.
    """
    return {"__unserialisable__": type(obj).__name__}


# ------------------------------------------------------------------------------ parent


def _run_one(index: int) -> dict:
    """Fork, let the child do its one test, and return what it said.

    Never raises on the child's behalf. A child that dies without reporting -- killed by
    RLIMIT_CPU, killed by the OOM killer, or calling os._exit(0) at import to claim a
    clean exit -- produces None here and is scored as a failure.
    """
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(rfd)
            _child(index, wfd)
        except BaseException:
            # _child must never return. If anything at all escapes it, die rather than
            # fall back into the parent's loop as a second scorer.
            os._exit(70)
    os.close(wfd)
    wait_status = 0
    try:
        msg = _recv(rfd)
    finally:
        os.close(rfd)
        with contextlib.suppress(OSError):
            _, wait_status = os.waitpid(pid, 0)

    # A child killed by a signal is RLIMIT_CPU, RLIMIT_FSIZE or the OOM killer doing
    # its job, and that is a verdict about the run rather than about one test. Before
    # the per-test fork it killed the only process there was and the executor scored
    # 'crash'; it must still say so, or `while True: pass` quietly becomes a failed
    # assertion and the containment it demonstrates goes unrecorded.
    if os.WIFSIGNALED(wait_status):
        return {"r": "died", "sig": os.WTERMSIG(wait_status), "flags": [], "tb": "", "v": None}

    if msg is None:
        return {
            "r": "fail",
            "tb": f"[test {index}] child produced no usable result (killed, or exited before reporting)",
            "flags": [],
            "v": None,
        }
    return msg


def _score(index: int, msg: dict) -> tuple[bool, str]:
    """Decide pass/fail in the parent. In check mode the expected value lives here."""
    spec = _CTL["specs"][index]
    kind = msg.get("r")
    if spec["k"] == _CHECK:
        if kind != "value":
            tb = msg.get("tb") or "no value returned"
            return False, f"[check {index}] {tb}"
        want = _CTL["expected"][index]
        got = msg.get("v")
        if got == want:
            return True, ""
        return False, f"[check {index}] {spec['src']} -> {got!r}, expected {want!r}"
    if kind == "pass":
        return True, ""
    return False, (msg.get("tb") or f"[test {index}] failed")


def main() -> int:
    with open(sys.argv[1]) as fh:
        payload = json.load(fh)

    # Nothing on disk, nothing in argv. Unlinked before anything is compiled, let alone
    # executed, so even a crash between here and the first fork leaves nothing behind.
    with contextlib.suppress(OSError):
        os.unlink(sys.argv[1])
    sys.argv = ["solution"]

    _CTL["workdir"] = payload["workdir"]
    _CTL["report"] = payload["report"]
    _CTL["token"] = payload["token"]

    with open(os.path.join(_CTL["workdir"], "solution.py")) as fh:
        source = fh.read()

    # Compiled here, in a process where no untrusted code has ever run, and before the
    # first fork. This is what makes rebinding `compile` in the solution pointless.
    specs: list[dict] = []
    expected: list = []
    for i, item in enumerate(payload["specs"]):
        if item["k"] == _CHECK:
            specs.append({"k": _CHECK, "src": item["src"], "code": _COMPILE(item["src"], f"<check{i}>", "eval")})
            expected.append(json.dumps(item["expected"], sort_keys=True))
        else:
            specs.append({"k": _SNIPPET, "src": item["src"], "code": _COMPILE(item["src"], f"<test{i}>", "exec")})
            expected.append(None)
    _CTL["specs"] = specs
    _CTL["expected"] = expected
    _CTL["solution"] = _COMPILE(source, "solution.py", "exec")

    total = len(specs)
    passed = 0
    tracebacks: list[str] = []
    flags: set[str] = set()

    import_error = ""
    for i in range(total):
        msg = _run_one(i)
        flags.update(x for x in msg.get("flags", []) if isinstance(x, str))

        if msg.get("r") == "died":
            # No report at all. The executor reads a missing report as 'crash', which
            # is the same verdict the single-process runner gave when the limit killed
            # it -- reached here deliberately rather than by dying ourselves.
            sys.stderr.write(
                f"child for test {i} was killed by signal {msg.get('sig')}; "
                "no score written\n"
            )
            return 70

        if msg.get("out"):
            sys.stdout.write(msg["out"])
        if msg.get("err"):
            sys.stderr.write(msg["err"])

        if msg.get("r") == "import_error" and not import_error:
            # Every test will report this, since none of them can run. Recorded once,
            # and the executor turns it into status='error' rather than 'fail': the
            # solution did not fail the tests, it never reached them.
            import_error = msg.get("tb", "")

        ok, why = _score(i, msg)
        if ok:
            passed += 1
        else:
            tracebacks.append(why)

    _write(
        _CTL["report"],
        {
            "token": _CTL["token"],
            "passed": passed,
            "total": total,
            "import_error": import_error,
            "tracebacks": tracebacks,
            "flags": sorted(flags),
        },
    )
    return 0


if __name__ == "__main__":
    # Catch Exception, NOT BaseException: sys.exit() raises SystemExit, so a bare
    # BaseException handler here catches our own clean exit, prints a spurious
    # traceback, and replaces the real exit code with 70 on every single run.
    try:
        _rc = main()
    except Exception:
        # Never let the runner die silently -- a missing report is scored as 'crash'
        # and we would rather see why on stderr.
        traceback.print_exc()
        _rc = 70
    sys.exit(_rc)
