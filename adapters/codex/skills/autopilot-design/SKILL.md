---
name: autopilot-design
description: "Use when a visual product surface needs references, design tokens, components or mockups, review, and development handoff. Owns design; default preset. Not for implementing an already-approved design in code or for document prose work."
---

# autopilot-design

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/autopilot-design.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info autopilot-design`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the intent card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. Main session only: after approval, read `capabilities/autopilot-design.md`. Registered owners read only `capabilities/autopilot-design/references/owner-execution.md`; stage workers read only their assigned contracts. The other entry-router steps apply to the main session. A registered owner uses only its Route steps and the latest receipt for runtime commands.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-design ...`; `preflight.sh route autopilot-design` is grounding only, not route participation. Compile it even for direct intensity: direct is a compiled inline node, not route absence. A native-subagent restriction never authorizes direct execution.
5. Run `adapters/codex/bin/preflight.sh capability-info autopilot-design` and obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `autopilot-design`
- Invocation class: `entry-router`
- Supported modes: `none`
- Argument shape: `<design task or app path> [--scope ui|webapp|slide|icon|diagram|mixed] [--artifact standalone|project] [--from <phase>] [--intensity direct|quick|standard|strong|thorough|adversarial]`
- Portable meaning: Visual-design pipeline coordinating references→tokens→components→review→handoff.


## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/autopilot-design/references/harness.md`
- `capabilities/autopilot-design/references/owner-execution.md`
- `capabilities/autopilot-design/references/paper-figure-policy.md`
- `capabilities/autopilot-design/references/pipeline-execution.md`

## Workflow Evidence

- Capability grounding only: `adapters/codex/bin/preflight.sh route autopilot-design [cwd] [session-id]`
- Before durable capability output: `adapters/codex/bin/preflight.sh route --capability autopilot-design <complete compile arguments>`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`
