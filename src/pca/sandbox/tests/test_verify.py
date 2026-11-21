"""Phase 1.4 — the agent-facing API, and the refusal that is the phase 1 gate.

Most of this file only runs inside the container, because `verify()` declines to execute a
model's patch anywhere else and that refusal is the point rather than an obstacle. The two
tests that force the refusal run everywhere: they move the marker rather than move the test,
so the behaviour is checked on the machine where it matters most.

On macOS these skip and the host suite reports them as skipped. Inside the container they run,
and `scripts/run-in-container.sh` still requires zero skips — which is how a mistake that
makes them skip *there* gets caught instead of reading as success.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import textwrap

import pytest

from pca.sandbox.verify import (
    NotContained,
    Report,
    containment,
    require_containment,
    verify,
)

CONTAINED = containment().ok
needs_container = pytest.mark.skipif(
    not CONTAINED, reason="verify() refuses to run a model's patch outside the container"
)

MATHY_BROKEN = "def add(a, b):\n    return a - b\n"
MATHY_FIXED = "def add(a, b):\n    return a + b\n"
TESTS = textwrap.dedent(
    """\
    from mathy import add


    def test_add():
        assert add(2, 3) == 5


    def test_add_zero():
        assert add(0, 0) == 0
    """
)
TESTS_GUTTED = textwrap.dedent(
    """\
    from mathy import add


    def test_add():
        assert True


    def test_add_zero():
        assert add(0, 0) == 0
    """
)


def patch_for(path: str, before: str, after: str) -> str:
    """Generate the diff instead of hand-writing one.

    A hand-written unified diff is three lines of context away from not applying, and a test
    that fails because its fixture is malformed looks exactly like a test that caught
    something.
    """
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


@pytest.fixture
def repo(tmp_path):
    """A two-file project whose `add` is wrong, with one test that catches it and one that does not.

    `test_add_zero` passes against the broken implementation, so the fraction is 0.5 rather
    than 0 — a partial score the loop can actually move, and proof the count is per-test.
    """
    (tmp_path / "mathy.py").write_text(MATHY_BROKEN)
    (tmp_path / "test_mathy.py").write_text(TESTS)
    return str(tmp_path)


def fingerprint(root: str) -> str:
    """Every path and every byte under `root`, hashed. Used to prove the original is untouched."""
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(base, name)
            digest.update(os.path.relpath(full, root).encode())
            with open(full, "rb") as fh:
                digest.update(fh.read())
    return digest.hexdigest()


# --------------------------------------------------------------------- the refusal, anywhere


def test_containment_refusal_names_what_is_missing(monkeypatch):
    """A refusal that does not say why is a wall.

    Checked by moving the marker rather than by moving the test, so this runs on the host and
    in the container and asserts the same thing in both.
    """
    monkeypatch.setattr("pca.sandbox.verify.MARKER", "/nonexistent/pca-sandbox")
    state = containment()
    assert not state.ok
    assert state.signals["marker"] is False
    assert "pca-sandbox" in state.detail and "docker/Dockerfile" in state.detail


def test_verify_refuses_when_the_marker_is_absent(monkeypatch, repo):
    monkeypatch.setattr("pca.sandbox.verify.MARKER", "/nonexistent/pca-sandbox")
    with pytest.raises(NotContained) as excinfo:
        verify(None, repo, "test_mathy.py")
    message = str(excinfo.value)
    assert "will not run here" in message
    assert "run-in-container.sh" in message, "the refusal must say what to do instead"


def test_network_is_the_signal_that_tests_the_claim(monkeypatch):
    """The marker is a file we wrote; the interface list is evidence.

    Running the right image with networking on leaves the marker in place and must still be
    refused, so this signal is the one that can actually fail closed.
    """
    monkeypatch.setattr("pca.sandbox.verify.NET_DIR", "/nonexistent/net")
    state = containment()
    assert not state.ok
    assert state.signals["network_down"] is False
    with pytest.raises(NotContained):
        require_containment()


# --------------------------------------------------------------------- the work, in Linux only


@needs_container
def test_unpatched_run_reports_which_test_fails(repo):
    """How the loop learns there is something to fix, before it tries to fix it."""
    report = verify(None, repo, "test_mathy.py")
    assert report.status == "fail", (report.status, report.detail)
    assert report.applied is True
    assert (report.passed, report.failed, report.total) == (1, 1, 2)
    assert report.fraction == 0.5
    assert [f.test.split("::")[-1] for f in report.failures] == ["test_add"]
    assert "add(2, 3) == 5" in report.failures[0].text


@needs_container
def test_a_patch_that_fixes_the_code_passes(repo):
    report = verify(patch_for("mathy.py", MATHY_BROKEN, MATHY_FIXED), repo, "test_mathy.py")
    assert report.status == "pass", (report.status, report.detail, report.stdout)
    assert report.ok
    assert (report.passed, report.failed, report.total) == (2, 0, 2)
    assert report.fraction == 1.0
    assert report.touched == ["mathy.py"]
    assert report.touched_tests == []


@needs_container
def test_a_patch_that_edits_the_oracle_is_restored_and_still_fails(repo):
    """The cheapest hack an agent finds: do not fix the code, weaken the test.

    The fix is not to reject the patch. A patch that repairs the code AND guts the test should
    have its repair measured against the real test, so the test file is restored and the run
    goes on. What must never happen is the gutted assertion counting as a pass.
    """
    report = verify(patch_for("test_mathy.py", TESTS, TESTS_GUTTED), repo, "test_mathy.py")
    assert report.applied is True
    assert report.touched_tests == ["test_mathy.py"]
    assert report.tests_restored is True
    assert report.status == "fail", "the gutted assertion was scored as a pass"
    assert (report.passed, report.failed) == (1, 1)


@needs_container
def test_a_patch_that_edits_both_is_measured_on_the_real_test(repo):
    """And the honest half of the same patch still counts."""
    combined = patch_for("mathy.py", MATHY_BROKEN, MATHY_FIXED) + patch_for(
        "test_mathy.py", TESTS, TESTS_GUTTED
    )
    report = verify(combined, repo, "test_mathy.py")
    assert report.tests_restored is True
    assert report.status == "pass", (report.status, report.detail)
    assert report.touched_tests == ["test_mathy.py"]


@needs_container
def test_a_patch_climbing_out_of_the_tree_is_rejected(repo, tmp_path):
    """`git apply` without --unsafe-paths refuses this. Asserting it means a later convenience
    flag cannot quietly turn it back on."""
    escape = (
        "--- a/../../escaped.txt\n"
        "+++ b/../../escaped.txt\n"
        "@@ -0,0 +1 @@\n"
        "+owned\n"
    )
    report = verify(escape, repo, "test_mathy.py")
    assert report.status == "patch_rejected", (report.status, report.detail)
    assert report.applied is False
    assert report.detail, "a rejection must carry git's reason"
    assert not os.path.exists("/escaped.txt")
    assert not (tmp_path.parent / "escaped.txt").exists()


@needs_container
def test_a_selector_that_matches_nothing_is_not_a_pass(repo):
    """pytest exits 5 with no failures. 'No failures' is what a naive wrapper reads.

    An agent writing its own node ids hits this constantly, and a reward that scores it as
    success teaches the model to write selectors that match nothing — which is the same
    absence-as-success failure the executor's report-not-exit-code rule exists to prevent.
    """
    report = verify(None, repo, "test_mathy.py::test_that_does_not_exist")
    assert report.status == "no_tests", (report.status, report.exit_code)
    assert report.total == 0
    assert report.fraction == 0.0
    assert not report.ok
    assert "not a pass" in report.detail


@needs_container
def test_the_original_repository_is_never_written_to(repo):
    """Every path and byte under the repo, before and after a patch that rewrites both files."""
    before = fingerprint(repo)
    combined = patch_for("mathy.py", MATHY_BROKEN, MATHY_FIXED) + patch_for(
        "test_mathy.py", TESTS, TESTS_GUTTED
    )
    report = verify(combined, repo, "test_mathy.py")
    assert report.applied is True
    assert fingerprint(repo) == before, "verify() wrote to the caller's repository"


@needs_container
def test_a_hanging_suite_times_out_rather_than_hanging_the_loop(repo):
    with open(os.path.join(repo, "test_slow.py"), "w") as fh:
        fh.write("import time\n\n\ndef test_slow():\n    time.sleep(999)\n")
    report = verify(None, repo, "test_slow.py", wall_s=6, cpu_s=30)
    assert report.status == "timeout", (report.status, report.detail)
    assert report.duration_ms < 30_000, f"{report.duration_ms} ms — the kill is not bounded"


@needs_container
def test_counts_come_from_the_xml_not_the_exit_code(repo):
    """The exit code says 'something went wrong'. The loop needs how many, of which kind.

    One failing test and one passing test produce the same exit code as twenty of each, so a
    wrapper built on the status can only ever return a boolean — and a boolean reward is the
    sparse one GRPO barely moves on.
    """
    report = verify(None, repo, "test_mathy.py")
    assert report.exit_code == 1
    assert (report.passed, report.failed, report.errored, report.skipped) == (1, 1, 0, 0)
    assert isinstance(report, Report)
    assert report.to_dict()["failures"][0]["kind"] == "failure"
