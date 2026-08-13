# Memory — Unified Store (canonical)

> Split from `CONVENTIONS.md` on 2026-06-23. Memory is an independent subsystem. Preserve the §7 numbering. This is the single source; the spec is `<artifact-root>/spec/prd.md` (`.agent_reports` first, with legacy `.claude_reports` compatibility), and the implementation is `tools/memory/mem.py`.

## §7. Unified Memory System

> Three former memory surfaces—short-lived post-its, durable learned memory, and the global user profile—share **one portable store**. The store is infrastructure, not a semantic policy engine. **The agent decides contextually what is worth storing, retrieving, promoting, merging, or pruning.** Deterministic code owns only mechanical concerns such as schema validity, scope isolation, lifecycle execution, pending protection, bounded I/O, telemetry, and recovery. Behavioral rules belong in the runtime bootstrap, `CONVENTIONS`, `WORKFLOW`, or Skills rather than memory.

### §7.0. Store Architecture

- **Store:** `<agent-home>/memory/memory.db` is the SQLite WAL source of truth with built-in FTS5. `dump.jsonl` is its deterministic, git-tracked text mirror. Keep both in a dedicated private memory repository; `memory/` is gitignored in the configuration repository at `<agent-home>`. Schema v7 adds the retrieval capsule (`headline`, `aliases`, `entities`, `topics`, `artifact_refs`, `canonical_id`) and temporal state (`status=active|superseded`, `superseded_by`) beside the original body. The normalized topic table and capsule FTS index are derived and rebuildable. Restore with `mem import dump.jsonl`.
- **Tier and scope:** the DB is the sole source of truth; file surfaces are on-demand views.

  | Channel | Store tier/scope | Synchronization |
  |---|---|---|
  | `post-it`, a DB working-tier alias authored by the `/post-it` Skill | working/project | `/post-it` → `mem note` or `mem add` → SessionEnd `mem sync` |
  | `projects/<cwd>/memory/`, built-in file memory whose direct writes are hard-blocked by `builtin-memory-guard.sh` | durable/project | SessionEnd `mem sync` absorbs only the current logical project's stray writes as a safety net; an explicit `mem migrate --all-projects` is the recovery/import path |
  | DB records with `type=profile`, the cross-project profile source of truth | durable/global | `analyze-user` → `mem add` → `mem sync`; `user_profile/*.md` is an on-demand `mem export` cache for human reading, not a source of truth |

