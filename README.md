# PCA — Personal Coding Agent

**A coding agent built for a model small enough to run on your own machine.**

Status: **day one.** The execution sandbox is here and tested. Nothing else is built yet.
Every number below that is not marked *measured* is a target, and stays blank until it is measured.

---

## The product this is based on

**Cursor's agent mode** and **Claude Code** do multi-step coding: read the repo, plan a change,
edit files, run the tests, fix what broke. They work, people pay for them, and both of them send
your code to someone else's GPU and bill you per token.

## The open-source alternatives people use instead

Measured 2026-11-20 against the GitHub API, not quoted from memory.

| Project | Stars | Last push | What it is |
|---|---|---|---|
| **OpenHands** | 93,932 | 2026-11-21 | Full agent platform with a sandboxed runtime |
| **Cline** | 71,795 | 2026-11-21 | VS Code agent extension |
| **goose** (Block) | 56,838 | 2026-11-20 | Local-first CLI agent, MCP-native |
| **Aider** | 50,202 | **2026-05-22** | Terminal pair programmer. Six months without a push |
| **Continue** | 36,532 | 2026-11-20 | Autocomplete and chat inside the editor |
| **SWE-agent** | 20,840 | 2026-11-16 | Research agent, built for SWE-bench |
| ~~Roo Code~~ | 24,237 | **archived 2026-05-15** | Archived. Not a live rival, and not listed as one |

**What all of them have in common.** The loop is a shell around a frontier API model. Every one
advertises local-model support, and what that means in practice is *you may configure one* — not
*it works*. Point any of these at a 1.5B model on a laptop and the loop falls apart, because the
loop was written assuming a model that can hold a long tool-use contract in its head across a
dozen turns.

**That gap is this project.** Not a better shell around Claude. A loop designed around the
limitation, plus a model trained for the steps the loop actually asks for.

---

## What PCA outputs

```
$ pca fix "tests/test_parser.py::test_nested_blocks is failing"

  read      src/parser.py — 412 lines, 3 spans around the failure
  propose   patch, 11 lines, src/parser.py:188
  verify    pytest tests/test_parser.py -q  →  14 passed     [sandboxed · no network]
  apply     src/parser.py

  2 attempts · 31s · 0 tokens sent off this machine
```

And in the editor, as you type, it fills in the code between what is above and below your
cursor in under half a second, and stays quiet when it is unsure.

Installed with one command. Runs with the network off.

---

## Where it wins, and how that gets measured

Head to head, same tasks, same hardware, **same local model**, their loop against this one.

| Axis | How it is measured | Measured against |
|---|---|---|
| **Task pass rate on a small local model** | One benchmark, one model, each agent configured to the same Ollama endpoint | Cline · OpenHands · goose |
| **Cost per task** | Tokens sent off the machine. PCA's is **zero by construction**, so the comparison is against what the rivals actually spend | Any of them on a frontier API |
| **Inner-loop latency** | p95 end to end on one completion, ~1,024-token prompt budget, through the shipped quantisation | Continue's default local setup |
| **Works offline** | Run the entire benchmark with the network interface down | All of them |

**The ceiling goes in the same table.** The same benchmark with a frontier model behind Cline is
the number PCA does not beat, and the leaderboard reports it anyway. A comparison that hides its
own ceiling is marketing.

The claim is narrower than "better than Cursor", and it is defensible: **on hardware you own,
with no network, this loop and this model beat the alternatives configured the same way.**

---

## Milestones, and the résumé line each one earns

Numbers in brackets stay blank until something measures them.

| | Milestone | Résumé line it earns |
|---|---|---|
| **M0** | The loop works end to end on a local model | Built a local-first coding agent that reads a repository, proposes a patch, and verifies it against the project's own tests inside a sandbox with no network access |
| **M1** | Harness, baselines, leaderboard | Measured [K] open coding agents driving the same local model on the same tasks, published as an open leaderboard reporting cost and p95 latency beside pass rate |
| **M2** | Reward and execution audit | Audited [N] open code-execution paths against [M] known exploits and found [X] that accepted at least one |
| **M3** | The trained model | Trained a [1.5B] model with GRPO on a 4 GB laptop GPU, raising [benchmark] from [a] to [b] with no latency regression |
| **M4** | Release | Released it as a one-command install: [D] pulls in the first month |

---

## What would make this fail

Written now, while it is cheap to say.

- **A 1.5B model may simply be unable to drive a useful agent loop.** M1 is designed to find that
  out before M3 spends 120 hours on it. If the answer is no, the honest result is the leaderboard
  showing where the floor is, and the project ships M1 and M2 and says so.
- **The rivals are not standing still.** goose and Cline push daily. Any advantage here has to be
  structural — the loop and the trained model — not a feature they can add in a week.
- **Agents run model-written code on your machine.** That is why the sandbox came first rather
  than last, and why M2 exists at all.

---

## Repository

| | |
|---|---|
| `src/pca/sandbox/` | Execution boundary. Score is read from a report file, **never** from the exit code. 23 adversarial cases. Carried over from ULTRON, proven there |
| `src/pca/eval/` | Answer extraction and pass@k. Carried over from ULTRON |
| `CLAUDE.md` | What the two machines are, what fits on them, and what not to suggest |
| `PLAN.md` | The phased build, with hours |
| `DECISIONS.md` | What is still open, and what each one blocks |
| `docker/` | The Linux container the sandbox is audited in |
| `LICENSE` · `NOTICE` | Apache-2.0, verbatim; attribution in `NOTICE` |

Run the gates:

```bash
.venv/bin/python scripts/check_env.py
```

```bash
.venv/bin/python -m pytest -q
```

```bash
./scripts/run-in-container.sh
```

The first reports how many checks it ran and fails below the count it declares. The second is
22 passed and 1 skipped on macOS. The third runs the same suite inside Linux and requires
**23 passed with nothing skipped** — the skipped case needs a `/proc` that macOS does not have,
and a container run reporting 22 has quietly skipped the only test it exists to enable.

ULTRON, the post-training curriculum this borrows its sandbox from, is parked as-is at
`../ULTRON` and is not a dependency.
