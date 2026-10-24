---
unit: material/figure-gen
family: material
role: deep maker
worker_type: stage
floor: low
read_only: false
stance: none
io:
  verdict: [SUCCESS, PARTIAL, FAIL]
  return: _shared/dual-io.md
tools: [tools/figure-semantic-verify.py]
branches: [paper, presentation]
aliases: {}
---

# Unit: material/figure-gen

Invocation: `figure <name> <spec>` or an equivalent natural-language request.

Create reproducible matplotlib or seaborn figures for papers and presentations. One
output unit includes the vector PDF, the raster preview PNG, and the script that
generated them.

## Outputs

- `<paper_dir>/figures/<name>.pdf` — vector PDF, with `pdf.fonttype=42`
- `<paper_dir>/figures/plot_<name>.py` — self-contained reproduction script with minimal
  dependencies
- `<paper_dir>/figures/<name>_preview.png` — raster preview, about 200 DPI by default

## Branch: paper (defaults)

- Serif font fallback: Times New Roman → Nimbus Roman → Liberation Serif → DejaVu
  Serif; STIX fontset for math (Times-compatible glyphs).
- Choose dimensions for the placement: 6.4×2.8 in single-column landscape, 4.5×2.8 in
  each side-by-side panel, or 3.5×2.6 in vertical single-column use.
- Keep a coherent palette across figures in the same paper (e.g. an OrRd sequential for
  ordered variants; cool/warm separation for categorical groups).
- Use subtle grids around alpha 0.25 and linewidth 0.5, with `which='both'` so minor
  grids appear on logarithmic axes.
- Embed-safe fonts: set both `pdf.fonttype=42` and `ps.fonttype=42` (PMLR/ICML
  validation-safe).

## Branch: presentation

Sans-serif treatment (Noto Sans / DejaVu Sans), larger figsize sized for 16:9 slides.

## Spectrogram Integrity (domain LAW — speech/signal)

Both the per-rate window sizes and the shared color axis are principles, not
preferences; they guarantee the honesty of the material itself.

- **Window size fixed per native sample rate (USER-CONFIRMED 2026-07-22):**
  - 8 kHz → window = 256
  - 16 kHz → window = 512
  - 48 kHz → window = 1024
  - For other rates (e.g. 24 / 44.1 kHz) use the nearest of these values; never
    interpolate a novel window rule. The verifier treats such rates as
    warn-not-fail because only the triple above is confirmed law.
- **No resampling** — STFT each signal at its native sample rate. Do not resample merely
  to make comparison panels uniform; the sample rate is meaningful context and must show
  as-is.
- **Shared color axis per comparison group** — within one comparison group (e.g.
  clean/noisy/restored, or models A/B/C) fix `vmin`/`vmax` to one group-wide value
  (`imshow(..., vmin=GROUP_VMIN, vmax=GROUP_VMAX)`) so intensity is comparable on a
  consistent scale. Separate figures may use different ranges.
- Deviations only on an explicit current-turn user instruction.

Panel layout and label patterns follow `mem profile 01_paper_figure_style`
(`python3 <agent-home>/tools/memory/mem.py profile 01_paper_figure_style`); run it and
treat its body as the default, overridden by explicit current-turn instructions.
Presentation-asset structure follows `mem profile 03_presentation_strategy`; caption and
label terminology follows `mem profile 05_domain_expertise`.

### Metric and Display Bands (fail-closed report QA)

- Declare analysis and presentation ranges as separate constants, for example
  `METRIC_BAND_HZ = (20, 1000)` and `FIGURE_BAND_HZ = (0, 24000)` for a 48 kHz
  full-band report. A metric crop is never a plotting default, and helper defaults are
  never passed implicitly to plot functions.
- Plot functions receive an explicit figure band. Changing a metric band must not change
  the report figure band; preserve this with a regression test.
- Every report spectrogram writes a semantic manifest entry containing
  `sample_rate_hz`, `min_hz`, `max_hz`, `dynamic_range_db`,
  `shared_scale_per_figure`, and `colormap`. Also record the STFT declaration
  `"stft": {"sample_rate_hz": <native rate>, "window_samples": <window>}` so the
  verifier enforces the confirmed window law deterministically instead of by
  reviewer memory.
- For the `spectrogram-report-48k-full-band` profile, run
  `python3 <agent-home>/tools/figure-semantic-verify.py --manifest <manifest.json>
  --report <report.md>`. It fails closed unless the metadata is exactly 48000 Hz,
  0–24000 Hz, shared scale true, and the required dynamic range/colormap are present,
  and unless band-sensitive report claims have range-compatible evidence. When a
  figure records an `stft` block, it also fails closed on a window that violates the
  confirmed 8 kHz→256 / 16 kHz→512 / 48 kHz→1024 law (other sample rates emit a
  warning, never a failure). Do not report completion when it fails.
- Full-band / high-frequency / broadband statements must link to figure or metric
  evidence whose range actually covers the claim — a 20–1000 Hz metric is never
  evidence for a full-band claim. A high-frequency claim attaches its explicit Hz/kHz
  interval to the term in report prose (e.g. `high-frequency (8–24 kHz)`) and records
  the identical interval in `claimed_band_hz`; the verifier parses and compares them.
- Visually open at least one representative PNG after generation. Confirm the y-axis
  spans 0–24 kHz, ticks and labels are readable, a colorbar is present, and comparison
  panels share vmin/vmax. File existence and pixel dimensions are not visual evidence.
  Record the PNG SHA-256, reviewer/tool, timestamp, and verdict in the manifest;
  re-review whenever the PNG changes.

## Script Convention

- Collect constants (color hex, figsize, font lists) at the top of the script in one
  place so the user can swap them in one line.
- Comment domain equations (e.g. loss formulas) so the reproduction evidence is
  explicit.
- Preview PNG at 200 DPI by default; increase only when the requested review needs it.
- Output packaging (individual PNGs; at most one combined PPTX; never one PPTX
  wrapper per figure) follows the durable figure-production rule
  `mem show feedback_feedback-figure-자동-제작_6eade0 --all`
  (`python3 <agent-home>/tools/memory/mem.py show feedback_feedback-figure-자동-제작_6eade0 --all`);
  run it and treat its body as the default, overridden by explicit current-turn
  instructions.

## Return

Per `_shared/dual-io.md`: `<output path> -- <verdict>` plus a 3–5 line summary in the
user's communication language, naming the reproduction script. Example:

```
latex_v3/figures/robust_loss_family.pdf -- ✅ generated
- 7 curves (l1 / Huber / Charbonnier / s-log1p / Cauchy / GM / Welsch) peak-normalized
- 6.4×2.8" landscape, Times-equivalent serif, OrRd palette
- reproduction script latex_v3/figures/plot_robust_loss_family.py
```

## Scope Boundary

This unit owns data figures. UI components and brand visual design belong to the design
family (maker); editorial wording polish belongs to the editorial family.

## Automatic Entry Points

- **autopilot-draft, paper mode:** supply a missing PDF referenced by
  `\includegraphics{<path>}`.
- **autopilot-research:** create report figures.
- **autopilot-code:** visualize results.

## Memory

Per `_shared/memory-flow.md`. Retention targets: venue-specific figure conventions,
reusable domain plotting templates, and user-approved figure style decisions.
