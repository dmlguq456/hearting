# OpenCode Adaptation

This adapter maps the common agent harness onto OpenCode. It targets harness
parity on OpenCode, not Claude surface parity.

OpenCode has a richer native surface than an instruction-only runtime: it
ships native commands, skills, agents, MCP, and a JS/TS plugin hook system.
This adapter prefers those native surfaces first, then adds shell preflight
wrappers only for harness-specific signals that OpenCode does not expose
directly. Claude files are implementation references, not files to port
wholesale.

## Canonical artifact and cleanup boundary (2026-07-14)

- **Runtime support:** official OpenCode agent/permission documentation exposes
  scoped `permission.external_directory` rules, and plugin lifecycle events
  expose session state but not Git merge/push completion.
- **Adapter realization:** dispatch injects `AGENT_ARTIFACT_ROOT` and merges
  exact canonical-root allow patterns into `OPENCODE_CONFIG_CONTENT` while
  preserving other settings. Shared guards reject worker-local artifact writes.
- **Parity gap/fallback:** `session.idle` never deletes worktrees. Main uses
  `preflight.sh worktree-cleanup` after integrated verification and push.

## Worker bootstrap realization (2026-07-16)

The OpenCode wrapper now renders the portable minimal kernel plus one worker
type around both generated and custom assignments. It does not add the full
main bootstrap or return a prose report; details stay in the artifact and the
terminal envelope is three lines. Project instruction auto-load is not treated
as physically masked until an official, locally verified disable surface exists.

## Design Principle

Start from the portable invariant in `core/`, then map it onto OpenCode-native
features where they exist. Use OpenCode-native surfaces first for instructions,
commands, skills, agents, model/session/context management, approvals, MCP,
and plugin hooks. Add adapter wrappers only for harness-specific signals that
OpenCode does not provide directly.

Keep the workflow/capability meaning canonical, describe OpenCode artifact
layout and config surfaces as adapter data, prove the runtime discovers
adapter-owned artifacts before claiming support, and fail closed when a
runtime feature is undocumented or missing.

## Investigated OpenCode Native Surfaces

Investigated against OpenCode 1.17.x via the published config schema
(`https://opencode.ai/config.json`), the `opencode` CLI help output, the
`opencode debug` subcommands, and the built-in `customize-opencode` skill
documentation.

| OpenCode runtime surface | Native? | Adapter source / projection |
|---|---|---|
| Project config (`opencode.json` / `opencode.jsonc` / `.opencode/opencode.json`) | yes | user-owned; adapter documents the `instructions` entry that loads `AGENTS.md` |
| Global config (`~/.config/opencode/opencode.json`) | yes | user-owned; adapter documents projection entries |
| Instruction files (`instructions` array in config) | yes | `adapters/opencode/AGENTS.md` projected through `opencode_setting/AGENTS.md` |
| Commands (`.opencode/command/<name>.md` or `.opencode/commands/<name>.md`) | yes | `adapters/opencode/commands/<name>.md` generated from `capabilities/` |
| Skills (`.opencode/skill/<name>/SKILL.md` or `.opencode/skills/<name>/SKILL.md`) | yes | `adapters/opencode/skills/<name>/SKILL.md` generated from `capabilities/` |
| External skill autoload (`~/.claude/skills/<name>/SKILL.md`, `~/.agents/skills/<name>/SKILL.md`) | yes (compat) | not relied on; adapter must generate its own skills, not depend on Claude skill autoload |
| Agents (`.opencode/agent/<name>.md` or `.opencode/agents/<name>.md`) | yes | `adapters/opencode/agents/<name>/<name>.md` generated from `roles/README.md` role profiles, plus an explicit `EXTRA_AGENTS` out-of-catalog path in `adapters/opencode/bin/sync-native-agents.py` (e.g. `memory-scout`, sourced from `core/MEMORY.md` §7.4 rather than a role-catalog row), with `mode: subagent` |
| Plugin hooks (JS/TS: `tool.execute.before`, `tool.execute.after`, `event`, `config`, `chat.message`, `command.execute.before`, `permission.ask`, `shell.env`, ...) | yes | `adapters/opencode/plugins/hearting-guards.js` bridges write/edit/patch tool execution to the shared write guard, `command.execute.before` to the spec-skill gate, and `read` post-execution to the spec read marker |
| Permission model (`permission` config: `allow`/`ask`/`deny` per tool, per-agent override) | yes | adapter documents recommended permission rules; not a harness guard replacement |
| Permission contract wrapper | yes | `adapters/opencode/bin/preflight.sh permissions` reports native permission surfaces and rejects Claude `allowedTools` as a portable contract |
| MCP servers (`mcp` config: local/remote) | yes | `adapters/opencode/bin/preflight.sh mcp` reports native MCP surfaces and rejects Claude `settings.json` MCP payloads as a portable contract |
| Model selection (`model`, `small_model`, per-agent `model`, `variant`) | yes | `adapters/opencode/bin/role-map.sh` resolves portable roles to model/variant |
| Statusline / footer | no user shell surface | TUI footer is native; harness status signals stay instruction-only/preflight |
| Shell hooks (Claude-style `settings.json` hook events) | no | harness guards run as explicit preflight wrappers |
| Session transcript | SQLite at `~/.local/share/opencode/opencode.db`, `opencode export <sid>` | `distill-delta` uses the shared OpenCode export source reader |
| No-tools worker flag | yes (verified) | `opencode run --pure --agent <distiller>` with every built-in tool set to `false` — zero tools, so no execution and no tool-retry hang (D-14 acceptance passed); distill auto-apply enabled by default |

