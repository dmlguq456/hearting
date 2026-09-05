# Capability: autopilot-spec

This is the portable capability contract for `autopilot-spec`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `autopilot-spec` |
| Group | `entry` |
| Supported modes | `app, library, api, cli, research, update` |
| Portable meaning | Create or update requirements/blueprints while keeping `prd.md` as the only spec-change path. |
| Argument shape | `<task description> [--mode auto\|app\|library\|api\|cli\|research\|update (comma-separated for multiple)] [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial] [--user-refine]` |
| Execution topology | `transactional-owner`; registry `capabilities/topologies.json` |
| Entry load phase | `post-approval`; owner contract `capabilities/autopilot-spec.md` |

## Invocation Semantics

General entrypoint for creating and updating requirements and blueprints: new
intent, cleanup/public-release preparation for existing code, and iteration of
an existing spec through `prd.md`. It supports app, library, API, CLI, and
research modes; multiple modes; auto detection; and update mode. Update mode
edits the existing `prd.md`, the canonical path for every spec change. The
shared transaction helper automatically snapshots the exact previous bytes
whenever an existing PRD actually changes, at every intensity. PRDs contain
common sections plus
independent per-mode sections. Automatically cite autopilot-research and
analyze-project outputs. This is the blueprint counterpart to analyze-project's
new-intent analysis. Actual code work belongs to autopilot-code, which detects
`spec/` context automatically.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not.

Spec work writes to `$AGENT_ARTIFACT_OUTPUT_DIR/spec/`. The canonical current blueprint is always `$AGENT_ARTIFACT_OUTPUT_DIR/spec/prd.md`.

`prd.md` opens with a bounded blueprint-summary block — exact `<!-- BLUEPRINT-SUMMARY:BEGIN -->` / `<!-- BLUEPRINT-SUMMARY:END -->` markers right after the H1 title, at most 40 lines between markers — refreshed in the same transaction as every body update. The block is the concise user-facing blueprint (vision, current shape, active decisions, in-flight cycles); downstream consumers such as note-app spec mirrors extract it by exact marker matching, so the markers are a stable contract. A legacy PRD without the block gains it on its next update.

Required public artifacts:

- `prd.md`: current product/project requirements and mode-specific contract;
- `pipeline_state.yaml`: current mode, phase status, timestamps, and resume metadata;
- `pipeline_summary.md`: concise decision log and update narrative;
- mode-dependent companion files such as `stack.md`, `ship.md`, `data_model.md`, `api_contract.md`, `ui_flow.md`, or `design/`.

Internal artifacts belong under `spec/_internal/`, including old PRD snapshots, drafts, raw notes, review records, and temporary scaffolding decisions.

For update mode, run the complete write through `utilities/spec-transaction.py`. It prepares the previous `prd.md` at `spec/_internal/versions/v{N}/prd.md` before the child command, retains it only when the PRD changes, and verifies byte identity. The owning command updates `pipeline_summary.md` under the same lock. Initial creation and no-op updates create no snapshot.

## Artifact Producer Lifecycle

W7C write-cutover contract (`utilities/artifact_producer.py`, registry table
`producer_lifecycle` in `capabilities/topologies.json`). The same lifecycle
binds `direct`, `quick`, and `standard+`; only the acting owner differs.

1. **begin before the first write.** After the route is compiled and bound,
   the owner (the inline session for `direct`, the dispatch-depth-1 owner for
   `quick` and `standard+`) runs `artifact_producer.py begin --artifact-root
   <root> --route <route file> --capability autopilot-spec --intensity <intensity>`.
   While the cutover is inactive this returns `legacy-compat` and the legacy
   `<artifact-root>/spec/` layout stays writable; once active it
   issues `campaign_id`/`cycle_id`/`producer_id` and the cycle directory
   `campaigns/<campaign-locator>/<cycle-locator>/artifacts/` before any artifact exists.
2. **write only inside the open cycle.** Every durable artifact goes under
   `<cycle_dir>/artifacts/spec/...` (`AGENT_ARTIFACT_OUTPUT_DIR`).
   `artifact_producer.py check-write` is the single allow/deny oracle used by
   `hooks/artifact-guard.sh`; an active cutover hard-denies new legacy
   top-level writes, and `shared/` is immutable in both states.
