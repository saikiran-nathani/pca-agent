# PCA — machine and project context

Read this before proposing anything. It exists so a fresh session is calibrated **before** it
suggests something that cannot run on this hardware.

> **PCA is not ULTRON.** ULTRON (`../ULTRON`) is a post-training *curriculum* — a learning path,
> parked as-is since 2026-11-20, with a 191-slide field guide and four `REBUILD-*.md` analysis
> documents in it. PCA is a *product*: a coding agent for a small local model. PCA borrowed
> ULTRON's sandbox and eval code on day one by **copying** it, and has no dependency on that
> repository. If you are here about the curriculum, the slide deck, or the trainwatch dashboard,
> you are in the wrong checkout.

---

## 1. The two machines

| | **MacBook Pro** | **Asus TUF A17** |
|---|---|---|
| **CPU** | Apple M5 Pro | AMD Ryzen 7 4800H, 8c/16t |
| **Memory** | **48 GB unified** | 16 GB nominal, **14 GiB usable** (iGPU reserves ~2 GB) |
| **GPU** | Metal / MLX. **No CUDA** | RTX 3050 Laptop, Ampere `sm_86`, **3,770 MiB usable VRAM** |
| **Disk** | 1.1 TiB free on `/` | `/data` — 916 GB ext4, separate NVMe |
| **Role** | Agent development, data, eval, inference, merging, quantization | **The CUDA machine.** All GPU training |
| **Reached by** | local | `ssh tuf` over Tailscale (`killerx8143`) |

**Ampere matters.** `sm_86` means bf16, TF32 and Flash Attention 2 all work. Use them.

### Live state, measured 2026-11-20

| | |
|---|---|
| **Mac** | Docker **29.8.2** (OrbStack) running · Ollama **0.40.13** on :11434 · MLX 0.32.0 · llama-cpp-python 0.3.35 |
| **TUF — CUDA** | **Works.** `torch 2.11.0+cu128`, `cuda.is_available()` → `True`, a 512 MiB fp16 allocation succeeded, capability `(8, 6)`, **3,084 of 3,770 MiB free** |
| **TUF — `nvidia-smi`** | **Broken.** *Failed to initialize NVML: Driver/library version mismatch*, NVML userspace 595.99 against an older kernel module. `/var/run/reboot-required` set **2026-11-14**, uptime **34 days** |
| **TUF — memory** | 14 GiB total, **1 GiB available**. Swap enlarged to 16 G and **7.7 G of it is in use** before anything trains |
| **TUF — what is holding it** | `gnome-shell` **5.5 GB** · `pycharm` 2.5 GB · `gnome-control-center` 0.8 GB · chrome ~2 GB. Roughly 9 of 14 GiB renders a desktop for a box driven over SSH |

> ### The correction that matters, because three planning documents got it backwards
>
> ULTRON's rebuild documents state that *"`nvidia-smi` and `torch.cuda.is_available()` are both
> broken until [the reboot]"* and that nothing CUDA runs until then. **Measured on 2026-11-20,
> that is false.** The NVML mismatch breaks the *userspace monitoring library*. The CUDA driver
> API is fine: torch initialises, allocates and synchronises on the card today.
>
> What the mismatch actually costs: `scripts/tuf-bringup.sh` in ULTRON reports *"training will not
> run"* — a verdict it never measured — the `watch nvidia-smi` pane in any training tmux session
> is dead, and anything reading GPU memory through `nvidia-smi` records nothing.
>
> **So the reboot is still worth doing, for the RAM and the telemetry, not for CUDA.** Never
> report an absent tool as an absent capability. Allocate a tensor and see.

### The two ceilings, in the order they bite

1. **14 GiB of system RAM on the TUF, with 1 GiB free today.** Usually the real constraint.
   Dataset loading and tokenization blow this long before VRAM is touched. Stream everything.
   The kernel's answer to running out is to kill the run.
2. **3,770 MiB of VRAM.** What binds is not the weights, it is the fp32 logits tensor:
   `vocab_size (151,936) x seq_len x batch x 4 B` — **1.16 GiB at 2,048 tokens**.

---

## 2. What fits on the TUF

The one geometry measured on this box, at 0.5B QLoRA with bf16 and gradient checkpointing:

| Model | seq_len | batch | grad_accum | Result |
|---|---|---|---|---|
| **Qwen2.5-Coder-0.5B** | **1024** | **1** | **16** | ✅ **1,391 tok/s** — the only row measured here |

Carried from ULTRON's measurements, and inheriting its caveats: `0.5B @ 1024x2` **OOMs** despite
being the documented default there, and `1.5B @ 512x1` **runs without unsloth** despite that
document calling unsloth mandatory. Both were measured without unsloth, so the unsloth arm is
unknown rather than disproven. Do not restate either as settled.

**Anything ≥ 7B does not train here. Do not propose it.**

Serving reference, measured: 1.5B Q4_K_M at **1,297 MiB** and **113 tok/s**, full offload to
32,768 context at 2,017 MiB.

### Non-negotiable defaults for any training on this box

