---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
description: "Use when a new document has an independent audience and document goal. Owns document; default preset. Not for experiment reports (lab), existing-document edits, or code."
argument-hint: "<task description> [--mode paper|presentation|doc] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine] [--no-clarify] [--from analyze|strategy|strategy-refine|draft|draft-refine|finalize]"
metadata:
  group: entry
  fam: doc
  invocation_class: entry-router
  modes: ["paper", "presentation", "doc"]
  blurb: "Draft independent documents through strategy, verification, and editing."
  use_when: "Use when a new document has an independent audience and document goal. Owns document; default preset."
  not_for: "Not for experiment reports (lab), existing-document edits, or code."
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

After approval, read the portable procedure set:

## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/autopilot-draft/references/convention-common.md`
- `capabilities/autopilot-draft/references/convention-doc.md`
- `capabilities/autopilot-draft/references/convention-paper.md`
- `capabilities/autopilot-draft/references/convention-presentation.md`
- `capabilities/autopilot-draft/references/invocation-and-args.md`
- `capabilities/autopilot-draft/references/owner-execution.md`
- `capabilities/autopilot-draft/references/pipeline-steps.md`
- `capabilities/autopilot-draft/references/review-and-qa.md`
- `capabilities/autopilot-draft/references/summary-and-safety.md`

## Guard pointer

Follow the portable artifact, worktree, role, and verification guards in the
selected owner contract. Before the first durable capability artifact, compile
and bind the checked route even for direct intensity; direct is a compiled
inline node, not route absence. A restriction on native subagents/agents is
surface-local and never selects direct execution. Runtime projections must
report unsupported mechanics and must not claim physical instruction masking,
token, billing, or cost savings without verified evidence.
