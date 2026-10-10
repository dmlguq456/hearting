# Capability: autopilot-lab

This is the portable capability contract for `autopilot-lab`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `autopilot-lab` |
| Group | `entry` |
| Supported modes | `setup, eval` |
| Portable meaning | Set up experiments, evaluate, and report results, including fixed results. |
| Argument shape | `<task description> [--mode setup\|eval\|auto] [--parent <slug>] [--ref <similar-model-path>] [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial] [--report] [--from spec\|scaffold\|run\|eval\|summary]` |
| Execution topology | `staged+resource`; registry `capabilities/topologies.json` |
| Entry load phase | `post-approval`; owner contract `capabilities/autopilot-lab.md` |

## Invocation Semantics

Rapid experiment prototype entrypoint. `setup` prepares an experiment from spec
through scaffold, smoke, and the full run when the execution environment and
`full-run` part are included in the approval obtained at route start. Otherwise,
report the run command and leave the part out for a later compose. `eval` owns
experiment evaluation, synthetic failure reproduction, model comparison, and the resulting analysis and report,
including interpretation of already fixed metrics without new measurements.
Prose and HTML reports remain lab outputs; audio/media uses playback HTML.
Reuse sufficient existing results and select only the needed stages. Extension
cases use `--parent <slug>` rather than new modes: fine-tuning creates a setup
config branch, and reevaluation uses eval. Enforce per-experiment folders, a
STORY narrative, and an append-only `_RUNLOG` timeline with pending/completed
state and parent links to prevent overwrites and ad hoc loss. Automatically read
`experiment_conventions.md` and `similar_models.md` from analyze-project, giving
the user's existing layer, prefix, and config patterns priority. Graduate
refinement or library work to autopilot-code.

Within `eval`, the named **Existing-result analysis** default composes
`metrics → diagnose → report`. At strong or higher, include `independent-verify`
with its declared two-way group and automatic owner close. For one requested
new measurement, add `eval-run` with its existing inputs and sealed raw-result
write scope. The mode-specific examples below cover both compositions.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Artifact root: `core/CONVENTIONS.md §5.1`; output placement: `§5`.

## Artifact Producer Lifecycle

W7C write-cutover contract (`utilities/artifact_producer.py`, registry table
`producer_lifecycle` in `capabilities/topologies.json`). The same lifecycle
binds `direct`, `quick`, and `standard+`; only the acting owner differs.

1. **begin before the first write.** After the route is compiled and bound,
   the owner (the inline session for `direct`, the dispatch-depth-1 owner for
   `quick` and `standard+`) runs `artifact_producer.py begin --artifact-root
   <root> --route <route file> --capability autopilot-lab --intensity <intensity>`.
   While the cutover is inactive this returns `legacy-compat` and the legacy
   `<artifact-root>/experiments/` layout stays writable; once active it
   issues `campaign_id`/`cycle_id`/`producer_id` and the cycle directory
   `campaigns/<campaign-locator>/<cycle-locator>/artifacts/` before any artifact exists.
2. **write only inside the open cycle.** Every durable artifact goes under
   `<cycle_dir>/artifacts/experiments/...` (`AGENT_ARTIFACT_OUTPUT_DIR`).
   `artifact_producer.py` owns cycle output paths and shared revisions.
3. **stage workers join, never fork.** `standard+` stage workers receive
   `AGENT_ARTIFACT_CAMPAIGN_ID`/`CYCLE_ID`/`PRODUCER_ID`/`CYCLE_DIR`/`OUTPUT_DIR`
   from the owner (dispatch env pass-through) and call `begin --node <id>`
   on the same route, which resumes the owner's open cycle.
4. **runtime-owned closure.** A new registered owner returns its final report;
   the shared completion controller owns workflow/route closure and exact-cycle
   sealing after PASS and process cleanup. Interrupted closure retains the
   result and transaction, retries without a model turn, and carries a recovery
   notice. `roles/worker-types/owner.md` defines this shared contract. Explicit
   close/finalize commands remain for inline work and legacy recovery.
