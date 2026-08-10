---
name: audit
description: "Use when completed work or artifacts need a read-oriented inspection for drift, inconsistency, omissions, or unsupported claims. Not for implementing fixes, producing the primary artifact, or replacing execution-stage verification."
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
2. Present the five-field confirmation card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. After approval, direct/quick acting sessions read `capabilities/audit.md`; at `standard+`, the dispatch-depth-1 owner reads it and stage workers read only their assigned contracts.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability audit ...`. A native-agent restriction never authorizes direct execution.
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


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`
- Before durable capability output: `adapters/opencode/bin/preflight.sh route --capability audit <complete compile arguments>`
- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability audit [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
