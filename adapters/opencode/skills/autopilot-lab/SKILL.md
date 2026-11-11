---
name: autopilot-lab
description: "Use when experiments need setup, evaluation/analysis, failure reproduction, or result reporting, including fixed results without new measurements. Owns experiments; default preset. Not for reusable evaluation code, independent documents, or fixed-data document layout/caption edits."
metadata:
  portable_source: capabilities/autopilot-lab.md
  adapter: opencode
  invocation_class: entry-router
---

# autopilot-lab

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/autopilot-lab.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info autopilot-lab`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the intent card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. Main session only: after approval, read `capabilities/autopilot-lab.md`. Registered owners read only `capabilities/autopilot-lab/references/owner-execution.md`; stage workers read only their assigned contracts. The other entry-router steps apply to the main session. A registered owner uses only its Route steps and the latest receipt for runtime commands.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-lab ...`. Compile it even for direct intensity: direct is a compiled inline node, not route absence. A native-agent restriction never authorizes direct execution.
5. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-lab` and obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `autopilot-lab`
- Invocation class: `entry-router`
- Supported modes: `setup, eval`
- Argument shape: `<task description> [--mode setup|eval|auto] [--parent <slug>] [--ref <similar-model-path>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--report] [--from spec|scaffold|run|eval|summary]`
- Portable meaning: Set up experiments, evaluate, and report results, including fixed results.


## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/autopilot-lab/references/auto-load-context.md`
- `capabilities/autopilot-lab/references/config-provenance.md`
- `capabilities/autopilot-lab/references/data-contract.md`
- `capabilities/autopilot-lab/references/eval-procedure.md`
- `capabilities/autopilot-lab/references/outputs-and-examples.md`
- `capabilities/autopilot-lab/references/owner-execution.md`
- `capabilities/autopilot-lab/references/setup-procedure.md`

## Workflow Evidence

- Before durable capability output: `adapters/opencode/bin/preflight.sh route --capability autopilot-lab <complete compile arguments>`
- For workflow state: `adapters/opencode/bin/preflight.sh status [cwd] [session-id]` and `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`
