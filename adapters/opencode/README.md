# OpenCode Adapter

This adapter maps the common agent harness onto OpenCode.

## Status

Experimental. OpenCode has a richer native surface than an instruction-only
runtime: it ships native commands, skills, agents, MCP, a JS/TS plugin hook
system, and a permission model. The portable contract is usable through
instruction-first preflight wrappers. OpenCode does not consume Claude Code's
`adapters/claude/settings.json`, slash command registry, hook event schema, or
statusline contract directly. `adapters/opencode/AGENTS.md` is the current
OpenCode bootstrap, loaded through the `instructions` array in
`opencode.json`/`opencode.jsonc`.

The target is harness parity on OpenCode, not Claude surface parity. Use
OpenCode native features first, including native commands, skills, agents, MCP,
permission config, and plugin hooks; add adapter wrappers only for
harness-specific signals that OpenCode does not already surface.

Native Skill projection is materialized under `adapters/opencode/skills/` from
portable `capabilities/*.md`. Native Agent projection is materialized under
`adapters/opencode/agents/` from `roles/README.md`. Native Command projection
is materialized under `adapters/opencode/commands/` from `capabilities/`.
Native guard plugin projection is materialized under `adapters/opencode/plugins/`.
Capability support still keeps explicit `preflight.sh` wrappers as fallback for
guards and tool-contract reporting.

## Worker bootstrap boundary

Headless dispatch wraps generated and caller-supplied assignments with the
portable kernel and exactly one worker type. It keeps verbose evidence in the
artifact and returns only `artifact` / `verdict` / `blocker`. The wrapper does
not manually load the full adapter bootstrap. Because a verified runtime switch
for physical project-instruction masking is not part of this adapter contract,
OpenCode reports prompt isolation as the checked fallback rather than claiming
full masking.

## Entry Points

| Surface | File |
|---|---|
| Adapter bootstrap | `adapters/opencode/AGENTS.md` |
| Core contract | `core/CORE.md` |
| Workflow routing | `core/WORKFLOW.md` |
| Shared conventions | `core/CONVENTIONS.md` |
| Git and dispatch operations | `core/OPERATIONS.md` |
| Memory contract | `core/MEMORY.md` |
| Hook invariants | `core/HOOKS.md` |
| Preflight wrappers | `adapters/opencode/bin/` |
| Native skills | `adapters/opencode/skills/` |
| Native agents | `adapters/opencode/agents/` |
| Native commands | `adapters/opencode/commands/` |
| Native guard plugin | `adapters/opencode/plugins/hearting-guards.js` |
| Capabilities | `capabilities/README.md` |
| Role profiles | `roles/README.md` |
| Hook and guard scripts | `hooks/`, `utilities/` |
| Selected tool projection | `adapters/opencode/tools/` |
| Selected utility projection | `adapters/opencode/utilities/` |

## Runtime Mapping

