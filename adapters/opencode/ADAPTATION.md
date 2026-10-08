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
terminal envelope is three lines. New launches use a role home with the small
global attach template and retained provider settings, login, guard plugins and
permission rules. Hearting main-bootstrap instruction entries are removed from
the copied config, and unrelated skills are hidden with native skill permission
rules. Claude global prompt/skill compatibility is disabled. Project instructions
and managed policy remain runtime-owned input, rather than a universal masking
claim. [Native rules](https://opencode.ai/docs/rules/),
[skill permissions](https://opencode.ai/docs/skills/).

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
| Project config (`opencode.json` / `opencode.jsonc` / `.opencode/opencode.json`) | yes | user-owned; adapter merges `skills.paths`, and the bootstrap `instructions` entry only when the config-home `AGENTS.md` auto-load is absent |
| Global config (`~/.config/opencode/opencode.json`) | yes | user-owned; adapter documents projection entries |
| Bootstrap auto-load (`<config-home>/AGENTS.md`) | yes | `adapters/opencode/AGENTS.md`, linked by `harness runtime activate`; the single bootstrap carrier (`core/ADAPTATION.md §6.1`) |
| Instruction files (`instructions` array in config) | yes | user-owned; carries `adapters/opencode/AGENTS.md` through `opencode_setting/AGENTS.md` only as the fallback carrier when the auto-load link is absent — never alongside it |
| Commands (`.opencode/command/<name>.md` or `.opencode/commands/<name>.md`) | yes | `adapters/opencode/commands/<name>.md` generated from `capabilities/` |
| Skills (`.opencode/skill/<name>/SKILL.md` or `.opencode/skills/<name>/SKILL.md`) | yes | `adapters/opencode/skills/<name>/SKILL.md` generated from `capabilities/` |
| External skill autoload (`~/.claude/skills/<name>/SKILL.md`, `~/.agents/skills/<name>/SKILL.md`) | yes (compat) | not relied on; adapter must generate its own skills, not depend on Claude skill autoload |
| Agents (`.opencode/agent/<name>.md` or `.opencode/agents/<name>.md`) | yes | `adapters/opencode/agents/<name>/<name>.md` generated from `roles/README.md` role profiles, plus an explicit `EXTRA_AGENTS` out-of-catalog path in `adapters/opencode/bin/sync-native-agents.py` (e.g. `memory-scout`, sourced from `core/MEMORY.md` §7.4 rather than a role-catalog row), with `mode: subagent` |
| Plugin hooks (JS/TS: `tool.execute.before`, `tool.execute.after`, `event`, `config`, `chat.message`, `command.execute.before`, `permission.ask`, `shell.env`, ...) | yes | `adapters/opencode/plugins/hearting-guards.js` records spec reads and design saves after tool execution; its `shell.env` blanks the inherited `AGENT_DISPATCH_CALLER_HARNESS` and other harnesses' session IDs in tool commands and sets `OPENCODE_SESSION_ID` and `AGENT_PARENT_COMPLETION_CARRIER`; its `opencode-turn` carrier starts an idle parent's next turn with the completion records owed to it (`core/ADAPTATION.md` §7.4) |
| Permission model (`permission` config: `allow`/`ask`/`deny` per tool, per-agent override) | yes | adapter documents recommended permission rules; not a harness guard replacement |
| Permission contract wrapper | yes | `adapters/opencode/bin/preflight.sh permissions` reports native permission surfaces and rejects Claude `allowedTools` as a portable contract |
| MCP servers (`mcp` config: local/remote) | yes | `adapters/opencode/bin/preflight.sh mcp` reports native MCP surfaces and rejects Claude `settings.json` MCP payloads as a portable contract |
| Model selection (`model`, `small_model`, per-agent `model`, `variant`) | yes | `adapters/opencode/bin/role-map.sh` resolves portable roles to model/variant |
| Statusline / footer | no user shell surface | TUI footer is native; harness status signals stay instruction-only/preflight |
| Shell hooks (Claude-style `settings.json` hook events) | no | harness guards run as explicit preflight wrappers |

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
entries. This does not make `roles/units/design/*` native OpenCode modes; those
mode fragments remain `mode-info status=unsupported` / `fallback=reference-only`
because they are adapter-coupled persona fragments, while the concrete
capability path is `autopilot-design` plus the visual harness contract.

`roles/units/material/browser-fetch.md` has an OpenCode-owned executable
tool-contract surface:
`adapters/opencode/bin/preflight.sh browser-fetch --check <url>` verifies
rendered browser access through `adapters/opencode/tools/material/` and reports
exit 69 when the local Playwright browser stack is unavailable.

`roles/units/material/data-script.md` is the first material mode with an
OpenCode-owned executable tool-contract surface:
`adapters/opencode/bin/preflight.sh data-script --check <script.py>` verifies
generated Python analysis scripts through `adapters/opencode/tools/material/`.

`roles/units/material/figure-gen.md` has an OpenCode-owned executable
tool-contract surface:
`adapters/opencode/bin/preflight.sh figure-gen --check <script.py>` verifies
generated matplotlib/seaborn figure scripts through
`adapters/opencode/tools/material/`. Report spectrograms additionally run
`figure-gen --verify-report <manifest.json> <report.md>` and fail closed on
metadata, scale, claim-evidence, or hash-bound visual-review drift.

`roles/units/material/pdf-extract.md` has an OpenCode-owned executable
tool-contract surface:
`adapters/opencode/bin/preflight.sh pdf-extract --check <file.pdf>` verifies
local PDF text extraction through `adapters/opencode/tools/material/` and
reports exit 69 when the local extractor is unavailable.

`roles/units/material/web-image-search.md` has an OpenCode-owned executable
tool-contract surface:
`adapters/opencode/bin/preflight.sh web-image-search --check <query>` verifies
a configured image-search provider command through
`adapters/opencode/tools/material/` and reports exit 69 when no provider is
configured.

`roles/units/qa/security-review.md` is portable read-only mode guidance for
OpenCode. It is consumed with OpenCode file and git diff tools and does not
project or invoke Claude's `/security-review` slash command.

`roles/units/research/claim-verify.md` has an OpenCode-owned executable
tool-contract surface:
`adapters/opencode/bin/preflight.sh claim-verify --check <claim>` verifies a
configured external verification provider command through
`adapters/opencode/tools/research/` and reports exit 69 when no provider is
configured.

`roles/units/qa/test.md` has an OpenCode-owned executable tool-contract
surface:
`adapters/opencode/bin/preflight.sh verification-runner --check -- <command>`
checks explicit verification commands and the same wrapper can execute them
with a bounded timeout.

## Native Plugin Hook Surface

Write-denying hook gates are retired; no write preflight or core-read marker is required.

When changing the plugin:

1. Keep it under `adapters/opencode/plugins/` as adapter-owned JS/TS.
2. Bridge to `adapters/opencode/bin/preflight.sh`, not to Claude hook files.
3. Prove discovery with `opencode debug config`.
4. Keep shell preflight wrappers as fallback so the adapter remains usable
   when plugins are disabled.

The plugin covers prompt lifecycle context, spec
read observations, and design post-write console checks. Memory has no idle
or session-end step (D-82, D-78: no automatic distiller); `mem.py` exchanges in
the background after writes and stale reads. `session.compacted` empties the
session's candidate display history through `mem.py _seen-reset`; a new
session ID starts empty on its own.

## Parity Status vs Claude

Goal of this adapter is harness behavior parity on OpenCode. The hard guard
invariants Claude enforces through `settings.json` hooks are enforced here
through the plugin and throw to abort, and the prompt/session lifecycle
injections are auto-applied. The table records the current state.

Registered headless launches allow native reads of the sealed agent home's
`capabilities/` directory (including its canonical symlink target), while
denying native edits there and retaining the existing worker write guards.
This is tool permission, not an OS filesystem sandbox. `preflight.sh read`
only records an already completed PRD read; it prints no document body and is
a no-op for capability files. Read the assigned portable contract with the
native `read` tool; a successful marker command is not evidence of its contents.

| Claude `settings.json` hook | OpenCode realization | Parity |
|---|---|---|
| `PreToolUse[Skill]` spec-skill gate (deny) | plugin `command.execute.before` → `preflight capability` (throws) | full — auto enforced (command path) |
| `PostToolUse[Read]` spec-read marker | plugin `tool.execute.after` on `read` → `preflight read` | full — auto enforced |
| `PostToolUse` design post-write | plugin `tool.execute.after` → `preflight design` | full — auto enforced |
| `SessionStart`-equivalent memory inject | plugin `experimental.chat.system.transform` → `memory`, computed once per session and re-emitted on every model call | full — auto injected, persists for the whole session |
| `UserPromptSubmit` capsule candidates / routing-contract signal / briefing | plugin `chat.message` → prompt/turn capture, then `experimental.chat.system.transform` → `candidates` / `prompt-signal` / `briefing`, computed once per user turn and re-emitted on every model call of that turn | full — candidate output is active current-project/global capsule-only, maximum six / 2,400 UTF-8 bytes; the prompt is held in plugin memory only (never written) and dropped at `session.deleted` |
| `SessionEnd` memory sync | none — `session.idle` keeps summary, pane, heartbeat and the cycle checkpoint observation only (D-82); `mem.py` exchanges after writes and reads | n/a — no session-end memory step on any adapter |
| `Stop` cycle checkpoint (`open-cycle-checkpoint.py`) | plugin `session.idle` → detached `utilities/artifact_checkpoint_trigger.py turn-end --harness opencode` with `{sessionID}` on stdin and the session's own environment (a worker's `AGENT_ARTIFACT_ROOT`/`AGENT_ARTIFACT_CYCLE_ID` included); main and worker both run it; `AGENT_ARTIFACT_CHECKPOINT=off` starts nothing | full — same launcher, interval and checkpoint child as Claude and Codex Stop (§45 D-124); no hook trust or user config is changed |
| `PreToolUse[Edit|Write|MultiEdit|NotebookEdit|Bash]` route presence gate (deny) | plugin `tool.execute.before` on `write`, `edit`, `multiedit`, `patch`, `apply_patch`, `bash` → `utilities/route_presence_gate.py --opencode` with `{tool, args, sessionID, cwd}`; exit 1 throws the one-line compose instruction. The session id is the plugin's `input.sessionID`, the same id `shell.env` exports as `OPENCODE_SESSION_ID`, which the route-chain writer records | full — auto enforced |
| `PreToolUse[Bash]` worktree-path guard (deny) | retired with the write gates; `preflight worktree-path` remains a no-op alias that prints a retired notice | none — the portable `git worktree add` path check is no longer enforced (`core/HOOKS.md:45`) |

Two items remain that cannot reach byte-for-byte Claude parity; they are
OpenCode runtime surface limits, not adapter debt:

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

### Delivery channel — why every model call is re-decorated

Claude and Codex deliver prompt-lifecycle context as
`hookSpecificOutput.additionalContext`, which merges into the user turn and
therefore stays in conversation history for the rest of the session. OpenCode
has no equivalent: `experimental.chat.system.transform` decorates the system
prompt of exactly one model call.

Measured on opencode 1.17.13 (probe plugin, one `balanced-deep`-profile model
from `adapters/opencode/config/models.conf`): the transform fires once
per model call — the session-title generation call, the answering call, and each
tool-loop continuation. A block injected once per session therefore lands on the
**title call** and never reaches the answering model. A probe token injected
under the old once-per-session rule came back `no` from the model; the same
token injected on every call came back `yes` with the third call's value.

That is why `memoryBySession` / `turnContextBySession` cache the computed blocks
and re-emit them on every call instead of injecting once. The probe/preflight
work still runs once per session (memory) or once per user turn (candidates,
prompt-signal, briefing), so this changes emission, not the `core/MEMORY.md`
caps or the process count per turn. Regression evidence: the original
`mm`-access failure — the model concluded "no mmctl, no MCP server, no API
token" while the matching capsule candidate existed — reproduces before the fix
and disappears after it (`mem show` → `mm me/teams/channels/read`).

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
| `roles/units/design/*` | Reference-only adapter-coupled mode fragments; concrete design work uses `autopilot-design` capability guidance plus `preflight.sh visual-harness` |

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
| artifact root detection | shared `utilities/artifact-root.sh` helper |
| headless/autopilot/background jobs | `preflight.sh headless` / `dispatch` / `liveness` / `harvest` provide the tool-contract path over `opencode run`; `preflight.sh status` surfaces in-flight jobs as `headless_open_jobs` / `headless_open_slugs` from the dispatch registry. A native graphical display remains optional polish |
| sibling `-wt/<slug>` dispatch detection | preserve the worktree naming invariant; choose an OpenCode-native display surface later |
| pipeline stage nudges | preflight/AGENTS instructions first; UI only when OpenCode exposes a suitable surface |
| oncall/note/study/drill/runtime-watch loop nudges | `preflight.sh briefing` plus `preflight.sh loop-info <loop>` for loop-specific support/fallback status |
| merge/rebase/merged-branch risk | `preflight.sh status` reports git operation, branch and worktree risks without blocking writes. |

## Required OpenCode Mappings

| Portable invariant | OpenCode adaptation requirement |
|---|---|
| design post-write verification | Run `adapters/opencode/bin/preflight.sh design <file>` after design HTML writes |
| routing-contract signal | OpenCode plugin system transform runs `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`; no statusline assumption |
| memory inject | OpenCode plugin system transform runs `adapters/opencode/bin/preflight.sh memory [cwd]` once per session and re-emits that cached block on every model call; run it manually when plugins are unavailable |
| memory candidate exposure / recall | The plugin captures the current user prompt in memory, runs `preflight.sh candidates <prompt> <cwd> <session-id> [turn-id]` once for that turn, and re-emits the result on every model call of the turn; the prompt is never written to disk and is dropped at `session.deleted`. It injects only bounded active capsule headlines/IDs and publishes the same-turn receipt; it does not inspect bodies or classify relevance. The model reads relevant records in full. Explicit `recall` provides deeper search and `recall-gate` recovers an unavailable probe |
| local evidence exposure | The **once-per-session** context blocks run `preflight.sh local-evidence [cwd]`, the portable `hooks/local-evidence-inject.sh` presence probe: research/documents/analysis bucket counts plus at most nine newest entry paths from the canonical artifact root, round-robined across buckets and deduplicated per artifact (2,400-UTF-8-byte bound, no body reads, no prompt classifier, silent when empty, worker-exempt, fail-open). OpenCode has no session-start context event, so `localEvidenceBySession` is the equivalent: computed once and re-emitted on every model call, the same shape `memoryBySession` already uses |
| oncall briefing | OpenCode plugin system transform runs `adapters/opencode/bin/preflight.sh briefing [cwd]`; run it manually when plugins are unavailable |
| loop guidance | Run `adapters/opencode/bin/preflight.sh loop-info <oncall|note|study|drill|runtime-watch>` before following loop guides; OpenCode reports manual contracts, missing implementations, and drill auto-run restrictions without executing loop scripts. The `note` loop and note semantics are application-owned; the harness exposes only the optional app-neutral `artifact-sink` port |
| worklog state signal | Run `adapters/opencode/bin/preflight.sh worklog [cwd]` to inspect configured `<agent-notes-root>` / `<worklog-board-app>` paths read-only before OpenCode updates notes or diagnoses board state |
| role profiles | Read `roles/README.md`, then run `adapters/opencode/bin/preflight.sh role <portable-role>` to resolve OpenCode model/variant settings |
| permission mapping | Run `adapters/opencode/bin/preflight.sh permissions` to inspect the OpenCode native permission contract and confirm Claude `allowedTools` is unsupported |
| MCP mapping | Run `adapters/opencode/bin/preflight.sh mcp --check` to inspect OpenCode's native MCP CLI/config surface; do not copy Claude `settings.json` MCP registrations or project `tools/design-mcp` wholesale |
| headless dispatch | Run `adapters/opencode/bin/preflight.sh headless --check <worktree>` before OpenCode `run` dispatch; it checks the worktree, command availability, and installed runtime projection without launching. The dispatch surface accepts `--model-profile deep|balanced-deep|light|mini` independently of optional behavioral `--model-role`. A route-bound profile resolves through `config/models.conf`; `_kernel/owner` rejects a stage `worker_mode` and may be profile-only, caller model/variant replacement is denied, and substantive registered `mini` is denied. OpenCode's `variant` axis is verified (2026-09-30, headless `opencode run --variant`), so the shipped tiers separate by variant as well as model: `balanced-deep`, `light` and `mini` are declared tiers (`mini` is the light model one variant step lower), `balanced` collapses into `light` (`collapsed-balanced-to-light`), and `deep`/`top` collapse into `balanced-deep` because no deep tier is declared (`collapsed-deep-to-balanced-deep`, `collapsed-top-to-balanced-deep`). A route-sealed `compose --pin` model is applied by the wrapper (`model_source=pin`). `runtime-default` is represented by omitting `--variant`. Registry/Fleet keeps capability mode, worker mode, role, profile, tier, and granularity separate. `--start` reruns the same projection check; liveness, harvest, merge, and cleanup boundaries remain unchanged |
| QA policy mapping | `adapters/opencode/bin/preflight.sh qa-policy <level> [code|research|doc|general]` maps the shared QA assurance budget to OpenCode role checks and fallback reporting. `stage_graph_selector=explicit-graph-or-intensity-default` preserves the core split: an explicit graph takes precedence over the default recipe; QA only scales selected checks |
| role modes | Read `roles/MODES.md`, then run `adapters/opencode/bin/preflight.sh mode-info <family/mode>`; treat adapter-coupled modes as unsupported unless wrappers exist, obey `fallback=reference-only`, and satisfy any named `tool_contract` / `tool_contract_check` before claiming tool-contract modes |
| capabilities | Read `capabilities/README.md`, then run `adapters/opencode/bin/preflight.sh capability-info <capability>`; do not assume Claude Skill invocation |
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

OpenCode uses `provider/model-id` strings and an optional `variant`. The shipped
default is `adapters/opencode/config/models.conf`; install seeds the user-owned
`agent-config/models.conf` once. Runtime consumers select one complete file and
fall back to the shipped file when the user file is invalid. Registered profiles
resolve to `deep=balanced-deep-tier` (reported as `collapsed-deep-to-balanced-deep`;
OpenCode declares no deep tier and the shipped `dispatch-defaults.yaml` keeps it out
of the deep band), `balanced-deep=balanced-deep-tier`,
`light=light-tier`, and
`mini=mini-tier` (lifecycle/micro-only), each with its declared variant. The wrapper
omits `--variant` only for `runtime-default`. `external adversary` remains unavailable
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
`usage-reset.opencode` under the canonical dispatch state root (the
registry's parent directory, SD-112 §13.33.2 — never a release-relative path;
SD-16 cache), and surfaces `early_death=`/`row_closed=`. It
also adds a `jobs_lock` flock so the SD-15 row-rewrite and concurrent appends cannot interleave.
No retry — detection/closure/surfacing only.

**Disclosed structural constraint (axis not fully realized)**: `opencode run` has a known bug
(anomalyco/opencode#8203) where it **hangs indefinitely on API errors** (rate limit / 429)
instead of exiting. The launch early-exit-watch requires the child to *exit*, so a hang-on-limit
escapes it and the row stays `open`. That case is instead caught by **axis 6** — 
`adapters/opencode/bin/dispatch-liveness.py`'s shared `route_authority.scan_anchored_death` log-tail scan (SD-15b) judges the open
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

## SD-110 runtime-owned deterministic stage advance — not an advance target

Registered standard+ owners use the shared CLI completion controller
(`utilities/claude-session-supervisor.py`, retained compatibility filename).
The native driver runs `opencode run --format json`, binds the observed
`sessionID` to the exact attempt, and resumes with `--session`. The controller
owns the live lease, exact child join, completion commit, receipt consumption,
and next model turn. A registered parent without that live lease receives the
printed bounded-wait fallback; registration alone never promises a wake.

Every registered dispatch-depth-1 owner, including quick and solo, uses this
controller (no probe; route binding and execution supervision are separate), so
`capability-route.py correct` is accepted from registration and a correction is
delivered as the next `--session` turn of the same native session. One sent after the
owner ended BLOCKED continues the route through a replacement owner (shared
`dispatch_replacement`). The first
turn runs under a placeholder session that binds to the observed `sessionID`.

Ordinary same-session continuation does not enable SD-110 deterministic
advance or SD-119 serial-chain owner support. The adapter exposes neither
`--enable-stage-advance` nor stage-advance receipt negotiation. Those surfaces
retain their checked single-session/registered-headless fallback. Interactive
OpenCode depth-0 stage advance also remains an explicit bounded wait; parent
completion instead follows the `opencode-turn` carrier (bounded wait only for
a parent running the pre-carrier plugin).

## Execution access request

`dispatch-headless.py --execution-access-file <execution_access_v1.json>` (or
`AGENT_DISPATCH_EXECUTION_ACCESS_FILE`) adds validated writable roots to the
per-launch `permission.external_directory` rules. This is
`tool-permission`, not an OS filesystem sandbox; network enforcement is
`none`. Consequently an `any` network request is `granted-unenforced`, while
`enforcement_required=os-sandbox` is refused before registration/model spawn.
The adapter never reports `enforced`, edits global configuration, or weakens
approval defaults. Cross-launch propagation is not implemented by this slice.
