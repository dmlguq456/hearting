---
name: audit
description: "Use when completed work or artifacts need a read-oriented inspection for drift, inconsistency, omissions, or unsupported claims. Owns audit report; default preset. Not for implementing fixes, producing the primary artifact, or replacing execution-stage verification."
metadata:
  portable_source: capabilities/audit.md
  adapter: opencode
  invocation_class: entry-router
---

# audit

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/audit.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info audit`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the intent card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. Main session only: after approval, read `capabilities/audit.md`. Registered owners read only `capabilities/audit/references/owner-execution.md`; stage workers read only their assigned contracts. The other entry-router steps apply to the main session. A registered owner uses only its Route steps and the latest receipt for runtime commands.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability audit ...`.
5. Run `adapters/opencode/bin/preflight.sh capability-info audit` and obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `audit`
- Invocation class: `entry-router`
- Supported modes: `none`
- Argument shape: `<artifact_path> [--scope auto|facts|style|structure|cross-ref|coverage|all] [--read-only] [--report-only] [--no-fact-check]`
- Portable meaning: Read-oriented post-run inspection for artifact drift, inconsistency, and omissions.


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

## Workflow Evidence

- Before durable capability output: `adapters/opencode/bin/preflight.sh route --capability audit <complete compile arguments>`
- For workflow state: `adapters/opencode/bin/preflight.sh status [cwd] [session-id]` and `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`

