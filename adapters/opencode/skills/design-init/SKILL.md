---
name: design-init
description: "Use only when autopilot-design dispatches design environment and state initialization. Not for top-level user requests or primary capability routing."
metadata:
  portable_source: capabilities/design-init.md
  adapter: opencode
  invocation_class: parent-invoked
---

# design-init

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/design-init.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info design-init`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/design-init.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-init`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `design-init`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<design task description> [--scope ui|slide|icon|diagram|mixed]`
- Portable meaning: Bootstrap the design environment and state.

## Portable Contract

- Invocation semantics: Design environment check and bootstrap — self-provisions the runtime design harness that powers visual self-verification, plus optional Figma MCP, shadcn/ui, Tailwind tokens, SVG rasterizer, and image-generation integration where supported. Adapter-native files own concrete MCP registration commands and runtime paths. Per spec §0.5 it installs what is missing rather than stopping. Creates design_state.yaml. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability design-init [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
