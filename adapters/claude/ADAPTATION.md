# Claude Code Adaptation

## Dispatch model realization

`deep maker`, `deep reviewer`, `deep editor`, and `deep orchestrator` map to
`opus`/xhigh. Retained `orchestrator` is the balanced mechanical role and maps to
`sonnet`/high; it is not an alias for the standard+ dispatch-depth-1 conductor. Fast
portable roles map to `sonnet`/high. The adapter mapper normalizes case,
hyphens, and underscores but accepts no undocumented role aliases.

This adapter preserves the previous Claude Code setting behavior while moving
runtime-specific files out of the common root.

## Worker bootstrap realization (2026-07-16)

The wrapper renders `roles/worker-bootstrap.md` plus one deterministic worker
type before the assignment. A masked profile adds only its attach layer and
selected specialization. Changed files, commands, logs, and findings remain in
the canonical artifact; the child returns only artifact path, verdict, and a
one-line blocker. Official Claude Code behavior loads the `CLAUDE.md` memory
hierarchy into ordinary custom subagents and provides no per-agent switch for
that input, so the adapter claims masked profile projection, not universal
physical instruction masking.

## Native Claude Surfaces

| Claude runtime surface | Adapter source | Projection |
|---|---|---|
| Session bootstrap | `adapters/claude/CLAUDE.md` | `claude_setting/CLAUDE.md` |
| Hook and permission config | `adapters/claude/settings.json` | `claude_setting/settings.json` |
| Keybindings | `adapters/claude/keybindings.json` | `claude_setting/keybindings.json` |
| Slash commands | `adapters/claude/commands/` | `claude_setting/commands` |
| Runtime worker wrappers | `adapters/claude/bin/` | `claude_setting/bin` |
| Agents | `adapters/claude/agents/` | `claude_setting/agents` |
| Skills | `adapters/claude/skills/` | `claude_setting/skills` |
| Agent modes | `adapters/claude/agent-modes/` | `claude_setting/agent-modes` |
| Hooks | `adapters/claude/hooks/` | `claude_setting/hooks` |
| Tools | `adapters/claude/tools/` | `claude_setting/tools` |
| Utilities | `adapters/claude/utilities/` | `claude_setting/utilities` |
| Loops | `adapters/claude/loops/` | `claude_setting/loops` |
| Scaffolds | `adapters/claude/scaffolds/` | `claude_setting/scaffolds` |
| Statusline | `adapters/claude/statusline.sh` | `claude_setting/statusline.sh` |

`~/.claude/*` should point at `claude_setting/*`, not directly at common files.

## Worklog And Agent Notes Realization

Claude Code currently realizes the portable continuity layer through local paths:

| Portable name | Current Claude realization | Classification |
|---|---|---|
| `<agent-notes-root>` | `<nas-or-home>/notes/` | mutable continuity state |
| `<worklog-board-app>` | `~/.claude/worklog-board/` | external/local app workspace |
| `<worklog-board-app>-wt/` | `~/.claude/worklog-board-wt/` | app worktrees |

The Claude adapter keeps note semantics outside the portable harness. A note
application may register the optional app-neutral `artifact-sink` extension;
the application owns ingestion, routing, scheduling, and UI approval
flows. The adapter must not move or delete existing data during harness
migration.

Keep these out of the harness repo: notes data, worklog local DB/cache, `.env*`,
`.next`, `node_modules`, `.dispatch`, app runtime logs, and worktrees. If the
board app source is later made portable, promote it as a separate app/tool with
its own repo or explicit source directory rather than treating the current
`~/.claude` workspace as adapter source.

## Design Harness Realization

Claude Code realizes the portable design harness through the projected
`claude_setting/tools/design-mcp` tree and Claude MCP registration:

```sh
claude mcp add design --scope user -- node ~/.claude/tools/design-mcp/server.js
cd ~/.claude/tools/design-mcp && npm run smoke
```

