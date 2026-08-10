---
name: draft-strategy
description: "Use only when autopilot-draft dispatches document strategy and evidence-plan creation. Not for top-level user requests or primary capability routing."
metadata:
  portable_source: capabilities/draft-strategy.md
  adapter: opencode
  invocation_class: parent-invoked
---

# draft-strategy

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/draft-strategy.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info draft-strategy`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/draft-strategy.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info draft-strategy`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `draft-strategy`
- Invocation class: `parent-invoked`
- Supported modes: `rebuttal, paper, review, report, proposal, presentation`
- Argument shape: `<mode> --inputs <comma-separated-paths> --output <artifact-dir> [--intensity direct|quick|standard|strong|thorough|adversarial] <task description>`
- Portable meaning: Create an initial document strategy and evidence-based writing plan.

## Portable Contract

- Invocation semantics: Create an initial document strategy. The internal mode enum has six values: rebuttal, paper, review, report, proposal, and presentation. Autopilot-draft's form-first paper/presentation/doc modes convert doc intent from natural-language keywords (rebuttal response, review, report, proposal, or generic) into one of these direct sub-skill mode labels. For direct invocation, require the user to provide one of the six modes as the first argument. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability draft-strategy [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
