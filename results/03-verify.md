# 03 — `verify()`, and the end of phase 1

**Built 2026-11-21.** Phase 1.4.

```python
verify(patch, repo, test_selector) -> Report
```

Copies the repository to a scratch tree, applies the model's patch to the copy, runs the
project's own tests, returns a structured result. The original is never written to. Nothing is
promoted — 2.4 does that, and only after this says pass.

**Phase 1 gate: all three clauses met.**

| clause | |
|---|---|
| every exploit scores zero, under repetition, inside the container | ✅ 10/10, 25 reps, both modes, both machines |
| the adversarial suite reports 23 there | ✅ 23, and 47 in total |
| `verify()` refuses to run outside a container and says so | ✅ below |

---

## 1. The refusal

Run on the Mac, which is where the mistake would actually happen:

```
verify() will not run here: /etc/pca-sandbox is absent, so this is not an image built from
docker/Dockerfile; /sys/class/net does not exist, so this is not Linux.
It applies a model's patch and executes a repository's test suite, and the only thing standing
between that and your machine is the container.
Run it through the harness instead:
    scripts/run-in-container.sh python -m pytest src/pca/sandbox/tests/test_verify.py
signals: {'marker': False, 'network_down': False}
```

A raised `NotContained`, not a field on the result. A caller that ignores a return value is the
failure mode this is guarding against, so it is not something a caller can ignore.

**Two signals, and they are not equally good.**

`marker` is `/etc/pca-sandbox`, written by our own Dockerfile. It answers "is this the image we
built", which is the only way to know the run carries the flags chosen in
`scripts/run-in-container.sh`. A generic "am I in a container" check answers a question nobody
asked: being in *some* container says nothing about whether the network is reachable.

`network_down` is the better half, because it **tests the claim instead of trusting a file we
wrote**. With `--network none` the only interface is loopback. Run this exact image with
networking on and the marker is still there — this is the signal that catches it.

**It guards against accident, not an adversary.** Anything that can already execute code in the
process calling `verify()` can create the marker. That is not the threat. The threat is running
a model's patch on a laptop, and the exception is what stops it.

## 2. Two traps, both closed in the API rather than left to the caller

**A selector that matches nothing is not a pass.** pytest exits 5 with no failures when a node
id is wrong, and "no failures" is exactly what a thin wrapper reads. An agent writing its own
selectors will hit this constantly, and a reward that scores it as success teaches the model to
write selectors that match nothing. `status="no_tests"`, `fraction=0.0`, and a detail that says
so in words.

This is the same shape as the executor's never-trust-the-exit-code rule, one level up.

**A patch that edits the tests is the cheapest hack there is.** Don't fix the code — weaken the
assertion. `protect_tests=True` restores any touched test file from the original after applying,
and `Report.touched_tests` and `Report.tests_restored` say it happened.

Restoring rather than rejecting is deliberate. A patch that repairs the code *and* guts the test
should have its repair measured against the real test, not thrown away. Both cases are tested:

| patch | result |
|---|---|
| guts the test only | `fail`, 1 of 2 — the gutted assertion did not count |
| fixes the code *and* guts the test | `pass`, measured on the restored test |

## 3. The structured part

Counts come from `--junitxml`, parsed with the stdlib, **never from the exit code**. One failure
and one pass produce the same exit status as twenty of each, so a wrapper built on the status
can only ever return a boolean — and a boolean reward is the sparse one GRPO barely moves on.

`Report` carries `passed / failed / errored / skipped / total`, a `fraction` over the
non-skipped cases, and `failures` as a list of `(test, kind, message, text)`. That list is what
the repair step in 2.3 is built from; everything else is for the reward.

A missing or unparseable XML is `crash`, not a zero. Scoring an absent result as 0/0 would make
a harness failure look like an honest no-op.

## 4. Patch application

`git apply`, run in the scratch tree, **without `--unsafe-paths`** — which is the whole reason
it is `git apply` and not a hand-rolled diff parser. Without that flag git refuses a patch whose
paths climb out of the tree. Tested directly: a diff targeting `../../escaped.txt` comes back
`patch_rejected` with git's own reason attached, and the file does not appear.

`--check` runs first so a rejection is clean and carries a diagnosable message, rather than a
half-applied tree.

The scratch copy uses `symlinks=True`, so links are copied as links rather than followed. The
default would turn a one-line symlink into an unbounded copy, and a link pointing outside the
repo into a file the patch could then edit "inside" it.

## 5. Two things this work found in the harness

Neither was what I was looking for, and both would have been quiet.

**The conftest guard was keyed to the directory, not the file.** Adding `test_verify.py` beside
the adversarial suite made `pytest src/pca/sandbox/tests/test_verify.py` fail collection: the
guarded *directory* was being collected and `test_fork_bomb_is_contained` — in the other file —
was not among the items. The guard was right about its rule and wrong about its scope. Narrowed
to the file, which opens the case where moving the test leaves the guard pointing at a path that
no longer holds it, so `_guarded_file_holds_the_anchor()` now checks that too. Verified by
renaming the test and confirming collection still fails.

**The container script never rebuilt a changed image.** `docker image inspect` succeeding was
the whole condition, so a Dockerfile edit took effect only when someone deleted the image by
hand. Survivable while the image held nothing but pytest; not survivable in 1.4, where
`/etc/pca-sandbox` and `git` both come from the Dockerfile and a stale image turns a correct
refusal into a baffling one. Now a content stamp of the Dockerfile and entrypoint is stored as
an image label and compared — content, not mtimes, because a checkout or a branch switch moves
mtimes without changing what gets built.

## 6. The weak mode is the one the loop uses, and that is not fixable here

`executor.run(checks=...)` keeps the expected value out of the sandbox and is strictly stronger
(`results/02-redesign.md` §4). **`verify()` cannot use it.** A project's pytest suite is
arbitrary Python; there is no expected value to hold back. So `verify()` runs the suite in the
scratch tree and trusts its report, and the containment that matters is the container around
the whole thing.

Written down because the failure mode is a reader six weeks from now concluding "PCA has check
mode" and assuming the loop is covered by it. It is not. Check mode is for training rewards,
where the task set is ours.

**RESIDUAL.** The repository's own tests are trusted to report honestly — they are the user's
code, and `protect_tests` keeps the model out of them. A genuinely hostile *repository* is
outside this threat model, and the junit XML it could rewrite is one more reason the container
is the boundary that counts.

## 7. State

```
ruff                          clean
scripts/check_env.py          6/6, seven sandbox invariants
pytest                        37 passed, 10 skipped   (macOS: 1 Linux-only, 9 need the container)
scripts/run-in-container.sh   47 ran, 47 passed, nothing failed, nothing skipped
run-corpus.py --mode snippet  10 exploits × 25 reps, every one zero
run-corpus.py --mode check    10 exploits × 25 reps, every one zero
```

Phase 1 is done. The execution boundary is built, measured against ten exploits it initially
lost six of, rebuilt, and wrapped in one call the loop can make.

Next is phase 2 — M0, the agent loop — and its first step is the model client, where the trap is
the worst shape there is: a wrong fill-in-the-middle template **does not crash**. It produces
fluent, plausible, wrong completions, and nothing in the stack says a word.
