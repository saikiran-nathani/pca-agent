# PCA — the build plan

**Written 2026-11-20**, against a live check of both machines the same day.
**Budget: 20 hours a week.** Every date below assumes that and says so, because a week count
with no hours-per-week assumption is decoration.

**Settled on 2026-11-20, and these are not reopened without a reason:**

| | |
|---|---|
| **The product** | An agent shell plus a model trained for it. Not a shell around a frontier API, and not a bare model |
| **Training phases** | **baseline → GRPO → merge.** SFT, RFT and DPO stay available and run only if a measurement earns them |
| **Order** | The execution boundary first, then the loop, then the measurement |
| **Inherited code** | ULTRON's sandbox and eval, **copied**. No dependency on that repository |
| **ULTRON** | Parked as-is. No further work goes into it |

---

## Why the order is what it is

You chose "the control first, then the harness and leaderboard". For an agent that resolves into
three phases rather than two, because **you cannot benchmark a loop that does not exist yet**:

```
   execution boundary  ──►  the loop (M0)  ──►  harness + leaderboard (M1)  ──►  train (M3)
   phase 1                  phase 2             phase 3                        phase 5
                                                      │
                                     phase 4 (rest of the audit, M2) ─┘ runs whenever
```

The boundary genuinely comes first, and for a stronger reason than it had in ULTRON. There, a
hole in the executor meant a reward pointing at a hole. **Here, the agent runs model-written code
on your laptop, against your repositories.** A containment bug is not a bad training signal, it
is a bad afternoon.

Phase 4 does not block anything. It is the publishable half of the audit and can land any time
after phase 1.

---

# Phase 0 — foundation · ~4 h · **largely done 2026-11-20**

| | | Status |
|---|---|---|
| 0.1 | Repo, `main` branch, `.gitignore`, `pyproject.toml` | ✅ done |
| 0.2 | venv at Python **3.12.13**, `uv`, dev and agent requirements | ✅ done |
| 0.3 | `src/pca/sandbox/` and `src/pca/eval/` copied, imports repointed | ✅ done |
| 0.4 | The adversarial suite runs: **22 passed, 1 skipped** (fork bomb, Linux only) | ✅ done |
| 0.5 | `pytest-timeout` installed, so `timeout = 120` is live rather than an unknown config option | ✅ done |
| 0.6 | `scripts/check_env.py` — the environment gate | **next** |

**0.6 is not a formality.** It asserts, on each machine: the Python version, that
`pytest-timeout` is actually loaded, that `torchvision`'s build tag matches `torch`'s on the TUF
(the `operator torchvision::nms does not exist` failure, caught before it costs an afternoon),
and **a minimum count of collected tests** — never a bare exit code. A gate pointed at an empty
directory must go red.

> **Phase 0 gate.** `python -m pytest -q` collects **at least 23 cases** and passes ·
> `scripts/check_env.py` fails when pointed at a deliberately broken venv.

---

# Phase 1 — the execution boundary · 16–20 h · Mac, in a container

**~1 week at 20 h/week. Target: 2026-12-01.**

#### 1.1 · The container harness — ✅ **done 2026-11-21**
`docker/Dockerfile`, `docker/entrypoint.sh`, `scripts/run-in-container.sh`.
`--network none` · `--read-only` · `--cap-drop ALL` · `--security-opt no-new-privileges` ·
tmpfs for `/tmp`, `/home/runner` and `/repo` · non-root uid 1000.

**Result: 23 passed, nothing skipped**, with `test_fork_bomb_is_contained` running for the first
time. On the host it remains 22 passed and 1 skipped, which is correct.

Three things this step settled, each of which would have been a wrong result rather than a
failure:

- **The source tree is copied to tmpfs, not bind-mounted.** `test_filesystem_escape_is_flagged_or_denied`
  asserts the escape *succeeds*, because Level 1 containment runs as the same uid and can only
  flag it. A read-only mount blocks the write and the test fails for the wrong reason; a
  read-write mount hands adversarial code the real repository. A throwaway copy is the only
  option honest about the executor and safe for the host.
- **`--memory` and `--pids-limit` are deliberately generous.** If Docker's limits fire before
  `RLIMIT_AS` and `RLIMIT_NPROC` do, every test still passes — having proved Docker contains a
  memory bomb rather than that the executor does. The container is defence in depth, never the
  thing under test.
