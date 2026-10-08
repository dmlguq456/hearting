# Codex Adaptation

This adapter is not a Claude Code surface clone. It defines the required mapping
so Codex can reproduce the portable harness invariants through Codex-native
surfaces, tool contracts, and explicit fallbacks without copying Claude-specific
assumptions into the common core.

## Canonical artifact and cleanup boundary (2026-07-14)

- **Runtime support:** the official Codex manual documents app-managed
  worktrees and their cleanup lifecycle, while the local `codex exec` CLI
  exposes `--add-dir` for an additional writable directory. That app lifecycle
  does not own harness-created sibling worktrees.
- **Adapter realization:** dispatch resolves the primary checkout's artifact
  root, injects `AGENT_ARTIFACT_ROOT`, and adds exactly that path with
  `--add-dir`. Shared guards reject worker-local artifact writes.
- **Parity gap/fallback:** Stop/SessionEnd cannot prove merge or push and never
  deletes worktrees. Main uses `preflight.sh worktree-cleanup` after integrated
  verification and push.

## Worker bootstrap realization (2026-07-16)

The headless wrapper now renders the portable minimal kernel plus one worker
type and wraps custom prompts as assignments. It no longer asks the worker to
read the full Codex adapter bootstrap or returns changed-file/test prose to
main; durable detail is artifact-only and the terminal handoff is three lines.
All new headless types, including session-tidy support, use the shared profile
projector's role home. Hearting's global main `AGENTS.md` is replaced with the
small attach template. This is controlled global input, distinct from native
project instructions, which remain. User credentials/config/hooks stay linked;
launch overrides disable apps, MCP servers, custom agents, native collaboration
and enabled plugins, and disable discovered system skills by documented path.
The assigned contract remains in the prompt. Native or managed input outside
these controls must be reported from actual runtime evidence.

Hook trust is path-keyed in Codex. The invocation maps existing user decisions
to the relocated copies of the same source definitions, including disabled
states; it reads those decisions again when a resumed command is built. Native
current-hash validation still rejects changed or unapproved definitions. No
user trust record is written and no hook-trust bypass is used. Validate this
with App Server `hooks/list`, rather than assuming a linked file is active.

Official surfaces: [instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md),
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

## Runtime router diagnostic boundary (2026-08-10)

`codex_core::tools::router` diagnostics belong to the Codex App Server/runtime,
not to Hearting dispatch. Correlate one with the typed result of the exact tool
call before assigning a harness failure. In the reported 2026-08-10 incident,
the preceding and following tool calls both completed successfully and no
Hearting failure receipt matched the diagnostic, so it is not evidence of
managed-marker loss or a failed dispatch. When no typed result exists, retry
only the exact idempotent tool call once; otherwise trust its typed completion.
Do not infer an unmanaged parent, scan the process tree as launch authority, or
switch dispatch surfaces from this uncorrelated runtime message.

## Design Principle

Codex adaptation targets harness parity on Codex, not Claude surface parity.
Start from the portable invariant in `core/`, then map it onto Codex-native
features where they exist. Claude files are implementation references, not files
to port wholesale.

Use Codex-native surfaces first for model/session/context/status, approvals,
sandboxing, skills/plugins, and built-in slash commands. Add adapter wrappers
only for harness-specific signals that Codex does not provide directly.

## External Reference Lessons

GSD Core (`https://github.com/open-gsd/gsd-core`) is a useful cross-runtime
installer reference pattern, not a source to copy. The relevant lesson is the
seam:

- keep the workflow/capability meaning canonical;
- describe each runtime's artifact layout and config surface as data;
- convert canonical files into runtime-native artifacts;
- prove the runtime discovers those artifacts;
- fail closed when a runtime feature is undocumented or missing.

For this adapter, that means Codex support should not be measured by whether
Claude files are visible under `codex_setting/`. It should be measured by
whether Codex has a native entrypoint or an explicit wrapper for the portable
invariant.

## Native Codex Surfaces

| Codex runtime surface | Adapter source | Projection |
|---|---|---|
| Session bootstrap | `adapters/codex/AGENTS.md` | `codex_setting/AGENTS.md` |
| Adapter guide | `adapters/codex/README.md` | `codex_setting/README.md` |
| Common contract | `core/` | `codex_setting/core` |
| Capability catalog | `capabilities/` | `codex_setting/capabilities` |
| Role catalog | `roles/` | `codex_setting/roles` |
| Preflight wrappers | `adapters/codex/bin/` | `codex_setting/bin` |
| Skills | `adapters/codex/skills/<name>/SKILL.md` generated from `capabilities/` | `codex_setting/codex-skills` |
| Custom agents | `adapters/codex/agents/<role>.toml` generated from `roles/README.md` | `codex_setting/codex-agents` |
| Mode guides | `adapters/codex/modes/*/*.md` generated from `roles/units/` with Codex mode-info contracts | `codex_setting/codex-modes` |
| Plugin marketplace | `adapters/codex/plugin-marketplace/.agents/plugins/marketplace.json` plus `adapters/codex/plugin-marketplace/plugins/hearting-codex` | `codex_setting/codex-plugin-marketplace` |
| Hook bridge | `adapters/codex/hooks/hooks.json`, `adapters/codex/hooks/run-hook.sh`, `adapters/codex/hooks/sessionstart-lifecycle.py`, `adapters/codex/hooks/sessionend-lifecycle.py`, `adapters/codex/hooks/stop-lifecycle.py`, `adapters/codex/hooks/userprompt-lifecycle.py`, `adapters/codex/hooks/permissionrequest-lifecycle.py`, `adapters/codex/hooks/posttooluse-interaction-clear.py`, `adapters/codex/hooks/posttooluse-read-marker.py`, `adapters/codex/hooks/posttooluse-design-check.py` | `codex_setting/codex-hooks` |
| Permission/sandbox contract | `adapters/codex/bin/preflight.sh permissions` | `codex_setting/bin/preflight.sh permissions` |
| MCP contract | `adapters/codex/bin/preflight.sh mcp` | `codex_setting/bin/preflight.sh mcp` |
| Design scaffold assets | `adapters/codex/scaffolds/` Codex-owned projection of shared scaffold HTML assets | `codex_setting/scaffolds` |
| Shared helper tools | selected `tools/`, selected `utilities/` | `codex_setting/tools`, `codex_setting/utilities` |
| Selected tools | `adapters/codex/tools/` adapter launchers plus selected portable tool projections | `codex_setting/tools` |
| Selected utilities | `adapters/codex/utilities/` adapter wrappers plus selected portable utility projections | `codex_setting/utilities` |

