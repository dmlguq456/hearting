# CLAUDE.md — Claude Adapter Bootstrap

This is the Claude Code adapter bootstrap, not the portable source of truth.
The semantic hierarchy is `core/capabilities/roles -> {Claude, Codex, OpenCode}`;
the three adapters are siblings. Edit portable sources first.

## Source Order

Read `core/CORE.md` first; load the remaining documents only when the task
touches the named domain.

1. `core/CORE.md`
2. `core/WORKFLOW.md` for routing and tracked work
3. `core/CONVENTIONS.md` for intensity, QA, roles, artifacts, and Skill rules
4. `core/OPERATIONS.md` for git, worktrees, locks, and dispatch
5. `core/MEMORY.md` for memory
6. `capabilities/README.md`, `roles/README.md`, and `roles/MODES.md` for task behavior

For runtime-surface or parity changes, verify current official documentation,
then inspect the local realization and its fallback. Never infer support from
another adapter.

## Runtime Router

- Treat `AGENT_HOME` as the installed harness root: for a managed release use
  `${XDG_DATA_HOME:-$HOME/.local/share}/hearting/current`, or an existing route's
  sealed release root. `$HOME/.claude` is the runtime projection, even when its
  harness files are symlinks; do not use it as the managed `AGENT_HOME`.
- Resolve the canonical artifact root with `utilities/artifact-root.sh`; linked worktrees write the primary checkout's `.agent_reports/`, and legacy `.claude_reports/` is only a fallback.
- Use portable model roles, never vendor model names, in shared artifacts.
<!-- BEGIN generated from core/fragments/bootstrap-compute-hosts.md by tools/sync-bootstrap-dispatch.py; edit the source -->
- Reach compute hosts through `compute-hosts` and its inventory (`~/.config/hearting/compute-hosts.yaml`). `run` receipts include measured GPU headroom; `probe` remains available for host comparison.
<!-- END generated from core/fragments/bootstrap-compute-hosts.md -->
- Repo-root `skills/` is the canonical Skill authoring tree; `tools/sync-entry-skill-layer.py` projects it into `adapters/claude/skills/` (generated — do not hand-edit the projection). Claude-native hooks, commands, settings, and kernel helper agents live under `adapters/claude/`; behavior personas live in the portable unit catalog `roles/units/`.
- Task-specific detail is progressively disclosed through the selected Skill and adapter README/ADAPTATION docs; do not preload unrelated procedures.
- Run harness utilities as `hearting run <utility>` (Routing and Execution); a checkout-relative call is for dev activation only (`AGENT_HOME` is that checkout itself).
- Peer-session steering (`OPERATIONS §5.14`): watch a peer depth-0 session with the checked `utilities/peer-steward.py watch`; its armed line carries the next-action field `parent_next=` (§5.10a), and `join`/`status`/`rearm`/`ack` read the watcher's disk receipt. `wait` stays a bounded foreground surface and must never be backgrounded; `peer-steward.py start` defaults a launched child session to `bypass` permissions (`dispatch-defaults.yaml` `steward.child_permission_mode`, opt-out is `inherit`). `peer-steward.py prompt` seals a ref for the exact sender, recipient session and body digest, then uses the receiver's native carrier. `prompted=queued` means the ref is pending or accepted by a native queue; only `prompted=true` with exact native receipt means it was received. Claude's plain interactive TUI may keep the ref pending until its next native callback. A blocked target is never sent to.

## Routing and Execution

<!-- BEGIN generated from core/fragments/bootstrap-dispatch.md by tools/sync-bootstrap-dispatch.py; edit the source -->
```
hearting run capability-route compose --start --prompt-file <task> \
  [--shape direct] [--campaign-key <stream>]
```

Shape-free flags are hints. Follow decided routes under §0.2.1; person-named
stages use `--shape staged --graph <stage,…>`.

