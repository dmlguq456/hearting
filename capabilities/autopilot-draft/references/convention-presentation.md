# §presentation — PowerPoint slide Markdown

> These rules apply to autopilot-draft `--mode presentation` in addition to `convention-common.md`.
>
> Scope: conference talks, seminars, lectures, full decks, and cheatsheet variants that extend an existing deck.

The output is one slide-by-slide Markdown file for manual copy and paste into PowerPoint. It is not a Pandoc-to-PPTX source. Avoid Pandoc-only syntax such as `::: notes`, `:::: {.columns}`, and title-generation frontmatter.

## §presentation-0. Enforce 16:9 content limits

For every slide, ask: *Will this fit on one 16:9 slide?*

- Use at most five or six bullet lines.
- Keep each line to one or two key phrases, usually no more than ten words.
- Reserve at least 60% of the slide for figures or tables.
- Keep tables to about six rows and five columns; split larger tables across slides.

Move long explanations, numeric justification, and detail into speaker notes or backup slides.

## §presentation-1. Minimize text inside figures

Avoid long suptitles and subplot titles. Use short token labels. Put numeric interpretation in the slide body or a table, and keep captions to one line describing what the figure shows. Use a neutral tone.

## §presentation-2. Share axes and scales across comparisons

Use the same axes and scale for every comparison panel. Per-panel normalization hides absolute differences. Set dynamic range from the data distribution.

## §presentation-3. Use robust axis limits

Do not set limits from raw extremes when one outlier would compress the plot. Use percentile-based robust limits and keep them consistent across panels.

## §presentation-4. Convert to audience-friendly units

Convert internal engineering units into ratios, log scales, percentages, or other units familiar to the audience. When comparing two values, show both absolute and relative differences, especially for non-specialists.

## §presentation-5. Match an existing deck

For a cheatsheet variant, match the existing header, bullet, and conclusion conventions. Extract the existing deck text during preflight so the first new slide follows the last old slide naturally.

## §presentation-6. Use the available assets

Use user-provided samples and intermediate artifacts across several cases or multipanel figures. A deck supported by only one or two visuals is usually too weak.

## §presentation-7. Link supporting raw assets

Bundle source data, audio, video, datasets, and other raw assets per page in a ZIP or cloud location. Link them from the draft as `[label](path)`.

## §presentation-8. Finalize plots before prose

Generate plots, collect user feedback, and revise them before writing the slide body. Embedding an incorrect plot first forces later revisions to both numbers and interpretation.

## §presentation-9. Apply these checks consistently

Apply this section to full decks and cheatsheet variants, including later presentation edits through draft-refine or audit.

## Slide-format conventions

1. **Show chapter context in the heading.** Use `## Slide N — [Ch.N Chapter] (sub.number) Slide title`. Mark chapter transitions with `— start`. Add a localized metadata line equivalent to `Chapter: N. Name (slide K of M)`.
2. **Show the chapter band in the visual block.** Begin each body-slide visual specification with a top header band naming the chapter. Make transition slides visually distinct from the preceding chapter.
3. **Make visual placeholders concrete.** Specify diagram type, components, layout, and color hints. Replace vague requests such as “comparison chart” with an implementable specification such as “three-row NeurIPS/ICLR/ICML table with h5-index and acceptance-rate columns.”
4. **Use unambiguous table headers.** Prefer full noun phrases and explicit units. Add a one-line note above the table when a column needs explanation.
5. **Gloss foreign-language quotations.** For a non-specialist audience, follow each quotation with a one-sentence explanation in the audience's language:

   ```markdown
   > "Quoted text..."
   > — Source

   📌 **Key phrase — "X"**: One audience-friendly explanation in the audience's language.
   ```

6. **Leave speaker notes empty by default.** Generate them only when the user asks, as a separate post-polish step. This avoids drift and unnecessary regeneration during iterative slide editing.
7. **Avoid duplicating body bullets in visuals.** Body bullets represent what the speaker says; visuals represent what the audience sees immediately. Simplify one when they repeat the same fact.
8. **Keep slide numbering consistent in one edit pass.** Update subsequent slide numbers, chapter counts in the agenda, the frontmatter `changelog`, the top-guide time budget, cross-slide references, and chapter progress metadata together.

## Required top-of-file guide

Localize the labels to the artifact language while preserving this structure:

```markdown
# {Presentation title} — Seminar Slide Deck

> **Usage**: This single Markdown file is for copying into PowerPoint. `---` separates slides, each organized as number, title, bullets, visuals, and optional speaker notes.
>
> - **Slide count**: **N main + M backup = total**
> - **Time budget ({X} minutes)**: Opening / Ch.0 / Ch.1 / ...
> - **Audience baseline**: One line describing the audience and writing choices
> - **Design intent**: One paragraph describing the chapters and narrative arc
```

## Per-slide template

Localize field labels and body text to the audience language.

```markdown
---

## Slide N — {Short slide title}

**Title**: {Title shown on the slide}

**Subtitle** (optional): {Use only on the opening or a chapter divider}

- Body bullet 1: concept, name, or number
- Body bullet 2
- Body bullet 3; usually use three to five

| Use a table when comparison is clearer | Value |
|---|---|
| Model A | number |
| Model B | number |

**Visuals**:
- Left or primary half: {implementable diagram or chart specification}
- Right or supporting half: {supporting visual}
- Or full screen: {full-page visual specification}

<!-- Embed only figures mapped by Step 4.0a/4.0b figure_index.md. -->
<!-- Source 1: <img src="../../../research/{topic}/figures/{paper_id}_fig{N}.png" alt="..." width="500" /> -->
<!-- Source 2: <img src="../../../analysis_project/paper/figures/{paper_id}_fig{N}.png" alt="..." width="500" /> -->
<!-- Source 3: <img src="../assets/figures/slideXX_*.png" alt="..." width="500" /> -->
{Embed a mapped figure when its topic match is unambiguous; otherwise retain a concrete placeholder for user polish.}

**Speaker notes** (only when requested):
1. {Explanation, intuition, analogy, or anecdote}
2. {Transition to the next slide or chapter}
3. {Optional answer to an anticipated question}

**Citation** (optional): [Author Year, Venue](cards/{file}.md)
```

## Structural requirements

- **Cover** (Slide 1): title, subtitle, presenter, affiliation, date, and one source line.
- **Agenda** (Slide 2): slide count and one-line summary for each chapter.
- **Chapter divider**: `## Slide N — Ch.X Title`, with one or two lines of intent or period context. Count it as a slide.
- **Body slides**: follow the per-slide template.
- **Chapter close** (optional): summarize Ch.X and transition to Ch.X+1.
- **Conclusion**: five takeaways, open problems, one-page summary, Q&A, and thank-you slide as appropriate.
- **Backup**: use `## Slide BN — Backup: Title` after the main flow.
- **References** (optional): collect core citations at the end.

## Tone and quality

- Use keywords, numbers, and model names in body bullets; reserve full explanations for requested speaker notes.
- Expand an acronym once at first use.
- Link citations to paper cards, for example `[Author Year](../../research/{topic}/cards/{file}.md)` or the local `cards/` directory.
- Put a concrete visual placeholder on every slide.
- Describe visuals precisely enough to draw them in PowerPoint, for example “five-stage horizontal timeline with five colors.”
- Map the strategy outline exactly, including total slide count and chapter time budget.
- When the user requests speaker notes, cover at least 80% of body slides, excluding covers and low-content greeting slides.
