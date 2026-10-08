# Codex Adapter

This adapter maps the common agent harness onto Codex-style sessions.

## Status

Experimental. The portable contract is usable, but Codex does not consume Claude Code's `adapters/claude/settings.json`, slash command registry, or hook event schema directly. `adapters/codex/AGENTS.md` is the current Codex-style bootstrap, and wrappers should still run guard scripts as deterministic checks where native hooks are unavailable.

The target is harness parity on Codex, not Claude surface parity. Use Codex
native features first, including built-in slash commands and `/statusline`; add
adapter wrappers only for harness-specific signals that Codex does not already
surface.

## Worker bootstrap boundary

Headless dispatch injects `roles/worker-bootstrap.md`, exactly one
`roles/worker-types/{owner,stage,review,support}.md`, and the assigned
capability/stage contract. It does not explicitly read the full adapter
`AGENTS.md`. Worker evidence stays in the artifact and the return is the fixed
`artifact` / `verdict` / `blocker` envelope. Codex may still auto-discover the
target project's `AGENTS.md`; no verified per-worker disable switch is claimed,
so this is prompt isolation with a documented physical-masking fallback.

Registered-headless child attempts are exact-parent bound across both Codex and
Claude Code. Codex liveness/harvest normalize Codex `turn.completed` and Claude
stream-json `result` envelopes, while parent-death reconciliation uses only
sealed attempt and PID/start/PGID evidence; runtime-native subagents are a
separate surface.

Codex native Skill projection is materialized under `adapters/codex/skills/`
from `capabilities/`. Codex custom agent projections are materialized under
`adapters/codex/agents/` from `roles/`. A Codex plugin projection is materialized under
`adapters/codex/plugins/hearting-codex` and exposed through the repo-local
marketplace projection at `adapters/codex/plugin-marketplace/`. Do not
project Claude Skill, Agent, command, hook, or statusline files into Codex.

## Entry Points

| Surface | File |
|---|---|
| Adapter bootstrap | `adapters/codex/AGENTS.md` |
| Core contract | `core/CORE.md` |
| Workflow routing | `core/WORKFLOW.md` |
| Shared conventions | `core/CONVENTIONS.md` |
| Git and dispatch operations | `core/OPERATIONS.md` |
| Memory contract | `core/MEMORY.md` |
| Hook invariants | `core/HOOKS.md` |
| Preflight wrappers | `adapters/codex/bin/` |
| Capabilities | `capabilities/README.md` |
| Role profiles | `roles/README.md` |
| Role mode inventory | `roles/MODES.md` |
| Hook and guard scripts | `hooks/`, `utilities/` |
| Native skills | `adapters/codex/skills/` |
| Native agents | `adapters/codex/agents/` |
| Native mode guides | `adapters/codex/modes/` |
| Native plugin | `adapters/codex/plugins/hearting-codex` |
| Native hooks | `adapters/codex/hooks/` |
| Design scaffolds | `adapters/codex/scaffolds/` |
| Selected tool projection | `adapters/codex/tools/` |
| Selected utility projection | `adapters/codex/utilities/` |

## Runtime Mapping

