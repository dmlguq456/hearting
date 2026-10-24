---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
description: "Use when documents need factual, structural, stylistic, or review-driven correction; preserve history. Owns revision; default preset. Not for new documents, empirical work, or evaluation/result reporting (lab)."
argument-hint: "\"<prompt>\" [--intensity direct|quick|standard|strong|thorough|adversarial] [--review-only | --memo <file>] [--confirm] [--no-fact-check] [--no-style-audit]"
metadata:
  group: entry
  fam: doc
  invocation_class: entry-router
  modes: []
  blurb: "Correct existing documents with snapshots and change history."
  use_when: "Use when documents need factual, structural, stylistic, or review-driven correction; preserve history. Owns revision; default preset."
  not_for: "Not for new documents, empirical work, or evaluation/result reporting (lab)."
---

# autopilot-refine

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
`capabilities/autopilot-refine.md`, then use the Reference Index below. Assigned
stage workers load only their assigned stage contracts.

## Reference Index

After approval, read the portable procedure set:

## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/autopilot-refine/references/examples-and-constraints.md`
- `capabilities/autopilot-refine/references/owner-execution.md`
- `capabilities/autopilot-refine/references/process-stages.md`
- `capabilities/autopilot-refine/references/versioning-and-modes.md`

## Guard pointer

Follow the portable artifact, worktree, role, and verification guards in the
selected owner contract. Before the first durable capability artifact, compile
and bind the checked route even for direct intensity; direct is a compiled
inline node, not route absence. A restriction on native subagents/agents is
surface-local and never selects direct execution. Runtime projections must
report unsupported mechanics and must not claim physical instruction masking,
token, billing, or cost savings without verified evidence.
