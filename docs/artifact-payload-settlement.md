# Artifact payload settlement and observer diagnostics

A code owner and its report completed successfully, but cycle finalization failed
on `artifacts/evidence/visual/test-results/.last-run.json`. Collection accepted
the file while manifest validation rejected every dot-prefixed component. The
completed work remained at the `route-closed` checkpoint.

## Shared responsibility

The common manifest validator now accepts dot-prefixed names inside the exact
cycle-relative `artifacts/` namespace. The producer uses that same path validator
before reading payload bytes. The separate, more permissive producer regex has
been removed. Invalid paths identify their exact locator and reason early.
Traversal segments, absolute paths, control characters, symlinks, reserved names
and files outside the payload remain rejected. Cycle control names retain their
separate contract. This is a CORE §3 correction to D-6's hidden-name restriction
for payloads; it does not rewrite immutable historical spec revisions or relax
legacy relocation policy.

Dot-prefixed output is preserved at its original path and included in the normal
manifest with its byte count and content digest. There is no Playwright-specific
exception, automatic exclusion, move, deletion or new operator switch. The old
retrospective `exclude_hidden` API remains limited to its existing explicit callers.

The existing completion controller still owns proof checks, route closure,
producer finalization, envelope publication and replay. A file-name restriction
can no longer strand this valid payload. A crash after manifest publication is
recovered by the same exact transaction; later content drift blocks consumption
without overwriting the committed manifest or prior PASS.

## Separate observer incident

An active owner's join subprocess failed and the shared controller published
`join-observer-failed`. Read-only observation confirmed the owner, child and
replacement join process were alive. The original controller subsequently
reconciled that exact child and resumed the same owner at 13:06:56Z on September
13, without intervention from this repair session.

Both supervisor implementations discarded the join's structured error reason
and emitted only `join-process-contract-failed`. That historical reason cannot
be reconstructed. A shared formatter now preserves the exit code and bounded
typed reason only for an error receipt bound to the exact parent. Arbitrary text,
wrong identities and malformed receipts are not relayed. Claude and OpenCode
share the session supervisor; Codex uses the App Server supervisor. All retain
the same existing wait controller, notice and observation retry responsibility.
No new worker retry, cancellation or process-death inference was added.

## Verification and recovery

Twelve distinct relevant suites passed. Two supervisor fixture failures also
reproduced on unchanged main: their whole-module stub lacked the current executor
helpers. They now stub only the marker observation and retain the real route
module. Both complete suites then passed.

Coverage includes the three owner-harness bindings through real terminal
settlement, a crash after manifest publication, exact replay, preserved original
bytes, post-seal dotfile drift, path escape, hidden symlinks and early invalid-path
diagnostics. Real failing join subprocesses feed the shared wait loop and recover
on its next observation while preserving registry bytes. Foreign/raw error
receipts are rejected. These are process/fixture tests, not new model canaries.

The reported cycle was inspected without writes: its 46 payload files passed the
new validator; the old validator rejected only `.last-run.json`. All 47 original
files, including the cycle binding, retained their digests. Private logs and
product evidence remain local.

After installing the fixed release, existing pinned owners use the new helper
against their existing canonical jobs path:

```sh
python3 <new-release>/utilities/dispatch_terminal_commit.py inspect --jobs <canonical-jobs> --attempt <exact-owner>
python3 <new-release>/utilities/dispatch_terminal_commit.py finish --jobs <canonical-jobs> --attempt <exact-owner>
```

`finish` retains all current identity, terminal-gate, cleanup, lease and content
checks. The repair session does not rerun product work or force operating records.
Older pinned validators still reject newly supported payload names; installed
consumers and the explicitly selected recovery helper must use the fixed release.

## Released and observed

Source `e9a597d1ae9eb136c6fbb86c5f599d74411646ad` is published as
[v2.141.1](https://github.com/dmlguq456/hearting/releases/tag/v2.141.1).
The [release run](https://github.com/dmlguq456/hearting/actions/runs/34759227029)
passed validation, packaging and the published-release smoke test. All three
local runtimes are installed, strict doctor reports fresh, and verify reports
no drift. The 13 changed release files match the source commit; the 11 observed
user settings/authentication files are unchanged. Installed locator, transaction
recovery and real join-error subprocess tests passed.

The product owner then ran the supplied normal exact `finish` command. Independent
read-only checks confirmed `completed / quiescent / owner-envelope-sealed`, an
unchanged report evidence digest, all 47 original files unchanged, and all 46
payloads (including `.last-run.json`) present with matching sizes and hashes.
Manifest, producer record and admission-index digests agree; installed exact
cycle verification returns `already-sealed`. Product work was not rerun. The
observer session performed no operating-record repair.

Local verification records: `/tmp/hidden-final-verification.json`,
`/tmp/homeapp-hidden-settlement-observation.json`,
`/tmp/homeapp-hidden-installed-inspect.json`, `/tmp/hidden-installed-integrity.json`,
`/tmp/hidden-runtime-doctor.json`, `/tmp/hidden-runtime-verify.json`.
