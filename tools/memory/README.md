# tools/memory — Unified Memory System (`mem`)

Portable storage and retrieval layer. The specification lives at
`<artifact-root>/spec/prd.md` (`.agent_reports` first, with legacy
`.claude_reports` compatibility).

## Boundary

Short-lived post-its, durable learned memory, and the global profile share one
SQLite store. The acting agent decides contextually what to store, retrieve,
promote, merge, or prune. Code owns mechanical integrity, scope isolation,
pending protection, lifecycle execution, bounded telemetry, and recovery.
`memory.db` is the source of truth; `dump.jsonl` is its text mirror.

## Storage layout

| Layer | Location | Git | Purpose |
|---|---|---|---|
| Source of truth | `${XDG_DATA_HOME:-~/.local/share}/hearting/memory/memory.db` (SQLite WAL; existing `<agent-home>/memory` remains compatible) | ignored binary | `records`, body/CJK FTS, retrieval-capsule FTS, and normalized topic index |
| Git mirror | `<memory-store>/dump.jsonl` (one ID-sorted record per line) | tracked in the memory repository or linked to it | deterministic text export and exact `mem import` recovery source |
| Harness projection | `<agent-home>/projects/<cwd>/memory/` | ignored | compatibility surface for stray auto-memory writes absorbed by `mem sync`; `mem project` can rebuild the projection |

`memory.db` and the mirror checkout may be split across filesystems. Keep the
live store on a local filesystem, place the `agent-memory` checkout elsewhere,
and make `<memory-store>/dump.jsonl` a symlink to that checkout's tracked
`dump.jsonl`. Export, commit, push, doctor, and maintenance follow the mirror
target without replacing the symlink; the SQLite WAL files remain local.

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
| `candidates "<prompt>" --session-id <id> [--turn-id <id>] [--hook]` | Main-prompt mechanical capsule lookup. Exposes at most three active current-project/global headline-and-ID candidates within 1,200 UTF-8 bytes, never bodies, and publishes a same-turn opportunity receipt on a successful probe. |
| `recall-gate --decision recall\|skip --reason … [--query …]` | Record the work-start opportunity decision without raw prompts; recall executes immediately. Applied outcomes require `--gate-id` and at least one `--record-id`; miss has no record ID. |
| `recall "<query>" [--topic] [--include-superseded] …` | Search active capsules first, then body/CJK/LIKE compatibility paths. Historical rows require explicit inclusion. |
| `topics [topic] [--include-superseded]` | List normalized topics or visible records for one exact topic. |
| `show <id> [--all] [--include-superseded]` | Show one visible record with capsule, temporal metadata, and full body. |
| `consume <id>` | Move a pending handoff/thread to consumed. Retrieval and injection never consume records implicitly. |
| `restore <id>` | Restore one record from the graveyard while preserving action/canonical metadata. |
| `index [--rebuild]` | Rebuild the FTS5 tables embedded in `memory.db`. |
| `export [--target dump\|profile] [--apply]` | Export `dump.jsonl` or an on-demand human-readable profile cache. Profile export is dry-run unless `--apply` is supplied. |
| `import <dump.jsonl>` | Recreate the DB exactly from a dump: delete existing records, replay the mirror, and rebuild FTS in the same connection. |
| `project [--cwd]` | Build the compatibility projection. Session context uses `inject`, not this command. |
| `migrate [--apply] [--all-projects]` | Scan only the current logical project by default. `--all-projects` is the explicit cross-project/global recovery path and is required for runtime-memory cleanup. |
| `lifecycle [--apply]` | Apply working expiry and expose durable duplicate/capacity candidates. Pending delivery records remain protected. |
| `stats` | Print a grouped store snapshot. |
| `log [--limit 20] [--action] [--tier] [--actor] [--json]` | Read the bounded write-event timeline (D-38), complementing the `stats` snapshot. |
| `doctor` | Run nine read-only checks covering integrity, FTS/schema invariants, working growth, stale pending, durable capacity, graveyard/dump consistency, and worker health. Exit 0 is clean, 1 is WARN, and 2 is FAIL. |
| `inject [--hook]` | Build bounded SessionStart context from working, durable, and profile records. Defaults to 2,000 characters and 15 bullets; `--hook` emits `additionalContext` JSON. |
| `sync` | Absorb only current-project stray projection writes, rebuild indexes, export, and append the bounded mirror commit. |
| `maintenance [--squash-days 14] [--apply]` | Operator-run compaction for the plain-commit dump history: squash first-parent auto-sync commits older than N days into one root, then `git gc`. Dry-run by default; never pushes (a mirror needs an explicit force-push afterwards). |
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
  `~/.local/state/agent-memory/`) outside the memory Git mirror.
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
  most three, at most 1,200 UTF-8 bytes), never record bodies. A successful
  probe writes a same-turn receipt under `MEM_RECALL_RECEIPTS`; raw prompts are
  not written to telemetry or receipts. The model must inspect a relevant
  record in full before applying it. Registered worker sessions stay silent.
- `MEM_RECALL_EVENTS` and `MEM_RECALL_RECEIPTS` override the bounded telemetry
  and same-turn receipt locations for tests or private runtime projection.

## Operational contract

- Schema v7 adds bounded retrieval capsules, normalized topics, and non-destructive
  temporal supersession to the v6 record contract.
- `dump.jsonl` is ID-sorted with `sort_keys=True`, one record per line, and
  explicit JSON `null` values. `mem import dump.jsonl` performs exact recovery.
- SessionStart injection may remain adapter opt-in when start events repeat on
  resume or compact. SessionEnd uses `mem sync`; optional distillation exits
  early on empty delta and spawns detached so it does not block lifecycle hooks.
- `recall.sh` is a thin wrapper over explicit `mem recall`.
- `register-postit` and `.postit-roots` exist only for legacy Markdown migration.

`index-check.sh` remains a separate checker for the legacy
`projects/*/memory/MEMORY.md` text index. Store search indexes are owned by
`mem index` inside `memory.db`.
