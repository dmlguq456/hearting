# Capability: artifact-projection-read

This is the portable, read-only Hearting boundary for Cairn artifact projection
queries. Cairn W3a remains the owner of request, cursor, authorization,
dereference, and closed-error semantics.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `artifact-projection-read` |
| Group | `support` |
| Supported modes | `none` |
| Portable meaning | Read-only lookup of Cairn artifact projections through the canonical W3a contract. |
| Argument shape | `one JSON read request on stdin` |

## Invocation Semantics

Run `utilities/cairn-artifact-read.sh` with exactly one JSON object on stdin.
The command emits exactly one JSON value on stdout and diagnostics only on
stderr. Set `CAIRN_ROOT` to the checkout containing W3a commit
`1fa0d99e4b714b5ce305f78c8f7c7773255e8f87`, provide the read-only
`CAIRN_READ_ENDPOINT`, and set `CAIRN_READ_TOKEN` only when the service
requires it. The request is validated by W3a's
`validateReadOptions`, then passed to W3a's `ArtifactProjectionClient` and
`HttpReadTransport` without local query, cursor, or authorization
interpretation. The token is never printed.

This capability is intentionally narrower than browse/search fallback paths.
It has no write, ingest, apply, activate, deactivate, migrate, or namespace
switch operation and never accepts a database URL. Missing or mismatched W3a
sources fail closed using W3a's imported contract errors.

If the W3a query fails, Hearting does not take ownership of a fallback; the caller may use the separately owned browse/search or memory paths.

Hearting may add a separate `hearting_verification_display` field to a
successful read response. It joins only exact stable IDs and locally verified
artifact revisions, and it preserves every W3a request, cursor, authorization,
row, and error result. Verification verdict, operational completion, and
required-input observation remain distinct. Missing or stale local evidence
is displayed as unresolved while the successful remote read stays successful.
This field is a read-time projection: it does not create a new input, gate,
service, verification round, or durable sidecar, and it never changes source
report or verdict bytes.

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

Portable procedure and reference documents are owned under `capabilities/artifact-projection-read/references/` when present. Claude, Codex, and OpenCode Skill bodies are generated projections of this capability and use the same logical procedure read-set.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/artifact-projection-read/SKILL.md` and `skills/artifact-projection-read/SKILL.md` are generated sibling/compatibility projections of this portable source. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info artifact-projection-read`. Use `adapters/codex/skills/artifact-projection-read/SKILL.md` as the native Codex Skill projection. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info artifact-projection-read`. Use `adapters/opencode/skills/artifact-projection-read/SKILL.md` and `adapters/opencode/commands/artifact-projection-read.md` as the native OpenCode projections. |
