# tools/memory — Unified Memory System (`mem`)

Portable storage and retrieval layer. The specification lives at
`<artifact-root>/spec/prd.md` (`.agent_reports` first, with legacy
`.claude_reports` compatibility).

## Boundary

Short-lived post-its, durable learned memory, and the global profile share one
SQLite store per server. Each local WAL database is that server's serving
truth; no SQLite, WAL, SHM, lock, or derived-index file is shared. The acting
agent decides contextually what to store, retrieve,
promote, merge, or prune. Code owns mechanical integrity, scope isolation,
pending protection, lifecycle execution, bounded telemetry, and recovery.
Servers converge asynchronously by exchanging immutable protocol-v2 operation
objects and pure-folding the validated operation set. `dump.jsonl` is only a
v1-compatible materialized projection, not the convergence or complete-recovery
source.

## Storage layout

| Layer | Location | Git | Purpose |
|---|---|---|---|
| Local serving truth | `${XDG_DATA_HOME:-$HOME/.local/share}/hearting/memory/memory.db` (SQLite WAL; existing `<agent-home>/memory` remains compatible) | ignored binary; never exchanged | semantic records, transactional outbox/applied/frontier/conflict state, local replica identity and counter, peer/migration evidence, graveyard, and rebuildable indexes |
| Immutable exchange | Bare repository at `${XDG_STATE_HOME:-$HOME/.local/state}/hearting/memory-sync/exchange`; Git tree path `protocol/v2/ops/<prefix>/<op_id>.json` | private dedicated Git repository, never checked out | canonical operation objects only; one semantic transaction per immutable path |
| Compatibility projection | `<memory-store>/dump.jsonl` (one ID-sorted record per line) | optionally tracked for old readers | materialized v1-compatible view; never a routine v2 push/fold input and not a complete v2 recovery source |
| Harness projection | `<agent-home>/projects/<cwd>/memory/` | ignored | compatibility surface for stray auto-memory writes absorbed by `mem sync`; `mem project` can rebuild the projection |

Keep `memory.db` on a local filesystem. `MEM_SYNC_DIR` may choose another
absolute private exchange repository, but its real path must remain outside all
synchronized project trees and the local configuration root. The transport
validates existing exchange paths without following symlinks, disables hooks
and repository-provided attributes/filter/merge behavior, and allowlists only
the protocol data tree.
It never uses the exchange checkout as an instruction-discovery cwd. An
unexpected path, symlink/traversal escape, immutable-object byte mismatch, or
executable/policy file is a hard failure before fold or push.

`MEM_SYNC_REMOTE_URL` may explicitly select the transport remote; otherwise the
implementation may derive `origin` from an existing compatibility-dump mirror
repository. `MEM_SYNC_REF` selects the full protected ref and defaults to
`refs/heads/hearting-memory-v2`. Routine synchronization stages only immutable
v2 objects, integrates without force-push, and confirms an outbox item only
after a fresh fetch proves reachability from that protected ref.

A record combines `tier` (`working|durable`), `scope` (`project|global`),
`type`, `delivery_state` (`ordinary|pending|consumed`), a retrieval capsule, and
temporal `status` (`active|superseded`). Working records have
a finite lifecycle; durable records persist until an agent chooses another
action. New `handoff` records and threads created with `--requires-consume`
start pending and remain protected from destructive operations until explicit
consumption. FTS5 and CJK bigram-shadow data live inside `memory.db`, not in a
separate `.index.db` file.

## Commands

Run commands as:

```text
python3 <agent-home>/tools/memory/mem.py <command>
```

