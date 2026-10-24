---
description: "Run the portable design-refs capability through the OpenCode adapter. Meaning: Collect external and user-provided visual references and create a brief."
---

Use the OpenCode adapter realization of portable capability `design-refs`.
This is adapter-owned output generated from `capabilities/design-refs.md`, not a runtime-specific command copy.

1. Read `capabilities/design-refs.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-refs` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability design-refs [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<design task> [--design <path>] [--refs <image paths>] [--no-web]`.

Portable contract excerpt:

- Invocation semantics: Reference collection and briefing: gather user-provided images, external web references through the material role's web-image-search mode, and existing design-system assets. Write a brief that informs subsequent phases. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
