---
description: "Run the portable analyze-project capability through the OpenCode adapter. Meaning: Creates persistent analysis of existing code, papers, or documents; initial analysis defaults here unless read-only/no-file or another primary applies."
---

Use the OpenCode adapter realization of portable capability `analyze-project`.
This is adapter-owned output generated from `capabilities/analyze-project.md`, not a runtime-specific command copy.

1. Read `capabilities/analyze-project.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info analyze-project` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before edits, run `adapters/opencode/bin/preflight.sh write <file> [session-id]`.
4. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability analyze-project [cwd] [session-id]`.
5. If the command receives arguments, map them to the portable argument shape:
   `[--mode code|paper|doc] [<scope/target/input-folder>] [--skip-qa]`.

Portable contract excerpt:

- Invocation semantics: Pre-work analysis capability — analyzes the project's primary materials and writes structured artifacts to `$AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/`. Invoke it when the user explicitly asks to analyze existing code, a paper, or document materials and no usable persistent analysis exists, when existing analysis is demonstrably stale for the requested downstream work, or when the user asks to refresh it. An explicit analysis request defaults to persistent output unless the user asks for conversational/read-only analysis or no files. A request only to understand the current project, recover prior context, resume work, or report status remains read-only orientation and is not an `analyze-project` trigger by itself. When analysis already exists, read it before deciding that reanalysis is needed. That orientation starts with one targeted, agent-chosen memory recall; reads a shortened relevant hit in full by record ID; prefers `.agent_reports/` and uses `.claude_reports/` only when the canonical root is absent; then reads the newest report/experiment artifact with its current PRD/spec before primary code or data. Resolve drift as latest spec or user confirmation, durable project fact, latest experiment contract, then legacy document, and report the conflict instead of silently selecting the older value. Three modes are available: code (codebase), paper (academic PDFs), and doc (miscellaneous document materials such as reviewer comments, format templates, samples, and internal notes). Mode auto-detects between code and doc when omitted; paper requires explicit `--mode paper`. Output is the persistent input source for downstream `autopilot-{draft,code,research}` capabilities. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