Permission/sandbox posture is now version-controlled for Codex the way it is for
Claude. Claude's auto-approve posture is captured in
`adapters/claude/settings.json` (`permissions.allow[]` plus
`defaultMode: "auto"`), so a fresh Claude Code install reproduces it from the
repo. The Codex equivalent — `approvals_reviewer`, per-project `trust_level`,
and the baseline `approval_policy`/`sandbox_mode` stance — is captured in the
adapter-owned fragment `adapters/codex/config/approval-sandbox.toml` (projected as
`codex_setting/codex-config/approval-sandbox.toml`, reported by
`preflight.sh permissions` as `config_fragment=…`). It holds the reproducible
posture only — no secrets and no machine-specific absolute project paths (the
`trust_level` project block is a template). The adapter never auto-applies it;
merge the relevant lines into `$CODEX_HOME/config.toml` on the target machine.
(codex-adapter-parity audit P-15: gap closed.)

Harness self-identity is the one fragment runtime activation does apply:
`adapters/codex/config/harness-identity.toml` (`[shell_environment_policy.filters]`) makes Codex
tool commands drop the inherited `AGENT_DISPATCH_CALLER_HARNESS` and Claude/OpenCode session
IDs, so the thread's own `CODEX_THREAD_ID` identifies it even when a shared app-server
daemon was started from another harness's shell. Nothing is exported that names a harness. Activation merges it into `$CODEX_HOME/config.toml` as one delimited managed block
and reports a policy it cannot merge safely as `config-conflict:`; a new Codex thread picks
it up, no daemon restart is needed.

Interactive Codex uses its native launcher and user-owned permission settings.
Hearting no longer inserts a managed gateway or changes interactive permission
posture. Registered headless work keeps its separately checked permission and
sandbox contract (`core/OPERATIONS.md` §5.10).

## Native Skill And Plugin Surface

Current Codex support includes generated native Skill projections:
`adapters/codex/skills/<name>/SKILL.md` is generated from
`capabilities/<name>.md` by `adapters/codex/bin/sync-native-skills.py` and
projected as `codex_setting/codex-skills`. Runtime discovery should use either
per-skill native symlinks or the adapter-owned Codex plugin by default, not both;
`install-runtime-projection.sh --skills-mode both` is reserved for compatibility
or debugging because it duplicates skill metadata in Codex's initial context.

The same generated skills are also packaged into the adapter-owned Codex plugin
`adapters/codex/plugins/hearting-codex`, with repo-local marketplace
metadata projected through `adapters/codex/plugin-marketplace/`. This makes
the harness discoverable through Codex's native plugin installer without
exposing Claude Skill files.

Codex custom prompts are deprecated. Command-like harness entries are therefore
realized through native Skills and the installable plugin, not through
`prompts/` files or Claude slash-command projections.

Before adding or changing Codex-native skills or plugins:

1. Use `capabilities/<name>.md` and `roles/` as source, not
   `skills/<name>/SKILL.md` or `adapters/claude/skills/`.
2. Generate or maintain concrete adapter-owned output under an explicit Codex
   adapter path, for example `adapters/codex/skills/<name>/SKILL.md`.
3. Keep Codex frontmatter, invocation syntax, sandbox/approval assumptions, and
   plugin metadata in the Codex adapter.
4. Add a guard that proves every generated Codex skill maps to a portable
   capability and that no Claude-native Skill file is exposed as Codex-native.
5. Verify discoverability using the Codex runtime contract, not byte parity with
   Claude files.

depth caveat: byte parity is not depth parity. Codex-native `SKILL.md`
projections stay at capability-summary depth, while the largest Claude Skills
reach roughly 59KB of step-level procedural detail — an order of magnitude
(roughly 8x) more than the generated Codex skill. This step-level depth gap is
a known parity limitation distinct from the byte-parity disclaimer above;
re-running `sync-native-skills.py` does not close it.

Design capabilities are a tool-contract exception: Codex has native Skill
guidance for them, but must run the adapter visual harness before claiming full
support. `capability-info` reports `status=tool-contract` for those capability
entries. Design mode fragments now have Codex-owned guides under
`adapters/codex/modes/design/`; `mode-info` reports the guide path and the
`visual-harness` contract, and Codex must report unavailable if the harness
cannot run. All generated mode guides embed sanitized projected portable mode
contracts so Codex sees the actual procedure while non-Codex runtime surfaces
are rewritten to Codex preflight/tool-contract wording.

`roles/units/material/browser-fetch.md` has a Codex-owned executable
tool-contract surface:
`adapters/codex/bin/preflight.sh browser-fetch --check <url>` verifies rendered
browser access through `adapters/codex/tools/material/` and reports exit 69
when the local Playwright browser stack is unavailable.

`roles/units/material/data-script.md` is the first material mode with a
Codex-owned executable tool-contract surface:
`adapters/codex/bin/preflight.sh data-script --check <script.py>` verifies
generated Python analysis scripts through `adapters/codex/tools/material/`.

`roles/units/material/figure-gen.md` has a Codex-owned executable tool-contract
surface:
`adapters/codex/bin/preflight.sh figure-gen --check <script.py>` verifies
generated matplotlib/seaborn figure scripts through
`adapters/codex/tools/material/`. Report spectrograms additionally run
`figure-gen --verify-report <manifest.json> <report.md>` and fail closed on
metadata, scale, claim-evidence, or hash-bound visual-review drift.

`roles/units/material/pdf-extract.md` has a Codex-owned executable
tool-contract surface:
`adapters/codex/bin/preflight.sh pdf-extract --check <file.pdf>` verifies
local PDF text extraction through `adapters/codex/tools/material/` and reports
exit 69 when the local extractor is unavailable.

`roles/units/material/web-image-search.md` has a Codex-owned executable
tool-contract surface:
`adapters/codex/bin/preflight.sh web-image-search --check <query>` verifies a
configured image-search provider command through `adapters/codex/tools/material/`
and reports exit 69 when no provider is configured.

