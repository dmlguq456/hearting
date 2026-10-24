# loops/ — Persistent Loop Catalog

This directory contains processes that run independently **outside** an agent session. Skills, agents, and hooks are in-session components: hooks enforce rules at tool-call time, while loops run independently of any session.

## Relationship to Autopilot

All task work belongs to the `autopilot-*` pipelines and their agents, Skills, and hooks. Loops operate only before a pipeline—detecting when it should run and proposing work—or after it, organizing artifacts, monitoring state, and checking instructions. No loop may change routing, pipeline order, or artifact conventions. Future loops such as training monitors and goal loops follow the same rule: when work is needed, invoke a pipeline instead of doing the pipeline's job directly.

**Autopilot is the verb—doing the work. A loop is the adverb—when, how often, and until when.** This boundary does not prohibit maintenance and monitoring. A loop may perform reversible, unambiguous maintenance such as memory pruning or removal of dead artifacts without supervision, provided it reports every action under D-25 below.

## Four Layers of Loop Engineering

The same action → verification → adjustment pattern runs at four timescales: **seconds (tools) → minutes (QA) → days (work) → weeks (settings)**.

| Layer | Cadence | Mechanism | Harness location |
|---|---|---|---|
| L1 agent loop | Seconds | LLM → tool → repeat; runtime-owned | Consumed from the active adapter runtime, such as Claude Code |
| L2 task loop | Minutes | Produce ↔ verify within one task; maker/verifier and QA rounds | Existing Skills and agents |
| L3 work loop | Hours to days | Detect, dispatch, and record outside sessions; cron + headless | This directory: `oncall`, `note` |
| L4 meta loop | Weeks | Test and improve the harness itself | This directory: `drill`, plus candidate `setting-audit` |

Common rules:

- **Loop autonomy (Cluster F D-25, redefined 2026-06-22):** a loop may handle *reversible and unambiguous* work unattended, but must report every action. The two guards are: (1) recovery must be guaranteed through a graveyard, git, or an equivalent path; and (2) every unattended action must appear in the morning briefing. Escalate only work that is difficult to reverse or requires judgment to the morning desk under D-26. This replaces prior approval with reversibility plus retrospective disclosure. The earlier rule that loops could only report or propose is retired; the curator already pruned unattended, and per-item approval was inefficient. Branch merges remain selected by the main agent under `core/OPERATIONS.md §5.10`.
- **Harness-policy exception:** D-25 never authorizes a loop to edit active instructions, portable/adapter source, generated projections, plugins, or runtime-owned config. Harness improvement follows the proposal and version-bound realization contract in `loops/improvement.md`; adoption remains a separate spec/code/release cycle.
- Execution traces live in `loops/*.log`, rotate themselves, and are gitignored. They consume subscription usage rather than a separate billing channel.
- Triggers have three forms: time-based through cron, event-based on demand, and state-based monitoring of an external signal.

## Active Loops

Filenames stay ASCII. Display names may pair a human-readable name with the identifier.

| Loop | Type | Trigger | Scope | Work | Output | User touchpoint |
|---|---|---|---|---|---|---|
| **On-call** (`oncall`) | Time | Cron at 05:37 | Workspaces: repos, artifacts, experiments, recent memory mutations, loop health, and missing drills | Overnight patrol; corroborate at most two memory-backed harness incidents and exact-deduplicate evidence into the proposal inbox, then report | `notes/oncall/<date>.md` plus offline proposal evidence | Review proposal IDs in the morning report; adoption remains separate |
| **Note** (`note`) | Time | Cron at 05:03 | Previous day's artifacts | Idempotent worklog-board Layer 2 note creation and routing | `notes/_layer2/notes/` plus digest | Worklog-board `/triage` |
| **Drill** (`drill/`) | Event | `drill/run.sh` after behavioral instruction changes; periodic `--sample 2`; related cases or full run only after major guard/routing changes | Main-agent compliance with instructions | Headless fixture tests and scoring; on failure, automatically draft diagnosis and a proposed fix without applying it | `drill/results/<timestamp>/` plus `<case>.diagnosis.md` | Approve a proposed fix after failure |
| **Study** (`study`) | Time | Sunday cron at 06:17 | External developments against the current settings | Survey recent agent-engineering work and adapter changes, compare with the harness, and produce proposals only; critical items may include an automatic draft | `notes/study/<date>.md` | Sign off on a proposal, apply it, then run a drill |
| **Runtime watch** (`runtime-watch`) | State | At most daily, or manually after a runtime-currentness event | Official Codex and Claude Code facts against local adapter projections | Fingerprint authoritative sources and probe local CLI/projection/usage helpers; report or propose only, with no policy auto-edits | `notes/runtime-watch/<date>.md` | Propose an `autopilot-spec`/`autopilot-code` cycle when change is detected |

