# autopilot-code

Read only this file for owner procedure. Run only the runtime commands your Route steps and the latest receipt print; never assemble a runtime command yourself. Read another reference or `core/` only when a receipt or Route step names that section. Named task inputs and artifacts remain inputs to read; policy cross-references below are lookup locations, not additional required reading.

Code-work entrypoint. Detect spec context and close the `plan → execute → test → report` loop at the selected intensity. This file defines routing and stage contracts; load another reference only when a Route step or receipt names its section.

## Quick Contract

- Standalone default output: `<artifact-root>/plans/<date>_<slug>/`. Routed work uses the selected graph's declared outputs. `direct` creates no durable plan; `quick` uses a micro-plan; plan/checklist belong only to selected planning work.
- When a spec exists, emit a one-line `spec-significance` judgment before editing code. For spec-significant work, the plan or code owner updates the affected component PRD before implementation through the same route: run `spec-transaction --node execute` inside `code-execute` for standard+, use the one-shot transaction for quick, and apply it inline for direct. Only an explicit user deferral skips the update.
- Before spec triage, edits, and every durable write-back or commit, check git/worktree state and remember entry HEAD. Merge, rebase, cherry-pick, or detached HEAD means STOP and report; never abort automatically. An unexpected HEAD change or new MERGE_HEAD before commit also means STOP. Another worktree on the branch, upstream ahead, or unrelated dirt means WARN; preserve unrelated edits. Non-git and single-checkout contexts pass harmlessly.
- Do not parallelize QA at every stage. Scale `plan-check` and final `code-test` from the rigor derived from intensity (CONVENTIONS §1.1).
- Follow an explicit artifact or audience language for user-facing reports. Otherwise, use the conversation language.

## Reference Index

| File | Load policy | Content |
|---|---|---|
| `context-and-guards.md` | Only when a receipt or Route step names it | Artifact, spec, and git guards; spec-mode detection; design/app/library/API/CLI/research boundaries; experiment-ready input; invocation routing |
| `arguments-and-decisions.md` | Only when a receipt or Route step names it | Argument parsing, defaults, active/partial/complete plan handling, and plan-path resolution |
| `dev-pipeline.md` | Only when a receipt or Route step names it | Stage orchestration, plan check, retry behavior, and `analyze-project` update |
| `debug-audit.md` | Only when a receipt or Route step names it | Debug diagnosis and fix flow; audit fan-out and autofix workflow |
| `pipeline-summary-safety.md` | Only when a receipt or Route step names it | Summary template, terminal-state reporting, and common safety rules |

## Argument Shape

`--mode dev|debug|audit <task/plan/error description> [--from <step>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine]`

Defaults:

- `--mode`: default to `dev`; infer `debug` when the request is centered on an error log or traceback.
- `--intensity`: choose from scope and risk. Use `direct` for a one-line task, `quick` for a small scoped change, and `standard+` for multi-stage or multi-file work. Verification rigor is derived from intensity rather than selected separately (CONVENTIONS §1.1). Quick shortens procedure, not the requested results: preserve the approved endpoint and assigned scope, and do not substitute a small example or concise report for explicitly requested investigation, deliverables, or verification.
- `--user-refine`: enable only when the user explicitly requests a review or note-taking pause.

## Default Stage Graph

The table is a preset. Execute the compiled graph as selected; do not add omitted
plan, review, or implementation nodes or their artifacts. Profile defaults also
yield to explicit choices (`compose --profile light` needs no demand JSON).

| Intensity | Graph | Durable artifact | Review policy |
|---|---|---|---|
| `direct` | intake → produce → sanity/report | None | No independent QA |
| `quick` | intake → orient-lite → micro-plan → plan-check-lite → produce → verify-lite → report | None by default | Inline check with 3-4 questions |
| `standard` | frame (2 legs, depth-0-launched before this route starts) → code-plan → plan-check → code-execute → impl-review → code-test → code-report | Required | Two frame legs already ran and joined at depth-0 (`core/WORKFLOW.md` frame procedure); the owner only consumes the resulting `intent.md` |
| `strong` | frame (2 legs, as above; not widened at strong+) → 2-way plan → plan-check arbitration → execute → 2-way implementation review → test → report | Required | Spend cheap asymmetric breadth early, then converge through the declared arbiters; every group remains cross-harness-first. `execute` carries the sealed subdivision permission; if the plan declares slices, use its printed slices command before an ordinary single-session start |
| `thorough`/`adversarial` | strong graph + 3-way plan and 3-way implementation review + deeper rigor | Required | Use the registry-declared third implementation-risk/failure-mode legs; never invent or widen a group outside the sealed route |