`roles/units/qa/security-review.md` is portable read-only mode guidance for
Codex. It is consumed with Codex file and git diff tools and does not project
or invoke Claude's `/security-review` slash command.

`roles/units/research/claim-verify.md` has a Codex-owned executable
tool-contract surface:
`adapters/codex/bin/preflight.sh claim-verify --check <claim>` verifies a
configured external verification provider command through
`adapters/codex/tools/research/` and reports exit 69 when no provider is
configured.

`roles/units/qa/test.md` has a Codex-owned executable tool-contract surface:
`adapters/codex/bin/preflight.sh verification-runner --check -- <command>`
checks explicit verification commands and the same wrapper can execute them
with a bounded timeout. `capability-info code-test` exposes the same
`verification-runner` contract plus the `test_logs/` artifact contract so the
capability and mode surfaces agree.

The boundary guard checks that generated Codex skills and the generated Codex
plugin remain in sync, and that neither surface is built from Claude Skill
files.

## Native Custom Agent Surface

Codex supports custom subagents through TOML files under `$CODEX_HOME/agents/`
or project `.codex/agents/`. This adapter materializes those role profiles as
`adapters/codex/agents/<role>.toml`, generated from `roles/README.md` by
`adapters/codex/bin/sync-native-agents.py` and projected as
`codex_setting/codex-agents`.

Each file defines Codex's required custom agent fields (`name`, `description`,
and `developer_instructions`) and the Codex-native runtime config fields
`model`, `model_reasoning_effort`, and `sandbox_mode`. Adapter defaults follow
the current Codex documentation shape: the fast/deep model tuple defined in
**Model Mapping** below, and read-only sandboxing for QA, external-adversary,
and memory-scout agents. The generated instructions also
encode role-specific runtime boundaries such as QA read-only behavior,
depth-one delegation, write preflight requirements, and external-adversary
independence. Mixed or variable role profiles include `Codex role-map inputs`
so the concrete role can be selected by mode and QA policy instead of
flattening the profile to one model role. Do not project Claude Agent files or
OpenCode Agent files into Codex.

parity caveat: Codex custom agents can carry model/reasoning/sandbox settings,
but they are not Claude Code Agent frontmatter. Runtime discovery, UI surfacing,
child approval behavior, config inheritance, and noninteractive/headless
behavior must be verified in Codex itself before claiming Claude Code parity.
Recent Codex issue reports show that model/reasoning settings can be runtime-
or surface-dependent, so this adapter treats TOML generation as the source
projection and keeps runtime validation separate.

permission-model caveat: Claude's per-agent `tools:` frontmatter allowlist
(for example `editorial-team` and `plan-team` both carry no `Bash` and no
network tools) has no Codex custom-agent-schema equivalent — Codex custom
agent TOML exposes no per-agent `tools` field, only `model`,
`model_reasoning_effort`, and `sandbox_mode`. The closest Codex approximation
is `sandbox_mode` plus `mcp_servers`, which cannot express a fine-grained tool
allowlist. Because `editorial-team.toml` and `plan-team.toml` both set
`sandbox_mode = "workspace-write"`, these two roles are strictly more
permissive under Codex than under Claude.

write-access caveat: Claude `qa-team` carries `Write` in its tool allowlist
and creates its durable review-log directly. Codex's `qa-team.toml` sets
`sandbox_mode = "read-only"` and cannot write at all, so under this adapter
the review-log for a Codex QA pass must be ghostwritten by the
orchestrator/dispatch harness on the QA agent's behalf, not written by the QA
agent itself. This is part of the Codex QA agent contract, not an oversight.

See Model Mapping below for the corresponding model-tier asymmetry across
these same custom agents.

Validation is currently structural plus install-path validation. The boundary
guard verifies generated TOML fields, `model_reasoning_effort` / `sandbox_mode`
runtime config fields, portable role references, role-map resolution,
role-specific runtime boundaries, and absence of non-Codex adapter paths. Codex
CLI 0.142.x exposes `codex debug prompt-input` for bootstrap/Skill/plugin
discovery, but it does not expose a `codex debug agent` listing surface; add
runtime discovery coverage when Codex exposes one.

## Native Hook Surface

Write-denying hook gates are retired; no write preflight or core-read marker is required.

Do not project Claude `hooks/` or `settings.json` into Codex. Use
`codex_setting/codex-hooks` as the install source, and keep explicit
`preflight.sh` calls as fallback where Codex hooks are disabled or untrusted.
`adapters/codex/bin/check-runtime-projection.sh` reports `check=hook-trust:ok`
or `check=hook-trust:review-needed`; run `/hooks` in Codex after hook definition
changes. The non-strict projection path skips that runtime probe so ordinary
headless children do not pay an App Server startup cost. The strict checker
queries authoritative App Server `hooks/list` state and requires each projected
definition's current hash, enabled state, and discovered source set to match.
Use `adapters/codex/bin/preflight.sh runtime-projection
--require-hook-trust` or `adapters/codex/bin/preflight.sh doctor --runtime-strict`
when hook trust must fail runtime checks.
The lifecycle hooks provide informational context. The design hook is a console-check alert path, not a
full render/screenshot visual harness.

Codex CLI 0.142.x exposes `codex debug prompt-input`, but not a hook listing or
hook firing debug surface. Current tests validate `hooks.json` structure and
execute the concrete bridge scripts with synthetic Codex hook payloads,
including top-level and nested tool input, `cwd`, and session variants; add a
runtime hook discovery test when Codex exposes a hook debug surface.

## Explicit Non-Support

Codex must not consume these Claude-native files as native configuration:

| Claude-native surface | Codex status |
|---|---|
| `adapters/claude/settings.json` | Not consumable; Codex needs wrapper/preflight equivalents |
| `adapters/claude/commands/` | Not consumable; command-like harness entries use Codex-native Skills |
| `skills/*/SKILL.md` | Compatibility reference only; Codex should start from `capabilities/README.md` |
| `adapters/claude/statusline.sh` | Not consumable; input schema is Claude statusline JSON |
| `adapters/claude/CLAUDE.md` | Reference only; not bootstrap |
| `adapters/claude/agents/*.md` | Reference only; Codex custom agents are generated from `roles/README.md` |
| `roles/units/*/*` | Portable source fragments; Codex consumes generated `adapters/codex/modes/*/*.md` guides plus `mode-info` metadata |

