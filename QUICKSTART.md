# Quickstart

This walks one small fix through Hearting, from install to a finished result.
The [README](README.md) explains what Hearting is; this page is the shortest
path to seeing it work.

## Before you start

- Linux or macOS. Windows (Git Bash) is experimental.
- Git, `curl` or `wget`, and Python 3.11 or later with PyYAML for that Python
  (`python3 -m pip install --user pyyaml`, or your OS package such as
  `python3-yaml`). The installer checks the Python version.
- At least one agent CLI, installed and signed in: Claude Code, Codex, or
  OpenCode. Claude Code alone is enough.
- You do not need herdr, Cairn, a NAS, remote memory exchange, or a GPU.

<!-- TODO(public-readiness stage 5): add the tested OS x runtime matrix. -->

## Install and check

```sh
curl -fsSL https://github.com/dmlguq456/hearting/releases/latest/download/install.sh | sh -s -- --runtime claude
~/.local/bin/hearting runtime doctor --runtime claude --strict
```

Drop `-s -- --runtime claude` and use `--runtime all` in the second command to
install for all three runtimes. The installer does not change the `PATH` of a
shell that is already open, so use the `~/.local/bin/...` paths until that
directory is on your `PATH`.

The installer prints one note you should read: background Claude workers run
in bypass mode by default, and the same note names the file that switches them
to allowlist mode. See [Permissions](README.md#permissions). If you also install
OpenCode and keep Claude Code user rules, another note says those rules now
apply in OpenCode too; the same section says how to turn that off.

`runtime doctor` names an optional piece that is missing, such as PyYAML or
herdr, and what does not work without it. A herdr line does not matter for this
walk-through.

## Fix one typo

Open your agent CLI inside a Git project that has a small bug and an existing
test, then ask in your own words:

> Fix the typo in the greeting output, run the existing test, and show me the diff.

You do not type any Hearting command. The agent keeps the change local, runs
the test, and tells you what it ran and what it did not. A passed test and a
test that could not run are reported as different outcomes.

<!-- TODO(public-readiness stage 1): link a published demo repository with the expected patch. -->

## Watch it finish

In a second terminal:

```sh
~/.local/bin/fleet --once    # one plain snapshot; no curses needed
~/.local/bin/fleet           # the live full-screen view
```

For a worker's raw output, ask the agent for its attempt id and run
`hearting run attempt-view <attempt-id> --follow`.

## Read the result

Read the changed files, the verification result, and anything left undone.
A small, reversible fix like this one is made in your own folder, and Hearting
records it automatically. A bigger change usually goes to a separate git
worktree beside your project, `<project>-wt/<name>`, on its own branch, and
your own folder stays as it was; the result names that folder and branch and
whether the change is committed.
For anything beyond a one-line fix, the plan, evidence, and report stay in the
project's `.agent_reports/` folder. Do not delete it to unblock a task; the
next stage reads the evidence it holds.

## What to expect

One measured run, not a promise, and due to be measured again right before
v4.0. The numbers below come from
[`tools/release/first-journey.py`](tools/release/first-journey.md): published
release v3.66.2 installed into an isolated Linux environment, one session per
runtime, no confirmation card answered. Times are model work only. They leave
out sign-in and the time you spend reading and replying.

| Runtime | Small fix (typo, focused test, diff) | Medium task (several files) |
|---|---:|---:|
| Claude Code | 33.7 s (a rerun: 27.5 s) | 47.8 s |
| Codex | 58.1 s (a rerun: 60.8 s) | 142.3 s |
| OpenCode | 45.0 s | 165.7 s |

- The medium task fixed a typo, added an optional argument and a new test file,
  updated the README, ran the full tests, and showed the diff in one session.
- Install took 6.4 s for Claude Code only and 13.4 s for all three runtimes.
- Every run succeeded, but the sample is small: a few runs, one machine, and
  one account per runtime.
- Token counts are collected, but each runtime counts differently (Codex
  input includes cache; Claude lists cache separately), so do not compare them
  across runtimes.
- We did not measure usage against a subscription quota. The shared quota gauge
  also moves with your other sessions, and tokens cannot be converted into a
  quota percentage.

<!-- TODO(public-readiness stage 4): replace this with the completion line (elapsed time and tokens) once it ships. -->

## If something blocks you

See the [FAQ](FAQ.md) for `PATH`, Python versions, single-runtime setups,
permissions, refused actions, and unknown progress. Tell the agent the
original request and the failure it reported.
