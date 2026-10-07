## Pipeline — Six Phases

### Phase 1: Source Discovery and Conversion

Discover, classify, and index all expected sources for each aspect. Convert formats that agents cannot read directly into PDF and, for slide material, PNG.

1. Read user-supplied `--source <path>` roots. Accept comma-separated paths. Without `--source`, index zero materials and request a reference-material path in one line.
2. Scan every supplied root recursively and classify:
   - `*.pdf` → figure, writing, analysis, or domain;
   - `*.pptx` and `*.ppt` → presentation, converted to PDF and PNG;
   - `*.docx`, `*.hwpx`, and `*.hwp` → writing, converted to PDF;
   - `model/`, `*.py`, `*.yaml`, and `*.ipynb` → coding convention or analysis;
   - `figures/` and `figure_ppt/` → figure;
   - `analysis/` → analysis;
   - `*.mp4`, `*.mov`, and other video → skip and report once.
3. For the domain aspect, include system-owned memory sources that may contain domain terminology.
4. For writing and domain aspects, include user-supplied paper lists and abstracts.
5. Convert Office formats with headless LibreOffice. Attempt installation if absent; if privilege or network access prevents installation, skip unconverted files and report the fallback.

```bash
if ! command -v libreoffice &>/dev/null; then
  echo "LibreOffice missing — attempting installation..."
  sudo apt install -y libreoffice 2>&1 || {
    echo "❌ Automatic installation failed; sudo permission may be required."
    echo "   Install manually: sudo apt install libreoffice"
    echo "   Or convert the source to PDF and rerun with that source path."
  }
fi
```

| Source | Conversion | Output |
|---|---|---|
| DOCX, HWPX, XLSX, DOC | `libreoffice --headless --convert-to pdf` | `<agent-home>/user_profile/_internal/converted_pdfs/<name>.pdf` |
| PPTX, PPT | PDF plus one PNG per slide; use PDF for text/layout and PNG for visual fidelity | `_internal/converted_pdfs/<name>.pdf` and `_internal/converted_pngs/<name>_slide{NN}.png` |
| HWP | Attempt LibreOffice conversion; if rendering breaks, ask the user to provide PDF | PDF on success; skip and report on failure |
| Video | Skip | One-line report |

```bash
# DOCX / HWPX / XLSX
libreoffice --headless --convert-to pdf "<file>" --outdir _internal/converted_pdfs/

# PPTX — PDF plus per-slide PNG
libreoffice --headless --convert-to pdf "<file>.pptx" --outdir _internal/converted_pdfs/
pdftoppm -png -r 150 _internal/converted_pdfs/<file>.pdf _internal/converted_pngs/<file>_slide
```

6. Record source type, mtime, size, and original path for converted material. Valid types include `figure`, `latex`, `slide`, `py-script`, `memory`, `paper-abs`, `code-model`, `code-train`, `code-config`, `code-notebook`, `converted-pdf`, and `converted-png`.
7. Write the complete inventory to `<agent-home>/user_profile/_internal/source_index.md` and return counts by aspect and conversion type.

### Phase 2: Aspect Analysis with Three-Instance Consensus

Extract reusable patterns independently through three `research/research-survey` unit shards per aspect (composed map-reduce dispatch). Each instance receives the same source index and prompt in a separate context; no cross-talk is allowed.

Use this operational prompt, adapting only the aspect-specific extraction axes:

