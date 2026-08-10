---
name: code-refine
description: "Use only when autopilot-code dispatches plan revision after user notes, plan-check feedback, or verification failure. Not for top-level user requests or primary capability routing."
metadata:
  portable_source: capabilities/code-refine.md
  adapter: opencode
  invocation_class: parent-invoked
---

# code-refine

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/code-refine.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info code-refine`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/code-refine.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info code-refine`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `code-refine`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<plan name or path> [--intensity direct|quick|standard|strong|thorough|adversarial]`
- Portable meaning: Revise an existing plan using user notes, plan-check feedback, and verification-failure notes.

## Portable Contract

- Invocation semantics: Reflect user memos/comments in a plan and update it (do NOT implement) Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability code-refine [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
