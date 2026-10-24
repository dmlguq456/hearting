### Step 4: Scaffolding and Skeleton Generation for All Five Modes

> **Stage dispatch (`standard+`, OPERATIONS §5.10 ③④, SD-1, SD-2)**: autopilot-spec is intentionally asymmetric. PRD authoring stays conductor-inline because the conductor writes `spec/prd.md` at the point where its own judgment is required; it is not a dispatched stage. The only clearly dispatchable stage is **scaffold** (`dev/new-lib` unit), a separable artifact-producing stage. At `standard+`, dispatch it as a **dispatch-depth-2 headless session** with **file-only handoff**: inputs come from files, never from prior-stage conversation. The dispatch-depth-1 conductor passes paths and reads only verdict/status. `direct` and `quick` stay inline, stage sessions never redispatch, and dispatch depth 3+ is forbidden. Pipelines need not have symmetric four-stage structures.

#### Stage-Worker Mapping

| Stage | Unit | Input artifacts | Output artifacts | Write class |
|---|---|---|---|---|
| PRD authoring | conductor-inline (N/A) | research/analysis outputs | `spec/prd.md` | conductor-inline, not dispatched |
| scaffold (Phase 2) | `dev/new-lib` unit | `prd.md` | scaffolded source/directories | scaffold source |

All five modes use the same scaffold stage. Even in an empty project, the spec phase produces structure and a skeleton; autopilot-code then adds logic to that layout. Reuse existing repositories whenever possible: prefer an external reference from autopilot-research or the closest internal model in the user's codebase, and use a generic skeleton only as the final fallback.

#### Phase 0: Choose and Confirm a Reference Source

Priority, highest first:

| Rank | Source | Use |
|---|---|---|
| 1 | Internal: `analysis_project/code/similar_models.md` or explicit user `--ref <path>` | Closest code in the same repository; strongest convention alignment |
| 2 | External: `research/{topic}/code_resources/`, `07_resources.md` with a pretrained-checkpoint URL, or explicit user `--ref <url>` | Research output such as an official paper repository, Hugging Face Transformers, ESPnet, or Lightning |
| 3 | Generic skeleton fallback | Use only when neither 1 nor 2 exists, after user confirmation |

> **Convention-prepend priority** is independent of reference-source priority. Always load `analysis_project/code/experiment_conventions.md` first as the per-project source of truth, then `mem profile 07_coding_convention` via `python3 <agent-home>/tools/memory/mem.py profile 07_coding_convention` only as a cross-project default that fills missing details. Prepend both relevant bodies to the Phase 2 `dev/new-lib` unit prompt. On conflict, the per-project convention wins.

Render the confirmation naturally in the user's communication language:

```
=== Reference source ===
Mode: <list>
Priority 1, internal: <similar_models recommendation or explicit --ref, if any>
Priority 2, external: <candidate from research/code_resources or 07_resources, if any>
Fallback: generic skeleton, only if neither source exists

Proceed with this reference? (proceed / modify — specify another reference / stop)
```

#### Phase 1: Acquire the Reference Repository or Checkpoint

| Source | Handling |
|---|---|
| Public Git repository (GitHub/GitLab) | Automatically run `git clone <url> /tmp/<name>_ref` |
| Hugging Face model or dataset | Run `huggingface-cli download X/Y` or use the Hugging Face MCP |
| Local path from `similar_models` or `--ref <path>` | Use the path directly |

For a private repository, the user clones it in their environment and supplies `--ref <local-path>`; handle it as a local path rather than cloning it inside this skill.

#### Phase 1.5: Verify a Pretrained Checkpoint Before Use

This is the default for ML/DL work. Before training or retraining, verify that the reference works in the empty target environment. Training is expensive, so start with one-sample inference.

Verification flow:

1. Identify the checkpoint URL or path from the Phase 0 external reference or explicit user input.
2. Extract the reference repository's inference command. Reuse an accumulated Quick verify command from autopilot-research `07_resources` or `06_implementation`; otherwise inspect its README, `inference.py`, or `demo.py`.
3. Run one-sample inference directly in Bash or dispatch the `qa/test` unit in smoke mode.
4. Pass only if the command completes without error and produces a reasonable output shape/value.
5. Report the result and confirm the next action with the user.

