## Argument parsing

Parse `$ARGUMENTS` as follows:

- **query:** topic, paper title, arXiv ID, or PDF path left after flags
- **`--mode`:** `academic` by default, `technology`, or `market`
- **`--depth`:** `shallow`, `medium` by default, or `deep`
- There is no `--refs`; preprocess local reference material through `/analyze-project --mode paper`. autopilot-research automatically detects its output under `<artifact-root>/analysis_project/paper/`.
- **Verification rigor:** `--intensity` selects the stage graph and depth and derives rigor deterministically under CONVENTIONS §1.1. There is no separate `--qa` selector. Quick/light keeps selected checks small and skips fact checking. Standard+ runs source and claim checks when the graph selects them. Adversarial adds an external adversary and claim verification where supported.
- **`--from`:** `search`, `analyze`, or `report`
- **`--no-clarify`:** skip scope clarification and use the current query as-is
- **`--no-figures`:** skip Step 3.5 web figure extraction; cards remain unchanged except that no `**Figures**:` line is added

## Modes

Mode selects search sources, Phase A/B/C activation, and report templates. All modes retain the search → analyze → report structure.

### `academic` (default)

Use for paper-centered surveys, method comparisons, and field trends.

- Sources: arXiv, Semantic Scholar, OpenAlex, Hugging Face paper search, Google Scholar
- Phases: A skimming, B reference chaining, and C code/model search
- Reports: nine files covering briefing, landscape, core papers, baselines, technical deep dive, datasets, implementation, resources, and reading guide

### `technology`

Use for industry standards, ecosystems, vendor solutions, protocols, and deployment constraints.

- Sources: WebSearch for industry material and white papers; WebFetch for standards organizations such as 3GPP, ITU-T, IEEE, and W3C; arXiv and Hugging Face as supplements
- Phases: full skimming of standards and white papers; reduced reference chaining; active open-source implementation search
- Reports: `00_briefing.md`, `01_landscape.md`, `02_standards.md`, `03_vendor_comparison.md`, `04_technical_deep_dive.md`, `05_deployment.md`, `06_implementation.md`, and `07_resources.md`

### `market`

Use for markets, competitors, analyst reports, products, adoption rates, and company strategy.

- Sources: WebSearch for analyst material, news, earnings, and press releases; WebFetch for company and investor sites
- Phases: market-report/news skimming only; disable academic reference chaining and code search
- Reports: `00_briefing.md`, `01_market_overview.md`, `02_key_players.md`, `03_trends.md`, and `04_opportunities.md`

When mode is absent, infer it from query terms. Preserve these multilingual trigger literals for compatibility:

- `논문`, algorithm, method, SOTA → academic
- `표준`, codec, protocol, 3GPP, ITU, chip, MCU → technology
- market, `시장`, competitor, analyst → market

If none matches, use academic and report the fallback in one line. If at least two modes match, resolve it in scope clarification.

## Decision defaults

Proceed automatically with sensible defaults; there is no autonomy dial.

| Decision | Default |
|---|---|
| Search-results review | Continue automatically |
| Query-expansion rounds | Continue automatically |
| Phase B loopback | Continue up to the depth-gated limit |
| External material | Auto-include `analysis_project/paper/` when present; otherwise suggest `/analyze-project --mode paper` only when the user expects local material |
| Zero results | Stop with `pipeline_summary(failed)` |
| Report generation | Continue automatically |

## Automatic context detection for new entry or reentry

The skill inspects the request and cwd even without `--from`.

### 1. Detect `research/<topic>/`

| Condition | Route |
|---|---|
| No matching `<artifact-root>/research/<topic>/pipeline_state.yaml` | **New:** begin at input parsing |
| One or more matching state files | **Reentry:** read `last_completed_stage` and infer the intended stage |

Extract `<topic>` by fuzzy-matching request keywords, for example `"speech enhancement 분야 재조사"` → `speech-enhancement`. Ask the user on multiple matches.

### 2. Infer the stage on reentry

Preserve these quoted multilingual signals:

| Request signal | Inferred stage | Flow |
|---|---|---|
| `X 재조사`, `최근 paper 추가`, `search 다시` | `--from search` | New queries and expansion rounds, merged with existing cards |
| `분석 다시`, `Phase B reference chaining 다시`, `card 보강` | `--from analyze` | Rerun Phase A/B/C on existing search results |
| `보고서 갱신`, `report 다시`, `06_implementation 자리 수정` | `--from report` | Regenerate reports from cards and analysis summary |

### 3. One-screen confirmation

Localize prose to the user's communication language while preserving this information:

```text
=== autopilot-research invocation ===
Topic: <name>
Artifact: research/<name>/ with last_completed_stage, or absent/new
Request: "<one-line user request>"
Inferred route: new or --from <stage>

Proceed? (continue / another stage / new topic / stop)
```

An explicit `--from <stage>` always wins. Small cross-artifact corrections belong to `autopilot-refine`; reentry here reruns an entire stage.

## Resume with `--from`

- `search` → Paper Search
- `analyze` → skimming, chaining, code search, and analysis summary
- `report` → report generation plus the selected report-check gate

The positional argument may be the artifact directory or a fuzzy-matchable topic. Resolve with `ls -d <artifact-root>/research/*$ARG* 2>/dev/null`. Read `pipeline_state.yaml` to recover query, mode, depth, intensity, and clarified intent; CLI flags override stored values. Resume always skips scope clarification.

### `pipeline_state.yaml`

Update after each completed stage:

```yaml
pipeline: autopilot-research
query: <original query>
mode: academic                   # academic | technology | market
depth: medium
intensity: standard              # Derives rigor; no separate qa axis
clarified_intent: <string or null>
last_completed_stage: analyze    # clarify | search | analyze | report
artifact_dir: <absolute path>
```
