---
description: "Run the portable artifact-projection-read capability through the OpenCode adapter. Meaning: Read-only lookup of Cairn artifact projections through the canonical W3a contract."
---

Use the OpenCode adapter realization of portable capability `artifact-projection-read`.
This is adapter-owned output generated from `capabilities/artifact-projection-read.md`, not a runtime-specific command copy.

1. Read `capabilities/artifact-projection-read.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info artifact-projection-read` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before edits, run `adapters/opencode/bin/preflight.sh write <file> [session-id]`.
4. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability artifact-projection-read [cwd] [session-id]`.
5. If the command receives arguments, map them to the portable argument shape:
   `one JSON read request on stdin`.

Portable contract excerpt:

- Invocation semantics: Run `utilities/cairn-artifact-read.sh` with exactly one JSON object on stdin. The command emits exactly one JSON value on stdout and diagnostics only on stderr. Set `CAIRN_ROOT` to the checkout containing W3a commit `1fa0d99e4b714b5ce305f78c8f7c7773255e8f87`; set `CAIRN_READ_TOKEN` only when the read service requires it. The request is validated by W3a's `validateReadOptions`, then passed to W3a's `ArtifactProjectionClient` and `HttpReadTransport` without local query, cursor, or authorization interpretation. The token is never printed. This capability is intentionally narrower than browse/search fallback paths. It has no write, ingest, apply, activate, deactivate, migrate, or namespace switch operation and never accepts a database URL. Missing or mismatched W3a sources fail closed using W3a's imported contract errors.


User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