| Core Concept | OpenCode Implementation |
|---|---|
| capability | Read `capabilities/README.md` for meaning; run `adapters/opencode/bin/preflight.sh capability-info <capability>` to confirm OpenCode realization; use `adapters/opencode/skills/<capability>/SKILL.md` as OpenCode-native guidance |
| native skill/command/agent surface | Skills are materialized under `adapters/opencode/skills/`; agents are materialized under `adapters/opencode/agents/`; commands are materialized under `adapters/opencode/commands/`. Future output must be generated from portable capability/role sources and verified with OpenCode discoverability (`opencode debug skill`, `opencode debug agent`, `opencode debug config`) |
| stage-session capacity | `dispatch-headless.py` projects the same phase brief, fixed-file fence, ledger, and `stage_authority=0` metadata as Claude/Codex. OpenCode's native-agent surface is not yet route-owned dispatch-depth-2 evidence, so the checked registered/inline fallback remains authoritative and no native parity is claimed. |
| role profile | Use `roles/README.md` for meaning; use `adapters/opencode/agents/<role>/<role>.md` as OpenCode-native role guidance, and use Claude agent files only as compatibility references |
| role mode | Run `adapters/opencode/bin/preflight.sh mode-info <family/mode>` before using a `roles/units/` fragment; portable modes can be used directly, tool-contract modes require equivalent tools, unsupported modes report `fallback=reference-only` when no OpenCode-native runtime surface exists |
| adapter bootstrap | `adapters/opencode/AGENTS.md` reaches a session as the auto-loaded `AGENTS.md` in the global config home, and must not also be listed in `instructions[]` (`core/ADAPTATION.md §6.1`); then load `core/CORE.md` plus task-relevant shared docs; do not treat `CLAUDE.md` as portable bootstrap |
| agent home | Set `AGENT_HOME` to the installed harness directory |
| permission model | Run `adapters/opencode/bin/preflight.sh permissions`; use OpenCode native `permission` config and plugin hooks, not Claude `allowedTools` |
| MCP config | Run `adapters/opencode/bin/preflight.sh mcp [--check]`; use OpenCode native `opencode mcp`/config surfaces, not Claude `settings.json` MCP payloads |
| artifact root | primary-checkout canonical `.agent_reports` via `utilities/artifact-root.sh`; linked-worktree snapshots are read-only; legacy fallback only at the canonical root |
| worktree cleanup | `preflight.sh worktree-cleanup`; dry-run first, apply only after merge + integrated verification + push |
| routing-contract signal | OpenCode plugin system transform runs `adapters/opencode/bin/preflight.sh prompt-signal [cwd] [session-id]`; run it manually when plugins are unavailable |
| harness status snapshot | Run `adapters/opencode/bin/preflight.sh status [cwd] [session-id]` for read-only artifact, notes, worktree, and git-risk signals. This does not replace OpenCode native model/context/session UI |
| token self-regulation v2 | Phase 2 automatic hook accounting and the Phase 3 isolated experiment CLI are deferred. Shared Fleet modules may be inspected as portable source, but OpenCode projects no token-budget utility, production hook, activation flag, or runtime-config mutation |
| adapter readiness | Run `adapters/opencode/bin/preflight.sh doctor` to check manifest freshness, native projections, and boundary rules in one command |
| headless dispatch | Tool-contract check: `adapters/opencode/bin/preflight.sh headless --check <worktree>` verifies the worktree, `opencode run` availability, and installed OpenCode runtime projection (`hearting`, native Skills path, native Agents, native Commands, and guard plugin). Use `adapters/opencode/bin/preflight.sh dispatch --dry-run|--register|--start --worktree <path> --slug <slug> --capability <name> --capability-mode <mode> [--worker-mode <family/mode>] --qa <level> [--agent <agent>] (--model-profile <deep|balanced-deep|light|mini> [--model-role <portable-role>]|--model-role <portable-role>|--model <model> --variant <variant>|--inherit-model-settings)` to build the command and register open jobs. The optional worker mode is a non-owner projection that must equal the selected portable unit; `_kernel/owner` rejects a stage mode and accepts a route-sealed owner profile alone. Route-bound profiles select the complete user `~/.config/opencode/agent-config/models.conf` when valid and otherwise the complete shipped `config/models.conf`. Installation seeds the user copy once and never rewrites or removes it. Caller model/variant replacement is rejected, and substantive registered `mini` is denied. OpenCode declares its own `balanced-deep`, `light` and `mini` tiers (no deep tier: `deep` and `top` collapse onto balanced-deep), records `profile_granularity` per profile (`full`, `collapsed-deep-to-balanced-deep`, `collapsed-balanced-to-light`, `collapsed-top-to-balanced-deep`), applies a route-sealed `compose --pin` model (`model_source=pin`), and omits `--variant` when the resolved value is `runtime-default`. Registry/Fleet rows keep capability mode, worker mode, role, profile, tier, and granularity separate. `--start` reruns the same runtime projection check before launching. Use `liveness` while waiting and `harvest --mark-done` after main-session harvest; merge and cleanup remain outside the wrapper |
| QA policy mapping | `adapters/opencode/bin/preflight.sh qa-policy <level> [code|research|doc|general]` maps portable QA levels from `core/CONVENTIONS.md` to OpenCode assurance scope, selected-pass reviewer budgets, external-adversary requirements, max rounds, and inline fallback reporting. `stage_graph_selector=explicit-graph-or-intensity-default` means these budgets do not open stages or depth by themselves |
| material browser fetch | Tool-contract check: `adapters/opencode/bin/preflight.sh browser-fetch --check <url>` verifies rendered browser access through the adapter-owned Playwright launcher before using `roles/units/material/browser-fetch.md`. Exit 69 means the local browser stack is unavailable |
| material data script | Tool-contract check: `adapters/opencode/bin/preflight.sh data-script --check <script.py>` verifies generated Python analysis scripts through the adapter-owned launcher before using `roles/units/material/data-script.md` |
| material figure generation | Tool-contract checks: `adapters/opencode/bin/preflight.sh figure-gen --check <script.py>` verifies generated matplotlib/seaborn scripts; report spectrograms additionally require `figure-gen --verify-report <manifest.json> <report.md>` for metadata, claim-evidence, scale, and hash-bound visual-review QA before using `roles/units/material/figure-gen.md` |
| material PDF extract | Tool-contract check: `adapters/opencode/bin/preflight.sh pdf-extract --check <file.pdf>` verifies local PDF text extraction through the adapter-owned launcher before using `roles/units/material/pdf-extract.md`. Exit 69 means the local extractor is unavailable |
| material web image search | Tool-contract check: `adapters/opencode/bin/preflight.sh web-image-search --check <query>` verifies that `OPENCODE_WEB_IMAGE_SEARCH_CMD` or `AGENT_WEB_IMAGE_SEARCH_CMD` provides a local image-search command before using `roles/units/material/web-image-search.md`. Exit 69 means no provider is configured |
| QA security review | Portable read-only persona: `roles/units/qa/security-review.md` is consumed with OpenCode file and git diff tools. Do not project or invoke Claude `/security-review` |
| QA verification runner | Tool-contract check: `adapters/opencode/bin/preflight.sh verification-runner --check -- <command>` verifies explicit QA/test commands through the adapter-owned runner before using `roles/units/qa/test.md` |
| research claim verify | Tool-contract check: `adapters/opencode/bin/preflight.sh claim-verify --check <claim>` verifies that `OPENCODE_CLAIM_VERIFY_CMD` or `AGENT_CLAIM_VERIFY_CMD` provides an external verification command before using `roles/units/research/claim-verify.md`. Exit 69 means no provider is configured |
| design post-write verification | `core/HOOKS.md` defines the invariant; run `adapters/opencode/bin/preflight.sh design <file>` after design HTML writes |
| design visual harness | Tool-contract check: `adapters/opencode/bin/preflight.sh visual-harness <file.html>` runs the adapter-owned render/screenshot/console wrapper. Inspect the reported screenshot before claiming visual completion. Do not project Claude Design MCP files into OpenCode |
| memory injection | OpenCode plugin system transform runs `adapters/opencode/bin/preflight.sh memory [cwd]` once per session and re-emits the cached block on every model call, because a system-transform block only lives for the one call it decorates; run it manually when plugins are unavailable |
| memory candidate exposure and deeper retrieval | OpenCode captures the current user prompt in plugin memory, runs `preflight.sh candidates` once for that turn, and re-emits the result on every model call of the turn (see ADAPTATION "Delivery channel"). The result is capsule-only, active current-project/global, and bounded to six headlines/IDs and 2,400 UTF-8 bytes. The same-turn message ID flows into the mutation gate. The prompt is never written to disk and is dropped at `session.deleted`. The model decides relevance and reads full records; explicit `recall` remains available for deeper search and `recall-gate` for hook recovery |
| oncall briefing injection | OpenCode plugin system transform runs `adapters/opencode/bin/preflight.sh briefing [cwd]`; run it manually when plugins are unavailable |
| loop guidance | `adapters/opencode/bin/preflight.sh loop-info <oncall|note|study|drill|runtime-watch>` reports whether a loop has an OpenCode manual contract, unsupported executable projection, or missing native implementation; `note` is application-owned and the harness exposes only the optional app-neutral `artifact-sink` port |
| capability mapping | `adapters/opencode/bin/preflight.sh capability-info <capability>` reports OpenCode's native Skill/command realization and instruction-only or tool-contract status; root Skill compatibility references are not projected and report `compat_reference=not-projected` |
| model role mapping | `adapters/opencode/bin/preflight.sh role <portable-role>` resolves portable model roles through OpenCode adapter environment variables |
| mode mapping | `adapters/opencode/bin/preflight.sh mode-info <family/mode>` reports whether a mode is portable, tool-contract, or unsupported for OpenCode; tool-contract and unsupported adapter-coupled modes include machine-readable `tool_contract`, optional `tool_contract_check`, `runtime_surface`, and `fallback` fields |
| memory sync | None at `session.idle` (D-82; `preflight session-end` is removed). `mem.py` exchanges in the background after writes and stale reads; `session.compacted` empties the candidate display history. No automatic distiller (D-78) |
| memory store | `tools/memory/{mem.py,protocol_v2.py,git_exchange_v2.py,sync_v2.py}` are runtime-neutral |
| permission model | OpenCode native `permission` config (`allow`/`ask`/`deny` per tool, per-agent override); adapter documents recommended rules, not a harness guard replacement |
| statusline | OpenCode TUI footer is native; no user shell statusline surface in config schema; harness status signals stay instruction-only/preflight |

