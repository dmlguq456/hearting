### Step 4: Report generation and selected report-check gate

At `standard+`, dispatch report generation and QA as separate dispatch-depth-2 stages under the [pipeline stage contract](pipeline-search-analysis.md). QA reads report files, not the prior session's conversation or hidden reasoning.

| Stage | Unit | Inputs | Outputs | Write class |
|---|---|---|---|---|
| Step 4a: Report Generation | `research/research-survey` unit (report branch) | `analysis_summary.md`, optional chaining and code search, search results, cards | Mode-specific report set | T1/T2 deliverables |
| Step 4b: QA Loop | review units (`research/fact-check`, `research/claim-verify`); external adversary at adversarial rigor | Reports and cards | `_internal/reviews/*`, optional `unresolved.md` | T3 raw |

When the reports require a new aggregate visualization or cross-card metric plot, the Step 4a worker records the asset `<spec>` in its artifact and the owner dispatches the `material/figure-gen` unit as a sibling node. The survey unit owns ordinary taxonomy tables, ASCII lineage diagrams, and per-paper cards.

#### Step 4a: Generate reports

```text
Dispatch unit research/research-survey:
  "Report generation.
   Analysis directory: {artifact_dir}
   Topic: {topic}
   Output directory: {artifact_dir}
   Date: {YYYY-MM-DD}

   Read:
   - analysis_summary.md, mandatory;
   - optional _internal/chaining_results.md;
   - optional _internal/code_search.md;
   - _internal/search_results.json;
   - at least the top 15–20 cards by discovery_count.

   Write T1/T2 report chapters to the artifact root. Do not modify _internal/.
   Return only file paths and a three-to-five-line summary in the user's
   communication language."
```

Shared report rules:

- Default artifact language to the user's communication language. An explicit audience, venue, publisher, or artifact-language requirement overrides it.
- Preserve titles, authors, venues, models, acronyms, metrics, and canonical domain terms when translation would reduce precision.
- Write one report set directly under `{artifact_dir}/`; do not create a separate `_internal/en/` mirror tree.
- End every comparison table with a bold **Takeaway** line.
- Source numbers and claims only from the analysis summary and cards.
- Link sibling reports as `[text](filename.md)`.
- After adversarial claim verification, label material findings with `high`, `medium`, or `low` confidence. Follow [claim-verify](../../../roles/units/research/claim-verify.md).
- Remove or qualify killed and abstained claims. Record them transparently in a localized section equivalent to `## Refuted or unverified claims`, with the contradiction source URL.

## Academic mode: nine files

### `00_briefing.md` — Executive Briefing

- Level 0: one-sentence summary.
- Level 1: three-to-five-line key-findings summary.
- Level 2: one-page overview containing a Mermaid relationship graph, research-axes table, five to seven findings, recommended architecture stack, and model-size spectrum.
- Level 3: guide to every report file and the question it answers.

Adapt graph subgroups to the domain; do not hard-code KWS-specific Backbone/QbE/QbT/On-Device categories when the topic differs.

### `01_landscape.md` — Research Landscape

- Formal problem definition and relevant variants.
- A multidimensional taxonomy appropriate to the field.
- Evolution table by period, transition, and representative paper.
- Detailed research-axis breakdown with paper counts.
- Comparison of major input, enrollment, or interaction paradigms where applicable.

### `02_core_papers.md` — Core Paper Analysis

- Grade papers as must-read (`discovery_count >= 5` or citations >100), close-read (`>=3` or >30), and reference.
- Draw domain-appropriate ASCII lineage diagrams.
- For must-read and close-read papers, include authors, venue/year, discovery and citation counts, code, core insight, architecture, key results, limitations, and connections.
- Put reference-grade papers in a compact table.

### `03_baselines.md` — Benchmark Comparisons

Build benchmark tables from the actual cards. Choose datasets, metrics, compute columns, deployment tiers, and task variants relevant to the topic. Include model-size or deployment spectra when supported. Never copy a domain-specific table schema onto unrelated research and never invent missing numbers.

### `04_technical_deep_dive.md` — Technical Deep Dive

- Cover five to eight themes as problem → approach comparison → key insight.
- Include a loss, objective, or core-mechanism comparison table when applicable.
- End with five to eight unresolved problems, each rated for difficulty and impact with plausible directions.

### `05_datasets.md` — Dataset Specifications

- Detail primary benchmarks and training datasets with available year, size, participants, labels, language, access, license, and observed usage.
- Include augmentation or noise resources where relevant.
- Add a dataset-usage map and recommended benchmark combinations by scenario.