- **The harness asserts the count, not the exit code.** Run with no arguments it requires
  **23 passed and zero skipped**. A run reporting 22 has silently skipped the one case the
  container exists to make possible, and "22 passed" reads exactly like success.

#### 1.2 · The exploit corpus — ✅ **done 2026-11-21**
Ten short Python "solutions" that must score **zero**:

`sys.exit(0)` before the asserts · `os._exit(0)` · an `AlwaysEqual` object whose `__eq__` always
returns `True` · patching `unittest` or the assert helpers · a `conftest.py` that marks everything
passed · reading the test file and hard-coding outputs · printing fake OK markers · overriding
builtins · exiting from an `atexit` handler · output flooding or a timeout tuned to the grader.

The executor reads its score from `ctl/report.json` and **never from the exit code**, which is
precisely the hole the first two walk through. That is a design decision worth being able to
explain.

**Run each under repetition, not once.** Isolation that holds for one unit test and not for a
loop running thousands of times is not isolation, and an agent repair loop is the second kind.

> **Result: six of ten were accepted, 25 times out of 25, on both machines.**
> Full record in `results/01-exploit-corpus.md`. Built as `src/pca/audit/`, which takes any
> executor rather than importing this one, so phase 4 can point it at someone else's sandbox
> without a rewrite.
>
> | | |
> |---|---|
> | Blocked | E01 `clean_exit` · E02 `hard_exit` · E09 `fake_markers` · E10 `output_flood` |
> | **Accepted** | **E03 `always_equal` · E04 `patch_compile` · E05 `forge_report` · E06 `steal_tests` · E07 `patch_runner` · E08 `atexit_forge`** |
>
> Three root causes, not six bugs: the scorer and the untrusted code share an interpreter (E04,
> E07) · `report_path` and the test sources are locals of the frame that calls the solution, so
> removing them from disk and argv does not remove them from reach (E05, E06, E08) · and
> `assert f(x) == y` is decided by `f(x).__eq__`, which the solution writes (E03).
>
> Nothing was flaky. Every verdict was 25/25 — deterministic properties of the design rather
> than a race, which is worse news and an easier fix.
>
> Two things the corpus settled that were not what it was looking for:
>
> - **`fs_read_outside_cwd` fires on `import ast`.** The audit hook flags any open outside the
>   work directory, which is every stdlib source file read to satisfy an import. A correct
>   solution that imports anything raises the same flag as one reading `/etc/passwd`. Not a
>   containment hole — a measurement defect, and 5.3 plans to spend that measurement as reward.
> - **E10 lands in the grader.** `RLIMIT_FSIZE` does not apply to a pipe and `_clip()` truncates
>   only after `communicate()` has read everything, so 16 MiB of output moved the *parent's*
>   peak RSS by 49–57 MB. The corpus's pass/fail axis called this case blocked; it is a resource
>   finding, and under a GRPO rollout it is every sample.

#### 1.3 · Fix anything accepted — ✅ **done 2026-11-21**
Any exploit that scores above zero blocks everything downstream. Do not move on with a known hole.

> **Result: all ten score zero, 25/25, in both test modes, on both machines.**
> Full record in `results/02-redesign.md`. `_runner.py` was rewritten; `executor.py` gained a
> report token, a `fraud` field and a `checks=` form. The inherited 23-case adversarial suite is
> unchanged and still passes.
>
> Three causes, three changes. **One fork per test**, so the process that scores never executes
> the solution (E04, E07). **Everything compiled before the first fork**, so there is no
> `compile` left to rebind (E04). **`_CTL.clear()` in the child and the report written last
> under a token**, so the path, the token and the other tests are out of reach and a forgery is
> overwritten or rejected (E05, E06, E08). **A flagged fraud scores zero** (E03) — X1.
>
> Both secondary findings closed too: the audit hook no longer flags an `import`, and a child's
> output is captured inside the child. E10's cost to the grader went from **+57 MB to +64 kB**.
>
> **The inherited suite went red six times during the rewrite, and every one was a real
> behaviour change.** `os._exit` skips the flush as well as atexit, so `r.stdout` came back
> empty. A child killed by a signal was being scored as a failed assertion, so `while True:
> pass` stopped reporting the containment that had just worked. A solution that will not import
> was reported as `fail` rather than `error`. That suite was inherited unexamined; this is the
> first evidence it is load-bearing.
>
> **The residual is measured, not asserted.** E11 `forge_own_result` finds the report pipe by
> fd, writes a forged pass and exits: **accepted 25/25 in snippet mode, blocked 25/25 in check
> mode.** That is the argument for X2 carrying both forms, as a number rather than a paragraph.
> `test_snippet_mode_still_accepts_the_residual` fails if it ever stops being true.
>
> Cost: a fork and a re-import per test — **~1.2 ms/test** for a trivial solution, ~3.2 ms with
> five stdlib imports, on a ~20 ms process-spawn floor. Import-time side effects now happen once
> per test, which is how the first cut of this made E10 worse before the capture landed.

