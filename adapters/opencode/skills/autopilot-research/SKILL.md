---
name: autopilot-research
description: "Use when a task needs a durable survey of new academic, technology, or market evidence before downstream specification or production. Not for repository-only project analysis, a simple factual lookup, or work already grounded by sufficient current evidence."
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
2. Present the five-field confirmation card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. After approval, direct/quick acting sessions read `capabilities/autopilot-research.md`; at `standard+`, the dispatch-depth-1 owner reads it and stage workers read only their assigned contracts.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-research ...`. A native-agent restriction never authorizes direct execution.
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


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`
- Before durable capability output: `adapters/opencode/bin/preflight.sh route --capability autopilot-research <complete compile arguments>`
- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability autopilot-research [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
