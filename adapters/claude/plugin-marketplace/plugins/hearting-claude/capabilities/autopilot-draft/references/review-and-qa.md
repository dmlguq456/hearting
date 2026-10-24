### Step 3: Strategy review

Resolve:

- `strategy_folder = <artifact-root>/documents/{YYYY-MM-DD}_{short-name}/`
- `strategy_path = {strategy_folder}/strategy/strategy.md`
- `strategy_mirror_path = {strategy_folder}/strategy/strategy_{language}.md`, when a mirror exists.

Use the intensity-derived rigor in CONVENTIONS §1.1. At `standard+`, quality review and fact review run in parallel.

| Rigor | Review configuration | Logs |
|---|---|---|
| `quick` | One fast `research/fact-check` unit spot-check; do not run draft-refine | `_internal/strategy_reviews/research_review.md` |
| `light` | One fast `research/fact-check` unit review of critical issues | `_internal/strategy_reviews/research_review.md` |
| `standard` | One deep quality reviewer plus one fast fact-checker | `research_review_quality.md`, `research_review_factcheck.md` |
| `thorough` | Selected axis-specific reviewers plus a fact-checker when sourced claims are in scope | One log per axis under `_internal/strategy_reviews/` |

For a thorough review, use bounded axes rather than asking every instance to do everything:

- **Domain quality** — Compare references and reviewer comments; check domain conventions, completeness, and cohesion. Log `research_review_domain.md`.
- **Methodology** — Check logical consistency, persuasiveness, experiment design, and adversarial weaknesses. Log `research_review_methodology.md`.
- **Style guide** — Confirm `## Style Guide` exists and citation, caption, bullet-depth, and speaker-note conventions are consistent. Log `research_review_style.md`.
- **Cross-reference and coverage** — Verify every `cards/{file}.md` target and identify relevant orphan cards omitted from the strategy. Log `research_review_coverage.md`.
- **Fact check** — Compare citations, venues, years, metrics, and lineage verbatim with cards or PDFs. Log `research_review_factcheck.md`.

Write localized `<!-- memo: ... -->` comments in the primary strategy artifact, prefixing each with `[STYLE]`, `[COVERAGE]`, `[FACT]`, or another stable axis name. Merge and deduplicate memos after parallel review.

#### Quality-review prompt

```text
Review this document strategy as the user's domain-expert proxy.
Task type: paper-driven document; mode: {mode}.

Primary strategy: {strategy_path}
Mirror, if any: {strategy_mirror_path}
Analysis: {strategy_folder}/analysis/
Discovered inputs: {discovered_inputs}
Log: {review_log_path}

Check:
- actual references, reviewer comments, and domain conventions;
- logical consistency, completeness, and missed reviewer points;
- presence and application of `## Style Guide`;
- T1/T2/T3 layout from CONVENTIONS.md §7;
- existence of every card link target;
- relevant source cards omitted from the strategy.

Do not verify individual venues, years, or metrics; the fact-checker owns that axis.
Write localized `<!-- memo: [AXIS] ... -->` comments in the primary strategy.
Write a structured review log and return a short memo summary or "no issues found".
```

#### Fact-check prompt

```text
Act only as a fact-check reviewer, not a narrative reviewer.
Mode: {mode} | Strategy: {strategy_path} | Inputs: {discovered_inputs}
Log: {fact_log_path}

For each material claim about citations, models, venues, years, metrics, datasets,
lineage, or classification, compare it verbatim with:
- `<artifact-root>/analysis_project/paper/*.md` when available;
- an original PDF only when listed in discovered inputs and the analysis lacks the fact;
- `{strategy_folder}/analysis/reviewer_analysis.md` for rebuttal claims.

Never use the strategy's own Style Guide as ground truth.
Limit the output to roughly 30 material claims in one table:
| Section | Claim | Source | Match (✅/🟡/❌) | Source type | Severity (🔴/🟡/🟢) |

Source types:
- cards-verbatim: the exact value appears in the card; ✅ allowed.
- cards-name-only: only the name appears; 🟡 and externally reverify.
- external-marker: `[외부 추정]`, `[?]`, or `[unverified]`; 🟡 and externally reverify.
- external-reverified: confirmed externally with a URL; ✅ allowed.
- conflict: the source gives a different value; 🔴.
- circular-ref: strategy/draft agreement without a primary source; 🔴.

