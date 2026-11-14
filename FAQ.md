# FAQ

## Do I need all three agent CLIs?

No. Claude Code alone is a supported setup. A separate session on the same
runtime can provide independent review; with more runtimes installed,
Hearting can place workers and reviews across them. In the
[measured run](QUICKSTART.md#what-to-expect), a Claude-only install and a small
fix completed.

CI checks the installation for all three runtimes on Ubuntu, and the measured
runs used Linux. macOS has not been checked end to end, and Windows (Git Bash)
is experimental.

## What permissions do background workers have?

Workers that Hearting dispatches in the background run Claude Code in bypass
mode (`--permission-mode bypassPermissions`) by default, because nobody is
there to answer a permission prompt. The installer prints a note about this.
Sessions you start yourself keep your own runtime permissions.

To use allowlist mode, edit
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/dispatch-defaults.yaml`. In an
existing `headless:` block, change the value below. If the block is absent,
add both lines:

```yaml
headless:
  claude_permission_mode: allowlist
```

Do not add a second `headless:` key or an inline mapping. Allowlist runs
workers with `acceptEdits` and a bounded tool list: Hearting's own utilities,
read-only git, test runners, and edits inside the worktree and the artifact
root. Where bypass cannot apply (root or sudo, or
`permissions.disableBypassPermissionsMode` in any Claude settings scope),
Hearting switches to allowlist by itself and records the mode it used. If the
file is invalid or unreadable, the shipped default, bypass, applies;
`hearting config status` shows whether your file was read.

`DISPATCH_DEFAULTS_CONFIG` points to a different file. This setting covers
Claude Code workers only. Codex workers run in Codex's `workspace-write`
sandbox without approval prompts; OpenCode workers start from your own
OpenCode permission configuration. Peer sessions started with
`peer-steward start` also default to bypass; set `child_permission_mode:
inherit` under `steward:` in the same file to keep the parent's mode. There is
no one-line command to change these settings.

## Why was an action refused?

By default the dispatcher carries on with current inputs when a difference stays
inside the work you approved. It refuses what would cause an observed harm:
writing or granting outside the approved scope, touching another route's work,
relaunching an attempt that already started, or treating a superseded result
as current. A record Hearting could not read at that moment is not a
refusal: the message asks to wait and run the same command again. Your
runtime or a missing configuration can also refuse for its own reasons. The
message names the reason. Give it to the agent so it can
continue from the failure within your original request; you do not need to
learn internal commands or start a second task.

## I don't see the change in my project folder

When a request changes source code, Hearting usually makes the change in a
separate git worktree beside your project, `<project>-wt/<name>`, on its own
branch. Your folder, branch, and uncommitted files stay as they were. The
result names the folder and branch that hold the change and whether it is
committed. Review it there, and ask the agent when you want it in your own
branch. A small, reversible fix is made in your own folder instead, and
Hearting records it automatically; there is nothing to start or finish.

## Why did .agent_reports appear in my project?

It stores plans, evidence, results, and handoffs shared by every runtime and
every git worktree of the project. An older `.claude_reports/` folder is still
read as a legacy alias. Do not delete these folders to unblock a task. Open its
`README.md` (or `index.html` in a browser), or run `hearting reports`, to see
the latest results; `hearting open <number or words>` opens one.
Optional: `hearting view` starts a local viewer (Ctrl-C to stop); `hearting open` shows its URL while it runs.
It uses port 8765; if that port is occupied, `hearting view --port 0` picks a
free one, or `--port 8766` chooses one.

Planned work uses one cycle folder,
`campaigns/<campaign>/<cycle>/artifacts/`, holding `plans/` (plan, checklist,
dev and test logs, evidence, final report), `research/`, `analysis_project/`,
`analysis/`, `spec/` (the current revision is shared under
`.agent_reports/shared/spec/`, and a revision keeps the previous one),
`documents/`, and `experiments/`. Small direct changes and automatic turn
records go to the campaign's daily journal (`journals/YYYY-MM-DD.md`) instead;
`hearting run artifact-journal dry-run --artifact-root <root>` previews older
small cycles that could be folded into it, and explicit `apply --cycle <id>` /
`restore --cycle <id>` back them up and restore them. A campaign can keep a
`STATUS.md`; Hearting never creates it, and Fleet shows it on the project page.

## hearting or fleet is not found

The launchers are in `~/.local/bin`. Add that directory to your `PATH` in your
shell's startup file; Hearting does not edit it for you. For the current
terminal, call them by full path, for example
`~/.local/bin/hearting runtime doctor --runtime claude --strict`.

## Which Python version do I need?

Python 3.11 or later, with PyYAML installed for that interpreter
(`python3 -m pip install --user pyyaml`, or your OS package such as
`python3-yaml`). The installer stops with a clear message when `python3` is
older than 3.11. It does not install PyYAML; `hearting runtime doctor` says
when it is missing. Without it, work that starts with the planning step stops
before planning, while a one-step direct change still runs.

## Can I use it without herdr, a NAS, GPUs, or an outside artifact tool?

Yes; these are optional. Messages, requests, and watching between sessions run
on Hearting's own records and need neither herdr nor tmux, and neither does the
panel. herdr or tmux is used to open and close peer windows, title panes, and
carry question notifications; without either those are off (a desktop session
still gets question notifications through `notify-send`), and
`hearting runtime doctor` names what is off. Sending finished artifacts to an
outside tool reports itself unavailable when none is connected. `compute-hosts` stays a
commented template until you name a host, and a compute request without a host
fails with a clear error. Memory stays local unless you configure an exchange
in `~/.config/hearting/memory-sync.json`.

## Can I turn the panel off?

Yes. `hearting session shell disable` makes new Claude Code, Codex, and
OpenCode terminals launch the native CLI as before, and
`hearting session shell enable` turns the panel on again;
`hearting session shell status` shows the current choice. Running sessions keep
what they started with. Updates keep your choice. To keep the panel but give
the native area more room, press **»** beside the panel's gear to fold it into
a narrow strip, or change its width and position under **⚙**
([docs/session-panel.md](docs/session-panel.md#narrow-windows-and-folding)).

## How do sessions pass work and messages to each other?

Ask in your own words: “Ask session [b2] to re-run the tests and report back.”
The number is the one Fleet and the panel show for that session. The agent
sends a **request**: it gets a number, the other session receives one short
line and opens the body itself, sends partial reports while it works and a
final report (or a rejection) when done, and each report wakes the session that
asked. The asker can send a new revision or cancel. Both panels list the
request until it closes. Short notes and watching (“tell me when [e8]
finishes”) work the same way. None of this needs herdr or tmux.

In a terminal with the panel, an idle session on any runtime is reached at
once. Codex is reached at once anywhere, through its own thread queue. Outside
the panel's terminal, an idle Claude Code session is reached at its next turn
and OpenCode at its next idle or tool callback; the request stays open and
listed meanwhile. Fleet does not list requests yet, and you cannot send one
from Fleet yet. Details:
[docs/session-panel.md](docs/session-panel.md#requests-and-messages).

## Does the language setting translate every label?

Almost. Conversation follows your language. The fixed text Hearting prints
for you, such as the route card at the start of a request, the one-line
direction notice, the summary at its end, and Fleet's screens, comes from one
shared catalog in English and Korean. It is English unless you declare Korean
once: `"title_language": "ko"` in
`${XDG_CONFIG_HOME:-~/.config}/hearting/fleet.json`, `FLEET_NOW_LANG=Korean`,
or Korean as Claude Code's `language` setting. The same declaration sets the
language of Fleet's session titles and NOW lines. Lines meant only for the
agent, such as internal diagnostics, are not translated.

## Why can progress be unknown? Can I read the original output?

A collector can lack data or fail. Fleet shows `?` for a value it could not
read; that is not zero activity and does not by itself mean the task failed.
Ask the agent for the real attempt id, then run
`hearting run attempt-view <attempt-id> --follow`.
`hearting run workflow-supervisor status --route <route-id> --json` says which
stage a request is in. The task's handoff under `.agent_reports/` also records
what happened.

## How much will a task cost?

There is no fixed estimate. The start card describes the workflow, the
summary at the end of a dispatched request has one cost line (elapsed time and
session tokens; a count the runtime did not report stays `unknown`), and Fleet
shows the token counts and usage windows that the runtimes report. None of
that is a bill or a quota forecast. What Hearting itself adds was measured on
Codex on one machine, three pairs each: the first response about 0.7 s slower
than without Hearting and about 2.4K more input tokens per request; a small
two-turn bug fix took about as long either way (38 s with Hearting, 39 s
without). The only end-to-end timings we have are in
[What to expect](QUICKSTART.md#what-to-expect), with their limits.

## Why do I need a new session after an update?

Switching the active release and reloading instructions in an open session are
different events. Run `~/.local/bin/hearting runtime status --runtime all` and
follow each runtime's `next=` value (`session_action` in `--json`). Do not
assume a runtime reloads all instructions live.

## Does Hearting change what OpenCode reads?

Yes, in one place. Hearting manages OpenCode's global `AGENTS.md` for its own
instructions. When `~/.claude/rules/` (or `$CLAUDE_CONFIG_DIR/rules/`) holds
Claude Code user rules, Hearting puts those rules before its instructions in
that file. Rules limited to certain paths are left out, and the
rule files are only read. The rule text then also reaches the model provider
your OpenCode uses. To stop it, add these lines to
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/dispatch-defaults.yaml`:

```yaml
user_rules:
  opencode: off
```

The next install, update, or `hearting runtime refresh --runtime opencode`
puts the plain instructions back. If you had edited that `AGENTS.md`, your
version is kept beside it as `AGENTS.md.edited-<time>`.

On Codex, Hearting writes into a file of yours only when you keep a non-empty
`~/.codex/AGENTS.override.md`: Codex reads only that one global file, so
Hearting appends its instructions below your text in one marked block. Update
replaces only that block and uninstall removes it; a missing or empty override
is never created. Background workers on every runtime also get your global
rules.

## Is Windows supported?

Windows (Git Bash) is experimental. `fleet --once` and `fleet --json` work
there because they do not need curses. Linux and macOS are the first-release
scope.

## What is the relationship to OpenRig?

Hearting focuses on connecting a request to verification and a durable handoff
across Claude Code, Codex, and OpenCode. OpenRig's terminal views and
persistent team operation are a useful reference for visibility. We compared
only OpenRig v0.6.13, not its current release, and have not measured a speed,
safety, or cost advantage either way.
