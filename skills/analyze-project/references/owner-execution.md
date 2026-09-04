# analyze-project

Upfront analysis entrypoint. Structure primary code, paper, and document materials for downstream capabilities such as `autopilot-code`, `autopilot-lab`, `autopilot-draft`, and `autopilot-research`. This file defines routing and mode contracts; load the relevant reference only when its detailed procedure or template is needed.

## Routing Boundary

Invoke this Skill when the user explicitly asks to analyze existing code, a
paper, or document materials and no usable persistent analysis exists, when
existing analysis is demonstrably stale for the requested downstream work, or
when the user requests a refresh. Treat an explicit analysis request as
persistent output unless the user asks for conversational/read-only analysis
or no files. Artifact absence alone is not a trigger. A request only to
understand the project, recover prior context, resume work, or report current
status remains read-only orientation, not an `analyze-project` trigger.

Before invocation, run one targeted, agent-chosen memory recall and read a
shortened relevant hit in full by record ID. Then resolve `.agent_reports/`,
falling back to legacy `.claude_reports/` only when the canonical root is
absent; read the newest report/experiment artifact and current PRD/spec before
primary code or data, as defined by `core/WORKFLOW.md §0.1`. For orientation,
invoke no capability and write no artifact. Resolve drift as latest spec or
user confirmation, durable project fact, latest experiment contract, then
legacy document, and report the conflict instead of silently selecting an
older value.

> Caller note: this skill performs deep analysis. Use `high` or `xhigh` effort
> when the runtime supports it; at lower effort, analysis depth narrows
> automatically.

> **Output folder convention**: CONVENTIONS.md §5 (`<agent-home>/core/CONVENTIONS.md#5-skill-output-convention--t1t2t3`) (3-tier T1/T2/T3). Write this skill's outputs under `<artifact-root>/analysis_project/{code,paper,doc}/`. Keep each mode's main outputs at its root and raw scan logs or QA reviews under `_internal/`.

> **Workspace assumption**: Run from the project root. Create `<artifact-root>/` in the current directory and resolve input code, PDFs, or document materials from that directory or its descendants.
> Resolve `<artifact-root>` by preferring `.agent_reports` and falling back to legacy `.claude_reports`: CONVENTIONS §5.1 (`<agent-home>/core/CONVENTIONS.md`).

## Language Rule

- Follow an explicit artifact or audience language when provided.
- Otherwise, write user-facing analysis in the conversation language according to `<agent-home>/roles/response-policy.md`.
- Preserve source identifiers, code, paths, quotations, and domain terms when translation would reduce precision.

## Argument Parsing

```
/analyze-project [--mode code|paper|doc] [<scope/target>] [--skip-qa] [--full]
```

- `--mode <X>`: explicit mode selection. If omitted → auto-detect (code vs doc only; paper requires explicit).
- `--skip-qa`: skip Phase 5 QA Verification.
- `--full`: force full reanalysis and ignore existing output. By default, use **incremental** analysis when existing output is found (reanalyze only changed files, typically 10-20% of full cost); otherwise run a full analysis.
- Positional `<scope/target>` (**optional in all modes**; default: cwd auto-discovery):
  - `code`: narrow the scope with a module keyword (`engine`) or subdirectory (`src/models/`). Default: project root.
  - `paper`: override the external folder (for example, `~/papers/2024/`). Default: cwd plus automatic discovery of one-level subfolders (`papers/`, `refs/`, or `pdfs/`).
  - `doc`: override the external folder or subtask name. Default: cwd plus automatic discovery of one-level subfolders (`docs/`, `reviews/`, `templates/`, or `reviewer_comments/`). When specified, use the external folder path as the input scope.

> **Workspace principle**: Discover project-local PDFs, reviewer comments, templates, and other inputs from cwd by default. Use the positional argument to narrow the project scope or point to an external folder.

### Mode Auto-Detection (when `--mode` omitted)

Inspect current directory:

| Indicators | Detected mode |
|---|---|
| `src/`, `lib/`, `models/`, `.git`, `package.json`, `pyproject.toml`, OR `*.py`/`*.ts`/`*.go`/`*.rs` files at root | **code** |
| Many `*.pdf` / `*.docx` / `*.md` files; no source dirs; no build manifests | **doc** |
| Both indicators present | **code** (default — user can override with `--mode doc`) |
| Neither / unclear | Ask which mode to use (`code`, `paper`, or `doc`) in the conversation language. Follow the active adapter's pause/autonomy policy; if no answer arrives, proceed with the mode best supported by cwd signals. |

> **`paper` mode is never auto-selected** — paper analysis requires explicit `--mode paper` because PDF presence alone is ambiguous (could be reviewer comments, templates, etc. for doc mode). The boundary between paper and doc is genuinely fuzzy in the wild.

## Output Directories