#### 1.4 · The agent-facing API — 3–4 h
`verify(patch, repo, test_selector) -> Report`. One call the loop can make. It applies a patch to
a scratch copy, runs the project's own tests inside the container, and returns a structured
report. The agent never executes anything any other way.

> **Phase 1 gate.** Every exploit scores zero, under repetition, inside the container · the
> adversarial suite reports 23 there · `verify()` refuses to run outside a container and says so.
>
> Two of three, as of 2026-11-21: the corpus is zero in both modes on both machines, and the
> container reports **35 ran, 35 passed, nothing failed, nothing skipped** — 23 adversarial
> plus 12 corpus. 1.4 is what is left.

---

# Phase 2 — M0, the agent loop · 40–50 h · Mac

**~2–2.5 weeks. Target: 2026-12-15.**

The milestone that makes this a product you use rather than a plan you have.

#### 2.1 · The model client — 6–8 h
Ollama and llama.cpp over HTTP. Fill-in-the-middle through the runtime's **own** FIM mode —
Ollama's `suffix` parameter, llama.cpp's `/infill` — never by hand-assembling tokens.

> **The trap, and it is the worst shape there is.** A wrong FIM template **does not crash.** It
> produces fluent, plausible, wrong completions, and therefore a silently wrong everything.
> Nothing in the stack will tell you. Qwen2.5-Coder uses `<|fim_prefix|>…<|fim_suffix|>…<|fim_middle|>`;
> StarCoder and DeepSeek use their own. Assert the format **per model**, before any number is
> recorded.

Prove the stop rule in the same step: a completion must end at the end of the middle. Without it
models run on and repeat the suffix, which looks like a bad model and is a bad harness.

#### 2.2 · Repo context — 10–12 h
tree-sitter spans, not whole files. A 1.5B model with a 1,024-token budget cannot be handed a
412-line file, and the selection of *which* spans is a large part of what makes the loop work
where the rivals' loops do not.

#### 2.3 · The loop — 12–15 h
read → propose a patch → `verify()` → repair, with a bounded attempt count and a hard stop.
Every intermediate state is on disk, so a killed run is resumable and a transcript is reviewable.

#### 2.4 · Patch application — 6–8 h
Unified diff, applied atomically to a scratch copy, promoted only after `verify()` passes,
reverted otherwise. The agent never half-edits a file.

#### 2.5 · The CLI — 4–5 h
`pca fix`, `pca explain`, `pca complete`. The output in `README.md` is the contract.

#### 2.6 · Dogfood it — ongoing
Use it on this repository. The first real bug it fixes here is the first evidence that any of
this works.

> **Phase 2 gate.** `pca fix` closes a real failing test in a repository that is not this one,
> end to end, with the network interface **down**, and the transcript shows every attempt.

---

# Phase 3 — M1, harness and the head-to-head leaderboard · 50–70 h

**~2.5–3.5 weeks. Target: 2027-01-12.**

#### 3.1 · `src/pca/eval/harness.py` — 16–20 h
One command produces one number over 3 seeds with ± std, and writes it to a store. Everything
measured in this project is gated on this file existing. It did not exist in ULTRON either, and
that is exactly why nothing there was ever measured past the baseline.

#### 3.2 · Split the benchmark before scoring against it — 2 h here, impossible later
Every gate from here to M3 is "the number goes up", which makes the benchmark a **development
set**, not a held-out one. A benchmark you select against cannot be the benchmark you claim.