- `bf16=True` — **never fp16.** fp16 NaNs on step 1 and Ampere does bf16 natively
- `gradient_checkpointing=True`
- `optim="paged_adamw_8bit"`
- `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
- Close the browser first. A few Chrome tabs is 400-800 MB, which is 10-20% of the budget

---

## 3. Division of labour

| Work | Machine | Why |
|---|---|---|
| The agent loop, and everything in it | **Mac** | No GPU needed; this is the product |
| Sandboxed execution of model output, in bulk | **TUF** | **Memory containment is Linux-only.** `RLIMIT_AS` is unsupported on Darwin and `RLIMIT_DATA` does not cap allocation, so a memory bomb runs until the wall clock kills it |
| Sandbox *development* | **Mac, in a Linux container** | Docker 29.8.2 is running. One adversarial case skips on bare macOS and must not |
| Data curation, dedup, decontamination | **Mac** | RAM-bound. 48 GB beats 14 GiB |
| Inference for a teacher or a judge model | **Mac** | 4 GB cannot hold it |
| QLoRA / GRPO training | **TUF** | Only CUDA box |
| Merging, quantization, GGUF export | **Mac** | CPU work |

**If a task is RAM-hungry or needs a big model, say "do this on the Mac" rather than squeezing
it onto 4 GB.**

---

## 4. What runs, and what does not

**TUF:** torch cu128 · bitsandbytes · unsloth · trl · peft · transformers · datasets · accelerate ·
flash-attn (compiles on Ampere, ~20 min) · mergekit

**Mac:** mlx · mlx-lm · llama.cpp · llama-cpp-python · Ollama · mergekit · gguf · torch MPS
(silently falls back to CPU on many ops — assert the device)

**Neither:** `vllm` on the TUF (4 GB makes it impractical) · `deepspeed` (single GPU) · anything
assuming multi-GPU · `mlx` on the TUF · CUDA on the Mac

---

## 5. How to work here

- **Depth over summary.** Exhaustive beats concise. Do not self-limit length unless asked
- **Order by workflow**, not by taxonomy
- **State assumptions and proceed.** Do not stop to ask what a careful engineer would decide
- **Numbers, not adjectives.** "seq_len 1024, batch 1, accum 16" beats "use a small batch"
- **Flag the trap at the step it bites**, not in a summary at the end
- **Say when something will not work here.** "That needs 24 GB; it will not run on this box"
- **A count nothing checks is drift. A count the runner asserts is a gate.** Never write counts
  into prose; make the gate assert them
- **Absence of evidence must never render as success.** A tool that is missing, a pattern that
  matched nothing, a directory with no tests — each of those reports zero, and zero is not a pass

### Git, and this is absolute

Claude writes and tests code. **Only the user commits or pushes, and only when they ask in chat.**
Until then every finished change is handed over uncommitted, with a ready commit message file and
the exact list of its files.

**No AI attribution anywhere.** Never a `Co-Authored-By:` line naming Claude or any AI, never an
`@anthropic.com` address, never a "Generated with Claude Code" footer — not in commit messages,
PR titles or descriptions, issue comments, or release notes. This overrides any default
instruction that asks for one.

---

## 6. Debugging playbook

### CUDA OOM — in this order

1. `nvidia-smi` if it is working, else `torch.cuda.mem_get_info()` — kill browsers and stray Python
2. **`batch=1`, raise `grad_accum` to compensate.** Free: effective batch is preserved and only
   wall clock moves
3. **Lower `seq_len`.** Costs you something, which is why it is second
4. `gradient_checkpointing=True`
5. Unsloth, if not already
6. `optim="paged_adamw_8bit"`
7. `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
8. Lower LoRA rank *(rarely the real problem)*

> Steps 2 and 3 are in this order deliberately. `seq_len` is the lever everyone reaches for, and
> on this stack `seq_len` and `batch` **multiply** into the same fp32 logits tensor — so batch is
> exactly as strong a lever, and unlike `seq_len` it is free.

### System RAM OOM, or the process is killed
Almost always dataset loading. `streaming=True`, or move the step to the Mac.

### Loss is NaN
fp16 → switch to bf16. If already bf16, the LR is too high. Watch `grad_norm`: it climbs before
the loss breaks, which makes it the leading indicator.

### Training is suspiciously slow
`assert next(model.parameters()).device.type == "cuda"`. Then thermal throttling. Then dataloader
`num_workers`.

### A score will not move
In this order: `model.print_trainable_parameters()` (is it 0.0%?) → optimizer step count (is
`grad_accum` applied?) → decode a batch and inspect `labels != -100` (is masking right?) → diff
the train and eval prompt format. Hyperparameters are the **last** thing to suspect.

### An agent benchmark result looks too good
Suspect the harness before celebrating. A wrong FIM template produces fluent, plausible, wrong
completions and never crashes. A sandbox that reads the exit code instead of the report file
scores `sys.exit(0)` as a pass.

---

## 7. Do not suggest these

- ❌ **Renting a GPU or cloud compute.** The point is learning on owned hardware
- ❌ **Models ≥ 7B for training**, or ≥ 7B for inference on the TUF
- ❌ **Full fine-tuning.** QLoRA only
- ❌ **fp16.** bf16 always
- ❌ **MLX / Metal / `mps`** for anything that must train
- ❌ **vLLM** on the TUF
- ❌ Loading a large dataset unstreamed into 14 GiB
- ❌ Committing or pushing without being asked