| Command | Behavior |
|---|---|
| `add <tier> <type> "<body>" [--headline] [--alias] [--entity] [--topic] [--artifact-ref] …` | Add a record and bounded retrieval capsule after mechanical validation. Repeat capsule-list options as needed. |
| `note "<body>" [--type] [--requires-consume]` | Shorthand for a working record. Use `--requires-consume` for delivery-bearing threads. |
| `candidates "<prompt>" --session-id <id> [--turn-id <id>] [--hook]` | Main-prompt mechanical capsule lookup. Exposes at most six active current-project/global headline-and-ID candidates within 2,400 UTF-8 bytes, never bodies, and publishes a same-turn opportunity receipt on a successful probe. |
| `recall-gate --decision recall\|skip --reason … [--query …]` | Record the work-start opportunity decision without raw prompts; recall executes immediately. Applied outcomes require `--gate-id` and at least one `--record-id`; miss has no record ID. |
| `recall "<query>" [--topic] [--include-superseded] …` | Search active capsules first, then body/CJK/LIKE compatibility paths. Historical rows require explicit inclusion. |
| `topics [topic] [--include-superseded]` | List normalized topics or visible records for one exact topic. |
| `show <id> [--all] [--include-superseded]` | Show one visible record with capsule, temporal metadata, and full body. |
| `consume <id>` | Move a pending handoff/thread to consumed. Retrieval and injection never consume records implicitly. |
| `restore <id>` | Restore one record from the graveyard while preserving action/canonical metadata. |
| `index [--rebuild]` | Rebuild the FTS5 tables embedded in `memory.db`. |
| `export [--target dump\|profile] [--apply]` | Export `dump.jsonl` or an on-demand human-readable profile cache. Profile export is dry-run unless `--apply` is supplied. |
| `import <dump.jsonl>` | Compatibility import of the materialized v1 view. It cannot recreate v2 frontiers/conflicts/tombstones/quarantine or peer/outbox state, so normal and recovery imports both refuse once any v2 protocol state exists. |
| `project [--cwd]` | Build the compatibility projection. Session context uses `inject`, not this command. |
| `migrate [--apply] [--all-projects]` | Scan only the current logical project by default. `--all-projects` is the explicit cross-project/global recovery path required for runtime-memory cleanup; this command is not a live multi-server seed/cutover executor. |
| `migration <phase> … --epoch <id> [--json]` | Inspect or orchestrate the sealed existing-store protocol-v2 cutover. Artifact/ref/store mutations are dry-run by default and require both `--apply` and a matching `--expect` state digest. See “Existing-store migration” below. |
| `lifecycle [--apply]` | Apply working expiry and expose durable duplicate/capacity candidates. Pending delivery records remain protected. |
| `stats` | Print a grouped store snapshot. |
| `log [--limit 20] [--action] [--tier] [--actor] [--json]` | Read the bounded write-event timeline (D-38), complementing the `stats` snapshot. |
| `doctor` | Run bounded read-only local and v2 protocol checks covering integrity, schema/index invariants, pending/capacity/graveyard/dump consistency, outbox/peer/migration state, and worker health. Exit 0 is clean, 1 is WARN, and 2 is FAIL. |
| `inject [--hook]` | Build bounded SessionStart context from working, durable, and profile records. Defaults to 2,000 characters and 15 bullets; `--hook` emits `additionalContext` JSON. |
| `sync [--json]` | Absorb only current-project stray projection writes, rebuild indexes, and write the compatibility projection. With remote sync explicitly enabled, finalize/render/fetch/validate/integrate/fold/export/push/fresh-confirm immutable operations. `--json` emits the versioned status and phase outcomes described below. |
| `conflicts` | List bounded unresolved conflict identities without adopting a provisional body. |
| `show-conflict <id>` | Show every full concurrent variant with explicit labels. |
| `resolve <id> …` | Create a new agent-authored operation that descends every current maximal head; field-wise automatic semantic merge is forbidden. |
| `replica status [--json]` | Show the active replica counter and whether copied-state detection requires rotation; install-secret bytes are never printed. |
| `replica rotate --reason <text>` | Explicitly start a new replica-ID boundary after copying/moving local state. Existing operation IDs and predecessor history are preserved. |
| `maintenance [--squash-days 14] [--apply]` | Compatibility-only operator maintenance for a separately tracked legacy dump history. It is not v2 operation compaction and is never run or pushed by routine sync. |
| `distill <sid> [--advance]` | Print normalized transcript text after the shared session marker and optionally advance that marker. |
| `curate-snapshot` | Print a read-only current-project snapshot, mechanical signals, and destructive `IDS:` membership. Pending records appear under `PROTECTED PENDING` but never in destructive IDs. |
| `curate-artifacts` | Print read-only git, plan, and spec evidence for the curator agent. |
| `promote-candidates` | Print a bounded view of visible durable records for agent-owned institutionalization review. Type and strength are metadata, not semantic gates. |
| `reinforce <id>` | Increment strength and update access time within the current-project whitelist. |
| `merge --canonical <id> <ids…>` | Merge records into the canonical ID and graveyard the rest. Any pending member cancels the operation atomically. |
| `prune <id>` | Delete only after a successful `deleted-records.jsonl` backup. Pending records are rejected before consumption. |
| `delete <id> [--force]` | User-initiated single-record deletion. Pending records require prior consumption or explicit `--force`. |
| `graduate <id> [--to durable]` | Move a whitelisted working record to durable. |
| `reattribute <id>` | Reassign a true orphan to the current project without deleting it. Reverse gates reject live, global, profile, or self targets. |
| `supersede <old> --by <new>` | Preserve the older row as historical and route its canonical id to the newer active record. Cross-scope/project, pending, profile, and cycle cases fail closed. |
| `activate <id>` | Guardedly reactivate a historical row only when its successor is no longer active and no canonical ambiguity exists. |
| `register-postit <path>` | Deprecated legacy-migration-only registry command. Current post-its write DB working records directly. |

