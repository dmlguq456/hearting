---
name: design-components
description: "Use only when autopilot-design dispatches component, mockup, or preview construction. Not for top-level user requests or primary capability routing."
metadata:
  portable_source: capabilities/design-components.md
  adapter: opencode
  invocation_class: parent-invoked
---

# design-components

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/design-components.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info design-components`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/design-components.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-components`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `design-components`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<design path or app path>`
- Portable meaning: Build UI components/mockups and preview artifacts.

## Portable Contract

- Invocation semantics: Component and visual-asset creation through the design role's maker mode. Produce shadcn/Tailwind components (`ui`), composed full-screen pages (`webapp`), slide visual guides (`slide`), SVG icons (`icon`), or Mermaid/direct-SVG/Excalidraw diagrams (`diagram`). Render and visually self-verify every output through a render→read→fix loop. With `--artifact standalone`, emit a self-contained single-file HTML preview. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability design-components [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
