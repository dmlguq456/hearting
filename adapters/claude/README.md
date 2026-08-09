# Claude Code Adapter

This adapter maps the common agent harness onto Claude Code.

## Entry Points

| Surface | File |
|---|---|
| Session bootstrap | `adapters/claude/CLAUDE.md` |
| Runtime settings | `adapters/claude/settings.json` |
| Slash commands | `adapters/claude/commands/` |
| Runtime worker wrappers | `adapters/claude/bin/` |
| Dispatch registry metadata | `adapters/claude/bin/dispatch-headless.py` records route/depth ownership plus SD-49 `attempt_id`, exact `parent_attempt_id`, PID/start/PGID identity, launch authority, fallback ordinal, checked nested tuple evidence, and the exact summary-owner identity in the inherited canonical global registry. The owner is attached before worker fence release and continues producing early/debounced/final sidecars with Fleet closed. |
| Capabilities | `adapters/claude/skills/*/SKILL.md` |
| Role profiles | `adapters/claude/agents/*.md` |
| Hook scripts | `hooks/`, `utilities/` |
| Status line | `adapters/claude/statusline.sh` |

Fleet is read-only. Interactive Claude summary refresh remains a statusline
lifecycle producer, while registered Claude dispatch uses the attempt-owned
supervisor and `dispatch-reconcile --apply` exact-live recovery contract.

## Worker bootstrap boundary

Headless dispatch injects the portable kernel and one worker-type fragment.
Masked profiles expose only selected Skills/agents, a small runtime attach
layer, and the selected specialization; they no longer instruct a worker to
read all four main core documents. Worker detail is artifact-only and the
return is the fixed `artifact` / `verdict` / `blocker` envelope. Claude custom
subagents can still inherit the runtime's project/user `CLAUDE.md` hierarchy,
so that residual runtime input is reported separately from profile masking.

## Runtime Mapping

| Core Concept | Claude Code Implementation |
|---|---|
| capability | Skill |
| role profile | Agent |
| adapter bootstrap | `adapters/claude/CLAUDE.md` |
| agent home | `$HOME/.claude` by default; overridable with `AGENT_HOME` or `CLAUDE_HOME` |
| artifact root | primary-checkout canonical `.agent_reports` via `utilities/artifact-root.sh`; linked-worktree snapshots are read-only; legacy fallback only at the canonical root |
| worktree cleanup | `adapters/claude/bin/worktree-cleanup.sh`; dry-run first, apply only after merge + integrated verification + push |
| artifact-order gate | `hooks/artifact-guard.sh` |
| spec read gate | `hooks/spec-skill-gate.sh` + `hooks/spec-read-marker.sh` |
| git safety gate | `hooks/git-state-guard.sh` |
| material route gate | `hooks/material-route-guard.py`; same-session route compile marker plus source Edit/Write and `git commit` chokepoints |
| interactive owner completion | `hooks/dispatch-owner-rewake.py`; `PostToolUse(Bash)` `asyncRewake` arms only from a successful same-session dispatch-depth-1 owner start, waits outside the model, and returns one exact-attempt receipt without recurring background monitors |
| memory write guard | `hooks/builtin-memory-guard.sh` |
| memory candidate exposure | `UserPromptSubmit` runs `hooks/mem-recall-inject.sh`: active current-project/global capsule headlines and IDs only, maximum three / 1,200 UTF-8 bytes, fail-open. The model decides relevance and reads full records. The bridge publishes the same-turn receipt required by main-session material mutation; explicit `recall-gate` is the fallback |
| stage-session capacity | `dispatch-headless.py` projects the portable sub-session axes, phase brief, fixed-file fence, and `_internal/state/<attempt_id>.md`. `PreCompact` flushes the ledger, `PostCompact` re-reads it, and the edit hook denies missing/stale/out-of-list state. Sub-sessions carry `stage_authority=0`; only the dispatch-depth-1 owner aggregates the one stage gate. |

## Runtime Home Projection

Target layout:

```text
$HOME/agent_setting/        # neutral repo
$HOME/agent_setting/claude_setting/ # versioned Claude projection
$HOME/.claude/              # Claude Code runtime home
```

Claude Code should see the same files it expects today, but they should be symlinked from the versioned Claude projection where practical:

```text
$HOME/.claude/CLAUDE.md      -> $HOME/agent_setting/claude_setting/CLAUDE.md
$HOME/.claude/README.md      -> $HOME/agent_setting/claude_setting/README.md
$HOME/.claude/core           -> $HOME/agent_setting/claude_setting/core
$HOME/.claude/skills         -> $HOME/agent_setting/claude_setting/skills
$HOME/.claude/agents         -> $HOME/agent_setting/claude_setting/agents
$HOME/.claude/agent-modes    -> $HOME/agent_setting/claude_setting/agent-modes
$HOME/.claude/hooks          -> $HOME/agent_setting/claude_setting/hooks
$HOME/.claude/utilities      -> $HOME/agent_setting/claude_setting/utilities
$HOME/.claude/tools          -> $HOME/agent_setting/claude_setting/tools
$HOME/.claude/commands       -> $HOME/agent_setting/claude_setting/commands
$HOME/.claude/bin            -> $HOME/agent_setting/claude_setting/bin
$HOME/.claude/statusline.sh  -> $HOME/agent_setting/claude_setting/statusline.sh
```

Keep Claude-owned mutable state in `$HOME/.claude`: credentials, sessions, projects, history, shell snapshots, cache, daemon logs, and local DBs. Do not move those into the neutral repo.

## Model Role Mapping

The Claude Code adapter maps portable roles from `core/CONVENTIONS.md §2` to concrete models while preserving established operating quality. Shared docs use role names; only Claude-specific frontmatter and Agent calls use concrete model names.

| Portable role | Claude Code mapping | Reproduced behavior |
|---|---|---|
| `fast reviewer` | `sonnet` | Broad cost-efficient coverage, typo, style, cross-reference, structure, and verbatim checks |
| `fast fact-checker` | `sonnet` | Narrow citation, venue, year, metric, and lineage checks against source artifacts |
| `fast writer` | `sonnet` | Assemble verified artifacts into a final report |
| `deep reviewer` | `opus` | methodology, domain expertise, completeness, safety/security, architecture risk |
| `deep maker` | `opus` | Planning, research synthesis, and visual/editorial work requiring high judgment |
| `deep orchestrator` | `opus` xhigh | Stage gates, failover, and evidence judgment for standard+ dispatch-depth-1 ownership |
| `fast implementer` | `sonnet` | Routine implementation and refactoring; escalate complex API/library design |
| `orchestrator` | `sonnet` high | Balanced mechanical coordination of decided calls, paths, and states |
| `external adversary` | Codex CLI via `codex-review-team` | Independent hostile review for the `adversarial` intensity pass. The same Codex engine may host a neutral cross-harness parallel leg, but that is a reviewer role, not this hostile role. |
| `external adversary orchestrator` | `sonnet` wrapper | Invoke and summarize the external engine rather than perform the review |

Route-bound registered work uses a second, independent execution-budget axis:

| Model profile | Claude realization | Registered topology use |
|---|---|---|
| `deep` | `opus` / `xhigh` | standard+ ownership, convergence, and highest-risk legs |
| `balanced-deep` | `opus` / `medium` | quick one-shot conduction and subordinate deep-model judgment at lower coordination cost |
| `light` | `sonnet` / `medium` | routine implementation, verification, reporting, and breadth legs |
| `mini` | `haiku` / `medium` | lifecycle and micro-semantic helpers only; substantive dispatch-depth-1/2 work is rejected |

The route compiler seals `model_profile`; the wrapper resolves it through `config/models.conf` and may also receive the independently sealed `model_role`. A dispatch-depth-1 `_kernel/owner` is valid with a profile and no stage `worker_mode`. Non-route jobs retain explicit role/concrete-model selection. Registered inheritance and config-declared interactive-main-only models are rejected before launch; `fable` therefore remains available only to the interactive main session, while its usage/status telemetry stays visible.

Two `CONVENTIONS §1.1` properties are intensity-independent and this adapter honors them: every review the `품질관리팀` runs carries the refute-by-default adversarial stance (anchored in `CONVENTIONS §1.1` / `roles/MODES.md`; `agent-modes/qa/_review_rules.md` is the single source for the code-review, plan-review, and test modes that load it), and every declared independent group records its realized independence. Registry-v6 groups launch 2–4 blind dispatch-depth-2 siblings atomically, use at least two harness families when `cross-harness` is required, and add asymmetric model profiles and perspectives to reduce correlated error. The hostile `external adversary` pass stays reserved for `adversarial`. If an explicitly requested cross-harness axis cannot be realized, fail loudly; an auto-selected group may use typed same-family degradation while preserving and reporting profile/perspective diversity.

## Compatibility

Claude Code projects created before the neutral artifact root use `.claude_reports/`. This adapter recognizes both names at the project-wide canonical root. New projects should use `.agent_reports/`; existing projects can migrate later or keep the legacy directory indefinitely.

For shell code, use `utilities/artifact-root.sh`. In a linked task worktree it resolves the primary checkout, so a tracked local artifact snapshot is never a write target. Headless dispatch passes that exact path with Claude `--add-dir`.

For harness-home paths, use `utilities/agent-home.sh` or the equivalent rule: prefer `AGENT_HOME`, then `CLAUDE_HOME`, then `$HOME/agent_setting` when present, then `$HOME/.claude` as legacy fallback.
