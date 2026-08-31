# Hearting Core

> Model-agnostic contract. This file defines the portable workflow substrate. Tool-specific files such as `adapters/claude/CLAUDE.md` are adapters that map this contract onto one runtime.

## 1. Layers

| Layer | Owns | Portable? |
|---|---|---|
| Core | workflow, artifact layout, memory lifecycle, QA tiers, model roles, safety invariants | yes |
| Adapter | tool bootstrap, hook schema, slash commands, status UI, permission model, concrete model mapping, runtime-home projection | no |
| Local runtime | credentials, session state, caches, daemon logs | no |

`adapters/claude/CLAUDE.md`, `adapters/codex/AGENTS.md`, and `adapters/opencode/AGENTS.md` are runtime adapter entry files. Runtime-specific adapter notes live under `adapters/`; future adapters should keep their own bootstrap instructions there and point back to this core contract.

## 2. Agent Home

The canonical neutral name for the installed harness root is:

```text
<agent-home>
```

Runtime code should resolve it in this order:

1. `AGENT_HOME`
2. adapter-specific compatibility variables such as `CLAUDE_HOME`
3. `${XDG_DATA_HOME:-$HOME/.local/share}/hearting/current` when a managed release is installed
4. `$HOME/hearting` when a canonical linked checkout is present
5. `$HOME/agent_setting` when a legacy linked checkout is present
6. the adapter's legacy default install path, currently `$HOME/.claude` for the Claude Code adapter

Use `utilities/agent-home.sh` in shell code when a concrete path is needed.

Supported physical layouts:

```text
$HOME/.local/share/hearting/releases/<version>/  # immutable managed release
$HOME/.local/share/hearting/current              # atomic pointer to active release
$HOME/hearting/                                       # canonical linked maintainer checkout
$HOME/agent_setting/                                  # legacy linked checkout fallback
$HOME/.claude/              # Claude Code runtime home, mostly runtime-owned
$HOME/.codex/               # Codex runtime home, mostly runtime-owned
```

General-user installation uses a checksum-verified managed release. Maintainer
development uses a linked checkout. Release updates stage and validate a new
root before switching `current`; they must not fetch, pull, or rewrite a linked
checkout. Runtime activation remains local after the release has been
downloaded, and session reload or restart boundaries remain runtime-specific.

Runtime homes should be adapter projections, not the source repository. Keep credentials, sessions, logs, SQLite state, caches, and other runtime-owned files in the runtime home. Expose the harness into each runtime home with symlinks or adapter-owned bootstrap files.

Portable model profiles belong to core, while each adapter owns its shipped
concrete model mapping. Installation seeds that mapping once as
`<runtime-home>/agent-config/models.conf`. The seeded file is user-owned: it is
never symlinked, refreshed, reapplied, or removed by a harness update or
uninstall. Runtime consumers select a valid, complete user file as one unit and
otherwise fall back to the shipped adapter default as one unit; they never merge
the two. Native runtime settings and adapter fragments remain outside
`agent-config`.

Project-independent global runtime state (the dispatch attempt registry and
similar cross-project bookkeeping) lives under `<agent-home>/.dispatch/` or an
XDG state directory, never inside a project artifact root — this is the
existing convention (see `core/OPERATIONS.md` §5.10/§5.12), restated here as
policy rather than changed.

Projection example:

```text
<runtime-home>/<adapter-bootstrap> -> <agent-home>/<adapter-projection>/<adapter-bootstrap>
<runtime-home>/core                -> <agent-home>/<adapter-projection>/core
<runtime-home>/<capability-surface> -> <agent-home>/<adapter-projection>/<capability-surface>
<runtime-home>/<role-surface>      -> <agent-home>/<adapter-projection>/<role-surface>
<runtime-home>/<hook-surface>      -> <agent-home>/<adapter-projection>/<hook-surface>
```

Legacy runtime homes remain adapter-owned compatibility paths during migration.
New cross-tool documentation should prefer `<agent-home>` and `<runtime-home>`
unless it is intentionally describing a specific adapter runtime home.

## 3. Artifact Root

The canonical project artifact directory is:

```text
.agent_reports/
```

Existing projects may still use:

```text
.claude_reports/
```

`.claude_reports/` is a legacy alias. Runtime code must recognize both names during migration. New projects and new documentation should prefer `.agent_reports/`.