| Core Concept | Codex Implementation |
|---|---|
| capability | Read `capabilities/README.md` for meaning; run `adapters/codex/bin/preflight.sh capability-info <capability>` to confirm Codex realization; use `adapters/codex/skills/<capability>/SKILL.md` as Codex-native guidance |
| native skill/plugin surface | Skills are materialized under `adapters/codex/skills/`; the installable plugin projection is materialized under `adapters/codex/plugins/hearting-codex`. Command-like capability entrypoints use these native Skills/plugin surfaces and are verified with Codex discoverability (`codex debug prompt-input`) |
| native hook surface | `adapters/codex/hooks/hooks.json` registers Codex `SessionStart` lifecycle prep, `SessionEnd` (Fleet cleanup and final summary; no memory step), a silent `Stop` boundary, `UserPromptSubmit` bounded capsule candidates plus prompt signals and turn nudges, privacy-minimal `PermissionRequest` approval-wait publication, `PostToolUse` approval-wait release, spec read markers, design HTML checks, and worker-only `PreCompact`/`PostCompact` ledger flush/re-anchor. Interaction bridges keep stdout empty and never own approve/deny; Stop has no completion authority or wildcard parent park |
| stage-session capacity | All registered wrappers share the portable sub-session axes. Codex supplies the phase brief plus a persistent ledger anchor in the generated prompt and environment; write hooks no longer enforce ledger cadence or file fences. Sub-sessions cannot publish a stage marker. One chain command registers the exact set and synchronously joins it inside the App Server-supervised owner, so that owner returns once after the chain. |
| role profile | Use `roles/README.md` for meaning; Codex custom agents are materialized under `adapters/codex/agents/*.toml`; `adapters/codex/bin/preflight.sh role <portable-role|role-profile|pipeline-stage>` resolves both concrete model roles and pipeline profiles such as `planning`, `implementation`, `verification`, and `report` |
| role mode | Run `adapters/codex/bin/preflight.sh mode-info <family/mode>` before using a `roles/units/` fragment; use the reported `native_mode_path` under `adapters/codex/modes/`; portable modes can be used directly, tool-contract modes require equivalent tools, unsupported modes report `fallback=reference-only` when no Codex-native runtime surface exists |
| native mode surface | Mode guides are generated under `adapters/codex/modes/` from `roles/units/`; design modes additionally require the Codex visual-harness tool contract before claiming rendered visual completion |
| adapter bootstrap | Load `adapters/codex/AGENTS.md`, then `core/CORE.md` plus task-relevant shared docs; do not treat `CLAUDE.md` as portable bootstrap |
| agent home | A valid explicit `AGENT_HOME` wins. Otherwise Codex-owned wrappers resolve `$HOME/.codex/hearting`, canonical `$HOME/hearting`, or legacy `$HOME/agent_setting` before falling back to their source checkout; invoking `preflight.sh` through a linked feature worktree therefore does not activate that worktree as the runtime harness root |
| permission model | Run `adapters/codex/bin/preflight.sh permissions`; use Codex native approval policy and sandbox settings, not Claude `allowedTools` |
| MCP config | Run `adapters/codex/bin/preflight.sh mcp [--check]`; use Codex native `codex mcp`/config surfaces, not Claude `settings.json` MCP payloads |
| artifact root | primary-checkout canonical `.agent_reports` via `utilities/artifact-root.sh`; linked-worktree snapshots are read-only; legacy fallback only at the canonical root |
| worktree cleanup | `preflight.sh worktree-cleanup`; dry-run first, apply only after merge + integrated verification + push |
| routing-contract signal | `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]` is a worker-startup/manual subcommand (not a per-turn injection) whose output includes `routing_contract=core/WORKFLOW.md`, `routing_action=read-workflow-and-select-codex-skill`, `capability_entrypoints=codex-native-skills`, and git dirty/worktree/dead-branch risk fields (re-derived from the harness status snapshot); run it manually or at worker startup |
| harness status snapshot | Run `adapters/codex/bin/preflight.sh status [cwd] [session-id]` for read-only artifact, notes, worktree, and git-risk signals, including tracked-dirty vs untracked counts and sibling worktree counts. This does not replace Codex `/statusline` for model/context/token/session fields |
| token/context pressure | Run `adapters/codex/bin/preflight.sh token-budget [cwd] [session-id] [kv|json|hook]` for exact-session rollout telemetry. `kv`/`json` are read-only L2 diagnostics. `hook` preserves Phase 1 output byte-for-byte while the UserPromptSubmit parent records exactly one content-free hashed-session outcome in bounded XDG accounting (8 KiB/file, 256 files, 2 MiB, atomic lock/replace/prune, fail-open). Active context, exact inserted directive bytes, and monotonic cumulative session-counter delta remain separate; no tokenizer estimate, billing, savings, cost, or ROI is derived. Normal, unknown, same-band, degraded/failure, and validated-native paths are zero-injection. Pressure may shorten output/defer optional extras only and never changes intensity, dispatch/depth, model role, required tools/tests/safety/input, or guards. The explicit `utilities/token-budget-experiment.py` replay/evaluator is production-disabled, never imported by hooks/preflight, and cannot adopt. Native `rollout_budget` remains a separately validated opt-in because it is under-development and disabled in local Codex 0.144.3; the adapter never edits runtime-owned `$CODEX_HOME/config.toml` |
| UI boundary | Run `adapters/codex/bin/preflight.sh ui-info` to report the Codex-native UI boundary. Codex `/statusline` and `/title` configure built-in footer/title items; arbitrary Claude-style live statusline scripts are unsupported, so harness signals use `preflight.sh status`. Codex hooks run silently (no `statusMessage` labels), matching Claude Code's quiet hooks. `/statusline` persists choices in runtime-owned `$CODEX_HOME/config.toml`; `codex_setting/codex-config/tui-statusline.toml` records the harness-recommended footer fragment without projecting the full config file. Run `adapters/codex/bin/preflight.sh tui-config` only when explicitly applying that fragment to the runtime-owned config |
| adapter readiness | Run `adapters/codex/bin/preflight.sh doctor` to check manifest freshness, native skill/plugin/agent/mode projections, native subagent feature availability, hook bridge syntax, and boundary rules in one command. Add `--runtime` to include the installed `$CODEX_HOME` projection check; use `--runtime-strict` when complete hook trust must be proven |
| runtime projection install | Run `adapters/codex/bin/install-runtime-projection.sh [--install-plugin] [--skills-mode native|plugin|both]` to wire `$CODEX_HOME` (default `$HOME/.codex`) to the harness projection: `agent-*` pointers for bootstrap, common docs, capabilities, roles, bin/tools/utilities, scaffolds, hooks, selected native skill discovery, native agents/modes/plugin marketplace, `hooks.json`, native agent symlinks, and a one-time user-owned `agent-config/models.conf` default. The model file is a real file, never a projection; install, update, reapply, and uninstall preserve an existing copy byte-for-byte. The managed launcher and managed entry are retired: the installer retires the managed launcher from the recorded metadata and `harness uninstall codex` restores the prior binding without removing the user model file; interactive `codex`, `resume`, and `fork` start the native Codex CLI directly, while headless/administrative commands pass through unchanged. The operation never touches Codex credentials, sessions, history, logs, caches, `config.toml`, or local databases (a pre-existing real `hooks.json` is backed up to `hooks.json.pre-harness`). Default skill discovery is native symlinks; with `--install-plugin`, the default is plugin discovery to avoid duplicate skill metadata. Run `adapters/codex/bin/check-runtime-projection.sh`, `adapters/codex/bin/preflight.sh runtime-projection`, or `adapters/codex/bin/preflight.sh doctor --runtime` for a read-only `status=ok|failed` wiring/discovery validation. Non-strict checks skip the App Server trust probe to keep every headless child launch cheap. Strict `--require-hook-trust`/`--runtime-strict` checks read trust authoritatively through App Server `hooks/list`, including each definition's current hash; `check=hook-trust:review-needed` means run `/hooks` in Codex and trust the changed harness hooks |
| dispatch-owner selection | An ordinary dispatch-depth-1 owner launches through `preflight.sh dispatch-owner [--adapter <harness>] --dry-run|--register|--start`, a separate surface from the `headless dispatch` row below. `utilities/dispatch-owner.py` prefers the user-owned `${XDG_CONFIG_HOME:-~/.config}/hearting/dispatch-defaults.yaml` and falls back to the shipped profile. Schema v3 runs explicit target → hard eligibility → profile quality band → fresh headroom → recent exact-attempt tie-break, so capacity can reorder Claude/Codex peers or cross an explicit relief threshold but cannot make OpenCode a deep quality peer. The selector preserves the actual caller runtime separately from the selected owner adapter and forbids caller-supplied completion-policy or unmanaged-poll flags |
| headless dispatch | Tool-contract check: `adapters/codex/bin/preflight.sh headless --check <worktree>` verifies the exact final isolated worktree, Codex CLI/App Server availability, and installed runtime projection with native Skills, native Agents, and native Modes. Its receipt distinguishes a runtime-global outage from an exact-worktree collision through `failure_scope`, `codex_command`, and `retry_on_isolated_worktree`; a worktree-local `.codex` collision requires re-isolation and reprobe and cannot trigger cross-harness fallback or a sandbox bypass. Standard+ dispatch-depth-1 owners default to `--completion-delivery auto`: a successful `codex app-server --help` probe selects `app-server-supervised`; forced `supervised` fails before registration when unavailable, and `poll` or an unavailable auto probe reports `poll-fallback`. Quick workers remain one-shot `codex exec`; stage/review workers use one ephemeral App Server turn for usage when the existing availability probe succeeds, with raw `codex exec --ephemeral --json` as the pre-inference unavailable fallback. This telemetry transport does not change one-shot completion or owner supervision. The wrapper validates the capability catalog's scalar `capability_mode`, validates an optional non-owner `worker_mode` through `mode-info`, and keeps both separate; `_kernel/owner` rejects a worker mode before prompt or registry writes and is valid with its route-sealed owner profile alone. Route-bound jobs resolve `--model-profile deep|balanced-deep|light|mini` through `config/models.conf`, keep `model_role` independent, reject caller model/reasoning replacement, and reject substantive registered `mini`. Registry/Fleet rows emit capability/worker mode plus model profile, resolved tier, and profile granularity as separate fields. Registry writes and harvest rewrites are serialized with a `.lock` file. `--register` and `--start` materialize the minimal typed worker prompt. A direct registered dispatch-depth-1 start binds completion mechanics to the actual parent runtime: native Codex records `codex-native-queue`, Claude retains `claude-parent-runtime`, and a gateway-free Codex parent is admitted with its calling CODEX_THREAD_ID. Only a human using the low-level wrapper may explicitly authorize finite recovery with `--allow-unmanaged-parent-poll`; `dispatch-owner` and model routes cannot select it. The path neither creates new native Stop state nor requires Stop/PreToolUse hook trust. Approval, route, liveness, harvest, and cleanup boundaries remain unchanged |
| completion delivery | Interactive Codex parents use the native per-thread queue, addressed to CODEX_THREAD_ID. Delivery is at-least-once with exact pending/history checks. The launcher and interactive gateway are retired; registered headless owners retain their private App Server supervisor. The TUI remains the sole question/approval owner. |
| launch publication settle | When an App Server owner turn ends during the atomic register-to-`launch_started=1` publication window, with or without the runtime-wait sentinel, the Codex supervisor briefly rereads only undelivered exact-parent rows outside registry locks. A fully fenced batch parks without a model retry or continuation charge; genuinely register-only rows retain the bounded `registration-required` correction. The supervisor never replays the dispatch itself |
| autopilot routing | Codex exposes `autopilot-*` as native Skills/plugin entries and can select matching Skills from descriptions, but the adapter does not emulate Claude slash-command routing. `prompt-signal` emits the portable routing contract and Codex-native entrypoint surface; for spec-backed work, rely on spec-read/capability gates plus the relevant Skill or explicit dispatch wrapper |
| subagent delegation | Codex supports native subagent workflows, but they are explicit or main-dispatched. Run `adapters/codex/bin/preflight.sh subagent-info --check` to verify the `multi_agent` runtime feature and projected custom agents before claiming delegation parity. Use prompt-directed subagents or `preflight.sh dispatch`; do not treat UI/status state as an automatic delegation trigger |

