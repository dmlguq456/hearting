# Mode `doc`

Analyze miscellaneous document-creation material—reviewer comments, format templates, prior samples, internal notes, and mixed reference packs—and produce structured per-task analysis.

## Phase 1: Discover and Classify Inputs

Resolve input scope in this order:

1. An explicit positional argument selects that folder, including an external-folder override.
2. Otherwise scan the current directory and one level below it for `docs/`, `reviews/`, `templates/`, `reviewer_comments/`, `format/`, and `guidelines/`, plus root-level `*.docx`, non-paper `*.pdf`, `*_review.md`, `*_template.*`, and `*_sample.*` files.
3. Name the output subdirectory from the positional folder basename, the current-directory basename, or the task description.

Classify every file:

| File pattern | Category | Output target |
|---|---|---|
| Filename contains `review`, `reviewer`, or `comment`, or body contains reviewer labels or scores | reviewer comments | `reviewers/` |
| Filename contains `template`, `format`, `guideline`, `cfp`, or `instructions` | format specification | `formats/` |
| Filename indicates a prior example: `sample`, `past`, `example`, or prior-year naming | sample | `samples/` |
| PDF has academic structure such as abstract and citations | paper-like | Recommend `--mode paper`, or place under `samples/` |
| Notes, sketches, or other mixed material | miscellaneous | `misc/` |

If classification remains ambiguous, delegate the judgment to the `research/research-survey` unit.

## Phase 2: Analyze Each Category

Dispatch the `research/research-survey` unit with this prompt:

```text
Analyze doc-creation materials in this folder: {input_folder}
Output: <artifact-root>/analysis_project/doc/{name}/

For each file in input folder, classify and produce structured analysis:

- reviewers/ : per-reviewer breakdown — score, confidence, summary, key points (severity-tagged), tone
- formats/ : structured format extraction — required sections, length limits, page limits, submission window, sub-types (for rebuttal: meta-only / dialogue / response-with-revision), tone guidelines
- samples/ : key structural patterns and stylistic choices observable in past examples
- misc/ : free-form summary indexing the file's content for later retrieval

Also write 00_overview.md at root of {name}/:
- Inventory of all files in the input folder, with classification
- Key findings per category
- Cross-references useful for autopilot-draft downstream
- "intended for mode": likely autopilot-draft mode this material targets (`paper` / `presentation` / `doc`; for `doc`, label the intent as rebuttal / review / report / proposal)

Use the selected target artifact language for narrative; preserve the source
language for verbatim quotes. Return ONLY paths + a concise summary.
```

## Phase 3: Verify

- Confirm that no input file was silently dropped.
- If classification remains ambiguous, ask the user to confirm or override it.
- Write logs under `<artifact-root>/analysis_project/doc/{name}/_internal/`.
