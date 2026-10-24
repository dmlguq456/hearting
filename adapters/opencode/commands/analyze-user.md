---
description: "Run the portable analyze-user capability through the OpenCode adapter. Meaning: Create or update a cross-project user-preference profile from coding, writing, and analysis patterns."
---

Use the OpenCode adapter realization of portable capability `analyze-user`.
This is adapter-owned output generated from `capabilities/analyze-user.md`, not a runtime-specific command copy.

1. Read `capabilities/analyze-user.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info analyze-user` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability analyze-user [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<aspect> [--source <path>] [--mode init|update] [--from discover|analyze|verify|qa|output|summary] [--user-refine]`.

Portable contract excerpt:

- Invocation semantics: Scan and analyze the user's cross-project artifacts (papers, presentations, reports, code, and memory) in stages, then accumulate general working preferences in DB `type=profile` records (`mem profile <stem>`). This uses the same ceremony level as autopilot entrypoints because every sub-agent treats the resulting profile as a default, so even small errors propagate. The six phases are source discovery, per-aspect analysis, cross-aspect consistency checks, multiple QA gates, artifact creation, and pipeline summary. QA is always adversarial and is not user-adjustable. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
