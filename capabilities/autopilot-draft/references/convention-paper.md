# §paper — Academic LaTeX prose

> These rules apply to autopilot-draft `--mode paper` in addition to `convention-common.md`.
>
> Scope: initial submissions, camera-ready papers, major revisions, theses, book chapters, and LaTeX paste-ready cheatsheets.

## Body structure

- Frontmatter: type, venue, `status: draft`, and date.
- Section-level draft:
  - **Abstract** — background → gap → method → results → impact.
  - **Introduction** — hook → context → gap → contribution → outline.
  - **Related Work** — organize around the strategy framing.
  - **Method** — follow the strategy outline; use placeholders for unavailable equations.
  - **Experiments** — setup → results → ablation; use table skeletons where needed.
  - **Conclusion**.
- Include figure and table placeholders with captions.

## Camera-ready and major revisions: natural integration

The canonical strategy rule is in the [draft-strategy delegate contract](../../draft-strategy/references/delegate-prompt.md), paper mode, “Natural-integration rule.”

When converting reviewer feedback or rebuttal material into a paper-body mutation, ask: *Can this be integrated naturally as a one- or two-sentence inline rewrite in the surrounding paragraph?*

- **Yes — inline rewrite.** Adjust the subsection opening and surrounding paragraph, including cascading figure references where required. Keep experimental values in the body or appendix, not in the opening or high-level framing.
- **No — drop or defer.** Do not paste rebuttal-format artifacts such as model-by-model comparison tables, structured Q&A blocks, or point-by-point response paragraphs into the paper body, even when a reviewer strongly recommends integration.

Reject a paste-ready mutation when any of these signals appears:

- It pastes a standalone rebuttal `\begin{table}` or `\begin{itemize}` block in full.
- It inserts experimental values verbatim into an opening or framing paragraph.
- It adds a separate `\paragraph{...}` without a bridge to the surrounding text.

