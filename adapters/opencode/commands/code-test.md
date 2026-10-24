---
description: "Run the portable code-test capability through the OpenCode adapter. Meaning: Verify implementation results in stages and record evidence."
---

Use the OpenCode adapter realization of portable capability `code-test`.
This is adapter-owned output generated from `capabilities/code-test.md`, not a runtime-specific command copy.

1. Read `capabilities/code-test.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info code-test` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability code-test [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<plan name, path, or test scope> [--intensity direct|quick|standard|strong|thorough|adversarial]`.

Portable contract excerpt:

- Invocation semantics: Run graduated verification after `code-execute` or on demand to verify code correctness. Intensity-derived rigor scales final verification and test-adequacy review; it does not force a separate parallel QA loop by itself. The capability resolves a plan path, changed-file list, or test scope, runs the applicable test levels, stops on the first failing level, and records durable evidence before reporting a verdict. When the verification target includes a report spectrogram, the graduated levels include the fail-closed figure semantic verifier against its manifest and report. Missing exact 48 kHz full-band metadata, range-compatible claims, shared-scale evidence, or a hash-current visual review is a failed level. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable. Put pytest basetemp and tool scratch data under the supplied `runtime_dir` or system temporary storage. Keep test reports and compact evidence in the declared artifact outputs.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
