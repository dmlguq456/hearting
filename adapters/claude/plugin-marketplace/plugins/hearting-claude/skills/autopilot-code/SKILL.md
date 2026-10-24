---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
description: "Use when source code must be implemented, debugged, refactored, or code-audited through a plan, execution, test, and report loop. Owns source changes; default preset. Not for requirements-only changes, new empirical experiments, or document-only editing."
argument-hint: "--mode dev|debug <task/plan/error description> [--from <step>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine]"
metadata:
  group: entry
  fam: code
  invocation_class: entry-router
  modes: ["dev", "debug", "audit"]
  blurb: "Code-work entrypoint that detects spec context and closes the plan→execute→test→report loop."
  use_when: "Use when source code must be implemented, debugged, refactored, or code-audited through a plan, execution, test, and report loop. Owns source changes; default preset."
  not_for: "Not for requirements-only changes, new empirical experiments, or document-only editing."
---

# autopilot-code

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
`capabilities/autopilot-code.md`, then use the Reference Index below. Assigned
stage workers load only their assigned stage contracts.

## Reference Index

After approval, read the portable procedure set:

## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/autopilot-code/references/arguments-and-decisions.md`
- `capabilities/autopilot-code/references/context-and-guards.md`
- `capabilities/autopilot-code/references/debug-audit.md`
- `capabilities/autopilot-code/references/dev-pipeline.md`
- `capabilities/autopilot-code/references/owner-execution.md`
- `capabilities/autopilot-code/references/pipeline-summary-safety.md`

## Guard pointer

Follow the portable artifact, worktree, role, and verification guards in the
selected owner contract. Before the first durable capability artifact, compile
and bind the checked route even for direct intensity; direct is a compiled
inline node, not route absence. A restriction on native subagents/agents is
surface-local and never selects direct execution. Runtime projections must
report unsupported mechanics and must not claim physical instruction masking,
token, billing, or cost savings without verified evidence.
