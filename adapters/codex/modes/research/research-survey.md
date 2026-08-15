# Codex Research Research Survey Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/research/research-survey.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info research/research-survey`.
4. Obey the reported status, tool contract, runtime surface, and fallback before claiming support.

## Codex Runtime Mapping

- Status: `portable`
- Realization: `portable-persona`
- Requirement: read/cite primary sources through available Codex tools
- Note: Codex may use the mode fragment after reading roles/MODES.md and resolving portable roles.

## Use

- Use Codex file, terminal, approval, sandbox, hook, and skill surfaces.
- Run `adapters/codex/bin/preflight.sh write <file> [session-id]` before edits.
- For `tool-contract` modes, run the named contract check before claiming the tool-backed result.
- If a required local provider or executable is unavailable, report the unavailable contract instead of silently downgrading.
- Treat `adapters/codex/modes/research/research-survey.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/research/research-survey.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: research/research-survey
family: research
role: deep maker
worker_type: stage
floor: high
read_only: false
stance: none
io:
  verdict: [complete, partial, error]
  return: _shared/dual-io.md
tools: []
branches: [search, analysis, chaining, code-search, compile, report]
aliases: {}
---

# Unit: research/research-survey

Own paper search, analysis, cards, and report generation for the research
pipeline (the autopilot-research stages). Determine the branch from the first
line of the invocation prompt:

- "Research survey mode: Paper search" → **search**
- "Research survey mode: Paper analysis" → **analysis**
- "Research survey mode: Reference chaining" → **chaining**
- "Research survey mode: Code and model search" → **code-search**
- "Research survey mode: Compile analysis summary" → **compile**
- "Research survey mode: Report generation" → **report**

## Context Sources

Before starting, read the relevant local knowledge base in this order of
authority; skip missing directories silently:
(1) `<artifact-root>/analysis_project/paper/00_overview_and_constraints.md`
(design constraints), (2) `<artifact-root>/analysis_project/paper/` (paper
docs), (3) `<artifact-root>/research/` (prior surveys — highest version
authoritative), (4) `<artifact-root>/analysis_project/code/`, (5) agent
memory. If all are absent, state that the result relies only on agent memory
and web sources, then continue.

Preload user-profile defaults per `_shared/profile-preload.md`:
`02_paper_writing_style`, `04_analysis_methodology`, `05_domain_expertise`,
and `01_paper_figure_style` when discussing figures.

When a research detail is ambiguous and cannot be delegated back to the user
without blocking reversible progress: choose the lower-risk option, keep scope
to the request, follow existing repository patterns, align ambiguous research
details with the source paper's method, and record the uncertainty in a memo
while continuing.

## Language Rule

Write prose in the selected target language for the selected audience
(default: the user's current communication language, unless a publication,
external-audience, or artifact contract specifies another). Preserve paper
titles, author names, venues, URLs, code identifiers, model names, dataset
names, and metric names in their established source form. Preserve established
methodology terms (`attention`, `transformer`, `contrastive learning`,
`metric learning`, …) when translation would reduce precision. Keep canonical
machine-readable values in `search_results.json` unchanged.

## Branch: search (paper search)

The orchestrator provides multiple expanded queries (original + 3–5 variants),
the output directory, and optional pre-fetched HF results.

**Multi-query search:** search every configured source for each query variant.
A paper discovered by several queries naturally receives a higher
`discovery_count` — a strong centrality signal. Do NOT deduplicate away
repeat discoveries; cross-source frequency is the importance signal.

**Sources** (in order for EACH query; if any source takes >3 minutes, skip it):

1. **HF paper_search:** use pre-fetched results if provided in the prompt;
   otherwise skip (MCP tools are not available to this worker).
2. **OpenAlex:** `https://api.openalex.org/works?search={query}&per_page={max}`
   — title, authors, year, cited_by_count, oa_url, id.
3. **arXiv API:** `http://export.arxiv.org/api/query?search_query={query}&max_results={max}`
   — title, authors, arXiv ID, summary.
4. **Google Scholar direct:** `curl -s -L` with a browser User-Agent against
   `https://scholar.google.com/scholar?q={query}`; parse HTML for titles,
   years, citation counts, links. Rate limit: 3 s between requests, 50/day
   max. On CAPTCHA or empty response, skip.
5. **Web search:** `"Google Scholar {query}"`, `"{query} site:arxiv.org"` —
   supplementary.
6. **Semantic Scholar** (only if `S2_API_KEY` is set):
   `curl -s -H "x-api-key: $S2_API_KEY" "https://api.semanticscholar.org/graph/v1/paper/search?query={query}&limit={max}&fields=title,authors,year,citationCount,externalIds"`
   with a mandatory `sleep 1` after each call.

**Input type detection:** arXiv ID (NNNN.NNNNN) → fetch metadata first; PDF
path → read and extract keywords; refs folder → extract keywords per PDF.

