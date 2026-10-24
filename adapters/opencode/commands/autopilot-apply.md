---
description: "Run the portable autopilot-apply capability through the OpenCode adapter. Meaning: Apply a cheatsheet draft to the real source artifact and verify the result."
---

Use the OpenCode adapter realization of portable capability `autopilot-apply`.
This is adapter-owned output generated from `capabilities/autopilot-apply.md`, not a runtime-specific command copy.

1. Read `capabilities/autopilot-apply.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-apply` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability autopilot-apply [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `"<cheatsheet hint / task>" [--target latex] [--source <path-to-real-source>] [--isolation branch|worktree] [--from preflight|apply|verify|handback]`.

Portable contract excerpt:

- Invocation semantics: Autopilot family — the document-side _apply + verify_ arm. Takes a draft-produced cheatsheet (a mutation/edit plan) and applies it to a real working source file _outside_ `<artifact-root>/` (e.g. the user's `main.tex`), under git, with a build/compile verify gate. This is the missing counterpart to autopilot-draft: draft _produces_ the cheatsheet (plan), autopilot-apply _executes_ it on the canonical source and _verifies_ it compiles — mirroring code-execute + code-test on the code side. Default target `latex` (latexmk compile gate + latexdiff rendered-diff review). Never touches the canonical source directly: applies on a git branch (or worktree), each mutation = one commit, hands back via `git merge`. Cheatsheet auto-discovered from `<artifact-root>/documents/*/draft/`. NOT for `<artifact-root>/` markdown artifacts (use autopilot-refine) or codebases (use autopilot-code). The preset is `apply → verify → handback`. The part catalog declares `start_approval: handback` on `autopilot-apply:handback` — the route-start card obtains approval for the selected handback part. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
