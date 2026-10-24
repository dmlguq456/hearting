## Pipeline

> **Stage-dispatch contract** (`standard+`, OPERATIONS §5.10 ③④, SD-1, SD-2): dispatch each durable stage as an independent dispatch-depth-2 headless session. The named unit runs inside that stage. The dispatch-depth-1 conductor passes artifact paths and reads verdicts or status only; each stage reads inputs from files. Keep `direct`, `quick`, and micro-stages inline. Dispatch depth-gated stages only when their depth condition is met. Stage sessions must not redispatch because dispatch depth 3+ is forbidden.

| Stage | Unit | Inputs | Outputs | Write class |
|---|---|---|---|---|
| Step 2: Source Search | `research/research-survey` unit | Orchestrator queries, optional HF `paper_search` results | `_internal/search_results.json` | T3 raw |
| Step 2e: Query Expansion | `research/research-survey` unit | Existing search results plus new keyword queries | Merged `_internal/search_results.json` | T3 append/merge |
| Step 3b: Parallel Skimming | `research/research-survey` unit, parallel batches | Search results and browser extracts | `cards/{paper}.md` | T1/T2 deliverables |
| Step 3c: Reference Chaining | `research/research-survey` unit | Cards and search results | `_internal/chaining_results.md` | T3 raw |
| Step 3e: Analysis Summary | `research/research-survey` unit | Cards, optional chaining and code search | `analysis_summary.md` | T1/T2 deliverable |
| Step 4a: [Report Generation](report-generation.md) | `research/research-survey` unit | Analysis summary, internal evidence, cards | Mode-specific `{00-08}_*.md` set | T1/T2 deliverables |
| Step 4b: [QA Loop](report-generation.md) | review units (`research/fact-check`, `research/claim-verify`); external-adversary review via the codex transport in adversarial mode | Reports and cards | `_internal/reviews/*`, optional `unresolved.md` | T3 raw |

`material/*` unit browser and image work is owner-dispatched support work inside these steps, not another durable stage. Step 2e merges rounds sequentially. Parallel Step 3b batches write distinct card files, so no lock-free concurrent mutation targets the same file.

### Step 1: Parse and validate input

- Detect keyword, paper title, arXiv ID, PDF path, or folder path.
- Resolve `--mode` explicitly or infer `academic`, `technology`, or `market` from the request. Report the inference in one line. Defer a multi-match to Step 1.5.
- If `<artifact-root>/analysis_project/paper/` exists, include it as supplementary chaining input. If the user requests local PDFs but none have been analyzed, recommend `/analyze-project --mode paper`.
- Build a lowercase, hyphenated topic name of at most 30 characters.
- Set `artifact_dir = <artifact-root>/research/{topic}/`.
- Create the directory only after validation succeeds.

### Step 1.5: Scope clarification

