# audit

Read-oriented post-run inspection entrypoint for artifacts and pipelines. Diagnose drift, inconsistency, and omissions through Stages A-E: detect type, select scope, ingest the P1 baseline, lint selected aspects, report findings, and optionally dispatch an auto-fix chain. This file defines routing and stage contracts; load the relevant reference only when its detailed procedure or template is needed.

> **Output folder convention**: CONVENTIONS.md §5 (`<agent-home>/core/CONVENTIONS.md#5-skill-output-convention--t1t2t3`) (3-tier). Do not modify the audited artifact during inspection; write only an audit report at `{artifact_dir}/_internal/audit/audit_{YYYY-MM-DDTHHMM}.md`.
> Resolve `<artifact-root>` by preferring `.agent_reports` and falling back to legacy `.claude_reports`: CONVENTIONS §5.1 (`<agent-home>/core/CONVENTIONS.md#51-workspace-assumption`).

## Position in autopilot family

`audit` is the **read-only inspection** counterpart to `autopilot-refine`:
- `autopilot-refine` reads + writes (proposes diff, applies on confirm, versions).
- `audit` reads only (lints, reports issues, never edits).

Use `audit` when:
- Batch-check accumulated minor drift. Because `autopilot-refine` applies minor edits directly and records them in `pipeline_summary.md`, auditing those edits together is the normal workflow.
- Run a sanity check before handing off a new artifact.
- Evaluate an artifact produced by someone else.

Use `autopilot-refine` when:
- The request calls for a major edit that should proceed through application: an explicit major revision, substantial structural change, or preparation for external review.

## Dual-Perspective Audit (Documents and Research)

Audit document and research artifacts from both perspectives:

| Perspective | What it examines | Report section |
|---|---|---|
| **P1 — against the last major baseline** | Read the minor-change log in `pipeline_summary.md` and diff the `_internal/versions/v{N}/` snapshot to determine how accumulated minor edits shifted the artifact as a whole. | `## Perspective 1 — Accumulated minor drift` |
| **P2 — against universal principles** | Inspect the current artifact with Stage C aspect lints for facts, style, structure, cross-references, and coverage. | `## Perspective 2 — Universal principles` |

**Why both perspectives matter**:
- P1 sees only what changed and can miss unresolved issues that predate the baseline.
- P2 evaluates the current state accurately but cannot identify which minor edit introduced an issue, making rollback or major-refinement baselines harder to choose.
- Cross-correlating them distinguishes recently introduced, high-priority fixes from older issues that can wait for the next cycle.

**Plan artifacts** have no minor-log convention, so skip P1 and run only P2.

## Cadence

| Trigger | Action |
|---|---|
| **Explicit `/audit <artifact>` request** (default) | Run immediately |
| **`AUDIT_HINT_THRESHOLD` reached** (default: 5 minor edits since the last major version) | After the current minor edit or `autopilot-refine` run, suggest `⚠ {N} minor edits accumulated since v{N} — recommend /audit {artifact_short_name}`. Do not run automatically. |
| **Audit spawned by an automatic fix chain** | Run when invoked by `autopilot-refine` or `autopilot-code` fix routing |

Calculate the threshold from the number of entries in the document or research artifact's minor-change log, or from `v{N}_M` rows in its version-history table.

## Language Rule

User-facing audit artifacts follow the audience-language-first rule in
`<agent-home>/roles/response-policy.md`. An explicit target artifact, external
audience, or publication language takes precedence; this skill imposes no fixed
chat locale.

## Argument Parsing

    /audit <artifact_path> [--scope auto|facts|style|structure|cross-ref|coverage|all] [--read-only] [--report-only] [--no-fact-check]

- `<artifact_path>` (REQUIRED): one of
  - Absolute path to a `<artifact-root>/{plans,research,documents}/*` directory
  - Fuzzy short name (for example, `se-seminar-tfrestormer`) — resolve with `ls -d <artifact-root>/{plans,research,documents}/*$ARG* 2>/dev/null`. Use a single match. For multiple matches, ask which artifact to use in the conversation language and follow the adapter pause/autonomy rule; if no answer arrives, use the most recently modified candidate. Return an error for zero matches.