## Native Skill, Command, And Agent Surface Debt

OpenCode has native skill, command, and agent surfaces. This adapter now
materializes native Skills from `capabilities/*.md` and native Agents from
`roles/README.md`, plus native Commands from `capabilities/*.md`. Current
support still runs through explicit preflight wrappers for guard and
tool-contract reporting.

Before adding or changing OpenCode-native skills, commands, or agents:

1. Use `capabilities/<name>.md` and `roles/` as source, not
   `skills/<name>/SKILL.md` or `adapters/claude/skills/`.
2. Generate or maintain concrete adapter-owned output under an explicit
   OpenCode adapter path, for example `adapters/opencode/skills/<name>/SKILL.md`,
   `adapters/opencode/commands/<name>.md`, or `adapters/opencode/agents/<name>/<name>.md`.
3. Keep OpenCode frontmatter (`name`, `description`, `mode`, `model`,
   `permission`, `variant`), command argument passthrough (`$ARGUMENTS`), and
   permission assumptions in the OpenCode adapter.
4. Skill and command generation are guarded by `check_opencode_native_skill_projection`
   / `check_opencode_native_command_projection`. Agent-generation completeness is
   guarded too: `check_opencode_native_agent_projection` requires every generated
   `adapters/opencode/agents/<name>/` directory to be in an approved set (the
   `roles/README.md` role profiles plus the explicit `EXTRA_AGENTS` out-of-catalog
   list), and `check_claude_native_agent_projection` cross-checks that every
   `adapters/claude/agents/*.md` has a corresponding OpenCode (and Codex) projection.
   No Claude-native file may be exposed as OpenCode-native.
5. Verify discoverability using the OpenCode runtime contract (`opencode debug
   skill`, `opencode debug agent`, or TUI invocation), not byte parity with
   Claude files. Use `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1` during this check
   so OpenCode's `~/.claude/skills/` compatibility autoload cannot produce a
   false pass.

Design capabilities are a tool-contract exception: OpenCode has native Skill
guidance for them, but must run the adapter visual harness before claiming full
support. `capability-info` reports `status=tool-contract` for those capability
entries. This does not make `roles/modes/design/*` native OpenCode modes; those
mode fragments remain `mode-info status=unsupported` / `fallback=reference-only`
because they are adapter-coupled persona fragments, while the concrete
capability path is `autopilot-design` plus the visual harness contract.

`roles/modes/material/browser-fetch.md` has an OpenCode-owned executable
tool-contract surface:
`adapters/opencode/bin/preflight.sh browser-fetch --check <url>` verifies
rendered browser access through `adapters/opencode/tools/material/` and reports
exit 69 when the local Playwright browser stack is unavailable.

`roles/modes/material/data-script.md` is the first material mode with an
OpenCode-owned executable tool-contract surface:
`adapters/opencode/bin/preflight.sh data-script --check <script.py>` verifies
generated Python analysis scripts through `adapters/opencode/tools/material/`.

`roles/modes/material/figure-gen.md` has an OpenCode-owned executable
tool-contract surface:
`adapters/opencode/bin/preflight.sh figure-gen --check <script.py>` verifies
generated matplotlib/seaborn figure scripts through
`adapters/opencode/tools/material/`. Report spectrograms additionally run
`figure-gen --verify-report <manifest.json> <report.md>` and fail closed on
metadata, scale, claim-evidence, or hash-bound visual-review drift.

`roles/modes/material/pdf-extract.md` has an OpenCode-owned executable
tool-contract surface:
`adapters/opencode/bin/preflight.sh pdf-extract --check <file.pdf>` verifies
local PDF text extraction through `adapters/opencode/tools/material/` and
reports exit 69 when the local extractor is unavailable.

`roles/modes/material/web-image-search.md` has an OpenCode-owned executable
tool-contract surface:
`adapters/opencode/bin/preflight.sh web-image-search --check <query>` verifies
a configured image-search provider command through
`adapters/opencode/tools/material/` and reports exit 69 when no provider is
configured.

`roles/modes/qa/security-review.md` is portable read-only mode guidance for
OpenCode. It is consumed with OpenCode file and git diff tools and does not
project or invoke Claude's `/security-review` slash command.

`roles/modes/research/claim-verify.md` has an OpenCode-owned executable
tool-contract surface:
`adapters/opencode/bin/preflight.sh claim-verify --check <claim>` verifies a
configured external verification provider command through
`adapters/opencode/tools/research/` and reports exit 69 when no provider is
configured.

`roles/modes/qa/test.md` has an OpenCode-owned executable tool-contract
surface:
`adapters/opencode/bin/preflight.sh verification-runner --check -- <command>`
checks explicit verification commands and the same wrapper can execute them
with a bounded timeout.

## Native Plugin Hook Surface

