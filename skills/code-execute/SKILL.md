---
# GENERATED METADATA — edit harness-manifest.json, then run tools/generate.py.
name: code-execute
description: "Use only when autopilot-code dispatches the implementation stage for an approved plan. Not for top-level user requests or primary capability routing."
argument-hint: "<plan name or path>"
metadata:
  group: sub
  fam: sub
  invocation_class: parent-invoked
  modes: []
  blurb: "Execute a plan step by step, delegate implementation to the development role, and record an execution log."
  use_when: "Use only when autopilot-code dispatches the implementation stage for an approved plan."
  not_for: "Not for top-level user requests or primary capability routing."
---

# code-execute

> **Stage-session entry (`standard+` dispatch, spec/stage-dispatch SD-2)**: Run in-session or as an isolated dispatch-depth-2 stage worker dispatched by the `autopilot-code` conductor. Resolve the plan path from arguments, read `plan.md` from disk, and never depend on prior-stage conversation. This is the only source-mutating stage. Its write class is source code, `checklist.md`, `dev_logs/`, `_internal/dev_reviews/`, and plan-frontmatter `status`. The stage runs as the compiled route's `dev/*` unit (sealed at compile); it never dispatches units itself, and any unforeseen narrow scaffolding uses an ephemeral native helper without unit semantics.