```text
Dispatch unit research/research-survey with prompt:
"""
Analyze the user's artifacts — aspect: figure (instance {A|B|C})
Source index: <agent-home>/user_profile/_internal/source_index.md
Existing profile record (mem profile 01_paper_figure_style): {existing content}

Materials to read:
- Directly readable originals such as PDF, PNG, Markdown, and Python files — read them directly.
- DOCX, HWPX, and XLSX — read `_internal/converted_pdfs/<name>.pdf`, generated automatically in Phase 1.
- PPTX — read **both** `_internal/converted_pdfs/<name>.pdf` for text and layout evidence and `_internal/converted_pngs/<name>_slide{NN}.png` for visual fidelity, text size, fonts, and placement. Cite the PNG first for visual or presentation-specific evidence.

Patterns to extract for the selected aspect, for example `figure`:
1. Architecture-diagram style
2. Curve-plot style
3. Scatter-plot and table layout
4. Spectrogram conventions
5. Domain-specific metric sets
6. Patterns used to emphasize the user's own method

For the `figure` aspect, extract high-level design taste and judgment. Phase 5b extracts source shapes as individual objects under `assets/figure/svg/<base>_slide-N.svg`. Exclude low-level reproduction specifications; focus on composition, emphasis and restraint, hierarchy and visual flow, the semantics of color, signature traits, and consistency.

Every pattern must cite a source. For PPTX evidence, name the corresponding `slide{NN}.png`.
Extract independently without reading the results from other instances.
In `mode=init`, replace the record completely; in `mode=update`, accumulate verified additions.

Output: <agent-home>/user_profile/_internal/aspect_{aspect}_run_{A|B|C}.md
"""
```

For `writing`, `presentation`, `analysis`, `domain`, and `coding_convention`, keep the same process and change only the extraction axes. `all` dispatches six aspects × three instances.

For `coding_convention`, extract model-folder layout, config mechanism, variant prefixes, preferred layers, framework, metrics, log/checkpoint paths, seed and reproducibility behavior, and naming conventions.

#### Phase 2.2: Consensus Aggregation

The main Skill reads all three catalogs and aggregates them directly:

1. Normalize semantically equivalent pattern names.
2. Weight by independent agreement:
   - 3/3 → confidence 1.0; include without metadata.
   - 2/3 → confidence 0.6; include with `(consensus 2/3)`.
   - 1/3 → confidence 0.3; quarantine until Phase 4 verifies, drops, or promotes it.
3. Resolve conflicting values by majority. If all three differ, record an open question.

Outputs:

- `_internal/aspect_{aspect}_run_{A|B|C}.md`;
- `_internal/aspect_{aspect}_draft.md` with confidence metadata and quarantine section;
- `_internal/aspect_{aspect}_consensus.md` with per-pattern vote counts;
- one verdict listing confidence counts.

### Phase 3: Cross-Aspect Validation

Read every aspect draft and compare palette, typography, domain terminology, metric sets, and the relationship between preferred layers and domain expertise. Prefer the pattern supported by more sources, then newer material. An explicit user-authored `mem profile-append` record is ground truth. Write `_internal/cross_aspect_consistency.md` with resolved contradictions and open questions.

### Phase 3.5: Prior-Version Reconciliation — update mode only

Compare the new draft with the current body from `mem profile <stem>` and, when useful, the newest `_internal/versions/` snapshot. Classify each pattern as:

- **confirm** — matches and strengthens the previous profile;
- **refine** — adds precision without contradiction;
- **contradict** — conflicts and requires synthesis;
- **new** — absent previously, retaining Phase 2 confidence.

For contradictions, do not select the newest value mechanically. Decide whether the change reflects user evolution, correction of prior overgeneralization, or context dependence that requires a conditional rule. Record one evidence line. The exact legacy DB section literal `## 사용자 수동 메모` has priority as explicit user evidence.

Write `_internal/prior_reconciliation.md`, apply the synthesis to the aspect draft, add one changelog line, and return category counts.

### Phase 4: Adversarial Multi-Agent QA

Run four reviewers in parallel because profile errors propagate across workflows:

- **A — source coverage**, fast reviewer, `_internal/qa_coverage.md`: every pattern needs a source; every indexed source must be considered.
- **B — pattern accuracy**, deep reviewer, `_internal/qa_accuracy.md`: verify exact colors, fonts, sizes, titles, venues, years, and other facts. Promote a verified 0.3 pattern to 0.6, drop unsupported overgeneralization, or retain a source-sparse pattern as an open question.
- **C — fact-check**, fast fact-checker, `_internal/qa_factcheck.md`: verify paper titles, venues, years, citation counts, DOI, arXiv ID, and metric values verbatim.
- **D — external adversary**, external adversary role, `_internal/qa_external.md` or legacy `_internal/qa_codex.md`: challenge bias, overgeneralization, and missing aspects.