**`standard+` dispatch**: Run every durable compiled node as dispatch depth 2, using its printed Route step. A sealed `parallel_group` starts through that command as one transaction, never member-by-member. The dispatch-depth-1 conductor passes artifact paths, reads only verdict/status, and yields while the adapter supervisor joins the exact child batch. Cross-harness means at least two harness families across the group; model-profile and perspective asymmetry are independently sealed and reported. Use `dispatch-wait` only for an explicit `poll-fallback`. Only `direct` and `quick` keep micro-stages inline. The owner never sets `AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN` or any other `AGENT_DISPATCH_*` lifecycle override, and never reads harness utility sources looking for one — the launcher evidence-binds that assertion to the launcher's own observed scope, and a registered headless owner inside a tool sandbox cannot make it. If the runtime hands a foreground `dispatch-batch` call to the background, that call has not failed: follow the latest receipt's wait instruction. Do not switch lifecycle.

## Owner stage flow

1. Follow only the selected Route steps: plan → selected plan-check → execute → selected implementation review → test → report; omit absent nodes.
2. Start each stage with its printed command. Read its artifact/verdict and latest receipt; never reconstruct registry, attempt, retry, completion, or sub-session flags.
3. Yield on `parent_next=end-turn` with `runtime_wait: registered-children`; run a printed `parent_next_command` once for `bounded-wait`.
4. Registered runtime-v1 child completion is written by the completion join. Use a printed completion command only for declared inline stages or sub-session manifests.
5. After FAIL, batch all blocking findings into one bounded correction, then the selected independent closure review; never rerun the whole stage for one finding.
6. Keep the sealed retry and review-round ceilings. An exhausted budget follows its receipt's next action; do not widen it or treat a refusal as permission for inline execution.
7. A code-test failure allows at most one pipeline retry outside quick: correct the affected implementation and repeat the selected implementation review and tests. Quick reports failure without retry; a second failure preserves unrelated edits, records recovery/unknowns, and stops before code-report.
8. A printed `single-session-required` means one ordinary session; a printed slices/sub-session command selects that form. Neither changes stage authority or the one gate per stage.
9. Keep stage and review scopes and independent review intact. Read any named policy section only when a Route step or receipt requests it.
10. Write the selected report and final handoff; the shared controller closes runtime-v1 routes. Use a printed finish command for inline or legacy work.

### Rules carried by the owner

- At thorough+, after every selected `plan-check` leg has its completion marker, read all findings and write a merge memo with frontmatter `auxiliary_findings_considered`: exactly one disposition per realized auxiliary leg (for example, adopted or rejected). Run the printed `arbitrate` command. If the resolved arbiter is a route node (`auxiliary-arbiter-is-node:<node>`), that node records the dispositions in its own completion evidence instead. At strong+, read every selected implementation-review verdict, adopt the stricter verdict, union blocking findings, and record the merge before continuing past the existing gate.
- An execute result of `failed` requires a failed `pipeline_summary.md` stating recovered, remaining, and ambiguous edits; stop before test or code-report. Route review/test findings through the bounded correction sequence; never inline hotfix. Retries run in place: never `git reset --hard`, and never roll back unrelated or ambiguous contents. On a second test failure, reverse only clearly attributable implementation edits, record residual state and skipped checks, and stop before code-report.
- Shared singleton writes (`pipeline_summary.md`, `pipeline_state.yaml`, and spec state) hold the existing artifact-root `.pipeline-lock` OS advisory lock across the write and release it immediately afterward. Use the existing transaction when its command is printed; lock exit 3 stops the write and reports the holder. Never delete a held lock. Cycle-separated plan files do not need it.
- After the report (also after debug), refresh `analysis_project/code/` using execute's recorded `launch_head` and dev logs to identify changes. A: at most three files in one module, only a small function/class/signature/rename/logic change affecting Interface Reference — update that reference and at most one short Role/body line directly. B: four or more files, module/model-folder addition/deletion/rename, broad cleanup, config mechanism, layer, train/eval, seed, or reproducibility changes — invoke incremental `analyze-project` code mode with `--skip-qa`. Ambiguity uses B. Report the number of analysis artifacts changed; skip only on explicit `분석 자료 update skip` or `--no-analyze-update`.
- Detect all spec modes from `spec/pipeline_state.yaml`'s `mode` array. App changes consume design tokens and selected design critique, leave destructive DB migration to an explicit decision, and account for CI/CD; library changes check semver/exports/examples; API changes check contracts/auth/rate limits; CLI changes check options/I/O/exit codes; research changes update reproduction/config/metric evidence. Only new underspecified dev work with irreversible choices needs the existing one-round intake; debug, resumes, and a complete spec skip it.

## Mode Routing

