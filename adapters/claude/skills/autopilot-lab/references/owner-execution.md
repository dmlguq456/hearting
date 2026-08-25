# autopilot-lab

> **Output location**: `<artifact-root>/experiments/` under the CONVENTIONS §5 (`<agent-home>/core/CONVENTIONS.md#5-skill-output-convention--t1t2t3`) 3-tier layout. Keep one `_RUNLOG` timeline and append one directory per experiment.

## Purpose

Rapid experiment-prototyping entrypoint for training setup and checkpoint evaluation. Add just enough structure to preserve what ran, why it ran, and what happened: one-screen spec, reproducible scaffold, machine-readable run state, and one-screen summary.

This capability fills the gap between research, specification, and production code:

| Capability | Purpose | Output |
|---|---|---|
| `autopilot-research` | External paper, technology, or market survey | Research report |
| `analyze-project` | Extract codebase and experiment conventions | `analysis_project/` |
| `autopilot-spec` | Define a reproducible research or CLI blueprint | `spec/` |
| **`autopilot-lab`** | Prototype a training setup or evaluate a checkpoint | `experiments/` |
| `autopilot-code` | Refine, productionize, or package code | `plans/` |

Heavy training and evaluation run in the user's compute environment, cluster, GPU queue, or scheduler. `autopilot-lab` prepares commands and scaffolds before the run, then analyzes and records results after the run. Do not claim that remote compute was executed unless the user explicitly places an executable environment in scope and the normal approval contract permits it.

## Reference Index

| File | When to load (mandatory) | Content |
|---|---|---|
| `auto-load-context.md` | Every invocation, Step 0 (required) | Context loading from project conventions, optional user-profile conventions, `_RUNLOG`, parent or prior experiments, similar models, and readiness checks |
| `data-contract.md` | When writing machine-readable logs, `run.json`, dispatch data, or `report/` | Append-only `metrics.jsonl`, lifecycle manifest, parent-lineage source of truth, terminal dispatch event, and iframe report contract |
| `config-provenance.md` | Before a full run or when reviewing config lineage | Resolution, sealing, manifest verification, smoke binding, and promotion handoff |
| `setup-procedure.md` | `--mode setup` or auto→setup | S1 spec and review, S2 scaffold and logger, S3 run guidance, `_RUNLOG` pending state, `run.json` birth record, and smoke/debug options |
| `eval-procedure.md` | `--mode eval` or auto→eval | E1 eval spec, E2 execution guidance, E3 analysis, figures, paper comparison, `REPORT.md`, `STORY.md`, `_RUNLOG` completion, `run.json` finalization, and dispatch event |
| `outputs-and-examples.md` | When resolving output layout, graduation, handoff, return format, or examples | Supported experiment layouts, `pipeline_state.yaml`, graduation to spec/code, optional continuity notes, return format, and worked examples |

## Workflow Position

```text
autopilot-research + analyze-project
                  ↓
experiment readiness check
  ├─ not ready → autopilot-code cleanup/refactor → recheck
  └─ ready
                  ↓
optional reproducibility blueprint: autopilot-spec --mode research,cli
                  ↓
autopilot-lab: setup → [user runs training] → eval
                  ↓
graduate reusable code or winning configuration through autopilot-code
```

## Git and Worktree Contract

Run experiments in a dedicated worktree and experiment branch, not directly on main. At the first setup—or a parentless first eval—create or reuse sibling worktree `<repo>-wt/<exp-slug>` and branch `exp/<slug>` under OPERATIONS §5.10 (`<agent-home>/core/OPERATIONS.md#510-work-isolation-and-parallel-dispatch`).

Unlike a normal `autopilot-code` worktree, an experiment branch is a long-lived line of work and is not merged wholesale into main.

1. Commit experiment configs on the experiment branch; do not hide them behind gitignore. This preserves the config-to-result relationship.
2. Keep large checkpoints, logs, and artifact-root outputs ignored. Reproducibility comes from committed configs plus `<artifact-root>/experiments/{slug}/`.
3. Graduate only reusable seams, modules, or a winning config through `autopilot-code`; do not merge the entire experiment branch.
4. Treat the branch as a workspace, not the archive. The durable archive is the experiment artifact plus committed configuration.

Keep orchestration in the parent session and edits or setup in the worktree. Respect the normal one-level nested-dispatch limit.

## Experiment Lifecycle

One experiment normally uses two invocations: **setup** → user-run training → **eval**. Together they fill one `_RUNLOG` entry.

| Mode | When | Work | Output/state |
|---|---|---|---|
| `setup` | Before training | Define the experiment, scaffold train/eval/config from a reference or parent checkpoint, and provide run commands | Scaffold plus pending `_RUNLOG` and birth-state `run.json` |
| `eval` | After training | Define evaluation, provide or run permitted evaluation steps, analyze metrics and ablations, compare papers, and produce the report | `REPORT.md`, summary index, `STORY.md`, completed `_RUNLOG`, and finalized `run.json` |

### Parent Lineage

- `setup --parent <slug>` continues training or fine-tuning from the parent checkpoint.
- `eval --parent <slug>` evaluates a parent checkpoint on new data without training.
- Record the human-readable parent link in `_RUNLOG`, `STORY.md`, and `pipeline_state.yaml`. Treat `run.json.parent` as the machine-readable lineage source of truth.