## Existing-store migration

`mem migration` is an operator protocol, not an automatic sync mode. It exposes
`status`, `inspect`, `capabilities`, two-seal `roster` operations, consistent
`snapshot`, deterministic `seed`, `fence`/`barrier`, captured `delta`,
`no-tail`, `fold`, `compare`, `activate`, and complete rollback-bundle phases. Use
`--json` for canonical machine output. Every mutating phase prints a
deterministic plan and writes nothing unless `--apply` is present; an applied
transition also requires `--expect` to match the current durable state digest.
`migration status` and `doctor --json` expose bounded membership/evidence
digests, each replica's latest sealed evidence phase, capture/outbox tail,
writer mode, equality timestamp/digest, rollback coverage, and the last applied
phase failure. They never include record bodies, credentials, or artifact paths.

A snapshot seed includes the complete captured causal closure present in the
snapshot, and every seeded operation must stay inside its author replica's
sealed logical-project keys. Seed counter reservation and durable operation
binding share one `BEGIN IMMEDIATE` transaction. A write after snapshot seal
but before that transaction fails seed construction with
`snapshot-tail-before-seed` before reserving counters or writing artifacts;
the operator must take a new checked snapshot rather than accept a partial
seed. Delta drain writes both the exact captured-delta manifest and its
`delta-seed` manifest, so publication, no-tail proof, and rollback inventory
refer to the same immutable operation bytes.

Snapshot creation also seals the exact local `deleted-records.jsonl` bytes and
a normalized `graveyard-source` manifest beneath the snapshot output. Seed
construction reads only that sealed copy. An absent graveyard entry never
creates a deletion; a proven entry for an ID absent from the snapshot produces
one complete prior-state operation followed by its exact tombstone. Existing
v2 transactional graveyard rows must already match tombstones in the snapshot's
captured causal closure. A store containing only deletion history and no live
records is therefore still migratable without inventing state.

Snapshots use SQLite backup artifacts in an explicit contained output
directory. They never exchange `memory.db`, WAL, or SHM files between servers.
After the old-writer fence is activated, unsupported and legacy writers fail
closed while reads remain available; protocol-v2 writes become available only
after equality is proven and the v2-only activation receipt is committed. A
production run still needs a separate operator plan naming the real roster,
backups, maintenance window, credentials, protected ref, rollback duration, and
human approvals. The test suite uses only temporary stores and local fake Git
repositories.

