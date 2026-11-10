---
name: autopilot-code
description: "Use when source code must be implemented, debugged, refactored, or code-audited through a plan, execution, test, and report loop. Owns source changes; default preset. Not for requirements-only changes, new empirical experiments, or document-only editing."
metadata:
  portable_source: capabilities/autopilot-code.md
  adapter: opencode
  invocation_class: entry-router
---

# autopilot-code

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/autopilot-code.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info autopilot-code`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the intent card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. Main session only: after approval, read `capabilities/autopilot-code.md`. Registered owners read only `capabilities/autopilot-code/references/owner-execution.md`; stage workers read only their assigned contracts. The other entry-router steps apply to the main session. A registered owner uses only its Route steps and the latest receipt for runtime commands.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-code ...`.
5. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-code` and obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `autopilot-code`
- Invocation class: `entry-router`
- Supported modes: `dev, debug, audit`
- Argument shape: `--mode dev|debug <task/plan/error description> [--from <step>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine]`
- Portable meaning: Code-work entrypoint that detects spec context and closes the plan→execute→test→report loop.


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

## Workflow Evidence

- Before durable capability output: `adapters/opencode/bin/preflight.sh route --capability autopilot-code <complete compile arguments>`
- For workflow state: `adapters/opencode/bin/preflight.sh status [cwd] [session-id]` and `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`

