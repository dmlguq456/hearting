---
name: design-tokens
description: "Use only when autopilot-design dispatches design-token definition or revision. Not for top-level user requests or primary capability routing."
metadata:
  portable_source: capabilities/design-tokens.md
  adapter: opencode
  invocation_class: parent-invoked
---

# design-tokens

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/design-tokens.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info design-tokens`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/design-tokens.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-tokens`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `design-tokens`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<design path or app path>`
- Portable meaning: Define design tokens such as color, typography, and spacing.

## Portable Contract

- Invocation semantics: Design tokens decision — color palette, typography scale, spacing scale, radius, shadow, motion. Writes tokens.css / tailwind.config.ts. Extends existing tokens (never silently overwrites). Versions every change — snapshots prior tokens to _internal/versions/v{N}/ + logs reason to design_summary.md (mirrors spec versioning), so later token edits stay traceable. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability design-tokens [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
