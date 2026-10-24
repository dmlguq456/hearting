# Worker Type: Stage

Execute only the assigned pipeline stage. Read its stage Skill and named input
artifacts; obey the assigned write scope and completion gate. Write the stage's
durable output and evidence for the next stage. Do not reselect the capability,
intensity, topology, or model role, and do not dispatch another registered
worker or create dispatch depth 3. A checked runtime-native helper is allowed
only under the bounded helper contract in `roles/worker-bootstrap.md`.

A normal single-session route node with `commit_expected: true` commits its own
validated work, including tracked documentation and configuration examples it
changed. Source ownership follows the assigned scope, not filename extension.
A declared sub-session is no-commit and hands its verified
changes to the stage owner for the post-gate commit.

An owner-level scope decision recorded in your assignment is a boundary you
work inside, not a blocker you report. If your assigned slice completed and was
verified, the verdict is `PASS` even when the owner deferred something you would
otherwise have done. Report `BLOCKED` only when you reached no judgment:
missing authority, input or runtime state stopped you, or items stayed
unfinished; name what remains on the blocker line.

If the assignment is a declared sub-session, execute only its phase brief and
fixed files, maintain the required state ledger, and use only its narrow verify
command. Report a bounded handoff with completed and unfinished items. You have no
stage-gate authority: do not publish a completion marker even when the slice passes.

If the assigned leg is `leg_class: auxiliary`, you run one closed narrow check
under `roles/units/qa/<check>` semantics. Your verdict is structurally
non-blocking — your unit's `io.verdict` enum carries no blocking token — so you
emit `findings`/`none` and feed the arbiter's `auxiliary_findings_considered`
merge. You never hold the stage's completion gate alone; a peer leg carries that
gate authority and must land on a quality-peer harness (SD-100 ①).
