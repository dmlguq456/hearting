## Pipeline

> **Stage dispatch** (`standard+`, OPERATIONS §5.10 ③④, SD-1, SD-2): dispatch each durable stage below as an independent dispatch-depth-2 headless session. The named unit runs inside that stage session. The dispatch-depth-1 conductor passes artifact paths and reads verdicts or status only; each stage reads its inputs from files. Keep `direct`, `quick`, and micro-stages inline. Stage sessions must not redispatch because dispatch depth 3+ is forbidden.

| Stage | In-session owner | Input artifacts | Output artifacts | Write class |
|---|---|---|---|---|
| Step 1: Material Analysis | Orchestrator; candidate stage | Discovered reference and analysis materials | `analysis/material_index.md`, `analysis/{ref,reviewer}_analysis.md` | analysis |
| Step 2: draft-strategy | `draft-strategy` subskill | `analysis/*`, discovered inputs | `strategy/strategy.md`, optional mirror | strategy |
| Step 4.0b: On-demand Figure Extraction | `material/pdf-extract` unit; candidate stage | Paper-card PDF paths | `figures/figure_index.md` | figures |
| Step 4.1: Draft Generation | `research/research-survey` unit | Strategy, Style Guide, analysis, discovered inputs | `draft/draft.md` | draft |
| Step 5.5: Editorial Polish | finalize node (`editorial/report` unit), mode-B polish | Primary draft and optional mirror | Same files, polished in place | draft, sequential single writer |
| Step 4b: Post-draft Factual Detector | Orchestrator | Primary draft and optional mirror | Decision Points row | inline micro-stage |
| Step 4c: Report Figure Semantic Gate | Orchestrator/QA | Spectrogram report, JSON manifest, generated PNG | Fail-closed verifier result and visual-review evidence | inline micro-stage |

Never let two stages mutate the same file concurrently. Step 4.1 writes `draft.md`; Step 5.5 may polish it later in sequence.

### Preflight validation

Run these checks before creating an artifact directory or invoking a subskill or agent. Abort immediately on a hard failure.

#### Universal checks

1. Resolve exactly one supported form: `paper`, `presentation`, or `doc`. Otherwise report `Unknown mode: {mode}. Supported: paper / presentation / doc.`
2. Fuzzily match the task against `<artifact-root>/analysis_project/{paper,doc}/*` and `<artifact-root>/research/*`.
3. Store matching paths in `{discovered_inputs}`.
4. Infer the `doc` genre from multilingual intent rather than requiring one fixed language or signal phrase.

Input requirements:

- `paper` and `presentation`: no hard input requirement. Warn when nothing matches and continue with a documented assumption unless a missing source makes the request impossible.
- `doc` rebuttal intent, including functional literals such as `rebuttal`, `OpenReview 응답`, or `리뷰 응답`: require a reviewer-comment file in `analysis_project/doc/*/reviewers/`.
- `doc` peer-review intent, including `peer review`, `review form`, or `리뷰 작성`: require a venue review form in `analysis_project/doc/*/formats/` and the target paper.
- Other `doc` genres such as reports, proposals, blogs, and memos: no hard source requirement; warn when none match.

#### Format-spec resolution

Search `analysis_project/doc/{matching}/formats/`:

- One candidate: use it and report `format spec auto-discovered: {path}`.
- Several candidates: include the selection in Step 0.
- None: apply the genre-specific fallback.

| Form or genre | Fallback |
|---|---|
| `paper` | Use a generic LaTeX article layout and strongly recommend the actual venue template for academic submissions. |
| `presentation` | Use generic slide-by-slide Markdown; a lab or venue template remains optional. |
| `doc` peer review | Abort and require the venue form; there are no built-in year-independent presets. |
| `doc` rebuttal | Require reviewer comments, then offer to preprocess a format, accept inline constraints, or use a warned generic rebuttal layout. |
| Other `doc` genres | Use generic prose and recommend the relevant institutional template, especially for grants. |

Determine rebuttal subtype from the format spec or task: `meta-only`, `reviewer-dialogue`, or `response-with-revision`. There is no subtype flag.

On abort, print a clear user-facing error, do not call `mkdir`, do not invoke a subskill, and do not write `pipeline_summary.md`.

After preflight succeeds, create `artifact_dir` and continue.

### Step 0: Scope clarification

