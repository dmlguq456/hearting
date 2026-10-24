---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
description: "Use when completed work or artifacts need a read-oriented inspection for drift, inconsistency, omissions, or unsupported claims. Owns audit report; default preset. Not for implementing fixes, producing the primary artifact, or replacing execution-stage verification."
argument-hint: "<artifact_path> [--scope auto|facts|style|structure|cross-ref|coverage|all] [--read-only] [--report-only] [--no-fact-check]"
metadata:
  group: ops
  fam: ops
  invocation_class: entry-router
  modes: []
  blurb: "Read-oriented post-run inspection for artifact drift, inconsistency, and omissions."
  use_when: "Use when completed work or artifacts need a read-oriented inspection for drift, inconsistency, omissions, or unsupported claims. Owns audit report; default preset."
  not_for: "Not for implementing fixes, producing the primary artifact, or replacing execution-stage verification."
---

# audit

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
`capabilities/audit.md`, then use the Reference Index below. Assigned
stage workers load only their assigned stage contracts.

## Reference Index

After approval, read the portable procedure set:

## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/audit/references/aspect-lints.md`
- `capabilities/audit/references/examples-and-checklist.md`
- `capabilities/audit/references/owner-execution.md`
- `capabilities/audit/references/report-and-autofix.md`
- `capabilities/audit/references/scope-and-baseline.md`

## Guard pointer

Follow the portable artifact, worktree, role, and verification guards in the
selected owner contract. Before the first durable capability artifact, compile
and bind the checked route even for direct intensity; direct is a compiled
inline node, not route absence. A restriction on native subagents/agents is
surface-local and never selects direct execution. Runtime projections must
report unsupported mechanics and must not claim physical instruction masking,
token, billing, or cost savings without verified evidence.
