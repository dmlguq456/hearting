# Install Layout

This harness is runtime-neutral. Its managed release or maintainer checkout
lives outside vendor runtime homes, and each runtime home projects that source
through native discovery paths and adapter bootstrap files.

> **Execution note — use `harness install` / `harness verify`.** This document
> no longer contains shell recipes to copy or a manual verification battery.
> The public `install.sh` is a self-contained GitHub Release asset. Root
> `install.sh` only redirects the former raw-main URL to that release path.
> `tools/install/harness.sh` automates activation, verification, updates,
> status, and removal through `runtime activate|status|refresh|doctor` and the
> legacy `install`/`verify`/`update`/`status`/`uninstall` subcommands. The
> installer PRD at `.agent_reports/spec/harness-installer/prd.md` supersedes
> the old manual recipes.
>
> **Design note — this document preserves contracts, not procedures.** It
> explains what maps where, why the mapping exists, and which cases are unsafe.
> The activation sections of the productization PRD own the full
> surface-by-channel decision matrix and channel specifications. This page
> keeps only their summary and local facts that are unique to it, such as
> concrete Windows paths and the fleet contract.

## Target Layout

```text
$HOME/.local/share/hearting/releases/<version>/  # managed immutable release
$HOME/.local/share/hearting/current              # atomic active pointer
$HOME/hearting/                                       # canonical maintainer checkout
$HOME/agent_setting/                                  # legacy checkout fallback
$HOME/.claude/              # Claude Code runtime home
$HOME/.codex/               # Codex runtime home
$HOME/.config/opencode/     # OpenCode global config home
$HOME/.local/share/opencode/  # OpenCode data home (DB, logs, snapshots)
$CODEX_HOME/.harness/bin/codex # Hearting-owned protected Codex ingress
$HOME/.local/bin/codex         # vendor-owned Codex command binding
```

Do not make `$HOME/.claude`, `$HOME/.codex`, or `$HOME/.config/opencode` the canonical repo. Those directories contain runtime-owned state such as credentials, sessions, logs, SQLite databases, caches, and shell snapshots.

Codex ingress is kept under `$CODEX_HOME/.harness/bin` so a vendor updater may
replace `$HOME/.local/bin/codex` without overwriting Hearting code. Shell profile
management is opt-in and byte-preserving; `manual`/`deny` leave the profile
untouched and report the exact PATH source needed for the current terminal.
`legacy-inplace-v1` is a separately authorized degraded mode. Claude remains
vendor-owned and has no Hearting executable ingress.

## Report Bundle Store

Installation also initializes one cross-runtime report store. Pass an explicit
absolute root when storage placement matters:

```bash
harness install all --report-bundle-root /srv/hearting/report-bundles
```

The installer records it once in
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/report-bundle.json`; later
install/update invocations preserve the file byte-for-byte. Without an option,
the initial value comes from `REPORT_BUNDLE_ROOT`, then falls back to
`${XDG_DATA_HOME:-$HOME/.local/share}/hearting/report-bundles`. Runtime
resolution gives the environment variable precedence over the stored value.
`harness verify` fails if the config or configured directory is invalid.

Published bundles use
`<root>/<project>/<experiment>/<version>/report/{index.html,REPORT.md,report_manifest.json,media/}`.
Consumers store stable identifiers and `report/index.html`, never this absolute
root.

## Shared Memory Exchange

A second machine joins an existing memory exchange with one command after
install:

```bash
harness memory join --remote-url git@github.com:<owner>/<repo>.git \
  --exchange-dir /srv/hearting/memory-sync/exchange
```

This records the policy in
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/memory-sync.json` — the portable
file every adapter reads, since only one runtime keeps a settings file able to
carry environment defaults — and then runs `mem migration join --apply --sync`,
which seals a fresh epoch, activates the writer fence, and folds the remote
into the new store. `--dry-run` prints the policy without writing or joining,
and `harness memory status` reports what is recorded.

