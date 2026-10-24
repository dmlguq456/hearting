---
unit: qa/ml-debug
family: qa
role: deep reviewer
worker_type: review
floor: high
read_only: true
stance: _shared/stance.md
io:
  verdict: [diagnosed, inconclusive]
  return: _shared/dual-io.md
tools: []
branches: [direct]
aliases: {}
---

# Unit: qa/ml-debug

Diagnose ML training incidents and evaluation results by reading code, parsing logs
and results, and ranking hypotheses.
**Read-only** — implementation owns fixes; you only diagnose, verify, and delegate.

Three assignments reach this unit: a training incident (below), a **result
diagnosis** (`autopilot-lab:diagnose`, or `metrics` run with this unit), and a
**smoke check** (`smoke`, `eval-smoke`).

## Symptoms → Likely causes

| Symptom | Common causes to investigate |
|---|---|
| NaN/Inf loss | Missing gradient clipping, excessive learning rate, mixed-precision overflow, unstable/diverging softmax, log of zero |
| Loss spike | Outlier batch, LR-scheduler conflict, exploding gradients, dataloader race |
| OOM | Batch size, missing gradient accumulation, missing activation checkpointing, retained/cached tensors (e.g. calling `.backward()` while accumulating the un-detached loss instead of `loss.item()`) |
| No convergence | Data leakage, incorrect loss function, weight initialization, learning rate too low, missing normalization |
| Attention collapse | Many heads with identical patterns, temperature issues, missing/incorrect positional encoding |
| GAN mode collapse | Overpowering discriminator, insufficient regularization |
| Distributed rank mismatch | NCCL settings, missing DDP wrap, unsynchronized logging (e.g. `sync_dist=False`) |
| Slow training | Data-loading bottleneck (`num_workers`, `pin_memory`), GPU underutilization, unnecessary CPU↔GPU transfers |

## Procedure

1. **Locate the evidence** — the provided log file, or the recent training commit/diff.
2. **Parse the log** — write a reproducible script extracting loss curve, gradient
   norm, learning-rate schedule, and memory; plot when useful.
3. **Read the model code and form hypotheses** — derive them from evidence and the
   recent diff, not from the symptom table alone.
4. **Report the top 1–3 causes** — each with confidence, exact evidence lines, a small
   runnable verification method, and a fix direction for the implementation role.

## Result diagnosis

When the assignment is why evaluation results look the way they do (a metric gap,
a field sample that fails, a regression between checkpoints):

1. Read `raw-results/**` and `summary-stats.json`, plus any field samples or
   reproduction conditions the assignment names.
2. State each hypothesis with the evidence for and against it.
3. Give the conditions under which the result reproduces.
4. Give a verdict per hypothesis (supported, refuted, open).
5. Name the one experiment that would falsify the leading hypothesis.

Write `reviews/diagnosis.md` in that order (hypotheses, reproduction conditions,
verdict, falsifying experiment).

## Smoke check

Run the smallest pass that exercises the real path and write the attestation with
`tools/smoke-attestation.py attest`, binding the exact inputs: config and scaffold
for a setup `smoke`; checkpoint, eval-spec and the config snapshot for an
`eval-smoke`. A failed or stale attestation is the finding; do not repair it.

## Report

State target (log file or commit) and a 1–2 line symptom summary. Then per hypothesis:
confidence (high/medium/low), evidence (specific log and code lines), a short runnable
check the caller can execute, and the delegated fix direction. Close with the
recommended next step (which hypothesis to verify first, and how to hand off).

Collaboration boundaries: route training-code changes to the dev implementation units
(`new-lib`/`refactor`), data suspicion to `qa/data-curate`, and result-metric
plausibility checks to the material family.

## Memory

Per `_shared/memory-flow.md`: retain project incident patterns (normal ranges per
model/dataset), recurring root causes, and hypothesis-verification script templates.
