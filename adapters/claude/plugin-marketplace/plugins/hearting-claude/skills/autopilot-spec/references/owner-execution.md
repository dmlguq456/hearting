# autopilot-spec

Create or update a product or technical blueprint without implementing the product itself. Store outputs under `<artifact-root>/spec/` using the three-tier output convention (`<agent-home>/core/CONVENTIONS.md#5-skill-output-convention--t1t2t3`) and plain names without numeric prefixes: `prd.md` as the always-current T1 document, plus `stack.md`, `design/`, `ship.md`, `pipeline_state.yaml`, and `_internal/`. Resolve `<artifact-root>` using the workspace assumption (`<agent-home>/core/CONVENTIONS.md#51-workspace-assumption`).

## Intake Gate

At entry, check whether the request covers the irreversible decisions relevant to the selected mode, such as stack, authentication, database, deployment target, and core entities. If coverage is insufficient, run one structured clarification round with an escape option under CONVENTIONS §6.6 (`<agent-home>/core/CONVENTIONS.md#66-autopilot-intake-gate`). Skip the gate when explicit arguments are already sufficient, the decisions are already stated, the work is intentionally throwaway, or the run is resuming known state. No extra flag is required.

## Modes

| Mode | Scope | Required blueprint content |
|---|---|---|
| `app` | User-facing application | Features, scenarios, API contract, data model, UI flow, stack, scaffolding, and skeleton |
| `library` | Public library or package | Exported functions, classes, and types; usage; compatibility; versioning; module structure |
| `api` | Backend service without a UI | Endpoints, bodies, errors, authentication, rate limits, and data model |
| `cli` | Command-line tool | Commands, options, subcommands, input/output, and exit codes |
| `research` | Experiment roadmap and reproducible research code | Step ladder with per-step decision criteria and evidence lineage, plus train/eval entrypoints, configs, reproduction commands, expected metrics, and baseline comparison |
| `update` | Existing blueprint change | Canonical `prd.md` update with version and decision history |

Combine modes naturally when one project has several public surfaces, such as `library,cli` or `research,cli`. Organize the PRD into shared requirements plus mode-specific sections.

Implementation, refactoring, and debugging belong to `autopilot-code`, which reads `spec/` before changing source. UI-only design belongs to `autopilot-design`; deployment setup belongs to `autopilot-ship`.

```text
autopilot-research + analyze-project
  → autopilot-spec
  → optional autopilot-design
  → repeated autopilot-code
  → optional autopilot-ship for application release setup
```

## Invocation

- Default `--mode` to `auto`; infer one or more modes from the request, existing code, and existing artifacts.
- Derive verification rigor from `--intensity`; there is no separate `--qa` axis. Use the canonical mapping (`<agent-home>/core/CONVENTIONS.md#11-verification-rigor-tiers`), with standard as the ordinary durable-spec default and escalation based on risk.
- Enable `--user-refine` only when the user explicitly asks to pause for review.
- Treat an explicit `autopilot-spec` invocation as the routing decision.

Route narrower work directly:

- implementation, refactoring, or debugging → `autopilot-code`
- UI design without blueprint changes → `autopilot-design`
- a small implementation-only edit or rename → the `dev/*` units under the owning code workflow
- deployment, environment, domain, or migration setup → [`autopilot-ship`](../../autopilot-ship/SKILL.md)

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write `prd.md`, supporting specs, and user-facing reports in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve code identifiers, file paths, public API names, commands, and technical terms when translation would reduce precision. The language of this skill file does not set the artifact language.

## Canonical Spec-Change Contract

`spec/prd.md` is the single current source for specification decisions, and this capability's update mode is the only supported path for changing it.

- **Update mode**: route every `prd.md` change through this capability, whether initiated by the user, drift found during `autopilot-code`, or a post-run correction. Existing `pipeline_state.yaml` activates update mode on re-entry; do not hand-edit `prd.md` ad hoc.
- **Version snapshot**: run every existing-`prd.md` update through `utilities/spec-transaction.py`. The helper prepares the exact current bytes before the child command, retains and verifies `_internal/versions/v{N}/prd.md` whenever the PRD changes, and creates nothing for initial/no-op updates. Major and minor classify review/history only; neither may bypass the snapshot. After five accumulated minor edits, recommend `/audit` without running it automatically.
- **Blueprint summary block**: every `prd.md` opens with a bounded summary block right after the H1 title, wrapped in exact `<!-- BLUEPRINT-SUMMARY:BEGIN -->` / `<!-- BLUEPRINT-SUMMARY:END -->` markers (≤40 lines between markers). Refresh it in the same lock transaction as every body update; a legacy PRD without the block gains it on its next update. Format and content rules live in `prd-authoring.md`; downstream consumers (e.g., note-app spec mirrors) extract the block by exact marker matching, so never rename or restructure the markers.
- **Intake gate**: apply the one-round decision-coverage check described above.
- **Concurrency lock**: immediately before entering a write stage, acquire `.pipeline-lock` under the contract in `prd-authoring.md` and OPERATIONS §5.8. If blocked, stop writing and report the owner and recovery path.
- **Confirmation and forbidden zones**: follow `operations-and-examples.md` for Continue, Revise, Back-jump, and Stop behavior and for deployment, billing, DNS, and real-secret boundaries.

## Reference Index

| File | Load when | Contents |
|---|---|---|
| `invocation-and-modes.md` | Parsing options, inferring modes, or choosing new versus resumed work | `--mode`, `--intensity`, `--user-refine`, mode clues, `pipeline_state.yaml` detection, step classification, confirmation summary, update-mode contract, and version management |
| `prd-authoring.md` | Writing or updating the PRD, Steps 1–3.5 | Information collection, one-screen confirmation, concurrency guard, three-part PRD authoring, semantic/rule boundary checks, PRD template, architecture diagrams, and bundled-update logic |
| `scaffolding.md` | Scaffolding and skeleton work, Step 4 | Reference-source selection, repository or checkpoint acquisition, pretrained-checkpoint preflight, `dev/new-lib` unit work, result confirmation, and Step 5 gate |
| `operations-and-examples.md` | Deployment routing, gate behavior, return format, continuity, or examples | `autopilot-ship` routing, forbidden zones, gate branches, pipeline-state management, return format, optional agent-owned continuity notes, and worked examples |

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/autopilot-spec.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability autopilot-spec
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `spec/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/spec/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