> **Plan resolution**: Treat [arguments-and-decisions.md#plan-resolution](../autopilot-code/references/arguments-and-decisions.md) as the single authority for resolving `$ARG` to a plan path.

> **Language rule**: Follow the audience and artifact language contract in [arguments-and-decisions.md#language-rule](../autopilot-code/references/arguments-and-decisions.md). Do not infer a fixed execution-log or report language from this skill file.

## Commit Messages

- Safety checkpoint: `chore: Safety checkpoint before {plan-name} execution`
- Success: `{type}: {description}\n\n{bullet list of key changes}`, where type is `feat`, `fix`, `refactor`, or `chore`

## Git Safety Checkpoint

Before source changes, establish a recoverable working state.

0. Run the [git working-state preflight](../../core/OPERATIONS.md#59-git-working-state-preflight) before the safety checkpoint and again before every success commit.
   - Stop and report an active merge, rebase, or cherry-pick, or a detached HEAD. Do not abort it automatically.
   - Warn about a dirty entry state, an upstream-ahead branch, or the same branch checked out in another worktree.
   - Record entry `HEAD`. Before committing, stop if `HEAD` changed unexpectedly or a new `MERGE_HEAD` appeared.
1. Run `git fetch && git pull` when the active workflow authorizes remote synchronization.
   - If the pull creates conflicts, run `git merge --abort`, report the conflict, and stop execution.
2. Run `git status` and inspect all uncommitted changes.
3. If changes exist, first check `git rev-parse -q --verify MERGE_HEAD` and `$(git rev-parse --git-dir)/rebase-merge` or `rebase-apply`.
   - If any merge or rebase is active, do not run `git add -A && git commit`; stop and report the state.
   - Otherwise, create an accurately described checkpoint with `git add -A && git commit` only when every included change is understood and in scope. Stop instead of sweeping unrelated user changes into the checkpoint.
4. Run `git rev-parse HEAD`, save it as `$SAFETY_COMMIT`, and persist it in the checklist header.

## Initialization

1. Read the resolved plan at `$ARG`.
2. Set `{log_dir}` to the task root two levels above `plan.md`.
   - Example: `<artifact-root>/plans/2026-03-18_refactor_engine/plan.md` → `<artifact-root>/plans/2026-03-18_refactor_engine/`
3. Detect resume state.
   - If `{log_dir}/checklist.md` contains `[x]`, `[FAIL]`, or `[SKIP-DEP]`, update its `Safety commit:` line, skip completed steps, and continue at the first `[ ]` step.
   - Otherwise create a fresh checklist.
4. Run `mkdir -p {log_dir}/dev_logs {log_dir}/_internal/dev_reviews` and write `{log_dir}/checklist.md` directly from the canonical plan:

   ```text
   Safety commit: {$SAFETY_COMMIT}

   Phase A: [description]
     [ ] Step 1: [file] — [what to change]
     [ ] Step 2: [file] — [what to change]
   Phase B: [description]
     [ ] Step 3: [file] — [what to change]
   ```

Use the checklist as the sole orchestration tracker. Keep plan files immutable during execution. Mark each step `[x]`, `[FAIL]`, or `[SKIP-DEP]`.

## Execution Rules

- Read the checklist before every step and execute steps in dependency order.
- Implement each step as the node's `dev/*` unit (default `dev/backend`; the compiled route seals the member) with concrete file and change instructions.
- Give each worker `{log_dir}/dev_logs/` and require a step log such as `dev_logs/step_01_model_py.md`.
- Run independent steps in parallel when the active workflow supports in-session parallel delegation.
- After a successful step, mark it `[x]`. The phase review owns syntax and import verification.
- Continue until every processable step has a terminal checklist mark.

## QA Scaling

Plan-frontmatter `qa_level` overrides automatic selection for every phase. Otherwise classify each phase by scope.

| Level | Auto-detect condition | Review action |
|---|---|---|
| **Quick** | Inherited `--intensity quick` direct invocation | One fast reviewer, one pass; log major findings without rollback retry and propagate them to `pipeline_summary.md` Decision Points |
| **Light** | At most 3 mechanical units in one variant | One fast reviewer |
| **Standard** | 4–10 units or logic changes in one module | One deep reviewer |
| **Thorough** | More than 10 units, cross-module or cross-variant work, or architectural change | Two or three reviewers in parallel: correctness/deep, consistency/fast, and safety/deep when more than 20 files are involved |
| **Adversarial** | Cross-variant or shared-module architectural work and an external adversary is available | Thorough review plus one external adversary in parallel |

For Thorough review, cover bugs, logic, and signature mismatches; naming, conventions, and dead code; and tensor-shape or `None` edge cases when relevant. Write each result to `_internal/dev_reviews/phase_{NN}_{focus}.md` and address every critical finding.

Before selecting Adversarial, use the active adapter's external-adversary availability check. An explicitly requested unavailable adversarial run fails loudly; automatic escalation falls back to Thorough and records the fallback.

## Change Logs and Phase Review

Each implementation step writes its own step log under `{log_dir}/dev_logs/`. Record exact old/new edits and a `Decision:` field explaining the rationale.

At the end of each phase:

1. Select the review level from plan override or phase scope.
2. Record the phase's step-log paths, `{log_dir}`, changed source files, focus, and output path in the stage artifact; the conductor dispatches the `impl-review` sibling node (unit `qa/code-review`) against them per the compiled route.
   - Light/Standard: one reviewer.
   - Thorough: parallel correctness, consistency, and optional safety reviewers using the portable fast/deep profiles above.
   - Adversarial: the Thorough batch plus one external adversary.
   - Require every reviewer to write its report and return only the path and a one-line verdict.
3. Read the review files and act:
   - Advisory-only findings: record them in the checklist and continue.
   - Critical findings that do not require rollback: re-enter the execute boundary **once** and fix every 🔴 of the review together (plus the follow-on gaps it named); then re-review to `phase_{NN}_fix{M}.md` as a closure check that names the round and the prior review path — never a fresh full audit, and never a full `execute` redispatch. Treat a critical finding still open after that single batched fix as major.
   - Major critical finding: roll back the phase automatically and continue under the family autonomy policy.
     1. Restore every `old_string` recorded in the phase logs.
     2. If rollback fails, read `$SAFETY_COMMIT`, run `git checkout .`, mark all steps `[FAIL]` with `Reverted by git checkout due to rollback failure in Phase N`, and stop at Final Report.
     3. If rollback succeeds, mark the phase steps `[FAIL]` with the reason.
     4. Continue to the next independent phase; mark dependent steps `[SKIP-DEP]`.

Record every rollback as a Decision Point for the pipeline summary. For plans with at most three steps, skip phase grouping and review once after all steps.

If every step ends as `[FAIL]` or `[SKIP-DEP]`, roll source changes back to the safety commit without touching the artifact root:

```text
git diff --name-only $SAFETY_COMMIT HEAD -- ':!<artifact-root>'
git checkout $SAFETY_COMMIT -- <changed files>
git status
```

## Critical Safety Rules

- Signature-change safety (find every call site, update every caller, inspect implicit contracts) is owned by the dev unit contract — the `dev/*` unit common rules and `roles/units/dev/refactor.md` — and by `plan/plan-author` call-site coverage; hold workers to it through phase review instead of restating it per step.
- If a step causes cascading errors beyond plan scope, mark it `[FAIL]`, roll it back from the step log, and continue only with independent steps.
- Do not change code outside plan scope except where a required contract change makes a caller update unavoidable.

## Plan Status

- All steps `[x]` → set plan frontmatter `status: done`.
- Some `[x]` and some `[FAIL]`/`[SKIP-DEP]` → set `status: partial` and list step numbers in `failed_steps`.
- No `[x]` → set `status: failed` and list every step number in `failed_steps`.

## Final Report

Read the checklist and report the overall verdict in the selected audience language. List only `[FAIL]` and `[SKIP-DEP]` steps with reasons; when all steps succeeded, a concise success verdict is sufficient. End by recommending `code-test <plan file path>` for functional verification. In a standalone invocation, this is the only user-facing progress report.

## Task

Execute the plan at: $ARG