One-shot data conversion, cleanup, or utility scripts are not lab experiments. Route logged work through `/autopilot-code --intensity quick`; use direct work only for a true throwaway task.

## Invocation

Infer `setup` for requests that define a new training, ablation, loss, model variation, or fine-tuning run. Infer `eval` for requests centered on an existing checkpoint, metrics, result analysis, test data, or paper comparison. Use `--parent` when the request clearly extends or reevaluates an existing experiment.

Routing is semantic (WORKFLOW §0.2 (`<agent-home>/core/WORKFLOW.md#02-semantic-primary-routing`)): a request that includes checkpoint reevaluation, new metrics, or new figure/media work keeps this capability primary even when phrased as a report update; refine, spec, draft, and note attach as secondaries. Before long-running eval execution, apply the WORKFLOW §0.3 (`<agent-home>/core/WORKFLOW.md#03-pre-execution-gate-for-long-running-work`) pre-execution gate and, at `standard+`, the eval stage-worker topology.

Defaults:

- `--mode auto`
- `--parent`: infer only when a unique parent is supported by the request and `_RUNLOG`; an explicit value wins
- `--ref`: use the best applicable recommendation from `similar_models.md`; ignore it when `--parent` supplies the base checkpoint
- `--intensity`: choose from scope and stakes; use a light graph for ordinary prototypes and escalate for publication or external-release claims
- `--from`: infer from `pipeline_state.yaml` and the latest relevant `_RUNLOG` entry

Direct boundaries:

- One-shot conversion or cleanup → `/autopilot-code --intensity quick`
- Plot-only work → the `material/figure-gen` unit
- Productionization, packaging, or broader spec work → `/autopilot-code` or `/autopilot-spec`
- Document wording/structure-only fixes with no new empirical work → `/autopilot-refine`
- An explicit `/autopilot-lab <args>` invocation supplies the routing choice directly

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, use the conversation language for user-facing artifacts according to `<agent-home>/roles/response-policy.md`. Preserve code identifiers, layer names, config keys, metric names, and source quotations.

## Arguments

### `--mode`

- `auto`: infer `setup` or `eval` from the request and available artifacts
- `setup`: define and scaffold a training experiment; the user runs heavy training
- `eval`: evaluate and analyze a completed checkpoint, then summarize the result

### `--parent <slug>`

Read the parent's `summary.md`, `STORY.md`, config, and checkpoint path. Use setup for continued training and eval for checkpoint-only reevaluation.

### `--ref <path>`

Select a reference implementation such as `model/TF_Restormer`. When omitted, consult `similar_models.md`. A parent checkpoint takes precedence over a reference baseline.

### `--intensity`

Derive verification rigor from intensity; there is no separate `--qa` axis. Use the canonical mapping in CONVENTIONS §1.1 (`<agent-home>/core/CONVENTIONS.md#11-verification-rigor-tiers`).

### `--from`

- setup: `spec`, `scaffold`, or `run`
- eval: `eval` or `summary`
- Infer automatically when `pipeline_state.yaml` is available

## Coding Constraints for Scaffold Work

Before dispatching scaffold changes to the `dev/new-lib` unit, load `analysis_project/code/experiment_conventions.md` when available. The acting agent may also consult `mem profile 07_coding_convention` when cross-project preferences would materially help; project conventions take precedence.

1. **Minimize changes**: copy and adapt an existing model folder selected by `--ref` or `similar_models.md`; do not introduce a new layer by default.
2. **Prefer existing layers**: follow the project's preferred-layer list. Require an explicit design decision before adding a new layer.
3. **Put minor variation in config**: prefer `config.yaml` over direct `model.py` edits when the existing configuration mechanism supports the change.
4. **Follow variation prefixes**: place fine-tuning variants next to the base file using the project's prefix pattern, such as `_ft01_` or `_ft02_`; use a new model folder only for a new base architecture.

## Confirmation Contract

At each gate, accept four outcomes in the conversation language:

| Outcome | Action |
|---|---|
| Continue | Advance to the next stage |
| Revise | Refine the current stage and write `_internal/refine_v{N}.md` |
| Back-jump | Rerun an earlier stage and reset downstream state |
| Stop | Halt and preserve `pipeline_state.yaml` |

When a materially different experiment decision remains ambiguous, ask rather than guessing. Otherwise, continue under the portable autonomy policy.

## Forbidden Zones

Without explicit scope and the appropriate owning capability, do not:

- introduce layers outside the preferred-layer list
- edit a reference or parent model folder directly; create a variation and preserve the base
- productionize or reorganize modules; route that work to `autopilot-code`
- make PRD or stack decisions; route them to `autopilot-spec`
- treat one-shot data utilities as lab experiments
- claim to run training or remote evaluation that remained in the user's environment
- destructively delete checkpoints or logs outside `_internal/`
- overwrite `configs/` without user approval
- move config files automatically between lifecycle roots

> Treat the [Reference Index](#reference-index) as the single source for reference files, load points, and contents.

## Task

$ARGUMENTS

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/autopilot-lab.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability autopilot-lab
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `experiments/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/experiments/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
