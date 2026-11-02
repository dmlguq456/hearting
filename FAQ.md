# FAQ

## Do I need all three agent CLIs?

No. Claude Code alone is a supported setup. A separate session on the same
runtime can provide independent review; with more runtimes installed,
Hearting can place workers and reviews across them. In the
[measured run](QUICKSTART.md#what-to-expect), a Claude-only install and a small
fix completed.

<!-- TODO(public-readiness stage 5): replace this with the tested OS x runtime matrix. -->

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
as current. Your runtime or a missing configuration can also refuse for its
own reasons. The message names the reason. Give it to the agent so it can
continue from the failure within your original request; you do not need to
learn internal commands or start a second task.

## Why did .agent_reports appear in my project?

It stores plans, evidence, results, and handoffs shared by every runtime and
every git worktree of the project. An older `.claude_reports/` folder is still
read as a legacy alias. Do not delete these folders to unblock a task.

## hearting or fleet is not found

The launchers are in `~/.local/bin`. Add that directory to your `PATH` in your
shell's startup file; Hearting does not edit it for you. For the current
terminal, call them by full path, for example
`~/.local/bin/hearting runtime doctor --runtime claude --strict`.

## Which Python version do I need?

Python 3.11 or later, with PyYAML installed for that interpreter
(`python3 -m pip install --user pyyaml`). The installer script only checks that
`python3` exists, so an older Python can fail later with an import error such
as `tomllib` not found. Check `python3 --version` first.

## Can I use it without herdr, Cairn, a NAS, or GPUs?

Yes; these are optional. Without herdr the related hooks exit quietly. Cairn
reports itself unavailable when it is not set up. `compute-hosts` stays a
commented template until you name a host, and a compute request without a host
fails with a clear error. Memory stays local unless you configure an exchange
in `~/.config/hearting/memory-sync.json`.

## Does the language setting translate every label?

No. Conversation follows your language. `title_language` in
`${XDG_CONFIG_HOME:-~/.config}/hearting/fleet.json` and `FLEET_NOW_LANG` set
the language of Fleet titles and its NOW line only. A few Fleet status words
are still Korean-only; moving them to a shared translation table is planned.

## Why can progress be unknown? Can I read the original output?

A collector can lack data or fail. Fleet shows `?` for a value it could not
read; that is not zero activity and does not by itself mean the task failed.
Ask the agent for the real attempt id, then run
`hearting run attempt-view <attempt-id> --follow`. The task's handoff under
`.agent_reports/` also records what happened.

## How much will a task cost?

There is no fixed estimate. The start card describes the workflow, and Fleet
shows the token counts and usage windows that the runtimes report. That is not
a bill or a quota forecast. The only timings we have are in
[What to expect](QUICKSTART.md#what-to-expect), with their limits.

## Why do I need a new session after an update?

Switching the active release and reloading instructions in an open session are
different events. Run `~/.local/bin/hearting runtime status --runtime all` and
follow the reported `session_action`. Do not assume a runtime reloads all
instructions live.

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
