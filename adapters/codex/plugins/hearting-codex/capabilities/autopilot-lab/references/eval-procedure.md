## Eval mode: post-training evaluation and analysis

Evaluate a checkpoint, reproduce an experimental failure, or analyze and report
the experiment's existing results. Use the latest setup experiment or
`--parent <slug>` when applicable. For report/media-only work, reuse sufficient
fixed metrics, checkpoint outputs, and media; select only needed stages under
`WORKFLOW §0.2.1`, retaining intensity and verification. E1/E2 and run-state
creation/finalization below apply when executing an evaluation, not merely
reporting a completed one. Do not rerun inference or alter a completed run's
status/timestamps to satisfy the eval label.

> **Stage dispatch:** use the contract in `setup-procedure.md`. Apply the `WORKFLOW §0.3` pre-execution gate before E2. At standard+, group separable stages into workers by write ownership per the eval execution topology in `capabilities/autopilot-lab.md`, and dispatch them as dispatch-depth-2 headless sessions with file-only handoff. Each worker reads supplied artifact paths such as `metrics.jsonl`, `REPORT.md`, and research inputs, never earlier conversational context. Routed outputs follow each node's declared paths. Running a separable stage inline requires the recorded reason in `_RUNLOG` or the experiment `_internal/` (`OPERATIONS §5.10` inline exceptions).

#### Eval steps and their parts (stage list: `capability-route.py stages --capability autopilot-lab`)

| Procedure step | Part | What the step adds to the part contract |
|---|---|---|
| E1 spec and smoke | optional `autopilot-lab:eval-spec`, `autopilot-lab:eval-smoke` | `eval-spec.md` and a hash-bound `reviews/smoke-attestation.json` that `eval-run` reads; without these parts the owner supplies both, or a partial graph fills them from the prior cycle |
| E2 execution | `autopilot-lab:eval-run` → `autopilot-lab:metrics` | Detached evaluation of `eval.py` and the checkpoint, then metric values in `run.json best` plus `metrics.jsonl`; `metrics:qa/ml-debug` or the optional `autopilot-lab:diagnose` adds cause diagnosis (`reviews/diagnosis.md`) |
| E3-2 plot / E3-5 media | `autopilot-lab:media` | `figures/*.{png,pdf}` and playback `report/*.html` from `metrics.jsonl` and audio outputs |
| E3-3 compare | on request, the `research/research-survey` unit inside the report work | Comparison section in `REPORT.md` from `research/` and `analysis_project/paper/` |
| E3-4/E3-5 report assembly | `autopilot-lab:report` | `REPORT.md`, `STORY.md`, `summary.md` from metrics, figures, and STORY inputs |
| Independent verification | `autopilot-lab:independent-verify` | Read-only verdict record on the final artifacts |
| Bundle publication and sync | `autopilot-lab:publish` → `autopilot-lab:sync` | Run `tools/report-bundle.py publish` with explicit project/experiment/version (never inferred) → `bundle-publication.json`; after terminal sync the optional artifact sink sends only bundle_id/version/report/index.html, or records `skipped/extension-unavailable` |

The dispatch-depth-1 owner integrates worker outputs, resolves cross-stage conflicts, and stays in the flow: liveness watching and harvest belong to the same work (`OPERATIONS §5.10` SD-14), not a fire-and-forget dispatch.

Do not let stages write the same file concurrently without the OPERATIONS §5.8 `.pipeline-lock`. `_RUNLOG.md` is an append-only timeline exception: S3-2 appends the experiment row, and E3-4 updates only that row rather than appending another.

### E1 — One-screen eval specification

**E1-1. Resolve the target.** Use `--from`, the latest pending `_RUNLOG` row, or `--parent` to select the experiment and checkpoint. Prefer the execution-time config snapshot and provenance manifest; never infer the current config from a checkpoint directory name. Historical compatibility requires an explicit migration map or provenance manifest. Infer evaluation data and metrics from the existing test set or new-data request.

**E1-2. Confirm in one screen**, localized to the user's communication language:

```text
=== Eval Spec ===
Experiment:     <date>_<slug> or parent: <slug>
Checkpoint:     $AGENT_RUNTIME_DIR/experiments/<slug>/runs/run-001/ckpt/best.pt
Mode:           eval
Evaluation data:<existing test set or new data>
Metrics:        <PSNR, SSIM, SI-SDR, ...>
Comparison:     <sibling, parent, or paper baseline>

Proceed? (continue / revise / stop)
```

**E1-3. Create `run.json` only for direct eval-only entry.** If `experiments/<id>/run.json` does not exist, create it here; never recreate or overwrite the file born in setup S3-2. For `eval --parent`, write `status: "running"`, `skill_mode: "eval"` from `pipeline_state.mode`, parent slug, current ISO 8601 `started_at`, parent config path or null `config_ref`, and the evaluated parent checkpoint path. Prefer the sealed snapshot/manifest and do not infer config from checkpoint directory names. Omit `best`. E3-4 updates it to done with `best` and `ended_at`.

### E2 — Execution guidance

Run the scaffolded `eval.py` against the checkpoint and data:

```bash
cd experiments/<slug>
python eval.py --config config.yaml --ckpt runs/run-001/ckpt/best.pt [--data <new_data>]
```

The user runs heavy evaluation. For an explicit lightweight request such as `가볍게 평가 돌려줘`, a small test set may be executed by dispatching the `qa/test` unit in functional eval mode with `eval.py` and the checkpoint. It returns final metrics summarized into `run.json best` and the per-step stream path `experiments/<slug>/metrics.jsonl`.

