# post-it

> This README summarizes the portable capability for users and maintainers. The model-neutral contract lives under `<agent-home>/capabilities/`; `SKILL.md` in this directory provides shared guidance for runtime-specific projections.

## Overview

A temporary sticky-note memory surface under explicit user control. It is separate from automatic memory under `<agent-home>/projects/*/memory/` and changes only when the user explicitly invokes `/post-it`. It bridges working context that would otherwise disappear between sessions.

**Core metaphor: a sticky note.** post-it is not a permanent record. Durable truth lives in artifacts (`plans/`, `documents/`, `spec/`, code, and git) or structured DB profile records (`type=profile`). post-it is the temporary working surface between them; remove an entry after it graduates into a durable artifact.

> **Invariant: the user does not read post-it directly.** It is an agent-facing continuity surface, not user documentation. The agent keeps it lean and prunes it, reporting only a one-line summary to the user.

## Lifecycle

Every entry eventually graduates or expires.

| State | Meaning | Handling |
|---|---|---|
| **graduated** | Permanently represented in an artifact or structured section | Expire with `sweep` or `promote` |
| **stale** | Old `[in-progress]` item or completed hint | Expire with `sweep` or `resolve` |
| **live** | Still valid and present only in a working record | Keep |

## Scope — Project vs User

| Scope | Storage | Update path |
|---|---|---|
| `project` (default) | cwd-scoped DB working tier via `mem note`/`mem add` | `/post-it`; `mem inject` loads DB working records into the session |
| `user <aspect>` | Durable DB profile record via `mem profile <stem>` / `mem add ... --source user-profile:<stem>`, inside the exact legacy block `## 사용자 수동 메모` | `/post-it --scope user <aspect>`; persistent manual channel between analyze-user runs, loaded by `mem profile` |

## Five Record Types

| Category | `type` value | Example content |
|---|---|---|
| Conventions | `convention` | Durable conventions such as a Notion location or commit-message language |
| External Resources | `reference` | External links or paths such as datasets or Overleaf |
| Open Threads | `thread` | `[in-progress YYYY-MM-DD]` current work |
| Decisions | `decision` | `YYYY-MM-DD:` decision and rationale |
| Next Session Hints | `hint` | Progress, next action, and cautions needed by the next session |

## Sub-Actions

| Command | Action | Confirmation |
|---|---|---|
| `/post-it` or `show` | Preview DB working records with `mem recall --tier working`; suggest `add` if none exist | None |
| `/post-it add <category> <text>` | Write `mem note "<text>" --type <type>`. `category` is one of convention, resource, thread, decision, with aliases conv/res/th/dec. Automatically prefix threads with `[in-progress YYYY-MM-DD]` and decisions with `YYYY-MM-DD:` | Immediate; use `--confirm` to preview |
| `/post-it resolve <hint>` | Fuzzy-match a thread record and delete deterministically with `mem delete <id>` | Preview → confirm; override with `--no-confirm` |
| `/post-it decide <text>` | Write `mem note "<YYYY-MM-DD: text>" --type decision` | Immediate; use `--confirm` to preview |
| `/post-it sweep` | Compare working records with `plans`, `documents`, `spec`, and git; flag or expire graduated/stale entries | Manual call: preview → confirm. Automatic nudge: prune only certain matches and report one line |
| `/post-it promote [--scope user <aspect>]` | Graduate an item from `## 사용자 수동 메모` into a structured profile section with read-modify-write: `mem profile` → splice → `mem add ... --source user-profile:<stem>` | Preview → confirm |
| `/post-it handoff [--no-confirm]` | Sweep first, review the conversation, create 5–10 bullets, then write them with `mem note --type hint` | Preview → confirm; override with `--no-confirm` |

## Confirmation Principle

Apply user-authored text immediately for `add` and `decide`. Review agent-generated, matched, classified, or graduated content for `resolve`, `sweep`, `promote`, and `handoff`. Because the user does not read post-it directly, an automatic nudge may prune only certain entries and report one line rather than forcing line-by-line review.

## Concision Rule

Working records are always loaded into session context. Keep them short and dense.

- One bullet per line, at most two lines. Prefer noun phrases and factual sentences; use abbreviations and symbols such as `→`, `&`, and `vs`.
- Store each fact in only one category. Threads require a `[status YYYY-MM-DD]` prefix; decisions require `YYYY-MM-DD:`.

## What This Skill Is Not

- **Not a replacement for automatic memory.** post-it is the human-editable working tier in the DB store, searchable through the same `mem recall` surface. Harness auto-memory mirrors `projects/*/memory/` into durable storage.
- **Not a permanent record.** Artifacts, code, git, and DB `type=profile` records are durable truth. Remove a sticky note after graduation.
- **Not a code/document change log.** Those belong under autopilot-code `plans/` or autopilot-draft `documents/`.

## Boundary with Auto-Memory

Both paths are separate views of one `memory.db` store.

| Path | Store location | Update |
|---|---|---|
| `<agent-home>/projects/*/memory/`, the harness auto-memory write surface | SessionEnd `mem sync --json` absorbs into the local durable store; remote v2 exchange remains explicit opt-in | Harness automatic write → sync |
| DB working tier used by this skill through `mem note`/`mem add` | Direct local `memory.db` records plus a compatibility-only `dump.jsonl` projection | Only through user invocation of `/post-it` |

Use `--scope project` for facts specific to the current repository. Use durable auto-memory or `--scope user` with `mem profile <stem>` for cross-project user/work preferences. `mem recall` searches both surfaces together.

---
*Portable capability contract: `<agent-home>/capabilities/post-it.md`; shared skill guidance: `<agent-home>/skills/post-it/SKILL.md`.*
