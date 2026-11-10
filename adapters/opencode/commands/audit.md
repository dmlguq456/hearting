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

- Invocation semantics: Read-only multi-aspect audit/lint for `<artifact-root>/{plans,research,documents}/*` artifacts. A single global entry auto-detects artifact type from the path prefix (`plans`=code, `research`=field survey, `documents`=document deliverable). Per-type aspects: documents use facts/style/structure/cross-reference/coverage; research uses card integrity/tier consistency/coverage/cross-card checks; plans use test results, lint, code review, TODOs, and unimplemented work. `--scope auto` selects from artifact characteristics by default; an explicit user scope overrides it. Report only—never modify the artifact. This complements autopilot-refine: refine edits, while audit inspects. A completed audit can find blocking defects. Its task completion and its judgment about the inspected subject are separate: 'changes required' or 'do not merge' belongs in the report and does not make the audit execution a failure. The existing final handoff reports completion of the assigned audit; no new handoff field is required. For a bound audit owner with a successful native turn, the legacy `FAIL` assessment means completed-with-findings only when its report has one unambiguous closed negative verdict: `최종 판정: 수정 후 병합 — head <7-40 hex>` or `Final verdict: changes requested — head <7-40 hex>`. Read only the first nonblank line that does not start with `#`, using the same rule as the PR review/publish reader. Normalize emphasis, surrounding spaces and one final period, then require the whole line to match. A different opening line retains FAIL; never search the rest of the report for a verdict in examples, quotes or history. Free prose and fence interpretation have no authority. Existing PASS handoffs are unchanged. The handoff may describe subject findings in its blocker text; preserve them as assessment detail. `BLOCKED`, runtime errors, missing artifacts and unfinished children remain unresolved. This does not turn a negative implementation-review gate into a passing gate. Use this capability for independent inspection of completed work, including code and experiment provenance. Code review within an implementation/debug cycle stays `autopilot-code --mode audit`. Measuring new data quality or reproducing an experimental failure is lab/eval; inspecting already completed evidence is audit. This boundary preserves both code-review capabilities. The preset is `inspect → report`; `report` writes the new audit report from the inspection findings (`editorial/report`). An explicit graph may borrow the research map-reduce parts for a parallel survey and synthesis inside the audit cycle, `--graph inspect,autopilot-research:retrieval,autopilot-research:synthesis,report` (written under `parts/autopilot-research/<stage>/`; audit's `map_anchor` for them is declared in `part_catalog.hosts`), and may pick `report:research/research-survey` when the report is a proposal rather than an account of findings, or `report:editorial/polish` only to polish an audit report that already exists. `capability-route.py stages --capability audit` lists the parts. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