Provenance (the 2026-05-19 camera-ready incident and the user's verbatim constraint) is held by the memory refs in the `mem profile 02_paper_writing_style` header (`[[feedback-paper-body-rewrite-pattern]]`); the rule above is the distilled operational form.

## Paste-ready cheatsheet contract

Apply this contract when paper-mode output is a set of cards the user pastes directly into LaTeX, such as `subtype: camera-ready-paste-ready`, rather than continuous prose.

Separate what the user must read from agent-only tracking metadata. The preview must make the next paste location easy to find. This contract responds to the user's original feedback: _“preview 에서 그냥 쭉 줄글로 깨진다 — 형식 자체 문제다.”_

### 1. Keep frontmatter minimal

Allow only user-relevant fields in `draft.md` or its language mirror:

- `type`, `venue`, `paper_id`, `status`, `date`, and `baseline` (the LaTeX path).

Move these fields to `_internal/draft_meta.yaml` or `pipeline_summary.md`:

- `changelog`, `mutation_count`, `intentional_id_gaps`, `predecessor`, `strategy_ref`, `intensity`, `subtype`, `scope`, and long tracking notes.

Long frontmatter renders as an unreadable block and delays access to the first actionable entry.

### 2. Establish document identity on the first screen

Use exactly:

- One H1 title, for example `# TF-Restormer Camera-Ready Cheatsheet v3 — Appendix + Conclusion`.
- A two- to four-sentence overview stating scope, number of entries, and paste flow. Do not include tracking statistics.
- One legend blockquote, localized to the artifact language, equivalent to: `> **Legend**: 🔴 mandatory · 🟡 high · 🟢 optional · ⏳ not applied / 📌 applied · audit link inline`.

Do not add extra opening blockquotes for strategy details, wording invariants, preservation notes, or usage instructions.

### 3. Make every entry a self-contained card

Use labels in the artifact's selected language. The structure and order are invariant:

```markdown
### {ID} {tier emoji} — {short action}

- [ ] **⏳ Not applied**

**Location**: `\section{...}` or `\paragraph{...}`

```latex
% paste-ready block after revision
...
```

**Reason**: {one-line rationale}

**Changes**:
- `old wording` → `new wording`
- New insertion — {what to insert and where}
```

Requirements:

- Put the task-list checkbox immediately below the H3 heading. Before application use `[ ]`, ⏳, and the localized “not applied” label. After application change all three to `[x]`, 📌, and the localized “applied” label.
- Use one stable localized label for location; do not alternate between `Anchor`, `LaTeX anchor`, and other synonyms.
- Include one paste-ready LaTeX block, plus at most one short companion block when both must be pasted together.
- State the reason in one line.
- Identify every token-level change. For an INSERT with no prior wording, state what is new and where it belongs.
- Preserve the order: location → LaTeX → reason → changes.

### 3.5. Identify tables and figures semantically; verify numbering mechanically

Before changing a table or figure, understand its purpose from:

1. The `\label` name.
2. Sections that reference it with `\ref`.
3. Its content and caption.

Do not identify an asset by float position alone. Verify numbers, duplicate labels, unresolved references, and citations against `main.aux` and `main.log`.

### 3.6. Run the mandatory paper-baseline gate

Read the complete paper baseline before drafting mutations. A fast reviewer is sufficient when this gate is explicit; adding review ceremony does not compensate for skipping fundamentals.

1. **Grammar** — Check subject–verb agreement, articles, number, tense, missing verbs, and malformed sentences line by line.
2. **LaTeX integrity** — Check multiply defined labels, unresolved `\ref` and `\cite`, and table or figure numbering against `main.aux` and `main.log`.
3. **Asset identity** — Determine the purpose of each table and figure from its label, references, content, and caption.
4. **Visual/layout review for the user's own paper** — Build once and inspect page limits, split footnotes, widows and orphans, and overfull boxes above 5pt. For two-column papers, render boundary pages with `pdftoppm` and inspect them visually. Document the location and recommended correction in the cheatsheet; leave minor layout tuning to the user. The apply stage runs the compile gate only and does not repeat this visual build.

### 4. Keep tracking metadata out of entries

Move reviewer mappings, dependency tables, wording invariants, verification-gate instructions, and inline refine markers such as `<!-- memo: [REFINE-R2] ... -->` to `_internal/draft_meta.md` or `pipeline_summary.md`.

Remove withdrawn, false-positive, or dropped mutations from the user-facing cheatsheet. Record the reason in `_internal/draft_meta.md` during drafting or `_internal/apply/apply_log.md` during application.

### 5. Put paste order at the end as an ordered list

Use one line per step, for example `1. M34: ...`. Do not use a table. Put a one-line `paste together: M{X}` note next to the entry's location when necessary.

### 6. Put user decisions where they occur

Do not create a separate preflight decision table. Add one short decision line inside the affected entry and recommend the format-compliant default.

### 7. Keep body-audit diagnostics separate

Store one-time status tables under `_internal/body_audit.md` or `analysis/`. Put only a single link to that audit in the cheatsheet.

### 8. Use one final verification checklist

Collect all final checks at the end of the document. Do not repeat a verification gate in every entry.

### 9. Isolate cycle metadata

Keep changelog details, reviewer-to-mutation mappings, dependency tables, wording notes, refine markers, mutation counts, and tier statistics in `_internal/draft_meta.md`, never in the user-facing draft.

### 10. Optimize for readability

The positive principle behind the preceding constraints is the user's original instruction: _“뭐가 됐든 사용자 가독성을 고려해야 한다.”_

- Break overview, rationale, and instruction prose at semantic boundaries. Split paragraphs longer than four or five sentences.
- Split a long location into `Location` and `Paste together` lines.
- Use bullets for parallel conditions and options.
- Keep one blank line between entries and two between major sections.
- Prefer short sentences and natural wording in the selected artifact language.
- Use tables and boxes only where they materially improve comparison or navigation.

After drafting, read the first screen from the user's perspective and split any block that obscures the next action.

Hard-fail and rewrite when the user-facing draft contains any of the following:

- More than seven frontmatter content lines, excluding delimiters.
- No H1 or overview paragraph on the first screen.
- Two or more opening blockquotes besides the legend.
- A Markdown table inside an entry; LaTeX `\begin{tabular}` is allowed.
- An inline `<!-- memo: ... -->` marker.
- Paste order presented as a table instead of an ordered list.

## Figure-asset gate

After Step 4 draft generation and before Step 5 review, extract every `\includegraphics{...}` argument and check whether `<paper_dir>/<path>` exists.

For each missing asset, choose one path:

### Path A — `--figures auto` (default)

Dispatch the `material/figure-gen` unit with an `<asset spec>`. Derive the specification from the caption and surrounding prose: function, axes, curves, target dimensions, and venue style. After the unit creates the PDF, reproducibility script, and preview PNG, update the entry with their paths.

### Path B — `--figures flag`

Flag the missing asset in the entry and provide a complete specification, including curve functions, axes, and recommended dimensions. The user can create it or dispatch the `material/figure-gen` unit separately.

Asset generation belongs to the `material/*` units so paste-ready LaTeX remains focused and figure style stays consistent across the paper.

Default locations:

- PDF: `<paper_dir>/figures/<name>.pdf`
- Reproducibility script: `<paper_dir>/figures/plot_<name>.py`
- Preview PNG: `<paper_dir>/figures/<name>_preview.png`

Embed the preview with a relative path so it appears in Markdown preview, for example:

```markdown
![Figure description](../../../../<paper_dir>/figures/<name>_preview.png)
```