### `06_implementation.md` — Goal-adaptive Roadmap

Infer and state one primary goal at the top:

```markdown
> Inferred goal: {goal} — {one-line rationale}
```

Infer meaning across languages. Common functional cues include:

- `build`: `구현`, implement, develop, reproduce, project.
- `seminar`: `세미나`, `발표`, lecture, slides, talk.
- `write`: `논문 작성`, `survey 쓰기`, review writing, thesis.
- `research`: `연구 방향`, open problem, hypothesis, what's next.
- `adopt`: `기술 도입`, `선택`, `어떤 모델 써야`, production adoption.

Default to `build` only when ambiguity remains and record the assumption.

Goal templates:

- **build** — Decision matrix, phased implementation plan, runnable illustrative snippets, paper-to-code map, and risk assessment.
- **seminar** — Chapter-level slide outline, chapter cheatsheet, backup deep dives, reproducible demo candidates, and anticipated Q&A. Do not generate slide-by-slide content.
- **write** — Section outline, argument scaffold, figure and table candidates, citation map, and writing timeline.
- **research** — Ranked open problems, testable hypotheses, minimal experiments, direction matrix, and scientific-risk register.
- **adopt** — Weighted criteria, three-to-five candidate shortlist, pilot plan, integration considerations, and technical plus organizational risks.

Adapt headings, decision keys, and phase counts to the domain. Card evidence drives the template, not the reverse.

Output scope is limited to the report set. Do not generate a paper draft, implementation, slide deck, or PPTX. Downstream pipelines own final-form artifacts.

Always close `06_implementation.md` with `## Next Pipeline` and one copy-paste-ready recommendation:

| Goal | Recommended command | Rationale |
|---|---|---|
| build | `/autopilot-code --mode dev "<task>"` | Run the code plan, execution, test, and report loop. |
| seminar | `/autopilot-draft "<task>" --mode presentation` | Generate slide-by-slide Markdown; PowerPoint transfer remains manual. |
| write | `/autopilot-draft "<task>" --mode paper` | Generate a paper strategy and paste-ready draft. |
| research | `/autopilot-draft "<task> grant proposal" --mode doc` | Turn hypotheses and experiments into proposal prose, or remain research-only. |
| adopt | `/autopilot-draft "<task> tech adoption report" --mode doc` | Produce a structured adoption decision document. |
| review | `/autopilot-draft "<task> peer review" --mode doc` | Requires preprocessing the venue review form first. |

Add a boundary disclaimer in the report language equivalent to: “This file is a high-level plan derived from field analysis. Hand full document creation or code implementation to autopilot-draft or autopilot-code.” autopilot-draft discovers `research/{topic}/` implicitly, so no separate path flag is required.

### `07_resources.md` — Code, Data, and Models

- Tier resources as directly usable, infrastructure/backbone, and supplementary.
- For repositories, record paper, popularity signal when available, language, last update, reproducibility, notes, and a one-line quick verification command.
- List high-impact papers without available code.
- For pretrained models, record architecture, parameters, framework, checkpoint, URL, and one-line verification command with expected output shape.
- Add a reproducibility matrix for code, data, checkpoint, and overall rating.

Derive verification commands from a repository quickstart, inference docstring, or model card. These commands are input to downstream specification prechecks; do not invent them.

### `08_reading_guide.md` — Reading Paths

Provide four or five purpose-based tracks, such as newcomer, efficient models, implementation, research gaps, and specialized deployment. Each track includes audience, goal, five to seven ordered papers, a reading point per paper, estimated time, and required/recommended/optional markers localized to the report language.

## Technology mode: seven reports (`00_briefing` through `07_resources`)

### `00_briefing.md`

One-line summary, three-to-five findings, one-page overview, Mermaid technology landscape, and three actionable insights.

### `01_landscape.md`

Category taxonomy, technology-by-category matrix, lineage diagram, and adoption stage: emerging, mainstream, or legacy.

### `02_standards.md`

Inventory standards body, specification ID, scope, year, and status. Detail mandatory and optional features, profiles, levels, and cross-specification dependencies. End with separate production and research recommendations.

### `03_vendor_comparison.md`

Compare vendor, product or SDK, license, platform, strengths, weaknesses, feature support, and cost model. End with scenario-specific recommendations.

### `04_technical_deep_dive.md`

Cover three to five core themes with problem, algorithm comparison, and insight. Add equations, pseudocode, or state machines only when useful. Analyze latency, quality, and complexity tradeoffs.