## Status Surface Boundary

Codex has its own `/statusline` configuration for the TUI footer. Do not replace
it with `adapters/claude/statusline.sh`, and do not duplicate Codex-native footer
items such as model, context, token/usage/limits, git baseline, session, or
Codex fast-mode state.

Codex UI customization is therefore a partial native parity surface, not a
Claude statusline clone. `/statusline` and `/title` configure Codex-owned
built-in item IDs; the adapter reports this boundary through
`adapters/codex/bin/preflight.sh ui-info`. Harness-specific state remains in
`preflight.sh status` output until Codex exposes an arbitrary dynamic footer
provider; Codex hooks themselves run silently with no `statusMessage` labels,
matching Claude Code's quiet hooks.

Harness-specific status signals still need Codex-native realization:

| Harness signal | Codex direction |
|---|---|
| routing-contract signal | `preflight.sh prompt-signal` (worker-startup/manual subcommand, not a per-turn injection) carries the full routing contract plus git dirty/worktree/dead-branch risk fields from `preflight.sh status`; explicit preflight remains fallback when hooks are unavailable |
| artifact/notes/git-risk snapshot | explicit `preflight.sh status`; includes tracked-dirty vs untracked counts and sibling worktree counts; keep Codex `/statusline` for native model/context/token/session fields |
| UI boundary report | explicit `preflight.sh ui-info`; reports built-in footer/title support, unsupported arbitrary live statusline scripts, Skill/plugin autopilot entrypoints, and explicit/main-dispatched subagent behavior |
| subagent delegation | explicit `preflight.sh subagent-info --check`; verifies the Codex `multi_agent` runtime feature and projected custom agents before claiming native subagent delegation parity |
| artifact root detection | shared `utilities/artifact-root.sh` helper |
| headless/autopilot/background jobs | `preflight.sh headless` / `dispatch` / `liveness` / `harvest` provide the tool-contract path; `preflight.sh status` surfaces in-flight jobs as `headless_open_jobs` / `headless_open_slugs` from the dispatch registry. A Codex-native graphical display remains optional polish |
| sibling `-wt/<slug>` dispatch detection | preserve the worktree naming invariant; choose a Codex-native display surface later |
| pipeline stage nudges | preflight/AGENTS instructions first; UI only when Codex exposes a suitable surface |
| oncall/note/study/drill/runtime-watch loop nudges | `preflight.sh briefing` plus `preflight.sh loop-info <loop>` for loop-specific support/fallback status |
| merge/rebase/merged-branch risk | `preflight.sh status` reports git operation, branch and worktree risks without blocking writes. |
| cycle checkpoint at turn end | `adapters/codex/hooks/stop-lifecycle.py` calls `artifact_checkpoint_trigger.launch_for_session("codex", <session id>)` for main and worker alike: a worker names its cycle in `AGENT_ARTIFACT_*`, an interactive session is found through its route-chain ledger. The detached `artifact_producer.py checkpoint --trigger turn-end` child refreshes an open cycle's interim manifest and, bounded and lock-free, observes closed cycles for changed files (§45 D-124). `AGENT_ARTIFACT_CHECKPOINT=off` starts nothing. When native Stop is not enabled or trusted, the checked fallbacks are the existing stage-complete and supervisor-poll triggers, `begin` (a named parent or a resumed cycle) and the explicit `artifact_producer.py checkpoint`; no trust setting or parity gate is added |
| fleet (multi-agent) observability | Fleet is a pure reader of registry and neutral sidecars. Each registered dispatch wrapper attaches an exact-attempt summary supervisor before releasing the worker launch fence; the supervisor owns early, debounced, and final updates even when Fleet is closed, and `dispatch-reconcile --apply` repairs a missing owner only for an exact live attempt. Interactive Codex `UserPromptSubmit`, Stop, and SessionEnd hooks trigger the same shared producer independently of Fleet. Interaction waits remain separate: `PermissionRequest` publishes approval wait and native `PostToolUse` plus turn/session boundaries release it without changing approval ownership. |

observability caveat: Codex keeps native `/statusline` ownership of model,
context, limits, and footer monitoring. Fleet does not own summary generation or
interaction lifecycle; it only renders stored evidence. The async question
call/acceptance/exact-reply rollout shape was observed locally on 2026-10-01.
That does not establish question observability for every client or Code Mode
wrapper. A later composer prompt clears the Fleet async wait and `delivery-`
completion deliveries do not (observed 2026-10-02). Unobserved shapes remain
unverified, with an explicit native question notice as the conversational
fallback; the client owns question styling.

## Required Codex Mappings