- **Harness integration:** automatic memory lifecycle belongs only to the interactive, user-facing main session (D-42). `mem inject --hook` may expose a bounded working, durable, and profile summary at main SessionStart; adapters whose start event repeats on resume, clear, or compact may keep this opt-in. Each eligible main-session prompt also runs a deterministic candidate probe over the active current-project/global capsule index. The probe exposes at most six headline-and-ID candidates within 2,400 UTF-8 bytes; it never reads bodies, touches access dates, widens scope, or decides relevance. Main SessionEnd runs current-project `mem sync` plus bounded `MEM_DUMP_PUSH=1` mirroring under D-31. Main SessionEnd and turn-counter triggers may launch a no-tools distiller agent through `mem-distill-dispatch.sh`; automatic additions must declare exactly one storage purpose: `decision`, `user-correction`, `unresolved-obligation`, or `artifact-pointer`. Artifact-backed knowledge is represented by a pointer and retrieval reason, not a duplicate artifact summary. Registered dispatches, loops, title workers, distillers/curators, and native subagents are worker sessions: they never run the prompt candidate probe, inject memory automatically, advance distill counters, sync on exit, or launch a curator. Every automatic model-backed distill path retains the D-41 bounded-worker controls. `builtin-memory-guard.sh` keeps DB writes on the unified `mem` path. Hook registration remains adapter-native.
- **Recall:** `mem candidates` is a mechanical prompt-time capsule lookup; the acting model ignores unrelated candidates and reads the full record before applying a relevant one. `mem recall` is the explicit deeper search over active capsules followed by body/CJK compatibility indexes, with optional exact `--topic`; historical rows require `--include-superseded`. If the ID is already known, use `mem show <id>`. A successful prompt probe writes a same-turn recall-opportunity receipt even for zero hits. Main-session material mutations require that receipt; a failed/missing hook recovers through an explicit contextual `recall` or `skip` decision in `mem recall-gate`. Registered route-bound workers are exempt because they do not own the main memory lifecycle.
- **CLI:** Core surfaces include `recall-gate`, `topics`, `supersede`, and guarded `activate` in addition to the existing record, lifecycle, export/import, and curator commands. `supersede <old> --by <new>` keeps the old row recoverable and removes it from default recall/injection; it never deletes either body. `migrate` is current-project by default and requires `--all-projects` for an explicit cross-project recovery scan.
- **Periodic curation (opt-in, default off):** where sessions are long-lived, SessionEnd fires too rarely to keep durable tiers under their soft ceiling. `utilities/mem-periodic-curate.sh` is an optional nightly backstop gated on `MEM_PERIODIC_CURATE_ENABLE=1`; unset is a complete no-op, following the `MEM_DISTILL_ENABLE` precedent. It is a **single cron firing point** that runs one `periodic-curate` dispatch per eligible project **sequentially** inside the ordinary D-41 slots and start budget. A session-event fan-out is forbidden here — that structure caused the v18 216-worker incident. The script refuses to run inside a worker, registered, or dispatch-child context, so cron cannot route around D-42, and `periodic-curate` mode never advances distill markers. SessionEnd curate remains the backstop rather than being replaced.
- **Curator commands under D-18:** `curate-snapshot` exposes active project records and a destructive allowlist. The existing guarded mutations remain, and `supersede` may relate two allowlisted active records without deleting history. Profile, pending-delivery, cross-scope, cross-project, nonexistent, and cyclic relations fail closed. The no-tools distiller emits action JSON only; scripts invoke validated argv with `shell=False`.
- **Invariant D-18, D-35, D-40:** the acting agent—main, distiller, or curator—makes semantic memory decisions from available context. Scripts may validate action shape, visibility, identity, transaction safety, pending protection, graveyard recovery, and bounded lifecycle mechanics. They must not decide relevance through keyword lists, content categories, confidence thresholds, or fixed phrases. Unconsumed pending handoffs fail closed against destructive operations until an explicit `consume` transition.

Sections §7.1–§7.3 define the semantic/mechanical mutation boundary; §7.4 defines agent-initiated retrieval.

### §7.1. Semantic Decisions Belong to the Agent

There is no deterministic promote/skip classifier. The acting agent judges whether a memory operation is useful in context, including whether information is durable, non-obvious, recoverable elsewhere, stale, or merely ephemeral. For automatic distillation, the four storage-purpose labels are a bounded ingress contract after that semantic judgment, not a keyword classifier: code validates the declared purpose but never infers it from content. Manual user-directed memory remains governed by its owning capability.

### §7.2. Write Mechanics and Deduplication

- After deciding to write, inspect visible memory and prefer updating the canonical record over creating an obvious duplicate. Fuzzy similarity is candidate evidence for the agent, not an automatic semantic verdict.
- Prefer artifact pointers over replicated artifact prose. A pointer includes the current artifact path and only enough body text to explain why and when it should be retrieved.
- Populate a concise headline plus bounded aliases, entities, and topics. These form the retrieval capsule and topic index; the full body remains evidence read on demand.
- A manual `mem add` should pass `--headline`/`--alias`/`--entity`/`--topic` explicitly: an empty capsule is not retrievable through capsule-first recall or prompt candidates. `write_record` mechanically extracts file paths, commit hashes, backtick identifiers, and ID-shaped tokens (e.g. `D-40`, `rt-*`) from the body into `entities` as a merge-only, non-semantic safety net (D-40 boundary — extraction, not judgment); it does not replace populating `--headline`/`--alias`/`--topic` by hand. When a write arrives with no capsule fields at all and nothing was mechanically extracted, `mem add`/`mem note` print one advisory stderr line — this never blocks the write.
- Preserve changed decisions with `supersede` so default reads see one active canonical path while historical rows remain auditable.
- After determining that memory is stale, use the guarded remove or merge path so recovery metadata is preserved.
- Related records may use `[[name]]` links in the DB `links` column. FTS5 indexing happens mechanically on insert; `MEMORY.md` remains a legacy projection view.
- Code may report capacity pressure and similarity candidates. The curator decides whether and how to consolidate them.

### §7.3. Agent-Backed Mutation Boundary

