# Mode `code`

Analyze a codebase and produce module-level documentation.

## Phase 0: Choose Incremental or Full Analysis

Inspect `<artifact-root>/analysis_project/code/_last_run.yaml`:

| Detection | Action |
|---|---|
| File absent, or `--full` specified | **Full analysis:** scan the codebase and extract all four experiment-support artifacts. |
| File present | **Incremental update:** reanalyze changed files and update only affected documentation. |

### Incremental Procedure

1. Read `_last_run.yaml` for `last_scan_time` and each module's SHA or mtime.
2. List changed files with `git log --since="$last_scan_time" --name-only --pretty=format:` when git is available, or `find <scope> -newer _last_run.yaml -type f \( -name "*.py" -o -name "*.cpp" -o -name "*.ts" \)` otherwise.
3. Classify the change as local—a function or class inside one module—or structural—a new module, model directory, or cleanup.
4. Update only affected outputs:
   - rewrite the changed module's document while preserving unaffected module documents;
   - update only that module's portion of the integrated Interface Reference;
   - update `experiment_conventions.md` for new layers or config changes;
   - update `experiment_readiness.md` for train/eval separation or seed changes;
   - add newly discovered unused code or dead branches to `cleanup_candidates.md`;
   - add rows to `similar_models.md` only for new model directories.
5. Set `last_scan_time` to now and refresh module SHAs.
6. Run Phase 5 QA only on changed entries; target roughly 10–20% of full-scan cost.

### `_last_run.yaml` Schema

```yaml
mode: code
last_scan_time: "2026-05-26T15:30:00Z"
scope: "."
modules:
  - path: src/models/conformer.py
    sha: <git blob SHA or file hash>
    last_analyzed: "2026-05-26T15:30:00Z"
  - path: src/train.py
    sha: <...>
    last_analyzed: "2026-05-26T15:30:00Z"
experiment_artifacts:
  experiment_conventions_sha: <...>
  experiment_readiness_sha: <...>
  cleanup_candidates_sha: <...>
  similar_models_sha: <...>
```

## Phase 1: Codebase Analysis

Resolve scope first:

- Directory target → read recursively.
- Keyword target such as `engine` or `inference` → map it through the project instruction file, then read relevant modules.
- Empty target → derive scope from the project structure section, or fall back to repository entry points and obvious source directories such as `src/` and `lib/`.

Identify each file or module's role and interface, data flow, dependencies, design intent, and core algorithms.

## Phase 2: Documentation

Write role-separated Markdown in the selected target artifact language under `<artifact-root>/analysis_project/code/`. Avoid one monolithic file and focus on code-level detail rather than a usage guide. End every document with:

```markdown
## Interface Reference

| Class/Function | File | Signature | Called by |
|---|---|---|---|
| `ClassName` | file.py:L | `(arg1, arg2, ...) → return` | `caller_module.func` |
| `function_name` | file.py:L | `(arg1, ...) → return` | `caller.func1`, `caller.func2` |
```

Include every public class, key function, and function with cross-module callers. The `Called by` column lets downstream roles, especially the `plan/plan-author` unit, assess impact without re-searching source.

## Phase 3: Project Instruction File

Keep implementation detail in analysis documents. The project instruction file should contain only the document list and coverage table, behavioral rules, project-structure tree, and execution examples. Preserve and merge existing rules.

## Phase 3.5: Persistent Experiment Support

Write four flat files at the `code/` root. `autopilot-lab` reads them on every Step 0; do not regenerate them on every lab invocation.

### 3.5.1 `experiment_conventions.md`

This is the project's experiment-pattern source of truth. Project-local conventions take priority; `mem profile 07_coding_convention` fills only missing entries. autopilot-lab, autopilot-spec, and the `dev/new-lib` unit receive this file first and the cross-project profile second.

Extract the project's actual structure:

```markdown
## Model Folder Structure
- Location: `model/{model_name}/`
- Bundle: model.py + config.yaml + train.py + ...

## Existing Models
- <model_1>: <one-line description>

## Configuration Mechanism
- yaml / argparse / hydra, based on live source
- where minor variants are expressed as config

## Tuning-Variant Prefixes
- `_ft01_`, `_ft02_`, ... for fine-tuning variants
- new base = new model folder; variant = prefixed file in the same folder

## Preferred Layers
- <model_1>: <layer_list>
- introducing a new layer requires explicit confirmation
```

