# PCA — open decisions

**Opened 2026-11-20.** Write your answer on the `SK:` line. Each one says what it blocks, so
nothing here stops work that does not depend on it.

---

## Settled on 2026-11-20

| | Decision | Answer |
|---|---|---|
| **S1** | What PCA is | **An agent shell plus a model trained for it.** Not a shell around a frontier API; not a bare model |
| **S2** | Training phases | **baseline → GRPO → merge.** SFT, RFT and DPO run only if a measurement earns them |
| **S3** | Build order | Execution boundary → the loop → harness and leaderboard → train |
| **S4** | Inherited code | ULTRON's `sandbox` and `eval`, **copied**. No dependency on that repo |
| **S5** | ULTRON | Parked as-is |
| **S6** | Budget | **20 h/week**, which puts M0 at 2026-12-15 and M3 in late February to late March 2027 |
| **S7** | Repo | Local, with a **public** GitHub remote |
| **S8** | Attribution | **No AI co-authorship anywhere.** No `Co-Authored-By`, no generated-with footer, in any commit, PR, issue or release note |

---

# ~~Blocking the first push~~ — answered 2026-11-21

## P1 — What is the repository called on GitHub?

`PCA` is three letters that already mean principal component analysis to most readers, which is
unhelpful for a repository whose job is to be found.

**Recommendation: `pca-agent`**, with the README's first line carrying the full name. Keeps the
short name you chose, and is not ambiguous in a search result.

`SK:` **pca-agent.** Answered 2026-11-21.

---

## P2 — Which licence?

A public repository with no `LICENSE` file is **all rights reserved**: legally, nobody may use it.
That defeats the point of publishing.

**Recommendation: Apache-2.0.** It is the default across this tooling ecosystem, and unlike MIT
it carries an explicit patent grant — which matters for anything that might ship a trained model.

`SK:` **Apache-2.0.** Answered 2026-11-21. `LICENSE` is the canonical text, verbatim and
unmodified — verified byte-identical (sha256 `c71d239d…`) against two independent copies shipped
by `peft` and `accelerate`, rather than retyped. Attribution is in `NOTICE`, per the Apache
convention of leaving `LICENSE` untouched. The copyright line reads **Sai Kiran**, which is what
`git config user.name` publishes commits under; change that one line if you want the full name.

---

# Blocking the TUF, whenever you get to it

## T1 — Reboot, and go headless in the same reboot?

Measured on the TUF today: `gnome-shell` **5.5 GB**, `pycharm` 2.5 GB, `gnome-control-center`
0.8 GB, chrome ~2 GB. Roughly **9 of 14 GiB renders a desktop for a box you drive over SSH**.
Swap is 16 G with 7.7 G in use, and 1 GiB of RAM is free before anything trains.
`/var/run/reboot-required` has been set since 2026-11-14.

**Recommendation: both, in one go.** `sudo systemctl set-default multi-user.target`, and
`sudo systemctl isolate graphical.target` brings the desktop back when you want it.

**Order matters: confirm SSH works after the reboot *before* setting the default**, since the GUI
is the fallback if SSH breaks.

**Note what this is and is not for.** It restores `nvidia-smi` and reclaims RAM. It does **not**
unblock CUDA — CUDA works today, measured. Three planning documents in ULTRON say otherwise and
are wrong.

**Blocks:** nothing until phase 5, except GPU telemetry during phase 3's three TUF measurements.

`SK:`

---

# Blocking phase 2 (the loop), ~2 weeks out

## L1 — Which local model does the loop drive by default?

The loop and the completion want **different models**, and this is easy to get wrong:

- **The loop** needs instruction following across turns → an **instruct** model
- **Completion** needs fill-in-the-middle → a **base** model. An instruct model is measurably
  worse at FIM, and instruction tuning for editing has been shown to drop FIM scores outright

**Recommendation: two, and say so plainly.** `Qwen2.5-Coder-1.5B-Instruct` for the loop,
`Qwen2.5-Coder-1.5B` base for completion. Both fit. The trained model in phase 5 replaces the
second one first, because that is where the measurement is cleanest.

`SK:`

---

# Blocking phase 3 (the leaderboard), ~6 weeks out

## B1 — Which task set?

SWE-bench is the obvious answer and the wrong one: its tasks are far out of reach for a 1.5B
model, so every agent scores near zero and the comparison measures nothing.

**Recommendation: two sets, both bounded.**
- **Repair** — failing-test-to-patch tasks, where the test is the verifier. Generated from real
  repositories, held out properly per phase 3.2
- **Completion** — SAFIM for fill-in-the-middle, with published baselines to re-measure locally

Re-measure every published number locally. Scores from different harnesses are not comparable,
and quoting one as though it were is a mistake this project should be too careful to make.

`SK:`

## B2 — How many cells before the board is frozen?

4 agents × 2 models × 2 quantisations is 16 runs, and the plan's own risk table rates scope
balloon here as **high**. Decide the shape of the table before running any of it.

**Recommendation: freeze at 4 agents × 1 local model × 1 quantisation, plus one frontier ceiling
row.** Widen only after the narrow board is published.

`SK:`

---

# Blocking phase 5 (training), ~12 weeks out

## TR1 — The `trl` version, pinned on both machines

On 2026-11-20 the Mac had **1.10.0** and the TUF **0.24.0** — a major version apart on the library
that owns loss masking. `requirements/cuda.txt` deliberately leaves it unpinned, with a comment
saying why: pinning a version months before any training code exists writes a number that nothing
checks.

**Decide it when `src/pca/train/` is written**, install the same version on both, and have
`scripts/check_env.py` assert they match.

`SK:`

## TR2 — The MLX go/no-go threshold, and it must be written before measuring

1.5B GRPO on 4 GB is genuinely uncertain. The plan times MLX steps on the Mac and decides from
that, which is an intention, not a threshold — **once you see a number it becomes the number you
accept.**

**Yours to set:** pick the wall clock you will accept for one 1.5B run, divide by the steps that
run needs, and write the it/s here **now**. For scale: at a published `mlx-lm-lora` rate of
0.02 it/s on a 0.6B model on an M4 Pro, 500 steps is about seven hours, and 1.5B is slower.

`SK:`

## TR3 — The model tag scheme

**Recommendation: `pca-tab:1.5b-fim1`.** The template version is in the tag, a template change is
a major version, and old tags stay resolvable. Before the first publish this costs a sentence;
afterwards it cannot be fixed, because a template change silently degrades every installed config
with no error and no way to reach whoever already pulled.

`SK:`

---

# Blocking phase 4 (the audit), whenever it runs

## A1 — Upstream disclosure

Phase 4 audits popular execution and reward paths and will probably find real holes. Filing an
issue is outward-facing.

**Recommendation: Claude drafts, you send.** Private disclosure first wherever the project has a
security policy. No AI attribution in any issue, PR or release note.

`SK:`
