---
name: post-it
description: "Use when the acting agent needs to store, retrieve, resolve, hand off, or promote a scoped working-memory item in support of current work. Not for primary task routing, broad artifact triage, or replacing the capability that owns the current work."
---

# post-it

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/post-it.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info post-it`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Read `capabilities/post-it.md` for the runtime-neutral contract.
2. Run `adapters/codex/bin/preflight.sh capability-info post-it`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
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



## Projected Portable Details

## Artifact Ownership

Use the shared artifact root rule: prefer `.agent_reports/`; use legacy `.claude_reports/` only when it already exists and `.agent_reports/` does not. Capability-specific output placement follows `core/CONVENTIONS.md` section 5 until this spec is expanded with a stricter per-capability artifact map.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- enforce git/worktree safety before edits;
- enforce artifact ordering before new durable artifacts;
- enforce spec-read gating when this capability changes spec-backed code or specs;
- use DB memory paths, not runtime-native memory files.


## Required Guards

- Before edits: `adapters/codex/bin/preflight.sh write <file> [session-id]`
- Before capability grounding/spec-changing work: `adapters/codex/bin/preflight.sh route post-it [cwd] [session-id]`
- Before spec-changing work: `adapters/codex/bin/preflight.sh capability post-it [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/codex/bin/preflight.sh read <prd.md> [session-id]`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