5. **shared admission.** This capability's output is cycle-local; it is never admitted to `shared/` (only `spec`, `analysis`, and explicitly promoted `research` are shared kinds).

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

Pipeline intensity follows `core/CONVENTIONS.md §1`: `direct` has no plan stage or durable plan artifact; `quick` is one registered-headless dispatch-depth-1 one-shot conductor with its inline micro-plan plus plan-check-lite; `standard+` uses the capability's durable work-cycle plan when applicable. This recipe has no separate `plan-check` node (only `autopilot-code` declares one): `quick` checks its micro-plan inline (plan-check-lite), and `standard+` reviews through the recipe's own review stages — `smoke` and `run-verify` (setup) and `independent-verify` (eval), plus the optional `diagnose` part — rather than after every stage. Verification rigor for those reviews and final verify is derived from intensity; it does not name a model or introduce a separate stage graph. `capabilities/topologies.json` (recipe plus `part_catalog`) is the one stage list; `capability-route.py stages --capability autopilot-lab` prints it.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- use DB memory paths, not runtime-native memory files.

Lab owner start/resume prepares the existing execution access request with the
initialized compute-hosts inventory's exact `run_root`, so normal resource work
can write its run records. This default belongs to the lab owner, not frame
sessions or ordinary code owners. External data roots come from the approved
task: a folder the start card's `범위:` field names is the owner's write root, and
other folders the task names are read roots. An explicit `execution_access_v1`
file through `AGENT_DISPATCH_EXECUTION_ACCESS_FILE` or the existing dispatch
`--execution-access-file` option still works and wins. The request is validated and
delivered through the existing owner/child path; children cannot exceed the
parent's effective access, and user permissions are never widened.
[Lab execution access](../docs/lab-execution-access.md) documents the request
shape, the derivation and current runtime limits.

## Mode-Specific Semantics

| Mode | Required coverage |
|---|---|
| `setup` | Experiment spec, scaffold, run commands, pending `_RUNLOG` row, birth `run.json`, post-run verification, and a recorded handoff naming the successor workflow. |
| `eval` | Eval spec, evaluation execution or guidance, metrics and per-array analysis, figures/media, report, `_RUNLOG` completion, lineage finalization. |

### Existing-result analysis (eval default composition)

For a request that analyzes existing results without a new measurement, default
to the named **Existing-result analysis** composition: `metrics → diagnose → report`.
At `strong` or higher, include `independent-verify`; its declared two-way group
feeds the automatic owner close, which reconciles both verdicts in one summary.
This uses the existing `eval` recipe and `--graph`, with the same `setup`/`eval`
modes. A frame uses this default when assembling that work; an already decided
route can use this staged example:

```sh
hearting run capability-route compose --prompt-file <task> \
  --shape staged --capability autopilot-lab --capability-mode eval \
  --intensity strong --graph metrics,diagnose,report,independent-verify --explain
```

Reuse the existing `raw-results/**` and `run.json`; omit already completed
aggregation when its metrics are sufficient. At standard or lower, the default
graph is `metrics,diagnose,report`. Selected inputs, report provenance and
completion contracts still apply.

For **analysis plus one small measurement**, add the existing `eval-run` before
metrics. With the required checkpoint, eval spec, smoke attestation and config
provenance already available, use the same command with
`--graph eval-run,metrics,diagnose,report,independent-verify`. If the measurement
needs a new contract or smoke attestation, use
`--graph eval-spec,eval-smoke,eval-run,metrics,diagnose,report,independent-verify`.
Both strong graphs retain the two-way verification and automatic owner close.

Bound a single CPU inference by its sample, checkpoint, count and output paths
in the eval spec and approved task. Run it through `eval-run`, whose write scope
already covers `run.json`, `raw-results/**` and `logs/**`; it retains the registry
resource class, hash-bound smoke/config requirements and supervised continuation.
The `metrics` stage, including `metrics:qa/ml-debug`, writes only `metrics.jsonl`
and `summary-stats.json`. It cannot create the new raw results.

