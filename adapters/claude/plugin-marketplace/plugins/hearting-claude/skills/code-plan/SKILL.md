---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: code-plan
description: "Use only when autopilot-code dispatches the planning and plan-check stage. Not for top-level user requests or primary capability routing."
argument-hint: "<task description> [--intensity direct|quick|standard|strong|thorough|adversarial]"
metadata:
  group: sub
  fam: sub
  invocation_class: parent-invoked
  modes: []
  blurb: "Analyze code, write a detailed implementation plan, and run the plan-check gate at the rigor derived from intensity."
  use_when: "Use only when autopilot-code dispatches the planning and plan-check stage."
  not_for: "Not for top-level user requests or primary capability routing."
---

# code-plan

Use the deepest eligible planning profile selected by the active adapter when cross-file reasoning or call-site analysis would benefit from it.

> **Stage-session entry (`standard+` dispatch, spec/stage-dispatch SD-2)**: Run in-session or as an isolated dispatch-depth-2 stage worker dispatched by the `autopilot-code` conductor. Inputs are the task description and `<artifact-root>/plans/`; never depend on prior-stage conversation. The write class is root `plan.md`, root `checklist.md`, an existing or explicitly requested audience-language companion such as legacy `plan_ko.md`, and `_internal/plan_reviews/`. The stage runs as the `plan/plan-author` unit (the node's unit); independent plan review is the sibling `plan-check` node dispatched by the conductor, not an in-stage delegation.

> **Language rule**: Follow the audience and artifact language contract in [arguments-and-decisions.md#language-rule](../autopilot-code/references/arguments-and-decisions.md). Write the canonical plan in the selected artifact language; do not generate a language mirror merely because the skill source is English or the conversation uses a particular language.

## Pre-Check

Search `$AGENT_ARTIFACT_OUTPUT_DIR/plans/` for a similar plan and branch on its frontmatter status:

- `active`: Ask whether to continue the active plan or create a new one. Do not proceed until that genuine choice is resolved.
- `done` or `failed`: Note it as a reference and create a new plan without pausing.
- `partial`: Read `failed_steps` and create a new plan covering only those failed or dependent steps without pausing.

Record any user-facing pause for `pipeline_summary.md` Decision Points.

## Delegate Planning

Run the `plan/plan-author` unit with this task, adapted only for the selected artifact language and known prior-plan state:

```text
Plan mode. Create a new implementation plan.

Task: {$ARGUMENTS}
Save canonical plan to: $AGENT_ARTIFACT_OUTPUT_DIR/plans/{YYYY-MM-DD}_{short-task-name}/plan.md
Save execution checklist to: $AGENT_ARTIFACT_OUTPUT_DIR/plans/{YYYY-MM-DD}_{short-task-name}/checklist.md
Artifact language: {selected audience or conversation language}
Date: {YYYY-MM-DD}
{If a done/failed/partial plan exists: "Reference previous plan: [path], status: [status]"}
{If partial: "Failed steps from previous execution: [list from plan frontmatter failed_steps]"}
```

Plan procedure, plan structure, and the single-line return contract are owned by the `plan/plan-author` unit persona; do not restate them in the prompt. The stage orchestrator receives only the unit's return line (`{path} -- {verdict}`); plan content stays in the file.

## Plan-Check Assurance

Derive verification rigor from the caller's `--intensity` and plan risk under [CONVENTIONS §1.1](../../core/CONVENTIONS.md#11-verification-rigor-tiers). Rigor does not select this stage: `code-plan` runs only after the caller chooses a durable `standard+` graph. `direct` skips it; `quick` uses a one-shot worker with an inline micro-plan and plan-check-lite.

Set `{log_dir}` to the directory containing root `plan.md`; for example, `$AGENT_ARTIFACT_OUTPUT_DIR/plans/2026-03-18_task/plan.md` resolves to `$AGENT_ARTIFACT_OUTPUT_DIR/plans/2026-03-18_task/`. Run `mkdir -p {log_dir}/_internal/plan_reviews` before independent review.

| Rigor | Plan-check action | Correction budget |
|---|---|---|
| `quick` | Normally unreachable; if invoked directly, run one fast sanity review or self-check | Record residual concerns; no repeated loop |
| `light` | One focused fast review or equivalent self-check | One pass only when an issue blocks execution |
| `standard` | One independent review for feasibility, missing steps, and concrete verification commands | At most one correction |
| `thorough` | Deeper or multi-axis review when explicitly selected by the graph | Up to two corrections after synthesizing reviews |
| `adversarial` | Thorough review plus failure-mode, security, and adversarial critique when the adapter proves availability | Explicit unavailable requests fail loudly; automatic escalation falls back to thorough |

After the `plan/plan-author` unit returns:

1. Check whether the selected graph requires independent review. Otherwise run an inline plan-check.
2. When independent review is required, the conductor dispatches the `plan-check` sibling node (unit `qa/plan-review`), which writes `{log_dir}/_internal/plan_reviews/round_{N}.md`. Use bounded separate reviewers only when the owner-worker graph and rigor select them.
3. If blocking issues exist, run **one batched correction** through the `code-refine` boundary: close every 🔴 of `round_{N}.md` together, including the follow-on gaps the review named. Then dispatch the re-review as round `N+1` with a `Round protocol` block naming the round number and `round_{N}.md`; it is a closure check (prior 🔴 closed? delta clean?), not a fresh independent pass. Never redispatch the full `plan` node for a correction. Budget: at most one correction at standard or the selected thorough/adversarial budget; do not loop solely because rigor is high.
4. If concerns remain after the budget, add them to the plan's risk or unresolved section and continue only when the caller can safely own the risk. When the last round still reported blocking findings, write `{log_dir}/_internal/plan_reviews/round_{N}.owner-closure.md` (frontmatter `verdict: closed-by-owner`, `node: plan-check`, `gate: code-plan-check`; body naming every blocking round's attempt id, the `round_{N}.md` it produced, and each finding's disposition) and complete `plan-check` with that record as `--evidence` against the last blocking attempt row; the gate refuses it while budget remains.

Record any user-facing pause, including active-plan ambiguity, for the pipeline summary.

## Optional Audience-Language Companion

The canonical `plan.md` should normally be sufficient because its prose already follows the selected artifact language while code identifiers and paths remain unchanged. Create or update a companion only when the user explicitly requests a second language, an external audience contract requires it, or an existing workflow depends on a legacy companion such as `plan_ko.md`.

When a companion is required, dispatch the `editorial/translate` unit (a conditional companion node appended at compile) to translate from canonical `plan.md` while preserving code identifiers, file paths, library names, step numbering, and semantics. Use the existing project naming convention for the output. Consult memory for writing preferences only when the acting agent judges the retrieved preference relevant; project and explicit audience requirements take precedence.

Report the canonical plan path, any requested companion path, a compact summary, and the QA verdict in the conversation language.

## Task

$ARGUMENTS