The artifact root is a **project-wide canonical write surface**, not a
per-worktree directory. Resolve it with `utilities/artifact-root.sh <cwd>`:

1. an explicit absolute `AGENT_ARTIFACT_ROOT`;
2. for Git, the primary worktree's `.agent_reports/`, falling back to its
   existing legacy `.claude_reports/` only when the new root is absent;
3. for non-Git, `cwd`'s own root first (self is not inheritance, no marker
   needed); otherwise a strict ancestor's root is inherited only when that
   ancestor also holds an `.agent-workspace` marker file; otherwise
   `<cwd>/.agent_reports/`.

Linked task worktrees are source-only execution surfaces. A tracked artifact
directory may appear there as a Git snapshot, but it is read-only shadow state
and must never receive agent output. Dispatch adapters pass the canonical root
to workers and grant only the runtime-specific access needed for that path.

The artifact root's top-level population is closed. Every top-level name is one
of the following, and its disposition class states how the harness treats it.
Declaring a name here is classification only: it never assigns a new owner and
never orders a move or a delete.

| Folder | Meaning | Disposition class |
|---|---|---|
| `analysis_project/` | project source analysis | `C-DUR` |
| `research/` | topic research and external references | `C-DUR` |
| `spec/` | current product/code blueprint | `C-DUR` |
| `plans/` | implementation cycles | `C-DUR` |
| `documents/` | document drafts and refinement artifacts | `C-DUR` |
| `experiments/` | experiment setup, evaluation, and run logs (declared, currently absent — a reserved boundary, not an error) | `C-DUR` |
| `designs/` | standalone design decision records (declared, currently absent — a reserved boundary, not an error; spec-owned design instead anchors at `spec/design/`) | `C-DUR` |
| `campaigns/` | W7C producer output: `campaigns/<camp>/cycles/<cyc>/artifacts/<bucket>/…` plus the machine-managed `campaign.json` and per-cycle `manifest.json` commit point; the only new-write target once the write-cutover is active (`utilities/artifact_producer.py`) | `C-DUR` |
| `shared/` | immutable shared revisions `shared/<spec\|analysis\|research>/<ref>/revisions/<rrev>/…`; created only by `admit-shared` from a sealed cycle, research only with an explicit promotion; never a direct write target | `C-DUR` |
| `_internal/` | cycle-internal support material — a cycle's child, not an independent entry | `C-INT` |
| `reviews/` | review support material | `C-INT` |
| `shards/` | parallel-leg support material | `C-INT` |
| `.runtime/` | artifact-root-scoped runtime state (route lifecycle records, producer cutover/cycle records, stage-session ledgers under `.runtime/stage-sessions/<route_id>/`, and similar); the only bucket name for this, legacy `_runtime/` is read-only | `C-RT` |
| `.core-grounding/` | core-document read-guard state | `C-RT` |
| `.spec-grounding/` | spec read-guard state | `C-RT` |
| `routes/`, `_routes/` | legacy route-record locations outside `.runtime/` | `C-LRT` |
| `notes/`, `proposals/`, `spec-research-alternative/`, `research-alternative/` | present containers whose owner is not declared — recognized, not adopted | `C-LEG(undeclared-container)` |
| `.git/`, `.agents/`, `.codex/`, `.probe-*/` | runtime residue that lives beside artifacts without being one | `C-LEG(runtime-residue)` |
| `_scratch/` | the sole exact census exclusion | `C-SCR` |

The following paths are a sealed-evidence exception set. These exact prefixes
(and their descendants) or exact files take precedence over their parent
top-level class. Census does not follow symlinks. The set is closed: adding,
removing, or widening an entry requires a product-spec revision.

| Exact path | Meaning | Disposition class |
|---|---|---|
| `plans/2026-08-24_artifact-knowledge-index-w7/` | sealed W7 knowledge-index evidence | `C-LEG(sealed-evidence)` |
| `plans/2026-08-25_artifact-knowledge-index-w7-e1/` | sealed W7 E1 evidence | `C-LEG(sealed-evidence)` |
| `plans/2026-08-25_artifact-knowledge-index-w7-e2-e3/` | sealed W7 E2/E3 evidence | `C-LEG(sealed-evidence)` |
| `plans/2026-08-25_artifact-write-cutover-w7c/` | sealed W7C cutover evidence | `C-LEG(sealed-evidence)` |
| `spec/artifact-path-contract/_internal/research/` | retained artifact-path-contract research evidence | `C-LEG(sealed-evidence)` |
| `research/hermes-agent/.gitignore` | retained research repository-control evidence | `C-LEG(sealed-evidence)` |

