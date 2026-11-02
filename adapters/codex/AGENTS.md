# AGENTS.md — Codex Adapter Bootstrap

This is a Codex adapter router, not the portable source of truth. The semantic
hierarchy is `core/capabilities/roles -> {Claude, Codex, OpenCode}`; adapters
are siblings and none is another's reference implementation. Edit core first.

The harness root `<agent-home>` is the active `AGENT_HOME`, else
`${CODEX_HOME:-$HOME/.codex}/hearting` (shared resolver
`utilities/agent-home.sh`). Don't probe `<cwd>/core/CORE.md` or report it
missing. `preflight.sh` means `<agent-home>/adapters/codex/bin/preflight.sh`.

<!-- BEGIN generated from core/fragments/bootstrap-body.md by tools/sync-bootstrap-dispatch.py; edit the source -->
## Purpose

Hearting keeps work consistent with the user's intent, whichever agent runs
it. You do not need to memorize its procedures: one command starts tracked
work, the runtime prints your next action, and its hooks wake you when work
you started reports back. Paths are relative to the harness root named above.
Read a document only when the task needs it.

## Judgment

- Look for what already exists before relying on your own knowledge:
  the repository's research, analysis, and briefing artifacts, its code,
  memory, and earlier outputs. An answer from memory alone says so and flags
  its risky specifics; this holds for explanations too.
- Stay inside the user's intent and the existing flow. Ask only for genuinely
  non-obvious or destructive choices, and use structured input only when the
  choice changes the goal, architecture, UX, large scope, or an external
  system. Continue low-risk reversible work autonomously, with its implied
  validation, records, commit, and push. If structured input is unavailable,
  ask one concise ordinary question; a helper never owns user input or
  approvals.
- Do not build your own waiting: no sleep or poll loops, background watchers,
  or self-scheduled wake-ups for work the runtime tracks.
- Native subagents and registered headless worker dispatch are separate
  surfaces; a restriction on one never silently extends to the other. Preserve
  model role, intensity, dispatch depth, tests, safety, and validation on
  fallback.
- Use portable model roles, never vendor model names, in shared artifacts.
  Runtime-owned credentials, sessions, logs, caches, databases, and config
  stay out of repositories.
- For harness changes, edit the portable sources (`core/`, `capabilities/`,
  `roles/`) first and expose changes before committing; commit and push the
  validated change in the same turn (`core/OPERATIONS.md §5.11`).

## Starting work

Route by `core/WORKFLOW.md §0.2`: the capability that owns the artifacts is
the primary. The main session decides only whether the work is direct
(§0.2.1): use `--shape direct` for one atomic, reversible inline change;
otherwise omit `--shape` and let the frame decide. Start with `hearting run
capability-route compose`:

```
hearting run capability-route compose --start --prompt-file <task> [--shape direct]
```

`hearting run <utility>` uses `AGENT_HOME` when set, else the installed
release. Other flags are hints; a refusal names what to add, such as
`--campaign-key`. For a user's material request that is not yet approved,
present the §0.4 five-field card before starting (진행 / 수정 / 중단); a
request from a peer session needs only the `[경로]` line compose prints plus
one clause of scope, unless the work is destructive or external-facing.
Dispatch depth 3 is forbidden.

## Following the runtime

Do the next action the command or its hook prints:

- `parent_next=end-turn`: end the turn with no wait, poll, or recap; the
  runtime wakes you.
- `parent_next=bounded-wait`: run `parent_next_command` once.
- `required_action`, `next_step`, `resume_command`, `correction_command`: do
  exactly what they name.
- No directive in view: read the printed `receipt_file`. A missing directive
  is not `end-turn`, so keep launch output unfiltered.

To message another session, run `hearting run peer-steward prompt
<name-or-pane> --body-file <file>`; reply text sends nothing.

## Finishing work (Tracked-Workflow Continuation)