Native Codex owns question timing. Codex 0.160 Default mode exposes only
`request_user_input_async`: the box is answerable only while the asking turn
runs (its 30 s countdown is cosmetic) and is dropped at turn end, so a late
answer arrives as an ordinary typed message. The blocking tool (feature
`default_mode_request_user_input`, under development) auto-resolves with empty
answers outside Plan mode and is therefore not enabled. Empty answers never
satisfy a human decision gate; the session restates an open decision question
at turn end and uses the next reply as its answer. The normal frame start
receipt keeps `human_wait.question_block` and the existing interview artifact;
empty/timeout/accepted responses return the same pending question without
saving answers or releasing approval. A plain resume waits for the answer
instead of opening another native box. The parent records the actual reply
with the existing answers template. Fleet question display uses
rollout/panel evidence.

The dispatch wrapper still validates the capability catalog, validates an
optional non-owner `worker_mode` through `mode-info`, and `_kernel/owner`
rejects a worker mode before prompt or registry writes. `--register` and
`--start` materialize the minimal typed worker prompt. Registry writes and
harvest rewrites are serialized with a `.lock` file. Interactive fallback is
reported before launch and never fails with a synthetic hook-trust error.
Registry writes and harvest rewrites are serialized with a `.lock` file; `_kernel/owner` rejects a worker mode before prompt or registry writes.
| material browser fetch | Tool-contract check: `adapters/codex/bin/preflight.sh browser-fetch --check <url>` verifies rendered browser access through the adapter-owned Playwright launcher before using `roles/units/material/browser-fetch.md`. Exit 69 means the local browser stack is unavailable |
| material data script | Tool-contract check: `adapters/codex/bin/preflight.sh data-script --check <script.py>` verifies generated Python analysis scripts through the adapter-owned launcher before using `roles/units/material/data-script.md` |
| material figure generation | Tool-contract checks: `adapters/codex/bin/preflight.sh figure-gen --check <script.py>` verifies generated matplotlib/seaborn scripts; report spectrograms additionally require `figure-gen --verify-report <manifest.json> <report.md>` for metadata, claim-evidence, scale, and hash-bound visual-review QA before using `roles/units/material/figure-gen.md` |
| material PDF extract | Tool-contract check: `adapters/codex/bin/preflight.sh pdf-extract --check <file.pdf>` verifies local PDF text extraction through the adapter-owned launcher before using `roles/units/material/pdf-extract.md`. Exit 69 means the local extractor is unavailable |
| material web image search | Tool-contract check: `adapters/codex/bin/preflight.sh web-image-search --check <query>` verifies that `CODEX_WEB_IMAGE_SEARCH_CMD` or `AGENT_WEB_IMAGE_SEARCH_CMD` provides a local image-search command before using `roles/units/material/web-image-search.md`. Exit 69 means no provider is configured |
| QA security review | Portable read-only persona: `roles/units/qa/security-review.md` is consumed with Codex file and git diff tools. Do not project or invoke Claude `/security-review` |
| QA verification runner | Tool-contract check: `adapters/codex/bin/preflight.sh verification-runner --check -- <command>` verifies explicit QA/test commands through the adapter-owned runner before using `roles/units/qa/test.md` |
| code-test capability | `capability-info code-test` reports `status=tool-contract`, `tool_contract=verification-runner`, `runtime_surface=adapter-owned-verification-runner`, and the `test_logs/` artifact contract before claiming verification support |
| research claim verify | Tool-contract check: `adapters/codex/bin/preflight.sh claim-verify --check <claim>` verifies that `CODEX_CLAIM_VERIFY_CMD` or `AGENT_CLAIM_VERIFY_CMD` provides an external verification command before using `roles/units/research/claim-verify.md`. Exit 69 means no provider is configured |
| design post-write verification | `core/HOOKS.md` defines the invariant; run `adapters/codex/bin/preflight.sh design <file>` after design HTML writes |
| design visual harness | Tool-contract check: `adapters/codex/bin/preflight.sh visual-harness <file.html>` runs the adapter-owned render/screenshot/console wrapper. Inspect the reported screenshot before claiming visual completion. Do not project Claude Design MCP files into Codex |
| design scaffold assets | Use `<agent-home>/scaffolds/` for reusable HTML scaffold assets. `codex_setting/scaffolds` points at the Codex-owned projection under `adapters/codex/scaffolds/`, not Claude runtime paths |
| memory injection | Codex `SessionStart` hook bridge keeps memory injection off by default because `SessionStart` can run on startup, resume, clear, and compact; set `CODEX_SESSION_MEMORY_INJECT=1` to emit `adapters/codex/bin/preflight.sh memory [cwd]` through `hookSpecificOutput.additionalContext`, or run it manually when needed |
| memory sync | Nothing runs at Codex `SessionEnd` (D-82; the `preflight.sh session-end` subcommand is gone) and there is no automatic distiller (D-78). `mem.py` exchanges in the background after a successful write (20-second batching window) and after a main-session read when the last receive is older than ten minutes; the remote is used only when `MEM_SYNC_REMOTE=1` (or the shared policy file) enables it, and with it off the exchange stops at the local dump commit. The adapter passes the user's `MEM_SYNC_REMOTE` and deprecated `MEM_DUMP_PUSH` environment unchanged and never forces remote exchange. A failed run leaves one line for the next `mem` command; `MEM_EXCHANGE_AUTO=0` turns the automatic exchange off. `SessionStart` with `source` `compact` or `clear` empties the session's candidate display history. Codex `Stop` never starts this lifecycle; its only side effect is clearing the exact Fleet interaction marker |
| memory candidate exposure and deeper retrieval | Codex `UserPromptSubmit` runs the fail-open capsule-only candidate bridge and adds at most six headline-and-ID candidates within 2,400 UTF-8 bytes. The bridge publishes a same-turn receipt; `PreToolUse` requires it before main-session material mutation. The model ignores unrelated candidates and reads relevant records in full. Use `preflight.sh recall <query> [cwd] [session-id]` for deeper search or `recall-gate` as the hook-failure recovery path. No prompt classifier or body injection is attached |
| oncall briefing injection | Codex `UserPromptSubmit` hook bridge runs `adapters/codex/bin/preflight.sh briefing [cwd]` and aggregates matching output into `hookSpecificOutput.additionalContext`; run it manually when hooks are unavailable |
| loop guidance | `adapters/codex/bin/preflight.sh loop-info <oncall|note|study|drill|runtime-watch>` reports whether a loop has a Codex manual contract, unsupported executable projection, or missing native implementation; `note` is application-owned and the harness exposes only the optional app-neutral `artifact-sink` port |
| capability mapping | `adapters/codex/bin/preflight.sh capability-info <capability>` reports Codex's native Skill realization and instruction-only or tool-contract status; an optional marketplace copy is informational only, and root Skill compatibility references report `compat_reference=not-projected` |
| autopilot-code pipeline | `capability-info autopilot-code` and `route autopilot-code` additionally report `stage_graph_contract=core/CONVENTIONS.md#pipeline-intensity-stage-graph-and-assurance`, `plan_policy=direct=no-plan;quick=registered-headless-dispatch-depth-1-one-shot-micro-plan+plan-check-lite;standard+=durable-plan`, the `standard+` `pipeline_contract=code-plan>code-execute>code-test>code-report`, optional `code-refine`, required plan artifacts, role mapping, and the dispatch fallback |
| model role/profile mapping | `adapters/codex/bin/preflight.sh role <portable-role|role-profile|pipeline-stage>` resolves behavioral roles and legacy native-agent profiles. Registered topology additionally carries a sealed execution `model_profile`; the headless wrapper selects the complete user `$CODEX_HOME/agent-config/models.conf` when valid and otherwise the complete shipped `adapters/codex/config/models.conf`, without conflating it with bootstrap mode or role |
| mode mapping | `adapters/codex/bin/preflight.sh mode-info <family/mode>` reports whether a mode is portable, tool-contract, or unsupported for Codex; tool-contract and unsupported adapter-coupled modes include machine-readable `tool_contract`, optional `tool_contract_check`, `runtime_surface`, and `fallback` fields |
| QA policy mapping | `adapters/codex/bin/preflight.sh qa-policy <level> [code|research|doc|general]` maps portable QA levels from `core/CONVENTIONS.md` to Codex assurance scope, selected-pass reviewer budgets, external-adversary requirements, max rounds, and inline fallback reporting. `stage_graph_selector=explicit-graph-or-intensity-default` means these budgets do not open stages or depth by themselves |
| memory store | `tools/memory/mem.py` plus `protocol_v2.py`, `sync_v2.py`, and `git_exchange_v2.py` are runtime-neutral. Each server keeps its SQLite/WAL/replica state local; an explicitly enabled remote sync uses immutable operations in a private dedicated exchange repository outside project/config trees and requires both an active old-writer fence and either a fresh store or a sealed seed epoch. The adapter never runs live migration/fence activation. `dump.jsonl` is compatibility output only |

