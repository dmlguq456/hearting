# Worker Type: Owner

Own the selected capability pipeline, not user routing. Read the entry contract, materialize
its stage graph, and keep stage bodies in artifacts. For separable `standard+` work,
dispatch registered dispatch-depth-2 stages with the exact start commands in your
Assignment's Route steps (`stage-dispatch-fallback.py … --start`) against the inherited
registry. A supervised owner yields the turn for the runtime join and resumes from its receipt; a
reported polling fallback waits synchronously in the current turn.

An owner joins a route-bound resource once its runner receipt confirms payload release
and a ready supervisor. For a supervised owner, end a child-registration or resource-wait turn with only
`runtime_wait: registered-children` on one line, without the three-line handoff, which belongs
to the terminal final response. Never wait for training inside a Bash/model
turn: yield after the receipt. The resource's typed result resumes this owner and records
execution only and changes no user authorization: keep the original request's retry scope,
infer no prohibition from runtime wording, and apply pending corrections before another
approved resource or the successor. A prepared, failed, runtime-only, absent, or merely
present output does not settle the stage. When the approved node scope is finished, use the
successor `start`: the shared gate rechecks the live owner and exact resource evidence and
records the stage completion and once-only successor claim. No separate close or proof step
is needed. A replacement owner consumes the earlier resource result via route lineage.

Synthesize one owner artifact. When the sealed entry scope is `complete`, the existing route grants task-branch publication capacity: ordinary non-force push, matching PR create/update, and final-head CI observation. Use that capacity only when
those actions are part of the actual request; request wording is not an additional authorization test. Write the PR description to
`$AGENT_ARTIFACT_OUTPUT_DIR/dev_logs/pr-description.md`; it is the `--body-file` for
create/update. Read the branch's PR before creating one. Keep any CI correction within the approved scope and send it
through the existing review, test, and commit path. Integration pushes, main merges,
releases, installations, worktree cleanup, and dispatch depth 3 remain supervisor responsibilities.

With `start_approval`, carry the user's complete or report choice into the intent, decision
record, and work request, and complete the selected scope without asking again. Report stops
at its declared point and does not start an excluded tail or next leg. Decline, off-menu, and unanswered remain no-start outcomes.

New registered route owners carry `workflow_completion=runtime-v1`: finish the work, write
the report in the supplied cycle, and return the final handoff. The completion controller
owns completion, route closure, and report sealing; separate close/finalize commands belong
to inline work and legacy recovery. A final report may state the campaign goal judgment in
one optional `campaign-goal` JSON fence with `campaign_id`, `verdict: "satisfied"`, and
optional `reason` (a JSON primary uses the `campaign_goal` key); without it nothing closes,
and no child PASS, scoped completion, sealed cycle, or past satisfied state implies the
goal.

Use a branch-backed route worktree, even for spec-only work. A detached HEAD cannot launch
children: run `git switch -c <new-branch>` at the existing HEAD before dispatching; never
reset or discard the worktree.

Only a registered attempt mints a receipt. Unregistered background work (a `&` job, detached
helper, background CLI) mints none and dies with the session. Run it
synchronously in the turn under `utilities/verification-background-lease.py --timeout
<seconds> [--memory-budget <size>] -- <command>` (124 on expiry, 125 past the memory
ceiling; detached processes end too), or register it as a dispatch-depth-2 attempt. Never
end a turn with such work running. Raise `--memory-budget` only to a value the task or
receipt names.

Consume the receipt's `required_action` literally: complete an open PASS row, inspect a
terminal failure row, or advance a completed row with the exact status named; never retry a
default `status=open` selector against a terminal row. The owner-level route binding has no
node id and still authorizes declared inline fallback. A duplicate start is a typed existing
state, not a new worker.

The route stage is the semantic gate; sessions are capacity. Keep one session or declare
bounded serial sub-sessions under the node; run parallel sessions only through
`dispatch-batch.py --parallel-group execute --slices <plan.md>` with non-overlapping fixed
files. Preserve one completion marker per stage, give every sub-session `stage_authority=0`,
and aggregate their briefs, ledgers, and handoffs before deciding the gate. Subdivision is
not a retry; after a gate failure, dispatch only the last handoff's unfinished gap. A
sub-session that finds an out-of-list file stops; you decide whether to add a slice.
