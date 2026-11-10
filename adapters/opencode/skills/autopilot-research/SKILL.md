---
name: autopilot-research
description: "Use when a task needs a durable survey of new academic, technology, or market evidence before downstream specification or production. Owns research; default preset. Not for repository-only project analysis, a simple factual lookup, or work already grounded by sufficient current evidence."
metadata:
  portable_source: capabilities/autopilot-research.md
  adapter: opencode
  invocation_class: entry-router
---

# autopilot-research

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/autopilot-research.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info autopilot-research`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the intent card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. Main session only: after approval, read `capabilities/autopilot-research.md`. Registered owners read only `capabilities/autopilot-research/references/owner-execution.md`; stage workers read only their assigned contracts. The other entry-router steps apply to the main session. A registered owner uses only its Route steps and the latest receipt for runtime commands.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-research ...`.
5. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-research` and obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `autopilot-research`
- Invocation class: `entry-router`
- Supported modes: `academic, technology, market`
- Argument shape: `<query> [--mode academic|technology|market] [--depth shallow|medium|deep] [--intensity direct|quick|standard|strong|thorough|adversarial] [--no-clarify] [--no-figures] [--from search|analyze|report]`
- Portable meaning: Shared upfront research that surveys academic, technology, or market sources before downstream routing.


## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/autopilot-research/references/invocation-and-modes.md`
- `capabilities/autopilot-research/references/owner-execution.md`
- `capabilities/autopilot-research/references/pipeline-search-analysis.md`
- `capabilities/autopilot-research/references/report-generation.md`
- `capabilities/autopilot-research/references/summary-and-briefing.md`

## Guard pointer

Follow the portable artifact, worktree, role, and verification guards in the
selected owner contract. Before the first durable capability artifact, compile
and bind the checked route even for direct intensity; direct is a compiled
inline node, not route absence. A restriction on native subagents/agents is
surface-local and never selects direct execution. Runtime projections must
report unsupported mechanics and must not claim physical instruction masking,
token, billing, or cost savings without verified evidence.

## Workflow Evidence

- Before durable capability output: `adapters/opencode/bin/preflight.sh route --capability autopilot-research <complete compile arguments>`
- For workflow state: `adapters/opencode/bin/preflight.sh status [cwd] [session-id]` and `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`