**Write cutover.** The legacy `C-DUR` buckets above (`analysis_project/`, `research/`, `spec/`, `plans/`, `documents/`, `experiments/`, `designs/`) are writable only while the producer cutover is inactive (`.runtime/artifact-producer/v1/cutover.json` absent). Once activated by the approval package, every new write must land under an open cycle's `campaigns/<camp>/cycles/<cyc>/artifacts/`, IDs are issued by `begin` before the first write, and `artifact_producer.py check-write` is the single allow/deny oracle for hooks and writers. The approval-gated follow-ups run through `utilities/artifact_cutover.py`: `migrate-delta`/`migrate-seal` copy the census-classified legacy delta into one sealed cycle and new shared revisions (sources preserved, journaled), `compat-close` records the map set legacy readers resolve through (`resolve-legacy`; the spec gate falls back to the latest `shared/spec/` revision), and `retire` deletes only digest-verified, backed-up sources. Existing legacy content stays readable; nothing is moved or deleted by activation. Readers resolve a bucket through `utilities/artifact_reader.py` (cycle dirs → latest shared revision → the legacy bucket as a read-only fallback, missing legacy paths via `resolve-legacy`); a reader that still opens `<root>/<bucket>/` directly sees only the retirement exclusions.

**Population.** `_scratch/` is the sole exact census exclusion; no other name is
excluded by name. Census never follows symlinks: a symlink is recorded as its own
row and produces no descendant rows, and a symlink whose target resolves outside
the canonical root is not canonical content.

**Runtime records.** The canonical route lifecycle record is
`<artifact-root>/.runtime/routes/<route_id>.json`, with the sole terminal sidecar
`<route_id>.outcome.json` beside it. No other location or basename is a valid
target for a new route record — including root-level `*route*.json`, `routes/`,
`_routes/`, and `.routes/`, and including alias basenames inside the canonical
directory. Records that already exist in those places are legacy migration
input: they stay readable and closeable so no route is stranded, and only new
writes to them are blocked.

**Cycle boundaries.** A cycle boundary is a bucket-specific function, not a
global depth-1 rule. The per-shape boundary table is owned by the component
blueprint `spec/artifact-path-contract/prd.md` D-23; it is not restated here.

**Exactly one disposition.** Except for `_scratch/`, every regular file and
container under the artifact root has exactly one disposition, and none is left
unclassified. Classification implies no destructive action: a `C-LEG` name is
quarantine-typed, never a delete instruction. The disposition rule sequence, its
measurement definitions, and its measured counts are owned by
`spec/artifact-path-contract/prd.md` §16; this section owns only the vocabulary
and the top-level assignment.

## 3.1. Report Bundle Root

Published reports use a user-installed, cross-project storage root named
`REPORT_BUNDLE_ROOT`. It is separate from the project artifact root: artifacts
record how work was produced, while the bundle root holds immutable,
self-contained reports for read-only consumers.

```text
<report-bundle-root>/<project>/<experiment-id>/<version>/report/
├── index.html
├── REPORT.md
├── report_manifest.json
└── media/
```

The installer records the absolute root once under the user Hearting config
and thereafter preserves it. Runtime resolution uses an explicit environment
override before that config. Source and database records carry only stable
`project/experiment/version` identity and `report/index.html`, never the
absolute bundle path. `bundle_id` is the reproducible `project/experiment`.

## 3.2. Agent Notes And Worklog Board

The canonical neutral name for the cross-project continuity board data root is:

```text
<agent-notes-root>
```

It is not the project artifact root and not the unified memory store. It is a
mutable operator-facing state layer used to carry agent work across projects and
sessions:

| Folder | Meaning | Commit policy |
|---|---|---|
| `cards/` | Layer 1 user-owned task/project cards | user data; never commit to the harness repo |
| `_layer2/` | Layer 2 agent-owned notes, catalogs, and source-to-card routing rows | mutable board data; never commit to the harness repo |
| `_triage/` | retired review queue (read-only history) | runtime history; never commit to the harness repo |
| `_feedback/`, `_change_review/` | feedback and change-review queues | runtime/user state; never commit to the harness repo |
| `digests/`, `oncall/`, `study/`, `manual/` | daily summaries, operator reports, study proposals, and board manual content | state/docs for the notes root; never commit to the harness repo unless intentionally mirrored in a separate notes repo |