For each 🔴 or 🟡 item, add a localized
`<!-- memo: [FACT] section X — claim Y conflicts with source Z -->`.
Return only the log path and a one-line verdict.
```

If memos exist:

- At `quick`, retain them as an audit trail, skip draft-refine, and record the automatic decision before Step 4.
- With `--user-refine`, set `paused_at_stage: strategy-refine`, report the resume command, and exit without invoking draft-refine.
- Otherwise invoke `draft-refine` on the primary strategy path.

If no memo exists, continue to Step 4. When resuming from `strategy-refine`, skip repeat review and apply draft-refine to the existing memos.

### Step 5: Draft review

Apply this step to `paper`, `presentation`, and `doc`.

For paper mode, run the baseline gate in `convention-paper.md` §3.6 before any axis review: sentence-level grammar, mechanical LaTeX integrity, semantic asset identity, and the one-build visual/layout check when reviewing the user's own paper. Review depth does not compensate for skipping this gate.

Resolve:

- `draft_path = {strategy_folder}/draft/draft.md`
- `draft_mirror_path = {strategy_folder}/draft/draft_{language}.md`, when present.

Use the same intensity scaling as Step 3:

| Rigor | Review configuration | Logs |
|---|---|---|
| `quick` | One fast spot-check; do not run draft-refine | `_internal/draft_reviews/draft_review.md` |
| `light` | One fast critical-issue review | `_internal/draft_reviews/draft_review.md` |
| `standard` | One deep quality reviewer plus one fast fact-checker | `draft_review_quality.md`, `draft_review_factcheck.md` |
| `thorough` | Axis-specific content, writing, style, and coverage reviews plus fact-check | One log per axis under `_internal/draft_reviews/` |

Thorough axes:

- **Content and strategy coverage** — Confirm every strategy point appears in the draft; for rebuttals, confirm every reviewer point has a response.
- **Writing quality** — Check flow, completeness, weak claims, and remaining `[TODO]` markers.
- **Style-guide compliance** — Apply every citation, caption, bullet, and speaker-note convention consistently.
- **Cross-reference and coverage** — Verify card links and identify relevant orphan cards absent from the draft.
- **Fact check** — Compare sourced claims verbatim against cards or PDFs.

Write localized memos in the primary draft, with stable axis prefixes, then merge and deduplicate them.

#### Draft quality-review prompt

```text
Review this draft as the user's domain-expert proxy.
Mode: {mode} | Draft: {draft_path} | Mirror: {draft_mirror_path}
Strategy: {strategy_path} | Analysis: {strategy_folder}/analysis/
Inputs: {discovered_inputs} | Log: {review_log_path}

Check strategy coverage, logical flow, completeness, remaining TODOs, reviewer-point
coverage for rebuttals, Style Guide consistency, card-link targets, and omitted cards.
Do not independently verify individual venue, year, or metric facts.

Write localized `<!-- memo: [AXIS] ... -->` comments in the primary draft,
write a structured review log, and return a short summary or "no issues found".
```

#### Draft fact-check prompt

```text
Act only as a fact-check reviewer.
Mode: {mode} | Draft: {draft_path} | Inputs: {discovered_inputs}
Log: {fact_log_path}

Compare each material citation, model, venue, year, metric, dataset, lineage, or
classification claim with `<artifact-root>/analysis_project/paper/*.md`. Use an
original PDF only when listed in discovered inputs and the analysis lacks the fact.
The strategy is not a primary source; strategy↔draft agreement alone is circular.

Apply the same cards-verbatim, cards-name-only, external-marker,
external-reverified, conflict, and circular-ref classifications as Step 3.
Output one table:
| Slide/Section | Claim | Source | Match (✅/🟡/❌) | Source type | Severity (🔴/🟡/🟢) |

For each 🔴 or 🟡 item, add a localized
`<!-- memo: [FACT] location X — claim Y conflicts with source Z -->`.
Return only the log path and a one-line verdict.
```

If memos exist:

- At `quick`, retain them, skip draft-refine, and continue to Step 6.
- With `--user-refine`, set `paused_at_stage: draft-refine`, report the resume command, and exit.
- Otherwise invoke `draft-refine` on the primary draft. Path auto-detection handles the optional mirror.

If no memo exists, continue to Step 5.5. When resuming from `draft-refine`, skip repeat review and refine from the existing memos.
