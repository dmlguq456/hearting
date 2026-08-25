# Capability: autopilot-code

This is the portable capability contract for `autopilot-code`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `autopilot-code` |
| Group | `entry` |
| Supported modes | `dev, debug, audit` |
| Portable meaning | Code-work entrypoint that detects spec context and closes the plan→execute→test→report loop. |
| Argument shape | `--mode dev\|debug <task/plan/error description> [--from <step>] [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial] [--user-refine]` |
| Execution topology | `staged`; registry `capabilities/topologies.json` |
| Entry load phase | `post-approval`; owner contract `capabilities/autopilot-code.md` |

## Invocation Semantics

General code-work entrypoint for libraries, research code, and applications,
whether new or existing; it detects the cwd automatically. It supports `dev`
(features/new work) and `debug` (diagnosis/fixes). When `spec/` exists, read it
and branch by spec mode: app adds design critique, migration safety, and
push/deploy handling; library checks public API consistency; CLI checks command
and option consistency; research checks reproducibility, configs, and metrics.
Non-code decisions such as PRDs, stack selection, skeletons, and ship setup
belong to autopilot-spec.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not.

Code work normally writes to `<artifact-root>/plans/<date>_<slug>/`, even when a `spec/` directory exists. `spec/` is the blueprint bucket; `plans/` is the work-cycle bucket.

Artifact intensity policy:

- `direct`: no new plan root, no `plan.md`, and no durable pipeline artifact unless the adapter or current repo policy explicitly requires one;
- `quick`: no durable `plan.md` by default; record a short summary/evidence only when a work-cycle artifact is already required;
- `standard+`: create or resume `<artifact-root>/plans/<date>_<slug>/`.

Required public artifacts for `standard+` work cycles:

- `plan.md` at the plan root;
- `checklist.md` at the plan root when the plan is multi-step;
- `pipeline_summary.md` at the plan root before completion;
- `dev_logs/` and `test_logs/` for implementation and verification evidence.

Internal artifacts belong under `_internal/`, including plan reviews, dev reviews, test reviews, retry notes, raw command logs, and model/team deliberation notes.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

Minimum role mapping:

- planning: planning role for `code-plan`;
- implementation: development role for `code-execute`;
- verification: QA role for `code-test`;
- review: QA/reviewer role for plan, code, and test review;
- app UI changes: design role as critic or handoff verifier when design artifacts exist.

Pipeline intensity is the primary ceremony selector. `direct` is inline and `quick` is one `balanced-deep` registered one-shot conductor. Every `standard+` owner uses `deep`; `standard` opens framing as two asymmetric cross-harness legs (`balanced-deep` anchor plus `light` alternative). `strong` adds a deep contrarian framing leg and opens width-two plan (`deep + balanced-deep`) and implementation-review (`balanced-deep + light`) groups. `thorough|adversarial` add the declared light implementation-risk plan leg and deep failure-mode review leg. All legs are dispatch-depth-2 siblings with disjoint artifacts, exact all-join, and route-sealed role/profile/perspective; other stages remain sequential. The same intensity determines plan-check, selected reviews, and code-test rigor without a separate user-facing QA axis. Concrete models remain adapter-specific.

## Stage Mapping

| Common stage | `autopilot-code` realization |
|---|---|
| `intake` | parse `dev|debug|audit`, classify spec significance, choose intensity and QA override |
| `orient` | read `spec/prd.md` and relevant source context; `orient-lite` reads only the touched area |
| `plan` | none for `direct`; registered-headless dispatch-depth-1 one-shot conductor micro-plan for `quick`; `code-plan` durable artifact for `standard+` |
| `plan-check` | none for `direct`; 3-4 question gate inside the registered-headless dispatch-depth-1 quick conductor; lightweight plan QA for `standard`; risk/adversarial critique only for `strong+` |
| `produce` | direct edit for `direct`; quick is one registered-headless dispatch-depth-1 one-shot conductor and `code-execute` or scoped implementation for `quick+` |
| `verify` | sanity check for `direct`; focused command/review inside the quick one-shot worker; `code-test` evidence for `standard+` |
| `synth` | only when dispatch-depth-2 perspective/verifier/adversary workers ran |
| `report` | concise user report for `direct`; quick returns its concise report from its registered-headless dispatch-depth-1 one-shot conductor; `code-report`/`pipeline_summary.md` for `standard+` |

