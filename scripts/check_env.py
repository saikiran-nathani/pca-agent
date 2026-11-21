#!/usr/bin/env python3
"""The environment gate.

It exists because of one failure shape this project refuses to repeat: a check
that reports success when it checked nothing. A pytest run pointed at a
directory with no tests collects zero and exits 0. A `-k` pattern that matches
nothing prints "23 deselected" and exits 0. A plugin named in config but not
installed is silently inert -- `timeout = 120` in pyproject.toml did nothing at
all until pytest-timeout was installed, and the only evidence was one warning
line nobody reads.

So this gate does not report pass or fail. It reports **how many checks it ran**
and refuses to pass if that number is below what it declares for this machine.
Deleting a check is then as loud as breaking one.

    python scripts/check_env.py            # this machine
    python scripts/check_env.py --verbose  # with every detail line

Exit 0 only when every check passed AND the declared count was met.
"""

from __future__ import annotations

import argparse
import importlib.util
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The minimum number of test cases `pytest --collect-only` must find. The
# adversarial suite alone is 23. A number the runner asserts is a gate; the same
# number written into prose is drift with a date on it, which is why it lives
# here and not in the README.
MIN_COLLECTED_TESTS = 23

# How many checks each machine must actually run. Raise these when you add one.
DECLARED_CHECKS = {"Darwin": 6, "Linux": 9}

MIN_PYTHON = (3, 12)


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


