---
name: draft-refine
description: "Use only when autopilot-draft or autopilot-refine dispatches an internal strategy or draft refinement stage. Not for top-level user requests or primary capability routing."
metadata:
  portable_source: capabilities/draft-refine.md
  adapter: opencode
  invocation_class: parent-invoked
---

# draft-refine

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/draft-refine.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info draft-refine`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/draft-refine.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info draft-refine`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `draft-refine`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<strategy or draft name or path> [--intensity direct|quick|standard|strong|thorough|adversarial]`
- Portable meaning: Refine a draft by applying memo/review feedback to a document strategy or draft.

## Portable Contract

- Invocation semantics: Reflect user memos/review feedback in a document strategy or draft. Before rewriting an existing file, obtain the route-bound `utilities/artifact-snapshot.py` receipt; it preserves exact prior bytes under `_internal/versions/v{N}/` (modern; per CONVENTIONS.md §5) or `_v{N}.md` siblings (legacy). The model never allocates or copies versions manually. Auto-managed `changelog:` array inside YAML frontmatter (NOT a top-of-file HTML comment — that breaks markdown preview when frontmatter is also present). Mandatory ref-grounding per memo (re-read source; override memo if it conflicts with source). Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability draft-refine [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