- `--scope` (default `auto`): select the aspect set. An explicit user value takes precedence. Otherwise, infer an appropriate set from artifact mode, refinement count, status, and structure. Map `facts | style | structure | cross-ref | coverage | all` to the type-specific groups in Stage B.
- `--read-only` (default for plans): for plan artifacts, skip aspects that require executing tests or linters and perform only static inspection. For research and document artifacts, read-only behavior is implicit and the flag is a no-op; report that those audit types are always read-only.
- `--report-only`: skip Stage E. Produce the report and stop without dispatching follow-up edits.
- `--no-fact-check`: opt-out flag honored per `feedback_factcheck_principles.md` Principle 0. If present, the `facts` aspect (and the `coverage` aspect's cards-set diff) are **skipped** before Stage C aspect dispatch — i.e., the aspect skip happens at the _pre-check_ stage, not via filtering after lint runs. Other aspects (style / structure / cross-ref / Tier / cross-card / test / lint / code review / TODO) still run. Stage D report emits an informational line at the top of "Aspects checked": `ℹ facts/coverage aspects: skipped via --no-fact-check flag (memory feedback_factcheck_principles Principle 0)`. This is the _only_ allowed disable mechanism for fact verification; ad-hoc prompt evasion must not be honored.

## Process

Run `Stage A → B (B.1/B.2) → B.5 → C → D (D.5) → E` in order. The references below preserve the complete procedures, signal tables, lint definitions, templates, and prompts; read a stage's reference before executing it.

| Stage | Content | Reference |
|---|---|---|
| Stage A | Detect artifact type (plans / research / documents) | `scope-and-baseline.md` |
| Stage B · B.1 · B.2 | Determine effective scope from auto-scope signals and type-specific aspect mappings | `scope-and-baseline.md` |
| Stage B.5 | Ingest the minor-log baseline for document and research artifacts (P1 input) | `scope-and-baseline.md` |
| Stage C | Run per-aspect pre-checks and lints for document, research, or plan artifacts | `aspect-lints.md` |
| Stage D · D.5 | Render the report, polish through the `editorial/polish` unit, and format the chat summary | `report-and-autofix.md` |
| Stage E | Auto-fix chain (default — `--report-only` opt-out) | `report-and-autofix.md` |

## Constraints

- **Audit pass is read-only** — Stage A-D never modify the audited artifact (the audit log is written under `_internal/audit/`). Stage E _dispatches a separate skill_ (`autopilot-code` or `autopilot-refine`) which then makes edits per its own confirmation flow. With `--report-only`, Stage E is skipped entirely.
- **No web fetch** — inspect only local `<artifact-root>/*` files through direct reads, searches, and static scans.
- **No delegated role invocation** — run `/audit` in the current agent session without dispatching `research/*` or `qa/*` units. A future version may add intensity-derived, agent-backed linting.
- **Type-specific aspects** — research aspects do not run on documents artifacts and vice versa. `--scope cross-ref` on plans warns and skips.
- **Suggestions only in Stages A-D** — each 🔴 or 🟡 finding may include a "Suggested fix" line. Stage E dispatches those suggestions to the appropriate capability, which follows its own application, halt, review, QA, commit, and reporting protocol.

## When NOT to use

- To modify the artifact → `/autopilot-refine`.
- To check one typo or cosmetic issue → use `grep` or a direct read.
- To rerun a full pipeline → `/autopilot-{research,doc,code}` or `--from <stage>`.
- When the artifact does not yet exist and upfront analysis is needed → `/analyze-project` or `/autopilot-research`.

## Reference Index

| File | When to load (mandatory) | Content |
|---|---|---|
| `scope-and-baseline.md` | Every invocation, before Stages A-B.5 (required) | Artifact-type detection, effective-scope signals and mappings, and minor-log baseline ingestion with P1 diffing, cross-correlation, and chat output |
| `aspect-lints.md` | When running Stage C | Pre-checks for `--no-fact-check`; document fact/style/structure/cross-reference/coverage lints; research consistency/tier/coverage/cross-card lints; plan test/lint/code-review/TODO/implementation/semantic-deterministic checks |
| `report-and-autofix.md` | When running Stages D-E | Report template, `editorial/polish` unit polish, chat format, and auto-fix chain conditions, prompt, dispatch, logging, and rationale |
| `examples-and-checklist.md` | For invocation examples or follow-up after `--report-only` | Examples and the post-audit checklist |

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/audit.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability audit
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `reviews/audit/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/reviews/audit/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
