---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: autopilot-spec
description: "Use when product requirements, architecture, evaluation policy, or another blueprint must be created or materially updated before implementation. Not for implementing an already-approved specification or for editing unrelated documents."
argument-hint: "<task description> [--mode auto|app|library|api|cli|research|update (comma-separated for multiple)] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine]"
metadata:
  group: entry
  fam: code
  invocation_class: entry-router
  modes: ["app", "library", "api", "cli", "research", "update"]
  blurb: "Create or update requirements/blueprints while keeping `prd.md` as the only spec-change path."
  use_when: "Use when product requirements, architecture, evaluation policy, or another blueprint must be created or materially updated before implementation."
  not_for: "Not for implementing an already-approved specification or for editing unrelated documents."
---

# autopilot-spec

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
`capabilities/autopilot-spec.md`, then use the Reference Index below. Assigned
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
