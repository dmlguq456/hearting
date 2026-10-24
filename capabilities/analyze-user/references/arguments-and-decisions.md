## Argument Parsing

### `<aspect>` — required

| Aspect | Profile record | Source supplied by `--source <path>`; never hardcode |
|---|---|---|
| `figure` | `mem profile 01_paper_figure_style` | Paper PDFs, figure collections, or PPTX figure sources converted to PNG |
| `writing` | `mem profile 02_paper_writing_style` | Paper PDFs, DOCX, LaTeX `main.tex`, and reports; convert DOCX, HWPX, and HWP automatically |
| `presentation` | `mem profile 03_presentation_strategy` | PPTX sources converted to PDF and PNG for layout fidelity |
| `analysis` | `mem profile 04_analysis_methodology` | Analysis scripts plus paper Method and Experiment sections |
| `domain` | `mem profile 05_domain_expertise` | Papers and an optional user GitHub URL |
| `coding_convention` | `mem profile 07_coding_convention` | Code repositories with `model/`, `train*.py`, `config*.yaml`, and `*.ipynb` patterns |
| `all` | All six records: 01, 02, 03, 04, 05, and 07 | One or more comma-separated source roots; classify recursively by material type |

The source root is always user-supplied. Without `--source`, discover zero materials and print one line requesting a reference-material directory, matching analyze-project document-mode behavior.

### `--source <path>` — optional, repeatable

Add source directories beyond the primary source. Accept comma-separated paths.

### `--mode init|update` — optional, default `update`

- `init`: snapshot the existing record body under `_internal/versions/`, then replace the DB record body. Do not replace a profile file.
- `update`: read the current body through `mem profile <stem>`, merge new evidence, and explicitly label additions, replacements, and removals.

### QA intensity

Use the selected intensity and stage graph for verification rigor and reviewer roles. This capability does not set a fixed reviewer count or automatically require an external adversary.

### `--from <stage>` — optional

Read `_internal/pipeline_state.yaml` and resume from the requested stage:

- `discover` → Phase 1
- `analyze` → Phase 2
- `verify` → Phase 3, including Phase 3.5 prior-version reconciliation in update mode
- `qa` → Phase 4
- `output` → Phase 5
- `repro` → Phase 5b, figure aspect only
- `summary` → Phase 6

### `--user-refine` — opt-in

Pause once immediately before Phase 5 so the user can add memos to extracted patterns. Enable it only on an explicit signal such as a request to insert a review or add memos. Resume with `--from output`.

## Decision Defaults

| Decision point | Default behavior |
|---|---|
| No source found for one aspect | Stop that aspect and continue the others; in `all`, one missing aspect does not stop the rest. |
| Cross-aspect contradiction | Resolve by evidence frequency and recency. If unresolved, record an open question and continue. |
| Phase 4 🔴 finding | Retry Phases 2–3 automatically, at most twice. |
| Two failed retries | Mark the pipeline failed, write the summary, and stop. |
| Figure reproduction gate, Phase 5b | Run only for `figure`. Render, compare, and correct the specification for at most two loops. Pass when correctable gaps converge or only source-replication differences remain; otherwise record an open question. If no SVG renderer exists, skip with one warning. |
| `--user-refine` | Pause once before Phase 5; resume at `output`. |

## Resume State

Command-line flags override stored state.

```yaml
aspect: figure
mode: update
qa_level: adversarial
last_completed_phase: verify
sources_indexed: 47
drafts_complete: [figure]
consensus:
  high: 18
  medium: 7
  low: 4
qa_findings:
  red: 0
  yellow: 3
  green: 12
quarantine_outcome:
  promoted: 2
  dropped: 1
  open_question: 1
timestamp: "2026-05-22T15:30:00Z"
```
