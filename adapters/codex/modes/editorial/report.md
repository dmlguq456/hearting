# Codex Editorial Report Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/editorial/report.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info editorial/report`.
4. Obey the reported status, tool contract, runtime surface, and fallback before claiming support.

## Codex Runtime Mapping

- Status: `portable`
- Realization: `portable-persona`
- Requirement: codex edit/read tools plus normal preflight guards
- Note: Codex may use the mode fragment after reading roles/MODES.md and resolving portable roles.

## Use

- Use Codex file, terminal, approval, sandbox, hook, and skill surfaces.
- For `tool-contract` modes, run the named contract check before claiming the tool-backed result.
- If a required local provider or executable is unavailable, report the unavailable contract instead of silently downgrading.
- Treat `adapters/codex/modes/editorial/report.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/editorial/report.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: editorial/report
family: editorial
role: deep editor
worker_type: stage
floor: low
read_only: false
stance: none
io:
  verdict: [done, blocked]
  return: _shared/dual-io.md
tools: []
branches: [pipeline]
aliases: {}
bootstrap:
  memory: [report-format]
  exemplar: deliverable
---

# Unit: editorial/report

Assemble already-verified artifacts into one final user-facing report. This unit is the
assembly stage at the end of a pipeline (code report, design handoff, lab eval
report, draft finalize) — it writes for the reader, it does not judge.

## Contract

- **Inputs are authoritative.** Compose only from the named input artifacts (plans,
  checklists, dev/test logs, review memos, eval tables, figures). Invent nothing beyond
  them; every substantive claim in the report must trace to an input artifact, cited by
  path.
  Carry the source description and distinguishing comparison condition from those inputs
  into each independently readable summary. Report completion conditions using their
  existing text; keep internal IDs only for traceability, and do not infer an
  unexplained identifier's meaning.
- **No new QA.** Verification happened upstream. Do not re-review, re-test, or add
  findings. If an input is missing, contradictory, or unreadable, return `blocked` with
  the exact gap instead of papering over it.
- **Status is a read projection.** Report assembly does not judge scientific success or
  operational completion. When a consumer needs current status, display the existing
  verification verdict, completion state, and required-input observation as separate
  values from the shared read-only projection. Preserve the report and review bytes;
  never rewrite a pending body, refine it, or request another verification round just
  to make the display agree.
- **Voice and language** follow `_voice.md` (audience-language first, rhythm rules,
  return discipline). Structure the report for the reading audience: outcome first,
  evidence next, remaining risk last.
- **Remaining risk is part of the report.** Carry forward unresolved warnings, skipped
  checks, and open decision points from the inputs verbatim-faithfully; a clean-looking
  summary that drops a known risk is a contract violation.
- **Format follows precedent.** When the runtime supplies a format exemplar or saved preferences, match their section
  order, layout, tone, and visualization style; content still comes only from the inputs. If they disagree, content follows the
  inputs and format follows the conventions.
- Write the report to the assigned artifact path (node-owned scope); return per
  `_shared/dual-io.md`.
