---
description: "Run the portable draft-strategy capability through the OpenCode adapter. Meaning: Create an initial document strategy and evidence-based writing plan."
---

Use the OpenCode adapter realization of portable capability `draft-strategy`.
This is adapter-owned output generated from `capabilities/draft-strategy.md`, not a runtime-specific command copy.

1. Read `capabilities/draft-strategy.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info draft-strategy` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability draft-strategy [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<mode> --inputs <comma-separated-paths> --output <artifact-dir> [--intensity direct|quick|standard|strong|thorough|adversarial] <task description>`.

Portable contract excerpt:

- Invocation semantics: Create an initial document strategy. The internal mode enum has six values: rebuttal, paper, review, report, proposal, and presentation. Autopilot-draft's form-first paper/presentation/doc modes convert doc intent from natural-language keywords (rebuttal response, review, report, proposal, or generic) into one of these direct sub-skill mode labels. For direct invocation, require the user to provide one of the six modes as the first argument. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