### Setup does not end at the training process

The existing start choice may select `report`, which ends successfully after
scaffold, smoke, and the existing hash-bound attestation; it omits full-run and
all later work. `complete` retains the supervised resource run, verification,
and recorded handoff. Smoke attestation, config provenance, and supervision
remain required.

The `setup` stage graph is `scaffold → smoke → full-run → run-verify → handoff`.
`full-run` is a detached resource run, so it declares the `supervised`
continuation and can never be the workflow terminal: the continuation supervisor
observes its exact termination and advances to `run-verify`, and only `handoff`
is terminal. `handoff`'s gate is satisfied by *recording the successor* — a
registered evaluation route or attempt, or an explicit human gate — so a run that
finishes with "evaluate it later" written in prose is not complete. The
`full-run` carries `start_approval: full-run`; the route card obtains approval
for this part before the route starts. Leave `full-run` out when the approval
or executable environment is absent, and compose it separately after approval.
The resource runner continues to enforce route verification, hash-bound smoke
attestation, config provenance, the governor, and its `supervised` continuation.

For a failed resource run, use `<manifest_run_id>__a<N>` (N starts at 1) on the same route; reusing the previous run ID with a new owner conflicts with its existing watch binding (`resource-watch-binding-conflict`).
`compute-hosts run` resolves the selected devices to host GPU indices and sets `CUDA_VISIBLE_DEVICES` to those indices (for example `0`), so owner scaffolds must not require that value to be a GPU UUID. Explicit `CUDA_VISIBLE_DEVICES=` or `CUDA_VISIBLE_DEVICES=-1` selects CPU-only execution without GPU admission.

This replaced a graph whose last node was the training process itself. On
2026-08-04 the BC_ResNet_tf run finished training and its hard-negative loop, the
wrapper contained no evaluation stage, the resource runner had no completion
callback, and the session ended with no follow-up mechanism registered. See
`core/WORKFLOW.md §0.6` and `core/OPERATIONS.md §5.12`.

For `eval`, `eval-run` is likewise a supervised detached run whose termination
advances `metrics`, and `sync` is the terminal node.

Workload progress is optional. The resource runner supplies
`AGENT_RESOURCE_PROGRESS_FILE`, `AGENT_RESOURCE_RUN_ID`, and `AGENT_RESOURCE_NODE`.
Lab scripts may call `resource_progress.write_progress(completed, "epoch", total=50)`
from the shared `utilities/` helper to atomically publish a short counter; a missing
or invalid observation never interrupts the workload or changes exit/sentinel completion.

The same separation applies to GPU monitoring. Generated execution bridges
read the payload's terminal evidence before sampling again and retain partial
samples plus a warning when a probe fails. `compute-hosts probe --json` returns
availability in the snapshot; a zero command exit does not prove GPU identity
or headroom. Optional monitoring never raises the payload's exit code, and
prelaunch identity/headroom requirements remain explicit checks. A required
during-run sample belongs to the existing `run-verify`/evaluation verification
check: missing evidence can fail that check without changing resource success.

### Parts (SD-165)