OpenCode exposes JS/TS plugin hooks that can enforce part of the harness guard
contract. This adapter materializes a concrete OpenCode plugin at
`adapters/opencode/plugins/hearting-guards.js`. It uses `chat.message` plus
`experimental.chat.system.transform` to inject prompt-time workflow, memory,
bounded capsule candidates, and briefing context through
`adapters/opencode/bin/preflight.sh` without copying Claude hook JSON. The
prompt is retained only from `chat.message` until that transform and is then
discarded. It uses `tool.execute.before` to detect
write/edit/patch targets and calls `adapters/opencode/bin/preflight.sh write
<file> <session-id>`, which runs the portable artifact-order, git-state,
core-first adapter edit, and memory-write guards. It also uses `tool.execute.after` to route saved design
HTML files through `adapters/opencode/bin/preflight.sh design <file>` as a
post-write console-check alert path.

It also enforces the spec read gate, mirroring Claude's
`PreToolUse[Skill]` + `PostToolUse[Read]` pair. `command.execute.before` calls
`adapters/opencode/bin/preflight.sh capability <name> <cwd> <session-id>` for
`autopilot-code` / `autopilot-spec` and throws (aborting the command before its
prompt expands) when the cwd is spec-backed and `prd.md` was not actually read
this session. `tool.execute.after` on a `read` of `.../spec/prd.md` calls
`adapters/opencode/bin/preflight.sh read <file> <session-id>` to drop the
grounding marker that lets the spec and core-first gates pass — non-blocking, so a marker failure
never aborts a successful read.

When changing the plugin:

1. Keep it under `adapters/opencode/plugins/` as adapter-owned JS/TS.
2. Bridge to `adapters/opencode/bin/preflight.sh`, not to Claude hook files.
3. Prove discovery with `opencode debug config`.
4. Keep shell preflight wrappers as fallback so the adapter remains usable
   when plugins are disabled.

The plugin covers prompt lifecycle context, write guard enforcement, spec
read-gate enforcement, design post-write console checks, and auto-distillation
(the `event` hook fires `session-end` on `session.idle`). The no-tools worker
contract is verified, so distillation is enabled by default — see the
"Auto-Distillation" section.

## Parity Status vs Claude

Goal of this adapter is harness behavior parity on OpenCode. The hard guard
invariants Claude enforces through `settings.json` hooks are enforced here
through the plugin and throw to abort, and the prompt/session lifecycle
injections are auto-applied. The table records the current state.

