---
name: autopilot-spec
description: "Use when product requirements, architecture, evaluation policy, or another blueprint must be created or materially updated before implementation. Owns specification; default preset. Not for implementing an already-approved specification or for editing unrelated documents."
metadata:
  portable_source: capabilities/autopilot-spec.md
  adapter: opencode
  invocation_class: entry-router
---

# autopilot-spec

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/autopilot-spec.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info autopilot-spec`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the intent card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. Main session only: after approval, read `capabilities/autopilot-spec.md`. Registered owners read only `capabilities/autopilot-spec/references/owner-execution.md`; stage workers read only their assigned contracts. The other entry-router steps apply to the main session. A registered owner uses only its Route steps and the latest receipt for runtime commands.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-spec ...`. Compile it even for direct intensity: direct is a compiled inline node, not route absence. A native-agent restriction never authorizes direct execution.
5. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-spec` and obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `autopilot-spec`
- Invocation class: `entry-router`
- Supported modes: `app, library, api, cli, research, update`
- Argument shape: `<task description> [--mode auto|app|library|api|cli|research|update (comma-separated for multiple)] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine]`
- Portable meaning: Create or update requirements/blueprints while keeping `prd.md` as the only spec-change path.


## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/autopilot-spec/references/invocation-and-modes.md`
- `capabilities/autopilot-spec/references/operations-and-examples.md`
- `capabilities/autopilot-spec/references/owner-execution.md`
- `capabilities/autopilot-spec/references/prd-authoring.md`
- `capabilities/autopilot-spec/references/scaffolding.md`

## Workflow Evidence

- Before durable capability output: `adapters/opencode/bin/preflight.sh route --capability autopilot-spec <complete compile arguments>`
- For workflow state: `adapters/opencode/bin/preflight.sh status [cwd] [session-id]` and `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`