The overnight order is 05:03 `note`, then 05:37 `oncall` to avoid overlap. `runtime-watch` is state-based and manual rather than mandatory every day because it checks network and policy currentness; this follows the 2026-07-13 Codex-window currentness incident.

## Loop Continuation Uses the Common Workflow Contract

A loop that opens or observes tracked work is bound by `core/WORKFLOW.md §0.6`
like every other surface: process exit is not workflow completion, and a
non-terminal stage must carry `inline-next`, `supervised`, `human-gate`, or
`monitor`. `oncall`, `note`, `study`, `drill`, and `runtime-watch` all follow
that state machine — a loop that detects a condition and proposes work either
registers the successor, records the human gate that owns it, or arms
`utilities/workflow-supervisor.py`.

“Automatic follow-up is impossible” is a real, allowed outcome, but only when no
supervisor exists for that surface; it is never a way to end a turn with an open
stage. An arbitrary detached shell is not a continuation and must not be
presented as one — an unsupervised background process is untracked work, whatever
its output says. `OPERATIONS §5.12` owns the supervisor mechanics.

## Backlog

| Candidate | Type | Start condition |
|---|---|---|
| **Goal loop** | Repeat until a goal is met | The first real use case with mechanically verifiable completion, such as all tests passing or no empty ablation-table cells. Required parts: an immutable mechanical goal, a fresh session and state file per round, a verification gate, human escalation after N rounds without progress, and a maximum round count |
| Training monitor | State | The next `autopilot-lab` setup, once real log formats and checkpoint paths are known |
| Code discovery | Time | After on-call operation is stable; scan broken tests and TODOs, then propose fixes |
| Three worklog-board operations panels: expanded approvals, an operations status strip, and a manual tab at `notes/manual/` | — | A spec update in a separate worklog-board repository session. All data already exists in `notes/oncall`, `notes/study`, `drill/results`, and `.dispatch/jobs.log`; the board only reads and displays it |

The automatic drill-failure diagnosis has graduated from the backlog: `run.sh` writes a diagnosis and fix draft but never applies it. Study can similarly include a critical-item draft. Both stop at the draft until the user signs off.

## Improvement Promotion

On-call, study, runtime-watch, drill diagnosis, and future incident collectors may write
bounded evidence to the offline proposal inbox. They do not gain an apply
command by doing so. Portable proposal state and runtime realization state are
separate, and official runtime/plugin updates require a new realization check.
See `loops/improvement.md` and `tools/improvement/README.md`.

## Core/Adapter Split for Loop Runners

Loop **cases**—prompts, fixtures, and assertions—are runtime-neutral; only the runner is adapter-specific. This extends the harness core/adapter split to loops. `loops/lib-runner.sh` normalizes `claude` (`claude -p --output-format json`), `codex` (`codex exec --json`), and `opencode` (`opencode run --format json`) into one transcript plus `turns|in_tok|out_tok|cost` contract through `run_case_on_adapter <adapter> …`. Select the runner with `drill/run.sh --adapter <adapter>` or `DRILL_ADAPTER`; the default is `claude`.

- **Portable cases:** marker paths use `DRILL_MARKER_DIR`, exported by the runner from the shared `harness_state_roots`/`dispatch_contract.state_root` resolver. Artifacts use `.agent_reports` with legacy `.claude_reports` compatibility. `g4_spec_gate` is the portable reference case. Design cases `g8*` and `mem_builtin` remain Claude-specific because they rely on Design MCP and Claude built-in memory.
- **On-call and study runners:** `run_claude_retry` in `loops/lib.sh` also dispatches through `LOOP_ADAPTER`, whose values are `claude` by default, `codex`, or `opencode`. Codex and OpenCode use their own sandbox and permission contracts and ignore Claude-only `--model` and `--allowedTools` arguments. Note semantics and scheduling belong to an external application; the harness exposes only the optional app-neutral `artifact-sink` port.
- **Behavioral verification prerequisite:** actually running cases on Codex or OpenCode requires both an installed runtime projection—bootstrap, hooks, and plugin—and an agent-home layout that keeps harness state writes such as markers inside the sandbox. Runner and case wiring are complete; this runtime gate remains.
- The diagnosis and judge meta layer still uses `claude -p`; it analyzes results and is not the runtime under test.

## Promoting a Case

After a real incident, reproduce it as a fixture under `drill/cases/`. The conversational trigger may simply ask to make the incident a drill case.