The store must be empty: a machine that already holds memory needs the
sealed-seed cutover (`mem migration`) instead, and the join refuses it rather
than forking that history. The exchange directory must be a private per-host
path outside every synchronized project tree and outside the local
configuration root; the default is
`${XDG_STATE_HOME:-$HOME/.local/state}/hearting/memory-sync/exchange`, which is
refused when the home directory is itself a registered project root. The
remote holds only immutable operation objects under `protocol/v2/ops/`, never
executable content, so the exchange repository is a data channel and never an
instruction source.

## User model configuration

Every adapter ships its release default at
`adapters/<adapter>/config/models.conf`. Install and activation copy that file
once to `<runtime-home>/agent-config/models.conf`. The runtime-home copy is a
user-owned Hearting preference surface, not a managed projection:

- an existing regular file is preserved byte-for-byte on install, refresh,
  update, reapply, and uninstall;
- the file is never a symlink and is not recorded as a managed copy-once file;
- runtime model/profile consumers use the complete user file only when it is
  safe and contains every shipped key, otherwise they use the complete shipped
  default and report the fallback reason;
- native runtime configuration such as Codex `config.toml`, Claude
  `settings.json`, and OpenCode `opencode.json(c)` remains independently owned
  by that runtime.

The Codex installer recognizes only the former harness-owned
`agent-config -> adapters/codex/config` projection as a legacy migration. It
replaces that link with a real directory and seeds `models.conf`; a foreign link
or real-file collision blocks instead of being guessed away.

## Activation Modes

The activation record lives at `<runtime-home>/.harness/activation.json` and is
the machine-readable source of truth for source root, source/active revision,
projection digest, discovery paths, duplicates, freshness, and session action.
Claude records also expose `config_conflicts` when a user-owned value prevents
an activation default from being adopted.

```bash
curl -fsSL https://github.com/dmlguq456/hearting/releases/latest/download/install.sh | sh
harness runtime status --runtime all --json
harness runtime doctor --runtime all --strict
```

The installer embeds distribution logic from the same immutable release tag,
then downloads that exact tag's archive and SHA-256 sidecar. It never combines
distribution code from `main` with a separately selected release archive.

`harness-manifest.json` owns the full capability, role, and mode set, and
activation always projects that complete set — there is no subset selection,
so a reactivation never silently narrows the discovery surface. A legacy
activation record carrying a retired `profile*` field is read but ignored.

- Managed `packaged` releases are the general-user default. The release
  updater verifies an archive in staging, activates only runtimes still bound
  to the previous managed release, then atomically switches `current`.
- The SHA-256 sidecar is an integrity/corruption check. Authenticity remains
  anchored to the repository's GitHub Release and HTTPS publisher account; the
  sidecar is not an independent signature.
- `linked` is the maintainer default. It projects one explicit local checkout;
  managed update never fetches, pulls, or repoints it.
- A manually activated `packaged` source changes only after `runtime refresh`;
  a managed release changes through `harness update`. Both use the same native
  discovery paths.
- Both modes disable prior harness plugin registry entries and quarantine only
  harness-owned plugin caches so plugin state cannot shadow the selected source.
- There is no `both` mode. Codex/Claude native+plugin and OpenCode local+npm
  duplicates are a strict doctor failure.
- Activation is journaled. A failed operation restores the previous links and
  activation record. Credentials, sessions, logs, DBs, and foreign caches are
  outside the owned write set.
- File replacement does not imply an already-running agent session reloaded its
  instructions. `runtime status` and `session_action` remain authoritative.

## Claude Code Projection

Claude Code expects files under `$HOME/.claude`. Runtime-owned files stay there;
harness-owned files are symlinked from the versioned Claude projection, and
`settings.json`/`keybindings.json` are copied once (never re-linked).

```bash
harness install claude
```

**Contract, not recipe**:

| Surface | Wiring | Contract |
|---|---|---|
| `CLAUDE.md`/`core`/`skills`/`agents`/`hooks`/`tools`/`statusline.sh`/… | symlink | Repository changes appear immediately; harness-owned and read-only from the runtime |
| `settings.json`/`keybindings.json` | copy once + hash manifest | Copy once and never link again (see configuration files below) |
| packaged activation | repo-local immutable bundle → same native surfaces | Does not use plugin registries or caches |