| Portable invariant | Codex adaptation requirement |
|---|---|
| design post-write verification | Run `adapters/codex/bin/preflight.sh design <file>` after design HTML writes |
| route presence gate | `hooks.json` `PreToolUse` on `Write|Edit|MultiEdit|apply_patch|functions.apply_patch|Bash|Shell|functions.exec_command` runs `adapters/codex/hooks/route-presence-gate.py` (through `run-hook.sh`), which execs `utilities/route_presence_gate.py --codex`; a refusal is `{"decision":"block","reason":…}`. The payload `session_id` is the thread id the route-chain writer records from `CODEX_THREAD_ID`; `exec_command` is judged in its `workdir`. Like every `hooks.json` change, the new entry needs Codex's current-hash hook trust before it runs (`hook-trust-status.py`); until then the gate is simply absent, never blocking |
| routing-contract signal | `adapters/codex/bin/preflight.sh prompt-signal [cwd] [session-id]` is the worker-startup/manual subcommand carrying the full routing contract; run it manually when no automatic hook is attached |
| token/context pressure | `preflight.sh token-budget [cwd] [session-id] [kv|json|hook]` reads an exact Codex rollout session and keeps active context, exact directive bytes, and cumulative raw counters separate. `kv`/`json` are read-only L2 accounting diagnostics. Unknown/degraded signals fail open. `hook` remains transition-only and byte-identical; its parent lifecycle is the single exactly-once accounting authority for success/timeout/error and writes only a bounded content-free sha256-session aggregate under XDG state. `utilities/token-budget-experiment.py` is an explicit isolated `offline-forecast-v1` replay/evaluator: production hooks/preflight do not import or activate it, its maximum verdict is `eligible_for_user_review`, adoption stays `pending_user_decision`, and it never writes config. Native rollout-budget ownership requires `AGENT_TOKEN_BUDGET_NATIVE_VALIDATED=1` only after feature + no-side-effect config probes pass; local Codex 0.144.3 reports the feature under development and disabled, so exact-session rollout observation remains the fallback. The adapter never writes `$CODEX_HOME/config.toml` |
| memory inject | Run `adapters/codex/bin/preflight.sh memory [cwd]` for plain-text memory injection; Codex SessionStart hook emission is opt-in via `CODEX_SESSION_MEMORY_INJECT=1` |
| memory sync | No Codex hook runs memory at session end (D-82; `preflight.sh session-end` is removed). `mem.py` exchanges in the background after writes and stale main-session reads against the local per-server SQLite source of serving truth. `sessionstart-lifecycle.py` calls `mem.py _seen-reset --session-id <id>` when the payload `source` is `compact` or `clear` (candidate display history only). Remote operation exchange is canonical opt-in through `MEM_SYNC_REMOTE=1`; `MEM_DUMP_PUSH=1` is a warned deprecated alias only when the canonical flag is unset and never pushes the compatibility dump. The runtime passes `MEM_SYNC_DIR`, `MEM_SYNC_REMOTE_URL`, and `MEM_SYNC_REF` through to the portable implementation, which requires a private dedicated exchange path outside project/config trees, an active old-writer fence, and either a fresh store or sealed seed epoch. The adapter performs no live migration/fence activation. Exit 1/2 remains visible after bounded curation |
| memory candidate exposure / recall | Every eligible main `UserPromptSubmit` invokes `mem candidates` through the portable bridge using prompt, cwd, session, and native turn/message ID when available. Only active current-project/global capsule headlines and IDs are exposed (maximum six / 2,400 UTF-8 bytes), with no body read, touch, or semantic classifier. `PreToolUse` requires its same-turn receipt for main-session material mutation. The model reads a relevant record in full; `preflight.sh recall <query> [cwd] [session-id]` remains the deeper-search path and explicit `recall-gate` recovers a failed or unavailable hook |
| local evidence exposure | Codex `SessionStart` runs the portable `hooks/local-evidence-inject.sh` presence probe for the session cwd: research/documents/analysis bucket counts plus at most nine newest entry paths from the canonical artifact root, round-robined across buckets and deduplicated per artifact (2,400-UTF-8-byte bound, no body reads, no prompt classifier, silent when empty, worker-exempt, fail-open). It moved off `UserPromptSubmit` in both directions: the block never changes between prompts, and `userprompt-lifecycle.py` fenced it at a 3-second subprocess timeout that a real store exceeded on every prompt, so Codex discarded the context silently rather than injecting it. `preflight.sh local-evidence [cwd]` is the manual path |
| oncall briefing | Run `adapters/codex/bin/preflight.sh briefing [cwd]` before prompt handling on the dedicated agent desk |
| loop guidance | Run `adapters/codex/bin/preflight.sh loop-info <oncall|note|study|drill|runtime-watch>` before following loop guides; Codex reports manual contracts, missing implementations, and drill auto-run restrictions without executing loop scripts. The `note` loop and note semantics are application-owned; the harness exposes only the optional app-neutral `artifact-sink` port |
| worklog state signal | Run `adapters/codex/bin/preflight.sh worklog [cwd]` to inspect configured `<agent-notes-root>` / `<worklog-board-app>` paths read-only before Codex updates notes or diagnoses board state |
| role profiles | Read `roles/README.md`, then run `adapters/codex/bin/preflight.sh role <portable-role|role-profile|pipeline-stage>` for behavioral-role or native-agent profile resolution. Registered routes separately seal `model_profile=deep|balanced-deep|light|mini`, resolved from the adapter config |
| permission mapping | Run `adapters/codex/bin/preflight.sh permissions` to inspect the Codex approval/sandbox contract and confirm Claude `allowedTools` is unsupported |
| MCP mapping | Run `adapters/codex/bin/preflight.sh mcp --check` to inspect Codex's native MCP CLI/config surface; do not copy Claude `settings.json` MCP registrations or project `tools/design-mcp` wholesale |
| dispatch-owner selection | An ordinary dispatch-depth-1 owner uses `dispatch-owner [--adapter <harness>] --dry-run|--register|--start`, a separate mapping row from `headless dispatch` below. It delegates to portable `utilities/dispatch-owner.py`, which prefers the user-local routing policy and runs explicit target → hard eligibility → sealed affinity → profile quality band → fresh headroom → recent-attempt tie-break. Capacity reorders peers or crosses a declared relief threshold but never silently makes OpenCode a deep quality peer. The selector execs only the chosen `adapters/<selected>/bin/dispatch-headless.py`, preserves actual caller runtime separately from selected owner adapter, and forbids completion-policy or unmanaged-poll flags |
| headless dispatch | Run `preflight.sh headless --check <worktree>` before launch; it verifies native Skills, native Agents, and native Modes. Use `dispatch --dry-run|--register|--start` for registered work. Standard+ dispatch-depth-1 owners use `--completion-delivery auto`: a checked App Server probe selects an ephemeral same-thread supervisor, forced `supervised` fails before registration when unavailable, and explicit/unavailable fallback is reported as `poll-fallback`. A registered quick/solo dispatch-depth-1 owner uses the same App Server supervisor when the probe reports support; otherwise it keeps one-shot `codex exec` without refusal and without input state (`owner-input-unsupported`). Owner corrections are accepted from registration and steer the active turn; one sent after the owner ended BLOCKED continues the route through a replacement owner (shared `dispatch_replacement`); dispatch-depth-2 and non-owner workers stay one-shot `codex exec`. A direct registered dispatch-depth-1 start binds `parent_completion_delivery` to the actual parent runtime, not the child: native Codex → `codex-native-queue`, Claude → `claude-parent-runtime`; a gateway-free Codex parent is admitted with its calling CODEX_THREAD_ID. A low-level operator may explicitly authorize finite recovery with `--allow-unmanaged-parent-poll`, but `dispatch-owner` and model routes cannot select it. The path never creates new Stop state or requires hook trust. The wrapper validates the scalar `capability_mode`, optional non-owner `worker_mode`, behavioral `model_role`, and execution `model_profile` as separate axes; `_kernel/owner` rejects a worker mode. Profiles resolve through `config/models.conf`, caller model/reasoning replacement and substantive registered `mini` are denied, and rows expose resolved tier/granularity for Fleet. Registration materializes the portable kernel, one worker type, route metadata, and assigned Skill/unit. Registry serialization, approval, harvest, and cleanup contracts remain unchanged |
| completion delivery | Native Codex queue delivery uses the caller CODEX_THREAD_ID, at-least-once submission and exact pending/history checks. The interactive launcher and gateway are retired. Registered headless owners keep their separate App Server supervisor. Native question timing is preserved; empty answers are not user decisions. |
| role modes | Read `roles/MODES.md`, then run `adapters/codex/bin/preflight.sh mode-info <family/mode>`; read the reported `native_mode_path`, obey `fallback=reference-only` only for unsupported modes, and satisfy any named `tool_contract` / `tool_contract_check` before claiming tool-contract modes |
| mode guides | Use `adapters/codex/modes/<family>/<mode>.md` as the Codex-native realization guide reported by `mode-info`; satisfy named tool contracts or report unavailable before claiming support |
| design modes | Use `adapters/codex/modes/design/<mode>.md` as the Codex-native realization guide; satisfy `visual-harness` or report unavailable before claiming rendered visual verification |
| capabilities | Read `capabilities/README.md`, then run `adapters/codex/bin/preflight.sh capability-info <capability>`; do not assume Claude Skill invocation |

