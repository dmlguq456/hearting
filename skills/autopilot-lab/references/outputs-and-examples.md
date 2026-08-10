## Output structure

```text
<artifact-root>/experiments/
├── _RUNLOG.md                      [T1] Timeline: one experiment per row; update pending to done or failed
├── {date}_{slug}/                  One experiment per directory
│   ├── pipeline_state.yaml         [T1] Resume state for --from: mode, parent, and phases
│   ├── run.json                    [T1] Machine-readable run manifest: born running, then done+best; parent is lineage SoT
│   ├── _internal/configs/          Exact sealed config snapshots and provenance manifests
│   ├── metrics.jsonl               [T1] Append-only per-step chart stream: {step,split,name,value,ts}
│   ├── REPORT.md                   [T1] Self-contained final evaluation report with figures inline
│   ├── STORY.md                    [T1] Accumulated narrative: motivation, parent, attempt, result
│   ├── experiment_spec.md          [T1] One-screen specification
│   ├── summary.md                  [T1] One-line index for RUNLOG/parent auto-load; points to REPORT.md
│   ├── train.py / eval.py / config.yaml  [T1] Setup scaffold
│   ├── runs/                       [T2] User-produced run results
│   │   └── run-001/
│   │       ├── ckpt/
│   │       ├── log.txt
│   │       └── eval_result.json    Optional one-off result; never name it metrics.json(l)
│   ├── figures/                    [T2] Plots from the `material/figure-gen` unit
│   ├── report/                     [T2] Staged canonical bundle
│   │   ├── index.html              Canonical published entrypoint
│   │   ├── REPORT.md               Self-contained report copy
│   │   ├── report_manifest.json    Schema v2 closed inventory
│   │   └── media/                  Empty for prose-only; declared media is decode-verified
│   ├── bundle-publication.json     Stable bundle_id/version/entrypoint only; no absolute root
│   └── _internal/                  [T3]
│       ├── plan_reviews/           `research/plan-review` unit logs
│       └── debug_reviews/          `qa/ml-debug` unit logs
```

When project convention keeps variants beside each model, as with TF_Restormer, use:

```text
model/
├── TF_Restormer/
│   ├── model.py
│   ├── config.yaml
│   ├── _ft01_lr_sweep.py
│   └── _ft02_no_mdta.yaml
└── ...

<artifact-root>/experiments/{date}_{slug}/
├── run.json
├── metrics.jsonl
├── REPORT.md
├── experiment_spec.md
├── summary.md
├── STORY.md
├── runs/
├── report/
└── _internal/
```

Choose the latter when `experiment_conventions.md` defines a model-directory variation prefix; choose the former when it defines a separate experiment folder. Project conventions take priority.

## Pipeline state

`experiments/{date}_{slug}/pipeline_state.yaml`:

```yaml
pipeline: autopilot-lab
slug: <slug>
date: <date>
mode: setup                    # setup | eval
parent: <slug or null>         # Lineage for fine-tuning or reevaluation; run.json.parent is machine-readable SoT
ref: model/TF_Restormer        # Reference model; null for parent-based runs
qa_level: light
phases:                        # setup: spec/scaffold/run; eval: eval/summary
  spec: done
  scaffold: done
  run: in_progress
  eval: pending
  summary: pending
last_updated: <timestamp>
```

## Graduation to autopilot-code

Once a lab prototype works:

- For library or publication-code cleanup, run `/autopilot-code "X 라이브러리화"`.
- For PRD consolidation, run `/autopilot-spec --mode research,cli`.

Lab keeps each experiment's summary within one screen, but once the intent becomes productization or library work, graduate it to autopilot-code. A winning config is handed off for user-approved promotion to `configs/`; lab never overwrites the canonical root automatically.

## Memory candidates

The agent may choose to remember recurring ablation patterns, preferred-layer extension points, common readiness gaps, and recurring ml-debug root causes. These are contextual agent judgments, not fixed keyword rules.

## Return format

```text
<artifact-root>/experiments/{date}_{slug}/ -- ✅ {mode}:{phase} complete
```

Then guide the next step:

- spec → ask whether to proceed to scaffold
- scaffold → provide `cd experiments/{date}_{slug} && python train.py --config config.yaml`
- run guidance → record `_RUNLOG` as pending and ask the user to return with `결과 평가` after training
- eval → organize metrics and analysis
- summary → finalize `_RUNLOG`, `run.json`, and completion dispatch, then suggest one next experiment

## Examples

The Korean user prompts below are intentional multilingual request fixtures and must remain verbatim.

### Example 1 — Learning-rate sweep: setup to eval

```text
사용자: lr 1e-3 → 3e-4 비교                           [setup mode]
→ S0: read the last five RUNLOG rows and recommend TF_Restormer from similar_models
→ S1: draft the spec from the prior 28.4 validation baseline, then confirm
→ S2: development/new-lib creates model/TF_Restormer/_ft01_lr_3e-4.yaml, changing config only
→ S3: provide the command and append a pending RUNLOG row

[The user trains on the cluster.]

사용자: lr_sweep 결과 평가해                          [eval mode; infer the latest experiment]
→ E1: resolve experiment and checkpoint, then confirm
→ E2: provide eval guidance or use the `qa/test` unit for a lightweight run
→ E3: draft summary, confirm, and update the row to done with 28.4→28.7 (+0.3)
```

### Example 2 — MDTA ablation

```text
사용자: TF_Restormer 에서 MDTA 빼고 비교
→ S0: cite the best config from the previous lr_sweep
→ S1: specify _ft02_no_mdta
→ S2: replace only MDTA with standard PyTorch MHA; introduce no new layer
→ S3: provide the command and record pending
→ After eval: record that removing MDTA changed 28.7 to 28.1 (-0.6)
```

### Example 3 — Fine-tuning with more data and a parent

```text
사용자: lr_sweep 모델에 newdata 추가해서 fine-tune     [setup --parent lr_sweep]
→ S0: read parent summary, config, and checkpoint
→ S1: record parent, new data, and domain-adaptation motivation
→ S2: set init_ckpt to the parent best checkpoint and create an _ft03_finetune branch
→ S3: provide the command and record pending with the parent marker
→ After eval: report newdata +1.2 with existing test performance preserved
```

### Example 4 — Reevaluation on new data without training

```text
사용자: 그 모델 newtestset 으로 평가만 해봐            [eval --parent <slug>]
→ E1: use the parent checkpoint and newtestset, with no training
→ E2: provide eval.py --ckpt <parent> --data newtestset
→ E3: summarize the domain gap and append a done RUNLOG row with the parent marker
```
