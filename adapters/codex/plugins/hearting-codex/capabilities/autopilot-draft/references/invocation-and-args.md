## Argument parsing

Parse `$ARGUMENTS` into the output form, flags, and remaining task description.

### `--mode`

The optional mode is form-first:

- `paper` — A LaTeX academic deliverable expressed as a paste-ready cheatsheet draft. Here *draft* means a card-based mutation or edit plan that the user can paste into LaTeX, not unconstrained blank-page prose. `autopilot-apply` applies it to `main.tex`. For new papers, each entry supplies a new body block. For camera-ready and major revisions, each entry modifies an existing baseline and must follow anchor and natural-integration rules in `convention-paper.md`.
- `presentation` — Slide-by-slide Markdown for conference talks, seminars, lectures, or cheatsheet variants. PPTX export is not supported; the user transfers the content into PowerPoint. Enforce 16:9 content limits.
- `doc` — Word, HWP, or Markdown prose for reports, post-mortems, proposals, rebuttals, peer reviews, technical articles, or institutional memos. Adapt tone, tense, structure, and artifact language to the genre and audience.

State purpose and genre naturally in the task description; there is no `--subtype` enum. Examples:

- `/autopilot-draft "ICML 2026 camera-ready cheatsheet" --mode paper`
- `/autopilot-draft "DSC 데이터셋 mid-report" --mode doc`
- `/autopilot-draft "OpenReview 응답 작성, reviewer cytr·95wX 응답" --mode doc`

When `--mode` is omitted, infer it from multilingual intent rather than a fixed language-specific recall keyword:

- Presentation, seminar, slide, PPT, or deck intent → `presentation`.
- Paper, camera-ready, revision, LaTeX, thesis, or book-chapter intent → `paper`.
- Report, proposal, grant, rebuttal, review response, peer review, blog, or memo intent → `doc`.
- Otherwise → `doc`, the broadest form.

Report the inference in one line and continue. Confirm it in Step 0 only when genuinely ambiguous.

`survey` mode has been removed. For academic, technology, or market research, run `/autopilot-research --mode academic|technology|market`; autopilot-draft discovers the resulting `research/{topic}/` artifacts.

## Implicit input discovery

Discover persistent inputs under `<artifact-root>/`:

- `analysis_project/paper/` — analyzed papers, available to every form.
- `analysis_project/doc/{matching}/` — reviewer comments, templates, samples, and other document inputs matched fuzzily to the task.
- `research/{topic}/` — external research from autopilot-research, also matched fuzzily.
- `analysis_project/code/` — code context commonly used by reports, proposals, and technical articles.

| Form or genre | Required inputs | Recommended inputs |
|---|---|---|
| `paper` | None | Own and cited paper analyses, venue template, field research |
| `presentation` | None | Slide template, analyzed paper, field research |
| `doc` rebuttal response | Matching `reviewers/` material | Venue format and relevant paper analysis |
| `doc` peer review | Venue review form plus target paper | Additional related work |
| Other `doc` genres | Requirements implied by the task | Organization template, paper or code analysis, field research |

If no required input matches, explain how to preprocess it with `analyze-project --mode {paper|doc} <folder>` and ask only when proceeding would materially change the result. When several candidates match, show the candidates and request a selection.

Prompt variables:

- `{discovered_inputs}` — newline-joined paths selected during preflight. Pass them to subskills as `--inputs <comma-separated paths>`.
- `analysis_project/paper/*.md` — the ground-truth paper-analysis source.

## Intensity-derived verification

