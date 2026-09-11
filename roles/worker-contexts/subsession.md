# Sub-session responsibilities

- When dispatch metadata declares a sub-session, treat its phase brief and fixed
  file list as an execution fence. Read the previous bounded handoff and the
  assigned `_internal/state/<attempt_id>.md`; do not reload the full specification
  unless the phase brief names it. If a required edit falls outside the fixed
  list, stop and hand the gap back to the owner instead of widening scope.
- In a declared sub-session, keep the state ledger current after at most three
  material edits and after each verification round trip. Before compaction, flush
  the current slice, completed items, exact next command, invariants, and
  forbidden files. After compaction, re-read the ledger before any edit. A missing
  required ledger is a hard stop for a declared sub-session; an ordinary route
  node has no ledger obligation.
- A sub-session has `stage_authority=0`. It may report its own attempt result and
  bounded handoff, but it must not create, claim, or satisfy the route stage's
  completion marker.
- A sub-session that belongs to a registered serial chain (SD-119) reads the
  chain-scoped handoff (`dispatch_subsession_handoff.py`, one file per chain
  under the artifact root) before its first edit — it is this session's only
  carrier of the predecessor's completed items, exact next command,
  invariants, and forbidden files. Immediately before ending its own attempt,
  flush a fresh chain-scoped handoff for the next index. Neither read nor
  flush touches `PreCompact`/`PostCompact` hooks; this handoff is scoped to
  the chain, not to compaction inside one attempt.

Native helper support inside a sub-session is checked separately from registered
dispatch and never changes the gate:

| Runtime | Runtime support | Local route-owned projection | Checked fallback |
|---|---|---|---|
| Claude Code | native subagent | supported (`claude-subagent`) | registered headless, then inline |
| Codex | native subagent | supported (`codex-native-subagent`) | registered headless, then inline |
| OpenCode | native agents | no route-owned dispatch-depth-2 evidence yet | registered headless where eligible, otherwise inline |

Any native helper stays inside the parent sub-session's fixed files, mutates
serially, returns only a bounded summary, and has no stage-gate authority.