**Merge & rank:** fuzzy-match titles across sources, merge metadata and source
lists, count `discovery_count`, then sort by discovery_count DESC, venue_tier
ASC (1 = best), citation_count DESC, year DESC. Treat missing tier as 5 and
missing citations as 0.

**Venue tiers** (formal-source authority grade):

- **Tier 1:** NeurIPS (NIPS), ICML, ICLR; ICASSP, Interspeech; ACL, NAACL,
  EMNLP; CVPR, ICCV, ECCV; major IEEE Transactions (T-ASLP, T-SP, …) and
  IEEE Signal Processing Letters.
- **Tier 2:** ASRU, SLT, WASPAA, ODYSSEY, EUSIPCO, APSIPA, MMSP; Speech
  Communication, Computer Speech & Language, JASA.
- **Tier 3:** other formal IEEE, ACM, or ISCA venues and workshop papers.
- **Tier 4:** unpublished / arXiv-only preprints.

The tier ladder is a deterministic ranking input for this branch; the user's
venue-preference context lives in `mem profile 05_domain_expertise` §6.

Derive venue from OpenAlex `primary_location.raw_source_name`, raw type, or
DOI patterns (e.g. `10.1109/icassp` = ICASSP); cross-check arXiv discoveries
in OpenAlex for formal publication. Venue tier measures venue reputation;
**source quality** is a separate axis (primary peer review / preprint /
secondary synthesis / unreliable) feeding claim-strength checks in
research/claim-verify.

**OpenAlex enrichment** (batch): for papers with arXiv IDs, fill
`referenced_works`, concepts, `cited_by_count`, and venue information.

**Output:** write `search_results.json` (schema provided by the orchestrator)
plus `search_results.md` (human-readable ranked table) to the output
directory.

**MERGE mode** (additional rounds): when the prompt says "MERGE mode", read
the existing `search_results.json` first; on fuzzy title duplicates increment
`discovery_count` and extend the `sources` array; append new papers; update
`total_papers`; regenerate `search_results.md`.

**Error handling:** if a source fails, skip and continue. If ALL sources
return 0 results, reformulate the query once (broaden keywords); if still 0,
write empty results and return an error verdict.

## Branch: analysis (paper analysis)

**Paywall fast-detect (BEFORE attempting access):** a paper with neither
`arxiv_id` nor `oa_url` is likely paywalled — if
`browser_extracts/{filename}.txt` exists, read it; otherwise jump straight to
the abstract-only fallback. Never WebFetch a likely paywall site (timeout /
hang risk).

**Per-paper timeout:** spend no more than 60 seconds obtaining any one paper
by any method, then fall through to the next rung.

**Access ladder** (try in order, fall through on failure or timeout):

1. arXiv HTML (`https://arxiv.org/html/{id}`; ar5iv equivalent) → full text +
   references + figure URLs. Skip without an arXiv ID.
2. Open-access HTML via `oa_url`. Skip without one.
3. Pre-extracted material-role output: `{output_dir}/browser_extracts/{filename}.txt`
   (the orchestrator has the material role pre-extract paywalled URLs before
   this phase). Skip if absent.
4. arXiv abstract page (`https://arxiv.org/abs/{id}`) or arXiv PDF. Skip
   without an ID.
5. Metadata fallback — OpenAlex/Crossref abstract, or a user-provided PDF via
   Read; always reachable (title + metadata alone if no abstract exists).

**Reading depth:** `citation_count > 10` (non-null) → full read (exclude
appendix); otherwise abstract-only. Exception: `discovery_count >= 3` AND
accessible (arxiv_id OR oa_url OR browser extract) → upgrade to full read;
`discovery_count >= 3` but NOT accessible → raise only the recommendation
grade, keep abstract-only (no paywall attempts).

**Reading recommendation grades** (user-facing priority, independent of
reading depth): citations > 100 → 🔴 must-read; 11–100 → 🟡 skim; ≤ 10 →
🟢 reference-only. Corrections: `discovery_count >= 3` upgrades ≤ 10 to 🟡;
every Tier 1 paper is at least 🟡 skim regardless of citation count (recent
Tier 1 papers matter despite low counts).

**Per-paper card** (`{output_dir}/cards/{year}_{first_author}_{arxiv_id_or_hash}.md`):

- **Venue:** formal venue + Tier 1–4; for arXiv preprints, mark
  "arXiv preprint" and check formal publication status.
- **Source quality:** `primary` (peer-reviewed venue/journal) / `preprint`
  (unpublished arXiv) / `secondary` (survey/blog synthesis) / `unreliable`
  (unverified). This feeds claim-verify's source-quality × claim-strength
  weighting (strong claims need primary). Separate axis from venue tier —
  tier = reputation, quality = verification strength.
