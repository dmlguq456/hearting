# CLAUDE.md — Claude Adapter Bootstrap

Claude adapter bootstrap, not the portable source of truth: `core/capabilities/roles -> {Claude, Codex, OpenCode}`, adapters are siblings; edit portable sources first.

The harness root is `AGENT_HOME`: for a managed release,
`${XDG_DATA_HOME:-$HOME/.local/share}/hearting/current` or an existing route's
sealed release root. `$HOME/.claude` is only the runtime projection, even when
its harness files are symlinks; it is not `AGENT_HOME`.

<!-- BEGIN generated from core/fragments/bootstrap-body.md by tools/sync-bootstrap-dispatch.py; edit the source -->
## Purpose

Hearting keeps work aligned with the user's intent. Its runtime prints the
next action and wakes you when tracked work reports back. Paths use the
harness root above. Read documents only as needed.

## Judgment

- Consult existing artifacts, code and memory first. When answering from
  memory alone, say so and flag uncertain specifics, including in explanations.
- Stay inside the user's intent and the existing flow. Ask only for genuinely
  non-obvious or destructive choices, and use structured input only when the
  choice changes the goal, architecture, UX, large scope, destructive work,
  or an external-system outcome. In an active "do X" flow, continue low-risk
  reversible work autonomously, with its implied validation, records, commit,
  and push. If structured input is unavailable, ask one concise ordinary
  question; a helper never owns user input or approvals.
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
  validated change in the same turn (`core/OPERATIONS.md §5.11`). A person or
  a scheduled loop starts behavior drills (`loops/drill`); do not start one
  on your own.

## Starting work

In this Git worktree, handle one reversible outcome inline, including a small
feature's source, tests and README: known scope, focused checks; no shared
contract change, long resource work, artifact handoff or independent verifier.
Read/edit/check/reply; recorded automatically without task/compose/finish/capability-info calls.
Compose before scope grows. Other work starts with:

```
hearting run capability-route compose --start --prompt-file <task>
```

Choose purpose and sequence in every folder; Git status changes placement only.
Small non-Git document corrections use direct in place; multi-step document
work uses the frame, retaining known deadlines and writer responsibilities.
Keep code and document results at their stated project path (default cwd),
from the first draft; keep plans, reviews, versions and target references in
`.agent_reports`.

Use the receipt's `runtime` command prefixes. `hearting run <utility>` uses
`AGENT_HOME`, else the installed release. Flags are hints unless the person
or a frame already chose the shape; honor it, using `--shape staged --graph
<stage,…>` for person-named stages (§0.2.1). Refusals name missing inputs.
Before unapproved material work, present the §0.4 intent card (진행 / 수정 /
중단). Peer requests need only compose's `[경로]` line and scope, except for
destructive or external-facing work. Dispatch depth 3 is forbidden.

## Following the runtime

Do the next action the command or its hook prints, in this order:

1. `required_action` and `next_step`: do what they say first, such as
   presenting a registered question.
2. Then `parent_next`. `end-turn`: end the turn with no wait, poll, re-run,
   or recap; the runtime wakes you. `bounded-wait`: run `parent_next_command`
   once.
3. `resume_command` and `correction_command` are handles, not orders: run
   `resume_command` when woken or told to, and answer a BLOCKED owner with
   `correction_command --message-file <file>`.

No directive in view: read the printed `receipt_file`. A missing directive
  is not `end-turn`, so keep launch output unfiltered.

To message another session, run `hearting run peer-steward prompt
<name-or-pane> --body-file <file>`; reply text sends nothing.

## Finishing work (Tracked-Workflow Continuation)

Process exit is not workflow completion (`core/WORKFLOW.md §0.6`): a workflow
is done only when every terminal node holds its completion gate. Keep
unfinished work here or in its registered continuation. A receipt, queue
entry, or ACK is not execution. When a result arrives, review it and do the
approved follow-through. Close material work with the §0.5 contract: result in one or two sentences,
important remaining failures, unfinished work or limits, at most one necessary
decision, and one pointer to the detailed record. Preserve unknowns and explicit
requested output. Keep the full runtime summary as agent context and in the
record; do not reproduce it for the person. Keep PIDs, logs, and digests there.

## Memory and context

A main prompt may carry memory candidates (headline and ID): ignore unrelated
ones and read a relevant record in full before using it, and retrieve a
pending obligation in full before applying it. Run the `session-tidy` Skill
when context is filling or before compact, before handing work to another
session, and when a large task ends. Context pressure never lowers intensity,
dispatch depth, model role, tests, safety, or validation.

## Where to look

- Layers, harness root, artifact root: `core/CORE.md`
- Routing §0.2, entry card §0.4, report §0.5, completion §0.6: `core/WORKFLOW.md`
- Intensity, QA, model roles, artifacts, Skills: `core/CONVENTIONS.md`
- Git, worktrees, dispatch, receipts §5.10a, frame questions §5.10b, continuation §5.12, other sessions §5.14: `core/OPERATIONS.md`
- Memory: `core/MEMORY.md`
- Capabilities, roles, modes: `capabilities/README.md`, `roles/README.md`, `roles/MODES.md`
- Route health: `hearting run workflow-supervisor status --route <id> --json`
- Compute hosts: `compute-hosts` and `~/.config/hearting/compute-hosts.yaml`; `run` receipts include GPU headroom
- This runtime's commands and edge cases: the adapter `README.md` and `ADAPTATION.md`

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
  closes material work. Report only usable results, necessary decisions, or
  material failure or risk; unchanged status, progress, and handoffs stay in the
  record.
- **Plain address** — ordinary words over harness jargon, conclusion first,
  no unexpanded internal terms or internal numbers unless needed. Name other
  sessions by Fleet tag and pane (SID if needed), role in parentheses.
- **Decisions** — proceed with a recommended in-scope reversible option and say
  which and why in one line; state usage as a fact and continue; ask only what
  only the person can decide, in one self-contained request with
  the choice, differences, recommendation and why, and what waits without an answer.
- Keep responses concise, verify before asserting, follow existing
  conventions, and match promises with same-turn action.
<!-- END generated from core/fragments/bootstrap-body.md -->

## Claude Notes

- Register ordinary questions with `hearting run questions ask`; use the
  shared response policy. Ask structured exceptions, including the §0.4 card, with `AskUserQuestion`
  (options 진행(권장) / 수정 / 중단); a plain-text card is the fallback.
- CronCreate, ScheduleWakeup, Monitor, and backgrounded waits are not ways to
  watch Hearting work; the runtime hooks wake this session.
- A Codex job the openai-codex plugin detaches runs in the plugin's own queue,
  not in `jobs.log` (Fleet shows it read-only); send Codex work that needs
  tracking or gates through registered dispatch.
- If the memory prompt hook is unavailable, record `recall` or `skip` with
  `mem recall-gate`.
- Hooks, statusline, Skills projection, and edge cases:
  `adapters/claude/README.md` and `adapters/claude/ADAPTATION.md`.
