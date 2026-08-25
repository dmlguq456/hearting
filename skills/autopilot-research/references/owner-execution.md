# autopilot-research

Run shared upfront research across academic, technology, and market domains through a `search → analyze → report` pipeline, then route the result to downstream work such as `autopilot-draft` or `autopilot-code`. Keep only routing and stage contracts here; load the references below for detailed orchestration and report templates.

Store outputs under `<artifact-root>/research/<topic>/`. Follow the three-tier output convention (`<agent-home>/core/CONVENTIONS.md#5-skill-output-convention--t1t2t3`): keep raw metadata such as `search_results.json`, `phase_a_*.json`, `chaining_results.md`, and `code_search.md`, together with reviews, under `_internal/`; keep T1/T2 chapters and `cards/` at the research root. Resolve `<artifact-root>` using the workspace assumption (`<agent-home>/core/CONVENTIONS.md#51-workspace-assumption`).

## Invocation

Infer the mode from the request and propose sensible options when confirmation is required by the active workflow. Use `academic` only as the fallback when the domain remains unclear. Default to `medium` depth. Derive intensity from scope, risk, and the requested confidence rather than assigning the highest rigor to every survey. Keep clarification enabled unless `--no-clarify` is explicit.

Route narrower requests directly:

- Send a single-paper fetch or paywall lookup to the `material/browser-fetch` unit.
- Send bulk PDF figure extraction to the `material/pdf-extract` unit.
- Send web reference-image search to the `material/web-image-search` unit.
- Use `autopilot-refine` for a small addition or correction in an existing research folder.
- Honor an explicit `autopilot-research` invocation without adding another confirmation step.

## Artifact Language

Follow the audience-language-first rule in `<agent-home>/roles/response-policy.md`: an explicit artifact or audience language takes precedence; otherwise use the conversation language. Do not derive a fixed output language from the language of this skill file.

## Mode Routing

The mode selects search sources, active analysis phases, and the report set. The `search → analyze → report` structure is shared across all modes. If a request matches multiple modes, resolve the choice during Step 1.5.

| Mode | Scope | Analysis | Report set |
|---|---|---|---|
| `academic` | Scholarly literature survey | Phases A, B, and C | 9 reports, `00_briefing` through `08_reading_guide` |
| `technology` | Standards, libraries, vendors, and ecosystems | Phases A and C | 7 reports, `00_briefing` through `07_resources` |
| `market` | Markets, competitors, and business models | Phase A | 5 reports, `00_briefing` through `04_opportunities` |

Read `invocation-and-modes.md` for source selection, report details, inference rules, and fallbacks.

## Arguments

`<query> [--mode academic|technology|market] [--depth shallow|medium|deep] [--intensity direct|quick|standard|strong|thorough|adversarial] [--no-clarify] [--no-figures] [--from search|analyze|report]`

- `query`: Research topic, paper title, arXiv ID, or PDF path after flags are removed.
- `--mode`: Explicit domain override. Otherwise infer from the query, with `academic` as the final fallback.
- `--depth`: Default `medium`; gate Phase B loopback and query-expansion rounds.
- `--intensity`: Select the stage graph and derive verification rigor. There is no separate `--qa` axis. Reduce gates for `direct` and `quick`, run fact-checking at `standard+`, and add an external adversary plus claim verification for `adversarial`.
- `--no-clarify` / `--no-figures`: Skip Step 1.5 / Step 3.5.
- `--from`: Re-enter at `search`, `analyze`, or `report`.

Read `invocation-and-modes.md` for exact defaults and flag behavior.

## Resume

Inspect both the request and current working directory to distinguish a new run from a resumed run. If `<artifact-root>/research/<topic>/pipeline_state.yaml` is absent, start at Step 1. If it exists, infer the intended `search`, `analyze`, or `report` re-entry stage from the request. An explicit `--from` always wins. Route one- or two-line corrections to `autopilot-refine`.

Read `invocation-and-modes.md` for the detection table, confirmation format, and `pipeline_state.yaml` schema.

## Pipeline

Proceed through routine decisions automatically. Pause only when the workflow requires a genuine user choice, such as an unresolved multi-mode match or an empty search result that materially changes the research plan.

| Stage | Steps | Work | Required reference |
|---|---|---|---|
| Intake | 1, 1.5 | Parse and validate input; clarify scope when needed | `pipeline-search-analysis.md` |
| Search | 2a–2e | Expand queries; prefetch Hugging Face metadata; dispatch the `research/research-survey` unit to search; validate results; run depth-gated expansion rounds | `pipeline-search-analysis.md` |
| Analyze | 3a–3e, 3.5 | Check browser access; run Phase A skim, Phase B chaining, and Phase C code search as enabled; write the analysis summary; extract web figures when enabled | `pipeline-search-analysis.md` |
| Report | 4a–4c | Generate the mode-specific report set; have the `editorial/polish` unit polish it; run the intensity-derived QA loop | `report-generation.md` |
| Close | 5, 6 | Write `pipeline_summary` and present the briefing | `summary-and-briefing.md` |

The references preserve detailed prompts, batching rules, QA tables, report templates, and decision-logging requirements.

## Safety and Context Rules

- Never fabricate citations, URLs, metrics, or source access.
- Continue with remaining sources when one source fails, and record the limitation.
- Do not expose a `--refs` flag. Read supplementary local material from `analysis_project/paper/` when available and relevant.
- Obey current provider limits and the active tool policy; do not rely on hard-coded historical rate limits.
- Require each delegated role to return file paths plus a 3–5 line summary.
- Store large results in files. Keep only counts, decisions, and a small leading sample in orchestrator context; do not load all of `search_results.json`.
- In merge mode, normalize titles by lowercasing, removing punctuation, and dropping leading English articles. Allow `discovery_count` only to increase.
- Clean up only browser processes launched by this run through the active browser harness. Never kill unrelated browser processes.

## Reference Index

| File | Load when | Contents |
|---|---|---|
| `invocation-and-modes.md` | Interpreting options, selecting mode sources/report sets, or resuming | Argument parsing, mode behavior, defaults, context detection, resume flow, and `pipeline_state.yaml` schema |
| `pipeline-search-analysis.md` | Running Steps 1–3.5 | Input parsing, scope clarification, source search, source analysis, and web figure extraction |
| `report-generation.md` | Running Step 4 | Mode-specific templates, editorial polish, QA loop, and status checks |
| `summary-and-briefing.md` | Running Steps 5–6 | Pipeline summary, briefing, and decision logging |

## Task

$ARGUMENTS

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/autopilot-research.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability autopilot-research
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `research/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/research/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