## Tool Projection

`codex_setting/tools` intentionally points at `adapters/codex/tools/`, not the
full shared `tools/` directory. The adapter currently exposes only tools that
Codex wrappers use directly:

- `memory/mem.py` (Codex-owned launcher for the shared memory CLI)
- `memory/recall.sh` (Codex-owned launcher for recall)
- `material/browser-fetch.sh` (Codex-owned launcher for rendered web page extraction)
- `material/data-script.sh` (Codex-owned launcher for Python data-analysis scripts)
- `material/figure-gen.sh` (Codex-owned launcher for generated matplotlib figure scripts)
- `material/pdf-extract.sh` (Codex-owned launcher for local PDF text extraction)
- `material/web-image-search.sh` (Codex-owned launcher for configured image search providers)
- `qa/verification-runner.sh` (Codex-owned launcher for explicit verification commands)
- `research/claim-verify.sh` (Codex-owned launcher for configured external claim verification providers)
- `design/visual-harness.sh` (Codex-owned launcher for render/screenshot/console checks)

Harness development tools and Claude-coupled helper surfaces such as
`build-manifest.py` and `web-bundle` stay out of the Codex projection until
Codex has a documented runtime realization for them. The shared `design-mcp`
package is not projected wholesale; Codex exposes only the adapter-owned visual
harness launcher.

