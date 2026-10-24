---
description: "Run the portable autopilot-design capability through the OpenCode adapter. Meaning: Visual-design pipeline coordinating references→tokens→components→review→handoff."
---

Use the OpenCode adapter realization of portable capability `autopilot-design`.
This is adapter-owned output generated from `capabilities/autopilot-design.md`, not a runtime-specific command copy.

1. Read `capabilities/autopilot-design.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-design` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability autopilot-design [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<design task or app path> [--scope ui|webapp|slide|icon|diagram|mixed] [--artifact standalone|project] [--from <phase>] [--intensity direct|quick|standard|strong|thorough|adversarial]`.

Portable contract excerpt:

- Invocation semantics: Unified design pipeline — orchestrates design-init → design-refs → design-tokens → design-components → design-review → design-handoff. For visual artifacts across UI/UX, slides, diagrams, icons, logos. Can be invoked standalone or auto-delegated from autopilot-spec Phase 2. Distinct from autopilot-draft (text-only documents) — autopilot-design handles visual deliverables. A runtime design harness must render every output for visual self-verification (preview/screenshot/console/eval_js/view_image where supported), run a separate-context verifier gate for console/layout breakage, apply shared design rules and reusable scaffold assets, and support PDF/PPTX/single-HTML bundle export where available. Outputs can be a self-contained single-file HTML preview viewable without any project stack. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