3. **stage workers join, never fork.** `standard+` stage workers receive
   `AGENT_ARTIFACT_CAMPAIGN_ID`/`CYCLE_ID`/`PRODUCER_ID`/`CYCLE_DIR`/`OUTPUT_DIR`
   from the owner (dispatch env pass-through) and call `begin --node <id>`
   on the same route, which resumes the owner's open cycle.
4. **finalize after route closure.** The owner runs `artifact_producer.py
   finalize --artifact-root <root> --cycle <cycle_id>` once the route is
   closed: it enumerates `artifacts/`, builds and validates the D-6 manifest,
   commits `manifest.json` (the commit point), applies the index, and seals
   the cycle record. Empty output leaves no lineage (D-9). `recover` rolls a
   crashed finalize forward or back from its journal.
5. **shared admission.** `spec` output is admitted to `shared/spec/` by `admit-shared --kind spec` after the cycle is sealed (canonical shared kind). A root holds one canonical `spec` reference: a repeat admit without `--reference`/`--key` lands on that single reference; a `--key` that matches none of the existing references is refused (`shared-reference-exists`) and a second reference is only ever created with `--new-reference`.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

Minimum role mapping:

- requirements planning: planning role;
- stack/API/data modeling review: planning plus QA review roles;
- app visual decisions: design role for token, flow, and handoff contracts;
- research or reference import: research role;
- final consistency pass: QA role.

Pipeline intensity follows `core/CONVENTIONS.md §1`: `direct` has no plan stage or durable plan artifact; `quick` is one registered-headless dispatch-depth-1 one-shot conductor with its inline micro-plan plus plan-check-lite; `standard+` uses the capability's durable work-cycle plan when applicable. `plan-check` is required for every non-`direct` graph, but independent QA is not repeated after every stage by default. Verification rigor for plan-check, selected independent reviews, and final verify is derived from intensity; it does not name a model or introduce a separate stage graph.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- enforce git/worktree safety before edits;
- enforce artifact ordering before new durable artifacts;
- enforce spec-read gating when this capability changes spec-backed code or specs;
- use DB memory paths, not runtime-native memory files.

At `thorough+` two groups realize an auxiliary leg, and they are arbitrated
differently. The `research` group's `assumption-check` leg has a **node**
arbiter: the downstream `review` node records `auxiliary_findings_considered` in
its own review log frontmatter, one entry per realized auxiliary leg, and its
completion marker is refused without it. Nothing in the runtime tells that node
it holds the role, so the owner must: **a node arbiter's dispatch prompt names
the group it arbitrates and how many auxiliary legs were realized.** The unit
clauses that require the key are written on the premise that the prompt says so,
and a worker that is never told writes a keyless artifact and is refused at its
own completion gate — a satisfiable condition nobody disclosed. The `review` group's `test-gap-check`
leg has an **owner-merge** arbiter: after that group joins, the owner puts the
same key in the merge record's frontmatter and registers it with
`capability-route.py arbitrate --group review`. `prd-transaction` is a
`capability-owner` node, so it does not pass a wrapper start-gate: there the
enforcement is the route's terminal-gate observation, which carries a failed
`parallel_group:review` row until the record exists. In neither case is the
group's own anchor the arbiter — it runs concurrently with the auxiliary leg.
`core/OPERATIONS.md §5.10` owns the transaction and its typed refusals.

Additional spec-entry gates:

- if user input lacks irreversible-decision coverage, ask one structured intake round before drafting;
- use `update` behavior whenever `spec/pipeline_state.yaml` already exists and the request changes the blueprint;
- do not hand-edit `prd.md` as an ad hoc side effect of code work;
- acquire the shared spec/pipeline lock before writing `prd.md`, `pipeline_state.yaml`, or `pipeline_summary.md`;
- when drift is clear, update the spec and report the drift route; when drift is ambiguous, ask the user before choosing semantics;
- keep deployment setup and environment/domain rollout work in `autopilot-ship` unless the task is only blueprint definition.

## Portable Procedure

