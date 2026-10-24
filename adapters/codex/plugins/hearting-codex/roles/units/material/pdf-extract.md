---
unit: material/pdf-extract
family: material
role: fast tool worker
worker_type: support
floor: near-zero
read_only: false
stance: none
io:
  verdict: [SUCCESS, PARTIAL, FAIL]
  return: _shared/dual-io.md
tools: []
branches: []
aliases: {}
---

# Unit: material/pdf-extract

**Input**: PDF file paths (`paper_pdfs: list[str]`) + output dir (default
`{artifact_dir}/figures/`).

You extract figures/tables from PDFs using **PyMuPDF (fitz) caption-aware bbox crop** —
high resolution (DPI 600-800).

## Procedure

1. For each PDF, prefer **PyMuPDF caption-aware bounding-box crops** over `pdfimages`
   (`pdfimages` handles raster only and misses vector figures).
2. **High-resolution policy (standing user directive, 2026-05-12):**
   - **DPI 600-800 (default 800)** for paper figure/table crops — publication / PPT
     zoom-in quality.
   - Never render full pages at default 72/96 DPI; it creates low-quality assets and
     rework.
3. **Caption-aware crop bbox**:
   - Search caption variants: `page.search_for("Figure N:")` and
     `page.search_for("Table N.")` — venue formats differ (`:`, `.`, or bare), so try
     the fallback punctuation patterns after a miss.
   - Choose the best caption rectangle: `x0 < 100` (left-margin start) AND lowest `y0`
     (a real caption, not an inline body reference).
   - Clip: `y_top = caption.y0 - 5`, `y_bot = next_caption.y0 - 5` or the end of body
     content based on text-block analysis.
4. **Detect two-column layouts automatically:**
   - ICML/NeurIPS/ICASSP/T-ASLP/IS standard: page width ≈ 612pt, column width ≈ 234pt,
     gap ≈ 26pt.
   - For a column-width table or figure, crop only that column
     (`x0=50, x1=303` left / `x0=315, x1=562` right) and remove neighboring-column
     remnants.
   - For a page-wide asset, keep the full content width (`[50, page_w-50]`).
5. Apply heuristic filters (size > 200×200, aspect ratio sane, exclude small logos).
6. Save as `{out_dir}/{paper_id}_fig{N}.png` or `{paper_id}_table{N}_*.png` (paper_id
   from cards filename or PDF metadata).
7. Build `figure_index.md` listing extracted figures/tables with thumbnail path +
   paper_id + page + caption + **resolution column** (DPI used).
8. (optional) Skip duplicates if the PDF was already processed (cache via SHA-1 of the
   PDF).
9. **Visual sanity check:** recommend in the result that the caller visually inspect at
   least one or two PNGs. Re-extract when neighboring-column remnants, footer noise, or
   blurred text remain.

## Caveats

- At 800 DPI, figure PNGs are typically 200–500 KB and page-wide tables 400–700 KB; PNG
  compression keeps growth bounded.
- Caption text encoding varies per PDF; try fallback punctuation patterns after a miss.
- PyMuPDF page indices are zero-based; report one-based page numbers to users.
- This is a draft asset pool, not a replacement for hand curation; later presentation
  polish may crop further.

## Output

`{out_dir}/{paper_id}_fig*.png` + `figure_index.md`

**Output packaging** follows the standing figure-output rule in
`material/figure-gen` §Script Convention: individual PNGs plus at most one combined
PPTX — never per-figure/per-slide PPTX wrappers.

## Cross-skill Reuse

Figures extracted during research persist under `research/<topic>/figures/` and are
indexed from paper cards, e.g. `**Figures**: ../figures/<paper_id>_fig1.png`. Downstream
draft and refine flows discover them through the research artifact.

## Return

Per `_shared/dual-io.md`. Verdict examples: "✅ N/M PDFs extracted (K figures)",
"⚠️ N/M extracted, K failed".

## Process Cleanup

- Release PyMuPDF resources explicitly with `doc.close()` for large batches.

## Automatic Entry Point

- **autopilot-research Phase B:** collect paper figures from downloaded PDFs.

## Memory

Per `_shared/memory-flow.md`. Retention target: venue-specific caption-extraction
behavior.
