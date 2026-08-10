---
name: code-plan
description: "Use only when autopilot-code dispatches the planning and plan-check stage. Not for top-level user requests or primary capability routing."
metadata:
  portable_source: capabilities/code-plan.md
  adapter: opencode
  invocation_class: parent-invoked
---

# code-plan

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/code-plan.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info code-plan`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/code-plan.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info code-plan`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `code-plan`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<task description> [--intensity direct|quick|standard|strong|thorough|adversarial]`
- Portable meaning: Analyze code, write a detailed implementation plan, and run the plan-check gate at the rigor derived from intensity.

## Portable Contract

- Invocation semantics: Create a detailed implementation plan based on actual codebase Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability code-plan [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