- **Configuration files:** Claude Code rewrites `settings.json` and
  `keybindings.json` in place through `/model`, `/config`, and in-app keybinding
  edits. An atomic write replaces a symlink with a regular file, so these files
  are copied once and never linked. Linking them even once can silently dirty
  the repository. `verify` and `update --reapply` use the hash manifest to
  detect, back up, and reapply drift.
- **Runtime activation:** harness hook entries, `statusLine`, and the explicitly
  allowlisted `env.MEM_DISTILL_ENABLE` default are merged into the existing
  `settings.json`. Referenced `tools`, `utilities`, and `statusline.sh` point to
  either the source or an immutable bundle. Missing or previously managed exact
  values are installed or refreshed. A conflicting user value is preserved and
  reported through `config_conflicts`; strict doctor remains unhealthy until the
  conflict is resolved. Uninstall removes only values that still exactly match
  the activation record, leaving custom values and unrelated settings untouched.
  The operation records an original-checksum backup and a transaction journal.
  The legacy plugin generator is an optional distribution artifact and is not
  part of Phase 1 activation.
- **Hook coexistence semantics** (`runtime_activation.py` merge contract):
  pre-existing user hook entries are preserved verbatim and keep their array
  position; harness entries are appended after them within each event, so on
  a shared event the user's hooks run first. Duplicate detection and managed
  ownership both use exact-JSON entry match: re-activation removes only the
  entries the previous activation recorded as managed, then appends the
  current source's entries. Consequently, hand-editing a harness-managed
  entry orphans it — the edited copy no longer matches, survives the next
  re-activation, and the fresh harness entry lands beside it as a
  near-duplicate. Keep customizations in separate user-owned entries instead
  of editing managed ones.

Keep these local to `$HOME/.claude`: `.credentials.json`, `.dispatch/`, `cache/`, `daemon/`, `history.jsonl`, `ide/`, `projects/`, `sessions/`, `session-env/`, `shell-snapshots/`, runtime logs, and other runtime-generated state.

If present, existing `worklog-board/` and `worklog-board-wt/` directories under
`$HOME/.claude` are local worklog app workspaces, not harness projection
targets. Do not move their data during harness installation. Their notes data
root is `<agent-notes-root>`, which is mutable continuity state and should not
be committed to this repo. Adapter docs own concrete local path realizations.

## Windows Projection (Git Bash)

Claude Code runs on Windows, but the harness assumes a POSIX runtime. Two
Windows-specific facts break the projection silently — a one-shot installer
(`adapters/claude/bin/install-windows.sh`, run from Git Bash) repairs both and
is idempotent. `harness install claude` invokes this delegate automatically
on a detected Windows host; the direct command remains available for manual
repair:

```bash
bash "$(sh ~/.local/share/hearting/current/utilities/agent-home.sh)/adapters/claude/bin/install-windows.sh"
```

1. **Unreliable `$HOME`.** The shell Claude Code spawns for hook / statusLine
   commands sees `$HOME` as either empty or the MSYS `/home/<user>` — never the
   real `%USERPROFILE%` where `.claude` actually lives. Every
   `bash "$HOME/.claude/hooks/<x>.sh"` command therefore fails to resolve and the
   hook/statusline no-ops with no obvious cause. The installer injects
   `HOME` / `CLAUDE_HOME` / `AGENT_HOME` into the runtime `settings.json` `env`
   block (which Claude Code applies to command execution) so those paths resolve.

2. **`core.symlinks=false`.** A Windows checkout writes repo symlinks out as
   small pointer-TEXT files (content = the link target path), not real files. So
   the entry files the Linux projection step symlinks into the runtime home
   (`CLAUDE.md`, `statusline.sh`) are absent or pointer-text on
   Windows, and `~/.claude/CLAUDE.md` never loads. The installer copies each from
   its single source of truth (`adapters/claude/<name>`) into the runtime home
   when the target is missing or a pointer — the Windows equivalent of the Linux
   `ln -sfn` step.

