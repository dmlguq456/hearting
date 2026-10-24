---
description: "Run the portable autopilot-code capability through the OpenCode adapter. Meaning: Code-work entrypoint that detects spec context and closes the plan→execute→test→report loop."
---

Use the OpenCode adapter realization of portable capability `autopilot-code`.
This is adapter-owned output generated from `capabilities/autopilot-code.md`, not a runtime-specific command copy.

1. Read `capabilities/autopilot-code.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-code` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability autopilot-code [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `--mode dev|debug <task/plan/error description> [--from <step>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine]`.

Portable contract excerpt:

- Invocation semantics: General code-work entrypoint for libraries, research code, and applications, whether new or existing; it detects the cwd automatically. It supports `dev` (features/new work) and `debug` (diagnosis/fixes). When `spec/` exists, read it and branch by spec mode: app adds design critique, migration safety, and push/deploy handling; library checks public API consistency; CLI checks command and option consistency; research checks reproducibility, configs, and metrics. Non-code decisions such as PRDs, stack selection, skeletons, and ship setup belong to autopilot-spec. Reusable evaluation drivers and HTML/report-generator bugs are code work. Experiment execution and result reporting belong to lab/eval, whose small support scripts do not automatically require a separate code cycle. Code review within implementation/debug work uses `--mode audit`; independent inspection of completed work uses `audit`. Both may inspect code. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