The neutral name for the UI/application that reads and updates this state is:

```text
<worklog-board-app>
```

The app may live in its own repository or local runtime workspace. Its source,
build output, local DBs, caches, `.env*`, dispatch logs, and worktrees are not
part of this harness repository unless a future migration explicitly promotes a
runtime-neutral board component into `tools/` or another portable source
directory.

## 4. Workflow Invariants

Artifacts move forward in one direction:

```text
research / analysis_project -> spec -> plans
research / analysis_project -> documents -> refinement
```

Each artifact should be changed through the capability that owns it:

| Artifact | Owner |
|---|---|
| `spec/` | spec capability |
| `plans/` | code capability |
| `documents/` | draft/refine capability |
| `experiments/` | lab capability |
| user profile records | analyze-user / post-it capability |

Where work runs is a separate axis from who owns its output. The installed
`compute-hosts` command is the common PATH operator surface; the session host
runs everything by default. When an operator keeps more than one machine, the
user-owned inventory at
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/compute-hosts.yaml` names them, and
`utilities/compute-hosts.py` both measures their live state and starts detached
work on them. Its exact detached-process claim surface reconnects a live root
PID to the launcher session only while PID start time, command hash, and current
ancestry still match. Consult it before starting anything that needs a GPU the session
host lacks, or that would occupy the session host long enough to slow the
conversation; a run started that way survives the session that launched it and
is reachable by id from any host sharing the run root. Nothing chooses a host
automatically — the acting agent does, and having read the inventory is the
difference between choosing and defaulting. `OPERATIONS.md#513-operator-compute-hosts`
owns the mechanics and the boundary against registry-owned resource jobs.
Install and update repair only the exact owned launcher link; foreign files and
symlinks are preserved. Full uninstall removes that shared launcher, while a
partial runtime uninstall retains it. No startup file is edited, no scheduler
selects a host, and no remote agent dispatch is involved.

Destructive filesystem mutation is authority-scoped. Before any unlink,
directory removal, overwriting rename, or rollback, the operation must prove a
canonical target inside an exact allowed root or closed allowlist, an explicit
ownership record, and the current expected state (kind, device/inode, plus
content digest or link target where applicable). Validation of the complete
request happens before snapshots, lock creation, temporary files, or state
writes. A rollback may restore its preimage only while the current path is its
sealed postimage; a different successor is preserved and reported as a typed
conflict. Backups, ambient environment paths, string-prefix containment, and a
path's mere existence never grant deletion authority. External user paths use
stable canonical target locks and atomic replacement without an
unlink-then-write window.

## 5. Adapter Responsibilities

Each adapter should provide:

- a bootstrap note that declares it is derived from core and that edits start in
  core before adapter changes;
- a bootstrap file that loads this core contract;
- a way to expose portable capabilities (`capabilities/`) and portable role profiles (`roles/`);
- a concrete mapping from portable model roles (`fast reviewer`, `deep reviewer`, `external adversary`, etc.) to runtime-specific models, tools, or prompt profiles;
- a projection from the neutral `<agent-home>` repository into the runtime home using symlinks, generated files, or runtime-native registration;
- hooks or checks for artifact order, git safety, and memory writes;
- hooks or checks that prevent adapter edits before the relevant core contract
  has actually been read in the current session;
- compatibility with both `.agent_reports/` and `.claude_reports/` until legacy projects are migrated;
- canonical artifact-root propagation plus a fail-closed guard against writes
  to linked-worktree artifact snapshots;
- a documented realization of `<agent-notes-root>` and `<worklog-board-app>` if
  that runtime reads or updates cross-project worklog state.

## 6. Naming Policy

Use neutral names for new cross-tool concepts:

| Prefer | Avoid for new core concepts |
|---|---|
| agent harness | Claude setting |
| artifact root | `.claude_reports` as a generic term |
| capability | Claude-only Skill semantics |
| role profile | Claude-only Agent semantics |
| model role | vendor model names as portable semantics |
| adapter | tool-specific bootstrap |