| Claude `settings.json` hook | OpenCode realization | Parity |
|---|---|---|
| `PreToolUse` git-state guard (deny) | plugin `tool.execute.before` → `preflight write` (throws) | full — auto enforced |
| `PreToolUse` artifact-order guard (deny) | plugin `tool.execute.before` → `preflight write` (throws) | full — auto enforced |
| `PreToolUse` builtin-memory guard (deny) | plugin `tool.execute.before` → `preflight write` (throws) | full — auto enforced |
| `PreToolUse[Skill]` spec-skill gate (deny) | plugin `command.execute.before` → `preflight capability` (throws) | full — auto enforced (command path) |
| `PostToolUse[Read]` spec-read marker | plugin `tool.execute.after` on `read` → `preflight read` | full — auto enforced |
| `PostToolUse` design post-write | plugin `tool.execute.after` → `preflight design` | full — auto enforced |
| `SessionStart`-equivalent memory inject | plugin `experimental.chat.system.transform` (first turn) → `memory` | full — auto injected |
| `UserPromptSubmit` capsule candidates / routing-contract signal / briefing | plugin `chat.message` → transient prompt, then `experimental.chat.system.transform` → `candidates` / `prompt-signal` / `briefing` | full — candidate output is active current-project/global capsule-only, maximum three / 1,200 UTF-8 bytes; prompt is discarded after the transform |
| `SessionEnd` + `UserPromptSubmit` auto-distillation | plugin `event` (`session.idle`) → detached `preflight session-end` → no-tools `opencode run` worker | full — auto applied by default (opt out `OPENCODE_DISTILL_ENABLE=0`) |
| `PreToolUse` material-route guard (deny) | plugin `tool.execute.before` → `preflight write` (structured), plugin `tool.execute.before` on `bash` → `preflight material-route check --tool Bash` (verbatim command), `preflight route` binds after a checked compile | structured: full for a resolvable target — a recognized mutation tool whose target does not resolve (`normalizeFile()` returns `""`, `targetFiles()` yields `[]`) still reaches no guard (see Codex's stricter `pretooluse-write-guard.py:291-293`); bash: full for documented commands. Automatic final-session clear is **not** claimed — `session.idle` is per-turn and `session.deleted` means deletion, not exit. |
| `PreToolUse[Bash]` worktree-path guard (deny) | plugin `tool.execute.before` on `bash` → `preflight worktree-path`, `preflight worktree-path` explicit fallback | portable `git worktree add` path check only — the built-in-worktree-tool deny is Claude-native and has **no** OpenCode counterpart (`core/HOOKS.md:45`) |

Auto-distillation, previously the one functional gap, is now closed (see the
"Auto-Distillation" section below). Two items remain that cannot reach
byte-for-byte Claude parity; they are OpenCode runtime surface limits, not
adapter debt:

1. **No persistent statusline.** OpenCode has a native TUI footer (model,
   context, tokens, session) but no user shell statusline surface. Harness
   signals (git risk, headless jobs) are injected per prompt
   through the plugin transform instead of shown persistently. Functional, not a
   persistent display.
2. **Prompt-lifecycle injection rides an experimental hook.**
   `experimental.chat.system.transform` is in OpenCode's `experimental.*`
   namespace; if OpenCode changes it, lifecycle injection breaks and the
   explicit preflight wrappers (`memory` / `prompt-signal` /
   `briefing`) remain the manual fallback.

### Auto-Distillation

Session auto-distillation reaches behavior parity with the Claude/codex
session-end distillers. The earlier "worker hangs" finding was a measurement
artifact: the hangs came from running several `opencode run` invocations
concurrently (provider/local-server contention) and from a dead free model, not
from tool-disable. Run serially with a working model, a fully tool-stripped
agent does not hang.

- **No-tools worker (verified).** `adapters/opencode/bin/distill-worker.sh` runs
  `opencode run --pure --agent <distiller>` where the distiller agent sets every
  built-in tool to `false`. With zero tools the model cannot execute or retry a
  tool: an adversarial "run `date >> file`" prompt produced no file and exited 0
  (D-14 acceptance). `--pure` also disables external plugins so the worker never
  re-enters the guard plugin, and `MEM_DISTILL=1` guards every lifecycle
  re-entry. The whole run is `timeout`-guarded so a slow/unreachable model can
  never stall the caller.
- **Trigger.** The plugin `event` hook fires on `session.idle` and detaches
  `preflight session-end`, which debounces per session
  (`OPENCODE_DISTILL_MIN_INTERVAL`, default 600s), then runs the worker. Enabled
  by default (`OPENCODE_DISTILL_ENABLE` defaults to 1 on that path), opt out with
  `OPENCODE_DISTILL_ENABLE=0`. Set `OPENCODE_DISTILL_MODEL` to a capable model
  for quality.
- **Delta extraction fix.** `opencode export` truncates its stdout at a
  pipe-buffer boundary (~64-80KB) when the consumer is a pipe, so the shared
  `OpenCodeExportSource` now redirects export to a temp file and parses that —
  without this, any session larger than the buffer silently distilled to nothing.
- **Apply.** Worker output is parsed by the shared
  `tools/memory/apply-distill-actions.py` (skips non-JSON / fenced lines), which
  argv-calls `mem.py`. End-to-end verified: an isolated run wrote a real record
  to a test DB.

## Explicit Non-Support

OpenCode must not consume these Claude-native files as native configuration:

| Claude-native surface | OpenCode status |
|---|---|
| `adapters/claude/settings.json` | Not consumable; OpenCode uses `opencode.json` config + plugin hooks |
| `adapters/claude/commands/` | Not consumable; OpenCode commands must be expressed as `.opencode/command/<name>.md` or `command:` config entries |
| `skills/*/SKILL.md` | Compatibility reference only; OpenCode should start from `capabilities/README.md`. The `~/.claude/skills/` autoload path is a compat convenience, not an adapter projection. |
| `adapters/claude/statusline.sh` | Not consumable; OpenCode has no user shell statusline surface |
| `adapters/claude/CLAUDE.md` | Reference only; not bootstrap |
| `adapters/claude/agents/*.md` | Reference only; OpenCode should start from `roles/README.md`. Claude Agent frontmatter is not OpenCode agent frontmatter. |
| `adapters/claude/hooks/*.sh` | Reference only; OpenCode has no shell hook event schema. Guards run as explicit preflight. |
| `roles/modes/design/*` | Reference-only adapter-coupled mode fragments; concrete design work uses `autopilot-design` capability guidance plus `preflight.sh visual-harness` |

## Status Surface Boundary

OpenCode has a native TUI footer that shows model, context, tokens, and
session state. There is no user-customizable shell statusline script
(`statusline.sh` equivalent) in the OpenCode config schema. Do not attempt to
replace the native footer, and do not project `adapters/claude/statusline.sh`.

Harness-specific status signals need OpenCode-native realization:

| Harness signal | OpenCode direction |
|---|---|
| routing-contract signal | OpenCode plugin system transform runs `preflight.sh prompt-signal`; explicit preflight remains fallback when plugins are unavailable or untrusted |
| artifact/notes/git-risk snapshot | explicit `preflight.sh status`; keep OpenCode native UI/config for model/context/session fields |
| artifact root detection | `preflight.sh write` and shared artifact-root helper |
| headless/autopilot/background jobs | `preflight.sh headless` / `dispatch` / `liveness` / `harvest` provide the tool-contract path over `opencode run`; `preflight.sh status` surfaces in-flight jobs as `headless_open_jobs` / `headless_open_slugs` from the dispatch registry. A native graphical display remains optional polish |
| sibling `-wt/<slug>` dispatch detection | preserve the worktree naming invariant; choose an OpenCode-native display surface later |
| pipeline stage nudges | preflight/AGENTS instructions first; UI only when OpenCode exposes a suitable surface |
| oncall/note/study/drill/runtime-watch loop nudges | `preflight.sh briefing` plus `preflight.sh loop-info <loop>` for loop-specific support/fallback status |
| merge/rebase/merged-branch risk | `preflight.sh write` git safety checks; `preflight.sh status` reports `git_operation` (merge/rebase/cherry-pick) and `git_branch_done` (non-default branch fully merged = DONE-BRANCH hazard). A native graphical warning remains optional polish |

## Required OpenCode Mappings

| Portable invariant | OpenCode adaptation requirement |
|---|---|
| artifact order | Run `adapters/opencode/bin/preflight.sh write <file> [session-id]` before writes |
| git state safety | Run `adapters/opencode/bin/preflight.sh write <file> [session-id]` before edits |
| memory write guard | Run `adapters/opencode/bin/preflight.sh write <file> [session-id]` before writes |
| design post-write verification | Run `adapters/opencode/bin/preflight.sh design <file>` after design HTML writes |
| spec read gate | OpenCode plugin enforces this automatically: `command.execute.before` runs `adapters/opencode/bin/preflight.sh capability <name> [cwd] [session-id]` (throws to abort `autopilot-code`/`autopilot-spec` when ungrounded) and `tool.execute.after` on a `read` of `prd.md` runs `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`. Run both manually when plugins are unavailable |
| routing-contract signal | OpenCode plugin system transform runs `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`; no statusline assumption |
| memory inject | OpenCode plugin system transform runs `adapters/opencode/bin/preflight.sh memory [cwd]` once per session; run it manually when plugins are unavailable |
| memory candidate exposure / recall | The plugin transiently captures the current user prompt, runs `preflight.sh candidates <prompt> <cwd> <session-id> [turn-id]`, then deletes it. It injects only bounded active capsule headlines/IDs and publishes the same-turn receipt; it does not inspect bodies or classify relevance. The model reads relevant records in full. Explicit `recall` provides deeper search and `recall-gate` recovers an unavailable probe |
| oncall briefing | OpenCode plugin system transform runs `adapters/opencode/bin/preflight.sh briefing [cwd]`; run it manually when plugins are unavailable |
| loop guidance | Run `adapters/opencode/bin/preflight.sh loop-info <oncall|note|study|drill|runtime-watch>` before following loop guides; OpenCode reports manual contracts, missing implementations, and drill auto-run restrictions without executing loop scripts. The `note` loop and note semantics are application-owned; the harness exposes only the optional app-neutral `artifact-sink` port |
| memory distill | The plugin `event` hook auto-distills on `session.idle` via detached `preflight session-end` → no-tools `opencode run` worker (verified); enabled by default, opt out `OPENCODE_DISTILL_ENABLE=0`, set `OPENCODE_DISTILL_MODEL` for quality. Manual: `preflight.sh distill-delta <sid>` extracts the delta, `preflight.sh distill-propose <sid>` runs a proposal |
| worklog state signal | Run `adapters/opencode/bin/preflight.sh worklog [cwd]` to inspect configured `<agent-notes-root>` / `<worklog-board-app>` paths read-only before OpenCode updates notes or diagnoses board state |
| role profiles | Read `roles/README.md`, then run `adapters/opencode/bin/preflight.sh role <portable-role>` to resolve OpenCode model/variant settings |
| permission mapping | Run `adapters/opencode/bin/preflight.sh permissions` to inspect the OpenCode native permission contract and confirm Claude `allowedTools` is unsupported |
| MCP mapping | Run `adapters/opencode/bin/preflight.sh mcp --check` to inspect OpenCode's native MCP CLI/config surface; do not copy Claude `settings.json` MCP registrations or project `tools/design-mcp` wholesale |
| headless dispatch | Run `adapters/opencode/bin/preflight.sh headless --check <worktree>` before OpenCode `run` dispatch; it checks the worktree, command availability, and installed runtime projection without launching. The dispatch surface accepts `--model-profile deep|balanced-deep|light|mini` independently of optional behavioral `--model-role`. A route-bound profile resolves through `config/models.conf`; `_kernel/owner` rejects a stage `worker_mode` and may be profile-only, caller model/variant replacement is denied, and substantive registered `mini` is denied. Because OpenCode has no verified distinct effort variant, `balanced-deep` collapses to `deep`, the row reports `profile_granularity=collapsed-balanced-deep`, and `runtime-default` is represented by omitting `--variant`. Registry/Fleet keeps capability mode, worker mode, role, profile, tier, and granularity separate. `--start` reruns the same projection check; liveness, harvest, merge, and cleanup boundaries remain unchanged |
| QA policy mapping | `adapters/opencode/bin/preflight.sh qa-policy <level> [code|research|doc|general]` maps the shared QA assurance budget to OpenCode role checks and fallback reporting. `stage_graph_selector=intensity-not-qa` preserves the core split: intensity selects graph/depth, QA only scales selected checks |
| role modes | Read `roles/MODES.md`, then run `adapters/opencode/bin/preflight.sh mode-info <family/mode>`; treat adapter-coupled modes as unsupported unless wrappers exist, obey `fallback=reference-only`, and satisfy any named `tool_contract` / `tool_contract_check` before claiming tool-contract modes |
| hook invariants | Read `core/HOOKS.md`; OpenCode plugin hooks cover prompt lifecycle context, write/edit/patch guards, the spec read gate (command/read), and design HTML post-write checks, while explicit preflight wrappers remain fallback for disabled/untrusted plugins and events not yet covered |
| capabilities | Read `capabilities/README.md`, then run `adapters/opencode/bin/preflight.sh capability-info <capability>`; do not assume Claude Skill invocation |
| material route participation | Structured plugin writes transit `preflight write`, which now ends with the material-route check, **for a resolvable target** — a recognized mutation tool whose target does not resolve still reaches no guard (`hearting-guards.js` `normalizeFile()`/`targetFiles()`; see Codex's stricter `adapters/codex/hooks/pretooluse-write-guard.py:291-293`). Documented `bash` commands are passed verbatim to `preflight.sh material-route check --tool Bash`. A successful checked `preflight.sh route --capability ...` compile binds using the `shell.env`-supplied `OPENCODE_SESSION_ID`; explicit `preflight.sh material-route check|bind|clear` remains the fallback. **Automatic final-session clear is not claimed** — `session.idle` is per-turn and `session.deleted` means deletion, not exit |
| material route caller trust (disclosure) | Direct `preflight.sh material-route bind` verifies route schema/hash, cwd, source commit, and registry/unit digests, but does **not** authenticate the calling process; any local caller able to present a valid route record can create the session marker. Same trust shape as Codex's PostToolUse auto-bind (`hooks/material-route-guard.py:801-812`) — disclosed, not defended against |
| worktree path isolation | Documented `bash` commands are passed unchanged to `hooks/worktree-path-guard.sh` through the plugin's `tool.execute.before`; `adapters/opencode/bin/preflight.sh worktree-path` is the explicit fallback. The built-in-worktree-tool deny is Claude-native and has **no** OpenCode counterpart (`core/HOOKS.md:45`, `core/ADAPTATION_INVENTORY.md:64`) |
| residual bash-write gap (mandatory) | Tranche A covers material commits and worktree add on the bash surface. Other bash-mediated writes do not transit `preflight write`, so the artifact, git-state, core-first, and builtin-memory guards do not run on them. **This matches the Claude Bash surface**, which also wires only worktree-path and material-route; it is a gap only against Codex's broader shell heuristic (`adapters/codex/hooks/pretooluse-write-guard.py:140-230,245-246`). The portable classifiers themselves have known limits that define "denied" vs "should have been denied" here: a quoted `git worktree add` string inside another command, a `-wt/` decoy token anywhere in the command line, and inert `echo git commit` text can all be matched or missed by the classifiers, independent of this tranche |
| memory curate tier | `session.idle` remains debounce-only; `session-end` alone selects `curate` (`adapters/opencode/bin/preflight.sh`); `increment` remains the default everywhere else (`distill-propose`, direct calls). The `AGENT_SESSION_ROLE=worker` no-op and the debounce/`session-end`-only gating are realized in `adapters/opencode/bin/preflight.sh` (`session-end)`, `is_worker_session()` at `:21`, checked at `:746`); the `MEM_DISTILL=1` recursion guard, per-sid lock, mode defaulting, and exec-gated advance are realized in `adapters/opencode/bin/distill-worker.sh`; the six allowed actions (`add\|reinforce\|merge\|prune\|graduate\|reattribute`) and the snapshot-id whitelist, and mode-aware apply (`--mode`/`--snapshot-ids`), are enforced in `tools/memory/apply-distill-actions.py`. Mirrors Codex's shape. OpenCode's increment and curate tiers intentionally share one model knob (`OPENCODE_DISTILL_MODEL`) in this tranche, unlike Codex's per-tier `models.conf` split |
| runtime qualifiers (unverified/unsupported) | The installed OpenCode version is unpinned. `shell.env`'s `sessionID` is **undocumented in the published plugin docs** and only typed `sessionID?` in the upstream `dev` source, so its runtime availability is unverified; the sessionless-compile path is the designed fallback. The `shell` tool alias, stable `tool.execute.after` exit metadata, and plugin coverage of `task`-spawned subagent tool calls remain unverified. The returned-hook-map shape this work targets is the current/legacy OpenCode plugin API; an OpenCode V2 plugin API with a different beta registration shape is a distinct, unverified migration risk, alongside the existing "installed OpenCode version is unpinned" qualifier |

## Model Mapping

OpenCode exposes concrete choices through environment or config and resolves
them with `adapters/opencode/bin/preflight.sh role <portable-role>`:

```text
AGENT_MODEL_FAST
AGENT_MODEL_BALANCED
AGENT_MODEL_DEEP
AGENT_MODEL_EXTERNAL
AGENT_VARIANT_FAST
AGENT_VARIANT_BALANCED
AGENT_VARIANT_DEEP
AGENT_VARIANT_EXTERNAL
AGENT_MODEL_ORCHESTRATOR
AGENT_VARIANT_ORCHESTRATOR
AGENT_EXTERNAL_CMD
```

OpenCode uses `provider/model-id` strings and an optional `variant`. The sole
concrete source is `adapters/opencode/config/models.conf`. Registered profiles
resolve to `deep=deep-tier/runtime-default`,
`balanced-deep=deep-tier/runtime-default` (reported as collapsed),
`light=light-tier/runtime-default`, and
`mini=mini-tier/runtime-default` (lifecycle/micro-only). The wrapper
omits `--variant` for `runtime-default`; it never claims a distinct effort axis
that the runtime has not verified. `external adversary` remains unavailable
unless `AGENT_MODEL_EXTERNAL` or `AGENT_EXTERNAL_CMD` is configured.

## Current Projection Boundary

`opencode_setting/` should remain minimal and explicit. It may expose
`AGENTS.md`, `README.md`, `core/`, `capabilities/`, `roles/`, `bin/`,
`opencode-skills`, `opencode-agents`, `opencode-commands`, `opencode-plugins`,
selected tools, and selected utilities, but must not expose Claude-native
`settings.json`, `commands/`, `skills/`, `statusline.sh`, or `hooks/` as if
OpenCode could consume them.

`opencode_setting/tools` points at `adapters/opencode/tools/`, not the entire
shared `tools/` directory. The current allowlist is:

- `memory/mem.py` (OpenCode-owned launcher for the shared memory CLI)
- `memory/apply-distill-actions.py`
- `memory/recall.sh` (OpenCode-owned launcher for recall)
- `material/browser-fetch.sh` (OpenCode-owned launcher for rendered web page extraction)
- `material/data-script.sh` (OpenCode-owned launcher for Python data-analysis scripts)
- `material/figure-gen.sh` (OpenCode-owned launcher for generated matplotlib figure scripts)
- `material/pdf-extract.sh` (OpenCode-owned launcher for local PDF text extraction)
- `material/web-image-search.sh` (OpenCode-owned launcher for configured image search providers)
- `qa/verification-runner.sh` (OpenCode-owned launcher for explicit verification commands)
- `research/claim-verify.sh` (OpenCode-owned launcher for configured external claim verification providers)
- `design/visual-harness.sh` (OpenCode-owned launcher for render/screenshot/console checks)

Do not project `build-manifest.py`: it is a harness development tool that reads
Claude adapter skills, agents, and settings. Do not project `web-bundle` until
OpenCode has a documented design/tooling realization that uses it directly. The
shared `design-mcp` package is not projected wholesale; OpenCode exposes only
the adapter-owned visual harness launcher.

`opencode_setting/utilities` points at `adapters/opencode/utilities/`, not the
entire shared `utilities/` directory. The current allowlist is:

- `agent-home.sh` (OpenCode-owned wrapper; no Claude runtime-home fallback)
- `artifact-root.sh`
- `agent-worklog-state.sh`
- `harness-status.sh`

Do not project the shared `dispatch-liveness.sh`; it is the cross-harness
registry/wait fallback, while OpenCode uses the adapter-owned
`adapters/opencode/bin/dispatch-liveness.py`, exposed as
`adapters/opencode/bin/preflight.sh liveness [jobs.log]`, and maps open
dispatch jobs to `~/.local/share/opencode/opencode.db` sessions by
`session.directory`. OpenCode harvest is adapter-owned under
`adapters/opencode/bin/preflight.sh harvest` and only updates the portable jobs
registry from `open` to `done`; it never performs merge or worktree cleanup. Do
not project material/design helpers such as `extract_web_figures.py` until an
OpenCode capability uses them directly.

OpenCode summary production is independent of Fleet. Registered dispatch starts
an exact-attempt supervisor before worker fence release and records its identity
in the same registry transaction; the supervisor parses the attempt JSONL and
owns early, debounced, and final sidecars. Interactive `chat.message` and
`session.idle`/deletion events trigger the shared producer against an exact
session DB cursor. `dispatch-reconcile --apply` may restore only a missing owner
for one exact live attempt. Fleet merely reads the resulting sidecars.

Token self-regulation v2 remains deferred for OpenCode: no automatic Phase 2
hook accounting, no projected `token-budget-experiment.py`, and no production
dynamic-policy import, activation flag, or config mutation. Portable Fleet
modules and the Codex realization may be used as implementation references, but
they are not OpenCode runtime support.

### SD-15 limit-death detection (OPERATIONS §5.10 ⑨) — parity: partial (disclosed)

`adapters/opencode/bin/dispatch-headless.py` ports the Claude wrapper's SD-15 early-limit-death
detection: `--early-exit-watch <secs>` watches a just-launched `opencode run` child; on a
clean exit within the window whose log tail matches a limit/auth `DEATH_PATTERN`, the wrapper
closes its own `jobs.log` row to `done,note=dead-<reason>[,reset=<x>]`, writes
`.dispatch/usage-reset.opencode` (SD-16 cache), and surfaces `early_death=`/`row_closed=`. It
also adds a `jobs_lock` flock so the SD-15 row-rewrite and concurrent appends cannot interleave.
No retry — detection/closure/surfacing only.

**Disclosed structural constraint (axis not fully realized)**: `opencode run` has a known bug
(anomalyco/opencode#8203) where it **hangs indefinitely on API errors** (rate limit / 429)
instead of exiting. The launch early-exit-watch requires the child to *exit*, so a hang-on-limit
escapes it and the row stays `open`. That case is instead caught by **axis 6** — 
`adapters/opencode/bin/dispatch-liveness.py`'s `LIMIT_RE` log-tail scan (SD-15b) judges the open
row DEAD from the `Rate limited`/`Provider Rate Limit exceeded`/`429` line the hung child leaves
in its log, independent of the SQLite session mtime. So OpenCode realizes: clean-exit-on-limit →
launch watch; hang-on-limit → liveness log scan. (Claude and Codex realize both axes because
their runtimes exit on limit.) Patterns are conservative per 2026-07 issue evidence
(#8203·#34886·#15890) and kept in sync with the shared list. Conformance (incl. the hang case):
`adapters/opencode/bin/dispatch-headless.sd15.test.sh`.

### SD-48~50 nested dispatch recovery — parity: full

OpenCode participates in the inherited canonical global attempt registry for
quick and registered dispatch-depth-1 work, and now satisfies the registered
standard+ dispatch-depth-2 contract: exact parent binding and supervisor
snapshot parity are implemented. The wrapper realizes the shared
pre-registration lifecycle recheck for dispatch-depth-1 work: a
transient scope promotes provisional `detached` to `foreground-scoped`, keeps
the wrapper alive through child exit, and records requested/effective selection
plus bounded namespace evidence. At dispatch-depth-2 it additionally resolves
exactly one open, live dispatch-depth-1 owner row (`resolve_live_parent_attempt`,
ported from the Claude wrapper) before any registry claim or runtime probe;
a missing or ambiguous live parent fails closed with `live-parent-not-found`
or `parent-attempt-not-found` and starts zero children. The shared
eligibility probe now reaches the same `command_check` runtime probe used by
every other harness instead of a blanket `opencode-standard-depth2-unsupported`
refusal. The adapter's `nested-headless` diagnostic establishes dispatch-depth-2
parity together with this binding.
The parent-bound foreground branch (`wait_foreground(..., parent_is_live=…)`) is
byte-identical to the Claude wrapper's. A 2026-08-10 acceptance cell now exercises
all three wrappers with the callback enabled and also runs the OpenCode wrapper in
a real bubblewrap PID namespace. The remounted `/proc` selects
`foreground-scoped` through the conservative `pid1-class` signal; an exact-parent
callback failure terminates and reaps the child group and records
`dead-parent-terminated`. Ordinary host-visible runs still select `detached` when
the host-like evidence permits it; this verification does not force foreground
lifecycles outside a transient namespace.
`nested-dispatch-eligibility.py --prospective-standard-owner --jobs <canonical-jobs.log>` is a Codex-only probe
(`failure_class=prospective-owner-codex-only` under any other parent harness, including
OpenCode); it does not check OpenCode owner eligibility.

### SD-62 direct headless delegation — realized

OpenCode consumes the v3 direct `dispatch-chain` contract for its supported
dispatch-depth-1 target surface. A conductor invokes the checked target adapter itself,
while the canonical registry first records the stable attempt as registered-only;
the shared launch fence publishes complete process identity with the claim and
records payload start before exec. This does not establish standard+
dispatch-depth-2 parity.
The retired broker exposes diagnostic `status`/`stop` only; v1/v2 broker routes
remain inspectable but cannot register or start new workers. Registered
standard+ dispatch-depth-2 requests remain explicitly unsupported as described above.

## Distillation Boundary

Claude's adapter runs a detached `claude -p` worker with tool use denied by
runtime flags. OpenCode's verified equivalent is `opencode run --pure --agent
<distiller>` with a fully tool-stripped agent (every built-in tool `false`).
The pipeline is implemented and enabled by default:

1. `distill-delta` is supported through the shared memory CLI's
   `OpenCodeExportSource`, which normalizes `opencode export <session-id>` JSON
   into the `.messages()` interface (`Msg(role, ts, text, uuid, is_sidechain)`).
   Export is captured to a temp file, not a pipe, because `opencode export`
   truncates piped stdout at a buffer boundary.
2. `distill-propose` / the `session-end` path run the no-tools worker
   (`distill-worker.sh`): `opencode run --pure --agent <distiller>`, timeout-
   guarded, `MEM_DISTILL=1` recursion guard. The D-14 acceptance (adversarial
   shell-exec prompt produces no execution, no hang) passed.
3. Worker output is parsed by the shared
   `tools/memory/apply-distill-actions.py` applier when `OPENCODE_DISTILL_APPLY=1`
   (the `session-end` path defaults it on).
4. Automatic distillation is enabled by default through the plugin
   `event`/`session.idle` trigger (debounced per session). Opt out with
   `OPENCODE_DISTILL_ENABLE=0`; set `OPENCODE_DISTILL_MODEL` for quality.

## Worklog Boundary

OpenCode must treat `<agent-notes-root>` as mutable continuity state, not as
harness source. Before changing notes/routing state, run normal `write`
preflight for the target file and inspect `preflight.sh worklog` output.
OpenCode may read/write notes-root files only when the task is explicitly about
notes, triage, feedback, or worklog routing. It must not copy worklog-board
DBs, caches, `.env*`, build output, dispatch logs, or worktrees into this repo.

## Stage-session capacity contract (2026-08-06)

- **Runtime support:** the plugin API exposes `experimental.session.compacting`
  and the `session.compacted` event; OpenCode also exposes native agents.
- **Adapter realization:** the wrapper records the same phase brief, fixed-file
  digest, ledger, chain identity, and `stage_authority=0` axes as Claude/Codex.
  Plugin compact events flush/re-read the ledger and structured writes transit
  the enforcing preflight.
- **Parity gap/fallback:** OpenCode native agents do not yet provide the local
  route-owned dispatch-depth-2 evidence required by this contract, so no native
  gate or helper parity is claimed. Use the checked registered-headless surface
  where eligible, otherwise the recorded inline fallback; the one stage gate is
  unchanged.
