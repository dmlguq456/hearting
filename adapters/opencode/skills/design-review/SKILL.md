---
name: design-review
description: "Use only when autopilot-design dispatches design quality, token-contract, and breakage review. Not for top-level user requests or primary capability routing."
metadata:
  portable_source: capabilities/design-review.md
  adapter: opencode
  invocation_class: parent-invoked
---

# design-review

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/design-review.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info design-review`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/design-review.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info design-review`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `design-review`
- Invocation class: `parent-invoked`
- Supported modes: `none`
- Argument shape: `<design path or app path>`
- Portable meaning: Review design output for quality, token-contract compliance, and breakage.

## Portable Contract

- Invocation semantics: Visual review with two gates. First, a verifier in a separate context uses the adapter visual harness to screen for console errors, layout collapse, and intent mismatch; it must pass before critique. Second, a critic evaluates hierarchy, alignment, accessibility, responsiveness, UX flow, and tone. Both gates render through the adapter-provided visual harness and inspect the image. Read only; never auto-fix. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability design-review [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