## Tool Projection

`opencode_setting/tools` intentionally points at `adapters/opencode/tools/`,
not the full shared `tools/` directory. The adapter currently exposes only
tools that OpenCode wrappers use directly:

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

Harness development tools and Claude-coupled helper surfaces such as
`build-manifest.py` and `web-bundle` stay out of the OpenCode projection until
OpenCode has a documented runtime realization for them. The shared `design-mcp`
package is not projected wholesale; OpenCode exposes only the adapter-owned
visual harness launcher.

## Utility Projection

`opencode_setting/utilities` intentionally points at
`adapters/opencode/utilities/`, not the full shared `utilities/` directory.
The adapter currently exposes only utility files that OpenCode wrappers or
docs use:

- `agent-home.sh` (OpenCode-owned wrapper; no Claude runtime-home fallback)
- `artifact-root.sh`
- `agent-worklog-state.sh`
- `harness-status.sh`
- `dispatch-route.sh` (read-only SD-23 selector; returns model `unknown` until an OpenCode probe exists)

Claude-specific helpers such as the shared `dispatch-liveness.sh` stay out of
the OpenCode projection. OpenCode exposes its adapter-owned liveness command
through `adapters/opencode/bin/preflight.sh liveness [jobs.log]`, backed by
`~/.local/share/opencode/opencode.db` session metadata and update times.
OpenCode also exposes `adapters/opencode/bin/preflight.sh harvest` for
registry-only status and selected `open` to `done` updates. It intentionally
does not merge branches or delete worktrees.