The installer also restores the per-machine memory DB from the git-tracked
`dump.jsonl` mirror when it is missing (`mem import`).

**`fleet` on Windows.** `fleet --json` and `fleet --once` (plain snapshot) run
on native Windows under Git Bash — `render.py` imports `curses` lazily, so the
snapshot/scripting paths need no `curses` at all and write UTF-8 directly (so a
cp949/non-UTF-8 console codepage does not raise `UnicodeEncodeError`). Only the
live full-screen TUI (`fleet` with no `--once`) needs `curses`; install it with
`pip install windows-curses`, or run the live view under WSL/Linux. The
installer also drops a `fleet` launcher into `~/.local/bin` (put that dir on
your Git Bash `PATH`) so `fleet` works as a one-word command. Everything else
(hooks, memory, statusline, skills) runs under Git Bash.

## Common operator CLI — `compute-hosts`

The installer places one shared `compute-hosts` launcher in
`~/.local/bin`; Claude, Codex, and OpenCode use the same entry. It resolves the
active pinned Hearting root and delegates to `utilities/compute-hosts.py`.
Choose a host explicitly. The installer seeds the user-owned
`${XDG_CONFIG_HOME:-$HOME/.config}/hearting/compute-hosts.yaml` once as a
commented template and never edits it again; it does not edit shell startup
files, schedule GPU work, or dispatch remote agents. A template that still has
no hosts is reported as `template` by `compute-hosts`, `harness verify`, and
the Fleet COMPUTE RESOURCES panel, never as an error.

## User-owned config registry — `harness config status`

Every user-owned configuration file the harness reads is registered in
`tools/install/user_config.py`: the compute-host inventory, the dispatch
routing policy, the report bundle root, the memory exchange policy, and each
runtime's `agent-config/models.conf`. `harness install` prints this registry
at the end of a run, `harness verify` checks the surfaces that can be invalid,
and `harness config status [--verbose] [--json]` repeats the same view on
demand with the path, the current state (`ok`, `template`, `absent`,
`missing`, `invalid`), the command that seeds it, and what applies while it is
absent. Adding a surface means adding one registry entry. Foreign PATH
files and symlinks are preserved; full uninstall removes only the owned shared
link, while partial runtime uninstall retains it.

## Cross-harness CLI — `fleet`

`fleet` is a cross-harness live dashboard under `tools/fleet/`. Its observation
and rendering core reads Claude Code, Codex, and OpenCode state through separate
collectors, so the dashboard itself belongs to no single adapter. Individual
collectors and optional enrichment features may still depend on runtime-specific
state or providers. The launcher `tools/fleet/fleet.sh` runs from the repository
directly; to get the one-word `fleet` command, symlink it onto `PATH`:

```bash
export AGENT_HOME="$HOME/hearting"
mkdir -p "$HOME/.local/bin"
ln -sfn "$AGENT_HOME/tools/fleet/fleet.sh" "$HOME/.local/bin/fleet"   # needs ~/.local/bin on PATH
```

No install step at all still works — run it by path: `bash "$AGENT_HOME/tools/fleet/fleet.sh"`. Zero-dep
(stdlib python3 + curses); nothing to build.

Title acquisition is best-effort and runtime-specific. Fleet reads native title
state where available: for example, Codex's state DB with the JSONL
`thread_name` index as a compatibility fallback, and OpenCode's session DB.
The optional live refresher currently supports active Claude Code and Codex
transcripts and writes Fleet-owned sidecars under
`${FLEET_TITLE_STATE_DIR:-${XDG_STATE_HOME:-~/.local/state}/agent-fleet/titles}/<harness>/`.
That refresher is not runtime-neutral today: its implementation defaults to the
Claude CLI with the no-tools `haiku` model as a compatibility provider. This is
an implementation fallback, not a portable Fleet requirement; when the provider
is absent or fails, the dashboard continues with native titles or slugs. To use
another small provider, point `FLEET_TITLE_COMMAND` at a no-tools wrapper. The
value is parsed as an argv template, never through a shell:

```bash
export FLEET_TITLE_MODEL="small-model"
export FLEET_TITLE_COMMAND='my-title-wrapper --model {model} --prompt {prompt}'
```

The wrapper owns its provider-specific no-tools restriction. `fleet --json` and
`fleet --once` never start title workers.

## Codex Projection

Keep `$HOME/.codex` runtime-owned. The portable harness projects through a
stable pointer plus adapter-owned Codex-native Skills, custom Agents, mode
guides, and hook bridges:

```bash
harness install codex
```

**Contract, not recipe**:

| Surface | Wiring | Contract |
|---|---|---|
| `AGENTS.md`/core/capabilities/roles/bin/tools/utilities/scaffolds | symlink using `agent-*` pointer names | Routes through generated projections under `codex_setting/` |
| Codex-native skills/agents/modes | symlink fan-out (`skills/*`, `agents/*.toml`) | Generated from `capabilities/` and `roles/` into `codex_setting/codex-{skills,agents,modes}`; do not reimplement |
| packaged activation | repo-local immutable bundle → same native surfaces | Does not use plugin marketplace, config, or cache state |
| hooks | `codex_setting/codex-hooks` → `hooks.json` | Adapter-owned hook bridge; executes only `type:"command"` entries |
| `config.toml` used by `/statusline` | not copied; fragment only | `config.toml` remains runtime-owned (see statusline below) |

- **Runtime activation:** both linked and packaged modes use Codex-native
  skills, agents, modes, and hooks. Existing harness plugin enable entries are
  preserved but set to `false`; their caches are isolated under
  `.harness/disabled-plugins/`. Activation does not register a marketplace or
  invoke plugin installation.
- **Statusline:** `config.toml` remains runtime-owned because the TUI rewrites it
  directly. The harness keeps only
  `codex_setting/codex-config/tui-statusline.toml`; applying it through
  `codex_setting/bin/preflight.sh tui-config` updates only
  `[tui].status_line` and `[tui].status_line_use_colors`.

For a project-scoped install, `harness install codex --scope project` symlinks
the generated TOML files into the project's `.codex/agents/` directory instead
of `$HOME/.codex/agents/`.

Do not symlink Claude-native surfaces such as `settings.json`, `commands/`,
root `skills/`, root `agents/`, `statusline.sh`, or `hooks/` into `$HOME/.codex`.
Future Codex-specific bootstrap files should live under `adapters/codex/` and
be symlinked or generated into `codex_setting/` without moving Codex
credentials, logs, sessions, or SQLite state into the repo.

## OpenCode Projection

OpenCode loads config from `opencode.json` / `opencode.jsonc` (project or
global `~/.config/opencode/`), auto-loads `AGENTS.md` from the global config
home, and reads instruction files listed in the `instructions` array. The
bootstrap uses exactly one of those two carriers: `runtime activate` owns the
`AGENTS.md` link, and `harness install opencode` merges the `instructions[]`
entry only when that link is absent. Both at once inject the bootstrap twice —
OpenCode dedupes instruction sources by resolved path, so a projection and the
link count as two sources (`core/ADAPTATION.md §6.1`).

Keep `$HOME/.config/opencode` and `$HOME/.local/share/opencode` runtime-owned;
the harness merges its skill entries — and the fallback instruction entry — into
the existing config **non-destructively** (existing user config preserved,
conflicts reported and the run stopped rather than resolved by guessed intent)
and projects adapter-owned surfaces through a stable pointer:

```bash
harness install opencode
```

**Contract, not recipe**:

| Surface | Wiring | Contract |
|---|---|---|
| `AGENTS.md`/core/capabilities/roles/bin/tools/utilities | symlink using `agent-*` pointer names | Routes through generated projections under `opencode_setting/` |
| OpenCode-native skills/agents/commands | symlink fan-out (`skills/*`, `agents/*.md`, `commands/*.md`) | Uses the current official plural discovery paths |
| guard plugin | `opencode_setting/opencode-plugins/hearting-guards.js` | JS/TS plugin hook surface; OpenCode has no marketplace or bundle format, so installer symlinks are the only channel |
| `opencode.json`/`opencode.jsonc` `skills.paths` | non-destructive merge | Preserves user config; reports conflicts and stops instead of guessing an automatic merge |
| `opencode.json`/`opencode.jsonc` `instructions[]` | conditional merge | Bootstrap carrier of last resort: added only when the config-home `AGENTS.md` auto-load is missing, and a previously installed entry is dropped once that link exists. User entries are never touched |

