---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: analyze-project
description: "Use when the user asks to analyze existing code, a paper, or a document and no usable persistent analysis exists, or to refresh stale analysis; default initial analysis to persistent output. Not for conversational, read-only, or no-file analysis; orientation, context recovery, status, experiments, external research, source changes, or completed-work audits."
argument-hint: "[--mode code|paper|doc] [<scope/target/input-folder>] [--skip-qa]"
metadata:
  group: pre
  fam: pre
  invocation_class: entry-router
  modes: ["code", "paper", "doc"]
  blurb: "Creates persistent analysis of existing code, papers, or documents; initial analysis defaults here unless read-only/no-file or another primary applies."
  use_when: "Use when the user asks to analyze existing code, a paper, or a document and no usable persistent analysis exists, or to refresh stale analysis; default initial analysis to persistent output."
  not_for: "Not for conversational, read-only, or no-file analysis; orientation, context recovery, status, experiments, external research, source changes, or completed-work audits."
---

# analyze-project

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
`capabilities/analyze-project.md`, then use the Reference Index below. Assigned
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