## Native Skill Projection

`adapters/opencode/skills/` contains OpenCode-native Skill projections generated
from `capabilities/*.md`:

All core projections are generated and checked through one command:

```bash
python3 tools/generate.py --check
```

Expose them to OpenCode through `opencode_setting/opencode-skills`, not through
a `skills/` projection. The plain `skills/` name is reserved for historical
Claude compatibility references.

## Native Agent Projection

`adapters/opencode/agents/` contains OpenCode-native Agent projections
generated from portable role profiles in `roles/README.md`. They declare
`mode: subagent` and defer concrete model/variant selection to
`adapters/opencode/bin/preflight.sh role <portable-role>`:

They are covered by `python3 tools/generate.py --check`.

Expose them to OpenCode by symlinking each generated `*.md` file into
`$HOME/.config/opencode/agent/` or a project `.opencode/agent/` directory,
using `opencode_setting/opencode-agents` as the projection source. Do not expose
`adapters/claude/agents/` as OpenCode-native agents.

## Native Command Projection

`adapters/opencode/commands/` contains OpenCode-native command projections
generated from portable `capabilities/*.md` specs. Each command includes
OpenCode's `$ARGUMENTS` placeholder so runtime command arguments are visible to
the portable capability contract:

They are covered by `python3 tools/generate.py --check`.

Expose them to OpenCode by symlinking each generated `*.md` file into
`$HOME/.config/opencode/command/` or a project `.opencode/command/` directory,
using `opencode_setting/opencode-commands` as the projection source. Do not
expose `adapters/claude/commands/` as OpenCode-native commands.

## Native Guard Plugin Projection

Write-denying hook gates are retired; no write preflight or core-read marker is required.

```bash
node --check adapters/opencode/plugins/hearting-guards.js
```

Expose it to OpenCode by symlinking the generated projection into a project or
global plugin directory:

```bash
mkdir -p .opencode/plugins
ln -sfn "$AGENT_HOME/opencode_setting/opencode-plugins/hearting-guards.js" .opencode/plugins/hearting-guards.js
```