### E3 — Analysis, report, and completion

**E3-1. Draft the result** when the user asks to organize results or the main agent observes eval completion. Use a localized one-screen preview with experiment, attempted change, best/final/delta metrics, sibling ablation table, parent delta, two or three observations, inline figure paths, and one next candidate. Ask whether to save, revise, or stop.

**E3-2. Optional plotting on user request.** For `결과 plot 그려줘` or `ablation 표 정리`, dispatch the `material/figure-gen` unit with `metrics.jsonl`, the project's figure conventions from memory or existing plots, and output paths under `figures/`.

Embed every generated figure in `REPORT.md` with `![<caption>](figures/<plot>.png)`. Do not merely list the path. Embed it in `STORY.md` too when it directly supports the narrative. Markdown already renders images, so do not create HTML for figures alone. Reserve E3-5 HTML for audio or media playback that Markdown previews cannot handle.

**E3-3. Optional paper comparison at standard+ on user request.** For `결과를 기존 paper 와 비교해줘`, dispatch the `research/research-survey` unit with `REPORT.md`, `<artifact-root>/research/`, and `analysis_project/paper/`. Compare metrics with published baselines, locate the changed surface in prior work, and explain whether observations support or challenge paper claims. Add a comparison table and a concise audience-language summary under `## 기존 paper 와의 비교` in `REPORT.md`.

**E3-5. Optional formal report** for `--report`, `보고서 써줘`, `공유용`, or high-stakes publication:

- **Experiment result report:** use lab's existing `report` / `editorial/report` stage with `summary.md`, `STORY.md`, `figures/`, and run metrics. Lab owns comparison, interpretation, limitations, and conclusions even when no new measurements are needed. Prose or sharing does not create a draft cycle. Only an independent paper, presentation, proposal, or other document brief routes to `autopilot-draft`.
- **Playback HTML for audio/media experiments:** have the `material/figure-gen` unit generate separated audio, spectrogram segments, and embedded `<audio>`/`<img>` at the report node's declared HTML output. Markdown previews block `<audio>`, so an audio domain makes this HTML the primary `interactive` playback representation and `REPORT.md` its `summary`/`navigation` companion; they are not interchangeable equivalent formats. Declare both in the manifest `bundle` with one shared `title` and `primary_representation_id`. Split long audio into pages of bounded segments. When necessary, serve locally through `python -m http.server --bind 0.0.0.0 <port>` and provide the URL.

Assemble publishable output in a staged `report/` with canonical
`index.html`, `REPORT.md`, `report_manifest.json`, `logs/`, and `media/`. Copy
the original report/evaluation logs needed for reproduction into `logs/` and
bind them through the exact v2 `files[]` inventory; never upload those logs or
report bodies to Turso. Manifest v2
may declare an empty media array for prose-only research. If media is declared,
audio is WAV, MP3, or OGG and the verifier requires the same actual ffmpeg decode
path, decodable images, scriptless playback HTML, and the complete 1:1 evidence
set. Every local Markdown/HTML/CSS link and every non-manifest file must be
inventory-bound before publication. Only `<a href>` may navigate remotely;
resource href/src values stay inventory-local. Audio must expose a real audio
stream, waveform/spectrogram files must be PNG/JPEG/GIF/WebP image streams, and
each sample kind occurs exactly once. The manifest is at most 1,048,576 bytes
with at most 10,000 rows in each inventory. All active HTML fails closed; the
consumer serves verified playback with CSP
`script-src 'none'; form-action 'none'`.

**E3-4. Save and finalize:**

- `REPORT.md` is the self-contained final report. Put Executive Summary first, followed by background, hypothesis, method, results, interpretation, conclusion, next steps, and reproduction. Embed figures. Define every condition, structure, acronym, and metric so a reader without the conversation understands it. Consolidate summary, STORY, metrics, and figures here.
- `summary.md` is only a one-line index for RUNLOG and parent auto-loading, with a verdict and a pointer to `REPORT.md`; it is not the user deliverable.
- `STORY.md` accumulates motivation, previous or parent context, this attempt, result, and next candidate.
- For an evaluation executed in this work, update the existing experiment row in `<artifact-root>/experiments/_RUNLOG.md` from pending to done with result and next step. Do not append a second row. Mark the attempt with `(← <parent_slug>)` when applicable. Append only when an eval-only entry has no existing row. Use failed status for interruption or failure. Reporting existing results preserves the completed run's state and lineage.
- For that executed evaluation, update the existing `run.json` to `status: "done"`, current `ended_at`, and `best: {name,value,step}` using the same metric as the report and RUNLOG. On failure, write `status: "failed"` and `ended_at` but omit `best`. A report-only failure does not relabel a completed evaluation as failed.
- Emit `run.json best` and parent delta for worklog consumption. Lab emits only; worklog receives and creates cards. Do not recompute the result or push proactively.
- After `REPORT.md`, `STORY.md`, `_RUNLOG.md`, and `run.json` are durable, verify the staged bundle, publish it atomically with explicit project/experiment/version, and verify the destination. Record `bundle-publication.json` without an absolute root. After sync, evaluate the route-sealed optional artifact-sink extension for done and failed terminal states with durable artifacts. When available, send receipt v2 with only stable bundle identity, version, and `report/index.html`; on unavailable, record `skipped/extension-unavailable`. User suggestion does not override either branch.
