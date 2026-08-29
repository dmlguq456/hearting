# autopilot-code

Code-work entrypoint. Detect spec context and close the `plan → execute → test → report` loop at the selected intensity. This file defines routing and stage contracts; load the relevant reference only when its detailed policy is needed.

## Quick Contract

- Default output: `<artifact-root>/plans/<date>_<slug>/`. `direct` creates no durable plan; `quick` uses a micro-plan; `standard+` writes the plan, checklist, `pipeline_summary`, development logs, and test logs.
- When a spec exists, emit a one-line `spec-significance` judgment before editing code. Route spec-significant changes through an `autopilot-spec` update first.
- Recheck git and worktree state at entry and immediately before durable write-back or commit. Stop on an active merge/rebase, detached HEAD, or an unexpected HEAD change.
- Do not parallelize QA at every stage. Scale `plan-check` and final `code-test` from the rigor derived from intensity (CONVENTIONS §1.1).
- Follow an explicit artifact or audience language for user-facing reports. Otherwise, use the conversation language.

## Reference Index

| File | When to load (mandatory) | Content |
|---|---|---|
| `context-and-guards.md` | Every invocation (required) | Artifact, spec, and git guards; spec-mode detection; design/app/library/API/CLI/research boundaries; experiment-ready input; invocation routing |
| `arguments-and-decisions.md` | When interpreting arguments, `--from`, pause/resume, or active-plan conflicts | Argument parsing, defaults, active/partial/complete plan handling, and plan-path resolution |
| `dev-pipeline.md` | When running `--mode dev` | Stage orchestration, plan check, retry behavior, and `analyze-project` update |
| `debug-audit.md` | When running `--mode debug` or `audit` | Debug diagnosis and fix flow; audit fan-out and autofix workflow |
| `pipeline-summary-safety.md` | At terminal, failed, partial, rollback, or summary states | Summary template, terminal-state reporting, and common safety rules |

## Argument Shape

`--mode dev|debug|audit <task/plan/error description> [--from <step>] [--intensity direct|quick|standard|strong|thorough|adversarial] [--user-refine]`

Defaults:

- `--mode`: default to `dev`; infer `debug` when the request is centered on an error log or traceback.
- `--intensity`: choose from scope and risk. Use `direct` for a one-line task, `quick` for a small scoped change, and `standard+` for multi-stage or multi-file work. Verification rigor is derived from intensity rather than selected separately (CONVENTIONS §1.1).
- `--user-refine`: enable only when the user explicitly requests a review or note-taking pause.

## Stage Graph

| Intensity | Graph | Durable artifact | Review policy |
|---|---|---|---|
| `direct` | intake → produce → sanity/report | None | No independent QA |
| `quick` | intake → orient-lite → micro-plan → plan-check-lite → produce → verify-lite → report | None by default | Inline check with 3-4 questions |
| `standard` | (`frame` + `frame-alternative`) → code-plan → plan-check → code-execute → impl-review → code-test → code-report | Required | Run the route-declared 2-way framing exploration with `balanced-deep` + `light` profiles and distinct perspectives |
| `strong` | 3-way frame → 2-way plan → plan-check arbitration → execute → 2-way implementation review → test → report | Required | Spend cheap asymmetric breadth early, then converge through the declared arbiters; every group remains cross-harness-first. `execute` carries the SD-103 subdivision permission at this tier — check it before dispatching a single long session (dev-pipeline Step 3) |
| `thorough`/`adversarial` | strong graph + 3-way plan and 3-way implementation review + deeper rigor | Required | Use the registry-declared third implementation-risk/failure-mode legs; never invent or widen a group outside the sealed route |

**`standard+` dispatch**: Run every durable compiled node as dispatch depth 2. Start each sealed `parallel_group` of 2–4 legs with one `dispatch-batch --parallel-group` transaction, never member-by-member. The dispatch-depth-1 conductor passes artifact paths, reads only verdict/status, and yields while the adapter supervisor joins the exact child batch. Cross-harness means at least two harness families across the group; model-profile and perspective asymmetry are independently sealed and reported. Use `dispatch-wait` only for an explicit `poll-fallback`. Only `direct` and `quick` keep micro-stages inline. The owner never sets `AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN` or any other `AGENT_DISPATCH_*` lifecycle override, and never reads harness utility sources looking for one — the launcher evidence-binds that assertion to the launcher's own observed scope, and a registered headless owner inside a tool sandbox cannot make it. If the runtime hands a foreground `dispatch-batch` call to the background, that call has not failed: poll `dispatch-current --route <id>` or wait at the runtime join. Do not switch lifecycle.

## Mode Routing

- `dev`: add features, refactor, or implement. `direct|quick` shorten the full pipeline; `standard+` uses framing, `code-plan`, durable `plan-check`, optional `code-refine`, `code-execute`, `impl-review`, `code-test`, and `code-report`.
- `debug`: diagnose the root cause before planning a fix. Proceed when the cause is clear; ask for a choice only when materially different causes remain plausible.
- `audit`: inspect a codebase or app comprehensively and apply low-risk fixes. Keep review fan-out read-only; make and verify changes in a worktree based on current HEAD before harvest.

## Critical Gates

1. Resolve the artifact root by preferring `.agent_reports` and falling back to legacy `.claude_reports`.
2. Run git-state preflight and remember starting `HEAD`.
3. If `spec/` exists, read `spec/prd.md` and emit `spec-significance`.
4. Choose stage graph from intensity before QA.
5. Before source write-back or commit, re-run git-state preflight.
6. On any terminal state, write `pipeline_summary.md` before reporting to the user.

> Treat the [Reference Index](#reference-index) as the single source for reference files, load points, and contents.

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/autopilot-code.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability autopilot-code
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `plans/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/plans/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Run `capability-route.py complete` for the terminal node(s) before running
   `capability-route.py close` on the same route — `close` reads the terminal
   completion markers to decide `terminal_gate_proven`, and by default now
   refuses (`route-close-before-complete`, exit 64) instead of permanently
   sealing a `false` proof when `complete` has not run yet. `--allow-unproven`
   exists only to record an intentionally abandoned route's honest `false`
   outcome for recovery/cutover bookkeeping — never pass it to route past a
   terminal node's ordinary completion. Never pass `--output` to
   `complete`: it targets an existing artifact path 1:1 and a collision would
   silently overwrite that artifact; the canonical completion marker location
   is written regardless.
5. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
6. Only then, if this capability owns a shared kind, `admit-shared`.
