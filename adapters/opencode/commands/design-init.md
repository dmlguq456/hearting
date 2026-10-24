---
description: "Run the portable design-init capability through the OpenCode adapter. Meaning: Bootstrap the design environment and state."
---

Use the OpenCode adapter realization of portable capability `design-init`.
This is adapter-owned output generated from `capabilities/design-init.md`, not a runtime-specific command copy.

1. Read `capabilities/design-init.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-init` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability design-init [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<design task description> [--scope ui|slide|icon|diagram|mixed]`.

Portable contract excerpt:

- Invocation semantics: Design environment check and bootstrap — self-provisions the runtime design harness that powers visual self-verification, plus optional Figma MCP, shadcn/ui, Tailwind tokens, SVG rasterizer, and image-generation integration where supported. Adapter-native files own concrete MCP registration commands and runtime paths. Per spec §0.5 it installs what is missing rather than stopping. Creates design_state.yaml. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
