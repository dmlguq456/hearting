# Release Policy

Hearting releases are immutable delivery points, not snapshots of every
documentation change. A release is created only when `main` changes something
that can alter the installed harness or its runtime behavior.

## Version decision

The release planner compares `main` with the highest stable
`vMAJOR.MINOR.PATCH` tag reachable from it and applies the highest matching
SemVer change:

| Change since the stable tag | Release |
|---|---|
| `type!:` or a `BREAKING CHANGE:` footer in a release-relevant commit | major |
| `feat:` in a release-relevant commit | minor |
| Any other release-relevant behavior change | patch |
| Documentation, reports, fixtures, and tests only | none |

Release-relevant paths include the portable contract, capabilities, roles,
runtime adapters, hooks, installers, generators, and operational tools. Public
documentation, internal reports, CI configuration, research, and test-only
paths do not create a release by themselves. An unclassified commit that
changes a release-relevant path receives a patch release rather than being
silently skipped.

## Automation

Every push to `main` runs `Checks`. Only a successful, completed `Checks` run
from this repository's `main` push admits an automatic release. The serialized
release workflow refreshes tags and reserves an annotated SemVer tag before
building the four release assets, then publishes using that run's exact commit
SHA. Failed, cancelled, pull-request, and foreign-repository runs cannot admit
a release. A later default-branch tip never replaces the tested commit. The
workflow does not rely on the generated tag starting a second workflow.

Repeated checks for a published commit, or delayed checks for a commit already
included in a newer published stable release, finish successfully without
publishing again. An existing tag for the exact commit is reused so an
interrupted publication can finish. If another commit occupies the planned
version, publication refreshes the tags and replans above the current stable
version. The reserved version is the one embedded in every asset and used for
release notes; tags and existing assets are never replaced.

Public Markdown documentation and the project's root `AGENTS.md` / `CLAUDE.md`
run the generated-surface and contract checks, without the full runtime,
installation and PID-namespace suites. Main compares all changes since its
latest published stable release, so a documentation push cannot hide earlier
unvalidated code. Pull requests compare against their merge base. Runtime
instructions under `core/`, `capabilities/`, `roles/` or adapters, tests, CI
configuration, unknown paths and unavailable comparisons still run all checks.
The release job reuses the successful checks for that exact commit instead of
rerunning the same source suites; package construction and the published-package
smoke test remain in the release job.

Maintainers may push an explicit SemVer tag, including a prerelease such as
`v1.2.0-rc.1`. Tag pushes and manual releases on `main` or a version tag run the
same reusable `Checks` workflow at the caller's exact commit before the release
job can run. They do not poll for another workflow or bypass the full suite.
Tag releases do not reinterpret the maintainer-selected version. Stable tags
and release assets are never moved or replaced; a correction gets a new version.

The planner and its policy fixtures can be run locally:

```bash
python3 tools/release/plan.py plan --repo . --head HEAD
./tools/release/plan.test.sh
```