Purely deterministic monitors may surface candidates but cannot promote, skip, merge, or prune based on semantic rules. A user-directed post-it flow or an agent-backed distiller or curator may perform the mutation. The script then enforces only the mechanical action contract and recovery boundary.

#### D-43 — On-call incident-to-proposal bridge

An agent-backed on-call loop may use bounded recent memory mutation events only
as incident leads. It must read any selected record in full and corroborate the
claim against current source, tests, logs, artifacts, or runtime evidence
before sending it to the offline improvement proposal inbox. Memory remains
unchanged and is never sufficient evidence by itself.

The acting agent chooses one stable incident identity. Deterministic proposal
code may compare that identity exactly, append bounded recurrence evidence
under the inbox lock, and fail closed on ambiguous duplicates; it must not infer
semantic equivalence. A named automated collector may create `observed` and
advance only through `reproduced` to `proposed`. It cannot change a reviewed or
terminal decision, impersonate a human approver, edit source or runtime state,
or activate a realization. This bounded task operation is not automatic memory
lifecycle: the on-call worker does not inject, curate, sync, consume, or mutate
memory, preserving D-42.

### §7.4. Recall — On-Demand Retrieval

`mem inject` may provide a bounded SessionStart summary of active working, durable, and profile records. On each eligible main prompt, `mem candidates` mechanically searches only the active current-project/global capsule FTS index and exposes at most six headline-and-ID candidates within 2,400 UTF-8 bytes. The agent decides whether any candidate is relevant; a relevant candidate must be read in full before use. The probe never reads or falls back to bodies, touches records, stores raw prompts, or treats a lexical hit as adopted truth. Its same-turn receipt proves only that retrieval was not silently omitted. When a prompt hook is unavailable or fails, record `recall` with a focused query or `skip` with a short contextual reason through `mem recall-gate`. Retrieval is information access, not handoff consumption. Bounded telemetry stores query/turn digests, result IDs and counts, output bytes, or the explicit gate decision and later `applied|miss` outcome without storing raw prompts.

| Helper | Purpose | Notes |
|---|---|---|
| `python3 <agent-home>/tools/memory/mem.py candidates <prompt> --session-id <id> [--turn-id <id>] [--hook]` | Expose bounded active capsule candidates and publish a same-turn recall-opportunity receipt. | Main prompt bridges own this mechanical call. Maximum six candidates and 2,400 UTF-8 bytes; no body fallback or access-date touch. |
| `python3 <agent-home>/tools/memory/mem.py recall-gate --decision recall|skip --reason <reason> [--query <query>]` | Record the work-start opportunity decision; `recall` immediately searches active current-project/global memory. | After a hit, use `--outcome applied --gate-id <id> --record-id <id>`; miss uses no record ID. No raw prompt is stored. |
| `tools/memory/recall.sh "<query>" [--full] [--limit 1..100] [--all] [--sessions]` | Capsule-first FTS5 recall with body/CJK fallback. | Default status is active; `mem recall` additionally supports `--topic` and explicit `--include-superseded`. |
| `python3 <agent-home>/tools/memory/mem.py show <id> [--all]` | Print metadata and the exact multiline body for a known ID. | Default visibility is active current project plus global; historical access requires `--include-superseded`. |
| `python3 <agent-home>/tools/memory/mem.py topics [topic]` | List the active normalized topic index or records under one topic. | This is a navigation index, not an automatic relevance classifier. |
| `tools/memory/index-check.sh [dir] [--fix]` | Check drift in the legacy `MEMORY.md` text index under `projects/<cwd>/memory/`, including missing and orphaned pointers. `--fix` appends missing pointers only. | The built-in FTS5 index in `memory.db` is separately owned by `mem index`. Preserve existing curated lines. |

There are two retrieval surfaces. First, curated memory in durable and working tiers is searched by `mem recall` through SQLite FTS5 with BM25 ranking, falling back to LIKE or `rg`. A shortened or ellipsized snippet is never final evidence: if a hit is truncated or insufficient for the judgment, immediately read its full body by record ID with `show <id>` or use `recall --full --limit N`. Direct SQLite or `dump.jsonl` queries are not a normal retrieval path. Second, `--sessions` searches raw historical transcripts that have not been distilled into memory. Raw sessions are noisy and should supplement curated memory only when useful.