**Tune on one half. Report the other. Touch the held-out half once, at the very end.**
If you would rather not split it, say so in the leaderboard instead — development score, protected
guards, caveat on the headline. Doing neither leaves the number optimistic by an amount nobody
can quantify.

#### 3.3 · The board — 12–16 h
PCA against **Cline**, **OpenHands** and **goose**, each configured to the same Ollama endpoint
and the same local model. Then the ceiling row: the same benchmark with a frontier model behind
one of them. The ceiling goes in the published table.

#### 3.4 · Cost, latency, offline — 8–10 h
Tokens sent off the machine · p50 and p95 end to end on a ~1,024-token prompt budget · the whole
benchmark re-run with the network down. Three of these measurements want the TUF, for its latency
at both quantisations and its VRAM figure.

#### 3.5 · Back up what you have just created — 2 h, and do it before 3.1 finishes
From the moment the harness writes its first number, this repository holds measurements that are
not re-derivable without re-running the work. rsync to the other machine on a schedule and after
every gate — **and then restore it once and regenerate a report from the restored copy.**
A backup that has never been restored is a file, not a backup.

#### 3.6 · Publish `results/leaderboard.md` — 5–6 h

> **Phase 3 gate.** One command, one number, 3 seeds, ± std · the report regenerates from the
> store · the format gate fails when a model is missing from the board · the held-out half has
> never been scored.

---

# Phase 4 — M2, the rest of the audit · 25–40 h · blocks nothing

The publishable half. Phase 1 already hardened PCA's own executor; this turns the corpus on
everyone else's.

Targets: the human-eval execution guard (whose own README says it is *not* a security sandbox) ·
SandboxFusion through verl · the code reward functions in TRL and open-r1 · mlx-lm-lora's reward
example · verl's sandbox reward manager. **Every one runs in the phase 1 container. None runs on
the host.**

Ships as `results/audit.md` with the per-target share of exploits accepted, plus a small package
that runs the corpus against any executor it has never seen.

**Disclosure is yours to send.** Private first wherever a project has a security policy. Claude
drafts; you send. No AI attribution in any issue, PR or release note.

---

# Phase 5 — M3 and M4, train and release · 120–160 h · TUF throughout

**~6–8 weeks. Target: late February to late March 2027.**

Developed entirely at **0.5B**; the settled recipe then runs **once** at 1.5B.

#### 5.0 · Checkpoint, resume, and prove it by killing a run — 4–6 h · **before any long run**
The TUF sits at 1 GiB available with 7.7 G of swap in use before anything trains. The run will be
killed. Nothing in any plan has ever said what happens next.

A checkpoint interval chosen against the cost of losing that much work · a resume path that is
actually exercised · and the test: **kill a training process mid-run and confirm it resumes from
the last checkpoint.** Do it at 0.5B where it costs twenty minutes.

#### 5.1 · Data curation — 25–35 h · **Mac**, RAM-bound
Python problems with tests. Blank out syntax-tree spans with tree-sitter so the original code is
the reference and the tests are the verifier.

> **Trap: contamination.** The benchmark's algorithmic split and the training corpora share
> sources. Decontaminate by problem id **or** URL **and** by n-gram overlap. If the training data
> contains the test, splitting the benchmark in 3.2 does not save you.
>
> **Trap: RAM.** `streaming=True`, always.
>
> **Licences are a release blocker.** Check each corpus, and write down the distinction that looks
> identical in a results table: **evaluating a model is not redistributing it.**

#### 5.2 · Set the stop indicators before the first step — 1 h
Four, each with a window and a threshold, written **before** step one. "Stop if the empty rate
climbs" is not a rule; it cannot fire.

| Indicator | Why |
|---|---|
| **empty rate** | a zero reward for empty output can collapse into "always empty" |
| **mean completion length** | length hacking is real and arrives quietly |
| **reward mean** | direction of travel |
| **`grad_norm`** | **leading** — it climbs before the loss breaks |

A leading indicator you have identified and do not alert on is wasted.

#### 5.3 · GRPO — 30–40 h · **TUF**
**Reward:** fraction of tests passed in the phase-1-hardened executor · a tree-sitter parse of the
assembled result · a length penalty · abstention scored positive for correct, negative for wrong,
zero for empty.