The private Codex owner supervisor derives its finite continuation ceiling from
the verified owner route: declared node count plus one slot for every unique
`resume_retry_boundaries` node, never below the compatibility floor. A positive
`--max-continuations` owner-launch value is an explicit replacement; missing or
mismatched route evidence stays at the finite floor.

Interactive completion is at-least-once through `thread/queue/add`, with stable
client identity and consumed/pending checks before retransmission. Only one
exact pending Hearting item may restart an interrupted thread. Native TUI
subscriptions and approvals remain untouched; no `thread/resume` workaround is
used. A refusal remains in the durable ledger and is rendered on the next real
prompt before acknowledgement. Parent identity is the caller's CODEX_THREAD_ID.

The wrapper validates the capability catalog, validates an optional non-owner
`worker_mode` through `mode-info`, and `_kernel/owner` rejects a worker mode
before prompt or registry writes. Registration materializes the portable
kernel, one worker type, route metadata, and assigned Skill/unit. Registry
writes and harvest rewrites are serialized with a `.lock` file.
The adapter accepts an optional non-owner `worker_mode` through `mode-info`; `_kernel/owner` rejects a worker mode before prompt or registry writes; registration carries the portable kernel, one worker type.
Registry writes and harvest rewrites are serialized with a `.lock` file.

## Model Mapping

`adapters/codex/config/models.conf` is the shipped concrete default. Install
seeds `$CODEX_HOME/agent-config/models.conf` once; a valid complete user file is
selected as one unit, otherwise the shipped file is selected as one unit.
Behavioral roles resolve through `preflight.sh role`; registered route profiles resolve as:

| Model profile | Concrete realization |
|---|---|
| `deep` | configured deep tier / `xhigh` |
| `balanced-deep` | configured deep tier / `medium` |
| `balanced` | configured light tier / `high` |
| `light` | configured light tier / `medium` |
| `mini` | configured mini tier / `low`, lifecycle/micro-only |
| `top` | configured top tier / `xhigh`; the main-session-only model, only through a route that sealed `top` for its depth-1 owner |

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

The profile is route-sealed and does not rename a role, worker type, or mode.
Fast roles, including implementation, default to light; deep roles default to
deep. Environment overrides remain available to non-route role selection and
checked capacity substitution, but not as a route-profile replacement.
`external adversary` remains unavailable unless `AGENT_MODEL_EXTERNAL` or
`AGENT_EXTERNAL_CMD` establishes an independent execution path. Generated
native-agent TOML pins are separate static role projections; route-bound
registered execution receives the compiled profile explicitly.

## Current Projection Boundary

`codex_setting/` should remain minimal and explicit. It may expose `AGENTS.md`,
`README.md`, `core/`, `capabilities/`, `roles/`, `bin/`, `codex-skills`,
`codex-agents`, `codex-hooks`, selected tools, and selected utilities, but must not expose Claude-native
`settings.json`, `commands/`, root `skills/`, `hooks/`, or `statusline.sh` as if Codex
could consume them.

`codex_setting/codex-plugin-marketplace` points at the dedicated marketplace
projection `adapters/codex/plugin-marketplace/`, not at the entire Codex
adapter. That projection exposes only `.agents/plugins/marketplace.json` and
`plugins/hearting-codex`.

`codex_setting/tools` points at `adapters/codex/tools/`, not the entire shared
`tools/` directory. The current allowlist is:

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
- `design/convert-harness.sh` (Codex-owned launcher for PDF/PPTX/bundle design export via the shared `convert.mjs`)

Do not project `build-manifest.py`: it is a harness development tool that reads
Claude adapter skills, agents, and settings. Do not project `web-bundle` until
Codex has a documented design/tooling realization that uses it directly. The
shared `design-mcp` package is not projected wholesale; Codex exposes the
adapter-owned visual harness launcher plus the converter launcher
(`design/convert-harness.sh`, wrapping the shared `convert.mjs` for PDF/PPTX/
bundle export — the design-handoff surface the visual harness alone does not
cover).

### MCP registration (design)

`preflight.sh mcp` reports `design_mcp_projection=policy-not-adopted-approval-gated`
rather than `unsupported`: the design MCP server *can* be registered with Codex and
its tools are discoverable and consume screenshots (runtime-verified — a
`[mcp_servers.design]` stdio server exposes the six tools, and a `codex exec` run
read `DESIGN PROBE` text out of a screenshot). The adapter does **not** adopt it as
the default design surface for two reasons: (1) policy — the owned visual harness +
converter launcher already cover render/screenshot/console and export without a
persistent server dependency; (2) a noninteractive `codex exec` under
`approval_policy = "never"` auto-denies MCP tool calls, so the render→view loop only
works interactively (TUI approval) or under an approval/trust policy that permits the
tool.

To register the design MCP server on a machine that wants the MCP path (guidance
only — the adapter never mutates `$CODEX_HOME/config.toml`):

