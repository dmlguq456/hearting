# Capability: draft-refine

This is the portable capability contract for `draft-refine`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `draft-refine` |
| Group | `sub` |
| Supported modes | `none` |
| Portable meaning | Refine a draft by applying memo/review feedback to a document strategy or draft. |
| Argument shape | `<strategy or draft name or path> [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial]` |

## Invocation Semantics

Reflect user memos/review feedback in a document strategy or draft. Before rewriting an existing file, obtain the route-bound `utilities/artifact-snapshot.py` receipt; it preserves exact prior bytes under `_internal/versions/v{N}/` (modern; per CONVENTIONS.md §5) or `_v{N}.md` siblings (legacy). The model never allocates or copies versions manually. Auto-managed `changelog:` array inside YAML frontmatter (NOT a top-of-file HTML comment — that breaks markdown preview when frontmatter is also present). Mandatory ref-grounding per memo (re-read source; override memo if it conflicts with source).

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

Artifact root: `core/CONVENTIONS.md §5.1`; output placement: `§5`.

## Artifact Producer Lifecycle

`draft-refine` is a `standard+` stage worker: it never issues its own campaign or
cycle. It receives the owner's open cycle through
`AGENT_ARTIFACT_CAMPAIGN_ID`/`AGENT_ARTIFACT_CYCLE_ID`/`AGENT_ARTIFACT_PRODUCER_ID`/
`AGENT_ARTIFACT_CYCLE_DIR`/`AGENT_ARTIFACT_OUTPUT_DIR` (dispatch env
pass-through); the resolver applies the selected node's declared outputs and
write scope while preserving the existing draft target.
`artifact_producer.py` owns the open cycle and its output paths; `finalize` and
`admit-shared` belong to the owner, never to a stage worker. See
`producer_lifecycle` in `capabilities/topologies.json`.

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- use DB memory paths, not runtime-native memory files.

Portable procedure and reference documents are owned under `capabilities/draft-refine/references/` when present. Claude, Codex, and OpenCode Skill bodies are generated projections of this capability and use the same logical procedure read-set.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/draft-refine/SKILL.md` and `skills/draft-refine/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/draft-refine/SKILL.md`, while `skills/draft-refine/SKILL.md` remains a generated compatibility projection sourced from `capabilities/` and kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info draft-refine`. Use `adapters/codex/skills/draft-refine/SKILL.md` as the native Codex Skill projection; do not consume `skills/draft-refine/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info draft-refine`. Use `adapters/opencode/skills/draft-refine/SKILL.md` and `adapters/opencode/commands/draft-refine.md` as native OpenCode projections; do not consume `skills/draft-refine/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/draft-refine/SKILL.md` and `adapters/claude/skills/draft-refine/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/draft-refine/SKILL.md`, while `skills/draft-refine/SKILL.md` remains a generated compatibility projection sourced from `capabilities/` and kept for parity/drift checks.
