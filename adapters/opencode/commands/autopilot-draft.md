---
description: "Run the portable autopilot-draft capability through the OpenCode adapter. Meaning: Draft independent documents through strategy, verification, and editing."
---

Use the OpenCode adapter realization of portable capability `autopilot-draft`.
This is adapter-owned output generated from `capabilities/autopilot-draft.md`, not a runtime-specific command copy.

1. Read `capabilities/autopilot-draft.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-draft` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability autopilot-draft [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<task description> [--mode paper|presentation|doc] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine] [--no-clarify] [--from analyze|strategy|strategy-refine|draft|draft-refine|finalize]`.

Portable contract excerpt:

- Invocation semantics: Document draft pipeline: analyze → strategy → strategy-refine → draft → draft-refine → finalize. In `paper` mode, “draft” means a **paste-ready cheatsheet draft**: LaTeX-ready cards describing mutations that the user applies to canonical `main.tex` through autopilot-apply, not blank-page body writing. This meaning is unchanged for new and existing papers. Output-form modes are `paper` (LaTeX academic cheatsheet), `presentation` (slide-by-slide PPT markdown), and `doc` (Word/HWP/Markdown prose such as reports, proposals, rebuttals, reviews, blogs, and memos). The mode is form-first; the natural language task describes purpose/genre without a subtype enum. Discover inputs from `<artifact-root>/{analysis_project,research}/*`; preprocess external materials with `/analyze-project --mode {paper|doc}`. Load matching format specs from `analysis_project/doc/{matching}/formats/` automatically. Mode conventions live under `## Mode-Specific Conventions` (common plus paper, presentation, or doc). Presentation mode produces Markdown only; PPTX export is unsupported, so use PowerPoint directly. When a draft contains generated spectrograms, finalization requires the report figure evidence contract in `core/CONVENTIONS.md §4.1`: a semantic manifest, the fail-closed verifier result, claim-to-range evidence, and at least one recorded representative PNG review. A file/count/link-only check cannot satisfy this gate. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
