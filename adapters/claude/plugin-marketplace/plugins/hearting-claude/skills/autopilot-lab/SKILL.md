---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
description: "Use when experiments need setup, evaluation/analysis, failure reproduction, or result reporting, including fixed results without new measurements. Owns experiments; default preset. Not for reusable evaluation code, independent documents, or fixed-data document layout/caption edits."
argument-hint: "<task description> [--mode setup|eval|auto] [--parent <slug>] [--ref <similar-model-path>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--report] [--from spec|scaffold|run|eval|summary]"
metadata:
  group: entry
  fam: code
  invocation_class: entry-router
  modes: ["setup", "eval"]
  blurb: "Set up experiments, evaluate, and report results, including fixed results."
  use_when: "Use when experiments need setup, evaluation/analysis, failure reproduction, or result reporting, including fixed results without new measurements. Owns experiments; default preset."
  not_for: "Not for reusable evaluation code, independent documents, or fixed-data document layout/caption edits."
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

After approval, read the portable procedure set:

## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/autopilot-lab/references/auto-load-context.md`
- `capabilities/autopilot-lab/references/config-provenance.md`
- `capabilities/autopilot-lab/references/data-contract.md`
- `capabilities/autopilot-lab/references/eval-procedure.md`
- `capabilities/autopilot-lab/references/outputs-and-examples.md`
- `capabilities/autopilot-lab/references/owner-execution.md`
- `capabilities/autopilot-lab/references/setup-procedure.md`

## Guard pointer

Follow the portable artifact, worktree, role, and verification guards in the
selected owner contract. Before the first durable capability artifact, compile
and bind the checked route even for direct intensity; direct is a compiled
inline node, not route absence. A restriction on native subagents/agents is
surface-local and never selects direct execution. Runtime projections must
report unsupported mechanics and must not claim physical instruction masking,
token, billing, or cost savings without verified evidence.
