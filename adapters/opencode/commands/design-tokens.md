---
description: "Run the portable design-tokens capability through the OpenCode adapter. Meaning: Define design tokens such as color, typography, and spacing."
---

Use the OpenCode adapter realization of portable capability `design-tokens`.
This is adapter-owned output generated from `capabilities/design-tokens.md`, not a runtime-specific command copy.

1. Read `capabilities/design-tokens.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-tokens` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability design-tokens [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<design path or app path>`.

Portable contract excerpt:

- Invocation semantics: Design token decisions cover palette, typography, spacing, radius, shadow, and motion. Record proposed values, rationale, and a rendered specimen in the design artifacts; hand off application-file edits to `autopilot-code`, which inspects and updates the real imported token source. Extend existing tokens by default and preserve version history in the design artifacts. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
