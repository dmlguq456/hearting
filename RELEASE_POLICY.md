# Release Policy

Hearting releases are immutable delivery points, not snapshots of every
documentation change. Merging to `main` and completing Checks do not publish a
release. An input-free `workflow_dispatch` request on `main` prepares one
release for that exact commit; until a request is made, the current latest
release remains available. A manual run on an existing `v*` tag ref remains
available for retrying or resuming that tag's workflow.

## Version decision

The planner compares the requested commit with the newest published stable
release reachable from it. Unpublished tags do not define the change range or
release notes. Every existing stable tag name remains protected against
collision, while the automatic version floor stays inside the published
baseline's major version.

| Change since the published stable release | Automatic release |
|---|---|
| `type!:` or a `BREAKING CHANGE:` footer in a release-relevant commit | next minor (`major-held` is recorded in the reason and notes) |
| `feat:` in a release-relevant commit | next minor |
| Any other release-relevant behavior change | next patch |
| Documentation, reports, fixtures, and tests only | none |

Major versions are opened only by an explicit stable tag push, permanently.
This includes the first public `v4.0.0`; automatic requests do not start a new
major after it is published. After a stable v4 release, automatic patch and
minor versions remain within v4, and breaking changes remain minor. An orphan
or other-branch higher-major tag cannot change that rule. Tags and existing
release assets are never moved or replaced.

Release-relevant paths include the portable contract, capabilities, roles,
runtime adapters, hooks, installers, generators, and operational tools. Public
documentation, internal reports, CI configuration, research, and test-only
paths do not create a release by themselves. An unclassified commit that
changes a release-relevant path receives a patch release rather than being
silently skipped.

## Request, validation, and publication

Every push to `main` still runs `Checks`, but no Checks completion starts a
release. A maintainer requests a bundle with:

```bash
gh workflow run release.yml --repo dmlguq456/hearting --ref main
```

The admission job looks for up to 20 exact-SHA `main` push runs of the existing
Checks workflow only for a manual request on `main`. A result is reused only
when its workflow name and path,
event, branch, completion and success state, full `head_sha`,
`head_commit.id`, and repository and head repository names and IDs all match.
The existing main Checks policy may skip expensive suites for documentation
only changes or a previously validated identical tree; a successful run still
has the same admission authority as before. The gate does not inspect artifacts
or execute candidate code.

If no trusted result exists, or the Actions API is missing, malformed, or
unavailable, the request runs the existing reusable Checks workflow on the
same requested SHA. Invalid repository, event, ref, or SHA identity is denied
before lookup. Tag pushes and manual runs on `v*` tag refs always run the full
reusable Checks workflow; tag runs never reuse main Checks. The admission job
has only `contents: read` and `actions: read`; write access stays in the
serialized release job.

The planner refreshes tags, finds the published stable baseline, computes all
release relevant changes since that tag, then reserves an annotated version
before building assets. API failures or the absence of a published baseline
stop planning; they never silently change the notes range. A failed Checks
lookup is different: it falls back to full validation to preserve availability.
The release notes start at the actual prior published stable tag, including
for explicit tags and resumed reservations. A reserved version is reused only
for the exact same source SHA. A conflicting tag is left untouched and causes
the planner to refresh and recalculate.

The workflow builds the four release assets from the admitted source SHA,
publishes with `--verify-tag` and generated notes, then invokes the existing
post publish smoke test with that exact version. A version-tag run validates
and uses the version already selected by the ref; it does not reinterpret or
recalculate a human-chosen version. Existing prerelease tag pushes, such as
`v1.2.0-rc.1`, continue to publish as prereleases through this same tag path;
this preserves existing SemVer behavior and adds no separate rc workflow or
release path. Consumers keep the existing
`latest` and exact `--version` installation behavior, checksums, and pinned
runtime roots.

For the first public release, complete the existing public readiness work
before a maintainer pushes `v4.0.0` at the prepared exact `main` commit. The
tag path validates the chosen commit and version. It works even if that same
commit has already been published under v3 or contains only documentation.
A manual run on that existing version-tag ref follows the same tag validation
and full Checks path, for retry or resume.

After an input free request, observe that workflow run and its summary, verify
the exact published release metadata, then update and verify the local install
using the actual version. A dispatch being accepted is not proof of
publication. For example:

```bash
hearting update --version <published-version> --yes
timeout 300 hearting verify
```

## Checks scope

Public Markdown documentation and the project's root `AGENTS.md` / `CLAUDE.md`
run generated surface and contract checks without the full runtime, installation,
and PID namespace suites. Main compares all changes since its latest published
stable release, so a documentation push cannot hide earlier unvalidated code.
Pull requests compare against their merge base. Runtime instructions under
`core/`, `capabilities/`, `roles/` or adapters, tests, CI configuration, unknown
paths, and unavailable comparisons still run all checks. The release request
reuses a trusted successful main Checks result where possible; otherwise the
reusable workflow runs the existing full validation.

## Local planner checks

Use an explicit published baseline for offline planning, or set
`GITHUB_REPOSITORY` and authenticate `gh` to resolve the published baseline
through the API:

```bash
python3 tools/release/plan.py plan --repo . --head HEAD --base-tag v3.43.6
./tools/release/plan.test.sh
```