`rollback prepare` first establishes a durable writer barrier, then collects a
fresh protected-ref view plus every registered snapshot, snapshot/delta seed,
captured delta, no-tail proof, fence receipt, activation receipt, operation,
and normalized state section. It creates only a `complete=true` verified
bundle; a missing local artifact fails before bundle or rollback-identity
writes. `rollback export-v1` also requires that complete bundle and refuses a
lossy projection. `rollback apply` accepts only a self-digested local
`rollback-target-request` that binds the epoch, replica, absolute store,
verified projection, and install output. It durably seals a target manifest,
backs up the target, installs the projection in one guarded SQLite transaction,
and reuses the same target/install evidence after a crash. Each active replica
publishes its canonical apply receipt. `rollback close` requires exactly one
`--apply-receipt` for every active sealed replica, imports that receipt set in
the closing transaction, commits the complete bundle digest, and removes the
persistent old-writer triggers only from DB-issued closed rollback evidence.
The terminal state deliberately remains `writer_mode=fenced`: a verified old
v1 binary without the v2 guard can resume after trigger removal, while this v2
binary remains fail-closed until a new checked cutover.

## Protocol-v2 synchronization

Every supported semantic writer commits its local record/graveyard effect,
replica counter, exact canonical operation, `sync_applied(result=local)`, and
queued outbox row in one `BEGIN IMMEDIATE` transaction. The command succeeds at
that local durability boundary. An offline, rejected, or failed remote exchange
does not roll back the local write; later sync retries the same operation ID.
The outbox advances monotonically through
`queued → rendered → committed → confirmed`, where confirmation requires a
fresh fetch and protected-ref reachability rather than a push response or stale
remote-tracking ref.

The active replica is bound to a private installation marker at
`${XDG_STATE_HOME:-$HOME/.local/state}/hearting/memory-sync/installation-id`
plus local machine/store evidence. Copying `memory.db` to another server makes
semantic writes and remote sync fail closed until the operator runs
`mem replica rotate --reason …`; rotation never rewrites predecessor objects.

`MEM_SYNC_REMOTE=1` is the canonical opt-in. If it is unset and
`MEM_DUMP_PUSH=1`, sync enables the same immutable v2 exchange and emits one
bounded deprecation warning; this alias never pushes or overwrites
`dump.jsonl`. If both are unset or `0`, sync is local only. Remote enablement
also requires a store that is either provably fresh v2 or has a completed sealed
seed epoch, **and** a verifiably active v2-only old-writer fence in
`sync_migration_epoch`.
A nonempty legacy, partially seeded, or unfenced store exits 2 before render,
fold, push, confirmation, or watermark change. `mem migration` executes only
the checked local phase named by the operator, with sealed input manifests,
explicit `--apply`, and a matching `--expect` digest. It never discovers or
self-authorizes a live roster, credentials, protected ref, or all-server
rollout; those remain a separately reviewed operator procedure.

`mem sync --json` uses the versioned status enum
`not-configured|local-only|queued-offline|fetched|folded|conflict|quarantined|push-retry-exhausted|remote-confirmed|hard-failure`
with bounded phase outcomes and identifiers, never bodies, prompts, secrets, or
host paths. Its exit classes are:

| Exit | Meaning |
|---|---|
| `0` | Local state is healthy and remote sync is explicitly disabled with no obligation, or every current outbox operation is freshly confirmed; no warning remains. |
| `1` | Local writes are safe, but offline/deferred/conflict/quarantine/retry-exhausted work remains. |
| `2` | Local integrity/schema, immutable protocol, equivocation, remote rewind, unexpected path, or migration-safety failure. |

