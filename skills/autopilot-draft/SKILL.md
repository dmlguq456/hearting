---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: autopilot-draft
description: "Use when a new paper, presentation, report, proposal, or other user-facing document must be produced from evidence. Not for correcting only an existing document or for source-code implementation."
argument-hint: "<task description> [--mode paper|presentation|doc] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine] [--no-clarify] [--from analyze|strategy|strategy-refine|draft|draft-refine|finalize]"
metadata:
  group: entry
  fam: doc
  invocation_class: entry-router
  modes: ["paper", "presentation", "doc"]
  blurb: "Document-drafting pipeline that produces an applicable artifact through strategy, drafting, verification, and editing."
  use_when: "Use when a new paper, presentation, report, proposal, or other user-facing document must be produced from evidence."
  not_for: "Not for correcting only an existing document or for source-code implementation."
---

# autopilot-draft

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
`capabilities/autopilot-draft.md`, then use the Reference Index below. Assigned
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
