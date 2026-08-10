---
name: analyze-project
description: "Use when the user asks to analyze existing code, a paper, or a document and no usable persistent analysis exists, or to refresh stale analysis; default initial analysis to persistent output. Not for conversational, read-only, or no-file analysis; orientation, context recovery, status, experiments, external research, source changes, or completed-work audits."
---

# analyze-project

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/analyze-project.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info analyze-project`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the five-field confirmation card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. After approval, direct/quick acting sessions read `capabilities/analyze-project.md`; at `standard+`, the dispatch-depth-1 owner reads it and stage workers read only their assigned contracts.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability analyze-project ...`; `preflight.sh route analyze-project` is grounding only, not route participation. A native-subagent restriction never authorizes direct execution.
5. Run `adapters/codex/bin/preflight.sh capability-info analyze-project` and obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `analyze-project`
- Invocation class: `entry-router`
- Supported modes: `code, paper, doc`
- Argument shape: `[--mode code|paper|doc] [<scope/target/input-folder>] [--skip-qa]`
- Portable meaning: Creates persistent analysis of existing code, papers, or documents; initial analysis defaults here unless read-only/no-file or another primary applies.



## Required Guards

- Before edits: `adapters/codex/bin/preflight.sh write <file> [session-id]`
- Capability grounding only: `adapters/codex/bin/preflight.sh route analyze-project [cwd] [session-id]`
- Before durable capability output: `adapters/codex/bin/preflight.sh route --capability analyze-project <complete compile arguments>`
- Before spec-changing work: `adapters/codex/bin/preflight.sh capability analyze-project [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/codex/bin/preflight.sh read <prd.md> [session-id]`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
