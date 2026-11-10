---
name: analyze-user
description: "Use when durable cross-project user preferences must be inferred from coding, writing, or analysis evidence and stored as a profile. Owns profile; default preset. Not for one-project context recovery, casual preference acknowledgment, or ordinary task execution."
---

# analyze-user

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/analyze-user.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info analyze-user`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the intent card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. Main session only: after approval, read `capabilities/analyze-user.md`. Registered owners read only `capabilities/analyze-user/references/owner-execution.md`; stage workers read only their assigned contracts. The other entry-router steps apply to the main session. A registered owner uses only its Route steps and the latest receipt for runtime commands.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability analyze-user ...`; `preflight.sh route analyze-user` is grounding only, not route participation. Compile it even for direct intensity: direct is a compiled inline node, not route absence. A native-subagent restriction never authorizes direct execution.
5. Run `adapters/codex/bin/preflight.sh capability-info analyze-user` and obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `analyze-user`
- Invocation class: `entry-router`
- Supported modes: `init, update`
- Argument shape: `<aspect> [--source <path>] [--mode init|update] [--from discover|analyze|verify|qa|output|summary] [--user-refine]`
- Portable meaning: Create or update a cross-project user-preference profile from coding, writing, and analysis patterns.


## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/analyze-user/references/arguments-and-decisions.md`
- `capabilities/analyze-user/references/integration-and-usage.md`
- `capabilities/analyze-user/references/owner-execution.md`
- `capabilities/analyze-user/references/pipeline-phases.md`

## Workflow Evidence

- Capability grounding only: `adapters/codex/bin/preflight.sh route analyze-user [cwd] [session-id]`
- Before durable capability output: `adapters/codex/bin/preflight.sh route --capability analyze-user <complete compile arguments>`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

