---
description: "Run the portable autopilot-refine capability through the OpenCode adapter. Meaning: Correct and update existing document/research artifacts while preserving snapshots and change history."
---

Use the OpenCode adapter realization of portable capability `autopilot-refine`.
This is adapter-owned output generated from `capabilities/autopilot-refine.md`, not a runtime-specific command copy.

1. Read `capabilities/autopilot-refine.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-refine` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before edits, run `adapters/opencode/bin/preflight.sh write <file> [session-id]`.
4. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability autopilot-refine [cwd] [session-id]`.
5. If the command receives arguments, map them to the portable argument shape:
   `"<prompt>" [--intensity direct|quick|standard|strong|thorough|adversarial] [--review-only | --memo <file>] [--confirm] [--no-fact-check] [--no-style-audit]`.

Portable contract excerpt:

- Invocation semantics: Autopilot family — post-creation iteration pipeline for research and doc artifacts (NOT code). Prompt-driven: target artifact identified via prompt fuzzy match against `<artifact-root>/{research,documents}/*`, then auto-discovers the artifact's file structure, plans edits, shows a diff preview in chat, and on user confirm applies edits with versioning + integrated history logging in `pipeline_summary.md` (single source of truth — no separate CHANGELOG). Default intensity is `quick` (one registered conductor). Its sealed `inline_human_gates` declares `preview-disposition`, bound at the one-shot terminal boundary and enforced before any target-artifact write. The conductor writes `reviews/refine/preview.md`, raises and awaits the gate, then applies within the same attempt only after the person releases it; escalate intensity to `standard|strong|thorough|adversarial` for multi-round review, fact-check, or external adversary work. Optional `--memo <file>` falls back to file-memo style for deferred reviews. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
