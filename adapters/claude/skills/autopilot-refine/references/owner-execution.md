# autopilot-refine

Post-creation refinement entrypoint for existing document and research artifacts. Preserve snapshots and change history while correcting or updating content. This file defines major/minor routing, invocation forms, and stage contracts; load a reference only when its detailed procedure is needed.

> **Output convention**: Follow CONVENTIONS §5 (`<agent-home>/core/CONVENTIONS.md#5-skill-output-convention--t1t2t3`). Store modern document and research snapshots under `_internal/versions/v{N}/`; recognize legacy sibling `_v{N}.md` snapshots when already present. The route write guard and `utilities/artifact-snapshot.py` own version allocation and exact pre-image copying.

## Position in the Autopilot Family

- `autopilot-research`, `autopilot-code`, and `autopilot-draft` create artifacts in the forward direction.
- `autopilot-refine` updates existing artifacts while preserving their lineage.
- `--intensity` selects the graph and derives verification rigor from CONVENTIONS §1.1. There is no separate `--qa` axis.
- Routine scoped refinements use the quick path unless scope, risk, or an explicit option requires more.

## Routing: Major vs Minor

Limit this capability to `<artifact-root>/research/*` and `<artifact-root>/documents/*`. Use direct editing for ordinary project Markdown, and use `code-refine`, `code-execute`, or `autopilot-code` for plan or code artifacts.

Route a change through the major refinement flow when any of these conditions holds:

1. The user explicitly requests a major version, broad rewrite, new refinement cycle, or `/autopilot-refine`.
2. The change affects roughly 200 or more lines, rewrites a whole section, reclassifies a batch of mutations, or realigns strategy and draft structure.
3. The user explicitly asks to prepare the artifact for an imminent external review, submission, grant, public rebuttal, or PR review. Do not infer this ceremony from cwd names or stale memory.

Treat the change as minor by default when it is limited to one mutation, a few cross-references, a caption or table cell, typo or wording polish, one or two missing references, or a figure/asset path correction.

For a minor change, edit directly and append a detailed entry to `pipeline_summary.md`. Do not create a snapshot; the latest major snapshot remains the audit baseline. When minor entries exceed `AUDIT_HINT_THRESHOLD` (default 5), recommend a batch `/audit` without running it automatically.

Explicit options override automatic classification:

- `--intensity standard|strong|thorough|adversarial` enters the refinement flow at that intensity.
- A clear request for direct edit without versioning or snapshots uses the minor path.
- `--review-only` inspects and previews without applying.
- Explicit `/autopilot-refine` enters the refinement flow; default to `quick` when no intensity is supplied.

Read `versioning-and-modes.md` for the exact minor-log format and major-version behavior.

## Verification Rigor

Derive rigor from `--intensity` through CONVENTIONS §1.1 (`<agent-home>/core/CONVENTIONS.md#11-verification-rigor-tiers`). Review the proposed diff before application.

| Rigor tier | Proposed-diff review |
|---|---|
| **light** (`direct`) | Run the factual/style detector and a sanity check; no independent review loop |
| **quick** (`quick`) | Investigate, run Stage B.5, preview the diff, and apply; no independent review loop |
| **standard** | Add one `deep reviewer`, two `fast reviewer` axes, and one `fast fact-checker` against in-artifact ground truth |
| **thorough** | Add a second `deep reviewer`, retain two fast review axes and fact-checking, and allow a second round when the graph selects it |
| **adversarial** | Thorough review plus an independent `external adversary` selected through the active adapter |

`--no-fact-check` and `--no-style-audit` are orthogonal opt-outs that disable only their corresponding Stage B.5 aspect. They do not change intensity or other verification.

This capability performs pre-apply review only. Use `draft-refine` when a separate post-apply review cycle is required.

## Invocation Forms

| Form | Behavior |
|---|---|
| `autopilot-refine "<prompt>"` | Investigate → preview diff → apply MECH/SEM changes → snapshot/version/log. Halt on STRUCT and recommend the heavier owning flow. |
| `autopilot-refine "<prompt>" --confirm` | Pause after diff preview and apply only after explicit confirmation. |
| `autopilot-refine "<prompt>" --review-only` | Investigate and preview; make no edit, snapshot, or log entry. |
| `autopilot-refine --memo <file> "<prompt or artifact hint>"` | Use the memo as proposal input, then follow the default flow; `--confirm` remains available. |

Resolve the target by fuzzy matching prompt terms against `<artifact-root>/{research,documents}/*`. Use one match, ask the user to choose among multiple matches in the conversation language, and report zero matches with guidance.

## Language Rule

Preserve the target artifact's existing or explicitly requested language. Otherwise, use the conversation language for user-facing summaries and reports according to `<agent-home>/roles/response-policy.md`. Preserve source quotations, code, paths, identifiers, and citations.

Resolve `<artifact-root>` by preferring `.agent_reports` and falling back to legacy `.claude_reports`: CONVENTIONS §5.1 (`<agent-home>/core/CONVENTIONS.md#51-workspace-assumption`).

## Process

After artifact resolution, run Stages A-E. Read `process-stages.md` for complete orchestration.

1. **Artifact Resolution**: fuzzy-match the target and detect its type.
2. **Stage A — Discover structure**: inspect the artifact tree, identify `cards/*` for research or `strategy/` and `draft/` for documents, and narrow the affected files with search.
3. **Stage B — Plan changes**: read only affected files, build a per-file change list, and classify each change as `MECH`, `SEM`, or `STRUCT`. Halt on STRUCT and recommend the owning heavier flow.
4. **Stage B.5 — Factual and style detectors**: run for every change, including quick. Compare factual claims against artifact-local ground truth and run the style lint. Mark unresolved findings as `⚠ Unverified` or `⚠ Style`. Only the two explicit opt-out flags may skip these checks.
5. **Stage C — Diff preview**: show the proposed change. Continue automatically by default, pause with `--confirm`, or stop with `--review-only`.
6. **Stage D — Apply**: obtain the deterministic route-bound snapshot receipt before editing, apply the change, and update all five `pipeline_summary.md` sections: metadata, version history, changes, migrated minor log, and in-file changelog. Never choose the version or copy the snapshot manually.
7. **Stage E — Memo form**: when `--memo <file>` is present, use the memo as proposal input before running Stages B-D.

## Reference Index

| File | When to load (mandatory) | Content |
|---|---|---|
| `versioning-and-modes.md` | When deciding or executing a major refinement | Minor-log format, major behavior, split rationale, adversarial propagation, default mode forms, STRUCT halt, and tunable constants |
| `process-stages.md` | During execution (required) | Artifact resolution and complete Stage A-E orchestration, detectors, preview, apply/versioning, and memo form |
| `examples-and-constraints.md` | For invocation examples, constraints, or post-apply decisions | Examples, prefer-gap-over-wrong-fill constraints, when-not-to-use guidance, and checklist |

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/autopilot-refine.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability autopilot-refine
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `target/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/target/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
