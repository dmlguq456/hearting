## Output data contract for machine-readable experiment dashboards

> Preserve lab behavior and standardize only its output format. `metrics.jsonl`, `run.json`, and `report/` are the machine-readable surfaces consumed directly from disk by the experiment dashboard in worklog §25. **Disk is the source of truth.** The contract source is `e0-lab-contract-scope.md §3` and worklog PRD §25.4; `claude_setting` is only a cross-reference. Human-facing mirrors such as `_RUNLOG`, `REPORT.md`, `summary.md`, and `STORY.md` remain in place. These surfaces supplement rather than replace them.

### `metrics.jsonl` — append-only per-step stream

Store it at `experiments/<id>/metrics.jsonl`, with one record per line. The scaffolded JSONL logger in train/eval scripts is the only writer and appends once per step.

```jsonl
{"step": 1200, "split": "val",   "name": "loss", "value": 0.342, "ts": "2026-06-23T14:02:11Z"}
{"step": 1200, "split": "train", "name": "loss", "value": 0.318, "ts": "2026-06-23T14:02:11Z"}
{"step": 1200, "split": "val",   "name": "psnr", "value": 28.41, "ts": "2026-06-23T14:02:11Z"}
```

| Field | Type | Meaning |
|---|---|---|
| `step` | int | X axis; the UI may toggle to `ts` wall time |
| `split` | str | Curve group: `train`, `val`, or `test` |
| `name` | str | Metric series such as `loss`, `psnr`, or `si_sdr` |
| `value` | float | Scalar value |
| `ts` | ISO 8601 str | Wall clock for time toggles and tail-follow |
| `kind` | optional str | `scalar` by default, with `image` and `hist` reserved; E0 supports only scalar |

Rules: append only, one record per line, and **the file is the source of truth**. Do not ingest this high-volume stream into the DB. worklog filters through `?name=&split=`.

**Path boundary:** the root `experiments/<id>/metrics.jsonl` is the one per-step chart stream. Put a one-off per-run evaluation blob at `runs/run-001/eval_result.json`. Never create `metrics.json` or `metrics.jsonl` under `runs/`; the consumer streams the single root file and would break on distributed same-name files.

### `run.json` — machine-readable run manifest

Store `experiments/<id>/run.json` as the consolidation of existing run facts such as slug, parent, mode, checkpoint, and best result. It introduces no new calculation. `_RUNLOG.md` is its human-readable mirror.

Optional provenance fields are `config_ref`, `config_sha256`, `source_commit`,
`source_dirty`, `source_git_state`, `run_id`, and `config_layout`; their
absence is visible as unsealed provenance for legacy or in-flight runs. The
sealed manifest is the
source of truth for snapshot bytes, and the harness-owned resource-run row
requires these fields when `--config-manifest` is supplied. The harness does not
write `run.json` or rewrite existing registry rows.

Hearting's separate public run record is one atomically replaced file at
`<artifact-root>/resource-runs/<run_id>.json`; `resource-runner observation
--registry <resource-runs.json> --run-id <run_id> --json` reads the same record.
It lists only detected `epochs-jsonl`, `epoch-json-files`, or `tfevents` paths and
metric keys, plus known run/route/host/output/progress/time fields; unknown values
stay empty. Cairn reads those listed files incrementally from NAS and does not
discover runs by scanning folders. The record does not rewrite experiment `run.json`
or replace the root `metrics.jsonl` contract. Existing run/config metadata may
optionally declare an observation path/key filter and known decision metric, proxy,
rules, time budget, baselines, or comparable group; none is required to start a run.

An in-flight process kept alive across a policy change records an optional
`existing_run_exception` object with a fixed shape:

| Field | Type | Meaning |
|---|---|---|
| `reason` | str | Why the running process is exempt from the new-run policy |
| `policy_version` | str | Identifier of the config-provenance policy the process predates |
| `applies_from` | ISO 8601 str | When the new policy starts governing subsequent runs |

```json
"existing_run_exception": {
  "reason": "policy predates this run",
  "policy_version": "2026-08-03",
  "applies_from": "2026-08-04T00:00:00Z"
}
```

The lab procedure writes this object into `run.json`; the harness never reads
or enforces it, since `run.json` is a user-script-owned surface (see
`references/config-provenance.md`).

```json
{ "id": "2026-06-23_lr_sweep", "parent": null, "skill_mode": "setup", "status": "done",
  "config_ref": "experiments/2026-06-23_lr_sweep/config.yaml",
  "ckpt_path": "$AGENT_RUNTIME_DIR/experiments/2026-06-23_lr_sweep/runs/run-001/ckpt/best.pt",
  "ckpt_sha256": "<sha256 once the checkpoint exists>", "ckpt_byte_size": 123456,
  "started_at": "2026-06-23T09:00:00Z", "ended_at": "2026-06-23T14:30:00Z",
  "best": { "name": "psnr", "value": 28.7, "step": 18000 } }
```

| Field | Meaning and lab source |
|---|---|
| `id` | `<date>_<slug>`, the experiment-directory name |
| `parent` | `--parent` slug or null; the lineage-edge source of truth |
| `skill_mode` | `setup` or `eval` from `pipeline_state.mode`; never hardcode it |
| `status` | `running`, `done`, or `failed`, mirrored by `_RUNLOG` status |
| `config_ref`, `ckpt_path` | Config and best-checkpoint pointers |
| `ckpt_sha256`, `ckpt_byte_size` | Optional checkpoint digest and byte size, recorded after creation and verified at handoff/evaluation; absent while running |
| `started_at`, `ended_at` | Birth and completion timestamps in ISO 8601 |
| `best` | `{name,value,step}` from evaluation; source for completion dispatch and summary cards |

Lifecycle: create with `status: "running"` during setup S3-2 or direct eval-only E1-3, then update to `done` with `best` and `ended_at` in E3-4. For `running` and `failed`, omit the `best` key entirely. A running run also omits `ended_at`; a failed run records `ended_at` but still omits `best`.

### Parent-lineage source of truth

Use `run.json.parent` as the machine-readable edge for comparison and lineage graphs. `pipeline_state.parent`, `_RUNLOG (← parent)`, and `STORY.md` mirror the same value. Graph consumers read only `run.json`.

### Completion dispatch from lab to worklog

At evaluation completion, emit the `run.json best` result plus its delta from the parent for the worklog approval queue and board to consume. Lab only emits; it does not push proactively. Worklog E3 performs receipt and card creation under PRD §25.7. Reuse the already computed `run.json best` without new analysis.

“Lab only emits; it does not push proactively” scopes to the worklog approval queue and Layer 1 card creation. The optional artifact-sink extension is separate and app-neutral under `WORKFLOW.md §0.2`: an available registered sink receives the canonical artifact, while an unavailable probe records `skipped/extension-unavailable`.

### `report/` — iframe-rendered report artifacts

`experiments/<id>/report/` is the legacy HTML directory rendered by the dashboard
in a sandboxed iframe. Ownership follows `WORKFLOW §0.2`, not the HTML format:

- Lab continues to generate its existing audio/media playback page at `report/report.html`, including separated audio, spectrograms, `<audio>`, and `<img>` embeds.
- Lab's report/editorial stage also produces the experiment's prose result
  report and canonical `report/index.html` bundle from existing or new results.
- `autopilot-draft` or design owns an independent document or design goal,
  not the experiment report merely because it contains prose HTML.

Prose and playback may coexist with `REPORT.md` under the same lab owner.
Coexistence is not equivalence: each is a distinct representation with its own
`roles` in the manifest `bundle`, and only an explicit
`bundle.equivalence_groups` entry declares two of them equivalent.