def _run(*cmd: str, cwd: Path = ROOT, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------
# checks that run on both machines
# --------------------------------------------------------------------------

def check_python_version() -> Result:
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    want = ".".join(str(x) for x in MIN_PYTHON)
    return Result(
        "python version",
        ok,
        f"{v.major}.{v.minor}.{v.micro}" + ("" if ok else f" — need >= {want}"),
    )


def check_venv_is_this_repos() -> Result:
    """A gate run against the wrong interpreter grades the wrong machine."""
    # sys.executable resolves through uv's symlink to the interpreter it
    # installed, so .venv never appears in its parents. sys.prefix is the venv
    # root itself, which is the question being asked.
    prefix = Path(sys.prefix).resolve()
    expected = (ROOT / ".venv").resolve()
    ok = prefix == expected
    return Result(
        "interpreter is this repo's venv",
        ok,
        str(prefix) + ("" if ok else f" — expected {expected}"),
    )


def check_package_imports() -> Result:
    """src-layout: `pca.sandbox.executor` must resolve without an editable install."""
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from pca.sandbox.executor import run  # noqa: F401
    except Exception as exc:  # pragma: no cover - the failure is the message
        return Result("pca package imports", False, f"{type(exc).__name__}: {exc}")
    return Result("pca package imports", True, "pca.sandbox.executor.run resolved")


def check_pytest_timeout_is_active() -> Result:
    """Installed is not the same as loaded.

    pyproject.toml sets `timeout = 120`. Without the plugin pytest emits
    `PytestConfigWarning: Unknown config option: timeout` and runs with no
    timeout at all -- which is how a hung test once consumed a six-hour CI job.
    Asking pytest which plugins it loaded is the only honest form of this check.
    """
    if importlib.util.find_spec("pytest_timeout") is None:
        return Result("pytest-timeout active", False, "not installed; `timeout = 120` is inert")
    proc = _run(sys.executable, "-m", "pytest", "--version", "--version")
    loaded = "timeout" in (proc.stdout + proc.stderr)
    return Result(
        "pytest-timeout active",
        loaded,
        "plugin loaded, `timeout = 120` is live" if loaded else "installed but not loaded",
    )


def check_collected_test_count() -> Result:
    """The count, asserted. This is the check the whole file exists for."""
    proc = _run(sys.executable, "-m", "pytest", "--collect-only", "-q")
    out = proc.stdout

    # Three shapes, because pytest has used all of them and a count check that
    # understands one version's output stops counting on upgrade -- silently,
    # which is the exact failure this file exists to prevent.
    n: int | None = None
    if m := re.search(r"(\d+)\s+tests?\s+collected", out):
        n = int(m.group(1))
    elif per_file := re.findall(r"^\S+\.py:\s*(\d+)\s*$", out, re.MULTILINE):
        n = sum(int(x) for x in per_file)
    elif node_ids := [ln for ln in out.splitlines() if "::" in ln]:
        n = len(node_ids)

    if n is None:
        tail = (out or proc.stderr).strip().splitlines()[-3:]
        return Result(
            "collected test count",
            False,
            "could not read a count from pytest — " + " / ".join(tail),
        )
    ok = n >= MIN_COLLECTED_TESTS
    return Result(
        "collected test count",
        ok,
        f"{n} collected"
        + ("" if ok else f" — below the declared minimum of {MIN_COLLECTED_TESTS}"),
    )


def check_sandbox_reads_a_report_not_an_exit_code() -> Result:
    """The one design property the agent's safety rests on.

    If the executor ever scores from the subprocess exit code, `sys.exit(0)`
    before the assertions is a passing grade. Asserting it here means a
    refactor that reintroduces it fails the gate rather than a benchmark.
    """
    src = (ROOT / "src/pca/sandbox/executor.py").read_text()
    ok = "report.json" in src
    return Result(
        "sandbox scores from a report file",
        ok,
        "executor references ctl/report.json" if ok else "no report.json reference — "
        "check the executor is not scoring from the exit code",
    )


# --------------------------------------------------------------------------
# TUF only
# --------------------------------------------------------------------------

def check_cuda_allocates() -> Result:
    """Allocate, do not ask.

    `torch.cuda.is_available()` and a working card are different questions, and
    so are `nvidia-smi` and CUDA: on 2026-11-20 this machine had a broken NVML
    userspace library, nvidia-smi refused to start, three planning documents
    recorded CUDA as down, and a 512 MiB allocation succeeded.
    """
    code = (
        "import torch;"
        "x=torch.zeros(128,1024,1024,dtype=torch.float16,device='cuda');"
        "torch.cuda.synchronize();"
        "f,t=torch.cuda.mem_get_info();"
        "print(torch.cuda.get_device_capability(), round(f/2**20), round(t/2**20))"
    )
    proc = _run(sys.executable, "-c", code)
    ok = proc.returncode == 0
    detail = proc.stdout.strip() if ok else proc.stderr.strip().splitlines()[-1:]
    return Result("cuda allocates a real tensor", ok, str(detail))


def check_torchvision_matches_torch() -> Result:
    """The failure this prevents: `operator torchvision::nms does not exist`.

    requirements/cuda.txt protects torch from the PyPI CPU wheel and cannot
    protect torchvision, which resolves to the plain PyPI build whose compiled
    ops will not load against torch+cu128. unsloth then fails at import, long
    after the install reported success. The fix is documented; a documented fix
    is not a build step, so it is asserted here.
    """
    code = "import torch, torchvision; print(torch.__version__, torchvision.__version__)"
    proc = _run(sys.executable, "-c", code)
    if proc.returncode != 0:
        return Result("torchvision matches torch", False, proc.stderr.strip().splitlines()[-1])
    torch_v, tv_v = proc.stdout.split()
    torch_tag = torch_v.split("+")[1] if "+" in torch_v else ""
    tv_tag = tv_v.split("+")[1] if "+" in tv_v else ""
    ok = bool(torch_tag) and torch_tag == tv_tag
    return Result(
        "torchvision matches torch",
        ok,
        f"torch {torch_v} / torchvision {tv_v}"
        + ("" if ok else " — rebuild torchvision from the cu128 index, see requirements/cuda.txt"),
    )


def check_bf16_supported() -> Result:
    proc = _run(sys.executable, "-c", "import torch; print(torch.cuda.is_bf16_supported())")
    ok = proc.returncode == 0 and proc.stdout.strip() == "True"
    return Result("bf16 supported", ok, proc.stdout.strip() or proc.stderr.strip()[-80:])


COMMON = [
    check_python_version,
    check_venv_is_this_repos,
    check_package_imports,
    check_pytest_timeout_is_active,
    check_collected_test_count,
    check_sandbox_reads_a_report_not_an_exit_code,
]
LINUX_ONLY = [check_cuda_allocates, check_torchvision_matches_torch, check_bf16_supported]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    machine = platform.system()
    checks = COMMON + (LINUX_ONLY if machine == "Linux" else [])
    declared = DECLARED_CHECKS.get(machine)

    if declared is None:
        print(f"FAIL  this gate declares no check count for {machine!r}.")
        print("      Add one to DECLARED_CHECKS rather than letting it pass by default.")
        return 2

    print(f"pca environment gate — {machine}, {len(checks)} checks declared {declared}\n")

    results = [c() for c in checks]
    for r in results:
        mark = "ok  " if r.ok else "FAIL"
        line = f"  {mark}  {r.name}"
        if args.verbose or not r.ok:
            line += f"\n          {r.detail}"
        print(line)

    ran, passed = len(results), sum(r.ok for r in results)
    print(f"\n{passed}/{ran} passed")

    # Absence of evidence is not success. A gate that ran fewer checks than it
    # declares has been quietly hollowed out, and that must read as a failure
    # rather than as a clean run.
    if ran != declared:
        print(f"FAIL  ran {ran} checks, declares {declared} for {machine}.")
        print("      A check was deleted or skipped. Fix it, or update DECLARED_CHECKS on purpose.")
        return 2
    if passed != ran:
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
