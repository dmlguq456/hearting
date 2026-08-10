---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: autopilot-lab
description: "Use when training setup, checkpoint evaluation, ablation, metric computation, or other new empirical output is required. Not for ordinary production-code changes that create no new experiment or evaluation result."
argument-hint: "<task description> [--mode setup|eval|auto] [--parent <slug>] [--ref <similar-model-path>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--report] [--from spec|scaffold|run|eval|summary]"
metadata:
  group: entry
  fam: code
  invocation_class: entry-router
  modes: ["setup", "eval"]
  blurb: "Rapid experiment prototyping around training setup and checkpoint evaluation/analysis."
  use_when: "Use when training setup, checkpoint evaluation, ablation, metric computation, or other new empirical output is required."
  not_for: "Not for ordinary production-code changes that create no new experiment or evaluation result."
---

# autopilot-lab

This is a compact pre-approval entry router. Its manifest-owned frontmatter is
the authoritative discovery metadata; it intentionally contains no execution
procedure.

## Pre-approval boundary

Use only this router, `harness-manifest.json`, and `core/WORKFLOW.md §0.2` to
propose a route. Present the one-time confirmation in `core/WORKFLOW.md §0.4`
unless the same route and scope are already approved. Do not load references
before approval.

## Post-approval owner contract

After approval, direct/quick sessions and the `standard+` dispatch-depth-1 owner load
`capabilities/autopilot-lab.md`, then use the Reference Index below. Assigned
stage workers load only their assigned stage contracts.

## Reference Index

| File | Load when | Obligation |
|---|---|---|
| [`references/owner-execution.md`](references/owner-execution.md) | After approval, by the selected direct/quick session or dispatch-depth-1 owner | Read the complete execution procedure before material work. This is the router's only post-approval reference edge. |

## Guard pointer

Follow the portable artifact, worktree, role, and verification guards in the
selected owner contract. Before the first durable capability artifact, compile
and bind the checked route even for direct intensity; direct is a compiled
inline node, not route absence. A restriction on native subagents/agents is
surface-local and never selects direct execution. Runtime projections must
report unsupported mechanics and must not claim physical instruction masking,
token, billing, or cost savings without verified evidence.