```toml
[mcp_servers.design]
command = "node"
args = ["<agent-home>/tools/design-mcp/server.js"]
```

Then run design work in an interactive Codex session (so tool approvals can be
granted) or set an approval/trust policy for the project that allows the tool
(see `adapters/codex/config/approval-sandbox.toml`). Actually performing the
registration is out of scope for the adapter; this section documents the path.
For headless/export use without a server, prefer `preflight.sh convert
<pdf|bundle|pptx> <file.html>`.

`codex_setting/utilities` points at `adapters/codex/utilities/`, not the entire
shared `utilities/` directory. The current allowlist is:

- `agent-home.sh` (Codex-owned wrapper; no Claude runtime-home fallback)
- `artifact-root.sh`
- `agent-worklog-state.sh`
- `harness-status.sh`

Do not project the shared `dispatch-liveness.sh`; it is the cross-harness
registry/wait fallback, while Codex uses the adapter-owned
`adapters/codex/bin/dispatch-liveness.py`, exposed as
`adapters/codex/bin/preflight.sh liveness [jobs.log]`, and maps open dispatch
jobs to `~/.codex/sessions/**/*.jsonl` by transcript `cwd`. Codex harvest is
adapter-owned under `adapters/codex/bin/preflight.sh harvest` and only updates
the portable jobs registry from `open` to `done`; it never performs merge or
worktree cleanup. Do not project material/design helpers such as `extract_web_figures.py` until a Codex
capability uses them directly.

### SD-15 limit-death detection (OPERATIONS §5.10 ⑨) — parity: realized

