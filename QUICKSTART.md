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
- You do not need herdr, an outside artifact tool, a NAS, remote memory
  exchange, or a GPU.

Tested so far: CI checks the installation for Claude Code, Codex, and OpenCode
on Ubuntu, and the measured runs below used Linux. macOS has not been checked end
to end, and Windows (Git Bash) is experimental.

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

To try it on a ready-made project, copy `greet.py` and `focused_test.py.txt`
from the [demo files](tools/release/fixtures/first-journey/) into an empty folder,
rename `focused_test.py.txt` to `test_greet.py`, and commit both in a new Git
repository. The test fails until `greet.py` returns `Hello` instead of `Helo`;
the expected change is one line:

```diff
-    return "Helo"
+    return "Hello"
```

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

One measured run per task, not a promise. The numbers below come from
[`tools/release/first-journey.py`](tools/release/first-journey.md): published
release v3.82.17 installed into an isolated Linux environment on 2026-11-06,
one session per runtime, no confirmation card answered. Times are model work only. They leave
out sign-in and the time you spend reading and replying.

| Runtime | Small fix (typo, focused test, diff) | Medium task (several files) |
|---|---:|---:|
| Claude Code | 20.7 s | 24.6 s |
| Codex | 22.8 s | 35.1 s |
| OpenCode | 9.5 s | 20.2 s |

- The medium task fixed a typo, added an optional argument and a new test file,
  updated the README, ran the full tests, and showed the diff in one session.
  Each runtime made the change in place, and Hearting recorded it automatically.
- Install took 6.9 s for Claude Code only and 13.0 s for all three runtimes.
- Every run succeeded, but the sample is small: one run per task, one machine,
  and one account per runtime.
- Token counts are collected, but each runtime counts differently (Codex
  input includes cache; Claude lists cache separately), so do not compare them
  across runtimes.
- We did not measure usage against a subscription quota. The shared quota gauge
  also moves with your other sessions, and tokens cannot be converted into a
  quota percentage.

When background workers finish a task, their completion summary shows the
elapsed time and session tokens, and marks counts that are missing or partial.
A tiny fix that Hearting records automatically, like the one above, does not get
this line.

## If something blocks you

See the [FAQ](FAQ.md) for `PATH`, Python versions, single-runtime setups,
permissions, refused actions, and unknown progress. Tell the agent the
original request and the failure it reported.
