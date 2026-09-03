---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: code-report
description: "Use only when autopilot-code dispatches the final code-cycle reporting stage. Not for top-level user requests or primary capability routing."
argument-hint: "<plan name or path>"
metadata:
  group: sub
  fam: sub
  invocation_class: parent-invoked
  modes: []
  blurb: "Assemble code-cycle results into a user-facing report."
  use_when: "Use only when autopilot-code dispatches the final code-cycle reporting stage."
  not_for: "Not for top-level user requests or primary capability routing."
---

# code-report

> **Stage-session entry (`standard+` dispatch, spec/stage-dispatch SD-2)**: Run in-session or as an isolated dispatch-depth-2 stage worker dispatched by the `autopilot-code` conductor. Read the plan, checklist, `dev_logs/`, `test_logs/`, `_internal/*_reviews/`, and `pipeline_summary.md` from disk; never assume prior-stage conversation is available. The write class is `final_report.md`, `<artifact-root>/analysis_project/code/*.md`, and lock-protected `pipeline_summary.md`. The stage runs as the `editorial/report` unit (the node's unit); it dispatches no units itself.

> **Plan resolution**: Treat [arguments-and-decisions.md#plan-resolution](../autopilot-code/references/arguments-and-decisions.md) as the single authority for resolving `$ARG`.

> **Language rule**: Follow the audience and artifact language contract in [arguments-and-decisions.md#language-rule](../autopilot-code/references/arguments-and-decisions.md). The report template must be localized to the selected artifact language rather than fixed to the language of this skill file.

## Writer and QA Policy

Use one portable `fast writer` at every rigor tier. Prior stages already review the plan, implementation, and tests; this stage synthesizes their artifacts. The incoming intensity-derived rigor or plan-frontmatter `qa_level` is context only. It does not change the writer role, add parallel writers, or open a report-review loop.

| Rigor | Report action |
|---|---|
| Light | One fast writer |
| Standard | One fast writer |
| Thorough | One fast writer |
| Adversarial | One fast writer; no external-adversary review of report prose |

## Generate the Report

Run the `editorial/report` unit (portable `fast writer` role) with this task:

```text
Generate a final change report.

Plan file: {$ARG}, resolved through the plan-resolution contract
Log directory: {task root above plan.md}
Existing audience-language companion plan: {path or none}
Report output: {log_directory}/final_report.md
Artifact language: {explicit audience/artifact language, otherwise conversation language}
Date: {YYYY-MM-DD}

Inputs:
1. Canonical plan and any existing required companion
2. checklist.md
3. dev_logs/step_*.md and test_logs/
4. _internal/{plan_reviews,dev_reviews,test_reviews}/
5. pipeline_summary.md when present

Procedure:
1. Read the plan for goals, current state, and change intent.
2. Read the checklist for successful, failed, and skipped steps.
3. Read all development logs and extract each old → new change, Decision rationale, modified file, and result.
4. Read implementation and test reviews; record findings, resolutions, and unresolved items.
5. Update $AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/code/ for successful steps only when that directory already exists. Map each source file to the best existing topic document. Common mappings include:
   - model/module → model_modules.md or the matching topic
   - network/backbone → network_modules.md
   - loss/objective → loss_functions.md
   - data pipeline → dataset_pipeline.md
   - training entrypoint → engine_training.md
   - inference entrypoint → engine_inference.md
   - utility → utilities.md
   - architecture/config/data flow/cross-variant → architecture.md
   - project structure, document table, or file rename → the existing project bootstrap document, including CLAUDE.md only when it is the project's intended target
   Update Interface Reference signatures, callers, and line numbers. Verify every class/function line number against post-edit source. If analysis_project/code/ is absent, skip and recommend `analyze-project --mode code` once.
6. Confirm documentation writes with:
   git diff --stat -- $AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/code/ CLAUDE.md
   Report a documentation update only when the relevant diff proves it happened. If the expected diff is empty, re-read and correct the update.
7. Read pipeline_summary.md. Summarize its Decision Points in section 4.5. If no events exist, write the natural equivalent of "No autonomous decision events (clean run)" in the selected artifact language.
8. Synthesize causes, effects, and durable lessons; do not merely enumerate steps.

Report structure:

# Change Report: {task name}
- **Date**: {YYYY-MM-DD} | **Plan**: {plan path} | **Status**: ✅/⚠️/❌

## 1. Change Overview
## 2. Key Changes
   ### 2.N {category} — {file/module}
   - Change / Reason / Principle / Impact
## 3. Design Insights
## 4. QA Summary
## 4.5 Decision Record
## 5. Failed or Skipped Steps
## 6. Follow-Ups

Localize headings naturally. Preserve code identifiers, paths, commands, numbers, and technical terms where translation would reduce precision. Aim for 1–3 pages: about one page for at most five steps and up to three pages for more than twenty.

Return ONLY the report path and a one-line summary. Do not return the report body.
```

## Reconcile the Report

After the writer returns:

1. Read `final_report.md` once.
2. Cross-check it against the best available cycle evidence. When in-session, current orchestration context may help; in an isolated stage session, rely on the artifact files: checklist marks, `dev_logs/`, `test_logs/test_report.md`, `_internal/*_reviews/`, and `git log` or `git diff <safety-commit>..HEAD`. Artifacts remain authoritative.
3. Verify step counts, critical-finding rounds, test pass/fail counts, commit hashes, file counts, cited `file.py:NNN` locations, resolved versus pending follow-ups, and plan deviations. Correct report wording or data when evidence disagrees.
4. Apply one in-place polish pass on `{log_directory}/final_report.md` when the polish invocation contract selects it — `roles/units/editorial/polish.md` is the single source for that gating (currently plan-frontmatter `qa_level` standard+). The pass runs inside this stage's `editorial/report` unit, not as a separate dispatch.

Use this editorial instruction:

```text
Polish {log_directory}/final_report.md in place for natural phrasing, notation consistency, and readable cadence in the selected artifact language.
Apply your content-boundary contract: edit wording only.
```

The must-not-change list (change content, rationale, QA summary, decision record, numbers, `file:line` references, decision meaning) is owned by the `editorial/polish` content-boundary contract; do not restate it in the prompt.

Do not run a separate report QA pass. Reconciliation is the lightweight accuracy check; code and test assurance remain owned by earlier stages.

If `final_report.md` embeds or cites a generated spectrogram, require a passing
semantic-verifier entry in `test_logs/` plus its manifest and hash-current
representative PNG review. Cross-check every full-band, broadband, or
high-frequency statement against the registered evidence range. Do not relay a
complete status when this evidence is absent or failing.

## Relay

Before returning the brief, make the report and completion artifacts durable and return the canonical `final_report.md` source path to the capability owner. The report-stage worker does not independently invoke an artifact sink. After the report terminal, the owner evaluates the route-sealed optional artifact-sink extension from `WORKFLOW.md §0.2`: when available, offer the report through the app-neutral receipt contract; when unavailable, record `skipped/extension-unavailable`. Any publication semantics remain application-owned.

Return a concise 2–3 paragraph brief in the conversation language, not only a path. Include final status and commit hash, 3–5 concrete deliverables, any report/evidence discrepancy, and obvious next steps.

## Task

Generate report for: $ARG