`adapters/codex/bin/dispatch-headless.py` ports the Claude wrapper's SD-15
early-limit-death detection homomorphically: `--early-exit-watch <secs>` watches a
just-launched `codex exec` child; if it exits within the window and its log tail matches a
limit/auth `DEATH_PATTERN`, the wrapper closes its own `jobs.log` row to
`done,note=dead-<reason>[,reset=<x>]`, writes `usage-reset.codex` under the
canonical dispatch state root (the registry's parent directory, SD-112
§13.33.2 — never a release-relative path; SD-16 usage-check cache), and
surfaces `early_death=`/`row_closed=` on stdout. No retry — detection,
closure, and surfacing only; re-dispatch/harness failover is the orchestrator's semantic
zone (⑧). `adapters/codex/bin/dispatch-liveness.py` adds the shared `route_authority.scan_anchored_death` log-tail scan (axis 6,
SD-15b) that judges an open row DEAD when its dispatch log shows a limit/auth pattern,
independent of transcript mtime. **Parity note vs OpenCode**: codex `exec` exits non-zero on
retry exhaustion (openai/codex#9148·#12677), so the launch early-exit-watch axis is realized;
runtime-currentness patterns (`exceeded retry limit`, `usage_limit_reached`, `429`) are
best-effort per 2026-07 issue evidence, conservative and kept in sync with the shared list.
Conformance: `adapters/codex/bin/dispatch-headless.sd15.test.sh`.

### SD-48~50 nested dispatch recovery — realized

Dispatch-depth-2 starts require checked tuple evidence and an inherited canonical
`AGENT_DISPATCH_JOBS`; noncanonical nested `--jobs` and unwritable global
registries fail before spawn. Rows carry attempt identity, launch authority,
fallback ordinal, and tuple evidence. `nested-headless` keeps the observed
Codex-in-Codex workspace-write tuple is supported only when the dispatch-depth-1 owner
has the wrapper-projected network contract, while `dispatch-chain` enforces
same-harness → cross-harness → native → inline ordering. Adapter launches attach
exact attempt, `pid`, and `/proc` start-tick identity; shared-worktree transcript
activity cannot revive an exited retry.

Before that owner exists, `dispatch-readiness` generates the exact-worktree
evidence atomically and automatically applies the prospective Codex owner
network and registry contract. A raw depth-0 `nested-headless` call without that
context is typed `prospective-owner-check-required`; it is not a runtime-global
network failure.

`dispatch-chain` selects a provisional launch lifecycle from its current PID
scope, and every Codex, Claude, or OpenCode wrapper rechecks its actual scope
before attempt registration. A transient wrapper promotes `detached` to
`foreground-scoped`, supervises the child until exit, and forwards termination
signals; durable scopes retain `detached`. Wrapper output and the exact jobs row
record the requested lifecycle, effective lifecycle, reselection result, and
bounded namespace evidence. `AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN=1` is the
checked long-lived-namespace override, but it is honored only when the
launcher's own observed scope is host-like and its sealed parent sandbox is
not a checked sandboxed value; a `codex/headless/workspace-write` owner
sandbox is never an eligible asserter, so the override there always promotes
to `foreground-scoped` (`launch_lifecycle_override=rejected`). When the
runtime hands a foreground `dispatch-batch` call to the background, that call
has not failed — poll `dispatch-current --route <id>` or wait at the runtime
join instead of switching lifecycle.
For a foreground Codex child whose actual parent tuple is
`codex/headless/workspace-write`, the child uses `danger-full-access` only as
its inner runtime setting because nested mount setup is unsupported there; the
already-checked outer workspace-write sandbox remains the effective boundary.
Wrapper output and the exact attempt row record the effective runtime sandbox.

### SD-51~53 direct headless launch — realized

Dispatch contract v3 removes the resident launch broker, request spool,
broker heartbeat, broker lease, and broker fencing identity. `dispatch-chain`
invokes the checked Codex, Claude, or OpenCode adapter wrapper directly from the
conductor. A stable attempt is first registered with `launch_claimed=0`; a
parent-death-safe fence remains blocked while the wrapper publishes complete
PID/start/namespace/leader-PGID identity and the only claim transition under
the canonical jobs lock. The fence records `launch_started=1` immediately
before payload exec. A duplicate or already-started claim starts zero children,
and a dead unstarted fence is retryable only after exact group quiescence.
Standard+ dispatch-depth-1 Codex owners run with
`sandbox_workspace_write.network_access=true` and
`AGENT_NESTED_HEADLESS_NETWORK=1`. Their writable `CODEX_HOME` lives at
`homes/codex/<worktree-key>.<release-key>` beneath the canonical dispatch state root,
outside the source worktree. These homes
link existing auth/config without copying or mutating credentials and keep
nested session/app-server state inside the owner sandbox. The home is linked to
the release the launch resolved, and the owner tree gets that same release as
its `AGENT_HOME` (not the moving pointer), so a release activated while the
owner runs changes neither; a projection check failure names
`reason=codex-runtime-projection-mismatch`. Dispatch-depth-2 workers
do not inherit the network widening. The outer Codex sandbox also admits only
the existing harness `.core-grounding` directory and Claude `session-env`
directory as downstream runtime scratch roots. This keeps adapter write guards
functional and lets a checked Codex→Claude worker initialize Bash without
making the rest of either runtime home writable.
Broker v1/v2 records remain readable for migration, and `preflight.sh broker`
retains only diagnostic `status` and idempotent `stop` during the drain release.

Foreground-scoped dispatch-depth-2 Codex workers reuse the already checked outer
`workspace-write` boundary and run the inner CLI with its mount sandbox disabled;
this avoids unsupported nested mount setup without widening the outer filesystem
or network authority. The wrapper also exports its exact self slug so
`dispatch-chain` can reject parent-identity drift before Fleet registration.

### SD-77 parent-bound orphan convergence — realized

Both registered-headless wrappers bind a dispatch-depth-2 attempt to one live
exact dispatch-depth-1 `parent_attempt_id`. Parent identity is checked before
spawn and again before fence release; the release requires aligned procfs/PID
namespace evidence, exact start identity, and `pgid == pid`. Foreground fences
retain parent-death coupling, while detached fences clear it before committing
`launch_started`. Process-group scans preserve inaccessible/incomplete as
unverifiable, and teardown signals only a current exact group leader after
adjacent identity checks. PID or PGID reuse is never signalled, same-slug
retries stay untouched, and completion markers or typed terminal handoffs
retain precedence.

The shared terminal inspector normalizes Codex `turn.completed` and Claude
stream-json `result` events into the same three-line handoff contract. Codex
liveness and harvest accept either registered harness while keeping runtime
native subagents, Claude subagents, and agent-team sessions outside this parity
claim.

## Worklog Boundary

Codex must treat `<agent-notes-root>` as mutable continuity state, not as harness
source. The `preflight.sh worklog` output describes the configured notes surface. Codex may read/write
notes-root files only when the task is explicitly about notes, triage, feedback,
or worklog routing. It must not copy worklog-board DBs, caches, `.env*`, build
output, dispatch logs, or worktrees into this repo.
## Stage-session capacity contract (2026-08-06)

- **Runtime support:** current Codex hook schema exposes native `PreCompact` and
  `PostCompact`; Codex also exposes checked native subagents.
- **Adapter realization:** every registered wrapper uses the same portable
  sub-session axes. The generated prompt includes a persistent ledger anchor;
  compact hooks flush/re-anchor. A sub-session is terminal evidence only and is
  rejected by `capability-route complete`; the owner alone publishes one
  aggregate stage marker.
- **Fallback:** untrusted/uninstalled hooks require explicit ledger commands;
  checked registered headless remains authoritative. Native helpers stay within
  one slice, mutate serially, return summary only, and have no gate authority.

## SD-110 App Server supervisor realization

`attempt_stage_advance` in `codex-app-server-supervisor.py` mirrors the Claude
session-resume realization function for function (kept in sync by hand, each
supervisor a self-contained process boundary): behind the same
`--enable-stage-advance` flag (off by default), it calls
`coordinate_stage_advance` with `RealStageAdvanceServices`, never lets a
refusal propagate past its own `dispatch.supervisor.stage-advance`/`-refused`
canary event, and feeds the durable `stage_advance_record_v1` it returns into
`receipt_with_stage_advance` under the identical flag before the receipt is
used for the next resume — the same single eligibility/delivery negotiation
decision documented in `core/OPERATIONS.md` §5.10 and the Claude adapter's own
call-site entry.

**Recorded asymmetry.** This supervisor's call site sits at its own
symmetric park point — immediately after `validate_delivery_timing`, before
the receipt enters the model's resume outbox — not at a
`terminal_route_completion` call, because this supervisor has no
`terminal_route_completion` precedent to sit in front of (Claude's fast-path
short-circuit for an already-fully-terminal route has no App Server
equivalent). `coordinate_stage_advance` is a distinct, unrelated function
from that fast path, so the missing precedent narrows nothing about SD-110
eligibility here; it only means this supervisor's own resume/terminal
handling for a route that is NOT advanced continues exactly as it did before
this cycle, without a comparable terminal shortcut. No SD-110 code path was
added or held back to compensate for this asymmetry.

## Execution access request

`dispatch-headless.py --execution-access-file <execution_access_v1.json>` (or
`AGENT_DISPATCH_EXECUTION_ACCESS_FILE`) adds only validated exact roots. The
one-shot `codex exec` builder emits them as repeated `--add-dir`; the App Server
supervisor builder emits repeated `--writable-root`. Existing grants and the
standard+ owner network predicate are unchanged when the option is absent.
Codex network access is a boolean OS-sandbox grant, not host allowlist
enforcement, so an `enforcement_required=any` request with `network.hosts` is
recorded as `granted-unenforced`; `os-sandbox` host enforcement is refused.
A writable-root request under an effective non-writable Codex sandbox, a child
request with no proven parent grant, and any request the current role cannot
enforce fail before registration/model spawn.
Owner-to-worker propagation and continuation hash binding remain a separate
unimplemented slice.

## Git commit grant

A commit-capable Codex run (an owner, or a single-session stage whose sealed node
is commit-expected) keeps the commit working under `workspace-write`, which
otherwise reads `.git` only. A linked worktree gets exact per-worktree and
common-dir `objects`/`refs`/`logs` roots. A primary checkout gets a named
permission profile (`utilities/codex_permission_profile.py`, applied by the
one-shot builder and, through `--primary-git-commit`, the App Server supervisor)
that makes `.git` writable while `.git/config`, `.git/hooks`, and, when present,
`.git/config.worktree` and `.git/info` stay read-only. A Codex without named
profiles gets no primary `.git` grant at all (`--add-dir .git` would open config
and hooks). Claude and OpenCode run with `runtime_sandbox=adapter-default`, no OS
filesystem sandbox, so they commit on a primary checkout unless launched inside a
Codex owner sandbox, whose grant they inherit.

Session and prompt bridges retain `hookSpecificOutput.additionalContext` for optional memory and lifecycle context.
