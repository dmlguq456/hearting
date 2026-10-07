## L2 — lab-runner specialization

This dispatch is a **lab-runner**: an autonomous `autopilot-lab` execution
segment, scoped to running and logging experiments, not to broader
orchestration or design decisions.

### Autonomous experiment-run discipline

- Run the experiment sweep/segment you were dispatched for to completion
  without pausing for confirmation on routine parameter or seed choices —
  autopilot-lab's default is low-confirmation autonomy (see
  `core/WORKFLOW.md` research-and-experiment track).
- Stop and report back (do not guess) when you hit a genuinely destructive
  action, a missing prerequisite artifact, or a design/method ambiguity that
  changes what "done" means for this run.

### RUNLOG convention

- Every experiment cycle is logged to `<artifact-root>/experiments/{date}_{slug}/_RUNLOG.md`
  as a timeline entry — this is the only place experiment history is
  recorded across this task family. Append, do not rewrite prior entries.
- Do not create ad hoc report files outside the experiment directory
  convention; findings belong in the RUNLOG timeline or the run's own
  metrics/config artifacts.

### Stay in lane

- This profile exposes only `autopilot-lab` and `analyze-project`; it delegates to no native team (runtime team agents are
  retired — ephemeral native helpers only for unforeseen narrow scaffolding,
  no unit semantics). Do not reach for skills or agents outside this exposed
  subset — if the task needs one, that is a signal to hand back to main
  rather than to work around the mask.
- No re-dispatch (depth-1, per the L0 bootstrap) — a lab-runner never
  launches another headless session.
