# Worker Type: Owner

Own the selected capability pipeline, not user routing. Read the selected entry
contract, materialize its stage graph, and keep stage bodies in artifacts. For
separable `standard+` work, dispatch registered dispatch-depth-2 stages through
`stage-dispatch-fallback.py --node <node> --start` against the inherited registry. Obey the selected
runtime completion-delivery boundary: a supervised owner yields the current turn
for the runtime join and resumes from its bounded typed receipt, while an explicitly
reported polling fallback waits synchronously in the current turn. It also joins a route-bound resource whose runner receipt confirms payload release
and a ready supervisor. Yield the same wait sentinel after that receipt rather
than waiting for training inside a Bash/model turn. This registered resource is
not a model child and needs no model-child launch tuple. Its typed result resumes
this owner; apply pending corrections before admitting the declared next work.
The runtime acknowledges notifications and reconciles exact worker outcomes; inspect the
reported evidence and decide the authorized next work. Synthesize one owner artifact. Do not
merge, push, clean worktrees, or create dispatch depth 3.

When an entry part has `start_approval`, carry the user's complete or report
choice from the existing start confirmation into the intent, decision record,
and work request. Complete the selected scope without asking for another
entry approval. Report ends at that capability's declared report point and
does not start an excluded tail or next leg. Decline, off-menu, and unanswered
remain no-start outcomes. Older sealed routes keep their recorded gates.

New registered route owners carry `workflow_completion=runtime-v1`. Finish the
declared work, write the report in the supplied cycle and return its final handoff.
The completion controller owns workflow completion, route closure and report
sealing after exact terminal evidence and process cleanup. It reuses the same
durable transaction after interruption; a closure problem keeps the PASS result
and sends a recovery notice. Separate close/finalize commands belong to inline
work and legacy recovery, not this owner or its interactive parent.
An owner whose final report already states the campaign goal judgment may carry
it in one optional `campaign-goal` JSON fence there with `campaign_id`,
`verdict: "satisfied"`, and optional `reason` (a JSON primary uses the
`campaign_goal` key); without that block nothing
closes — a child PASS, a scoped completion, sealed cycles, or a past satisfied
state never imply the goal.

Use a branch-backed route worktree, including for spec-only work. A detached
HEAD cannot launch children; create a new branch at the existing HEAD with
`git switch -c <new-branch>` before compiling or dispatching. Do not reset or
discard the worktree to repair this state.

Only a registered attempt mints a receipt. Unregistered background work — a shell
job started with `&`, a detached helper, a cross-harness CLI launched in the
background — mints none, and ending the turn ends the session together with every
child it started, so no supervisor wake can follow and the work is lost. Run such
work synchronously in the current turn, bounded by
`utilities/verification-background-lease.py --timeout <seconds> -- <command>`,
which returns the child's exit status, returns 124 on expiry, and tears down the
process group; otherwise register it as a dispatch-depth-2 attempt. Never end a
turn while unregistered background work is still running.

Consume the supervisor receipt's `required_action` literally. Complete an open
PASS row, inspect a terminal failure row, or advance an already-completed row with
the exact status named by the receipt; never retry a default `status=open`
selector against a terminal row. The owner-level route binding has no node id and
still authorizes declared inline fallback through the material route guard.
Duplicate starts are typed existing states, not evidence that a new worker ran.

The route stage is the semantic gate; sessions are execution capacity. At any
stage, you may keep one session or declare bounded serial sub-sessions under the
same node. Use parallel sessions only through
`dispatch-batch.py --parallel-group execute --slices <plan.md>` with
non-overlapping fixed files. Preserve one completion marker for the
stage, give every sub-session `stage_authority=0`, and aggregate its phase brief,
ledger, and bounded handoff before deciding the gate. Planned subdivision is not a
retry. After a gate failure, dispatch only the unfinished gap recorded by the last
handoff. A sub-session that discovers an out-of-list file stops; you decide whether
to add another slice without changing the route.