## Utility Projection

`codex_setting/utilities` intentionally points at
`adapters/codex/utilities/`, not the full shared `utilities/` directory. The
adapter currently exposes only utility files that Codex wrappers or docs use:

- `agent-home.sh` (Codex-owned wrapper; no Claude runtime-home fallback)
- `artifact-root.sh`
- `agent-worklog-state.sh`
- `harness-status.sh`
- `dispatch-route.sh` (read-only SD-23 candidate selection; adapter model probe owns exact ID proof)

Claude-specific helpers such as the shared `dispatch-liveness.sh` stay out of
the Codex projection. Codex exposes its adapter-owned liveness command through
`adapters/codex/bin/preflight.sh liveness [jobs.log]`, backed by
`~/.codex/sessions/**/*.jsonl` metadata and mtime.
Codex also exposes `adapters/codex/bin/preflight.sh harvest` for registry-only
status and selected `open` to `done` updates. It intentionally does not merge
branches or delete worktrees.

## Native Skill Projection

`adapters/codex/skills/` contains Codex-native Skill projections generated from
portable `capabilities/*.md` specs:

All core projections are generated and checked through one command:

```bash
python3 tools/generate.py --check
```

Expose them to Codex by activating the selected profile into
`$CODEX_HOME/skills/`. Do not expose the optional marketplace copy at the same
time because it duplicates skill metadata in Codex's initial context. Do not expose root
`skills/` or `adapters/claude/skills/` as Codex native skills.

