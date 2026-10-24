---
description: "Run the portable design-components capability through the OpenCode adapter. Meaning: Build UI components/mockups and preview artifacts."
---

Use the OpenCode adapter realization of portable capability `design-components`.
This is adapter-owned output generated from `capabilities/design-components.md`, not a runtime-specific command copy.

1. Read `capabilities/design-components.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-components` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability design-components [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<design path or app path>`.

Portable contract excerpt:

- Invocation semantics: Component and visual-asset creation through the design role's maker mode. Produce shadcn/Tailwind components (`ui`), composed full-screen pages (`webapp`), slide visual guides (`slide`), SVG icons (`icon`), or Mermaid/direct-SVG/Excalidraw diagrams (`diagram`). Render and visually self-verify every output through a render→read→fix loop. With `--artifact standalone`, emit a self-contained single-file HTML preview. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
