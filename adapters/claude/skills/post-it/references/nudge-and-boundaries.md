## Proactive Nudge

post-it helps the agent continue the user's flow and surface things the user may have missed. Working-memory storage is automatic under the integrated-memory §7 write invariant and requires no confirmation. To preserve continuity, the main agent records working context when it semantically detects situations such as:

- **Context pressure around 50% or higher**, indicated by the status-line context bar, a long conversation, or approaching compaction → record working context and report one line.
- **A wind-down intent**, with quoted examples such as "오늘 여기까지", "내일 이어서", or an imminent `/clear` → record an automatic handoff containing in-progress work, decisions, and the next hint. These are examples, not fixed trigger phrases.
- **Completion of a coherent work unit** with a reusable thread or decision → automatically write `mem note`.

> **Automatic-recording model:** the user does not read post-it directly. Store working context automatically without confirmation. Automatic handoff includes a sweep that prunes only certain graduated/stale items, keeps ambiguous items, and reports one line. Confirm irreversible prune/delete operations; require line-level review only when the user invokes `/post-it sweep` directly.

> The runtime adapter bootstrap also carries a one-line semantic nudge because `SKILL.md` is loaded only after invocation. An optional PreCompact hook may provide a hard backstop, but a shell hook can emit only a fixed reminder; only the conversational agent can create an intelligent handoff summary.

## What This Skill Is Not

- **Not a replacement for automatic memory.** post-it is the human-editable working tier in the DB store, searchable through the same `mem recall` surface. Harness auto-memory mirrors `projects/*/memory/` into durable storage.
- **Not a permanent record.** Durable truth lives in artifacts, code, git, and structured DB `type=profile` records. Remove entries after graduation through `sweep` or `promote`.
- **Not a code/document change log.** Those belong under autopilot-code `plans/` or autopilot-draft `documents/`.
- **Not a session activity log.** That information already accumulates in `pipeline_summary.md` and related artifacts.

Store only the limited working context that a new session needs and that has not yet graduated into an artifact.

## Boundary with Auto-Memory

Both paths are separate views of one `memory.db` store.

| Path | Store location | Update |
|---|---|---|
| `<agent-home>/projects/*/memory/`, the harness auto-memory write surface | SessionEnd `mem sync --json` absorbs into local `memory.db`; optional remote sync exchanges immutable v2 operations and never pushes `dump.jsonl` | Harness automatic write → sync |
| DB working tier used by this skill through `mem note`/`mem add` | Direct local project-scoped `memory.db` records plus a compatibility-only `dump.jsonl` projection | Only through user invocation of `/post-it` |

Distinguish them as follows:

- Facts specific to this repository, such as a Notion location, dataset path, or current task → `--scope project`
- User identity or general work preferences, such as communication language or code style → durable auto-memory or `--scope user`

When they overlap, prefer the project working record because it is more precise local context. `mem recall` searches both surfaces together.

## Writing Style

Working records are always loaded into session context. Keep them short and dense.

- **One bullet per line**, at most two lines when unavoidable.
- **Noun phrase or factual sentence.** Minimize adjectives, asides, honorifics, and explanations.
  - ❌ `Overleaf 는 X 폴더 아래 정리하는 게 좋습니다. 이유는...`
  - ✅ `Overleaf 정리 위치: <Overleaf URL>/TF-Restormer`
- **Use only key terms.** Mixed-language identifiers, abbreviations, and symbols such as `→`, `&`, and `vs` are acceptable.
- **No category duplication.** Store the same information once.
- **Time notation:** no date for convention/reference; require `YYYY-MM-DD:` for decision; require `[in-progress YYYY-MM-DD]` or `[blocked YYYY-MM-DD]` for thread.
- **No meta-comments** such as "TODO 추후 확인"; represent them as a thread.
- Apply the same one-line rule to handoff summaries and sweep pointers.
