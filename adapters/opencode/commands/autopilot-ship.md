---
description: "Run the portable autopilot-ship capability through the OpenCode adapter. Meaning: Prepare deployment/release setup or package existing artifacts."
---

Use the OpenCode adapter realization of portable capability `autopilot-ship`.
This is adapter-owned output generated from `capabilities/autopilot-ship.md`, not a runtime-specific command copy.

1. Read `capabilities/autopilot-ship.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info autopilot-ship` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability autopilot-ship [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `<task description (optional)> [--mode default|package] [--intensity direct|quick|standard|strong|thorough|adversarial]`.

Portable contract excerpt:

- Invocation semantics: Two delivery purposes share this entrypoint. Omitted mode and `--mode default` retain the existing deployment/release path. `--mode package` collects existing artifacts into a delivery archive and uses its own minimal recipe below; it does not enter the deployment procedure. Default mode is an application deployment-setup entrypoint for projects with an existing `spec/` and substantially complete functionality. Guide the first ship setup, environment, domain, and migration deployment; select hosting (Vercel, Fly, Railway, Cloudflare, or EAS); create CI/CD files, `.env.example`, domain guidance, and a deployment record. The route card obtains approval for the selected `deploy` part at start. Within that approved scope, the owner may run deployment and verification; otherwise leave `deploy` out for a later compose. Keep it distinct from autopilot-spec's initial spec/skeleton work. It may be rerun for environment changes, added domains, or production migration deployment. Package mode reuses the existing delivery template, explicitly named model versions, requested input/output list, and existing reports. Collect only that scope, check file presence, relative links, version consistency and archive contents, then provide the ZIP or requested equivalent. An application spec, hosting choice, CI/CD setup, deployment, installation, security/release review, training/evaluation, or report rewrite is not implied by packaging. Reuse sufficient results first. If sample outputs are missing, the necessary inference belongs to lab/eval and stays limited to the requested samples; do not silently expand to earlier datasets, alternative/old models, or full benchmark runs. A request for received field samples plus one or two simulations does not authorize raw59, A-weight96, IPC, or historical-model sweeps. A later approval to finish a 167-item batch is local to that batch, not a new default. Reusable packaging-program implementation belongs to code; new experiment analysis/reporting to lab; independent document drafting to draft. Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
