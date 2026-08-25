# Capability: analyze-user

This is the portable capability contract for `analyze-user`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `analyze-user` |
| Group | `pre` |
| Supported modes | `init, update` |
| Portable meaning | Create or update a cross-project user-preference profile from coding, writing, and analysis patterns. |
| Argument shape | `<aspect> [--source <path>] [--mode init\|update] [--from discover\|analyze\|verify\|qa\|output\|summary] [--user-refine]` |
| Entry load phase | `post-approval`; owner contract `capabilities/analyze-user.md` |

## Invocation Semantics

Scan and analyze the user's cross-project artifacts (papers, presentations,
reports, code, and memory) in stages, then accumulate general working
preferences in DB `type=profile` records (`mem profile <stem>`). This uses the
same ceremony level as autopilot entrypoints because every sub-agent treats the
resulting profile as a default, so even small errors propagate. The six phases
are source discovery, per-aspect analysis, cross-aspect consistency checks,
multiple QA gates, artifact creation, and pipeline summary. QA is always
adversarial and is not user-adjustable.

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
   <root> --route <route file> --capability analyze-user --intensity <intensity>`.
   While the cutover is inactive this returns `legacy-compat` and the legacy
   `<artifact-root>/user_profile/` layout stays writable; once active it
   issues `campaign_id`/`cycle_id`/`producer_id` and the cycle directory
   `campaigns/<camp>/cycles/<cyc>/artifacts/` before any artifact exists.
2. **write only inside the open cycle.** Every durable artifact goes under
   `<cycle_dir>/artifacts/user_profile/...` (`AGENT_ARTIFACT_OUTPUT_DIR`).
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

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- enforce git/worktree safety before edits;
- enforce artifact ordering before new durable artifacts;
- enforce spec-read gating when this capability changes spec-backed code or specs;
- use DB memory paths, not runtime-native memory files.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/analyze-user/SKILL.md` and `skills/analyze-user/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/analyze-user/SKILL.md`, while `skills/analyze-user/SKILL.md` remains the compatibility reference kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info analyze-user`. Use `adapters/codex/skills/analyze-user/SKILL.md` as the native Codex Skill projection; do not consume `skills/analyze-user/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info analyze-user`. Use `adapters/opencode/skills/analyze-user/SKILL.md` and `adapters/opencode/commands/analyze-user.md` as native OpenCode projections; do not consume `skills/analyze-user/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/analyze-user/SKILL.md` and `adapters/claude/skills/analyze-user/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/analyze-user/SKILL.md`, while `skills/analyze-user/SKILL.md` remains the compatibility reference kept for parity/drift checks.