- `dev`: add features, refactor, or implement. `direct|quick` shorten the full pipeline; `standard+` uses framing, `code-plan`, the compiled graph's selected `plan-check`, optional `code-refine`, `code-execute`, selected `impl-review`, `code-test`, and `code-report`.
- `debug`: diagnose the root cause before planning a fix. Proceed when the cause is clear; ask for a choice only when materially different causes remain plausible.
- `audit`: inspect a codebase or app comprehensively and apply low-risk fixes. Keep review fan-out read-only; make and verify changes in a worktree based on current HEAD before harvest.

## Critical Gates

Before committing assembled source, the integration owner runs `python3 tools/generate.py`, updates the projected/deferred census for new `utilities/*` or `tools/*`, then runs `python3 tools/generate.py --check` and `./tools/check-adaptation-boundary.sh`. Reuse applicable results when the assembled source and projection inputs are unchanged; later review, test, report, publication, or parent handoff does not require another run. Stage generated output with source; do not edit projections manually.

The boundary check covers gaps outside `generate.py`: Claude counterparts are
automatically filled only for `loops/`, `scaffolds/`, `tools/memory`,
`tools/install`, `tools/integrations`, and `tools/fleet`, so a new top-level
`tools/<file>` still needs its counterpart. Existing git-state, commit, push,
and pre-push guards remain active. Normal hooks may run their declared checks;
record their results rather than bypassing or manually triggering them early.

1. Resolve the artifact root (CONVENTIONS §5.1).
2. Run git-state preflight and remember starting `HEAD`.
3. If `spec/` exists, read `spec/prd.md` and emit `spec-significance`.
4. Choose stage graph from intensity before QA.
5. Before source write-back or commit, re-run git-state preflight.
6. On any terminal state, write `pipeline_summary.md` before reporting to the user.

> Treat the [Reference Index](#reference-index) as the single source for reference files, load points, and contents.

## Pre-Owner Direction Gate (SD-123)

The direction gate is finished before you are launched. The frame legs run in
the depth-0 bootstrap layer after route binding and producer begin, before
owner launch. Depth-0 joins them, builds
`shards/frame/frame-summary.json` and the interview `shards/frame/interview.json`
(schema `frame_interview_v1`, SD-129), asks the user, releases `frame-review`,
and renders `shards/frame/intent.md` under the inherited cycle's
`AGENT_ARTIFACT_OUTPUT_DIR`. Both frame legs run `top` (a pin or explicit profile
wins). A route composed from an approved proposal (`--route-plan`) has no frame
nodes of its own: the direction was settled in its frame route's interview, and
`intent.md` arrives as an input all the same. Legacy sealed route bytes remain
unchanged; consume the gate declarations of your assigned route.

1. **You receive `intent.md`'s path as an input.** Read it first. Do not raise
   `frame-review`, do not call `await-release` for it, and do not render the
   intent yourself — that work is already done, and a registered owner's
   release is refused typed anyway (`gate-release-authority-refused`, the gate
   is sealed `release_authority=depth-0` at the raise).
2. The runtime supplies the released understanding and actual answers in each
   owner and stage prompt, even without a plan node. Use stage assignments for
   stage-specific detail, not to reconstruct this context. Keep every output
   within the agreed scope and include the decisions relevant to that stage.
3. Your first work node is fenced by the released gate: every launch surface
   refuses to start it while the entry gate is unreleased
   (`human-gate-unreleased` / `human-gate-not-raised`, defect M). If your route
   seals `frame-review` (its Route steps name the entry gate) and arrives with
   no `intent.md`, stop and report it rather than proceeding — an owner that
   plans without the agreed intent is exactly the silent path this gate exists
   to close. A route whose Route steps say no entry gate is sealed has no
   `intent.md` by design; proceed with the Assignment. A `revise` or `stop` decision is spent before the
   route starts, so neither consumes your `code-refine` retry budget.
4. `confirmation.mode` (`profiles/dispatch-defaults.yaml` /
   `utilities/dispatch-defaults.py`, default `hybrid`) governs the ordered
   pair — blocking direction gate first, route notice after — read it via
   `query_confirmation_mode`, never hardcode `hybrid`. `core/WORKFLOW.md` §0.4
   owns the user-facing card text.

## Artifact Producer Lifecycle (W7C)

The shared completion controller owns registered route preparation and closure.
The launcher supplies the exact cycle context. Stage workers use their sealed
node outputs and write scope, finish the declared work, and return the report's
final handoff. After exact PASS and process cleanup, the controller
completes the workflow, closes the route and seals the cycle using one durable
transaction. An interruption retains that transaction and sends a recovery
notice; it does not convert PASS to failure or require a new model execution.

For inline `direct` work and older attempts without `workflow_completion=runtime-v1`,
explicit `begin → terminal node complete → route close → producer finalize`
remains the compatibility path. Shared admission is separate and applies only
to capabilities that own a shared kind. Full producer semantics live in
`capabilities/autopilot-code.md` and `capabilities/topologies.json`.
