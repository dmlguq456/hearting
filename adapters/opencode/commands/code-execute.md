---
description: "Run the portable code-execute capability through the OpenCode adapter. Meaning: Execute a plan step by step, delegate implementation to the development role, and record an execution log."
---

Use the OpenCode adapter realization of portable capability `code-execute`.
This is adapter-owned output generated from `capabilities/code-execute.md`, not a runtime-specific command copy.

1. Read `capabilities/code-execute.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info code-execute` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability code-execute [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<plan name or path>`.

Portable contract excerpt:

- Invocation semantics: Execute an implementation plan with progress tracking Put environments, caches, external checkouts, temporary work and training checkpoints under the supplied `runtime_dir` or an established external project location. Keep reports/configs and compact provenance in artifacts; use `runtime_dir` or system temporary storage for pytest and tool scratch data. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