`hearting` is on PATH. `hearting run <utility>` uses `AGENT_HOME` when set (dev
activation: that checkout), else the installed release. The slug comes from the
task. The stream defaults to this session's latest artifact-root stream, else
this folder's only active one; otherwise use `--campaign-key` (a folder name or
close spelling joins that stream; refusal lists keys) or `--unassigned`. The
runtime prepares the cycle, selects each leg's harness from live usage within
sealed candidates, starts frames and owner, reuses exact attempts, and closes
the route and cycle at success or idle.

Follow the receipt. `parent_next=end-turn`: end with no wait, poll or recap;
a runtime carrier owns it. `parent_next=bounded-wait`: run `parent_next_command`
once. Start saves `receipt_file`; read it if hooks omit `parent_next`.
No directive is not `end-turn`; never filter stdout.
After wake/correction, run `resume_command`; answer a BLOCKED owner with `correction_command
--message-file <file>`. At `needs-question`, compare the two frame briefs and
follow the receipt's `next_step`: `resume_command --interview <file>` registers
the question, the person answers it once in the native question surface, and
`resume_command --answers <file>` records the intent, releases the gate and
starts the owner.

Inside an owner, a stage is `python3 "$AGENT_HOME/utilities/stage-dispatch-fallback.py"
--node <node> --start`: route, slug, parent and harness come from the owner's
route and environment, and a member of a sealed parallel group starts its
whole group in one batch. Dispatch depth 3 is forbidden.