Portable capability specs refer to this as the runtime design harness. The
Claude adapter owns the concrete MCP command, `~/.claude/tools/design-mcp`
runtime path, and any Claude-specific preview/screenshot/console wiring.

## Memory Distiller Realization

Claude `UserPromptSubmit` first runs the portable `mem-recall-inject.sh` bridge.
It emits `hookSpecificOutput.additionalContext` only for bounded active
current-project/global capsule headline-and-ID candidates (maximum six and
2,400 UTF-8 bytes), never record bodies. The bridge is fail-open but publishes
a same-turn recall-opportunity receipt on a successful probe, including a valid
zero-hit probe. Main-session material mutation requires that receipt; the
explicit `recall-gate` path recovers a missing or failed hook. Registered
workers stay silent. Candidate relevance and full-record adoption remain model
judgments.

`UserPromptSubmit` also runs the portable `local-evidence-inject.sh` presence
probe for the prompt cwd: research/documents/analysis bucket counts plus at
most six newest entry paths from the canonical artifact root (2,400-UTF-8-byte
bound, no body reads, no prompt classifier, silent when the root holds no such
artifacts, worker-exempt, fail-open). It realizes `roles/response-policy.md`
"Local evidence before recall" deterministically.

Claude Code realizes the portable memory distillation hooks through
`adapters/claude/settings.json` hook registration and concrete hook scripts under
`adapters/claude/hooks/`.

The current Claude distiller mapping is:

| Portable worker role | Claude realization |
|---|---|
| `fast distiller` / turn-counter add-only worker | `adapters/claude/bin/mem-distill-worker.sh` maps `fast-distiller` to the mini lifecycle tier declared in `adapters/claude/config/models.conf` |
| `deep curator` / SessionEnd action worker | `adapters/claude/bin/mem-distill-worker.sh` maps `deep-curator` to the curate lifecycle tier (light) declared in `adapters/claude/config/models.conf` |

`hooks/mem-distill-dispatch.sh` keeps the existing Claude behavior: opt-in via
`MEM_DISTILL_ENABLE=1`, recursion guard through `MEM_DISTILL=1`, no-tools output
contract through the Claude worker's `--disallowedTools`, JSON/action validation
in shell/Python code, and `mem` CLI as the only DB mutation path. The shared
dispatcher contract is `MEM_DISTILL_WORKER=<executable>` with
`<mode> <model> <prompt-file>` arguments; this adapter sets that executable to
the Claude worker when using the shared dispatcher. Other adapters must provide
their own transcript source and worker invocation or explicitly keep automatic
distillation unsupported.

## Dispatch And Statusline Realization

Claude Code realizes the portable dispatch contract through headless Claude Code
main sessions and the adapter-owned statusline script. Its CLI supports one-off
model/effort overrides, so dispatch model selection follows the core rule:
main/orchestrator chooses per job and the wrapper only reflects that choice:

- Standard+ dispatch-depth-1 owners run under `utilities/claude-session-supervisor.py`:
  the first print turn uses one generated `--session-id`, exact child batches are
  joined outside the model, and each batch resumes once with `--resume`. The
  wrapper suppresses intermediate `result` rows and exposes only the final result
  to the attempt-specific `.claude.jsonl`. Quick and dispatch-depth-2 one-shot workers keep
  `--no-session-persistence`. `--completion-delivery auto` probes both CLI flags;
  forced supervised mode fails before registration, and the checked legacy path
  is reported as `poll-fallback`. Before each supervised turn the bridge writes
  an atomic attempt-scoped delivered-set state and supplies a command-scoped
  `--settings` PreToolUse hook. While the batch is undelivered, only another exact
  same-parent dispatch start is admitted, so a first child does not block its
  siblings; once the receipt is delivered, only exact harvest of delivered open
  rows is admitted. Waits and unrelated tools are denied in both phases, invalid
  state is recovery-only harvest, and normal exit or the exact owner watcher
  removes the state. The watcher reconciles every exact owner exit, including a
  model/auth/limit failure before the owner creates a route child, so a dead
  process cannot leave an `open` row that is absent from Fleet but still parks
  the parent. This extra
  settings object applies only to the spawned command and never edits the user's
  Claude settings.
  Its finite continuation ceiling is derived from the verified owner route's
  node count plus one slot per unique `resume_retry_boundaries` node, never below
  the compatibility floor. A positive `--max-continuations` owner-launch value
  replaces that default; missing or mismatched route evidence stays at the floor.
