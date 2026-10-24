## Step 0: Auto-load context on every invocation

The skill should not require the user to restate the experiment context. Read these sources automatically:

| Layer | Source | Accumulation unit | Priority and use |
|---|---|---|---|
| **Experiment conventions** | `<artifact-root>/analysis_project/code/experiment_conventions.md` | Project | **First priority.** This project's actual conventions are the source of truth, including external-reference, alternate-framework, and legacy constraints. |
| Cross-project user patterns | `mem profile 07_coding_convention` or `python3 <agent-home>/tools/memory/mem.py profile 07_coding_convention` | Cross-project | **Second priority and fallback.** Fill only gaps in project conventions. If absent, recommend `/analyze-user coding_convention`. |
| Project timeline | Last five rows of `<artifact-root>/experiments/_RUNLOG.md` | One row per experiment | Prior context plus pending and complete status |
| Previous experiment detail | `summary.md` and `STORY.md` from the latest experiment | One experiment narrative | Results and next candidates |
| **Parent experiment** | Parent `summary.md`, `STORY.md`, config, and checkpoint path when `--parent` is used | One experiment | Fine-tuning base or reevaluation target |
| External research | Latest artifact under `<artifact-root>/research/`, when present | Topic | Motivation |
| Code blueprint | `<artifact-root>/analysis_project/code/`, when present | Project | Baseline understanding |
| **Similar models** | `<artifact-root>/analysis_project/code/similar_models.md` | Project | Automatic `--ref` recommendation |
| Cleanup candidates | `<artifact-root>/analysis_project/code/cleanup_candidates.md`, when present | Project | Avoid dead-code areas |
| Experiment readiness | `<artifact-root>/analysis_project/code/experiment_readiness.md`, when present | Project | If incomplete, recommend `autopilot-code` in one line |

### When `experiment_conventions.md` is absent

If `analyze-project --mode code` has never run:

1. Run a lightweight scan: list `model/*/`, sample `config.*`, and read one representative model.
2. Draft the model-folder layout, config mechanism, prefix pattern, and preferred-layer candidates, then ask for one-screen confirmation.
3. On yes, save `analysis_project/code/experiment_conventions.md` and continue.
4. On correction, let the user edit it before continuing.

Later invocations read this file without re-extracting it.

### When experiment readiness is incomplete in setup mode

If `experiment_readiness.md` contains a failure, pause setup and present a concise localized report equivalent to:

```text
=== Experiment readiness incomplete ===
- ❌ Model folders are not separated: model/ is absent
- ❌ train.py and eval.py are not separated: everything is in main.py
- ⚠️ Config mechanism is inconsistent: argparse and YAML are mixed

Recommended first step: /autopilot-code "separate model folders and train/eval, then unify config"
(continue despite gaps / invoke autopilot-code / stop)
```