### `05_deployment.md`

Reference architectures, latency budget, migration paths, failure modes, mitigations, and an applicable cost model.

### `06_implementation.md`

Use the shared goal-adaptive roadmap, usually prioritizing `build` or `adopt`.

### `07_resources.md`

Tier open-source code, models, and tools; include platform, license, evaluation resources, and evidence-backed quick verification commands.

## Market mode: five files

### `00_briefing.md`

One-line summary, three-to-five findings, one-page overview, and three strategic implications.

### `01_market_overview.md`

TAM, SAM, SOM; segmentation by relevant region, customer, or use case; three-to-five-year growth projections; and a source/date/confidence table. Clearly separate reported values from inference.

### `02_key_players.md`

Profile five to ten players with supported revenue or share, products, strategy, and recent moves. Include a two-dimensional positioning map and recent M&A, partnership, or funding activity.

### `03_trends.md`

Drivers, inhibitors, potential disruptors, and a short-, medium-, and long-term timeline.

### `04_opportunities.md`

Identify unmet needs, compare organic, partnership, and acquisition entry options, maintain a risk register, and prioritize recommended actions.

## Final generation directives

- Use `graph TD` and style key nodes in Mermaid diagrams.
- Keep code snippets runnable and clearly illustrative; do not claim untested execution.
- Use only card and analysis-summary facts.
- Write files rather than returning report bodies in chat.

#### Step 4a polish: optional editorial pass

At `standard+`, dispatch one `editorial/polish` unit mode-B pass over the selected report set. Skip at `direct`, `quick`, or on explicit user request.

```text
Mode B — polish multiple files in place.
Directory: {artifact_dir}/
Files: the complete report set for the selected mode.

Use the `editorial/polish` unit contract.
Improve natural phrasing in the selected report language, terminology consistency,
line breaks, bullets, and spacing. Preserve claims, values, and citations. Preserve
canonical domain terms when translation would reduce precision. Apply terminology
decisions consistently across files.

Return a change summary and at most three deliberate terminology decisions.
```

#### Step 4b: QA loop

Derive rigor from `--intensity`; see [CONVENTIONS §1.1](../../../core/CONVENTIONS.md#11-verification-rigor-tiers).

| Rigor | Quality review | Fact check | Claim verification | Rounds |
|---|---|---|---|---|
| `quick` | One fast spot-check | Skip | Skip | One; never reinvoke |
| `light` | One fast review | Skip | Skip | Up to two |
| `standard` | One deep review | One fast fact-check | Skip | Up to two |
| `thorough` | Two deep reviews | One fast fact-check | Skip | Up to two |
| `adversarial` | Two deep reviews plus external adversary | One fast fact-check | One skeptical N-vote verifier | Two plus one external pass |

The quality reviewer owns coverage, non-fabrication, progressive disclosure, and roadmap actionability. The fact-checker compares report claims verbatim against cards. The adversarial claim verifier searches for external contradictory evidence under [claim-verify](../../../roles/units/research/claim-verify.md).

```text
round = 0
review_dir = {artifact_dir}/_internal/reviews/

Loop:
  increment round
  run the selected reviewer roles in parallel

  Quality prompt:
    Review coverage, non-fabrication, progressive disclosure, and roadmap
    actionability. Do not individually verify citations. Write
    round_{round}_quality.md and return path plus one-line verdict.

  Fact-check prompt:
    Compare up to 30 material report claims verbatim with cards/*.md.
    No matching card is a red fabrication risk. Output:
    | Report | Section | Claim | Source card | Match | Severity |
    Write round_{round}_factcheck.md and return path plus one-line verdict.

  Adversarial claim-verify prompt:
    For up to 25 material claims, run three skeptical voters that try to refute
    each claim using external evidence, source quality, recency, and support.
    Kill on at least two refutes. Require two valid votes; otherwise abstain.
    Output every kill and abstention with confidence and contradiction URL to
    round_{round}_claimverify.md.

  if no red findings: exit
  if quick: write unresolved.md with any residuals and exit
  if round < 2: re-dispatch the survey unit with quality findings, mandatory named-card
     grounding, and removal or qualification of killed claims
  otherwise: write unresolved.md, tag fact residuals [FACT-RESIDUAL], and exit
```

Move killed claims to a localized refuted/unverified section with evidence. Downgrade abstentions to low confidence.

#### Step 4c: Status check

Require `{artifact_dir}/00_briefing.md`. If absent, write a failed pipeline summary and stop.