- Every registered `claude -p` turn (one-shot worker, quick owner, and the
  session-resume supervisor's first and resumed turns) pins its starting
  permission mode from `headless.claude_permission_mode` in
  `dispatch-defaults.yaml` (`--permission-mode` / `CLAUDE_DISPATCH_PERMISSION_MODE`
  per launch): `bypass` (shipped default) appends `--permission-mode
  bypassPermissions`; `allowlist` appends `--permission-mode acceptEdits`.
  Both postures append `--allowedTools` naming the harness utilities under the
  sealed `AGENT_HOME`, read-only git (plus `git add`/`git commit` for an
  owner), the harness test runners, and `Edit(//<worktree>/**)` /
  `Edit(//<artifact-root>/**)`. Root/sudo and
  `permissions.disableBypassPermissionsMode: "disable"` in any settings scope
  (managed, project-local, project, user) demote `bypass` to `allowlist`
  automatically. The wrapper prints and records `permission_mode=`,
  `permission_mode_reason=`, and `permission_inherited_mode=` (the settings
  `defaultMode` the turn would otherwise inherit) so the applied posture is
  ledger evidence, and the proven-fatal `--disallowedTools` deny still applies in both postures
  (`core/OPERATIONS.md §5.10` "Registered headless permission posture").
- Direct registered child completion is selected by the parent runtime, not by
  the child wrapper. An interactive Claude parent receives the successful exact
  owner-start receipt through `PostToolUse(Bash)` and arms one native
  `asyncRewake` hook for that owner attempt. The hook waits for terminal
  quiescence outside the model and wakes once with an exact harvest command;
  ordinary Bash calls are silent no-ops, and no Background Bash monitor,
  `dispatch-wait`, progress recap, or periodic re-arm is created. Immediately
  before rendering, the hook re-reads the exact row and its sealed completion
  evidence: every terminal receipt, success or attention, exits two — Claude
  Code wakes an idle session for an `asyncRewake` hook only on exit code 2 and
  holds exit-0 output until the next user interaction (corrected 2026-08-29;
  success still carries its structured notification on stdout). The
  SessionStart/UserPromptSubmit sweep re-delivers any SD-111 pending record at
  the next prompt as the durable backstop. Registered Claude
  owners keep one `--input-format stream-json` process for the route and submit
  each non-terminal joined receipt immediately. When the current rows and exact
  sealed markers prove every declared terminal node complete, the supervisor
  skips the redundant final owner turn and closes the process before terminal
  row reconciliation. An explicit `--claude-command` keeps the checked historical
  `--session-id`/`--resume` per-turn fallback. Monotonic control rows measure every
  turn, join, and terminal teardown without exposing model text. Both paths work
  for either a Claude or Codex child. A Codex parent
  that launches a Claude child uses the checked `codex-managed-gateway` only
  when that parent was created through the explicit managed entry; its
  completion sidecar is control-only and never becomes a Claude or Codex
  approval client. The selector preserves the actual Codex caller independently
  of the chosen Claude owner. A new unmanaged interactive Codex parent then
  fails with `managed-entry-required` before registry mutation or spawn instead
  of entering model-owned polling. Only a human using the low-level wrapper may
  explicitly authorize bounded poll recovery; portable owner/model routes
  cannot select that override.
- A route-bound headless job carries a sealed `model_profile` independently of
  its behavioral `model_role`. The wrapper resolves `deep`, `balanced-deep`,
  `light`, or `mini` through the complete user `agent-config/models.conf` when
  valid and the complete shipped `config/models.conf` otherwise; it rejects caller replacement
  with a trailing concrete model/effort and rejects `mini` for substantive
  registered dispatch-depth-1/2 work. A `_kernel/owner` may therefore be profile-only and
  never needs a stage `worker_mode`. Non-route jobs retain explicit role or
  concrete-model selection. Headless inheritance is rejected because it cannot
  prove that interactive-main-only `fable` will not leak into a worker.
- Dispatch prompts and jobs.log rows must spell out capability, mode, QA,
  intensity, depth, parent slug/session, worker type, model role/profile,
  profile tier/granularity, owner capability, and owner harness. Route-declared
  parallel groups are bounded 2–4-way atomic batches with sealed perspective and
  leg indexes; cross-harness means at least two realized harness families, with
  typed reduced-independence reporting when auto-selected fallback is allowed.
  Cross-harness launches from Codex pass `CODEX_THREAD_ID` through `parent_sid`,
  so fleet can render the Claude worker under the Codex orchestrator instead of
  as an orphan.
- `utilities/dispatch-liveness.sh` first inspects the exact wrapper JSONL for a
  final Claude `result` envelope, then uses process identity and legacy Claude
  session transcript mtimes as lower-precedence liveness evidence.
- `dispatch-chain` supplies a provisional lifecycle, then the Claude wrapper
  rechecks its actual scope before attempt registration. A transient wrapper
  promotes `detached` to `foreground-scoped`, remains alive with `claude -p`,
  and forwards termination signals. Wrapper output and the exact jobs row record
  requested/effective lifecycle, reselection result, and bounded namespace
  evidence. Outside that scope it keeps `detached`; the explicit
  long-lived-namespace override also remains detached.
- A dispatch-depth-2 claim resolves one live exact dispatch-depth-1 owner and
  seals its `parent_attempt_id`. The row stays registered-only until a blocked
  parent-death-safe fence has a complete PID/start/namespace/leader-PGID
  identity; publication and `launch_claimed=1` share the jobs lock, and the
  fence records `launch_started=1` immediately before exec. Parent identity is
  rechecked before fence release. Foreground fences retain parent-death
  coupling, procfs-incomplete group scans fail closed, and teardown signals
  only a current exact group leader; retries with the same slug are never
  targeted.

- **SD-110 session-resume supervisor call site.** `attempt_stage_advance` runs
  in `claude-session-supervisor.py` immediately before the existing
  `terminal_route_completion` decision, once per just-joined route-bound
  child. Behind `--enable-stage-advance` (off by default), it derives
  `parked`/`running-turn` from the same owned/open-children predicate the
  supervisor already computes, calls `coordinate_stage_advance` with
  `RealStageAdvanceServices`, and never lets a refusal — typed or an
  unexpected exception — propagate past a `dispatch.supervisor.stage-advance`
  or `-refused` canary event. With the flag off, or on any refusal, delivery
  is byte-identical to pre-SD-110. Eligibility negotiation
  (`receipt_schema_negotiated`) and delivery negotiation
  (`receipt_with_stage_advance`'s `negotiated`) are the same
  `--enable-stage-advance` boolean, not two independently toggled ones: the
  durable `stage_advance_record_v1` this function returns for an
  `outcome == "advanced"` boundary is fed straight into
  `receipt_with_stage_advance` before the join receipt is used for the next
  resume, so an advance the model is never told about cannot occur. That
  attachment call is a no-op unless both an actual advance happened and the
  same flag is on, so the ordinary v1/v2 delivery path stays untouched at
  every other boundary and refusal.

Codex and future adapters should preserve the dispatch invariant, but must map
it onto their own thread/subagent/session/status surfaces instead of copying the
Claude statusline or `claude -p` process model.

## Compatibility Realizations

These surfaces are still consumed by Claude Code directly, but their runtime
paths now point at adapter-owned realization files instead of the common root:

| Surface | Current projection | Why compatibility realization is allowed for now | Required split |
|---|---|---|---|
| Skills | `claude_setting/skills -> ../adapters/claude/skills` | Adapter-owned concrete Claude Skill files preserve old behavior while portable specs grow under `capabilities/` | Continue splitting semantics into `capabilities/<name>.md`; keep Claude frontmatter and runtime wording here |
| Agent modes | `claude_setting/agent-modes -> ../adapters/claude/agent-modes` | Adapter-owned concrete mode projection files preserve current Claude behavior while `roles/MODES.md` classifies portability | Continue splitting adapter-coupled mode semantics into runtime-neutral fragments or adapter-native notes as non-Claude adapters implement equivalents |
| Hooks | `claude_setting/hooks -> ../adapters/claude/hooks` | Adapter-owned concrete hook projection files preserve current Claude behavior; `core/HOOKS.md` names the invariant layer | Continue splitting Claude payload handling from portable invariant checks as non-Claude adapters implement equivalents |
| Utilities | `claude_setting/utilities -> ../adapters/claude/utilities -> ../../utilities` (whole-layer symlink) | Split complete (2026-07-22): zero Claude-only utility files remain; the last delta (`agent-worklog-state.sh` local paths) moved to runtime `settings.json` env (`AGENT_NOTES_ROOT`/`CAIRN_APP`/`CAIRN_WT`; legacy `WORKLOG_BOARD_*` read fallback) | None — a future Claude-only utility delta requires deliberately reintroducing the per-file layer plus an exemptions row |
| Tools | `claude_setting/tools -> ../adapters/claude/tools` | Adapter-owned concrete tool files preserve current Claude helper behavior while tool semantics are split | Isolate Claude session adapters under adapter or tool plugin |
| Loops | `claude_setting/loops -> ../adapters/claude/loops` | Adapter-owned concrete loop files preserve current Claude drill/oncall/study behavior | Split runtime-coupled loop invocation if non-Claude adapters need native loop runners |
| Scaffolds | `claude_setting/scaffolds -> ../adapters/claude/scaffolds` | Adapter-owned concrete scaffold files preserve current Claude design/template behavior | Move Claude-only scaffold assumptions into adapter-native files when found; keep portable scaffold intent in common docs |

Direct symlink passthrough from adapter-owned runtime surfaces back into the
common root is a temporary migration state, not the final adapter shape.

Agent files have completed the first split: portable role meaning is summarized
in `roles/README.md`, while Claude Agent frontmatter, tool lists, and concrete
model mapping live in `adapters/claude/agents/`.

Capability files have started the same split: portable capability meaning lives
in `capabilities/README.md` and `capabilities/<name>.md`, while Claude Skill
mechanics live as concrete adapter projection files under
`adapters/claude/skills/<name>/SKILL.md`. The current projection intentionally
preserves previous Claude behavior; future edits should move invariant meaning
to `capabilities/` first, then adjust the Claude Skill wording here.

Mode files now follow the same concrete projection pattern as skills:
`claude_setting/agent-modes` points at `adapters/claude/agent-modes/`, whose
family entries are adapter-owned files copied from the current `roles/modes/`
content. This preserves old Claude behavior while `roles/MODES.md` continues to
classify which fragments are portable, tool-contract-bound, or adapter-coupled.

Hook scripts now follow the same concrete projection pattern:
`claude_setting/hooks` points at `adapters/claude/hooks/`, whose files are
adapter-owned copies of the current shared `hooks/` scripts. This keeps the
existing Claude `settings.json` commands stable while `core/HOOKS.md` continues
to define the portable invariant layer and future adapter wrapper split.
`material-route-guard.py` is registered on PreToolUse source Edit/Write and Bash
commit surfaces, on PostToolUse Bash to bind one successfully compiled route to
the exact Claude session, and on SessionEnd to remove that session marker. It
revalidates the immutable route on every protected action; the separate
capability-grounding Skill marker is display-only and never satisfies this gate.

`fleet-interaction-state.py` is the Claude-native payload wrapper for the
portable interaction-wait invariant. `PreToolUse(AskUserQuestion)` publishes a
decision marker and `PermissionRequest(*)` publishes a permission marker;
`PostToolUse(*)`, `PostToolUseFailure(*)`, `UserPromptSubmit`, `Stop`, and
`SessionEnd` clear only the exact session. The hook stores no question,
choices, command, arguments, reason, model output, or tool payload, emits no
stdout, and is a no-op for registered workers and native subagents. Herdr's
separate hook remains optional corroboration and is not edited. Claude's native
`PostToolUseFailure` has no verified Codex counterpart, so the adaptation
boundary records that event as an explicit non-parity exception.

Utility scripts are a whole-layer collapse (2026-07-22): `claude_setting/utilities`
points at `adapters/claude/utilities`, which is itself ONE symlink to the shared
`utilities/` layer. Every shared utility — including a newly added file — resolves
for Claude with zero per-file mirror work, and per-file Claude deltas are retired.
Runtime-local worklog discovery paths live in runtime `settings.json` env
(`AGENT_NOTES_ROOT`, `CAIRN_APP`, `CAIRN_WT`; legacy `WORKLOG_BOARD_*` read fallback), not in script copies.

Scaffold assets now follow the same concrete projection pattern:
`claude_setting/scaffolds` points at `adapters/claude/scaffolds/`, whose files
are adapter-owned copies of the current shared `scaffolds/` assets. This keeps
Claude-facing scaffold paths stable while future edits can split portable
template intent from runtime-specific integration.

Loop helpers now follow the same concrete projection pattern:
`claude_setting/loops` points at `adapters/claude/loops/`, whose files are
adapter-owned copies of the current shared `loops/` helpers. This keeps current
Claude loop entry points available without treating the common root as the
runtime projection.

Tooling now follows the same concrete projection pattern:
`claude_setting/tools` points at `adapters/claude/tools/`, whose files are
adapter-owned copies of the current shared `tools/` helpers, excluding local
cache artifacts such as `__pycache__`. This keeps current Claude helper paths
available while future edits can split portable tool logic from runtime-specific
session integration.

The first-level Claude adapter support surfaces above no longer use symlink
passthrough entries back into the common root. `claude_setting/` remains the
runtime projection layer and should continue to point at `adapters/claude/*`.

## Model Mapping

Claude Code maps portable roles as follows:

| Portable role | Claude mapping |
|---|---|
| `fast reviewer` | `sonnet` |
| `fast fact-checker` | `sonnet` |
| `fast writer` | `sonnet` |
| `fast implementer` | `sonnet` |
| `deep reviewer` | `opus` |
| `deep maker` | `opus` |
| `external adversary` | Codex CLI via `codex-review-team` when available |
| `orchestrator` | `sonnet` unless a task explicitly requires deep judgment |

Concrete model names belong here and in Claude-native files only.

## Role Profile Frontmatter Mapping

Behavior personas live in the portable unit catalog `roles/units/` and carry
portable role names only — they have no Claude `model:` frontmatter. The former
per-team agent files (plan-team, dev-team, qa-team, and the other five) were
removed in the unit-catalog migration. The only kernel helper agent under
`adapters/claude/agents/` is `memory-scout`, whose frontmatter pins the mini
tier (haiku) as an explicit `check-model-config.py` exemption. Every other
native subagent spawn resolves its model through the native-subagent default
below.

## Native Subagent Default Model (2026-07-23)

Native Claude subagent spawns (Agent/Task tool) no longer silently inherit the
interactive session model. `adapters/claude/settings.json` registers
`hooks/subagent-model-default.sh` on PreToolUse matcher `Agent|Task`; when the
spawn carries no model decision of its own, the hook re-emits the full
`tool_input` with a `model` field added via the PreToolUse `updatedInput`
output. The injected value derives from `adapters/claude/config/models.conf`
(`CFG_NATIVE_SUBAGENT` names a tier; the tier resolves through
`CFG_TIER_<TIER>_MODEL`), so no concrete model ID lives in the hook.

An explicit eligible per-invocation model or eligible agent-definition pin is
preserved. A config-declared interactive-main-only model, explicit `inherit`,
fork inheritance, or a missing eligibility policy returns a typed PreToolUse
deny before spawn. Malformed or non-Agent payloads remain silent because they
do not identify an actionable native-worker launch.

Rejected realization — global `CLAUDE_CODE_SUBAGENT_MODEL` env: the official
subagent model precedence is env > per-invocation `model` param > definition
frontmatter > session model (official subagents doc, "Choose a model"; since
v2.1.196 an `inherit` value is treated as unset). The env level is therefore a
hard override that would kill the `memory-scout` frontmatter pin and per-call
escalation. The PreToolUse injection sits at the per-invocation level instead
and yields to frontmatter pins through its own skip logic.

Runtime limits: Claude Code exposes no global subagent
reasoning-effort knob and the Agent tool input has no effort field, so this
realization controls the model tier only; per-agent effort remains a
custom-agent frontmatter concern. `CLAUDE_NATIVE_SUBAGENT_MODEL=<eligible-alias>`
forces a different injected model at runtime; `inherit` and main-only aliases
are rejected without touching config.

## Reproduction Contract

The following runtime paths must continue to work:

```text
~/.claude/CLAUDE.md
~/.claude/README.md
~/.claude/settings.json
~/.claude/keybindings.json
~/.claude/commands/
~/.claude/statusline.sh
~/.claude/skills/
~/.claude/agents/
~/.claude/hooks/
~/.claude/tools/
~/.claude/utilities/
```

If a future split changes any target path, update `claude_setting/` first and
verify through the runtime path above.

## Canonical artifact and cleanup boundary (2026-07-14)

- **Runtime support:** official Claude worktree documentation says changed
  worktrees require retention/cleanup handling, and noninteractive
  `-p --worktree` sessions are not automatically cleaned. `--add-dir` is the
  native scoped path surface.
- **Adapter realization:** the wrapper resolves the primary checkout's artifact
  root, injects `AGENT_ARTIFACT_ROOT`, and passes that one path through
  `--add-dir`. Shared guards reject worker-local artifact writes.
- **Parity gap/fallback:** SessionEnd cannot prove merge or push and never
  deletes worktrees. Main uses `bin/worktree-cleanup.sh` after integrated
  verification and push.

## SD-62 direct headless delegation — realized

Claude conductors launch checked same- or cross-harness dispatch-depth-2 headless
adapters directly. The immutable v3 route binds the checked tuple and stable
attempt identity; register/start are atomically claimed in the canonical
registry. The retired broker retains diagnostic `status`/`stop` only, and
historical v1/v2 broker routes are read-only.

## Stage-session capacity contract (2026-08-06)

- **Runtime support:** Claude Code exposes native `PreCompact` and `PostCompact`
  hooks and native subagents.
- **Adapter realization:** the wrapper records the portable sub-session identity,
  phase brief, exact fixed files, expected round trips, ledger, and
  `stage_authority=0`. Edit hooks enforce the ledger/file fence; compact hooks
  flush and re-read it. The dispatch-depth-1 owner aggregates all PASS/quiescent attempts
  into the route node's one marker.
- **Parity boundary:** a native Claude helper is allowed only inside its parent
  slice, returns a bounded summary, mutates serially, and never owns the stage
  gate. The registered-headless chain remains the checked fallback.
