# Claude Code Adapter

This adapter maps the common agent harness onto Claude Code.

## Entry Points

| Surface | File |
|---|---|
| Session bootstrap | `adapters/claude/CLAUDE.md` |
| Runtime settings | `adapters/claude/settings.json` |
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
| interactive owner completion | `hooks/dispatch-owner-rewake.py`; `PostToolUse(Bash)` `asyncRewake` arms from the start receipt or the registry's same-session dispatch-depth-1 owner row — never from the command text; an arm ledger keeps one waiter per attempt and re-arms after the owner's gate closes (SD-129) — waits outside the model, wakes at once when the owner raises a human gate, and returns one exact-attempt receipt without recurring background monitors |
| steward watch completion | `hooks/peer-steward-rewake.py`; `PostToolUse(Bash)` `asyncRewake` arms only from a same-session `peer-steward.py watch` armed line with `wake=hook`, waits on the watch receipt outside the model, acks it, and returns one exit-2 notice per watch; `hooks/peer-message-record.py prompt` sweeps un-acked receipts at the next prompt if the hook dies |
| memory candidate exposure | `UserPromptSubmit` runs `hooks/mem-recall-inject.sh`: active current-project/global capsule headlines and IDs only, maximum six / 2,400 UTF-8 bytes, fail-open. The model decides relevance and reads full records. The bridge publishes the same-turn receipt required by main-session material mutation; explicit `recall-gate` is the fallback |
| stage-session capacity | `dispatch-headless.py` projects the portable sub-session axes, phase brief, fixed-file fence, and `_internal/state/<attempt_id>.md`. `PreCompact` flushes the ledger, `PostCompact` re-reads it, and the edit hook denies missing/stale/out-of-list state. Sub-sessions carry `stage_authority=0`; only the dispatch-depth-1 owner aggregates the one stage gate. |

## Runtime Home Projection

Target layout:

```text
$HOME/hearting/             # canonical neutral repo
$HOME/hearting/claude_setting/ # versioned Claude projection
$HOME/.claude/              # Claude Code runtime home
```

Claude Code should see the same files it expects today, but they should be symlinked from the versioned Claude projection where practical:

```text
$HOME/.claude/CLAUDE.md      -> $HOME/hearting/claude_setting/CLAUDE.md
$HOME/.claude/README.md      -> $HOME/hearting/claude_setting/README.md
$HOME/.claude/core           -> $HOME/hearting/claude_setting/core
$HOME/.claude/skills         -> $HOME/hearting/claude_setting/skills
$HOME/.claude/agents         -> $HOME/hearting/claude_setting/agents
$HOME/.claude/hooks          -> $HOME/hearting/claude_setting/hooks
$HOME/.claude/utilities      -> $HOME/hearting/claude_setting/utilities
$HOME/.claude/tools          -> $HOME/hearting/claude_setting/tools
$HOME/.claude/commands       -> $HOME/hearting/claude_setting/commands
$HOME/.claude/bin            -> $HOME/hearting/claude_setting/bin
$HOME/.claude/statusline.sh  -> $HOME/hearting/claude_setting/statusline.sh
```

Keep Claude-owned mutable state in `$HOME/.claude`: credentials, sessions, projects, history, shell snapshots, cache, daemon logs, and local DBs. Do not move those into the neutral repo.

## Model Role Mapping

The Claude Code adapter maps portable roles from `core/CONVENTIONS.md §2` to concrete models while preserving established operating quality. Shared docs use role names; only Claude-specific frontmatter and Agent calls use concrete model names.

| Portable role | Claude Code mapping | Reproduced behavior |
|---|---|---|
| `fast reviewer` | `sonnet` | Broad cost-efficient coverage, typo, style, cross-reference, structure, and verbatim checks |
| `fast fact-checker` | `sonnet` | Narrow citation, venue, year, metric, and lineage checks against source artifacts |
| `fast writer` | `sonnet` | Short mechanical synthesis of verified artifacts |
| `deep editor` | `opus` | Reader-facing prose written for a person to read: final reports, polish, translation |
| `deep reviewer` | `opus` | methodology, domain expertise, completeness, safety/security, architecture risk |
| `deep maker` | `opus` | Planning, research synthesis, and visual/editorial work requiring high judgment |
| `deep orchestrator` | `opus` xhigh | Stage gates, failover, and evidence judgment for standard+ dispatch-depth-1 ownership |
| `fast implementer` | `sonnet` | Routine implementation and refactoring; escalate complex API/library design |
| `orchestrator` | `sonnet` medium | Balanced mechanical coordination of decided calls, paths, and states |
| `external adversary` | Codex CLI via `codex-review-team` | Independent hostile review for the `adversarial` intensity pass. The same Codex engine may host a neutral cross-harness parallel leg, but that is a reviewer role, not this hostile role. |
| `external adversary orchestrator` | `sonnet` wrapper | Invoke and summarize the external engine rather than perform the review |

Route-bound registered work uses a second, independent execution-budget axis:

