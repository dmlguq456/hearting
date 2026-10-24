### Stage C: Per-Aspect Lint — report only

Before dispatching aspects, if `--no-fact-check` is present, remove `facts` and `coverage` from the resolved set and record that the explicit flag disabled them. Ad-hoc prompt exemptions do not disable these checks.

Each issue has this stable shape:

```text
(aspect, file, line_range, severity 🔴|🟡|🟢, message, suggested_fix|null)
```

#### Document Aspects

Resolve the cards source identically for `facts` and `coverage`:

1. If `pipeline_summary.md` frontmatter or `strategy.md` contains `cards_source: <path>`, use that research topic as the primary root.
2. Include a self-contained `{artifact_dir}/cards/` when present.
3. Only when both are absent, search `<artifact-root>/research/*/cards/*.md` and warn that generic acronyms may false-positive; recommend adding `cards_source`.
4. If no cards exist, skip facts and coverage with an informational line. Continue style, structure, and cross-reference checks.

- **facts:** scan draft and strategy for model names, venues, years, task categories, arXiv IDs, metrics, and section context. Classify every lookup:
  - **cards-verbatim ✅:** exact value appears in card body or the legacy `## 메타` field.
  - **cards-name-only 🟡:** model or author exists but the claimed venue, year, or metric does not. Recommend external verification; never promote name-only evidence to ✅.
  - **external-marker 🟡:** artifact contains `[외부 추정]`, `[?]`, or `[unverified]`; recommend external verification.
  - **conflict 🔴:** card value contradicts the claim, including section-context conflicts.
  - **no-match 🔴:** no card matches.
  - **circular-ref 🔴:** strategy and draft support each other without a card or external source. Recommend autopilot-refine to trace and verify the claim.
  - **ambiguous 🟡:** multiple cards match without a unique best source.
- **style:** read `## Style Guide` in `strategy.md` and compare citation format, captions, bullet depth, and speaker notes. A deviation is 🟡. A missing Style Guide is one 🔴 issue with an autopilot-refine suggestion.
- **structure:** enforce the [CONVENTIONS §5](../../../core/CONVENTIONS.md#5-skill-output-convention--t1t2t3) three-tier layout: T1 contains `pipeline_summary.md`, `draft/`, and `strategy/`; T3 uses `_internal/`. Extra root files are 🟡; missing required entries are 🔴.
- **cross-ref:** verify every `cards/{file}.md` citation target. Broken targets are 🔴; cited cards omitted from `## References`, when present, are 🟡.
- **coverage:** let S be candidate cards from the shared resolution and T be cards cited in draft or strategy. Build T from only two high-precision tokens:
  1. the short identifier in `{year}_{firstauthor}_{arxivid}_{shortname}.md`;
  2. the exact value of `**arXiv ID**` in the legacy `## 메타` section.

  Do not add title-word or author-regex heuristics in v1. For every `S - T` card, emit 🟡: `coverage: card '{card path}' is never cited in any chapter/section; verify whether omission is intentional`. Skip coverage when S came from a broad cross-research fallback. A future v2 may add title and author matching, but that is out of scope.

#### Research Aspects

- **card integrity:** every `cards/*.md` must have H1 plus the legacy schema sections `## 메타` and `## 분류`, or equivalent canonical sections. Missing sections are 🔴; empty metadata fields are 🟡.
- **Tier consistency:** every cited paper's chapter Tier must match its card. Mismatch is 🔴; a cited paper without a card is 🟡.
- **coverage:** every card should appear in a top-level chapter or be marked not yet integrated. Orphans are 🟡.
- **cross-card:** verify card-to-card references. Broken references are 🔴.

#### Plan Aspects

- **test results:** read `test_logs/test_report.md`. Failed tests are 🔴. Missing tests are 🟡 only when the scope explicitly includes test results.
- **lint:** with `--read-only`, inspect existing lint output in `dev_logs/` rather than executing lint. Missing output is 🟡; reported errors are 🔴.
- **code review:** inspect `_internal/dev_reviews/` and `_internal/plan_reviews/`. Unresolved 🔴 stays 🔴; 🟡 stays 🟡.
- **incomplete work:** inspect unchecked `[ ]` entries in `plan/checklist.md` plus referenced source TODO, FIXME, and XXX comments. Unchecked critical steps are 🔴; source TODOs are 🟡.
- **semantic-deterministic consistency:** compare semantic requirements in `<artifact-root>/spec/prd.md`, or the plan's referenced spec, with target implementation files. Determine whether contextual judgment was reduced to token matching or fixed rules without preserving meaning. Map spec sections or module names to target files from the plan or checklist. A clear mismatch is 🔴; the issue message must state both sides in one sentence, for example `spec {prd.md:N} semantic requirement ↔ code {src.py:M} token rule`, and suggest one of the three remedies from DESIGN_PRINCIPLES §0.7. If mapping is unclear, emit 🟡 rather than claiming a mismatch. Reuse the stable issue shape; do not introduce another framework.
