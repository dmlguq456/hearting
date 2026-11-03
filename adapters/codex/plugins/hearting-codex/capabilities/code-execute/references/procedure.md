# code-execute

> **Stage-session entry (`standard+` dispatch, spec/stage-dispatch SD-2)**: Run in-session or as an isolated dispatch-depth-2 stage worker dispatched by the `autopilot-code` conductor. Resolve the plan path from arguments, read `plan.md` from disk, and never depend on prior-stage conversation. This is the only source-mutating stage. Its write class is assigned source files (including tracked docs/configuration examples), `checklist.md`, and `dev_logs/`. Source ownership is defined by scope and tracked content, not filename extension. A commit-expected single session commits all validated owned source paths; artifact shadows, runtime state, and unrelated dirty files are excluded. The stage runs as the compiled route's `dev/*` unit (sealed at compile); it never dispatches units itself, and any unforeseen narrow scaffolding uses an ephemeral native helper without unit semantics.

> **Plan resolution**: Treat [arguments-and-decisions.md#plan-resolution](../../autopilot-code/references/arguments-and-decisions.md) as the single authority for resolving `$ARG` to a plan path.

> **Language rule**: Follow the audience and artifact language contract in [arguments-and-decisions.md#language-rule](../../autopilot-code/references/arguments-and-decisions.md). Do not infer a fixed execution-log or report language from this skill file.

## Slice Mode

If the assignment is a sub-session (phase brief, fixed files, `stage_authority=0`), complete all processable in-scope items, edit only the fixed worktree files, write the report to the artifact path assigned by the phase brief or dispatch prompt, and run the narrow verify command as a required check plus any additional checks named in the brief; a check this environment cannot run is a `handed-over:` line in the report, not an unfinished item. The report is separate from source `fixed_files`. A slice makes no git writes (no add, commit, checkout, restore, stash, reset), reads git state with `GIT_OPTIONAL_LOCKS=0`, and leaves `checklist.md` and the dev log to the owner. If a needed source file is outside the list, finish independent in-scope work and report the exact path, change, and unfinished item without widening the fence.

## Source state and commit ownership

Use the runtime-provided worktree and preserve its entry HEAD and existing dirty
state. Do not fetch, pull, create a safety commit, or commit per phase. A
commit-expected single-session stage commits only its validated owned paths;
a sub-session slice makes no Git writes. The route/runtime owns completion and
recovery mechanics. Record the changed paths and handoff in the declared artifact
locations.

## Initialization

1. Read the resolved plan at `$ARG`.
2. For a registered stage, write `checklist.md` and step logs only to the runtime-declared output locations under `$AGENT_ARTIFACT_OUTPUT_DIR`, within the node's declared outputs and write scope. For a standalone invocation, keep them in the plan's own folder.
3. Detect resume state.
   - If the declared `checklist.md` contains `[x]`, `[FAIL]`, or `[SKIP-DEP]`, skip completed steps and continue at the first `[ ]` step.
   - Otherwise create a fresh checklist.
4. Write `checklist.md` directly from the canonical plan, creating only its parent directory when needed:

   ```text
   Phase A: [description]
     [ ] Step 1: [file] — [what to change]
     [ ] Step 2: [file] — [what to change]
   Phase B: [description]
     [ ] Step 3: [file] — [what to change]
   ```

Use the checklist as the sole orchestration tracker. Keep plan files immutable during execution. Mark each step `[x]`, `[FAIL]`, or `[SKIP-DEP]`.

## Execution Rules

- Read the checklist before every step and execute steps in dependency order.
- Implement each step within this node's sealed `dev/*` unit with concrete file and change instructions.
- Record each step in the declared `dev_logs/` location, such as `step_01_model_py.md`.
- Run the focused checks named by the plan for each step. Add or repeat a check only when its result could change the step's verdict or an open risk; do not rerun a passed check on unchanged code. The selected sibling review node owns implementation review.
- Your verdict covers your steps and the checks your environment can run; the leg's wider verification belongs to `code-test` and the owner. A focused check that ran and failed is a step failure, not a handed-over check. A check this environment cannot run (sandbox, network, host or hardware limits) does not fail its step: write `handed-over: <command> (reason: ...)` in the step log, finish the step, and leave the check to `code-test` (the owner when no test stage follows).
- After a successful step, mark it `[x]`.
- Continue until every processable step has a terminal checklist mark.

Record each edit in the existing step log with its file path, old and new content, edit order, enough location or surrounding context to distinguish it, and a `Decision:` entry.

## Failure Recovery

For phase failure, total failure, cascading errors, failed debug verification, or an exhausted test retry, use the shared content-ownership recovery procedure in `capabilities/code-execute.md#portable-procedure`. Recovery covers only logged edits the failed work actually made within its assigned source scope. Compare the current file and context with the existing logs, then reverse only clearly attributable edits in reverse order. A matching string or changed path alone does not prove ownership.

Preserve unrelated edits in the same file, earlier successful work, staged content, untracked files, and any ambiguous edit. Do not fall back to whole-file or whole-tree restore, `checkout`, `reset`, `stash`, `clean`, commit revert, or index rewrite. Reverse a created, deleted, or renamed path only when every affected path and surviving content is clearly attributable to the failed work.

Keep the original command result and partial state; never replay a failed command automatically. Record what was recovered and what remains, why any edit was ambiguous, the observed index/worktree state, and checks skipped because they depend on unresolved edits. Do not claim complete recovery. Continue only with independent work; otherwise stop through the existing failure handoff.

Registered owners and workers leave route closure to the runtime. Only the explicit direct/inline and legacy recovery paths in `core/WORKFLOW.md §0.5` are exceptions; they do not authorize a general manual close/finalize procedure.

## Assurance

Use the intensity and selected stage graph as the assurance authority. A plan's
`qa_level` is descriptive and does not override the graph. Do not choose extra
reviewers or passes from file count.

## Handoff

Record each implementation step in the declared dev log with the changed files,
commands and outcomes, deviations, unfinished items, `handed-over:` checks, and relevant risks. The
selected sibling review node owns implementation review; do not dispatch it or
require an additional pass from this procedure. Keep correction and recovery
within the compiled route and preserve unrelated work.

## Final Report

Read the checklist and report the bounded handoff in the selected audience language. List only `[FAIL]` and `[SKIP-DEP]` steps with reasons; when all steps succeeded, a concise success verdict is sufficient. For a registered stage, the owner selects the next node. For a standalone invocation, end by recommending `code-test <plan file path>` for functional verification; this is the only user-facing progress report.

## Task

Execute the plan at: $ARG
