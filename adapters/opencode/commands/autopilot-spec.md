---
description: "Run the portable autopilot-spec capability through the OpenCode adapter. Meaning: Create or update requirements/blueprints while keeping `prd.md` as the only spec-change path."
---

Use the OpenCode adapter realization of portable capability `autopilot-spec`.
This is adapter-owned output generated from `capabilities/autopilot-spec.md`, not a runtime-specific command copy.

1. Read `capabilities/autopilot-spec.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-spec` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability autopilot-spec [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<task description> [--mode auto|app|library|api|cli|research|update (comma-separated for multiple)] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine]`.

Portable contract excerpt:

- Invocation semantics: General entrypoint for creating and updating requirements and blueprints: new intent, cleanup/public-release preparation for existing code, and iteration of an existing spec through `prd.md`. It supports app, library, API, CLI, and research modes; multiple modes; auto detection; and update mode. Update mode edits the existing `prd.md`, the canonical path for every spec change. The shared transaction helper automatically snapshots the exact previous bytes whenever an existing PRD actually changes, at every intensity. PRDs contain common sections plus independent per-mode sections. Automatically cite autopilot-research and analyze-project outputs. This is the blueprint counterpart to analyze-project's new-intent analysis. Actual code work belongs to autopilot-code, which detects `spec/` context automatically. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