| Mode | Output | Scoping |
|---|---|---|
| code | `<artifact-root>/analysis_project/code/` | flat (project-level, accumulates over time) |
| paper | `<artifact-root>/analysis_project/paper/` | flat (project's paper collection accumulates) |
| doc | `<artifact-root>/analysis_project/doc/{name}/` | per-task subdir |

For doc mode, derive `{name}` from the input folder basename (positional argument) or the cwd basename (default when the positional argument is omitted). Example: running `--mode doc` in `/.../tf_restormer/` produces `analysis_project/doc/tf_restormer/`; `--mode doc tf_restormer_patent` overrides it with `analysis_project/doc/tf_restormer_patent/`.

---

## Mode Overview

All three modes follow the same flow: discover input → analyze directly or dispatch the `research/research-survey` unit (composed route) → write structured output under `<artifact-root>/analysis_project/` → verify. The relevant reference preserves each mode's complete phase procedures, prompts, and templates.

- **code** — Analyze codebase modules; produce topic-specific Markdown, an Interface Reference, an update to the project's active instruction file when applicable, and four lab inputs (`experiment_conventions`, `experiment_readiness`, `cleanup_candidates`, and `similar_models`). In Phase 0, inspect `_last_run.yaml` to choose incremental or full analysis. Run Phase 5 QA by dispatching the `qa/code-review` unit. → `mode-code.md`
- **paper** — Dispatch analysis of reference PDFs and the user's own paper to the `research/research-survey` unit. Branch by purpose: (A) survey external PDFs for citation and grounding, or (B) review an in-progress `main.tex`, with `00_self_paper_analysis.md` as the main output. Treat `00_overview_and_constraints.md` as the highest-priority integrated output. → `mode-paper.md`
- **doc** — Classify writing materials (reviewer comments, templates, samples, and miscellaneous inputs), then dispatch analysis to the `research/research-survey` unit; produce per-task `doc/{name}/` output and a `00_overview.md` inventory. → `mode-doc.md`

## Composed-route dispatch (large analysis)

`analyze-project` owns no registry recipe (it is a `pre`-group capability, not
`entry`), so unit-grade analysis dispatches through a **composed route**, never a
native sub-agent. Native sub-agents are one-shot helpers; unit-grade work is
registered dispatch (v3 boundary). Assemble the route with
`utilities/compose-route.py`, which turns a unit list into a full-shape recipe,
assembles nested-eligibility dispatch evidence, and delegates sealing to
`capability-route.py compile --composed-recipe`:

```bash
python3 "$AGENT_HOME/utilities/compose-route.py" \
  --capability analyze-project --capability-mode code \
  --slug "$TASK_SLUG" \
  --units-json '[{"id":"survey","unit":"research/research-survey","write_scope":["analysis_project/code/**"],"gate":"research-retrieval"}]' \
  --cwd "$PWD" --artifact-root "$ARTIFACT_ROOT" \
  --tracking tracked --spec-read "$PRD_SHA" --drift-verdict within-spec \
  --workflow-mode tracked --artifact-guard conductor-prechecked \
  --output "$ARTIFACT_ROOT/.runtime/routes/<route_id>.json"
```

- `role` and `kind` are derived from each unit's frontmatter — do not restate them.
- `gate` is required for a unit that backs several unit-io gates (e.g.
  `research/research-survey` → `research-retrieval` / `research-synthesis` /
  `research-report`); a single-gate unit (e.g. `qa/plan-review`) auto-derives it.
- The tracked-gate fields (`--spec-read`, `--drift-verdict`, ...) are stated by
  the caller and passed through unchanged; the helper never fabricates them.
- Dispatch evidence comes from the live nested-eligibility probe by default; pass
  `--dispatch-evidence <file>` when you already hold checked evidence. With no
  supported tuple the route fails closed.

**Small or incremental analysis stays inline.** A single-module reanalysis or a
Phase 0 incremental pass (typically 10-20% of files) does not justify a composed
route — run it in the owner session and reserve compose-on-demand for large,
fan-out analyses.

## Completion

Once the selected mode's analysis directory and QA evidence are durable, evaluate the optional artifact-sink extension from `WORKFLOW.md §0.2`. When available, offer the canonical analysis artifact through the app-neutral receipt contract; on unavailable, record `skipped/extension-unavailable`. This applies across code, paper, and doc modes without duplicating their mode procedures.

## Reference Index

| File | When to load (mandatory) | Content |
|---|---|---|
| `mode-code.md` | When running `--mode code` (required) | Phase 0 incremental/full branch and `_last_run.yaml` schema; Phase 1 codebase analysis; Phase 2 documentation and required Interface Reference sections; Phase 3 project-instruction update; Phase 3.5 templates for the four lab inputs; Phase 4 coverage verification; Phase 5 QA verification |
| `mode-paper.md` | When running `--mode paper` (required) | Complete `research/research-survey` dispatch prompt: inputs, §0 complete own-paper analysis, §1-6 reference survey, `00_overview_and_constraints.md` structure, and post-analysis steps |
| `mode-doc.md` | When running `--mode doc` (required) | Phase 1 input-scope resolution and classification heuristics; Phase 2 per-category delegation prompt for reviewers, formats, samples, and miscellaneous input plus `00_overview.md`; Phase 3 verification |
| `outputs-and-integration.md` | When finalizing output structure or selecting follow-up capabilities | Standard output structure by mode, integration rules for `autopilot-code`, `autopilot-lab`, `autopilot-draft`, and `autopilot-research`, and a typical workflow |

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/analyze-project.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability analyze-project
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `analysis_project/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
