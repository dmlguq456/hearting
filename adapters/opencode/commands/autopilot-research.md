---
description: "Run the portable autopilot-research capability through the OpenCode adapter. Meaning: Shared upfront research that surveys academic, technology, or market sources before downstream routing."
---

Use the OpenCode adapter realization of portable capability `autopilot-research`.
This is adapter-owned output generated from `capabilities/autopilot-research.md`, not a runtime-specific command copy.

1. Read `capabilities/autopilot-research.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-research` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability autopilot-research [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<query> [--mode academic|technology|market] [--depth shallow|medium|deep] [--intensity direct|quick|standard|strong|thorough|adversarial] [--no-clarify] [--no-figures] [--from search|analyze|report]`.

Portable contract excerpt:

- Invocation semantics: Shared research-survey entrypoint with three modes: academic (papers, trends, and field mapping), technology (libraries, projects, stacks, and code baselines), and market (market/competitor/reference-app/UX patterns). Downstream routing: academic → autopilot-draft for papers/presentations and autopilot-code for academic baselines; technology → autopilot-code for library or research implementation and autopilot-spec for stack/reference decisions; market → autopilot-draft for proposals/reports and autopilot-spec for reference-app UX. This capability produces field intelligence only; downstream skills create actual documents, code, or applications. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