| Model profile | Claude realization | Registered topology use |
|---|---|---|
| `deep` | `opus` / `xhigh` | standard+ ownership, convergence, and highest-risk legs |
| `balanced-deep` | `opus` / `medium` | quick one-shot conduction and subordinate deep-model judgment at lower coordination cost |
| `balanced` | `sonnet` / `high` | long multi-step execution after the decision is settled |
| `light` | `sonnet` / `medium` | routine implementation, verification, reporting, and breadth legs |
| `mini` | `sonnet` / `low` | lifecycle and micro-semantic helpers only; substantive dispatch-depth-1/2 work is rejected |
| `top` | `fable` / `max` | exception above deep: the main-session-only model, reachable only from a route that seals the explicit `top` profile on a dispatch-depth-1 owner (`model_source=profile-top`; the wrapper refuses `top` without that route); no cascade in or out, no `--model` override |

These are the shipped defaults and equal the user's runtime mapping (2026-09-09
사용자 결정: a runtime value changed by instruction becomes the shipped default).
The top model is reserved for the main session, so both deep-side profiles ride
`opus` and separate by effort. The deep-tier capacity cascade is `opus -> sonnet`.

The route compiler seals `model_profile`; the wrapper resolves it through the
complete user copy at `$CLAUDE_CONFIG_DIR/agent-config/models.conf` (default
`~/.claude`) when valid, otherwise through the complete shipped
`config/models.conf`. Installation seeds the user copy once and never rewrites
or removes it. The wrapper may also receive the independently sealed
`model_role`. A dispatch-depth-1 `_kernel/owner` is valid with a profile and no
stage `worker_mode`. Non-route jobs retain explicit role/concrete-model
selection. Config-declared interactive-main-only models are rejected before
launch, and so is registered inheritance while that list names a model (one
rule for every adapter, `utilities/model_config.py`). The shipped `CFG_MAIN_SESSION_ONLY_MODELS`
list names `fable`, so a registered headless or native delegated launch of Fable
is refused with a typed reason instead of being silently remapped; the deep tier
launches `opus` there. A user copy with a different list replaces this one whole —
and that list is checked against the *generated* agent definitions, which are built
from the **shipped** file. Restricting a model those definitions pin denies the
matching subagent type outright (`native-subagent-main-session-only-model`), so a
user copy naming `opus` today would deny `deep` and `general-purpose`. The same
mismatch appears for one release cycle whenever the two files are changed and the
new release is not yet installed.

Two `CONVENTIONS §1.1` properties are intensity-independent and this adapter honors them: every review or verification unit carries the refute-by-default adversarial stance (anchored in `CONVENTIONS §1.1` / `roles/MODES.md`; `roles/units/_shared/stance.md` is the single source those units load), and every declared independent group records its realized independence. Registry-v6 groups launch 2–4 blind dispatch-depth-2 siblings atomically, use at least two harness families when `cross-harness` is required, and add asymmetric model profiles and perspectives to reduce correlated error. The hostile `external adversary` pass stays reserved for `adversarial`. If an explicitly requested cross-harness axis cannot be realized, fail loudly; an auto-selected group may use typed same-family degradation while preserving and reporting profile/perspective diversity.

## Compatibility

Claude Code projects created before the neutral artifact root use `.claude_reports/`. This adapter recognizes both names at the project-wide canonical root. New projects should use `.agent_reports/`; existing projects can migrate later or keep the legacy directory indefinitely.

For shell code, use `utilities/artifact-root.sh`. In a linked task worktree it resolves the primary checkout, so a tracked local artifact snapshot is never a write target. Headless dispatch passes that exact path with Claude `--add-dir`.

For harness-home paths, use `utilities/agent-home.sh` or the equivalent rule: prefer `AGENT_HOME`, then `CLAUDE_HOME`, then a managed release, `$HOME/hearting`, legacy `$HOME/agent_setting`, and finally the managed-release default path unvalidated (`$HOME/.claude` is the runtime home, not a harness root).

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
explicit settings win, and other missing required keys retain whole-file fallback,
except tier keys (`CFG_TIER_<tier>_MODEL/EFFORT`) of a tier that the user copy never
references **and** this adapter's own wrappers never read by name: a release that adds
a tier (2026-09-08 `balanced-deep`) leaves an older complete user copy selected
whole-file, so its own main-only list and tiers keep applying (receipt
`unreferenced_tier_keys`). The role mappers reach `CFG_TIER_DEEP_MODEL` and friends
without a profile, so those keys stay required; the per-adapter list is declared in
`utilities/model_config.py` (`WRAPPER_REQUIRED_TIERS`). A test fails if a wrapper
under `adapters/<adapter>/bin` starts naming a tier the list omits; a consumer that
builds the key name at runtime or lives elsewhere has to be added to the list by hand.
OpenCode reports collapsed-balanced-to-light and preserves its full light budget.
No install/update/reapply/uninstall writes the normalization back. Source checks
do not activate an installed release or change an in-flight sealed route.

A profile value may name a tier (`deep:high`) or an explicit model
(`model/<id>:high`). This keeps existing user tier keys intact when two profiles
need different models. The shipped deep point is the `deep` tier (`opus`/xhigh);
balanced-deep rides its own `balanced-deep` tier (`opus`/medium), so the two
deep-side profiles currently share a model and differ only in effort. The `top`
tier (`fable`/max) is the one exception to `CFG_MAIN_SESSION_ONLY_MODELS`, and
only through a route that sealed the `top` profile for its owner. A user-selected legacy tier continues to override the shipped profile.