1. Parse the task and resolve mode: `auto`, one or more of `app/library/api/cli/research`, or update of an existing spec.
2. Resolve artifact root and identify the target `spec/` directory. In monorepos, choose the component spec from cwd and user wording.
3. Run the intake gate when core irreversible choices are missing.
4. Import existing analysis and research artifacts when present: `analysis_project/code/`, `analysis_project/paper/`, and `research/<topic>/`.
5. Draft or update `prd.md` with a common section plus mode-specific sections.
6. Produce or update companion contracts for the active modes.
7. Run the configured QA/refine passes.
8. For update mode, run the new `prd.md`, `pipeline_state.yaml`, and `pipeline_summary.md` writes inside the spec transaction helper; do not copy the snapshot manually.

## Mode-Specific Semantics

| Mode | Required blueprint coverage |
|---|---|
| `app` | Feature scenarios, API contract, data model, UI flow, stack, scaffold/skeleton intent, design handoff hooks. |
| `library` | Public API, exports, examples, compatibility, versioning, module structure. |
| `api` | Endpoints, request/response bodies, errors, auth, rate limiting, data model. |
| `cli` | Commands, subcommands, options, input/output format, exit codes. |
| `research` | Experiment roadmap (staged ladder with per-step decision criteria; see below) plus the reproduction contract: train/eval entry points, configs, seeds, reproduction commands, expected metrics, baselines. |

Composite modes are valid. Keep shared decisions in the common PRD section and each mode's contract in its own section.

### Research-Mode Roadmap Semantics

Contract modes (`app`, `library`, `api`, `cli`) describe a target state. A
`research` blueprint is a progressive roadmap advanced step by step; results
legitimately reshape the remaining ladder. Grounded in live usage
(BC_ResNet_tf WWD blueprint v4, 2026-07), a research-mode `prd.md` requires:

- **Step ladder**: ordered steps, each with status (done/active/planned),
  objective, configuration, decision criteria, and follow-on work. A closed
  step keeps its one-line verdict with evidence links.
- **Decision protocols**: operating targets and judgment axes recorded with
  their user-confirmation dates; changing a protocol is a blueprint change.
- **Premises and measured constants**: task context and empirically measured
  constants carried with their source experiment.
- **Evidence lineage**: steps and verdicts cite experiment slugs and report
  sections; `pipeline_state.yaml` records `source_analysis` (evidence read)
  and `next` (the follow-on lab/code handoff).
- **Rejected tracks**: rejected directions stay in the PRD with the rejection
  basis and revival cues instead of being deleted.
- **Execution order and completion criteria**: resource-aware run order and
  per-step deliverables, including experiment `_RUNLOG` lineage.

Update semantics follow: a research-mode update that closes a step records its
evidence and re-plans the remaining ladder in the same transaction (snapshot
rules unchanged). A result contradicting the plan is the roadmap's normal
replanning trigger, not ambiguous drift; the drift-ambiguity guard above
applies to contract modes.

## Routing Boundary

`autopilot-spec` decides what should exist and records the blueprint. Actual implementation, refactoring, debugging, and test repair are `autopilot-code` work. Visual artifact production is `autopilot-design`; deployment execution is `autopilot-ship`. A change to evaluation policy or another blueprint surface is spec-sync only: under `WORKFLOW §0.2` it never substitutes for the reevaluation or implementation that applies it, which stays with `autopilot-lab` or `autopilot-code`.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/autopilot-spec/SKILL.md` and `skills/autopilot-spec/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-spec/SKILL.md`, while `skills/autopilot-spec/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info autopilot-spec`. Use `adapters/codex/skills/autopilot-spec/SKILL.md` as the native Codex Skill projection; do not consume `skills/autopilot-spec/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info autopilot-spec`. Use `adapters/opencode/skills/autopilot-spec/SKILL.md` and `adapters/opencode/commands/autopilot-spec.md` as native OpenCode projections; do not consume `skills/autopilot-spec/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/autopilot-spec/SKILL.md` and `adapters/claude/skills/autopilot-spec/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-spec/SKILL.md`, while `skills/autopilot-spec/SKILL.md` remains the compatibility reference kept for parity/drift checks.