For an already verified run's same-code/config resume, epoch extension or repeat,
select `--shape direct|solo --graph resume-run,run-verify`. This starts no frame,
scaffold, smoke, pre-run owner or new approval. Use the route's `resource-runner
start` instruction once with the approved command; the runtime connects its
sentinel and supervisor to the independent verification after exit. The small
graph realizes only that verifier as a depth-1 one-shot and preserves the
requested shape separately. It does not authorize new code/data/model work.
`full-run` retains its smoke requirements; `resume-run` records the explicitly
approved verified-run scope and preserves any supplied config provenance.
Parent completion uses the existing registered delivery surface, with queued,
received and unsupported results kept distinct. A bare process exit is not
verification or workflow completion.

Every stage above is a part `autopilot-lab:<stage>`; `capabilities/topologies.json`
(recipes plus `part_catalog`) is the one stage list and
`capability-route.py stages --capability autopilot-lab` prints it. The presets
stay as they are; the following appear only in an explicit `--graph`:

| Part | Kind / unit | What it adds |
|---|---|---|
| `autopilot-lab:eval-spec` | pipeline-stage, `plan/plan-author` | Writes the evaluation contract `eval-spec.md` (data, checkpoint, metrics, comparison baseline) inside the eval route. |
| `autopilot-lab:eval-smoke` | review-worker, `qa/ml-debug` | Small evaluation pass that writes `reviews/smoke-attestation.json` with `tools/smoke-attestation.py`, hash-bound to checkpoint, eval-spec and the config snapshot; the route links it to `eval-run`'s `smoke-attestation` input as `part_io`. |
| `autopilot-lab:diagnose` | review-worker, `qa/ml-debug` | After `metrics`: `reviews/diagnosis.md` with hypotheses, reproduction conditions, verdict and a falsifying experiment. Optional external inputs (field samples, reproduction conditions) are named in the assignment. Shareable. |
| `metrics:qa/ml-debug` | unit choice | Runs `metrics` with the diagnosing unit instead of `material/data-script`. |

The analysis and new-measurement examples above use these existing parts. When
`eval-spec` or `eval-smoke` is left out of a partial graph, `eval-run`'s
`eval-spec` and `smoke-attestation` inputs are filled from the prior cycle under
the catalog's name mapping (`eval-spec.md`, `reviews/smoke-attestation.json`);
nothing found leaves the route as before.

`autopilot-lab:smoke` and `autopilot-lab:full-run` are shareable to
`autopilot-code` and `autopilot-lab` routes, so a code route can run training
or a benchmark without leaving its cycle. The resource runner's conditions are
unchanged: a verified route, a hash-bound smoke attestation, config-manifest
provenance, the governor, and the `supervised` continuation. `full-run` carries
the declared mark `start_approval: full-run`; it is data for the route card and
the frame catalog, not a gate.

### Eval execution topology (`standard+`)

The full recipe's separable stages of a `standard+` eval are: (1) context and experiment
contract, (2) evaluation harness preparation, (3) checkpoint evaluation run,
(4) metrics and per-array analysis, (5) figures, audio, and playback HTML,
(6) canonical report-bundle assembly, (7) independent verification, (8)
atomic publication to the installed report-bundle root, and (9) spec sync
when applicable followed by the optional identity-only artifact-sink extension. Group stages into workers by file ownership and dependency rather than
opening one session per stage:

| Worker | Owns (write) | Typical stages |
|---|---|---|
| eval worker | eval harness, raw metrics (`metrics.jsonl`, `run.json`), `_RUNLOG` row | 2–4 |
| media worker | `figures/`, audio segments, playback `report/*.html` | 5 |
| report worker | staged `report/{index.html,REPORT.md,report_manifest.json,logs/,media/}`, `STORY.md`, `summary.md` | 6 |
| verification worker | read-only checks; verdict artifact only | 7 |
| publication stage | `report-bundle publish` with explicit project/experiment/version, then destination verification; writes only the installed bundle root and `bundle-publication.json` | 8 |
| closing stage | `autopilot-spec` update when applicable (a research-mode blueprint advances as a roadmap: close the step with its verdict and evidence, re-plan the tail), then offer only `bundle_id`, version, and `report/index.html` to the optional app-neutral sink; unavailable records `skipped/extension-unavailable` | 9 |

This is a stage catalog, not a requirement to repeat completed computation.
Browser verification writes its screenshots, logs and verdict in its review
folder, using a process-owned temporary Chromium profile outside cycle
`artifacts/`. Existing `reviews/**/browser/profile/` and `profile-*/` runtime
directories are left in place and excluded by the producer on close and refresh.
For reporting or media work on fixed results, use `WORKFLOW §0.2.1` to select
the needed subgraph (for example `report,independent-verify,publish,sync` for a publishable
report from existing inputs). Reuse metrics, checkpoints, and media; omit
training, inference, and metric computation when the request does not need
them. Keep the selected stages' intensity, verification, provenance, and
completion contracts. The existing `report` stage uses `editorial/report`;
its prose does not require a draft primary or a new cycle. Do not fabricate
run state or rewrite a completed run merely to report its results.

The main session or its dispatch-depth-1 conductor applies the `WORKFLOW §0.3`
pre-execution gate before the checkpoint evaluation run, dispatches workers
under `OPERATIONS §5.10`, and stays in the flow: liveness watching and harvest
are part of the same work, not a fire-and-forget dispatch. Reevaluation always
uses `--parent <slug>` lineage and the append-only `_RUNLOG`. Running a
separable stage inline requires the recorded reason in the experiment
`_RUNLOG` or `_internal/`.

## Config Lifecycle and Provenance

The 2026-08-03 BC_ResNet_tf pilot accumulated configs without distinguishing
adopted, rejected, and historical reproduction settings. The prior SR_CorrNet
case hid configs in gitignored or per-run directories without a stable snapshot
or hash. These incidents motivate the following contract.

### Lifecycle roots

Unless a repository declares another layout, `configs/` contains adopted public
defaults, `configs_exp/<experiment-slug>/` contains active or unadopted
experiments, and `configs_legacy/` is reserved for historical
model-shape/checkpoint reproduction. New setup configs go under
`configs_exp/<slug>/`.

A repository declares a genuinely different physical layout with a
`.lab-config-layout.json` root declaration (`{"schema_version": 1, "layout":
"<name>", "roots": {"default": "<dir>", "exp": "<dir>", "legacy": "<dir>"}}`);
`tools/lab-config-provenance.py resolve` then resolves bare, `config:`, `exp:`,
and `legacy:` references against those declared physical roots, not the fixed
defaults. Root directories may nest (e.g. an `exp` root inside the `default`
root); attribution uses longest-match on the resolved path. An explicitly
prefixed `config:`/`exp:`/`legacy:` reference must canonicalize into its own
namespace — a nested-root crossover (e.g. a `config:` reference that
longest-match-attributes into a nested `exp` root) is rejected; an explicit
physical path may still reach any nested root. A plain-text
`.lab-config-layout` file or an `experiment_conventions.md` label declares only
the `config_layout` label, not physical roots — the tool's `resolve` output
always exposes the *actually used* `roots` alongside `layout_declaration`
(`json-roots`/`label-only`/`conventions-label`/`none`), so a label-only
declaration that never remapped the physical roots is visible to any caller,
not silently assumed.

### Resolution

Bare names, `config:<name>`, `exp:<slug>/<name>`, `legacy:<name>`, and explicit
physical paths all resolve to a normalized `config_ref` (e.g. `config:a.yaml`)
independent of which input form was used. There is no implicit root fallback
and traversal or symlink escape is rejected, including within a declared
custom layout. An unstructured repository is not rewritten: use an explicit
path, require an exact snapshot, and record `legacy/unstructured`.
Case-insensitive filesystems are an explicit non-goal (Linux-only harness).

### Sealing before a full run

Before a full run, seal the resolved path, normalized `config_ref`, a required
`--slug` and derived collision-safe run ID, config SHA256, source commit, and
source-scoped git state (`source_git_state`; `source_dirty` is
`source_git_state != "clean"`). `seal` derives its output directory from a
required `--artifact-root` as
`$AGENT_ARTIFACT_OUTPUT_DIR/experiments/<slug>/_internal/configs/` — there is no `--out`.
The manifest fields are named by `capabilities/lab-config-manifest.schema.json`
(`schema_version` 2; v1 manifests are rejected without migration) and enforced
by `tools/lab-config-provenance.py`. Same-input retries are idempotent; a
hash-named snapshot with mismatched content fails closed.

### Smoke binding

The hash-bound smoke attestation binds *both* the config snapshot and its
source: `config_sha256`/`config_source_sha256`/`config_source_path` are
top-level fields, and `verify()` requires an input row whose path matches
`config_source_path` and whose digest matches `config_source_sha256` — a
snapshot-only match cannot satisfy this, since source and snapshot bytes are
identical by construction. `verify()` requires `attestation_hash`; config
provenance may be absent as a whole but not partially — if any of the three
config fields is present, all three must be. The snapshot row itself is
proven by a distinct input row carrying the config hash, unless the source's
own real bytes already are the claimed snapshot bytes
(`config_source_sha256 == config_sha256`); genuine binding to the snapshot's
*path* still only happens at `resource-runner start`, which cross-checks the
attestation against the sealed manifest. Any post-smoke config mutation
invalidates it. `_RUNLOG` and existing provenance manifests are append-only.

**Limits (by design):** attestation requires the source file to exist *at
attest time* — a manifest whose source was later deleted stays
`verify`-valid and snapshot-reproducible, but cannot back a *new* attestation.
A sealed manifest is not portable on its own: `verify` re-proves the sealed
identity, not just field shapes — it requires the full
`experiments/<slug>/_internal/configs` directory chain (not just the
hash-named snapshot beside it), the exact `<run_id>.manifest.json` filename,
and that the slug recovered from that chain, together with `config_ref` and
`source_sha256`, recomputes the same `run_id`. The manifest and its snapshot
directory must therefore move together *and* keep their `experiments/<slug>`
parents intact. If `experiments` itself is a symlink to a real sibling
directory, the manifest must be addressed via the documented derived path
(`$AGENT_ARTIFACT_OUTPUT_DIR/experiments/<slug>/_internal/configs/<run_id>.manifest.json`)
— addressing it via the fully-resolved path is rejected, since resolution
collapses the `experiments` segment `seal` itself recorded.

### Execution and evaluation lineage

`config_ref`, `config_sha256`, `source_commit`, `source_dirty`,
`source_git_state`, `run_id`, and `config_layout` are exposed in run metadata
and registered resource-run rows. Run IDs include the experiment slug.
Evaluation uses the runtime snapshot or manifest and never infers current
config from checkpoint directory names; historical compatibility requires an
explicit migration map or provenance manifest. Config lineage is visible
through the resource-run registry JSON and `resource-runner status`/`tail`,
plus lab-owned `run.json`/`_RUNLOG`. Fleet consumes the harness-owned
resource-run global index as a first-class source and renders each exact
`resource-runner` row as a separate `LAB resource` job with config/source
provenance. It recomputes liveness from `pid+starttime+command_hash`; ordinary
unregistered processes are never presented as training runs.

### In-flight compatibility, termination, and promotion

Existing processes are not restarted or altered: preserve their worktree,
command, config path, and run ID. New policy applies to new runs or explicit
restarts, with an `existing_run_exception` object in `run.json`; existing rows
are not rewritten. Recommend winning configs for handoff to the code/spec owner
without overwriting `configs/` without user approval. Keep unadopted configs in
their experiment root; move to legacy only for historical reproduction.
`package-data` has two modes: the default static-declaration check reports
whether the three config roots are named in `pyproject.toml`/`setup.py`/
`setup.cfg`/`MANIFEST.in` (a pre-build declaration, not proof of packaging);
`--archive <path>` verifies an actual built `.whl`/`.zip`/`.tar.gz`/`.tgz`/
`.tar` contains a file under each declared root (symlink and hardlink members
count), exposing the matched member path per root.

## Routing Boundary

Full-run entry is gated by a current hash-bound smoke attestation and detached
resource-run identity. Evaluation reports use one `report_manifest.json`. New
publishable bundles use schema v2 from
`capabilities/report-bundle-manifest.schema.json`, permitting prose-only
reports while requiring a closed file/hash/link inventory. Declared media
additionally requires WAV/MP3/OGG parity through actual bounded ffmpeg decode,
scriptless DOM-bound playback, and the 1:1 evidence set. Exact experiment and
evaluation logs needed for reproduction live under `report/logs/` as ordinary
v2 `files[]` members; log/report/media bytes and absolute bundle paths are never
uploaded to Turso. All active HTML fails closed and Cairn serves only verified
bundles under CSP `script-src 'none'; form-action 'none'`. Only `<a href>` is
remote navigation; every other resource link is inventory-local. Audio must
expose `0:a:0`; waveform/spectrogram must be PNG/JPEG/GIF/WebP with image magic
and an image stream; each sample kind occurs exactly once. Serialized manifests
are at most 1,048,576 bytes and each of `files`/`media` is capped at 10,000 rows.
Publication is sibling-stage,
same-descriptor hash verified, and atomic no-replace; consumers mount the root
read-only. Periodic validation records per-bundle health transitions only while
one bounded global heartbeat proves monitor liveness. Existing-note backfill is
limited to the authoritative 38-bundle census and ordered IDs-only dry-run
mappings; ambiguity, hierarchy/order drift, source hash drift, or canonical
project-root aliasing rejects the whole candidate without changing `l2_notes`.
Legacy
schema v1 remains validated by `tools/report-manifest-verify.py` for 48 kHz/full-band media, summary statistics, hashes,
1:1 audio/waveform/spectrogram/playback sets, and visual evidence. Its optional `bundle`
block declares each representation's `format`, `roles`, and file binding plus one shared
`title` and one `primary_representation_id`; for audio/media evaluations the playback HTML
is the primary `interactive` representation and `REPORT.md` is its `summary`/`navigation`
companion, not an interchangeable equivalent format. A manifest without `bundle` stays
readable and is classified `legacy/unspecified`. The legacy figure-semantic verifier
remains a compatibility checker, not a second report manifest.

`autopilot-lab` owns experiment evaluation and its result report, with or
without new empirical work. Under `WORKFLOW §0.2`, comparisons, interpretation,
limitations, listening examples, and conclusions stay lab/eval even when the
request names a report or HTML. New inference or synthetic failure reproduction
also belongs here; fixed-data font, size, layout, caption, or table-order edits
do not become empirical work merely because a figure is rendered again.
`autopilot-refine`/`autopilot-apply` own document correction, including putting
finalized metrics into an existing paper; `autopilot-draft` owns independent
paper, presentation, proposal, or other document goals. Internal evaluation
writing uses the existing lab report/editorial stage. `autopilot-spec` records
evaluation-policy or blueprint changes without executing them. Reusable
evaluation-driver or HTML-generator implementation/debugging belongs to
`autopilot-code`; small support scripts can stay in the lab implementation
stage without a separate code cycle. Every
completed setup or eval durable terminal evaluates the route-sealed optional
artifact-sink extension under `WORKFLOW §0.2`: after atomic bundle publication,
an available sink receives receipt v2 identity (`bundle_id`, `version`, and
`report/index.html`) without an absolute bundle path or upload, while unavailable
state records `skipped/extension-unavailable` and preserves lab completion.
The extension remains separate from lab execution and other secondary
ownership. None of these secondaries replaces the lab execution, and lab does
not absorb their artifact ownership.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/autopilot-lab/SKILL.md` and `skills/autopilot-lab/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-lab/SKILL.md`, while `skills/autopilot-lab/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info autopilot-lab`. Use `adapters/codex/skills/autopilot-lab/SKILL.md` as the native Codex Skill projection; do not consume `skills/autopilot-lab/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info autopilot-lab`. Use `adapters/opencode/skills/autopilot-lab/SKILL.md` and `adapters/opencode/commands/autopilot-lab.md` as native OpenCode projections; do not consume `skills/autopilot-lab/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/autopilot-lab/SKILL.md` and `adapters/claude/skills/autopilot-lab/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-lab/SKILL.md`, while `skills/autopilot-lab/SKILL.md` remains the compatibility reference kept for parity/drift checks.
