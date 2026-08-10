---
name: autopilot-code
description: "Use when source code must be implemented, debugged, refactored, or code-audited through a plan, execution, test, and report loop. Not for requirements-only changes, new empirical experiments, or document-only editing."
---

# autopilot-code

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/autopilot-code.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info autopilot-code`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the five-field confirmation card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. After approval, direct/quick acting sessions read `capabilities/autopilot-code.md`; at `standard+`, the dispatch-depth-1 owner reads it and stage workers read only their assigned contracts.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-code ...`; `preflight.sh route autopilot-code` is grounding only, not route participation. A native-subagent restriction never authorizes direct execution.
5. Run `adapters/codex/bin/preflight.sh capability-info autopilot-code` and obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `autopilot-code`
- Invocation class: `entry-router`
- Supported modes: `dev, debug, audit`
- Argument shape: `--mode dev|debug <task/plan/error description> [--from <step>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine]`
- Portable meaning: Code-work entrypoint that detects spec context and closes the plan→execute→test→report loop.



## Required Guards

- Before edits: `adapters/codex/bin/preflight.sh write <file> [session-id]`
- Capability grounding only: `adapters/codex/bin/preflight.sh route autopilot-code [cwd] [session-id]`
- Before durable capability output: `adapters/codex/bin/preflight.sh route --capability autopilot-code <complete compile arguments>`
- Before spec-changing work: `adapters/codex/bin/preflight.sh capability autopilot-code [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/codex/bin/preflight.sh read <prd.md> [session-id]`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
