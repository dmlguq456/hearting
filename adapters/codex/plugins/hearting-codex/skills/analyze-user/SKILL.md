---
name: analyze-user
description: "Use when durable cross-project user preferences must be inferred from coding, writing, or analysis evidence and stored as a profile. Not for one-project context recovery, casual preference acknowledgment, or ordinary task execution."
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
2. Present the five-field confirmation card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. After approval, direct/quick acting sessions read `capabilities/analyze-user.md`; at `standard+`, the dispatch-depth-1 owner reads it and stage workers read only their assigned contracts.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability analyze-user ...`; `preflight.sh route analyze-user` is grounding only, not route participation. A native-subagent restriction never authorizes direct execution.
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



## Required Guards

- Before edits: `adapters/codex/bin/preflight.sh write <file> [session-id]`
- Capability grounding only: `adapters/codex/bin/preflight.sh route analyze-user [cwd] [session-id]`
- Before durable capability output: `adapters/codex/bin/preflight.sh route --capability analyze-user <complete compile arguments>`
- Before spec-changing work: `adapters/codex/bin/preflight.sh capability analyze-user [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/codex/bin/preflight.sh read <prd.md> [session-id]`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
