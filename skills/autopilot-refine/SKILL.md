---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: autopilot-refine
description: "Use when an existing document or research artifact needs factual, structural, stylistic, or review-driven correction with history preserved. Not for drafting a new artifact or performing new empirical work required before the document can change."
argument-hint: "\"<prompt>\" [--intensity direct|quick|standard|strong|thorough|adversarial] [--review-only | --memo <file>] [--confirm] [--no-fact-check] [--no-style-audit]"
metadata:
  group: entry
  fam: doc
  invocation_class: entry-router
  modes: []
  blurb: "Correct and update existing document/research artifacts while preserving snapshots and change history."
  use_when: "Use when an existing document or research artifact needs factual, structural, stylistic, or review-driven correction with history preserved."
  not_for: "Not for drafting a new artifact or performing new empirical work required before the document can change."
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
