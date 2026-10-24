---
description: "Run the portable design-handoff capability through the OpenCode adapter. Meaning: Package design results as assets and specifications for development handoff."
---

Use the OpenCode adapter realization of portable capability `design-handoff`.
This is adapter-owned output generated from `capabilities/design-handoff.md`, not a runtime-specific command copy.

1. Read `capabilities/design-handoff.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-handoff` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability design-handoff [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<design path or app path>`.

Portable contract excerpt:

- Invocation semantics: Final handoff — consolidates design artifacts into a single handoff.md that frontend devs (or autopilot-spec build phase) can use directly. Lists components, token paths, import paths, reproduction guide. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