`runtime activate` uses the official plural convention directories. Singular
projections from the legacy `harness install opencode` path remain for
compatibility but are not part of the active-source contract. Activation
removes an explicitly configured harness npm plugin from JSON while preserving
all other user keys. It cannot safely remove the exact plugin entry from JSONC
without a comment-preserving parser, so it blocks before mutation and reports a
manual removal path. Project-scoped runtime activation must resolve OpenCode's
cwd-to-worktree config hierarchy together with project `AGENTS.md` ownership;
Phase 1 therefore supports it for none of the three runtimes. `--scope project`
fails explicitly instead of silently falling back to the global home. The
legacy `harness install opencode --scope project` compatibility path is
separate and is not considered an active-source installation.

Do not symlink Claude-native surfaces such as `settings.json`, `commands/`,
`skills/`, `statusline.sh`, or `hooks/` into `$HOME/.config/opencode`.
Future OpenCode-specific bootstrap files should live under `adapters/opencode/`
and be symlinked or generated into `opencode_setting/` without moving
OpenCode credentials, DB state, logs, sessions, or snapshots into the repo.

## Migration / Verification

The manual `ln -sfn` recipes and the ~275-line manual verification battery
this section used to hold are now machine-checked:

```bash
harness install [claude|codex|opencode|all]
harness verify  [claude|codex|opencode|all] --json
harness runtime status --runtime all --json
harness runtime doctor --runtime all --strict
```

`verify` follows the selected runtime's active installation contract. When an
`activation.json` record exists it runs strict activation doctor checks for the
recorded source, projection, duplicate, and freshness state. Without
an activation record it preserves the legacy driver's `checks()` list (symlink
existence · canonical generator drift · preflight assertions · bootstrap load
smoke). This avoids mixing activation's per-file discovery paths with the old
installer layout while retaining verification for installations that have not
migrated to activation yet.

**Contract-level invariants that remain true regardless of implementation**
(kept as prose, not runnable steps):

- **Exit codes** (`tools/install/installer.py`, PRD `[cli]` "### Exit code"):
  `0` success (verify: every check passed) · `1` execution failure (I/O,
  unmet precondition) · `2` verify failure (≥1 check ✗) · `3` **BLOCKED** —
  a target runtime process is active or a destination is a pre-existing
  real file/dir the installer refuses to overwrite (this is the direct
  successor of the old Migration Order step 2's "stop long-running runtime
  processes first" rule — the installer now detects and refuses instead of
  relying on the operator to remember) · `4` drift detected (`--reapply` or
  a manual backup check needed) · `64` usage error.
- **Never overwrite runtime-owned state.** The installer's symlink step
  refuses (`status: "blocked"`) rather than clobbers when a destination is
  a real file/dir, not a symlink — this preserves an operator's pre-existing
  vanilla runtime config instead of silently replacing it.
- **hash-manifest drift**: only files the installer actually *copies*
  (`settings.json`, `keybindings.json`, Windows copy-branch files) are
  hash-tracked — symlinks are self-evidently canonical and excluded, plugin
  cache is runtime-owned and excluded. A mismatch on `verify`/`update` means
  the user edited a copied file in place; `update --reapply` backs it up
  under `local-patches/` before reapplying, and a 3-way conflict is reported
  rather than auto-merged.
- **Marketplace bundles are optional distribution artifacts.** Core generation,
  activation, `doctor`, and `verify` neither register nor require them. An
  explicit legacy `install --plugin` flow owns its own runtime-currentness and
  registration checks; it cannot shadow or duplicate the active native
  activation.

Do not run drill automatically during migration; it invokes headless runtime sessions and can spend tokens. Run a targeted drill only after `harness verify` reports clean.
