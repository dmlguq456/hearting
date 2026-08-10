---
name: design-refs
description: "Use only when autopilot-design dispatches visual-reference collection and brief creation. Not for top-level user requests or primary capability routing."
metadata:
  portable_source: capabilities/design-refs.md
  adapter: opencode
  invocation_class: parent-invoked
---

# design-refs

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/design-refs.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info design-refs`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/design-refs.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-refs`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `design-refs`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<design task> [--design <path>] [--refs <image paths>] [--no-web]`
- Portable meaning: Collect external and user-provided visual references and create a brief.

## Portable Contract

- Invocation semantics: Reference collection and briefing: gather user-provided images, external web references through the material role's web-image-search mode, and existing design-system assets. Write a brief that informs subsequent phases. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability design-refs [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
