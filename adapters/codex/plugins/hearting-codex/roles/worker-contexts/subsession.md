# Sub-session responsibilities

- When dispatch metadata declares a sub-session, treat its phase brief and fixed
  file list as an execution fence. Read the phase brief and assigned
  `_internal/state/<attempt_id>.md`; do not reload the full specification unless
  the phase brief names it. If a required edit falls outside the fixed list,
  finish independent in-scope work, then report the exact path, change, and
  unfinished item to the owner without widening scope.
  For a bound artifact-only node, the effective list names its in-scope outputs
  from the phase brief; declared worktree files are read-only inputs, and brief
  output paths are relative to the cycle output directory. For a bound node
  that also has source scope, a serial or gap-retry session keeps its fixed
  files and adds the brief's in-scope artifact outputs; a parallel slice keeps
  its declared list.
  When the sealed QA plan requires `done_when` records, the exact primary
  report's `.items.json` sidecar follows that report through the same fence,
  ledger, and any continuation derived from it.
  Known approved test files belong in the initial `fixed_files`; a later simple
  omission returns as a bounded patch plus the exact-path handoff under the same
  approved owner — no new approval, no new owner, no separate repair, and no
  relaxation of the runtime `fixed_files` union or the foreign-sibling fence.
- In a declared sub-session, keep the state ledger current after at most three
  material edits and after each verification round trip. Before compaction, flush
  the current slice, completed items, exact next command, invariants, and
  forbidden files. After compaction, re-read the ledger before any edit. A missing
  required ledger is a hard stop for a declared sub-session; an ordinary route
  node has no ledger obligation.
- A sub-session has `stage_authority=0`. It may report its own attempt result and
  bounded handoff, but it must not create, claim, or satisfy the route stage's
  completion marker.
- In a registered serial chain (SD-119), only index 2 or later reads a
  predecessor handoff, using the exact path supplied in its dispatch prompt.
  Parallel slices, index 1, and a 1/1 gap-retry have no predecessor handoff.
  The runtime writes any successor's chain-scoped handoff from the ledger; the
  worker does not create a separate chain handoff output. Ledger updates and
  compaction state remain separate from that runtime handoff.

Native helper support inside a sub-session is checked separately from registered
dispatch and never changes the gate:

| Runtime | Runtime support | Local route-owned projection | Checked fallback |
|---|---|---|---|
| Claude Code | native subagent | supported (`claude-subagent`) | registered headless, then inline |
| Codex | native subagent | supported (`codex-native-subagent`) | registered headless, then inline |
| OpenCode | native agents | no route-owned dispatch-depth-2 evidence yet | registered headless where eligible, otherwise inline |

Any native helper stays inside the parent sub-session's fixed files, mutates
serially, returns only a bounded summary, and has no stage-gate authority.
