# Owner terminal settlement repair — 2026-09-13

A standard specification owner completed its declared review and returned an
exact native PASS, but public start kept advertising a running owner. The
transactional node belonged to the depth-1 owner, while closure required a
second worker's marker. The transaction had no such worker. An additional
producer check independently required a marker file even after shared terminal
proof succeeded.

## Ownership and changes

- `capability-route.terminal_gate_observation` now resolves the actual executor.
  Existing markers retain their validation. A terminal `capability-owner`
  operation without a marker consumes its bound owner's exact native PASS,
  readable artifact, complete external prerequisite proofs and quiescence. Its
  earlier owner operations share this executor; resource predecessors retain
  their resource marker contract without an invented agent attempt. No synthetic
  worker row or marker is written. The terminal claim binds this proof's digest.
- `artifact_lifecycle` consumes the common evidence digest instead of independently
  requiring a marker file. Existing marker-backed manifest digests remain stable.
- The executing owner receives missing prerequisite evidence before it exits.
  After committed success, the completion controller owns route/cycle/envelope
  settlement and replay. Result bytes remain unchanged; missing native evidence,
  conflicts, unfinished cleanup and changed artifacts retain their obligations.
- Public start refreshes the owner after joining. An exited owner is completed
  or needs attention, including when it exits during the observation. It is not
  advertised as running with a promise of another model turn.
- `dispatch_terminal_commit.py inspect --jobs <jobs> --attempt <attempt>` is a
  read-only diagnosis of current gates, cleanup, checkpoint and exact recovery
  command. `finish` uses the existing checked transaction and launches no model.
  Notices identify this responsibility without promising an unattended retry.

The same report exposed two separate admission problems:

- A native quota failure can disprove the availability estimate sealed before
  launch. The existing frame gate now allows two completed attempts on the
  remaining harness when every other eligible harness has an exact, settled
  quota failure on that route. It records the failed and accepted attempts in
  the existing degradation ledger. An unknown process, generic error, foreign
  route or later live attempt does not qualify. The user question/release remains.
- The final *plan* passed, but both independent *reviews* failed. The review cap
  correctly stopped another automatic review; its refusal omitted the existing
  owner-closure path. All three launch surfaces now share recovery guidance:
  resolve each finding with evidence and record owner judgment, or hand back the
  unresolved findings. Owner closure is explicitly distinct from independent PASS.
  No cap increase or new review/route policy was introduced.

## Verification

The isolated runner covered 13 related suites. The first run found a quick
marker regression, a writer-census expectation and a list-versus-text assertion;
all were corrected. The five affected suites passed their complete rerun, and
the final public-start race test passed a separate full-suite run. The other
eight suites passed initially, including one existing expected-failure case
that passed (`TestResourceLifecycle.test_sentinel_wrapper_persists_the_payload_exit_status`).
This is recorded as XPASS rather than a changed baseline.

Production route compilation, real marker writers, native terminal parsers,
the join/controller, route closure, producer manifest sealing and replay are
connected in the new specification-owner integration test for Claude, Codex and
OpenCode formats. It also checks absent native evidence, retained invalid
markers, read-only inspection and post-settlement artifact drift. Frame tests
cover native quota evidence, recorded fallback, live/unknown processes, foreign
routes, success rows and generic errors. Public-start tests cover delayed closure
and exit during the join, with no duplicate launch or false wait directive.
An additional resource→owner publish→owner sync case checks resource evidence
drift and the absence of any synthetic worker/owner row.

Initially the original incident was inspected read-only: the owner and its
review were settled, the new proof passed, and only its producer binding existed.
After the peer received the recovery commands, the original owner reached
`owner-envelope-sealed`, its workflow completed and its cycle sealed. The
installed helper reverified `completed / quiescent`. The four observed incident
rows retained their exact bytes. This observer ran no operating-state repair,
new model canary or product feature work. Private logs and their paths remain local,
not attached to this repository. Direct peer alias/ID lookup failed, but the
agent list resolved the exact thread to its pane; findings and recovery commands
were delivered with a verified state transition.

Local verification records: `/tmp/owner-terminal-settlement.tsv`,
`/tmp/owner-terminal-settlement-final.tsv`, `/tmp/owner-terminal-start-race.tsv`,
`/tmp/owner-terminal-settlement-boundary-final.log`,
`/tmp/homeos-hearting-stall-investigation.json`.

## Release and installation

Source `25e0b9c88d8561efe5c838fe525ea68e0662265a` is published as
[v2.140.2](https://github.com/dmlguq456/hearting/releases/tag/v2.140.2).
The [release run](https://github.com/dmlguq456/hearting/actions/runs/34727890189)
passed validation, deterministic packaging and the published-release smoke test.
An earlier run was deliberately cancelled before tag creation/publication to
include the resource-executor boundary correction.

Claude, Codex and OpenCode are installed at v2.140.2; strict runtime doctor is
fresh for all three and verification reports no drift. All 21 changed release
files match the source commit byte-for-byte, and the 11 snapshotted user settings
and authentication files are unchanged. The installed resource/owner boundary
test and read-only inspection of the recovered original owner both passed.
Existing sessions retain their original release pins; the exact new recovery
helper works against their existing canonical registry without another owner.

Installation evidence: `/tmp/owner-terminal-runtime-doctor.json`,
`/tmp/owner-terminal-runtime-verify.json`, `/tmp/owner-terminal-install-integrity.json`,
`/tmp/homeos-owner-terminal-installed-inspect.json`.