Use `ls model/*/`, one representative model and config, `_ft` pattern search, and import analysis.

### 3.5.2 `experiment_readiness.md`

Check each item as ✅, ⚠️, or ❌ and add one autopilot-code cleanup recommendation for gaps.

| Item | Meaning | Check |
|---|---|---|
| Model folders | Each model is a cohesive `model/{name}/` bundle | `ls model/` |
| Config consistency | One main YAML, argparse, or Hydra mechanism | Import search |
| Train/eval separation | Training and evaluation are not one monolithic script | File presence |
| Base/variant distinction | Prefixes such as `_ft01_` are consistent | Filename patterns |
| Log/checkpoint layout | Runs accumulate under a stable path such as `runs/{run-id}/` | Directories and `.gitignore` |
| Reproducibility | Seeds and git hashes are recorded | Train-script search |

```markdown
## Experiment Readiness

| Item | Status | Notes |
|---|---|---|
| Model folders | ✅ | model/TF_Restormer/, model/SR_CorrNet/ |
| Config | ⚠️ | yaml and argparse mixed |
| Train/eval separation | ❌ | one main.py |
| Prefix pattern | ⚠️ | _ft01_ appears once; inconsistent elsewhere |
| Logs/checkpoints | ✅ | runs/ established |
| Reproducibility | ❌ | no seed handling |

## Recommendation
/autopilot-code "Split main.py into train.py and eval.py; record seed and git hash; consolidate yaml/argparse"
```

### 3.5.3 `cleanup_candidates.md`

List pre-experiment cleanup candidates for autopilot-code:

| Candidate | Detection |
|---|---|
| Unused imports or dead code | `ruff` or `pyflakes` when available |
| Commented experiment residue | Search `# old:`, `# TODO:`, and `# debug:` |
| Many variants in one file | Search branches such as `if config.variant == ...` |
| Unused layers or modules | Definitions with no import references |
| Finished ablation residue | Search `# ablation1`, `# v1`, and `# old version` |

```markdown
## Cleanup Candidates

| File | Location | Type | Assessment |
|---|---|---|---|
| model/X.py:42 | `from old_utils import _legacy_func` | unused import | safe removal |
| model/X.py:120-180 | `if config.variant == "v1":` | dead branch | v2 active; v1 likely removable |
| model/old_layer.py | class defined but never imported | unused module | whole-file candidate |
| train.py:80 | `# TODO: try learning rate ablation` | stale comment | cleanup |

## Suggested Command
/autopilot-code "Remove unused imports, dead branches, and stale experiment comments"
```

### 3.5.4 `similar_models.md`

Provide automatic `--ref` suggestions for autopilot-lab. Extract one-line descriptions from docstrings, layer sets from imports and definitions, datasets from configs, and metrics from train or eval code.

```markdown
## Model Similarity

| Model | Domain | Core layers | Dataset | Metrics | Similarity |
|---|---|---|---|---|---|
| TF_Restormer | speech restoration / TF dual-path | TF dual-path attention, ConvFFN, LayerNorm+GLU | VCTK / URGENT | PESQ / LSD / SI-SNR / DNSMOS | itself |
| SR_CorrNet | speech separation·CSS / TF dual-path | correlation-to-filter (CorrAttention), RMSNorm+SwiGLU | WSJ0-2mix / LibriCSS | SI-SNRi / SDRi (PIT) / PESQ | shares the complex-STFT front-end and TF dual-path blocks with TF_Restormer |

## Reference Suggestion Logic
- speech restoration / enhancement → TF_Restormer
- separation / CSS or correlation-based filtering → SR_CorrNet
- prefer dataset and metric matches
```

These four files persist until a substantial codebase change—new layer, prefix convention, model, or config mechanism—triggers another code-mode analysis.

## Phase 4: Verify Coverage

Confirm that every source file in major directories such as `models/`, `utils/`, and `src/` appears in at least one document. Code-execute owns documentation updates after implementation; hooks do not.

## Phase 5: QA Verification

Unless `--skip-qa` is set, dispatch the `qa/code-review` unit to compare updated Interface Reference entries with live source.

- Scope: files updated in this run.
- Minimum: two entries per file, checking signature, path, and line number.
- Portable role: light QA with a fast reviewer.
- Logs: `<artifact-root>/analysis_project/code/_internal/reviews/`.
