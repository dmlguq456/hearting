---
name: autopilot-ship
description: "Use when preparing deployment/release or a delivery archive (package mode). Owns release; default preset. Not for code, new inference/evaluation, report rewriting, or unauthorized deployment; package mode does not run the deployment/review loop."
---

# autopilot-ship

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/autopilot-ship.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info autopilot-ship`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the five-field confirmation card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. After approval, direct/quick acting sessions read `capabilities/autopilot-ship.md`; at `standard+`, the dispatch-depth-1 owner reads it and stage workers read only their assigned contracts.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-ship ...`; `preflight.sh route autopilot-ship` is grounding only, not route participation. A native-subagent restriction never authorizes direct execution.
5. Run `adapters/codex/bin/preflight.sh capability-info autopilot-ship` and obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `autopilot-ship`
- Invocation class: `entry-router`
- Supported modes: `default, package`
- Argument shape: `<task description (optional)> [--mode default|package] [--intensity direct|quick|standard|strong|thorough|adversarial]`
- Portable meaning: Prepare deployment/release setup or package existing artifacts.


## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/autopilot-ship/references/examples.md`
- `capabilities/autopilot-ship/references/owner-execution.md`

## Workflow Evidence

- Capability grounding only: `adapters/codex/bin/preflight.sh route autopilot-ship [cwd] [session-id]`
- Before durable capability output: `adapters/codex/bin/preflight.sh route --capability autopilot-ship <complete compile arguments>`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
