---
description: "Run the portable code-report capability through the OpenCode adapter. Meaning: Assemble code-cycle results into a user-facing report."
---

Use the OpenCode adapter realization of portable capability `code-report`.
This is adapter-owned output generated from `capabilities/code-report.md`, not a runtime-specific command copy.

1. Read `capabilities/code-report.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info code-report` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability code-report [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<plan name or path>`.

Portable contract excerpt:

- Invocation semantics: Generate a detailed change report from plan + dev logs — focuses on key changes, principles, and insights for future reference. When it embeds or cites a generated spectrogram, completion also requires a passing semantic manifest, range-compatible claim evidence, and a hash-current representative PNG review. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