Concurrent live variants are preserved in full. A deterministic order may
select a provisional internal service projection but never resolves the
conflict: default injection, candidates, and recall exclude the conflicted body
and expose only bounded conflict identity/index evidence. Tombstones, restore,
supersession, merge removal, and pending consumption remain effective only when
their operation causally covers the required complete frontier. First-release
v2 operation/tombstone compaction and physical deletion are unsupported.

## Curator safety invariant (D-18/D-35/D-40)

The distiller model never invokes mutation commands directly. Automatic adds
must declare one of `decision`, `user-correction`, `unresolved-obligation`, or
`artifact-pointer`; the latter requires `artifact_refs` and must not duplicate
artifact prose. A no-tools worker
emits action JSON, and `tools/memory/apply-distill-actions.py` parses the shape,
checks snapshot membership, and calls `mem.py` with argv-only values. Each
command also enforces its own project whitelist. Pending records are protected
both in snapshot membership and through a transaction-time DB check. Prune,
merge, and delete retain recoverable graveyard data.

These safeguards validate operations; they do not decide meaning. Main agents,
distillers, and curators make contextual decisions about whether any action is
useful. Keyword lists, fixed phrases, content categories, record types, scores,
and confidence thresholds never substitute for that judgment.

## Retrieval boundary (D-40)

- `show`, explicit recall/full, and SessionStart injection do not consume a
  handoff. Explicit recall/show update `last_accessed` unless `--no-touch` is
  supplied. Source upsert/body dedup never lowers pending to ordinary.
- Prompt-submit hooks mechanically query only the capsule index on every
  eligible main prompt. They expose bounded candidates but never classify
  relevance or adopt a record. The agent ignores unrelated candidates and
  reads a relevant record in full before applying it.
- A successful candidate probe publishes a same-turn receipt, including a
  legitimate zero-hit result. Search errors publish no receipt. Main-session
  material mutation requires that opportunity; `mem recall-gate` is the
  explicit `recall`/`skip` recovery when the hook is unavailable. Registered
  route-bound workers are exempt from main-session memory lifecycle.
- FTS/BM25 ranking, CJK/identifier tokenization, scope fences, and limits
  organize candidates and explicit results; they do not decide relevance.
- Retrieval telemetry stores no raw prompt and distinguishes `candidate-probe`,
  `candidate-probe-error`, `explicit-recall`, `show`, `session-inject`, and
  `consume`.
- Telemetry defaults to
  `$XDG_STATE_HOME/agent-memory/recall-events.jsonl` (fallback:
  `~/.local/state/agent-memory/`) outside both the compatibility-dump location
  and immutable operation exchange.
  `MEM_RECALL_EVENTS` overrides the path.

## Write telemetry and diagnostics (Cluster J)

- Every mutation appends one bounded `write-events.jsonl` entry with
  `ts/action/id/tier/scope/type/actor/sid/cwd/snippet`. Rotation keeps at most the
  recent 500 lines within a 256 KiB bound. This local telemetry is not mirrored.
- Journal precedence is `MEM_WRITE_EVENTS`, then a path beside an overridden
  `MEM_STORE`, then `$XDG_STATE_HOME/agent-memory/write-events.jsonl`.
- Telemetry is fail-open; a logging failure never blocks a mutation. Graveyard
  recovery remains fail-closed because it protects destructive actions.
- Actor precedence is explicit `MEM_ACTOR`, distiller context, operation-specific
  defaults, then `manual`. Curator application sets `MEM_ACTOR=curator`.
- `mem log` is a timeline and complements rather than replaces `stats`.
- `mem doctor` is read-only. Its findings are evidence for an agent; it does not
  automatically consolidate, merge, graduate, or delete records.

## Environment overrides

- `MEM_STORE` controls both `memory.db` and `dump.jsonl` location.
- `MEM_SYNC_REMOTE=1` is the canonical explicit opt-in for immutable protocol-v2
  remote exchange. Unset or `0` keeps synchronization local.