If the external adversary is unavailable, skip D with one warning; A, B, and C are mandatory. Aggregate 🔴, 🟡, and 🟢 counts plus promotion, drop, and open-question outcomes. Any 🔴 finding retries Phases 2–3.5, at most twice; two failed retries end the pipeline.

### Phase 5: Write Verified Profile Records

Write verified drafts to global DB records with `type=profile`; do not edit profile files. Target a self-contained body of roughly 7–10K tokens. Keep high-confidence and medium-confidence patterns, concrete anchors, and the exact `## 사용자 수동 메모` section. Move dropped quarantine items, open questions, long narrative, and source inventories to `_internal/` files.

```markdown
## 1. Architecture Diagram
- block: rounded rectangle, grayscale outline (TF-Restormer Fig.1 / TF-CorrNet Fig.2)
- color: encoder green #548235 / decoder orange #ED7D31 / ours red #C00000
- arrow: solid 1.5pt, sans-serif label 8pt
```

Procedure:

1. If `--user-refine` is set, report draft and QA paths, then pause. Resume with `/analyze-user --from output`.
2. For each stem:
   - `init`: read any current body with `python3 <agent-home>/tools/memory/mem.py profile <stem>`, preserve `## 사용자 수동 메모`, snapshot the previous body under `_internal/versions/v{N}/`, then write the complete replacement body.
   - `update`: read the newest tie-broken body only through `python3 <agent-home>/tools/memory/mem.py profile <stem>`. Splice verified changes while preserving `## 사용자 수동 메모`; append a dated `## Changelog` line.
   - Write the complete body with `python3 <agent-home>/tools/memory/mem.py add durable profile <body> --scope global --source user-profile:<stem>`.
   - `mem add` uses source-keyed upsert by `(tier, scope, source)`, preserving identity and preventing duplicate rows.
   - analyze-user and `mem profile-append` are two writers of the same record. Always read through `mem profile <stem>` before splicing. A raw query can read a stale duplicate and orphan an appended memo.
3. Read back each stem through `mem profile <stem>` and compare it with the just-written body. A mismatch may indicate source-blind deduplication across stems; fail loudly for manual inspection.

### Phase 5b: Extract PPTX Objects — figure aspect only

Extract vector slide objects as a user reference library. The LLM does not recreate paper architecture figures; the user completes them in PPTX while design roles provide layout guidance.

1. Convert every slide in `figure_ppt/*.pptx`, or the corresponding converted PDF, with `pdftocairo -svg -f N -l N <pdf> out.svg`.
2. Store `<agent-home>/user_profile/assets/figure/svg/<base>_slide-N.svg` and optionally copy representative slides to canonical `ex1_*.svg` anchors.
3. Update profile 01 §B0 to point to the SVG library rather than raster previews.

Return extracted-slide count and canonical anchors. If the source library is absent, report it and provide layout guidance only; do not attempt LLM reconstruction.

### Phase 6: Pipeline Summary

Append to `<agent-home>/user_profile/_internal/pipeline_summary.md`:

```markdown
## {YYYY-MM-DD} — {aspect} {mode}

**Source**: {N source files scanned, breakdown by type}
**Extracted patterns**: {M new + K updated + L removed}
**Consensus distribution**: confidence 1.0 = {n_high} · 0.6 = {n_medium} · 0.3 (quarantine) = {n_low}
**Quarantine outcome (Phase 4 QA)**: promoted {n_up} · dropped {n_drop} · open question {n_oq}
**QA findings**: 🔴 {n_red} 🟡 {n_yellow} 🟢 {n_green} (resolved {res})
**Affected records**: {profile record source list in user-profile:<stem> form}
**Retry count**: {0 / 1 / 2 if any}
**Figure reproduction gap**: {convergence by axis, loop count, residual source-replication-only / open question}
**Total time**: ~{minutes}

**Improvements**: {3–5 lines summarizing new or corrected patterns}
**Open questions**: {unresolved Phase 3 contradictions and Phase 4 quarantine outcomes}
```
