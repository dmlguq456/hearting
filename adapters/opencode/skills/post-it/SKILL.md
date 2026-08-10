---
name: post-it
description: "Use when the acting agent needs to store, retrieve, resolve, hand off, or promote a scoped working-memory item in support of current work. Not for primary task routing, broad artifact triage, or replacing the capability that owns the current work."
metadata:
  portable_source: capabilities/post-it.md
  adapter: opencode
  invocation_class: model-support
---

# post-it

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/post-it.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info post-it`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/post-it.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info post-it`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `post-it`
- Invocation class: `model-support`
- Supported modes: `none`
- Argument shape: `[show] | add <category> <text> | resolve <hint> | decide <text> | handoff [--no-confirm] | sweep [--no-confirm] | promote [<hint>] [--scope project|user [<aspect>]]`
- Portable meaning: Store project/cross-project notes and handoffs in working memory.

## Portable Contract

- Invocation semantics: Manually-controlled working-memory layer, two scopes. `--scope project` (default): `mem note`/`mem add` (working tier, per-cwd) — thread/decision/convention/reference records in DB. `--scope user <aspect>`: `mem add` (durable, global, profile-adjacent) — splices a note into the `## 사용자 수동 메모` block of the profile record (`source user-profile:<stem>`), shared with analyze-user. All entries are designed to graduate (into artifacts/profiles) or expire — `sweep` flags stale working records; `promote` graduates user notes into the profile record. DB working tier is injected at session start by `mem inject` (not a file read). Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability post-it [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