- `MEM_DUMP_PUSH=1` is a deprecated compatibility alias used only when
  `MEM_SYNC_REMOTE` is unset. It selects v2 exchange with a bounded warning and
  never pushes or overwrites the compatibility dump.
- `MEM_SYNC_DIR` selects the absolute private dedicated exchange repository;
  the default is
  `${XDG_STATE_HOME:-$HOME/.local/state}/hearting/memory-sync/exchange`.
- `MEM_SYNC_REMOTE_URL` optionally selects its remote URL. Otherwise sync may
  derive `origin` from the compatibility-dump mirror repository.
- `MEM_SYNC_REF` selects the full protected v2 ref and defaults to
  `refs/heads/hearting-memory-v2`.
- `MEM_PROFILE` controls the human-readable profile export directory.
- `MEM_INJECT_MAX_CHARS`, `MEM_INJECT_MAX_BULLETS`,
  `MEM_INJECT_MAX_WORKING`, `MEM_INJECT_MAX_DURABLE`,
  `MEM_INJECT_CLEANUP_LINES`, and `MEM_INJECT_SNIPPET_CHARS` tune bounded
  injection budgets. Defaults are 2,000 characters, 15 bullets, 8 working,
  4 durable, 2 cleanup lines, and 100 characters per snippet.
- `MEM_DISTILL_ENABLE=1` enables background distillation. It is opt-in because
  it spends model capacity and sends potentially untrusted transcript data to
  a no-tools worker. Adapter-native settings own runtime enablement.
- `MEM_DISTILL=1` prevents recursive distillation lifecycle launches.
- `MEM_DISTILL_WORKER` selects an adapter-owned executable with contract
  `<worker> <mode> <model> <prompt-file>` and JSON-lines stdout.
- `MEM_DISTILL_MODEL` selects the portable model role; concrete defaults belong
  to adapter realization documents.
- `MEM_WRITE_EVENTS`, `MEM_ACTOR`, and `MEM_SID` override telemetry metadata.
- `mem-recall-inject.sh` is the fail-open prompt bridge for `mem candidates`.
  It exposes only active current-project/global capsule headlines and IDs (at
  most six, at most 2,400 UTF-8 bytes), never record bodies. A successful
  probe writes a same-turn receipt under `MEM_RECALL_RECEIPTS`; raw prompts are
  not written to telemetry or receipts. The model must inspect a relevant
  record in full before applying it. Registered worker sessions stay silent.
- `MEM_RECALL_EVENTS` and `MEM_RECALL_RECEIPTS` override the bounded telemetry
  and same-turn receipt locations for tests or private runtime projection.

## Operational contract

- Schema v7 adds bounded retrieval capsules, normalized topics, and non-destructive
  temporal supersession to the v6 record contract. The v27 schema-v8 migration
  adds local replica/outbox/applied/frontier/conflict/peer/quarantine tables;
  v28 schema v10 adds sealed migration receipts, the writer-fence contract,
  and bounded last-failure/status evidence without exchanging rebuildable indexes.
- `dump.jsonl` is ID-sorted with `sort_keys=True`, one record per line, and
  explicit JSON `null` values. It is a v1-compatible materialized view only;
  exact v2 recovery requires protected immutable objects plus a consistent
  local backup or a separate lossless v2 bundle.
- SessionStart injection may remain adapter opt-in when start events repeat on
  resume or compact. SessionEnd uses `mem sync`; adapters pass the user's remote
  opt-in environment unchanged, report the bounded sync exit class, and still
  run their bounded curator fallback before returning a nonzero sync status.
- `recall.sh` is a thin wrapper over explicit `mem recall`.
- `register-postit` and `.postit-roots` exist only for legacy Markdown migration.

`index-check.sh` remains a separate checker for the legacy
`projects/*/memory/MEMORY.md` text index. Store search indexes are owned by
`mem index` inside `memory.db`.
