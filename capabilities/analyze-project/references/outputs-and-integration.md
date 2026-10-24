# Standard Output Structure

## Code

```text
analysis_project/code/
├── 00_overview.md or topic_*.md   [T1] module analysis
├── integrated interface_reference [T1]
├── experiment_conventions.md       [T1] lab input: model folders, config, prefixes, preferred layers
├── experiment_readiness.md         [T1] lab input: experiment-readiness checks
├── cleanup_candidates.md           [T1] lab input: unused code, dead branches, stale comments
├── similar_models.md               [T1] lab input: model similarity and `--ref` suggestions
└── _internal/                      [T3]
    └── reviews/                    QA logs
```

## Paper

```text
analysis_project/paper/
├── 00_overview_and_constraints.md  [T1] integrated overview
├── per-paper analysis (*.md)        [T1/T2]
└── _internal/                       [T3]
```

## Document

```text
analysis_project/doc/{name}/
├── 00_overview.md                   [T1] inventory, classification, and target mode
├── reviewers/                       [T2] per-reviewer breakdown
├── formats/                         [T2] extracted templates and guidelines
├── samples/                         [T2] patterns from prior examples
├── misc/                            [T2] other summaries
└── _internal/                       [T3] raw scans and QA reviews
```

# Cross-Skill Integration

`analyze-project` outputs are durable assets consumed implicitly by downstream autopilot Skills:

- `autopilot-code` reads `analysis_project/code/` during module mapping. It also uses `cleanup_candidates.md` and `experiment_readiness.md` for experiment-readiness cleanup and refactoring.
- `autopilot-lab` reads `experiment_conventions.md`, `experiment_readiness.md`, `cleanup_candidates.md`, and `similar_models.md` at every Step 0. Project-local layer, prefix, and config patterns take priority. If absent, lab performs a lightweight scan, asks the user to confirm, and stores the results here.
- `autopilot-draft` resolves sources by form-first mode:
  - `paper` → `analysis_project/paper/`.
  - `presentation` → `analysis_project/paper/` plus matching `analysis_project/doc/{name}/formats/`.
  - `doc`, rebuttal response → matching `reviewers/` plus `analysis_project/paper/`, both required.
  - `doc`, peer review → matching `formats/`, required; fail if absent.
  - `doc`, report, proposal, or generic prose → matching `formats/`, optional.
- `autopilot-research` primarily searches externally but may use owned material under `analysis_project/paper/`.

Inputs are discovered from durable `<artifact-root>/analysis_project/*` and `research/*` artifacts. The family does not require flags pointing directly to external folders.

## Typical Workflow

Place source material inside the project, enter the project directory, and invoke without a positional argument when auto-discovery is sufficient.

```bash
cd <project_root>

/analyze-project --mode code
/analyze-project --mode paper
/analyze-project --mode doc

# Rare external-folder override
/analyze-project --mode doc ~/external_patent_folder/

/autopilot-code --mode dev "<task>"
/autopilot-draft "<task>" --mode presentation
/autopilot-research <topic>
/autopilot-refine "<prompt>"
/autopilot-lab "<one-line experiment>"
```