- Reading recommendation grade; methodology (2–3 lines); performance metrics;
  experiment environment (GPU, training time, framework); datasets; baselines;
  limitations / open problems; key figures (arXiv HTML image URLs); code /
  checkpoints; connections (← builds on, → improved by).
- Record central claims with quotations where available. Never imply full-text
  support when only an abstract was read.

## Branch: chaining (reference chaining)

Via OpenAlex `referenced_works`:

1. Read all paper cards from `cards/`.
2. For each paper with an OpenAlex ID, fetch
   `https://api.openalex.org/works/{id}` and collect `referenced_works`.
3. Count `reference_frequency` across all papers.
4. Identify foundational papers: `reference_frequency >= 2` AND not in the
   original search.
5. Depth: medium = 1-hop; deep = 2-hop + lineage trace.
6. Write `chaining_results.md`: new papers, frequency table, Mermaid citation
   graph, recommended additions (top papers by reference_frequency).

The orchestrator controls loopback (medium: max 1, deep: max 2); this unit
handles only its single invocation scope.

## Branch: code-search (code & model search)

Runs only AFTER the analysis phase completes (skipped for shallow depth).
For each paper in `cards/`:

1. Web search: `"{paper_title} github"`, `"{paper_title} code"`.
2. Web search: `"{paper_title} huggingface model"`,
   `"site:huggingface.co/models {topic}"`,
   `"site:huggingface.co/datasets {topic}"`. (MCP hub tools are not available
   to this worker; if they become available, prefer them.)
3. Verify: checkpoints, training code, license, last commit, stars/forks.

**File isolation:** write per-paper files to
`{output_dir}/code_resources/{paper_filename}.md` plus an aggregate
`code_search.md`.

## Branch: compile (analysis summary)

Compile `{output_dir}/analysis_summary.md` from `cards/`,
`chaining_results.md`, and `code_search.md`. Contents: **phase status flags**
(`chaining_available`, `code_search_available` — true/false + reason), total
papers analyzed (full-read vs abstract-only counts), global caps applied,
citation graph (if chaining ran), code/model availability summary, key
findings (top-5 most-cited, top-5 most-connected).

## Branch: report (report generation)

The orchestrator provides `mode`, `analysis_dir`, `topic`, `output_dir`, and
the full mode-specific report structure (chapter outlines, table schemas,
diagrams) in its prompt — treat that prompt as canonical for chapter contents;
this unit fixes only the file inventory:

| Mode | Output files | Count |
|---|---|---|
| `academic` (default) | `00_briefing` / `01_landscape` / `02_core_papers` / `03_baselines` / `04_technical_deep_dive` / `05_datasets` / `06_implementation` / `07_resources` / `08_reading_guide` | **9** |
| `technology` | `00_briefing` / `01_landscape` / `02_standards` / `03_vendor_comparison` / `04_technical_deep_dive` / `05_deployment` / `06_implementation` / `07_resources` | **7** |
| `market` | `00_briefing` / `01_market_overview` / `02_key_players` / `03_trends` / `04_opportunities` | **5** |

If a "Mode: {mode}" line is absent, default to `academic` (9 files).

**Single source of truth:** read `analysis_summary.md` FIRST; its phase flags
override file existence. Do not read stale files from previous runs.

**Graceful degradation:** `chaining_available == false` → label the
relationship diagram "reference chaining incomplete" and rely on the cards'
Connections field; `code_search_available == false` → add a "code search
incomplete" notice and use card code metadata. Use cards and chapters to
expose incomplete enrichment rather than hiding it.

**Quality:** no fabricated citations, URLs, or metrics (Language Rule above
governs language and identifier preservation).

**QA cooperation:** if re-invoked with "Fix these 🔴 issues: ...", fix only
the listed issues.

## Return

Per `_shared/dual-io.md`. Return concise completion verdicts with paper counts
and artifact paths (e.g. "✅ search complete (N papers)", "✅ analysis
complete"); `error` when a branch could produce no results.

**When this node is a declared auxiliary arbiter** — the prompt names it as the
arbiter of an upstream `parallel_group` with `leg_class: auxiliary` legs (the
`compile`/`report` branches downstream of a fanned-out `retrieval`) — the
artifact's frontmatter carries `auxiliary_findings_considered` with exactly one
entry per realized auxiliary leg, each saying whether that leg's finding was
adopted or rejected and why:

```yaml
---
auxiliary_findings_considered:
  - "assumption-check: authority scope narrowed to peer-reviewed venues — adopted"
---
```

This is conditional, not unconditional: the same unit runs in positions where it
arbitrates nothing, and there the key is absent. The completion gate compares the
array length against the realized auxiliary leg count, so a wrong length or a
missing key is a gate failure, not a warning.

## Memory

Per `_shared/memory-flow.md`. Retention targets: key papers, core methods, and
important repositories per domain; useful domain source lists and query
patterns; domain-specific paywall patterns and recurring access failures;
venue-tier discoveries.
