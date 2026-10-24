---
description: "Run the portable draft-refine capability through the OpenCode adapter. Meaning: Refine a draft by applying memo/review feedback to a document strategy or draft."
---

Use the OpenCode adapter realization of portable capability `draft-refine`.
This is adapter-owned output generated from `capabilities/draft-refine.md`, not a runtime-specific command copy.

1. Read `capabilities/draft-refine.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info draft-refine` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability draft-refine [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<strategy or draft name or path> [--intensity direct|quick|standard|strong|thorough|adversarial]`.

Portable contract excerpt:

- Invocation semantics: Reflect user memos/review feedback in a document strategy or draft. Before rewriting an existing file, obtain the route-bound `utilities/artifact-snapshot.py` receipt; it preserves exact prior bytes under `_internal/versions/v{N}/` (modern; per CONVENTIONS.md §5) or `_v{N}.md` siblings (legacy). The model never allocates or copies versions manually. Auto-managed `changelog:` array inside YAML frontmatter (NOT a top-of-file HTML comment — that breaks markdown preview when frontmatter is also present). Mandatory ref-grounding per memo (re-read source; override memo if it conflicts with source). Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