## Native Plugin Projection

`adapters/codex/plugins/hearting-codex` is an optional distribution
artifact copied from the Codex-native Skill projection. It is deliberately
outside `tools/generate.py`, runtime activation, `doctor`, and core `verify`.

Expose the repo-local marketplace through `codex_setting/codex-plugin-marketplace`.
That projection is a dedicated marketplace root, not a link to the entire
Codex adapter:

```bash
codex plugin marketplace add "$AGENT_HOME/codex_setting/codex-plugin-marketplace"
codex plugin add hearting-codex@hearting
```

The plugin copies generated Codex Skill files into plugin-local `skills/` so
Codex discovers them as `hearting-codex:<capability>`. Do not build the
plugin from Claude Skill files.

## Native Agent Projection

`adapters/codex/agents/` contains Codex custom agent TOML projections generated
from portable role profiles in `roles/README.md`:

They are covered by `python3 tools/generate.py --check`.

Expose them to Codex by symlinking each generated `*.toml` file into
`$CODEX_HOME/agents/` for a user/global install, or into project
`.codex/agents/` for a project-scoped install, using
`codex_setting/codex-agents` as the projection source. The TOML files define
the required Codex custom agent fields (`name`, `description`, and
`developer_instructions`) plus Codex-native runtime config fields: `model`,
`model_reasoning_effort`, and `sandbox_mode`. Adapter model defaults are derived
from `adapters/codex/config/models.conf` (the sole source of concrete Codex model
IDs): the light tier for fast review/tool workers, the deep tier for fast
implementation and deep/demanding workers, the light tier for balanced
orchestration, and read-only sandboxing for QA, external-adversary, and memory-scout
agents. The generated instructions also encode role-specific runtime boundaries
such as QA read-only behavior, depth-one delegation, write preflight
requirements, and external-adversary independence. Mixed or variable role
profiles include `Codex role-map inputs` so the concrete role can be selected
by mode and QA policy instead of flattening the profile to one model role. Do
not expose `adapters/claude/agents/` as Codex-native agents.