[`CONVENTIONS.md §1.1`](../../../core/CONVENTIONS.md#11-verification-rigor-tiers) is the source of truth. Verification is not a separate user-selected `--qa` axis.

- Derive rigor as `direct` → none/light, `quick` → quick, `standard|strong` → standard, `thorough` → thorough, and `adversarial` → adversarial.
- Keep narrative, coverage, and logic with the quality reviewer. Use the fast fact-checker for narrow matching of citations, venues, years, metrics, and lineage against source material.
- Propagate `--intensity` and its derived rigor to draft-strategy and draft-refine.
- If a resume request targets `strategy-refine` or `draft-refine` while stored intensity is `quick`, abort and instruct the user to resume with `--intensity standard` or higher.

## `--user-refine`

Default: false. Set this flag only when the user explicitly requests a review pause, such as `--user-refine`, “사용자 검토 끼워,” or “memo 추가하게 멈춰줘.” Do not invent the pause.

After the `research/research-survey` unit writes memos in Step 3 or Step 5:

1. Set `user_refine: true` and `paused_at_stage: strategy-refine|draft-refine` in `{strategy_folder}/pipeline_state.yaml`.
2. In the user's communication language, report the memo path and the equivalent resume command:

   ```text
   Review memos were written to {memo_path}.
   Add your own memo, then resume with:
       /autopilot-draft --mode {mode} --from <strategy-refine|draft-refine> <strategy_folder>
   ```

3. Exit without writing `pipeline_summary.md`; the pipeline is paused, not finished.

Skip the pause when no memo exists.

## `--from <stage>`

- `analyze` — Step 1 material analysis.
- `strategy` — Step 2 draft-strategy.
- `strategy-refine` — Step 3 review, optional user memo, and strategy refinement.
- `draft` — Step 4 draft generation.
- `draft-refine` — Step 5 review, optional user memo, and draft refinement.
- `finalize` — Step 6 summary.

Resolve the artifact directory from an explicit path or with `ls -d <artifact-root>/documents/*$ARG* 2>/dev/null`. Read `pipeline_state.yaml` to restore `mode`, `intensity`, `discovered_inputs`, and `user_refine`. CLI flags override stored values.

## Format-spec discovery

There is no `--format-ref` flag and no built-in venue preset. Discover actual review forms, rebuttal templates, paper templates, proposal sections, slide conventions, and related guidance under `analysis_project/doc/{matching}/formats/` after preprocessing them with `/analyze-project --mode doc <folder>`.

Accept readable `.md`, `.txt`, `.pdf`, `.html`, `.docx`, and similar files.

Resolution order:

1. One candidate: use it and report `format spec auto-discovered: {path}`.
2. Several candidates: include the selection in Step 0.
3. No candidate: apply the following fallback.

| Form or genre | No-format behavior |
|---|---|
| `paper` | Warn and use a generic LaTeX article layout; recommend preprocessing the venue template. |
| `presentation` | Warn and use generic slide-by-slide Markdown. |
| `doc` peer review | Hard fail and require the venue review form. |
| `doc` rebuttal | Offer to preprocess and retry, accept inline constraints, or proceed with a warned generic format. |
| Other `doc` genres | Warn and use generic prose; recommend an institution template for grant work. |

Extract subtype, section, page-limit, and evaluation-criteria information from the chosen format file. If it remains insufficient, resolve the gap in Step 0 or document a safe assumption.

Presentation output is Markdown only. PPTX export is unsupported because template, font, layout, and OOXML compatibility are unreliable; the user transfers the content using the target PowerPoint template.

## Decision defaults

Run autonomously with safe defaults and pause only for genuine ambiguity or destructive choices.

| Decision | Default |
|---|---|
| Material-analysis confirmation | Continue automatically. |
| Missing required source | Ask during preflight. |
| Missing reviewer comments for a rebuttal | Ask during preflight. |
| Review produces memos | Refine automatically, or pause when `--user-refine` is set. |
| Format-spec resolution | Use discovery and the genre-specific fallback above. |
| Scope clarification | Ask two to four questions, unless `--no-clarify` is set. |

Record actual pauses in the `pipeline_summary.md` Decision Points table. Do not log every routine automatic decision.

## `pipeline_state.yaml`

Update `{strategy_folder}/pipeline_state.yaml` after each completed stage:

```yaml
pipeline: autopilot-draft
mode: presentation
intensity: thorough
user_refine: true
discovered_inputs:
  - <path-to-persistent-input>
format_ref: <path or null>
format_ref_source: <auto-discovered|user-supplied-at-prompt|fallback-generic>
clarified_intent: <string or null>
last_completed_stage: strategy
paused_at_stage: strategy-refine
artifact_dir: <abs path>
```

On resume, CLI flags override stored values. Clear `paused_at_stage` after the corresponding refinement finishes.

## Input-source convention

External material must already exist under `<artifact-root>/`; there is no ad hoc `--refs` path.

| Input | Preprocessing capability | Location |
|---|---|---|
| Academic papers | `/analyze-project --mode paper` | `analysis_project/paper/` |
| Reviewer comments, format templates, and samples | `/analyze-project --mode doc <folder>` | `analysis_project/doc/{name}/` |
| External field research | `/autopilot-research <topic>` | `research/{topic}/` |
| Code context | `/analyze-project --mode code` | `analysis_project/code/` |

## Artifact structure

```text
<artifact-root>/documents/{YYYY-MM-DD}_{short-name}/
├─ pipeline_summary.md
├─ draft/
│  ├─ draft.md
│  └─ draft_{language}.md      # conditional mirror
├─ strategy/
│  ├─ strategy.md
│  └─ strategy_{language}.md   # conditional mirror
├─ analysis/
│  ├─ reviewer_analysis.md
│  ├─ ref_analysis.md
│  └─ material_index.md
└─ _internal/
   ├─ strategy_reviews/
   ├─ draft_reviews/
   └─ versions/v{N}/strategy|draft/
```
