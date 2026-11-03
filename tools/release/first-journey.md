# First journey measurement

This opt-in operator tool records installation, a focused first task, and
available usage evidence for `claude-only` and `all` profiles. It does not
change release CI. `claude-only` runs the task with Claude; `all` installs all
three runtimes and runs the task once with Codex and once with OpenCode, each in
its own demo repository.

## Commands

```sh
python3 tools/release/first-journey.py login --harness claude
python3 tools/release/first-journey.py login --harness codex
python3 tools/release/first-journey.py login --harness opencode
python3 tools/release/first-journey.py run --source published --output /tmp/journey
python3 tools/release/first-journey.py run --source published --model-free --output /tmp/journey-check
python3 tools/release/first-journey.py run --source checkout --profile claude-only --output /tmp/journey-claude
```

`--profile` (repeatable) runs only the named profile; without it both run.

Without a dedicated measurement login, `run` uses the login you already have
for each harness (`~/.claude/.credentials.json`, `~/.codex/auth.json`, or
`opencode auth export --standalone`; the harness's own `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, and `XDG_DATA_HOME` are honored). For each harness, under one lock,
the credential is copied into the private auth root (mode 0600, next to a
`.seeded` marker), used in the isolated world, handed back to the original file
if the task refreshed it and the original is still the copied value, and
deleted. A copy left by a killed run is recognised by its marker and dropped at
the next run. Credentials are never printed or exported, and a kept failure
world has its credential files removed. If a refreshed login cannot be handed
back (the original changed meanwhile, or OpenCode V2, where `auth import` keeps
existing entries), `run` says `login-writeback-failed` on stderr and in the
report: sign in to that harness again. OpenCode V2 leaves out OAuth entries that
expire within an hour so a refresh is unlikely. OpenCode starts on your
last-used model only for such a seeded login. Account percentages are shared
with every other session using that login, so judge cost from the session token
counts. `login` stays available for a separate measurement account and takes
precedence when present (it clears any stale marker).

Login is performed in an isolated environment and retains only the selected
harness credential under `/tmp/hearting-measure-auth-UID`. OpenCode V1 keeps
using `opencode/auth.json`. OpenCode V2 stores the bounded
`auth export --standalone` payload in the `credentials-v2.json` file in the
`opencode` directory; each
fresh run imports it with `auth import --standalone`, then exports it again
under the same exclusive lock after the task. V2 commands include
`--standalone`. The runner determines the generation once from the sandboxed
`--version`; if that probe fails, it reports `opencode-version-unknown` and
does not guess V1 or start an authenticated task. Never point `--auth-root`
at a real profile.

The temporary V2 import file is created mode 0600 inside the isolated world and
removed immediately after import. Credential export is captured separately
from stderr with 64 KiB per-stream limits and a 10-second timeout; secret
payloads are not written to journey logs. A hard kill during credential
rotation can require login again. A retained failure world contains its
imported credential in its private database, so remove that world when it is no
longer needed.

`--model-free` skips authentication and model calls. On Linux, `bwrap` is
required; if it is unavailable, the run reports `isolation-unavailable` and
does not fall back to a weaker model run. macOS supports only environment and
HOME separation, so its report marks filesystem protection and d3 unproven.

On Linux the bwrap world is the safety boundary, so the harness's own approval
prompts (which a headless run cannot answer) are switched off there
(`--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`,
`--auto`); other platforms keep them on. With approvals off the only boundary is
the filesystem world; the network is not unshared. OpenCode starts on the model the user
used last (`state/opencode/model.json`), because its own default can point at a
stale login.

Published runs resolve `latest` to a release tag, download that tag's
`install.sh` and checksum, and verify the bytes with SHA-256 before staging the
installer into the isolated world. Checkout runs build a local release from a
committed ref. Each sandbox step sees only the selected read-only CLI mounts.

Reports contain timed install, runtime status, doctor, task, and verification
steps; turns and canned confirmation answers; world-scoped session tokens when
available; Claude account percentages and reset epochs when available; and
failures. Account attribution is uncertain and may include other work. Raw
transcripts and credentials stay in the local experiment world; only JSON and
Markdown summaries are exported. Task output is never exported.
OpenCode V2 usage is read from `session_v2` when that table exists, summing
input, output, reasoning, cache-read, and cache-write token buckets. When it
does not exist, the runner reads the V1 `session` table. The reported total is
the sum of those exposed buckets, not a cost or account quota. Worlds are
removed on success and retained on failure by default; use
`--keep-world never|always` to change this.

If separate OpenCode databases in one world expose different generations
(V1 `session` and V2 `session_v2`), token usage is reported as unknown rather
than combining the tables under one formula.

The demo starts with `Helo` and a test expecting `Hello`. The runner records the
failing baseline and judges local refs, worktrees, and commits against the
original focused test bytes. Each harness starts one session; only that session
can resume after the structured confirmation card, up to three times. Other
questions, refusals, and incomplete work stop without retry. Sessions the
installed product starts on its own count toward the same task: tokens are summed
over every transcript in the world. A task is `completed` only when a candidate
passes the original focused test and the product left a route record
(`.runtime/routes/rt-*.json`) in the world; a fix without one is reported as
`fixed-without-route-evidence`. A route the product recorded on its own for a
turn that changed files without one (`utilities/turn_change_record.py`, for
example after a shell `sed -i`) counts the same and is also reported as
`route_evidence_auto`.

The sandbox is a PID namespace, so everything left in it dies when the agent
CLI exits, while on a user's machine the product's detached hook steps keep
running. Each sandboxed turn therefore runs under a small subreaper that waits
up to 30 seconds for those descendants after the CLI exits. Turn and task
seconds are the CLI's own time; the wait is reported as `settle:<harness>:<n>`
steps (`left_running` when the bound ended it).

Inside the sandbox the world keeps its host path, while the real HOME and
`/home/nas` are empty tmpfs mounts; the report's isolation checks show this.
PATH holds the installed launchers, the selected CLIs and the standard shell
tools (`grep`, `sed`, `ls`, ...), never herdr; the fresh world has no Cairn
setting, compute-host inventory or memory remote. A completed run therefore
also shows that the basic task needs none of these optional pieces.
