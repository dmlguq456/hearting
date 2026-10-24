---
name: autopilot-draft
description: "Use when a new document has an independent audience and document goal. Owns document; default preset. Not for experiment reports (lab), existing-document edits, or code."
metadata:
  portable_source: capabilities/autopilot-draft.md
  adapter: opencode
  invocation_class: entry-router
---

# autopilot-draft

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/autopilot-draft.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info autopilot-draft`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the five-field confirmation card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. After approval, direct/quick acting sessions read `capabilities/autopilot-draft.md`; at `standard+`, the dispatch-depth-1 owner reads it and stage workers read only their assigned contracts.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability autopilot-draft ...`. A native-agent restriction never authorizes direct execution.
5. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-draft` and obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `autopilot-draft`
- Invocation class: `entry-router`
- Supported modes: `paper, presentation, doc`
- Argument shape: `<task description> [--mode paper|presentation|doc] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine] [--no-clarify] [--from analyze|strategy|strategy-refine|draft|draft-refine|finalize]`
- Portable meaning: Draft independent documents through strategy, verification, and editing.


## Shared Procedure References

Read the complete portable procedure files listed here when they apply; this logical read-set is shared across Claude, Codex, and OpenCode.

- `capabilities/autopilot-draft/references/convention-common.md`
- `capabilities/autopilot-draft/references/convention-doc.md`
- `capabilities/autopilot-draft/references/convention-paper.md`
- `capabilities/autopilot-draft/references/convention-presentation.md`
- `capabilities/autopilot-draft/references/invocation-and-args.md`
- `capabilities/autopilot-draft/references/owner-execution.md`
- `capabilities/autopilot-draft/references/pipeline-steps.md`
- `capabilities/autopilot-draft/references/review-and-qa.md`
- `capabilities/autopilot-draft/references/summary-and-safety.md`

## Workflow Evidence

- Before durable capability output: `adapters/opencode/bin/preflight.sh route --capability autopilot-draft <complete compile arguments>`
- For workflow state: `adapters/opencode/bin/preflight.sh status [cwd] [session-id]` and `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
