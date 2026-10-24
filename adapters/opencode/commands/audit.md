---
description: "Run the portable audit capability through the OpenCode adapter. Meaning: Read-oriented post-run inspection for artifact drift, inconsistency, and omissions."
---

Use the OpenCode adapter realization of portable capability `audit`.
This is adapter-owned output generated from `capabilities/audit.md`, not a runtime-specific command copy.

1. Read `capabilities/audit.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info audit` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability audit [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<artifact_path> [--scope auto|facts|style|structure|cross-ref|coverage|all] [--read-only] [--report-only] [--no-fact-check]`.

Portable contract excerpt:

- Invocation semantics: Read-only multi-aspect audit/lint for `<artifact-root>/{plans,research,documents}/*` artifacts. A single global entry auto-detects artifact type from the path prefix (`plans`=code, `research`=field survey, `documents`=document deliverable). Per-type aspects: documents use facts/style/structure/cross-reference/coverage; research uses card integrity/tier consistency/coverage/cross-card checks; plans use test results, lint, code review, TODOs, and unimplemented work. `--scope auto` selects from artifact characteristics by default; an explicit user scope overrides it. Report only—never modify the artifact. This complements autopilot-refine: refine edits, while audit inspects. Use this capability for independent inspection of completed work, including code and experiment provenance. Code review within an implementation/debug cycle stays `autopilot-code --mode audit`. Measuring new data quality or reproducing an experimental failure is lab/eval; inspecting already completed evidence is audit. This boundary preserves both code-review capabilities. The preset is `inspect → report`. An explicit graph may borrow the research map-reduce parts for a parallel survey and synthesis inside the audit cycle, `--graph inspect,autopilot-research:retrieval,autopilot-research:synthesis,report` (written under `parts/autopilot-research/<stage>/`; audit's `map_anchor` for them is declared in `part_catalog.hosts`), and may pick `report:research/research-survey` when the report is a proposal rather than a polish of findings. `capability-route.py stages --capability audit` lists the parts. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
