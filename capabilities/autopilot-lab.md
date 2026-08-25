# Capability: autopilot-lab

This is the portable capability contract for `autopilot-lab`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `autopilot-lab` |
| Group | `entry` |
| Supported modes | `setup, eval` |
| Portable meaning | Rapid experiment prototyping around training setup and checkpoint evaluation/analysis. |
| Argument shape | `<task description> [--mode setup\|eval\|auto] [--parent <slug>] [--ref <similar-model-path>] [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial] [--report] [--from spec\|scaffold\|run\|eval\|summary]` |
| Execution topology | `staged+resource`; registry `capabilities/topologies.json` |
| Entry load phase | `post-approval`; owner contract `capabilities/autopilot-lab.md` |

## Invocation Semantics

Rapid experiment prototype entrypoint. The user runs heavy training; the lab
supports the work before and after it. `setup` prepares an experiment from spec
to scaffold and run commands. `eval` analyzes a trained checkpoint through
metrics, ablations, paper comparisons, plots, and optional formal reports
(prose routes to autopilot-draft; audio/media uses playback HTML). Extension
cases use `--parent <slug>` rather than new modes: fine-tuning creates a setup
config branch, and reevaluation uses eval. Enforce per-experiment folders, a
STORY narrative, and an append-only `_RUNLOG` timeline with pending/completed
state and parent links to prevent overwrites and ad hoc loss. Automatically read
`experiment_conventions.md` and `similar_models.md` from analyze-project, giving
the user's existing layer, prefix, and config patterns priority. Graduate
refinement or library work to autopilot-code.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not. Capability-specific output placement follows `core/CONVENTIONS.md` section 5 until this spec is expanded with a stricter per-capability artifact map.

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
   `campaigns/<camp>/cycles/<cyc>/artifacts/` before any artifact exists.
2. **write only inside the open cycle.** Every durable artifact goes under
   `<cycle_dir>/artifacts/experiments/...` (`AGENT_ARTIFACT_OUTPUT_DIR`).
   `artifact_producer.py check-write` is the single allow/deny oracle used by
   `hooks/artifact-guard.sh`; an active cutover hard-denies new legacy
   top-level writes, and `shared/` is immutable in both states.
3. **stage workers join, never fork.** `standard+` stage workers receive
   `AGENT_ARTIFACT_CAMPAIGN_ID`/`CYCLE_ID`/`PRODUCER_ID`/`CYCLE_DIR`/`OUTPUT_DIR`
   from the owner (dispatch env pass-through) and call `begin --node <id>`
   on the same route, which resumes the owner's open cycle.
4. **finalize after route closure.** The owner runs `artifact_producer.py
   finalize --artifact-root <root> --cycle <cycle_id>` once the route is
   closed: it enumerates `artifacts/`, builds and validates the D-6 manifest,
   commits `manifest.json` (the commit point), applies the index, and seals
   the cycle record. Empty output leaves no lineage (D-9). `recover` rolls a
   crashed finalize forward or back from its journal.
5. **shared admission.** This capability's output is cycle-local; it is never admitted to `shared/` (only `spec`, `analysis`, and explicitly promoted `research` are shared kinds).

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

Pipeline intensity follows `core/CONVENTIONS.md §1`: `direct` has no plan stage or durable plan artifact; `quick` is one registered-headless dispatch-depth-1 one-shot conductor with its inline micro-plan plus plan-check-lite; `standard+` uses the capability's durable work-cycle plan when applicable. `plan-check` is required for every non-`direct` graph, but independent QA is not repeated after every stage by default. Verification rigor for plan-check, selected independent reviews, and final verify is derived from intensity; it does not name a model or introduce a separate stage graph.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- enforce git/worktree safety before edits;
- enforce artifact ordering before new durable artifacts;
- enforce spec-read gating when this capability changes spec-backed code or specs;
- use DB memory paths, not runtime-native memory files.

## Mode-Specific Semantics

| Mode | Required coverage |
|---|---|
| `setup` | Experiment spec, scaffold, run commands, pending `_RUNLOG` row, birth `run.json`, post-run verification, and a recorded handoff naming the successor workflow. |
| `eval` | Eval spec, evaluation execution or guidance, metrics and per-array analysis, figures/media, report, `_RUNLOG` completion, lineage finalization. |

### Setup does not end at the training process

The `setup` stage graph is `scaffold → smoke → full-run → run-verify → handoff`.
`full-run` is a detached resource run, so it declares the `supervised`
continuation and can never be the workflow terminal: the continuation supervisor
observes its exact termination and advances to `run-verify`, and only `handoff`
is terminal. `handoff`'s gate is satisfied by *recording the successor* — a
registered evaluation route or attempt, or an explicit human gate — so a run that
finishes with "evaluate it later" written in prose is not complete. The
`full-run-authorization` human gate binds to `full-run` as an entry gate, which
makes `smoke`'s continuation a human gate: a full run is never started
automatically.

This replaced a graph whose last node was the training process itself. On
2026-08-04 the BC_ResNet_tf run finished training and its hard-negative loop, the
wrapper contained no evaluation stage, the resource runner had no completion
callback, and the session ended with no follow-up mechanism registered. See
`core/WORKFLOW.md §0.6` and `core/OPERATIONS.md §5.12`.

For `eval`, `eval-run` is likewise a supervised detached run whose termination
advances `metrics`, and `sync` is the terminal node.

### Eval execution topology (`standard+`)

The separable stages of a `standard+` eval are: (1) context and experiment
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
`<artifact-root>/experiments/<slug>/_internal/configs/` — there is no `--out`.
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
(`<artifact-root>/experiments/<slug>/_internal/configs/<run_id>.manifest.json`)
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

`autopilot-lab` owns new empirical work: training setup, checkpoint
reevaluation, metric/ablation computation, and experiment figure/media
generation. Under `WORKFLOW §0.2`, a request containing such work keeps
`autopilot-lab` as the primary capability even when phrased as a document
update. `autopilot-refine` corrects existing document surfaces only;
`autopilot-spec` records evaluation-policy or blueprint changes without
executing them; formal prose assembly hands off to `autopilot-draft`. Every
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