**Agent-owned adoption:** deterministic candidate exposure prevents the search step from being silently omitted, but the agent alone decides whether prior context may materially improve the current judgment. No fixed signal words, mandatory topic list, prompt classifier, category-to-recall rule, or score threshold may adopt a candidate. Ignore unrelated candidates. Before using a candidate, read its full record; widen to historical, cross-project, body, or raw-session data only when useful, and cross-check retrieved claims against current code or artifacts because memory can be stale.

- **Pointer follow-through:** when a recall result names a project file or
  artifact path relevant to the task, read that current target before using
  the memory claim to report project state. Memory supplies continuity and
  navigation; the referenced artifact and live code supply current facts. A
  missing or changed target is evidence that the memory pointer may be stale,
  not a reason to substitute the remembered summary for the file.
- **Inline recall:** for a simple agent-chosen search, run `tools/memory/recall.sh "<query>"` directly for one or two queries. If a hit needs context, follow with `mem show <id>` or `recall --full --limit N`.
- **Memory-scout capability:** use this read-only capability for deep searches across raw sessions, multiple query angles, many full bodies, or multiple working directories. Start with narrow recall and useful synonyms in any relevant language; inspect hit IDs through `show` or a few full results; widen to `--all` on a miss, then to raw sessions; finish with one line cross-checking live code. Do not bypass the interface through direct DB or `dump.jsonl` reads. Return at most 15 lines: verdict—present, absent, or ambiguous—up to three key quotations, record IDs, and one application instruction. All writes are forbidden, including `add`, `note`, `consume`, `restore`, `delete`, `reinforce`, `merge`, `prune`, or file edits.

Per-cwd isolation remains the default. Use `--all` only for an explicit cross-project need. A mass `--fix` touches gitignored live user data under `projects/` and therefore belongs in a user-directed flow; automated paths may report missing pointers but not repair them.

### §7.5. Mechanical Scaffold — Retrieval, Curation Candidates, and Pending Protection

Deterministic code may detect mechanical conditions and expose candidates around lifecycle operations; the deep curator decides semantic actions. Scripts execute only validated actions under D-18 and D-40.

**D-40 agent-owned semantic memory judgment, superseding the automatic portions of D-15 and D-34:**

- Prompt-submit bridges run a bounded capsule-only candidate probe for every eligible main prompt. They do not run a semantic classifier, read record bodies, or inject score-threshold-qualified truth.
- `mem candidates` owns only tokenization, active/current-project-or-global scope fences, ranking, and output limits. At most six headline-and-ID candidates and 2,400 UTF-8 bytes are exposed; no hit is an adoption decision.
- `mem recall` and `tools/memory/recall.sh` remain explicit deeper retrieval tools. `mem recall-gate` is the recovery path when a prompt probe is unavailable and may record a contextual `recall` or `skip`; neither path classifies the prompt deterministically.
- `mem recall --auto` remains retired. `hooks/mem-recall-inject.sh` is now the fail-open portable candidate bridge registered by each active runtime adapter.
- `$XDG_STATE_HOME/agent-memory/recall-events.jsonl` remains bounded raw-prompt-free telemetry. `$XDG_STATE_HOME/agent-memory/recall-opportunities/` stores one atomic bounded receipt per hashed session. A search error creates no valid receipt; zero legitimate hits do.
- Main-session material mutations require a current same-turn receipt or an explicit recall-gate recovery receipt. Registered route-bound workers are exempt and continue to skip main-session memory lifecycle.

**D-16 cleanup signals in `mem inject`:** after the existing `mem inject --hook` block, add a `## 🧹 Cleanup signals` section only when nonempty. It may show cwd-scoped durable near-duplicate groups, capacity over `durable > soft_ceiling=80`, and working records within three days of expiration. The section remains inside the SessionStart cap and defaults to two lines. It is read-only and performs no deletes or flag writes. Under D-18 it is informational: main performs no housekeeping. The session-end deep curator reads `curate-snapshot` and its signals, emits action JSON, and `mem-distill-dispatch.sh` validates and executes it through an allowlist with `shell=False`. This moved deletion from main under D-17 to the session-end curator under D-18 because deep-role context plus graveyard/dump recovery makes the worst likely outcome inefficiency rather than loss.

**D-35 unconsumed handoff and thread protection:**