This is the research-track instance of the [Autopilot Intake Gate](../../../core/CONVENTIONS.md#66-autopilot-intake-gate).

Clarify when mode selection or search breadth would materially change the report set, including:

- Two or more modes match.
- The query is shorter than about 50 Hangul characters or 12 English words and has no time range, platform, target metric, or comparable constraint.
- The query contains only meta-intent such as the functional literals `조사`, `분석`, or `survey`, with no deliverable or scope.

Suggested questions:

- `academic`: depth, must-read cutoff, and field boundary.
- `technology`: standards body and year, deployment context, vendor scope, and priority among performance, cost, and license.
- `market`: region, time range, named competitors, and decision objective.

Skip for `--no-clarify`, `--from <stage>`, or a concrete query with a clear mode. Pass the refined query to Step 2 and record a one-line `clarified_intent` in `pipeline_state.yaml`.

If a non-blocking clarification receives no answer, use the inferred mode, `depth: medium`, and the narrowest defensible scope, then report that assumption once.

### Step 2: Source search

Select sources by mode:

- `academic`: arXiv, Semantic Scholar, OpenAlex, Hugging Face `paper_search`, and Google Scholar.
- `technology`: industry and vendor sources, standards pages from bodies such as 3GPP, ITU-T, IEEE, and W3C, supporting arXiv work, and relevant Hugging Face models.
- `market`: analyst material, news, press releases, company sites, and investor pages. Disable arXiv, Semantic Scholar, and OpenAlex by default.

#### Step 2a: Initial query expansion

Generate two or three synonyms or alternative expressions from the user query so adjacent terminology is included in the first search. For example, expand “user-defined keyword spotting” with “query-by-example KWS” and “personalized wake word detection.”

```text
queries = [original_query, variant_1, variant_2]
```

This differs from Step 2e: Step 2a uses prior semantic knowledge; Step 2e extracts terminology from discovered sources.

#### Step 2b: Hugging Face prefetch

Attempt `paper_search` for every query before dispatching the survey unit. Store combined results as `hf_results_json`; set it to null and log the failure when unavailable.

#### Step 2c: Dispatch the Survey Unit

```text
Dispatch unit research/research-survey:
  "Research survey mode: Paper search.
   Queries: {queries_list}
   Original query: {original_query}
   Query type: {detected_type}
   Output directory: {artifact_dir}
   Route raw metadata to `{artifact_dir}/_internal/` and deliverable cards,
   chapter files, and analysis_summary.md to `{artifact_dir}/`.
   Max results per source per query: 10.
   Supplementary local paper analysis: {analysis_project_path_if_any}
   HF results: {hf_results_json_if_any}
   Skip a source that exceeds three minutes.

   Write `_internal/search_results.json` with this schema:
   {
     \"query\": \"string\", \"date\": \"YYYY-MM-DD\", \"sources_used\": [\"string\"],
     \"total_papers\": int,
     \"papers\": [{
       \"title\": \"string\", \"authors\": [\"string\"],
       \"year\": int|null, \"citation_count\": int|null,
       \"discovery_count\": int, \"sources\": [\"string\"],
       \"arxiv_id\": string|null, \"oa_url\": string|null,
       \"openalex_id\": string|null, \"referenced_works\": [\"string\"]|null,
       \"venue\": string|null, \"venue_tier\": int|null,
       \"raw_type\": string|null, \"url\": string|null
     }]
   }

   Google Scholar parsing:
   - block: <div class='gs_r gs_or gs_scl'>
   - title: stripped <h3> content
   - year: , (\\d{4})\\s*[-–]
   - citations: >Cited by (\\d+)<

   Return paths and a three-to-five-line summary in the user's communication language."
```

#### Step 2d: Validate search output

1. Parse `_internal/search_results.json`.
2. On invalid JSON, invoke the agent once more to repair the file.
3. Require a non-empty `papers` array and a title for every paper.
4. Stop with a failed pipeline summary if repair fails.
5. Stop when `total_papers == 0`, recording a localized equivalent of “zero search results.”

Stop similarly when the agent call itself fails or returns no output.

#### Step 2e: Expand from discovered terminology

Depth controls additional rounds:

- `shallow`: no expansion.
- `medium`: at most one extra round.
- `deep`: at most two extra rounds.

For each round:

1. Read paper titles and extract recurring or newly introduced terms.
2. Create two or three queries absent from the existing query set.
3. Dispatch the `research/research-survey` unit on only the new queries in merge mode:

   ```text
   Dispatch unit research/research-survey:
     "Research survey mode: Paper search.
      Queries: {new_queries_only}
      Original query: {original_query}; context only, do not search it again.
      Output directory: {artifact_dir}
      MERGE mode: append to `_internal/search_results.json`, increment
      discovery_count for duplicates, and add new papers."
   ```

4. Revalidate the merged file.
5. Stop early when fewer than three new papers appear or no genuinely new term can be extracted.

Continue automatically after expansion.

### Step 3: Source analysis

Activate phases by mode:

- `academic`: skimming, reference chaining, and code/model search.
- `technology`: full skimming and code/model search; disable academic reference chaining.
- `market`: skimming only; disable reference chaining and code/model search.

#### Step 3a: Browser precheck and prefetch

Use the runtime's browser-fetch preflight contract. When rendered browser access is available, identify records with neither `arxiv_id` nor `oa_url` and dispatch the `material/browser-fetch` unit to prefetch their landing pages into `_internal/browser_extracts/{filename}.txt`.

```text
Dispatch unit material/browser-fetch:
  "URLs: {paywall_url_list}
   Output directory: {artifact_dir}
   Write extracted text to `_internal/browser_extracts/`.
   Return successes and failures."
```

If browser access or prefetch fails, continue with abstract-only analysis.

#### Step 3b: Parallel skimming batches

Classify access before batching:

- `accessible`: has an `arxiv_id`, `oa_url`, or browser extract.
- `paywall-only`: none of the above; use metadata and abstract only.

Batching:

- Accessible and more than ten citations: one paper per full-read call.
- Ten or fewer citations, unknown citations, or paywall-only: up to ten papers per abstract-only call.
- `discovery_count >= 3` plus accessible: upgrade to a one-paper full read.
- Never repeatedly fetch a paywall-only source.

```text
Dispatch unit research/research-survey:
  "Research survey mode: Paper analysis.
   Papers: {batch_json}
   Output directory: {artifact_dir}
   Supplementary input: {analysis_project_path_if_any}
   Browser extracts: {artifact_dir}/_internal/browser_extracts/
   Per-paper timeout: 60 seconds. Batch budget: 10 minutes.
   Skip redirect loops or empty responses.
   Return paths and a summary in the user's communication language."
```

Launch independent batches in parallel. Log an individual failure and continue; stop only when no batch succeeds.

#### Step 3c: Reference chaining

Skip at `shallow` or in modes where citation-graph chaining is disabled.

```text
Dispatch unit research/research-survey:
  "Research survey mode: Reference chaining.
   Paper cards: {artifact_dir}/cards/
   Search results: {artifact_dir}/_internal/search_results.json
   Depth: {depth}
   Output: {artifact_dir}/_internal/chaining_results.md"
```

Extract papers with `reference_frequency >= 2`. If new papers exist and the loopback limit is not reached, skim the top ten new papers and rerun chaining. Limits: one loop at `medium`, two at `deep`.

#### Step 3d: Code and model search

```text
Dispatch unit research/research-survey:
  "Research survey mode: Code and model search.
   Paper cards: {artifact_dir}/cards/
   Output: {artifact_dir}/code_resources/
   Aggregate: {artifact_dir}/_internal/code_search.md"
```

#### Step 3e: Compile the analysis summary

```text
Dispatch unit research/research-survey:
  "Research survey mode: Compile analysis summary.
   Inputs: cards/, optional _internal/chaining_results.md,
   optional _internal/code_search.md.
   Set chaining_available and code_search_available.
   Output: {artifact_dir}/analysis_summary.md
   Return the path and a summary in the user's communication language."
```

Require a non-empty `analysis_summary.md`. At `shallow`, disabled chaining is an intentional success. For other missing optional phases, mark the run partial, warn, and continue.

### Step 3.5: Optional web figure extraction

After cards are written, extract figures only for accessible papers. Skip paywall-only papers, `quick` intensity, or `--no-figures`.

```text
Dispatch unit material/web-image-search:
  "Papers: [{arxiv_id, paper_id, title}, ...]
   Output: {artifact_dir}/figures/

   Per paper:
   1. Try https://ar5iv.labs.arxiv.org/html/{arxiv_id}; parse <img> or
      <figure>, and retain meaningful images at least 200x200 pixels.
   2. Fall back to https://www.arxiv-vanity.com/papers/{arxiv_id}/.
   3. Fall back to https://arxiv.org/pdf/{arxiv_id}; temporarily download,
      extract raster images, then delete the PDF.
   4. Record zero figures if every path fails.

   Write `{paper_id}_fig{N}.png` and `figure_index.md`."
```

Add one functional card field after frontmatter or `## Reference`:

```markdown
**Figures**: ../figures/{paper_id}_fig1.png · ../figures/{paper_id}_fig2.png
```

Use `**Figures**: (none extracted)` when no figure was obtained.

ar5iv does not cover every recent paper. Its HTML usually preserves vector figures better than the PDF raster fallback. Treat extracted figures as research references, preserve attribution for external distribution, and expect occasional manual recropping or replacement.