**Memory, all of it aimed at the thing that binds** — the fp32 logits tensor: log-probs over
completion tokens only, chunked vocabulary projection, prompts ≤ 1,024, completions ≤ 64, the
group generated one at a time.

#### 5.4 · Record every promotion — 10 min per gate
No hosted runner has a GPU, so every training gate is a human reading a number. That is the only
option on owned hardware, and it should still be a control with a shape: run id, every score,
seed count, and the commit the recipe was at. Otherwise "the merged model is better" has no
provenance six weeks later.

#### 5.5 · Merge — 8–10 h · **Mac** · 5.6 · The single 1.5B run — 20–30 h

#### 5.7 · Serve and release — M4 — 15–20 h
GGUF at Q8_0 and Q4_K_M · an Ollama Modelfile carrying the FIM template · latency on both
machines · a model card with the before-and-after table · the recipe.

- **The template version goes in the tag** — `pca-tab:1.5b-fim1`. A template change is a major
  version and old tags stay resolvable. Changing it silently degrades every installed config with
  no error, and there is no way to reach people who already pulled
- **Install-test on a machine you do not own.** Both of yours have `/data` laid out and the
  environment exported, and one of them built the artifact. A clean container with nothing but
  Ollama is the cheapest honest version of a one-command claim
- **Publishing is one-way.** You approve it

> **Phase 5 gate — and it is not "the model exists".** The model card carries a measured
> before-and-after table · the one-command install works somewhere that is not your hardware ·
> the held-out half is scored **once**, here, and reported whatever it says.

---

# When to stop

Written now, because a stop condition written before starting is the only kind that works.

**Stop phase 5 and ship M0, M1 and M2 instead if:**
- two consecutive training phases fail their gate after the data gate has passed, **or**
- 1.5B GRPO is measured infeasible on both machines **and** the 0.5B result is below the floor.

**Stop the whole project and say why, publicly, if:**
- M1 shows a small local model cannot drive a useful loop at all. That is a real result, it is
  worth publishing, and it is not a failure — it is the question M1 exists to answer.

---

# Risks, with a trigger on each

A risk with no stop trigger is a paragraph.

| Risk | Likelihood | Impact | Trigger that forces a decision |
|---|---|---|---|
| A 1.5B model cannot drive the loop | **medium** | kills M3, not M0-M2 | M1 shows pass rate below the floor on every local model tested |
| The TUF's kernel kills long runs | **high** | lost work, repeatedly | the first killed run with no working resume → 5.0 is not done |
| Rivals close the gap | medium | the pitch narrows | a rival ships a loop designed for small models → re-aim at the trained component |
| Benchmark contamination | medium | the headline is wrong | decontamination report shows any overlap → rebuild the split |
| Scope balloon in phase 3 | **high** | M1 never ships | 4 agents × 2 models × 2 quantisations is already 16 cells. Freeze the board before running it |
| The agent damages a real repository | low | **severe** | any write outside the scratch copy → stop, fix, re-audit |

---

# The arithmetic

| Phase | Hours |
|---|---|
| 0 — foundation | ~4 (largely done) |
| 1 — execution boundary | 16–20 |
| 2 — M0, the loop | 40–50 |
| 3 — M1, harness and leaderboard | 50–70 |
| 4 — M2, rest of the audit | 25–40 |
| 5 — M3 and M4, train and release | 120–160 |
| **Total** | **255–344 h** |

**At 20 h/week — the budget you set — that is 13 to 18 weeks.**

| Milestone | Cumulative | Target, at 20 h/week from 2026-11-20 |
|---|---|---|
| Phase 1 done | 20–24 h | **2026-12-01** |
| **M0** — the loop works | 60–74 h | **2026-12-15** |
| **M1** — leaderboard published | 110–144 h | **2027-01-12** |
| **M2** — audit published | 135–184 h | **2027-02-02** |
| **M3 / M4** — model trained and released | 255–344 h | **2027-02-23 → 2027-03-23** |

20 hours a week is a half-time job on top of coursework and the co-op hunt. If a week comes in at
10, the dates move by the same arithmetic rather than the plan quietly slipping — recompute and
write the new date down.