Stage-local gates must not become full independent QA loops after every sub-stage. Keep plan-check small, concentrate expensive independent review in the selected risk point or final verification, and keep raw logs in artifacts rather than parent context. A stage remains one semantic gate even when the dispatch-depth-1 owner realizes it as several bounded sessions; those sub-sessions have no gate authority and return phase-brief/ledger/handoff evidence only. `code-plan`, `code-refine`, and `code-test` inherit the selected graph: `code-plan` is standard+ durable planning, `code-refine` is optional correction, and `code-test` is final concrete verification rather than hardcoded-thorough QA.

**Corrections are batched, never atomic.** A failed review gate (`plan-check`, `impl-review`, `test`) is followed by exactly one correction pass that closes every 🔴 finding of that round together, plus the follow-on gaps the review named; the owner never redispatches the full `plan` or `execute` node to fix a single finding. The plan correction runs through the `code-refine` boundary and the code correction re-enters the `execute` boundary as a bounded fix under the same node. The re-review that follows is a **closure check** under the review unit's Round Protocol — the owner's assignment names the round number and the prior review artifact and asks whether the prior 🔴 items are closed and the delta is clean; it never asks for a fresh independent re-audit of the whole artifact. Each correction consumes one unit of the `core/CONVENTIONS.md §1.1` retry budget; when the budget is spent, remaining concerns go to the plan's risk/unresolved section and the owner reports them instead of opening another round.

A declared `plan-check` parallel group is a 2-way read-only review: two plan-check verdicts merge under the existing review-anchor merge contract (stricter-wins plus the union of blocking findings). When the two legs nominate different plan legs as winner, `plan.md` materialization is blocked unless the owner writes a bounded merge-arbitration memo, which is the only path into the existing bounded `code-refine` flow. `plan-check` itself never mutates the plan.

At `thorough+` the group realizes a third `simplicity-check` leg with `leg_class: auxiliary`. Its arbiter is the **owner**, not the group's anchor — the anchor runs concurrently with it. After the group joins, the owner puts `auxiliary_findings_considered` in the merge memo's frontmatter with exactly one entry per realized auxiliary leg (adopted or rejected, with the reason) and registers it with `capability-route.py arbitrate --group plan-check`. Until that record exists, `code-execute` is refused at the start-gate with `auxiliary-arbitration-missing` and the route's terminal-gate observation carries a failed `parallel_group:plan-check` row. `core/OPERATIONS.md §5.10` owns the transaction and its typed refusals.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- in a linked task worktree, treat the local artifact snapshot as read-only and
  write plans/logs/reports only to the canonical root passed through
  `AGENT_ARTIFACT_ROOT`;
- enforce git/worktree safety before edits;
- enforce artifact ordering before new durable artifacts;
- enforce spec-read gating when this capability changes spec-backed code or specs;
- use DB memory paths, not runtime-native memory files.

Additional code-entry gates:

- before any code edit, classify the request against existing `spec/prd.md` when present and emit a one-line `spec-significance` verdict;
- route `spec-significant` changes through `autopilot-spec` update before implementation unless the user explicitly defers;
- detect whether `spec/pipeline_state.yaml` has changed since the last relevant plan and re-read newer spec/design artifacts before editing;
- for app mode, treat design tokens and handoff artifacts as source contracts, not suggestions;
- for destructive DB/schema/migration work, explain the command and risk, but do not auto-run destructive operations without explicit user approval;
- for non-trivial feature, multi-file, or module work, use the runtime's isolated-worktree or equivalent dispatch policy from `core/OPERATIONS.md`; for standard+ create the final isolated worktree before collecting route eligibility evidence, and seal only tuples whose `checked_worktree` exactly matches the route `cwd`.
- after main/orchestrator merges, verifies the integrated tree, and pushes the
  integration ref, invoke the guarded worktree cleanup check/apply path; never
  infer cleanup eligibility from a runtime session-end event.

## Portable Procedure