This is the document-track instance of the [Autopilot Intake Gate](../../../core/CONVENTIONS.md#66-autopilot-intake-gate). Clarify before execution only when ambiguity would materially alter the deliverable.

Trigger when any of these is true:

- Mode or genre inference is low-confidence or multi-match.
- The task is shorter than about 15 words and lacks a concrete deliverable.
- A peer-review request omits venue, length, or review form.
- A presentation omits audience or duration.
- A proposal omits funding body, deadline, or budget scope.

Ask two to four focused questions in the user's communication language. Suggested topics:

- Paper, report, proposal: audience, page or length limit, emphasis, deadline.
- Presentation: audience expertise, duration, one core message.
- Peer review: venue, review form, scoring system.
- Rebuttal: length, whether new experiments are possible, response tone.

Skip clarification when `--no-clarify` is set, the task is already concrete, or `--from <stage>` resumes captured state. If a non-blocking question receives no response, proceed with the recommended narrow default and report the assumption once.

Store a one-line resolved intent in `pipeline_state.yaml` as `clarified_intent`.

### Step 1: Material analysis

Read and catalog every discovered input.

1. Write `analysis/material_index.md` with file paths and brief descriptions.
2. Analyze according to form and genre:
   - Rebuttal: write `analysis/reviewer_analysis.md` with reviewer, point, and severity breakdowns.
   - Paper: write `analysis/ref_analysis.md` covering methods, gaps, and positioning.
   - Peer review: assess the target paper's method and quality.
   - Report: assess findings, evidence, and data quality.
   - Proposal: assess prior art, feasibility evidence, and competitive context.
   - Presentation: extract key messages, audience needs, and narrative structure.
3. Use the applicable PDF-extraction contract for large PDFs and read them in bounded page ranges.
4. Briefly report the inventory, then continue without asking for routine confirmation.

### Step 2: draft-strategy

Invoke:

```text
draft-strategy <resolved_mode> --inputs <comma-separated-discovered-paths> --output <artifact-dir> <task description>
```

Map form-first modes to draft-strategy labels:

| autopilot-draft form | Genre intent | `<resolved_mode>` |
|---|---|---|
| `paper` | Any paper genre | `paper` |
| `presentation` | Any presentation genre | `presentation` |
| `doc` | Rebuttal, response, OpenReview, reviewer reply | `rebuttal` |
| `doc` | Peer review, review form, assessment | `review` |
| `doc` | Report, progress, results, status, mid-project report | `report` |
| `doc` | Proposal, grant, RFP | `proposal` |
| `doc` | Memo, blog, or other prose | `report` fallback |

Use meaning across languages rather than a closed set of mandatory signal words.

After draft-strategy returns, read `strategy/strategy.md` and require a `## Style Guide` section. If absent, append a localized version of this template to the primary strategy and mirror it only when a mirror exists:

```markdown
## Style Guide

> Formatting rules for this artifact. Draft generation and refinement must consult this section first.

### Citations and venues
- Prefer the user's established venue and citation conventions when available as context.
- Otherwise use `{venue abbreviation} {year}` for published work and `_arXiv:XXXX.XXXXX_` for arXiv-only work.
- If both apply, use `{venue} {year} / arXiv:2402.XXXXX`.
- Use `[Author et al., YYYY]` for inline author-year citations.
- Never show a year without its venue when the venue is known.

### Figure captions
- Cited figure: `**Figure N**: {one-line caption}. Source: cards/{file}.md`
- Original diagram: `**Figure N**: {caption}`

### Bullet depth and speaker notes
- Limit body bullets to three levels.
- Number speaker notes as `1.`, `2.`, `3.`; do not use dash bullets for notes.

### Model classification
- Copy model, venue, task, and year values verbatim from `{research_artifact}/cards/*.md`.
- Exclude unsupported models or mark them `[?]`.
- Reuse category labels found in the source card's `## 분류` section. Do not invent a category without explicitly extending the cards.
```

Treat this Style Guide as the artifact's formatting source of truth. Memory may inform judgment and preferences, but it does not create rigid output-language rules or fixed recall signals.

### Step 4: Draft generation

All three forms generate a draft.

#### Step 4.0a: Discover figures from multiple sources

Search in this order:

1. `<artifact-root>/research/*/figures/figure_index.md`, choosing the best topic match.
2. `<artifact-root>/analysis_project/paper/figures/figure_index.md`.
3. `{artifact_dir}/assets/figures/figure_index.md` or `{artifact_dir}/assets/figures/*.png`.

Merge every discovered index into a paper-ID-to-path map. On duplicates, prefer research, then paper analysis, then local artifact assets.

#### Step 4.0b: Extract figures on demand

When all indexes and figure directories are empty:

1. Search paper cards for functional fields such as `**PDF 위치**` and `**arXiv ID**` under both analysis and research artifacts.
2. If PDFs exist, dispatch the `material/pdf-extract` unit to create a shared `figures/figure_index.md` and mapped images.
3. Reparse the generated index.
4. If no PDF exists, warn that `analyze-project --mode paper` or autopilot-research can materialize figure sources, then continue without embeds.

Extraction specification:

```text
Dispatch unit material/pdf-extract:
      description="PDF figure/table extraction for document drafting"
      prompt="Inputs: {pdf_paths}.
              Output a figure_index.md mapping paper_id to paths.
              Apply your unit's standing high-resolution policy for paper
              figures and tables.
              Crop to figure body plus caption and remove neighboring-column,
              body-text, and footer noise. Preserve page-wide elements."
```

#### Step 4.0b-quality: Resolution and crop policy

| Asset | Policy |
|---|---|
| Paper figure or table | The `material/pdf-extract` unit's standing high-resolution policy (single source for the DPI numbers) |
| Caption-aware crop | Include figure body and caption; exclude adjacent text and footer noise |
| Two-column element | Restrict the horizontal box to the owning column |
| Page-wide element | Preserve the full content width |
| Slide-source PDF | Render the full page at 160–180 DPI |
| Main results table | Prefer the verified paper crop over manual Markdown retyping |

Visually inspect at least one or two extracted PNGs. Re-extract immediately when they contain neighboring-column remnants, footer noise, or blurred text.

#### Step 4.0c: Relative paths

Compute paths from the draft automatically:

- Research figure: `../../../research/{topic}/figures/{file}.png`
- Paper-analysis figure: `../../../analysis_project/paper/figures/{file}.png`
- Artifact-local figure: `../assets/figures/{file}.png`

Apply the same pattern to `figure_index.md`. Users must not have to calculate these paths.

#### Step 4.1: Dispatch the Survey Unit

Require `strategy/strategy.md` and ensure any `## 미해결 이슈` section is resolved or explicitly accepted. Then dispatch the `research/research-survey` unit with the primary strategy, optional mirror, analysis directory, discovered inputs, figure map, and this contract:

```text
Generate a complete working draft from the finalized strategy.

Mode: {mode}
Task: {task description}
Primary strategy: {strategy_path}
Strategy mirror, if present: {strategy_mirror_path}
Analysis: {strategy_folder}/analysis/
Discovered inputs: {discovered_inputs}
Output: {strategy_folder}/draft/draft.md

Read `## Style Guide` before writing. Apply it to every citation, caption,
bullet depth, speaker note, model classification, and venue/year tag.
Use `[?]` instead of fabricating unsupported metadata.

Write one primary-language draft. Do not create a mirror here.
Return only the path and a three-to-five-line summary.
```

#### Select the artifact language

The orchestrator must explicitly state the selected primary language. No agent locale default may override it.

| Form and genre | Default primary language |
|---|---|
| Academic paper body | Venue language, usually English |
| Camera-ready mutation cheatsheet | User's working language because it is an internal work tool |
| Presentation | Audience language |
| Rebuttal or peer review | Venue language |
| Report, post-mortem, proposal, blog, memo | Audience or publisher language |

An explicit user language request always wins. User-facing explanations follow the user's communication language; artifact prose follows its audience and venue.

Once selected, use that language consistently for headings, labels, rationale, paste order, checklists, and comments outside code or LaTeX blocks. Preserve quotations, titles, commands, formulas, code, and LaTeX in their source language when appropriate.

#### Tone propagation

For `presentation` and `doc`, read the strategy frontmatter `tone` field first.

- `tone: administrative`: use factual status reporting and a calm review request. Avoid marketing superlatives, heroic asks, “strengths” framing, artificial Hook → Call-to-Action arcs, and decision-option sales boxes. The speaker reports to decision-makers rather than pitching to peers.
- `tone: default` or absent: use the narrative or persuasive pattern appropriate to the selected genre.

An administrative strategy paired with a heroic-pitch draft is a critical mismatch.

#### Required convention files

- [Common rules](convention-common.md)
- [Paper](convention-paper.md)
- [Presentation](convention-presentation.md)
- [Document prose](convention-doc.md)

These four files are the source of truth for drafting, refinement, and audit. Do not depend on an unrelated external conventions folder.

#### Quality requirements

- Trace every claim to analysis or a discovered source.
- Do not fabricate citations, data, or results.
- Mark uncertainty with `[TODO: ...]`.
- `paper`: fill every substantive section; for paste-ready work, satisfy the complete cheatsheet contract.
- `presentation`: keep slide count within ±10% of the strategy; use separators; fit the 16:9 limits; provide implementable visual specifications. Generate speaker notes only when requested.
- `doc` rebuttal: draft a response to every reviewer point.
- `doc` peer review: fill every required form section and support strengths, weaknesses, and scores with specific paper evidence.
- Other `doc` genres: avoid heading-only sections and provide substantive content throughout.

Do not reread and rewrite the draft in the orchestrator after the agent writes it.

#### Step 4 companion generation — explicit contract only

Skip companion generation unless the user explicitly requests a second language, an external audience contract requires one, or the existing artifact workflow already depends on a companion. A difference between artifact and conversation languages is not sufficient by itself. When a companion is required, dispatch the `editorial/translate` unit (mode A, appended as a conditional companion node at compile) and use the explicit target language and the existing project naming convention.

```text
Mode A — translate from {source language} to {target language}.
Source: {strategy_folder}/draft/draft.md
Target: {strategy_folder}/draft/draft_{language-code}.md
Use the `editorial/translate` unit contract.

Preserve LaTeX commands, paper titles, author names, venues, acronyms, model
names, datasets, metrics, organization names, project names, technical terms,
quotations, and other source-language identifiers when translation would damage
precision. Use one consistent rendering for each concept.

Return the path, a three-to-five-line summary, and at most two deliberate
terminology decisions.
```

Take the target language and audience requirement from the explicit request or existing artifact contract. Resolve genuine ambiguity in Step 0; do not invent a fixed language pair or filename suffix.

### Step 4b: Post-draft factual detector

Run this inline at every intensity.

1. Apply regex, card lookup, and section-context checks to `draft/draft.md` and any mirror.
2. Classify each material claim as `verified`, `unverified`, `ambiguous`, or `conflict`.
3. Do not modify the draft.
4. Append a Decision Points row:

   ```markdown
   | Step 4 | draft factual check | auto | {N + K} unverified/conflict + {M} ambiguous in draft — recommend /audit before publish |
   ```

5. Report counts in one line and recommend `/audit {artifact_short_name} --scope facts`. Explain that `--report-only` requests inspection without an automatic refinement chain.

When every claim verifies, report the verified count and log the clean result.

### Step 4c: Report figure semantic gate

Run this inline whenever a draft embeds a generated spectrogram. Skip only
when the draft contains no generated spectrogram.

1. Require the versioned manifest defined by `core/CONVENTIONS.md §4.1`.
2. Run `python3 <agent-home>/tools/figure-semantic-verify.py --manifest
   <manifest.json> --report <draft-or-report.md>` or the adapter-native
   `figure-gen --verify-report` wrapper.
3. Treat exit 2 as QA failure and exit 64/66/69 as blocked/unavailable. Do not
   finalize or publish when required metadata, exact 48 kHz 0–24 kHz range,
   shared panel scales, registered range-compatible claims, or a hash-current
   representative PNG review is absent.
4. Record the command, exit status, manifest path, PNG hash, and visual-review
   evidence. File existence, dimensions, count, and link checks do not satisfy
   this gate.

### Step 5.5: Editorial polish

At `standard`, `thorough`, or `adversarial` rigor, run one final mode-B polish pass inside the `finalize` node (`editorial/report` unit) after review. Skip it at `direct` and `quick`.

```text
Finalize polish task:
  prompt: `Polish {strategy_folder}/draft/draft.md and its mirror, if present.
This is a user-reviewed, paste-ready artifact. Improve natural wording,
terminology consistency, and sentence rhythm in the selected language.
Preserve claims, numbers, citations, decisions, LaTeX, code, and formulas.`
```

Edit in place once, without a new snapshot, then continue to Step 6. For a paper paste-ready cheatsheet, read but do not alter content inside LaTeX blocks; polish only the surrounding instructions.