The plugin bridges to `adapters/opencode/bin/preflight.sh`; it does not copy or
invoke Claude hook files. Keep explicit `preflight.sh` calls as the fallback
path for runtimes or invocations where plugins are disabled. When a runtime
loads a copied plugin file instead of a symlinked projection, set `AGENT_HOME`
to the harness repo so the plugin can resolve `adapters/opencode/bin/preflight.sh`.

## Runtime Home Projection

Target layout:

```text
$HOME/hearting/             # canonical neutral repo
$HOME/.config/opencode/     # OpenCode global config home
$HOME/.local/share/opencode/  # OpenCode data home (DB, logs, snapshots)
```

OpenCode runtime state such as `auth.json`, `opencode.db`, logs, snapshots,
and tool output should stay under `$HOME/.local/share/opencode` and
`$HOME/.config/opencode`. The neutral harness should be referenced from
OpenCode through the auto-loaded bootstrap file described below. At minimum,
the OpenCode adapter should expose a stable pointer back to the neutral repo:

```text
$HOME/.config/opencode/hearting -> $HOME/hearting
```

OpenCode auto-loads `AGENTS.md` from the global config home, so the bootstrap
is delivered by that link and the config carries no `instructions` entry for it:

```text
$HOME/.config/opencode/AGENTS.md -> <active harness source>/adapters/opencode/AGENTS.md
```

`harness runtime activate` owns that link and points it at the active immutable
release/snapshot. Adding the same bootstrap to `instructions[]` as well loads it
twice — OpenCode dedupes instruction sources by resolved path, so the projection
and the link count as two different sources (`core/ADAPTATION.md §6.1`). Only a
config home without that link needs the entry, which is why
`harness install opencode` merges it only when the link is absent.

Further OpenCode-specific files can be added under `adapters/opencode/` and
symlinked or generated into the config home as the adapter matures.

## Model Role and Execution-Profile Mapping

OpenCode uses `provider/model-id` strings and a `variant` (verified 2026-09-30 with
`opencode run --format json --variant <v>` against each declared tier's model; the
supported values per model are recorded in `config/models.conf`; the wrapper omits
`--variant` only when the resolved value is `runtime-default`). There
is no numeric effort axis. Behavioral roles resolve through `preflight.sh
role`, while route-bound registered work carries one of these sealed profiles:

| Model profile | OpenCode realization | Granularity |
|---|---|---|
| `deep` | configured **balanced-deep** tier (its declared variant, `xhigh`) | collapsed (`collapsed-deep-to-balanced-deep`): OpenCode declares no deep tier, and the shipped `dispatch-defaults.yaml` keeps OpenCode out of the deep band, so it takes deep work only when explicitly requested (`--owner opencode`, `--pin`) |
| `balanced-deep` | configured balanced-deep tier (variant `xhigh`) | distinct |
| `balanced` | configured light tier (variant `max`) | collapsed to `light` (`collapsed-balanced-to-light`) |
| `light` | configured light tier (variant `max`) | distinct |
| `mini` | configured mini tier (the light model, variant `high`, one step below light) | distinct variant; lifecycle/micro-only, substantive dispatch-depth-1/2 work is rejected |
| `top` | configured balanced-deep tier | collapsed to `balanced-deep` (`collapsed-top-to-balanced-deep`): this account has no model above it, so a route that sealed the `top` exception profile for its owner still runs here, typed as a demotion; the wrapper refuses `top` without that route, and no `--model` override runs under the `top` label. `--inherit-model-settings` follows the rule every adapter shares: it is refused while `CFG_MAIN_SESSION_ONLY_MODELS` names a model, and the shipped file declares none, so it is accepted here. A user copy that declares the key gets the same main-session-only refusal as Claude and Codex (receipt `main_session_only_policy=declared|absent`) |

The portable policy assigns `balanced-deep` to quick one-shot conduction and `deep` to
every standard+ owner. OpenCode preserves those sealed labels, but its current
runtime realization maps both to the configured balanced-deep tier and reports that
reduced granularity explicitly. A route can choose another OpenCode model once with
`compose --pin <owner|frame|worker>=opencode:<provider/model>[@<variant>]`; the
wrapper then reports `model_source=pin`.

Non-route role compatibility overrides remain explicit and config-derived:

```text
AGENT_MODEL_FAST
AGENT_MODEL_DEEP
AGENT_MODEL_EXTERNAL
AGENT_MODEL_ORCHESTRATOR
AGENT_VARIANT_FAST
AGENT_VARIANT_DEEP
AGENT_VARIANT_EXTERNAL
AGENT_VARIANT_ORCHESTRATOR
AGENT_EXTERNAL_CMD
```