Default to this session's route; start an interactive peer with `hearting run
peer-steward start` only for long work needing human answers or recovery.
Command output panes use `hearting run pane open <command>`.

Message another session with `hearting run peer-steward prompt
<name-or-pane> --body-file <file>`; reply text addressed to it reaches nobody.
<!-- END generated from core/fragments/bootstrap-dispatch.md -->

Route by `core/WORKFLOW.md §0.2`: semantic precedence names the capability
that owns the artifacts. The main session decides only whether the work is
direct (§0.2.1): use `--shape direct` for one atomic, reversible inline change;
otherwise omit `--shape` and let the frame decide.

Use `hearting run capability-route compose`; without `--shape`, capability,
mode, graph, and profile flags are routing hints only. Follow frame- or
person-decided shapes under §0.2.1; person-named stages use
`--shape staged --graph <stage,…>`. Cwd, artifact root, tracking, drift verdict,
spec-read gate, and both eligibility probes default from the checkout. Use the
preset recipe (`capability-route.py compile`) only when the request names the
entry's full loop or a promotion signal or spec-backed flow requires it; never
bend a loosely matching request into the nearest preset graph. Apply §0.3. For
`direct`/`solo` routes user-authored turns receive the §0.4 card; received
peer envelopes use the one `[경로]` line `compose` prints plus one clause
of scope, unless the work is destructive or
external-facing; otherwise present the five-field card in §0.4 before
material work unless scope and route are already approved — deliver it
through `AskUserQuestion` (five fields as the question body, options
진행(권장)/수정/중단; plain-text card only as fallback) — and close material
work with the first-report content required by §0.5; its card is optional.
Load full capability detail only in the acting owner or worker; spec work
retains spec-read gate.

For `autopilot-code`, `direct` is inline, `quick` is one registered dispatch-depth-1
owner, and `standard+` follows `code-plan -> code-execute -> code-test ->
code-report` under `core/OPERATIONS.md §5.10`. Dispatch depth 3 is forbidden.

Contract v3 claims one
stable attempt before spawn; the retired broker only supports `status`/`stop`.

Keep native agents distinct from registered headless worker dispatch; a restriction on one surface never silently extends to the other. Preserve model role, intensity, depth, tests, safety, and validation on fallback. Do not run drill automatically. A Codex job the openai-codex plugin detaches after its foreground timeout runs in the plugin's own queue, never in jobs.log; Fleet shows it only as a read-only plugin-queue row. Launch substantial Codex delegation that needs attempt-grade tracking or gates through registered dispatch instead.

## Tracked-Workflow Continuation

Process exit is not workflow completion (`core/WORKFLOW.md §0.6`). A workflow is
complete only when every declared terminal node holds its completion gate. Every
non-terminal stage declares `inline-next`, `supervised`, `human-gate`, or
`monitor`; a detached resource run must be `supervised` and can never be
terminal. A graph that breaks this is refused at `capability-route.py compile`
and at launch.

<!-- BEGIN generated from core/fragments/bootstrap-continuation.md by tools/sync-bootstrap-dispatch.py; edit the source -->
Keep unfinished work here or in its registered continuation; state checked fallback if unsupported. Lead with condition/result, decision/blocker; keep PID/start, exit/log, digests and paths in evidence.
On completion, review exact results and do approved follow-through here or in its continuation. Receipt/queue/ACK is not execution; report chronology. Reply once with immediate result or short receipt for delayed/sustained work. Informational notices need no reply. Accept typed handoff only by its supplied command; refs/cards and same-seat clear need none.
<!-- END generated from core/fragments/bootstrap-continuation.md -->

`OPERATIONS §5.12` owns the mechanics.

## Runtime Lifecycle

Claude hooks realize portable invariants for workflow signals, spec-read observations, memory, and design checks. Main-session memory lifecycle does not run for workers. Session end never owns destructive worktree cleanup.

Use `statusline.sh` only for runtime status. Harness detail remains available through the adapter tools and docs. Runtime-owned credentials, sessions, logs, caches, databases, and config stay outside this repo.

## Context and Memory

Each eligible main prompt receives bounded capsule headline-and-ID candidates.
Ignore unrelated candidates and read a relevant record in full before use. If
the prompt hook is unavailable, record `recall` or `skip` with
`mem recall-gate`. Retrieve full pending obligations before applying or
consuming them. Workers do not run this main-session probe.
Call `/session-tidy` when context is filling or before compact, before handing
work to another session, and when a large task ends; a new session receives the
seat's card once.

Context pressure is orthogonal to quality and stage graph. Ordinary hook states stay silent. Static bytes, code lines, and directive counts are footprint measures, not token or billing savings. `core/ADAPTATION.md §6.1` owns budgets; real savings claims require paired production sessions.

## Response Policy

Portable behavior contract = `roles/response-policy.md`.

- **Audience-language first** — user artifacts default to the user's current communication language unless a stronger audience or repository contract applies. Code, code comments, commit messages, and PR text follow the repository's language, even when the runtime `language` setting names another.
- Keep responses concise and match promises with same-turn action.
- **Answer first, bounded** — lead with the answer; unrequested explanation stays within about five lines or five short bullets unless the user asked for depth or the turn closes material work. Offer the rest rather than delivering it unbidden.
- **Plain address** — write for a tired reader: ordinary words over harness jargon, conclusion before its qualifications, no clause-stacked sentences or unexpanded internal terms. Other sessions use Fleet tag and pane (SID if needed), role in parentheses; without a tag, herdr name and pane, rather than internal abbreviations.
- Verify before asserting and follow existing conventions.
- **Local evidence before recall** — answer a domain question from the repository's research/analysis/briefing artifacts first; model memory is the fallback, a memory-only answer says so and flags its risky specifics, and the §0.4 card exemption never waives this evidence check.
- Ask only for genuinely non-obvious or destructive choices; proceed with the recommended reversible path when no answer is needed. Use structured input only for choices that materially change the goal, architecture, UX, large scope, destructive work, or an external-system outcome. Continue low-risk reversible work autonomously. If structured input is unavailable, ask one concise ordinary question; a helper never owns user input or approvals.
- In an active “do X” flow, implied records, validation, commit, and push follow without repeated confirmation.

Claude-specific realization: keep work grounded in current files, expose changes before committing, and commit/push validated harness changes in the same turn under `core/OPERATIONS.md §5.11`.

## Compatibility Boundary

Codex and OpenCode files are sibling implementation references, never Claude bootstrap input. Portable meaning comes from `core/`, `capabilities/`, and `roles/`; map that meaning to Claude-native runtime surfaces.