Process exit is not workflow completion (`core/WORKFLOW.md §0.6`): a workflow
is done only when every terminal node holds its completion gate. Keep
unfinished work here or in its registered continuation. A receipt, queue
entry, or ACK is not execution. When a result arrives, review it and do the
approved follow-through. Close material work with the first-report content
required by §0.5; its card is optional. Lead with the result or the decision
needed; keep PIDs, logs, digests, and paths in the evidence.

## Memory and context

A main prompt may carry memory candidates (headline and ID): ignore unrelated
ones and read a relevant record in full before using it, and retrieve a
pending obligation in full before applying it. Run the `session-tidy` Skill
when context is filling or before compact, before handing work to another
session, and when a large task ends. Context pressure never lowers intensity,
dispatch depth, model role, tests, safety, or validation.

## Where to look

| Need | Read |
|---|---|
| Layers, harness root, artifact root | `core/CORE.md` |
| Routing §0.2, entry card §0.4, report §0.5, completion §0.6 | `core/WORKFLOW.md` |
| Intensity, QA, model roles, artifacts, Skills | `core/CONVENTIONS.md` |
| Git, worktrees, dispatch, receipts §5.10a, frame questions §5.10b, continuation §5.12, other sessions §5.14 | `core/OPERATIONS.md` |
| Memory | `core/MEMORY.md` |
| Capabilities, roles, modes | `capabilities/README.md`, `roles/README.md`, `roles/MODES.md` |
| Route health | `hearting run workflow-supervisor status --route <id> --json` |
| Compute hosts | `compute-hosts` and `~/.config/hearting/compute-hosts.yaml`; `run` receipts include GPU headroom |
| This runtime's commands and edge cases | the adapter `README.md` and `ADAPTATION.md` named below |

Resolve the artifact root with `utilities/artifact-root.sh`; linked worktrees
write the primary checkout's `.agent_reports/`, and legacy `.claude_reports/`
is only a fallback. For runtime-surface or parity changes, check current
official documentation, then the local realization and its fallback; never
infer support from another adapter.

## Response Policy

Portable behavior contract = `roles/response-policy.md`.

- **Audience-language first** — user artifacts use the user's current
  communication language unless a stronger audience or repository contract
  applies. Code, comments, commits, and PR text follow the repository's
  language, even when a runtime language setting names another.
- **Answer first, bounded** — lead with the answer; keep unrequested
  explanation to about five lines unless the user asked for depth or the turn
  closes material work.
- **Plain address** — ordinary words over harness jargon, conclusion first,
  no unexpanded internal terms. Name other sessions by Fleet tag and pane (SID
  if needed), role in parentheses.
- Keep responses concise, verify before asserting, follow existing
  conventions, and match promises with same-turn action.
<!-- END generated from core/fragments/bootstrap-body.md -->

## Codex Notes

- Before using a capability or mode, run
  `adapters/codex/bin/preflight.sh capability-info <capability>` or
  `preflight.sh mode-info <family/mode>` and obey its contract:
  named `tool_contract`, `tool_contract_check`, `runtime_surface`, and `fallback`.
  Before claiming a tool works, run its check; exit 69 means unavailable, so
  use the reported fallback and never borrow a Claude-native tool to claim
  Codex parity.
- Ask structured questions, including the §0.4 card, with
  `request_user_input`. It submits empty after 120 s; an empty answer is no
  decision, so restate the question at turn end.
- Completion arrives through Codex's native queue for this thread. Detached
  shell output does not wake you; for other long-running work follow
  `preflight.sh loop-info runtime-watch`.
- If the memory prompt hook is unavailable, record `recall` or `skip` with
  `preflight.sh recall-gate <cwd> ...`. `$CODEX_HOME/config.toml` is
  runtime-owned.
- Command surface, tool checks, hook trust, token budget, and edge cases:
  `adapters/codex/README.md` (Session Command Reference) and
  `adapters/codex/ADAPTATION.md`. Command output is authoritative.

## Compatibility Boundary

Claude/OpenCode files are sibling references, not Codex bootstrap input. Portable
meaning comes from `core/`, `capabilities/`, and `roles/`; map it to Codex
tools, approval, sandbox, lifecycle, and discovery.
