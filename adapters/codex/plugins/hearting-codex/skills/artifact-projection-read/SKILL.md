---
name: artifact-projection-read
description: "Use when an agent or operator needs to browse or query an existing Cairn artifact projection without changing artifacts or runtime state. Not for primary task routing, ingest, mutation, activation, deactivation, apply, migration, namespace switching, or flat-browse fallback ownership."
---

# artifact-projection-read

This is a Codex-native Skill projection generated from the portable capability
contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/artifact-projection-read.md`
- Runtime check: `adapters/codex/bin/preflight.sh capability-info artifact-projection-read`
- Bootstrap: `adapters/codex/AGENTS.md`

## Use

1. Read `capabilities/artifact-projection-read.md` for the runtime-neutral contract.
2. Run `adapters/codex/bin/preflight.sh capability-info artifact-projection-read`.
3. Obey the reported status:
   - `instruction-only`: use this Skill as Codex guidance plus explicit preflight guards.
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



## Projected Portable Details

## Artifact Ownership

The command is read-only and owns no artifact mutation. Generated runtime
projections must be produced from this portable file and the manifest. The
Hearting bridge imports W3a `request.ts`, `client.ts`, and `errors.ts`; it must
not copy their request fields, error names, retry policy, or exit table.

## Role Requirements

The acting agent may select and invoke this read-only support capability while
another capability remains the primary owner of the task. It must not broaden
the request, interpret projection results as permission to mutate, or treat a
failed projection read as ownership of the external fallback paths.

## Guard Requirements

- Accept no command option, database URL, write credential, mutation key, or
  namespace activation/switch operation.
- Emit exactly one JSON object on stdout for success and failure. Any process
  diagnostic belongs only on stderr and must not contain a token, endpoint
  credential, database URL, or response body.
- Treat the imported 17-code W3a error order as the sole exit mapping: the
  corresponding exits are injective `2..18`.
- Verify that the required W3a D-41 correction commit is an ancestor of the selected Cairn
  checkout before importing its runtime modules.
- When W3a modules cannot be imported, fail closed with the public D-41
  `INTERNAL_FAILURE` code and exit `18`; never expose private bootstrap codes.

## Portable Procedure

1. Pipe one read request object to `utilities/cairn-artifact-read.sh`.
2. Parse the single stdout object. On a non-zero exit, use its typed
   `error.code`; do not infer meaning from stderr prose.
3. When the projection is unavailable or rejected, retain flat browse, `rg`,
   and memory `artifact-pointer` recall as additive external fallbacks. This
   capability does not own or replace those paths.


## Required Guards

- Before edits: `adapters/codex/bin/preflight.sh write <file> [session-id]`
- Before capability grounding/spec-changing work: `adapters/codex/bin/preflight.sh route artifact-projection-read [cwd] [session-id]`
- Before spec-changing work: `adapters/codex/bin/preflight.sh capability artifact-projection-read [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/codex/bin/preflight.sh read <prd.md> [session-id]`
- For workflow state: `adapters/codex/bin/preflight.sh status [cwd] [session-id]` and `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as Codex-native source. Those files are compatibility/reference surfaces only.
