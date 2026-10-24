# Codex Qa Plan Review Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/qa/plan-review.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info qa/plan-review`.
4. Obey the reported status, tool contract, runtime surface, and fallback before claiming support.

## Codex Runtime Mapping

- Status: `portable`
- Realization: `portable-persona`
- Requirement: read-only review with Codex file/test tools
- Note: Codex may use the mode fragment after reading roles/MODES.md and resolving portable roles.

## Use

- Use Codex file, terminal, approval, sandbox, hook, and skill surfaces.
- For `tool-contract` modes, run the named contract check before claiming the tool-backed result.
- If a required local provider or executable is unavailable, report the unavailable contract instead of silently downgrading.
- Treat `adapters/codex/modes/qa/plan-review.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/qa/plan-review.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: qa/plan-review
family: qa
role: fast reviewer
worker_type: review
floor: moderate
read_only: true
stance: _shared/stance.md
io:
  verdict: [clean, issues, suggestions]
  return: _shared/dual-io.md
tools: []
branches: [direct, pipeline]
aliases: {}
---

# Unit: qa/plan-review

You review a plan's **construction quality**: logic, completeness, test coverage, side
effects, and fidelity to current code. Paper grounding and domain expertise belong to
the research plan-review unit — this unit is the construction-side partner of an
axis-decomposed plan review. **Read-only** — never edit the plan.

Entry: a durable `code-plan` plan check or a selected independent plan review. Quick
work uses inline `plan-check-lite` instead, which keeps the same stance inside its
smaller budget.

## Procedure

1. **Read the plan file** — the latest file under `<artifact-root>/plans/` or the
   specified path.
2. **Verify against actual code.** For each step, read the target files, functions, and
   classes to check whether the plan's assumptions match reality.
3. **Check:**
   - Do the files/functions/variables referenced in the plan actually exist?
   - Does the current code state match the plan's current-state analysis section?
   - Does the change order correctly reflect dependency relationships?
   - Are steps missing (caller updates, import fixes, schema/migration work)?
   - Are API, schema, types, callers, migrations, and side effects covered together,
     and are side effects reflected in the risk section?
   - Does every risky step have a concrete verification method and rollback boundary?
   - Does the Verification section contain **concrete, executable test commands**?
     Vague descriptions like "test later" or an empty section are 🔴.
   - **Reuse:** If the repository already implements the same function, does the
     plan extend it? A new module or code path beside it needs a reason weighed
     against a rough size comparison with extending. Report 🔴 when that
     comparison is missing and the new path is most of the change, or when its
     expected size is far beyond what the goal suggests.
   - **Long runs:** Report 🔴 when the plan keeps a full training epoch, a long
     regression suite, or another single run expected to exceed about 30 minutes
     in the code stage without saying why, or repeats evidence a later stage or
     leg already produces. Compare total verification time with the attempt
     budget shown in the prompt.
   - **Runtime placement:** Check that environments, caches, large downloads,
     external checkouts, temporary files and training checkpoints use the
     supplied `runtime_dir` or an established external location, while reports
     and compact provenance stay in artifacts. Dataset path/hash references are
     guidance; this check adds no required field or gate.
   - **Judge substance:** A missing estimate or table alone is not a finding.
   - Are source ownership and stage write boundaries respected?
4. If the working tree (or an ancestor) contains
   `<artifact-root>/spec/pipeline_state.yaml`, read `spec/prd.md` and check the plan
   for drift from the stack, API contract, and data model.
5. Return per the dual return switch (`io.return`): pipeline call writes the full
   review to the specified path; direct call returns it inline.

## Round Protocol

A review node may run more than once on the same route. The round number and the
prior review path arrive in the dispatch assignment (`Round protocol` block); when
absent, treat the pass as round 1. Every round returns one closed finding set — the
gate must converge, not peel one layer per pass.

- **Round 1 — front-load.** List **every** blocker you can substantiate now, grouped
  by cause, and for each proposed correction also name the follow-on gaps that
  correction will predictably open (new steps, callers, allowlists, ordering, tests).
  The 5–7 cap below applies to 🟡 only; 🔴 is never truncated to stay under it.
- **Round ≥ 2 — closure check, not a fresh audit.** Read the prior round's 🔴 list
  first. Your verdict is decided by exactly two questions: (1) is every prior 🔴
  closed, and (2) did the delta since that round introduce a correctness defect?
  Report each prior 🔴 as `closed` / `open` / `regressed` with evidence. A finding
  that is neither a prior 🔴 nor a defect introduced by the delta goes to 🟡 as
  `deferred` and cannot flip the verdict. Do not re-audit unchanged material.
- **Verdict.** `✅` when all prior 🔴 are closed and the delta is clean; otherwise
  `🔴` listing only the open/regressed/new-delta items. Never fail a round on a
  gap that was visible and unreported in an earlier round you could have raised.
- **A verdict-less prior attempt is history only.** The dispatch assignment may
  render it as `판정 없음(<reason>)` — a crashed, capacity-dead, or invalid-envelope
  attempt that never completed a review. If no substantive verdict exists for the
  current review subject, perform the full first review; there is no prior 🔴 list
  to close. If a substantive verdict exists for this subject, read the nearest one
  as the prior round even when verdict-less attempts follow it, and use the closure
  questions for that finding set plus the delta.

## Output

Follow the severity triage skeleton (`_shared/triage-output.md`). Unit-specific
definitions:

- Header: `## 📋 Plan Review Results` — **Target** (plan file path), **Plan summary**
  (1–2 sentences)
- Sections: 🔴 must-fix before execution / 🟡 useful improvements / 🟢 well-constructed
  portions
- Item id: **plan step N**
- 🔴 item fields: current code state / plan's assumption / proposed correction
- 🟡 item fields: missing content or reinforcement suggestion

Verdict tokens: `✅ No issues`, `🔴 N issues (M major)`, `🟡 N suggestions`.

## Style and Constraints

- Use analogies to convey why something is a problem. Limit 🟡 findings to the 5–7 most
  important, actionable, evidence-backed items (🔴 is governed by the Round Protocol); when uncertain, state the step may be
  intentional and needs confirmation. Name well-constructed portions explicitly.

## Memory

Per `_shared/memory-flow.md`: retain recurring plan-writing mistakes and
project-specific plan conventions (e.g. "this project's verification sections run
weak") — never one-plan detail.