1. Parse arguments and infer `dev`, `debug`, or `audit` when the adapter allows natural-language entry.
2. Resolve artifact root and create or resume a `plans/<date>_<slug>/` work cycle.
3. Run git/worktree preflight and remember the starting `HEAD`.
4. If `spec/` exists, read `spec/prd.md` plus relevant mode contracts before planning.
5. Emit `spec-significance: within-spec` or `spec-significance: SPEC-SIGNIFICANT (...)`.
6. Select the stage graph from pipeline intensity, then map common stages to code sub-capabilities. `direct` skips `code-plan`; `quick` runs as one registered-headless dispatch-depth-1 one-shot conductor with an inline micro-plan and plan-check-lite; `standard+` uses `code-plan`, optional `code-refine`, `code-execute`, `code-test`, and `code-report` according to the selected graph, QA override, and resume point. For `standard+`, a dispatch-depth-1 capability owner may dispatch bounded dispatch-depth-2 planner/verifier/adversary workers when the task is separable and must synthesize their short reports before final write-back. It may also split any one stage into declared serial sub-sessions under the same route node, or use an existing sealed parallel group for disjoint ownership. The owner preserves exactly one stage gate, treats planned subdivision as non-retry capacity, and after a failed gate opens only a gap session for unfinished handoff items. `direct` stays inline; `quick` is one registered-headless dispatch-depth-1 one-shot conductor unless explicitly escalated.

   `code-execute` is the one node in this capability that carries a subdivision permission, so this is where the surface is used: the owner passes the slice manifest to `dispatch-batch.py --subdivision-manifest` at admission and to `capability-route.py complete --subsession-manifest` at the gate, and reads the typed `single-session-required` receipt as "run this stage as one ordinary session" rather than as a failure. Slices are no-commit workers, and the owner commits once after quiescence — **in that order: close the stage gate first, then commit.** A commit made before the gate that carries the slices' own files is refused, because the gate cannot tell it apart from a slice having committed; a commit made after it is ordinary, and replaying the same gate resumes the published marker. The gate judges by what a commit carries, so a pre-gate commit that carries no slice's `fixed_files` and leaves content identical to the admission baseline passes — in that one shape the no-commit rule is stated but not enforced, which `core/OPERATIONS.md §5.10` declares as the accepted cost of not judging by HEAD movement alone. History off the baseline commit's first-parent line, a change outside the declared file union whether committed or not, and a missing admission baseline each refuse the stage marker with their own typed reason. `core/OPERATIONS.md §5.10` owns the full surface, the refusal vocabulary, and the gap-retry derivation.
7. Before each durable write-back or commit, re-run git/worktree safety and stop if `HEAD` or merge state changed unexpectedly.
8. Record implementation evidence and verification results in `pipeline_summary.md`.
9. After the durable report terminal, evaluate the route-sealed optional
   artifact-sink extension. When available, offer `final_report.md` through the
   app-neutral receipt contract; otherwise record
   `skipped/extension-unavailable` without changing code-cycle completion.

## Mode-Specific Semantics

| Spec mode | Extra requirement |
|---|---|
| `app` | Use design handoff and token artifacts when present; UI changes get design review; destructive migration work requires explicit approval. |
| `library` | Check public API, exports, semver impact, compatibility notes, and examples. |
| `api` | Check endpoint/body/error/auth/rate-limit contracts and security implications. |
| `cli` | Check command names, options, input/output formats, and exit codes. |
| `research` | Check train/eval entry points, configs, seeds, reproducibility commands, and metrics. |

When no spec exists, infer mode lightly from project files, report the inference, and keep the stricter spec-only gates disabled.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/autopilot-code/SKILL.md` and `skills/autopilot-code/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-code/SKILL.md`, while `skills/autopilot-code/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info autopilot-code`. Use `adapters/codex/skills/autopilot-code/SKILL.md` as the native Codex Skill projection; do not consume `skills/autopilot-code/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info autopilot-code`. Use `adapters/opencode/skills/autopilot-code/SKILL.md` and `adapters/opencode/commands/autopilot-code.md` as native OpenCode projections; do not consume `skills/autopilot-code/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/autopilot-code/SKILL.md` and `adapters/claude/skills/autopilot-code/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-code/SKILL.md`, while `skills/autopilot-code/SKILL.md` remains the compatibility reference kept for parity/drift checks.