The adapter reports reduced profile granularity instead of claiming four-step
parity, and it omits a `--variant` argument for `runtime-default`. Environment
role overrides remain available outside a route; they cannot replace a sealed
profile. `external adversary` remains unavailable unless
`AGENT_MODEL_EXTERNAL` or `AGENT_EXTERNAL_CMD` provides independent execution.

## Compatibility

OpenCode should create new project artifacts only under the root returned by
`utilities/artifact-root.sh`. In a linked task worktree this is the primary
checkout's `.agent_reports/`, not the tracked local snapshot. The dispatch
wrapper injects `AGENT_ARTIFACT_ROOT` and adds exact
`permission.external_directory` allow rules while retaining all other config;
legacy `.claude_reports/` remains a canonical-root fallback.

OpenCode should resolve harness-home paths through `AGENT_HOME` or the
OpenCode-owned `utilities/agent-home.sh`. Some shared legacy tools still accept
`CLAUDE_HOME` as a migration alias, but OpenCode-owned wrappers should not use
it as their runtime-home fallback.

Claude Code-specific files remain valid as implementation references, not as
OpenCode bootstrap files:

- `CLAUDE.md` contains Claude Code routing and response rules.
- `adapters/claude/settings.json` registers Claude Code hooks and permissions.
- `adapters/claude/commands/` defines Claude Code slash commands.
- `skills/*/SKILL.md` is still Claude Skill format; start from
  `capabilities/README.md` for portable meaning. OpenCode auto-loads
  `~/.claude/skills/` as a compat convenience, but the adapter must not depend
  on it.
- `adapters/claude/statusline.sh` targets Claude Code's statusline contract.

For native OpenCode surface checks, disable the Claude compatibility autoload:

```bash
OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 \
OPENCODE_CONFIG_CONTENT='{"skills":{"paths":["/path/to/hearting/opencode_setting/opencode-skills"]}}' \
  opencode debug skill --pure
```

When porting a behavior, copy the underlying invariant from `CORE.md`,
`WORKFLOW.md`, `CONVENTIONS.md`, or `OPERATIONS.md`; then map it to OpenCode's
tool, permission, agent, and session model.

### SD-88 demand selection

`preflight.sh compose --profile-demands demands.json` (Codex/OpenCode) and the
portable `utilities/capability-route.py compile|compose` consume the same JSON map:
keys are realized node IDs or `__owner__`, values are full schema-v1 demands with
both axes, reasons and evidence references. `--explicit-profiles profiles.json`
uses a matching map and cannot bypass the judgment floor. Custom unit recipes
provide `profile_demand` on every ad-hoc unit. Missing fields fail closed.

### SD-165 part tokens

`stages [--capability <name>] [--json]` prints the part catalog a graph is
assembled from: every stage as `capability:stage` with its one-line summary,
inputs and outputs, `unit_choices`, `shareable`, `start_approval`, optional parts,
and the parts of other recipes this host can borrow. `compose --graph` accepts a
host stage id or a shareable `capability:stage` (for example
`inspect,autopilot-research:retrieval,autopilot-research:synthesis,report`); a
borrowed part is written under `parts/<capability>/<stage>/` inside the host's own
artifact scope. Claude reaches both through `adapters/claude/bin/capability-route.py`,
Codex and OpenCode through `preflight.sh compose|stages`; every surface forwards
the arguments unmodified and prints the same output.

When assembling `staged` routes, follow the shared
[WORKFLOW §0.2.1 guidance](../../core/WORKFLOW.md#021-shape-before-preset-sd-135).

The five portable profiles are deep, balanced-deep, balanced, light and mini.
Concrete defaults and generated native agents come from this adapter's
`config/models.conf`; runtime loading selects the user's whole file first.
A missing balanced row is derived only from that user's light row in memory;
explicit settings win, and other missing required keys retain whole-file fallback.
OpenCode reports collapsed-balanced-to-light and preserves its full light budget.
No install/update/reapply/uninstall writes the normalization back. Source checks
do not activate an installed release or change an in-flight sealed route.

The plugin retains `experimental.chat.system.transform` for lifecycle context and `tool.execute.after` for spec-read observations and `preflight.sh design` checks.