parity caveat: Codex custom agents are real native subagents, but they are not
Claude Code Agent markdown/frontmatter. Runtime discovery, UI surfacing, child
approval behavior, config inheritance, and noninteractive/headless behavior
must be verified in Codex itself before claiming Claude Code parity. Treat
current GitHub issues and community examples as secondary evidence of runtime
gaps, not as the source of truth.

Current validation is structural plus install-path validation: the boundary
guard verifies generated TOML fields, `model_reasoning_effort` / `sandbox_mode`
runtime config fields, portable role references, role-map resolution,
role-specific runtime boundaries, and absence of non-Codex adapter paths. Codex
CLI 0.142.x exposes `codex debug prompt-input` for bootstrap/Skill/plugin
discovery, but it does not expose a `codex debug agent` listing surface. Add a
runtime discovery test when Codex exposes one.

## Native Mode Projection

`adapters/codex/modes/` contains Codex-owned mode realization guides generated
from `roles/units/`. These files are not copied from another runtime. They keep
the portable mode source visible while mapping each mode through
`adapters/codex/bin/preflight.sh mode-info <family/mode>`. Each generated guide
also embeds a sanitized projected portable mode contract so Codex sees the
actual procedure, with non-Codex runtime surfaces rewritten to Codex
preflight/tool-contract wording.

They are covered by `python3 tools/generate.py --check`.

`mode-info` reports `native_mode_path=adapters/codex/modes/<family>/<mode>.md`.
For tool-contract modes, run the reported `tool_contract_check` or report the
unavailable contract before claiming support. Design modes report
`realization=codex-native-mode-with-tool-contract` and require
`adapters/codex/bin/preflight.sh visual-harness <file.html>` before claiming
rendered visual verification.

## Command-Like Entries

Custom prompts are deprecated in Codex. Do not generate a `prompts/` projection
or copy Claude slash-command files into Codex. Reusable command-like capability
entrypoints are represented by Codex-native Skills and the installable
`hearting-codex` plugin.

## Native Hook Projection

Write-denying hook gates are retired; no write preflight or core-read marker is required.

Fleet reads structured rollout question evidence. Blocking `request_user_input`
calls remain pending until their matching output or turn boundary. Async
`request_user_input_async` calls retain only call/turn identity, time and
unanswered question indices: `accepted:true`, ordinary activity, and normal
turn completion do not answer them. A later composer prompt (a UserMessage
`item_completed` whose `client_id` is not a `delivery-` Hearting completion
delivery and whose content is not an exact reply envelope) clears the open async
questions, as the TUI does; a peer-steward prompt typed into the pane is
indistinguishable and clears them too. Exact native user reply envelopes with
`questionItemId=["request_user_input_async", call_id, index]` resolve each
question; a failed request or originating-turn interruption also clears it.
The async call/acceptance/reply shape was observed in local Codex traffic on
2026-10-01 and is regression-tested. Other clients and Code Mode wrappers
without this observable shape remain unverified; Fleet never parses executable
tool input or conversational prose to infer a wait. Questions and answers are
never retained in Fleet state. The visible badge says `question`, while JSON
preserves the portable `decision` kind. Client question styling stays native.

Expose it through `codex_setting/codex-hooks`, not through a plain `hooks/`
projection:

```bash
ln -sfn "$AGENT_HOME/codex_setting/codex-hooks/hooks.json" "$HOME/.codex/hooks.json"
```

The pre-write bridge accepts Codex hook stdin JSON across top-level and nested
tool payload shapes (`tool_name`/`tool_input`, `tool` + `input`, or
`toolUse.input`) and resolves `cwd` / `session_id` from top-level or nested
runtime payloads. It returns a `decision=block` hook result when the shared
guard fails. The read bridge is a marker path only, and the design bridge is a
post-write alert path only.
Neither bridge consumes Claude `settings.json` or Claude hook payloads. Codex's
local hook path does not cover every hosted/specialized tool, so those remain a
disclosed prompt/conformance fallback rather than total enforcement. There is
one targeted matcher only; no wildcard hook can seize unrelated tools. The
strict runtime check reads the
authoritative App Server `hooks/list` trust status instead of inferring trust
from a stale config key.
Use `adapters/codex/bin/preflight.sh doctor --runtime-strict` for the combined
projection, bridge, boundary, and current-hash trust gate.

## Runtime Home Projection

Target layout:

```text
$HOME/hearting/             # canonical neutral repo
$HOME/.codex/               # Codex runtime home
```

Codex runtime state such as `auth.json`, logs, SQLite state, sessions, model caches, and shell snapshots should stay in `$HOME/.codex`. The neutral harness should be referenced from Codex through explicit bootstrap instructions, symlinks, or wrapper configuration. At minimum, the Codex adapter should expose a stable pointer back to the neutral repo, for example:

```text
$HOME/.codex/hearting -> $HOME/hearting
```

Further Codex-specific files can be added under `adapters/codex/` and symlinked or generated into `$HOME/.codex` as the adapter matures.

## Model Role and Execution-Profile Mapping

The Codex adapter keeps behavioral role and execution budget separate. Role
resolution remains available through `preflight.sh role`; a compiled registered
route additionally seals one of these profiles from the single
`adapters/codex/config/models.conf` source:

| Model profile | Codex realization | Registered topology use |
|---|---|---|
| `deep` | configured deep tier / `xhigh` | standard+ ownership, convergence, and highest-risk legs |
| `balanced-deep` | configured deep tier / `medium` | quick one-shot conduction and subordinate deep-model judgment at a lower coordination budget |
| `balanced` | configured light tier / `high` | long multi-step execution after the decision is settled |
| `light` | configured light tier / `medium` | routine implementation, verification, reporting, and breadth legs |
| `mini` | configured mini tier / `low` | lifecycle and micro-semantic helpers only; substantive dispatch-depth-1/2 work is rejected |
| `top` | configured top tier / `xhigh` | exception above deep: the main-session-only model (`CFG_MAIN_SESSION_ONLY_MODELS`, enforced by this wrapper since 2026-09-10; a user copy without the key stays selected whole-file and unrestricted, receipt `main_session_only_policy=absent`), reachable only from a route that seals the explicit `top` profile on a dispatch-depth-1 owner (refused without it); no cascade in or out, no `--model` override, and `--inherit-model-settings` is refused while the key names a model (the same rule on every adapter) |

Non-route role compatibility overrides remain explicit and config-derived:

```text
AGENT_MODEL_FAST
AGENT_MODEL_DEEP
AGENT_MODEL_EXTERNAL
AGENT_MODEL_ORCHESTRATOR
AGENT_REASONING_FAST
AGENT_REASONING_DEEP
AGENT_REASONING_EXTERNAL
AGENT_REASONING_ORCHESTRATOR
AGENT_EXTERNAL_CMD
```

Fast roles, including `fast implementer`, resolve to the light tier by default;
deep roles, including `deep orchestrator`, resolve to the deep tier. The balanced
`orchestrator` remains light/medium. `external adversary` is unavailable unless
`AGENT_MODEL_EXTERNAL` or `AGENT_EXTERNAL_CMD` supplies genuine independent
execution. Environment compatibility knobs remain accepted for non-route role
selection, but they cannot replace a route-sealed profile. Native custom-agent
TOML profiles retain their explicit static settings; registered topology uses
the route profile passed by the wrapper.

## Compatibility

Codex should create new project artifacts only under the root returned by `utilities/artifact-root.sh`. In a linked task worktree this is the primary checkout's `.agent_reports/`, not the tracked local snapshot. The dispatch wrapper injects `AGENT_ARTIFACT_ROOT` and grants exactly that path with Codex `--add-dir`; legacy `.claude_reports/` remains a canonical-root fallback.

Codex should resolve harness-home paths through `AGENT_HOME` or the Codex-owned `utilities/agent-home.sh`. Some shared legacy tools still accept `CLAUDE_HOME` as a migration alias, but Codex-owned wrappers should not use it as their runtime-home fallback.

Claude Code-specific files remain valid as implementation references, not as Codex bootstrap files:

- `CLAUDE.md` contains Claude Code routing and response rules.
- `adapters/claude/settings.json` registers Claude Code hooks and permissions.
- `adapters/claude/commands/` defines Claude Code slash commands.
- `skills/*/SKILL.md` is still Claude Skill format; start from `capabilities/README.md` for portable meaning.
- `adapters/claude/statusline.sh` targets Claude Code's statusline contract.

When porting a behavior, copy the underlying invariant from `CORE.md`, `WORKFLOW.md`, `CONVENTIONS.md`, or `OPERATIONS.md`; then map it to Codex's tool, approval, and session model.
# Material-route boundary

Hook write gates are retired except the two kept core write gates and the route presence gate (`core/HOOKS.md`). Spec-read observations, design checks, memory and session signals remain available.

Resource-runner startup requires a sealed route, exact detached resource node,
and smoke attestation before launch state or a child process exists. Installing
the projection and satisfying current-hash hook trust are separate operator
actions; this source tree alone does not activate runtime enforcement.

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

### Native parent identity

The caller's CODEX_THREAD_ID is sealed as the parent. Sibling App Server threads
never replace it. Existing managed panels can send via their native upstream
queue endpoint until they exit; new sessions use Codex directly. The sidecar
never resumes/subscribes to a thread or rewrites its settings. Refused delivery
remains a durable pending obligation.

Session and prompt bridges retain `hookSpecificOutput.additionalContext` for optional memory and lifecycle context.
