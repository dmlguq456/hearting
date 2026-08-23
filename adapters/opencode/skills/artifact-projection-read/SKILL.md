---
name: artifact-projection-read
description: "Use when an agent or operator needs to browse or query an existing Cairn artifact projection without changing artifacts or runtime state. Not for primary task routing, ingest, mutation, activation, deactivation, apply, migration, namespace switching, or flat-browse fallback ownership."
metadata:
  portable_source: capabilities/artifact-projection-read.md
  adapter: opencode
  invocation_class: model-support
---

# artifact-projection-read

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/artifact-projection-read.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info artifact-projection-read`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

1. Read `capabilities/artifact-projection-read.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info artifact-projection-read`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `artifact-projection-read`
- Invocation class: `model-support`
- Supported modes: `none`
- Argument shape: `one JSON read request on stdin`
- Portable meaning: Read-only lookup of Cairn artifact projections through the canonical W3a contract.

## Portable Contract

- Invocation semantics: Run `utilities/cairn-artifact-read.sh` with exactly one JSON object on stdin. The command emits exactly one JSON value on stdout and diagnostics only on stderr. Set `CAIRN_ROOT` to the checkout containing W3a commit `1fa0d99e4b714b5ce305f78c8f7c7773255e8f87`; set `CAIRN_READ_TOKEN` only when the read service requires it. The request is validated by W3a's `validateReadOptions`, then passed to W3a's `ArtifactProjectionClient` and `HttpReadTransport` without local query, cursor, or authorization interpretation. The token is never printed. This capability is intentionally narrower than browse/search fallback paths. It has no write, ingest, apply, activate, deactivate, migrate, or namespace switch operation and never accepts a database URL. Missing or mismatched W3a sources fail closed using W3a's imported contract errors.


## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`

- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability artifact-projection-read [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