- `delivery_state` is `ordinary`, `pending`, or `consumed`. New `type=handoff` records and explicit `--requires-consume` threads are pending. Only `mem consume <id>` performs the normal pending-to-consumed transition. Source upsert and body dedup preserve pending monotonically rather than lowering it through an ordinary rewrite. `show`, recall/full, and inject are non-consuming. When retrieval exposes `[pending:<id>]`, read the full obligation, apply and verify it, then call `mem consume <id>`. A working pending record does not expire before consumption; its 21-day TTL starts over when consumed.
- `curate-snapshot` shows pending records under `PROTECTED PENDING` but excludes them from destructive `IDS:`. `prune`, `delete`, `merge`, and `lifecycle --apply` recheck DB state immediately before execution and fail closed on pending records. A merge containing any pending record aborts entirely without changing strength or deleting anything.
- Deleted records remain in the graveyard with action and canonical metadata and can be restored one at a time through `mem restore <id>`. Automatic consumption is allowed only in narrow pipeline or post-it paths that name the handoff ID and prove successful application through artifacts.

### §7.6. User Profiles — Aspect-to-Consumer Matrix

> Moved from `user_profile/README.md` on 2026-06-23 when the mapping document was removed. **Read profiles from the DB** through `mem profile <stem>` or `python3 <agent-home>/tools/memory/mem.py profile <stem>`. This matrix maps agents to aspects; the aspect body source of truth is the durable/global DB record with `type=profile`. This section is the single source for the matrix. `capabilities/analyze-user.md` and every adapter-native `analyze-user` projection must reference it; root `skills/analyze-user/SKILL.md` is only a compatibility reference.

| Stem | Domain | Consumers |
|---|---|---|
| `01_paper_figure_style` | Paper figures, tables, color, fonts, size, and metric grouping | material, design, research for figure citation style, and editorial roles |
| `02_paper_writing_style` | Paper tone, argumentation, and citation | research, editorial, and planning roles |
| `03_presentation_strategy` | Slide structure, narrative flow, visual decisions, and audience adaptation | material for presentations, design, and editorial roles |
| `04_analysis_methodology` | Data and experiment analysis approaches and verification patterns | material, research, planning, implementation for metrics and verification, editorial, and the main agent for analytical replies |
| `05_domain_expertise` | Domain background such as speech, TF DNN, and signal processing, plus terminology preferences | research, material, design, editorial, planning for abbreviations, implementation for identifiers, and the main agent for recognizing user terminology |
| `07_coding_convention` | Project layout, config, prefixes, preferred layers and frameworks, metric sets, logs and checkpoints, seeds and reproducibility, and naming | implementation, planning for code plans, and the main agent during `autopilot-lab` Step 0, `autopilot-spec` Phases 0 and 2, and the four `autopilot-code` principles |

Aspect 06, conversational meta rules, is excluded because the runtime adapter response discipline is its single source and applies only to the main agent; subagents do not speak directly to the user. The `06_collaboration_style` record remains the default collaboration target for `/post-it --scope user`. Aspect 07 applies only to implementation, planning, and main-agent code work, not editorial wording. Each agent normally consults three to five relevant aspects.

**Update protocol:** profile bodies live in durable/global DB records. Two flows may update them. `/analyze-user <aspect>` scans prior artifacts such as papers, presentations, code, and reports, extracts patterns, and accumulates them with `mem add durable profile --source user-profile:<stem>` during setup, new-material ingestion, or incremental `--mode update`. `/post-it --scope user <aspect>` adds a generally useful pattern discovered in conversation.

**Source-keyed upsert hazard (data loss):** `mem add ... --source user-profile:<stem>` upserts by `(tier, scope, source)` and REPLACES the entire profile body with the payload. Never pass a partial body through raw `mem add` to "append" one item to a profile — everything not in the payload is destroyed. Interactive single-item additions go through `/post-it --scope user <aspect>`; body rewrites go through `/analyze-user`, which reads the current body via `mem profile <stem>`, splices while preserving `## 사용자 수동 메모`, and only then writes the complete replacement.

**Consumption pattern:** at the start of relevant work, an agent reads each aspect through `mem profile <stem>` and treats the body as a default unless the user says otherwise in the current turn. Per-project conventions in `analysis_project/code/experiment_conventions.md` take priority; profiles are the cross-project fallback. For example, a material figure task reads aspects 01, 03, 04, and 05, while a new-library implementation reads project conventions first and then aspects 07, 04, and 05.
