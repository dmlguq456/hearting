---
description: "Run the portable design-review capability through the OpenCode adapter. Meaning: Review design output for quality, token-contract compliance, and breakage."
---

Use the OpenCode adapter realization of portable capability `design-review`.
This is adapter-owned output generated from `capabilities/design-review.md`, not a runtime-specific command copy.

1. Read `capabilities/design-review.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-review` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability design-review [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<design path or app path>`.

Portable contract excerpt:

- Invocation semantics: Visual review with two gates. First, a verifier in a separate context uses the adapter visual harness to screen for console errors, layout collapse, and intent mismatch; it must pass before critique. Second, a critic evaluates hierarchy, alignment, accessibility, responsiveness, UX flow, and tone. Both gates render through the adapter-provided visual harness and inspect the image. Read only; never auto-fix. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