```
=== Pretrained checkpoint verification ===
Checkpoint: <URL or path>
Inference command: <one line>
Result: ✅ pass (output shape <X> / reasonable value) | ❌ fail (root cause: <one line>)

(pass → Phase 2 / fail → another reference / proceed anyway / stop)
```

**Phase 1.5 is required by default for ML/DL work with a checkpoint.** Verify before entering an expensive training or retraining path.

Narrow automatic-skip cases:

| Case | Reason |
|---|---|
| Code-only references in `library`, `api`, or `cli` mode | No checkpoint exists to verify |
| An internal `similar_models` reference already verified in the user's codebase | Reverification adds no value |
| Disk/network constraints for a very large checkpoint, such as 100 GB+, or insufficient local disk | The user may explicitly skip with a request such as `"ckpt 너무 무거우니 검증 skip"` or `--no-verify`; verification remains the default. If the checkpoint exceeds the environment's limits, recommend skipping in one line and ask for confirmation. |

For a lightweight checkpoint, from tens of MB to a few GB, enforce verification even if the user asks to skip. This protects the user from beginning fine-tuning when the baseline reference does not run in the empty environment.

#### Phase 2: Adapt to Project Conventions (`dev/new-lib` unit)

```
Dispatch unit dev/new-lib:
  "Mode: scaffold for {target_mode}.
   Reference source: {ref_path}
   Target directories: spec/ + mode-specific directories

   ## Four required code-change principles
   1. Minimal change — copy only the required portions of the reference, then adapt them to project conventions.
   2. Preserve the original layer first — use preferred layers from experiment_conventions.md (first, per-project), supplemented by mem profile 07_coding_convention (second, cross-project default). Per-project conventions win on conflict.
   3. Minor variants belong in config; do not modify model.py.
   4. Variant prefix — follow the prefix pattern in experiment_conventions.md. If absent, use the pattern in mem profile 07_coding_convention, for example _ft01_.

   ## Project conventions (first — source of truth)
   {Quote relevant conventions from analysis_project/code/experiment_conventions.md when available}

   ## User cross-project defaults (second — fill per-project gaps only)
   {Quote model-directory, config, prefix, preferred-layer, and framework defaults from mem profile 07_coding_convention; per-project rules win on conflict}

   ## Mode-specific scaffold outputs
   {mode_specific_outputs}

   ## Do not
   - Introduce a layer outside the preferred-layer list.
   - Copy unnecessary reference-repository content for other datasets, tasks, or experiments.
   - Turn the result into a polished library; that belongs to autopilot-code.

   Return: list of created files + summary."
```

Mode-specific outputs:

| Mode | Output |
|---|---|
| **app** | Existing flow: `create-next-app` + `prisma/schema.prisma` + empty page routes + base layout |
| **library** | `pyproject.toml` or `setup.py` + public API skeleton in `src/<pkg>/__init__.py` + reference export structure |
| **api** | `app/main.py` (FastAPI) or `index.ts` (Express) + router skeleton + reference middleware/auth structure |
| **cli** | `cli.py` entry with argparse or Typer + command/subcommand skeleton + reference command structure |
| **research** | `train.py`, `eval.py`, `config.yaml`, and `model/<name>/` skeleton + reference training-loop/model-layer structure, using preferred layers only and reaching a runnable inference path |

For a combined mode such as `research,cli`, scaffold both modes.

#### Phase 3: Confirm the Skeleton Result

Render this template naturally in the user's communication language:

```
=== Scaffold result ===
Mode: <list>
Reference source: <internal / external / generic>
Created files:
  <list>

(app mode) Scaffolding command: create-next-app or equivalent completed ✓
(research mode) Phase 1.5 checkpoint verification: ✅ pass / ❌ fail / skipped

(proceed → Step 5 / modify → rerun scaffold / back-jump → Phase 0 / stop)
```

### Step 5: CONFIRM Gate — Refinement May Begin

```
Spec complete:
  Mode: <list>
  Key decisions: <3–5 bullets>

(proceed → autopilot-design or autopilot-code / modify → refinement v2 / stop)
```

If `--user-refine` is on, or the user asks to modify the result, enter the PRD refinement loop.
