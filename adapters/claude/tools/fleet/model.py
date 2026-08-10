"""Normalized cross-harness schema + shared helpers (zero-dep, stdlib only).

`Session` / `DispatchJob` are the harness-agnostic rows the render layer consumes.
Collectors fill them; no harness-specific logic lives here. Any field a harness
cannot provide stays `None` and renders as `—` (an explicit "not available",
never blank, per the PRD missing-cell rule).
"""
import hashlib
import json
import re
from dataclasses import dataclass, field, asdict, fields, is_dataclass
from typing import Optional


# --- shared time helpers (ps etime parsing + human format) ---
def etime_to_min(et):
    """ps etime ([[DD-]HH:]MM:SS) → whole minutes (int)."""
    et = (et or "").strip()
    if not et:
        return 0
    days = 0
    if "-" in et:
        d, et = et.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            days = 0
    try:
        nums = [int(p) for p in et.split(":")]
    except ValueError:
        return 0
    while len(nums) < 3:
        nums.insert(0, 0)
    hh, mm = nums[-3], nums[-2]
    return days * 1440 + hh * 60 + mm


def fmt_min(m):
    """minutes → '20m' / '5h20m' / '19d4h' (rolls over to days past 24h); None/invalid → '—'."""
    if m is None:
        return "—"
    try:
        m = int(m)
    except (TypeError, ValueError):
        return "—"
    if m < 0:
        return "—"
    if m < 60:
        return f"{m}m"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h}h{mm:02d}m"
    d, hh = divmod(h, 24)
    return f"{d}d{hh}h"


def dash(v, fmt=None):
    """Render helper: None/'' → '—', else fmt(v) or str(v)."""
    if v is None or v == "":
        return "—"
    return fmt(v) if fmt else str(v)


# 4-state herdr vocabulary (+ stale/dead/unused) — single source for render coloring.
LIVENESS_STATES = ("working", "idle", "unused", "blocked", "done", "stale", "dead", "unknown")
INTERACTION_KINDS = ("decision", "approval", "permission", "elicitation")

# Stable public name consumed by Fleet, the progress watchdog, liveness and
# registry reconciliation.  Keeping the exact-attempt verdict here prevents
# each surface from growing a subtly different PID/start-time classifier.
ATTEMPT_CLASSIFIER_SOURCE = "tools.fleet.model.classify_attempt_evidence"
PROGRESS_PHASES = ("launch", "analysis", "tool", "file-write", "test", "artifact", "terminal")


def _public_value(value):
    """Serialize additive Fleet values without exposing private evidence caches."""
    if is_dataclass(value):
        payload = {
            f.name: _public_value(getattr(value, f.name))
            for f in fields(value)
            if not f.name.startswith("_")
        }
        if value.__class__.__name__ == "WorkProjection":
            ambiguity = payload.get("ambiguity")
            payload["ambiguity"] = [] if ambiguity is None else (
                ambiguity if isinstance(ambiguity, list) else [ambiguity]
            )
        return payload
    if isinstance(value, dict):
        return {key: _public_value(item) for key, item in value.items()
                if not str(key).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    return value


@dataclass(frozen=True)
class ContextProjection:
    """Public, display-only context telemetry.

    Shape is intentionally fixed to ``used_pct``, ``band`` and ``source``.  The
    private evidence used to validate freshness and ordering never crosses this
    boundary.
    """
    used_pct: Optional[int] = None
    band: str = "unknown"
    source: str = "unknown"

    def to_dict(self):
        payload = _public_value(self)
        if payload.get("degradation") is None:
            payload.pop("degradation", None)
        return payload


@dataclass(frozen=True)
class ContextEvidence:
    """Private context sample used by the source-agnostic resolver."""
    used_pct: Optional[int] = None
    source: str = "unknown"
    sequence: Optional[tuple] = None
    source_head_sequence: Optional[tuple] = None
    observed_at: Optional[float] = None
    fresh_until: Optional[float] = None
    invalid_reason: Optional[str] = None


@dataclass(frozen=True)
class ProgressProjection:
    """Stable public progress shape: completed and total node counts."""
    done: int = 0
    total: int = 0

    def to_dict(self):
        return _public_value(self)


@dataclass(frozen=True)
class ActiveNodeProjection:
    """Stable public shape for one active route node."""
    id: str
    depends_on: tuple = ()
    level: Optional[int] = None
    unit: Optional[str] = None
    unit_choices: tuple = ()
    gate: Optional[str] = None
    write_scope: Optional[str] = None
    state: Optional[str] = None
    progress: Optional[dict] = None
    parallel_group: Optional[str] = None
    replica_group: Optional[str] = None  # one-window route compatibility alias
    model_profile: Optional[str] = None
    perspective: Optional[str] = None
    degradation: Optional[dict] = None

    def to_dict(self):
        payload = _public_value(self)
        if payload.get("degradation") is None:
            payload.pop("degradation", None)
        return payload


@dataclass(frozen=True)
class WorkProjection:
    """One fail-closed orchestration projection shared by every Fleet surface."""
    source: str = "none"
    route_id: Optional[str] = None
    route_hash: Optional[str] = None
    route_node: Optional[str] = None
    attempt_id: Optional[str] = None
    assigned_contract: Optional[str] = None
    unit: Optional[str] = None
    stage_label: Optional[str] = None
    node_state: Optional[str] = None
    active_nodes: tuple = ()
    progress: Optional[ProgressProjection] = None
    ambiguity: Optional[str] = None
    _route_view: Optional[dict] = field(default=None, repr=False, compare=False)

    def to_dict(self):
        payload = _public_value(self)
        # Public v16 shape is an array so future resolver diagnostics can be
        # additive without changing the JSON type.  Keep the scalar internal
        # storage compatible with existing constructors and comparisons.
        value = payload.get("ambiguity")
        payload["ambiguity"] = [] if value is None else (
            value if isinstance(value, list) else [value]
        )
        return payload


def project_of(cwd):
    """Grouping key for the render v2 cwd-project groups — one group per parent repo.

    Rule precedence (documented, not accidental): `-wt` OUTRANKS `_worktrees`, and both
    passes are OUTERMOST-COMPONENT-FIRST (left→right). This is a TWO-PASS scan (all
    components checked for `-wt` before ANY component is checked for `_worktrees`) —
    a single interleaved pass would let an outer `_worktrees` component win over an
    inner `-wt` component, which is the wrong precedence (see the mixed 7th test case
    below). Edge cases verified — see plan Verification §1 / dev_logs/step_01_model.md:
      /x/hearting-wt/fleet-dashboard                   -> hearting
      /x/.claude/worklog-board-wt/studio-c2            -> worklog-board
      /x/.claude-wt/definitions-manifest               -> .claude
      /x/Stream_Diar_Baselines_worktrees/m5b_ls_eend_engine -> Stream_Diar_Baselines
      /x/worklog-board.broken-20260629-151852          -> worklog-board
      ''                                                -> (unknown)
      /a/foo_worktrees/bar-wt/leaf                     -> bar   (outer `_worktrees` component
                                                                   loses to inner `-wt` component
                                                                   because of the two-pass order)

    Known accepted edge: basename-only merge means `/home/alice` and
    `/home/nas/user/alice` both project to `alice` (same human, different mount —
    treated as one group; acceptable, not a bug).

    Quirk (not a bug, a consequence of rule 1 being unable to distinguish a worktree
    PARENT from a leaf literally named `<x>-wt`): a non-worktree directory named
    e.g. `/x/my-cool-wt` (no children, not actually a worktree root) still truncates
    to `my-cool` — rule 1 has no way to tell the two apart from the path alone.
    """
    if not cwd:
        return "(unknown)"
    parts = [p for p in cwd.rstrip("/").split("/") if p]
    # Group each temporary drill repository and its worktrees as one drill:<case> card.
    if cwd.startswith("/tmp/"):
        for comp in parts:
            m = re.match(r"^drill-(.+)-[^-/]+$", comp)
            if m:
                return "drill:" + m.group(1)
    # pass 1 (outermost-first): `-wt` suffix — takes precedence over `_worktrees`.
    for comp in parts:
        if len(comp) > 3 and comp.endswith("-wt"):
            return comp[: -len("-wt")]
    # pass 2 (outermost-first): `_worktrees` suffix.
    for comp in parts:
        if len(comp) > len("_worktrees") and comp.endswith("_worktrees"):
            return comp[: -len("_worktrees")]
    # fallback: basename, with a trailing `.broken*` marker stripped.
    base = parts[-1] if parts else ""
    base = re.sub(r"\.broken.*$", "", base)
    return base or "(root)"


@dataclass
class Session:
    """One live harness session (backbone = one per matched process)."""
    harness: str                       # claude | codex | opencode
    pid: int
    cwd: str = ""
    orphan: bool = False               # /proc/<pid>/cwd had ' (deleted)' (worktree gone)
    app_server: bool = False           # codex app-server companion (procscan-detected — see collectors/procscan.py)
    managed_dir: Optional[str] = None  # managed-codex session state dir parsed from argv (join key
                                       # between a `--remote` TUI client and its `app-server` peer)
    is_child: bool = False             # worker/headless child marker — shown as a dispatch row under its parent, not as a top-level session
    detached: bool = False             # running in a tmux session with no client attached (backgrounded) — distinct from idle
    elapsed_min: int = 0               # ps etime
    # --- enrichment (None = harness doesn't expose it → render '—') ---
    session_id: Optional[str] = None
    slug: Optional[str] = None
    title: Optional[str] = None        # harness session title (ai-title/DB title) — render name zone only, slug fallback when None
    model: Optional[str] = None
    effort: Optional[str] = None
    ctx_pct: Optional[int] = None      # context window used %
    rl_5h: Optional[int] = None        # legacy fixed 5h slot (Claude + old Codex payloads)
    rl_7d: Optional[int] = None        # legacy fixed 7d slot (Claude + old Codex payloads)
    rl_ms: Optional[list] = None       # model-scoped buckets [[label, pct], ...]
    rl_rs: Optional[tuple] = None      # (reset_epoch_5h, reset_epoch_7d) — ↻ countdown in the meters
    rl_windows: Optional[list] = None  # dynamic account windows [[label, pct, reset_epoch], ...]; preferred over rl_5h/rl_7d when present
    cost: Optional[float] = None
    tokens: Optional[int] = None
    # Token-budget telemetry is explicit because legacy ``tokens`` is not
    # cross-harness comparable (Codex=current context; Claude/OpenCode=cumulative).
    active_context_tokens: Optional[int] = None
    context_window_tokens: Optional[int] = None
    session_input_tokens: Optional[int] = None
    session_cached_input_tokens: Optional[int] = None
    session_output_tokens: Optional[int] = None
    session_reasoning_output_tokens: Optional[int] = None
    session_total_tokens: Optional[int] = None
    status: Optional[str] = None        # raw harness status (claude idle/shell/busy)
    task_lifecycle: Optional[str] = None  # exact Codex task_started/task_complete/turn_aborted
    # F-47 (v30) — owned exec evidence, additive. `exec_child` = the long-lived non-helper
    # descendant this session is running ({'pid','comm','etime_s'}, collectors/procscan.py);
    # `exec_tool` = a Codex rollout tool_call with no matching output yet ({'name','command'}).
    # Both are None when there is no evidence — never a guess (prd.md:263).
    exec_child: Optional[dict] = None
    exec_tool: Optional[dict] = None
    # Exact privacy-minimal decision/approval evidence. Content is impossible
    # in the sidecar schema; only kind/source/time reach this public projection.
    interaction_state: Optional[dict] = None
    mtime: Optional[float] = None       # newest transcript/db mtime (epoch sec) for liveness
    liveness: str = "unknown"
    # --- F-25/F-26 registry first-class fields (all Optional → absent harness = None) ---
    proc_start: Optional[str] = None       # ACTUAL /proc/<pid>/stat field 22 (clock ticks, str)
    registry_proc_start: Optional[str] = None  # registry's procStart CLAIM — mismatch vs
                                           # proc_start = the pid was recycled (PID-reuse guard)
    started_at: Optional[float] = None     # registry startedAt (epoch sec)
    updated_at: Optional[float] = None     # registry updatedAt (epoch sec)
    registry_name: Optional[str] = None    # registry `name` — explicit name chain link (slug also carries it)
    kind: Optional[str] = None             # registry `kind` (interactive/...)
    provenance: Optional[str] = None       # best-effort launcher lineage: herdr|terminal|vscode|worker
    state_evidence: Optional[dict] = None  # F-25 classifier verdict + inputs (additive; --json via asdict)
    branch: Optional[str] = None        # git branch override — demo fixtures; None = compute from cwd
    branch_ahead: Optional[int] = None  # additive background git telemetry
    branch_behind: Optional[int] = None
    mem_worker: bool = False   # Memory worker or title refresher; summarized and hidden by default.
    # F-29 (v9, prd.md:290-295) — enrichment ONLY, never a session-existence signal (prd.md:291).
    # None = source absent/unconfirmed (honest gap, prd.md:292's "no guessing"); [] = source
    # checked, no sub-agents running.
    subagents: Optional[list] = None
    # F-16/F-17 merge (사용자 2026-07-19): live one-sentence status from the same haiku call
    # that produces the title — None = no fresh sidecar summary (render stays silent, F-13).
    summary: Optional[str] = None
    # F-63: sidecar write time of `summary` — render appends an aged `⏳<elapsed>` tag
    # past the live window. None with a summary = age unknown, no tag (additive).
    summary_ts: Optional[float] = None
    # v16 additive evidence and projections.  Underscored values are private caches.
    route_file: Optional[str] = None
    route_id: Optional[str] = None
    route_hash: Optional[str] = None
    route_node: Optional[str] = None
    attempt_id: Optional[str] = None
    assigned_contract: Optional[str] = None
    unit: Optional[str] = None
    worker_type: Optional[str] = None
    owner: Optional[str] = None
    model_role: Optional[str] = None
    context: Optional[ContextProjection] = None
    work_projection: Optional[WorkProjection] = None
    cap_grounding: Optional[dict] = None   # {capability, mode?, intensity?} for an inline entry
                                            # session (capability-grounding marker); None otherwise.
    association_ambiguity: Optional[str] = None
    _context_evidence: Optional[ContextEvidence] = field(default=None, repr=False, compare=False)
    _refresh_source: Optional[dict] = field(default=None, repr=False, compare=False)

    def to_dict(self):
        return _public_value(self)


@dataclass
class SubAgent:
    """F-29 (v9) — one sub-agent/child-session row observed under a parent Session.

    additive-only: this dataclass plus `Session.subagents` and
    `DispatchJob.subagents` are the entire F-29 surface; existing field shapes remain
    unchanged (prd.md:294 zero-regression contract).
    """
    agent_type: Optional[str] = None    # harness-reported sub-agent/type label; None = unknown
    active: bool = True                 # False = completed (dim, hidden unless `a` toggled)
    started_at: Optional[float] = None  # epoch sec, when available
    source: Optional[str] = None        # opencode-db | claude-sidechain | codex-state-db
    # Execution-budget observation (사용자 2026-07-29): which tier this sub-agent
    # actually runs on. None = source exposes no model/effort (honest gap,
    # prd.md:292 "no guessing") — currently only claude-sidechain fills these,
    # via the subagents/*.meta.json toolUseId join.
    model: Optional[str] = None         # resolved model id (or requested alias fallback)
    effort: Optional[str] = None        # reasoning effort level observed in the transcript
    # When a completed sub-agent actually FINISHED (사용자 2026-07-29: '언제
    # 끝났는지' — total runtime and time-asleep are different questions).
    # Claude: tool_result/task-notification timestamp, else the sub-agent
    # transcript's mtime; Codex: the thread's updated_at. None for active
    # entries and sources without a completion signal (honest gap).
    ended_at: Optional[float] = None    # epoch sec of completion, when observable

    def to_dict(self):
        return asdict(self)


@dataclass
class DispatchJob:
    """One headless dispatch job (autopilot-*/loops process, or jobs.log row)."""
    key: str                            # pipe key: autopilot-code / oncall / ...
    stage: Optional[str] = None         # plan | exec | test | done (live_stage)
    mode: Optional[str] = None          # legacy overloaded --mode value (read-only)
    capability_mode: Optional[str] = None  # entry capability behavior (dev/debug/...)
    worker_mode: Optional[str] = None   # non-owner family/mode compatibility projection
    mode_axis_conflict: bool = False    # owner/stage or canonical/legacy contradiction
    qa: Optional[str] = None            # --qa value
    pid: Optional[int] = None           # observer-authoritative dispatch pid; None when a registry pid
                                        # belongs to another namespace without a verified mapping
    proc_start: Optional[str] = None    # authoritative /proc/<pid>/stat field 22 — the other half of the pid's
                                        # identity. F-27 refuses to signal without it, so a job row
                                        # is only a kill target when this is filled alongside pid.
    pid_scope: Optional[str] = None     # namespace-local for dispatch-depth-2 launches whose recorded PID
                                        # is only meaningful inside the capability-owner namespace.
    pid_local: Optional[int] = None     # raw registry pid= value; display/audit only when not authoritative
    pid_local_start: Optional[str] = None  # raw registry pid_start= paired with pid_local
    pid_host: Optional[int] = None      # NSpid[0] candidate, usable only with bound proof+namespace
    pid_host_start: Optional[str] = None
    pid_host_ns: Optional[str] = None
    pid_ns: Optional[str] = None
    pid_observer_ns: Optional[str] = None
    pid_host_proof: Optional[str] = None
    pgid: Optional[int] = None
    pid_identity_source: Optional[str] = None  # local | host | None (unverifiable)
    model: Optional[str] = None         # dispatch runtime model (own statusline if resolvable; else parent's, filled at render)
    elapsed_min: Optional[int] = None
    slug: str = ""
    cwd: str = ""
    parent_sid: Optional[str] = None    # spawning parent session id (CLAUDE_CODE_SESSION_ID from environ)
    parent_cwd: Optional[str] = None    # fallback parent cwd when runtime session id is unavailable/mismatched
    parent_managed_dir: Optional[str] = None  # exact managed Codex state dir derived from
                                             # registered managed_sidecar_log (F-68)
    is_child: bool = False              # portable/adapter worker marker
    harness: Optional[str] = None       # claude | codex | opencode — dispatch runtime (None = unknown / jobs.log-only)
    qa_source: Optional[str] = None     # provenance of effective qa: argv | jobslog | plan | default
    source: str = "proc"                # proc | jobs
    status: Optional[str] = None        # raw jobs.log status (open/running/...)
    afterglow: bool = False             # F-46 (v29): a `done` registry row still inside the
                                        # 15-min afterglow window. Display-only and additive —
                                        # `status` stays the verbatim registry word, the row is
                                        # dim/blinkless, and it counts in no working/idle/job
                                        # census.
    liveness: str = "unknown"
    profile: Optional[str] = None       # dispatch profile name (masked config home) — None = main home
    artifact_root: Optional[str] = None  # registry artifact_root meta — a source-only worktree
                                         # (OPERATIONS §5.10) writes plans/ THERE, not under cwd
    branch: Optional[str] = None        # git branch override — demo fixtures; None = compute from cwd
    depth: int = 1                      # display compatibility alias; derived from dispatch_depth on current rows
    dispatch_depth: Optional[int] = None  # portable route topology: 0 main, 1 owner, 2 bounded node
    transport: Optional[str] = None
    execution_surface: Optional[str] = None
    registered_worker: Optional[bool] = None
    fallback_hop: Optional[str] = None
    attempt_schema_version: Optional[int] = None
    legacy_read_only: bool = False
    attempt_contract_status: Optional[str] = None
    parent_slug: Optional[str] = None   # parent dispatch slug for dispatch-depth-2 rows
    intensity: Optional[str] = None     # direct|quick|standard|strong|thorough|adversarial
    worker_type: Optional[str] = None   # owner | stage | review | support bootstrap overlay
    assigned_contract: Optional[str] = None  # exact portable Skill/contract assigned to worker
    unit: Optional[str] = None          # selected portable unit-catalog entry; independent of Skill/type
    worker_role: Optional[str] = None   # legacy compatibility metadata; never canonical identity
    capability_owner: Optional[str] = None  # owning capability slug/name for sub-workers
    effort: Optional[str] = None        # dispatch runtime effort (pipe `effort=`; None = parent-inherit)
    model_role: Optional[str] = None    # Portable model role from pipe model_role=.
    model_profile: Optional[str] = None  # route-sealed portable execution profile
    model_tier: Optional[str] = None     # adapter-resolved concrete tier
    profile_granularity: Optional[str] = None  # full | collapsed-balanced-deep | legacy
    parallel_group: Optional[str] = None
    replica_group: Optional[str] = None  # one-window registry compatibility alias
    perspective: Optional[str] = None
    parallel_leg_index: Optional[int] = None
    parallel_leg_count: Optional[int] = None
    state_evidence: Optional[dict] = None  # F-25 classifier verdict + inputs (additive; --json via asdict)
    # F-28a (v10, prd.md:302) — immutable route-record link, read-only. route_file is the
    # record's on-disk path (may be /tmp and already gone by the time fleet reads it — tolerant,
    # never an error); route_hash is absent for proc jobs (AGENT_ROUTE_HASH is not exported,
    # plan §3.2.2) so integrity still rests on the record's own recomputed hash (route.py P1).
    route_file: Optional[str] = None    # pipe route_file= / env AGENT_ROUTE_FILE
    route_id: Optional[str] = None      # pipe route_id= / env AGENT_ROUTE_ID
    route_hash: Optional[str] = None    # pipe route_hash= (jobs.log rows only)
    route_node: Optional[str] = None    # pipe route_node= / env AGENT_ROUTE_NODE — the node
                                        # this job is executing
    attempt_id: Optional[str] = None    # canonical registry attempt identity (SD-49)
    registry_order: Optional[int] = None  # append order in canonical jobs.log
    registry_priority: Optional[int] = None  # 0 canonical; larger values are legacy fallbacks
    title: Optional[str] = None         # the child session's own sidecar title, adopted in
                                        # collect_all (F-14 reach into dispatch rows) — None
                                        # keeps the slug as the row identity
    summary: Optional[str] = None       # the child session's own live sidecar summary,
                                        # adopted the same way as title (F-16/F-17 merge)
    summary_ts: Optional[float] = None  # F-63: sidecar write time of `summary`, adopted
                                        # with it so render can tag its age
    # F-29: runtime-native sub-agents owned by this exact dispatch attempt. None means
    # no trustworthy source; [] means the source was checked and found no calls.
    subagents: Optional[list] = None
    note: Optional[str] = None          # SD-64/71: registry `note=` annotation for this exact
                                        # attempt (e.g. "dead-parent-orphaned"); None = no note
    resume_boundary: Optional[str] = None  # SD-64/71: first incomplete route node, set only
                                        # alongside note == "dead-parent-orphaned"
    # F-50d (v33): the openai-codex plugin-queue record exactly as observed, so `--json`
    # consumers read the third-party fields verbatim instead of a fleet re-interpretation.
    # The prompt body is the one omission (F-50d: fleet never carries a prompt), marked as
    # `prompt_omitted` inside `request`.
    plugin_job: Optional[dict] = None
    # F-50f (v34): context/token telemetry joined from the plugin job's own Codex rollout
    # (`threadId` == rollout filename sid, exact-1 only). Display-layer additive — it is
    # attached after the F-50b state verdict and is never an input to it. None = honest gap
    # (no match, ambiguous match, or unparseable tail).
    plugin_telemetry: Optional[dict] = None
    # F-65: exact registered-dispatch telemetry uses the same public shape as a
    # live Session.  The counters stay explicit because active context and
    # cumulative session usage are different quantities.
    ctx_pct: Optional[int] = None
    active_context_tokens: Optional[int] = None
    context_window_tokens: Optional[int] = None
    session_input_tokens: Optional[int] = None
    session_cached_input_tokens: Optional[int] = None
    session_output_tokens: Optional[int] = None
    session_reasoning_output_tokens: Optional[int] = None
    session_total_tokens: Optional[int] = None
    exec_child: Optional[dict] = None
    exec_tool: Optional[dict] = None
    context: Optional[ContextProjection] = None
    work_projection: Optional[WorkProjection] = None
    cap_grounding: Optional[dict] = None   # {capability, mode?, intensity?} for an inline entry
                                            # session (capability-grounding marker); None otherwise.
    association_ambiguity: Optional[str] = None
    _context_evidence: Optional[ContextEvidence] = field(default=None, repr=False, compare=False)
    _dispatch_context_owned: bool = field(default=False, repr=False, compare=False)
    _refresh_source: Optional[dict] = field(default=None, repr=False, compare=False)

    def to_dict(self):
        return _public_value(self)


@dataclass
class ResourceJob:
    """One detached lab resource, deliberately separate from dispatch jobs."""
    run_id: str
    cwd: str = ""
    project: str = "(unknown)"
    job_type: str = "resource"
    resource_class: str = "lab"
    elapsed_min: Optional[int] = None
    liveness: str = "stale"
    pid: Optional[int] = None
    starttime: Optional[str] = None
    command_hash: Optional[str] = None
    process_group: Optional[int] = None
    registry_status: Optional[str] = None
    registry_path: Optional[str] = None
    log_path: Optional[str] = None
    log_updated_at: Optional[float] = None
    route: Optional[str] = None
    node: Optional[str] = None
    config_ref: Optional[str] = None
    config_sha256: Optional[str] = None
    source_commit: Optional[str] = None
    source_dirty: Optional[bool] = None
    source_git_state: Optional[str] = None
    started_at: Optional[float] = None
    # Tracked-workflow projection (OPERATIONS §5.12): a resource row exposes why it
    # ended, which registered attempt owns it, and what the workflow thinks it is.
    workflow_state: Optional[str] = None
    exit_code: Optional[int] = None
    ended_at: Optional[float] = None
    failure_class: Optional[str] = None
    parent_attempt_id: Optional[str] = None
    sentinel: Optional[str] = None
    state_evidence: Optional[dict] = None

    def to_dict(self):
        return _public_value(self)


# =============================================================================
# F-25 — single state classifier (PRD v8 §4.8). The ONLY place a fleet liveness
# string is decided. Collectors gather evidence; they do not judge.
#
# Source priority (plan §2.1) — a lower tier NEVER beats a higher tier when they
# contradict. A lower tier may only judge when the higher tier is SILENT, or
# refine a higher-tier verdict WITHIN THE SAME AXIS (§2.2):
#   tier 1  explicit registry declaration (jobs.log status / sessions/<pid>.json)
#   tier 2  strong process evidence (exact pid + start-time, fd ownership, env, orphan cwd)
#   tier 3  mtime heuristics (transcript/rollout/db recency) — derived, shown with `~`
# =============================================================================

# --- F-25 state model constants (single block; PRD v8 §4.8) ---
SESSION_WORK_SEC       = 60      # absorbed from liveness.py `age_min < 1.0`
SESSION_STALE_MIN      = 48 * 60 # absorbed from liveness.py STALE_MIN
JOB_STALE_MIN          = 15      # absorbed from dispatch.py _job_liveness(stale_min=15)
JOB_QUEUED_GRACE_MIN   = 15      # absorbed from dispatch.py _QUEUED_GRACE_MIN
ATTEMPT_HEARTBEAT_LIVE_SEC = JOB_STALE_MIN * 60
UNUSED_ACTIVITY_MS     = 2000    # §2.2: updatedAt-startedAt at or below this = never prompted
                                 # (measured: the pid 1168514 ghost sits at 119ms)

# F-50b (v33) — the openai-codex plugin queue declares its own status vocabulary in
# state.json. Only the words the plugin actually emits are translated into fleet's job
# vocabulary; anything else stays `unknown` rather than being given a synthesized meaning
# (F-50a: 미지 status 어휘는 의미 합성 금지). `running` is deliberately absent — its
# activity axis is decided by the pid evidence, not by the word alone.
PLUGIN_QUEUE_STATES = {
    "queued":    "queued",
    "completed": "done",
    "failed":    "dead",
    "cancelled": "killed",
}

# Downgrade dwell — a threshold must hold CONTINUOUSLY this long before the state drops.
# Upgrades (activity resumed) and strong evidence (dead/killed) are immediate (0).
# ★ Applies to tier-3 (mtime-derived) transitions ONLY — see HYST_APPLIES_TO_TIER.
HYST_DOWNGRADE_DWELL_SEC = {
    ("working", "idle"):   90,   # tick=2s → 45 ticks. Absorbs mtime 60s-boundary flapping.
    ("working", "stale"):  300,
    ("idle",    "stale"):  300,
    ("working", "queued"): 300,
    # The two 0-entries below are belt-and-braces documentation, not live config: both are
    # already settled earlier in settle() — idle→unused by the equal-rank fast path (idle and
    # unused both rank 4, so it is not a downgrade), and queued→dead by HYST_IMMEDIATE_STATES.
    # They are listed so the table reads as a complete statement of intent.
    ("idle",    "unused"):  0,   # unused rests on registry evidence, not time decay → immediate
    ("queued",  "dead"):    0,   # strong evidence
}
HYST_IMMEDIATE_STATES = ("dead", "killed", "done")   # never delayed
HYST_APPLIES_TO_TIER  = (3,)     # ★ dwell only for tier-3 derivations; tier-1/2 = immediate

# Downgrade ordering. Pairs absent from HYST_DOWNGRADE_DWELL_SEC settle immediately.
_STATE_RANK = {"working": 5, "idle": 4, "unused": 4, "blocked": 4, "queued": 3,
               "stale": 2, "dead": 1, "killed": 1, "done": 1, "unknown": 0}


class StateTracker:
    """Cross-tick memory backing the hysteresis dwell.

    Owned by model.py (not the render loop) because --json / --once / live all funnel
    through collect_all(); parking it beside the classifier is the only placement that
    behaves identically on all three paths.

    Keys: session ("s", harness, pid, proc_start) — proc_start is what makes this
    PID-reuse-proof; job ("j", slug).

    `prev` means "the previous SETTLE for this key", which is the previous tick only because
    each key is settled at most once per tick. Two settles of one key in a single tick would
    manufacture a dwell out of a first observation. That cannot currently happen — sessions
    key on (pid, proc_start) and jobs are deduped by slug before classification
    (dispatch.py `_scan_jobs_log` skips slugs already seen as proc rows, and the drill
    reconcile passes track=False for the row it drops) — so the invariant holds structurally.
    If a second same-tick settle ever becomes reachable, this is where the guard belongs.
    """

    def __init__(self):
        self._store = {}
        self._seen = set()

    def settle(self, key, state, tier, now, desc=None):
        """(effective_state, hysteresis|None, effective_tier, effective_desc).

        `desc` is the (source, rule) pair describing THIS tick's verdict. When a dwell holds
        the old state, the old state's own descriptors are returned too — otherwise the
        evidence would claim `working` while its `rule` read "no activity within 60s", and
        F-25's entire purpose is evidence you can actually audit.
        """
        self._seen.add(key)
        prev = self._store.get(key)
        if prev is None:
            # First observation — nothing to hold back against. --once/--json land here
            # every time, which is exactly why hysteresis is a no-op on snapshot paths.
            self._store[key] = {"state": state, "since": now, "pending": None,
                                "tier": tier, "desc": desc}
            return state, None, tier, desc
        if state == prev["state"]:
            prev.update({"pending": None, "tier": tier, "desc": desc})
            return state, None, tier, desc
        if (state in HYST_IMMEDIATE_STATES or tier not in HYST_APPLIES_TO_TIER
                or _STATE_RANK.get(state, 0) >= _STATE_RANK.get(prev["state"], 0)):
            # Strong evidence, a higher-tier truth, or an upgrade → land it now.
            prev.update({"state": state, "since": now, "pending": None,
                         "tier": tier, "desc": desc})
            return state, None, tier, desc
        dwell = HYST_DOWNGRADE_DWELL_SEC.get((prev["state"], state), 0)
        if dwell <= 0:
            prev.update({"state": state, "since": now, "pending": None,
                         "tier": tier, "desc": desc})
            return state, None, tier, desc
        pending = prev.get("pending")
        if not pending or pending[0] != state:
            # Target changed mid-dwell (A→B held, now A→C) → restart the clock against C.
            prev["pending"] = (state, now)
            pending = prev["pending"]
        elapsed = now - pending[1]
        if elapsed >= dwell:
            prev.update({"state": state, "since": now, "pending": None,
                         "tier": tier, "desc": desc})
            return state, None, tier, desc
        # Hold the old state. Report the held state's OWN tier/rule, and record the verdict
        # being suppressed under `hysteresis` so nothing is hidden.
        sup_source, sup_rule = desc if desc else (None, None)
        hyst = {"pending": state, "dwell_sec": dwell, "elapsed_sec": round(elapsed, 1),
                "suppressed_tier": tier, "suppressed_source": sup_source,
                "suppressed_rule": sup_rule}
        return prev["state"], hyst, prev.get("tier", tier), prev.get("desc", desc)

    def sweep(self):
        """Drop keys not seen this tick (unbounded-growth guard). Call once per tick."""
        for key in [k for k in self._store if k not in self._seen]:
            del self._store[key]
        self._seen.clear()

    def reset(self):
        self._store.clear()
        self._seen.clear()


_TRACKER = StateTracker()


def tracker_sweep():
    _TRACKER.sweep()


def reset_state_tracker():
    """Test hermeticity: clear cross-tick memory so dwell never leaks between cases."""
    _TRACKER.reset()


def _evidence(state, tier, source, rule, inputs, raw_status=None, hysteresis=None):
    # `inputs` is copied: evidence is a snapshot of the tick that produced it, and the
    # caller's dict must not be able to mutate a verdict after the fact.
    return {"state": state, "tier": tier, "source": source, "rule": rule,
            "derived": tier == 3, "inputs": dict(inputs), "raw_status": raw_status,
            "hysteresis": hysteresis}


def _settle(key, state, tier, now, source, rule):
    """Tracker hand-off shared by both classifiers → (state, tier, (source, rule), hysteresis)."""
    state, hyst, tier, desc = _TRACKER.settle(key, state, tier, now, (source, rule))
    return state, tier, desc, hyst


def exec_child_evidence(exec_child):
    """The `exec_child` dict when it is usable F-47 evidence, else None.

    Re-applies the ≥`SESSION_WORK_SEC` age gate here rather than trusting the collector:
    `classify_session` is a pure function over a caller-supplied evidence dict (demo
    fixtures, `--json` replays, tests), so the threshold has to hold at the decision point
    too. A malformed/short-lived entry is silent, never a promotion.
    """
    if not isinstance(exec_child, dict):
        return None
    comm = exec_child.get("comm")
    etime = exec_child.get("etime_s")
    if not comm or not isinstance(etime, (int, float)) or isinstance(etime, bool):
        return None
    if etime < SESSION_WORK_SEC:
        return None
    return exec_child


# F-47 (v47) — wait primitives. A session whose descended child is one of these is
# WAITING, not working: a poll loop's instantaneous sample almost always lands on its
# `sleep`, so promoting on it painted sleeping sessions green. The evidence is still
# shown (dim `⏳` badge, render.py) — only the promotion is suppressed. Deliberately a
# tiny POSIX-primitive list, not a helper list that rots (F-26): these names are the
# OS's fixed vocabulary, not this harness's.
EXEC_WAIT_COMMS = ("sleep", "wait", "inotifywait", "flock")


def exec_child_is_wait(exec_child):
    """True when the exec-child evidence names a wait primitive (F-47 v47)."""
    return (isinstance(exec_child, dict)
            and exec_child.get("comm") in EXEC_WAIT_COMMS)


def exec_child_is_background(exec_child, updated_at, now):
    """Whether later registry activity proves that an old child is background work.

    Claude can retain a background Bash process while later model turns continue.  Its
    registry then stays at ``shell`` even though that particular child no longer describes
    the foreground turn.  ``etime_s`` gives the child's approximate start and registry
    ``updatedAt`` gives the latest session activity.  A full work-window gap avoids treating
    ordinary status-write/process-start skew as a later turn.

    This realizes F-47's existing ``background child -> idle + dim badge`` contract.  Missing
    or malformed clocks preserve the older fail-open classification.
    """
    child = exec_child_evidence(exec_child)
    if child is None:
        return False
    if (
        not isinstance(updated_at, (int, float))
        or isinstance(updated_at, bool)
        or not isinstance(now, (int, float))
        or isinstance(now, bool)
    ):
        return False
    child_started_at = now - child["etime_s"]
    return updated_at > child_started_at + SESSION_WORK_SEC


def _session_status_state(status, exec_child=None, background_child=False):
    """tier-1 registry status → activity-axis state. None = registry is silent.

    F-47 (v30, prd.md:612): `shell` is "the Bash tool is running", not an idle synonym. On
    its own it stays idle (a shell waiting at a prompt really is idle), but combined with
    tier-2 evidence that the session owns a long-lived child it resolves to `working` — the
    registry says WHAT the session is doing and the process tree confirms it is still doing
    it, so the two tiers agree rather than compete. `busy`/`idle` are untouched.

    v47 exception: a wait-primitive child (`sleep` & co) never promotes — the process
    tree confirms the session is WAITING, and calling that working is the misread.
    """
    if status == "busy":
        return "working"
    if (status == "shell" and exec_child_evidence(exec_child)
            and not exec_child_is_wait(exec_child) and not background_child):
        return "working"
    if status in ("idle", "shell"):
        return "idle"
    return None


def _status_desc(status, state, exec_child, background_child=False):
    """(source, rule) for a tier-1 registry verdict — honest about the combined F-47 case."""
    if status == "shell" and background_child:
        child = exec_child_evidence(exec_child)
        return (
            "claude-registry+proc",
            "registry status=shell + long-lived child %s, but later registry activity proves background"
            % child.get("comm"),
        )
    if status == "shell" and exec_child_is_wait(exec_child) and exec_child_evidence(exec_child):
        child = exec_child_evidence(exec_child)
        return ("claude-registry+proc",
                "registry status=shell + wait primitive %s (%ds) — waiting, not promoted"
                % (child.get("comm"), int(child.get("etime_s"))))
    if status == "shell" and state == "working":
        child = exec_child_evidence(exec_child)
        return ("claude-registry+proc",
                "registry status=shell + long-lived child %s (%ds)"
                % (child.get("comm"), int(child.get("etime_s"))))
    return ("claude-registry", "registry status=%s" % status)


def deterministic_progress_fingerprint(ev_in):
    """Hash only bounded, attempt-scoped progress evidence.

    Transcript text/mtime and speech are deliberately excluded.  Callers may
    pass a registry transition, heartbeat, tool call, file/artifact signature,
    or test result.  The normalized JSON digest is stable across processes.
    """
    scoped = {
        "attempt_id": ev_in.get("attempt_id"),
        "route_id": ev_in.get("route_id"),
        "route_node": ev_in.get("route_node"),
        "registry_transition": ev_in.get("registry_transition"),
        "heartbeat": ev_in.get("heartbeat"),
        "tool": ev_in.get("tool"),
        "file_signature": ev_in.get("file_signature"),
        "artifact_signature": ev_in.get("artifact_signature"),
        "test_result": ev_in.get("test_result"),
        "terminal_observation": ev_in.get("terminal_observation"),
    }
    present = {key: value for key, value in scoped.items() if value not in (None, "", {})}
    if not any(key in present for key in (
        "registry_transition", "heartbeat", "tool", "file_signature",
        "artifact_signature", "test_result", "terminal_observation",
    )):
        return ""
    payload = json.dumps(present, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _matching_attempt_heartbeat(ev_in):
    """Return a structurally valid heartbeat for this exact route attempt."""
    heartbeat = ev_in.get("heartbeat")
    identity = ("attempt_id", "route_id", "route_node")
    if not isinstance(heartbeat, dict) or any(not ev_in.get(key) for key in identity):
        return None
    if any(heartbeat.get(key) != ev_in.get(key) for key in identity):
        return None
    if heartbeat.get("phase") not in PROGRESS_PHASES:
        return None
    try:
        if int(heartbeat.get("sequence", 0)) <= 0 or float(heartbeat.get("updated_at", 0)) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    return heartbeat


def _matching_attempt_terminal(ev_in):
    """Return exact attempt-scoped terminal infrastructure evidence."""

    terminal = ev_in.get("terminal_observation")
    identity = ("attempt_id", "route_id", "route_node")
    if not isinstance(terminal, dict) or any(not ev_in.get(key) for key in identity):
        return None
    if any(terminal.get(key) != ev_in.get(key) for key in identity):
        return None
    action = terminal.get("terminal_action")
    if not isinstance(action, str) or not (
        action in {"process-exited", "registry-terminal", "completed-marker"}
        or action.startswith("dead-")
    ):
        return None
    return terminal


def classify_attempt_evidence(ev_in, now=None):
    """Pure F-25 exact-attempt verdict shared by all dispatch surfaces.

    Return ``None`` only when neither an exact PID/start pair nor canonical
    attempt/route identity is available. Otherwise return an auditable verdict
    with deterministic progress evidence. This function never reads the process
    table itself.
    """
    identity = ("attempt_id", "route_id", "route_node")
    has_process_identity = ev_in.get("pid") is not None and bool(ev_in.get("proc_start"))
    has_recorded_identity = (
        has_process_identity
        or (
            ev_in.get("pid_local") is not None
            and bool(ev_in.get("pid_local_start"))
        )
    )
    if not has_recorded_identity and any(not ev_in.get(key) for key in identity):
        return None
    heartbeat = _matching_attempt_heartbeat(ev_in)
    terminal = _matching_attempt_terminal(ev_in)
    pid_scope = ev_in.get("pid_scope")
    observed = ev_in.get("observed_liveness")
    observed_state = observed.get("state") if isinstance(observed, dict) else None
    # The shared observer is authoritative only when it has a positive exact
    # process/registry conclusion.  ``unverifiable`` is deliberately no verdict:
    # Fleet may still have a stronger local procscan/transcript signal for older
    # current-contract rows that predate namespace metadata.
    if observed_state in {"alive", "terminal", "reconcile-needed"}:
        state = {
            "alive": "working",
            "terminal": "done",
            "reconcile-needed": "stale",
        }[observed_state]
        source = "shared-observer"
        rule = (
            "terminal-observed/reconcile-needed"
            if observed_state == "reconcile-needed"
            else "shared observed-liveness: %s (%s)"
            % (observed_state, observed.get("reason", "unknown"))
        )
    elif terminal:
        completed = (
            terminal.get("terminal_action") == "completed-marker"
            or terminal.get("note") == "completed-marker"
        )
        state, source = ("done" if completed else "dead"), "terminal-observation"
        rule = (
            "exact attempt has completion-marker terminal evidence"
            if completed
            else f"exact attempt observed terminal action {terminal['terminal_action']}"
        )
    elif pid_scope == "namespace-local":
        if heartbeat and heartbeat.get("phase") == "terminal":
            state, source = "done", "heartbeat"
            rule = "namespace-local attempt emitted an exact terminal heartbeat"
        elif (
            ev_in.get("pid_authoritative") is True
            and ev_in.get("pid_alive") is True
            and ev_in.get("proc_start_match") is True
        ):
            state, source = "working", "proc"
            rule = "namespace-bound authoritative pid/start-time is live"
        elif ev_in.get("pid_authoritative") is True and (
            ev_in.get("pid_alive") is False
            or ev_in.get("proc_start_match") is False
        ):
            state, source = "dead", "proc"
            rule = "namespace-bound authoritative process identity ended or changed"
        elif ev_in.get("attempt_descendants") == "empty":
            # SD-58: a heartbeat is the attempt talking about itself, and a row
            # that stopped mid-sentence keeps a fresh one forever. A proven-empty
            # scan for the attempt's own tag outranks it -- nothing is running,
            # whatever the last message said. Freshness alone left these rows
            # permanently open, because classify() produced no note to close on.
            state, source = "dead", "namespace"
            rule = "namespace-local attempt has no surviving attempt-tagged process"
        elif heartbeat and now is not None and now - float(heartbeat["updated_at"]) <= ATTEMPT_HEARTBEAT_LIVE_SEC:
            state, source = "working", "heartbeat"
            rule = "namespace-local attempt has a fresh exact UI heartbeat"
        else:
            # Reached when the scan itself was impossible (`unverifiable`), or on
            # an older caller that supplies no probe at all. Both stay fail-closed.
            state, source = "unknown", "namespace"
            rule = "namespace-local process identity has no authoritative observer proof"
    elif not has_process_identity:
        state, source = "unknown", "registry"
        rule = "exact registered attempt has no process identity"
    elif ev_in.get("pid_alive") is False:
        source = "proc"
        state, rule = "dead", "recorded attempt pid is not alive"
    elif ev_in.get("proc_start_match") is False:
        source = "proc"
        state, rule = "dead", "recorded attempt start-time mismatch (pid reuse)"
    elif ev_in.get("pid_alive") is True and ev_in.get("proc_start_match") is True:
        source = "proc"
        state, rule = "working", "recorded attempt pid/start-time is live"
    else:
        source = "proc"
        state, rule = "unknown", "exact attempt identity is not currently verifiable"
    return {
        "state": state,
        "tier": 2,
        "source": source,
        "rule": rule,
        "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
        "attempt_id": ev_in.get("attempt_id"),
        "route_id": ev_in.get("route_id"),
        "route_node": ev_in.get("route_node"),
        "pid": ev_in.get("pid"),
        "pid_scope": pid_scope,
        "proc_start": ev_in.get("proc_start"),
        "actual_proc_start": ev_in.get("actual_proc_start"),
        "pid_authoritative": ev_in.get("pid_authoritative"),
        "pid_identity_source": ev_in.get("pid_identity_source"),
        "pid_local": ev_in.get("pid_local"),
        "pid_local_start": ev_in.get("pid_local_start"),
        "pid_host": ev_in.get("pid_host"),
        "pid_host_start": ev_in.get("pid_host_start"),
        "pid_host_ns": ev_in.get("pid_host_ns"),
        "pid_ns": ev_in.get("pid_ns"),
        "pid_observer_ns": ev_in.get("pid_observer_ns"),
        "pid_host_proof": ev_in.get("pid_host_proof"),
        "pgid": ev_in.get("pgid"),
        "heartbeat": ev_in.get("heartbeat"),
        "terminal_observation": terminal,
        "observed_liveness": observed,
        "registry_transition": ev_in.get("registry_transition"),
        "progress_fingerprint": deterministic_progress_fingerprint(ev_in),
        "observed_at": now,
    }


def _is_unused(state, ev_in):
    """§2.2 — `unused` refines `idle` on the inactivity-history axis using the SAME
    tier-1 registry evidence (startedAt/updatedAt) that declared `idle`, so the
    "lower tier never beats higher tier" invariant holds.

    Hard guard: `busy` is NEVER narrowed to unused (that would cross axes), and
    tier-3 mtime alone can never mint an unused (registry evidence is mandatory).
    All three conditions must hold; any one absent → stay `idle` (tolerate).
    """
    if state != "idle" or ev_in.get("transcript"):
        return False
    act = ev_in.get("activity_ms")
    return act is not None and act <= UNUSED_ACTIVITY_MS


def classify_session(ev_in, now, stale_min=SESSION_STALE_MIN, key=None):
    """(state, evidence). ev_in = collected evidence, never a live probe (hermetic).

    Recognized keys: pid_alive, proc_start_match, orphan, interaction_wait, status,
    task_lifecycle, exec_child, mtime, transcript, started_at, updated_at, activity_ms,
    harness, pid, proc_start, fd_owner, is_worker.
    """
    def out(state, tier, source, rule):
        if key is None:
            return state, _evidence(state, tier, source, rule, ev_in,
                                    raw_status=ev_in.get("status"))
        state, tier, (source, rule), hyst = _settle(key, state, tier, now, source, rule)
        return state, _evidence(state, tier, source, rule, ev_in,
                                raw_status=ev_in.get("status"), hysteresis=hyst)

    # --- tier 2: existence axis terminates everything. A vanished process is not a
    # contradiction of the registry's activity claim — it ends the row. ---
    if not ev_in.get("pid_alive", True):
        return out("dead", 2, "proc", "pid not alive")
    if ev_in.get("proc_start_match") is False:
        # registry procStart != /proc/<pid>/stat field 22 → the pid was recycled; every
        # registry claim about it is about a DIFFERENT process → discard, fail closed.
        return out("dead", 2, "proc", "start-time mismatch (pid reuse) — registry evidence discarded")
    if ev_in.get("orphan"):
        return out("stale", 2, "proc", "orphan cwd (deleted worktree)")

    interaction_wait = ev_in.get("interaction_wait")
    if (
        isinstance(interaction_wait, dict)
        and interaction_wait.get("kind") in INTERACTION_KINDS
    ):
        source = interaction_wait.get("source")
        source = source if isinstance(source, str) and source else "unknown"
        return out(
            "blocked",
            2,
            "interaction:%s" % source,
            "unresolved %s wait" % interaction_wait["kind"],
        )

    status = ev_in.get("status")
    background_child = exec_child_is_background(
        ev_in.get("exec_child"), ev_in.get("updated_at"), now
    )
    st = _session_status_state(
        status, exec_child=ev_in.get("exec_child"), background_child=background_child
    )
    st_source, st_rule = _status_desc(
        status, st, ev_in.get("exec_child"), background_child=background_child
    )
    m = ev_in.get("mtime")

    lifecycle = ev_in.get("task_lifecycle")
    if ev_in.get("harness") == "codex" and st is None:
        if lifecycle == "task_started":
            return out("working", 2, "codex-lifecycle", "latest exact Codex task is started")
        if lifecycle in {"task_complete", "turn_aborted"}:
            return out("idle", 2, "codex-lifecycle", f"latest exact Codex task is {lifecycle}")

    if m is None:
        # No recency signal at all → lean on the registry, else idle.
        if st:
            if _is_unused(st, ev_in):
                return out("unused", 1, "claude-registry",
                           "idle refined to unused (no transcript, updatedAt≈startedAt)")
            return out(st, 1, st_source, st_rule)
        return out("idle", 3, "mtime", "no mtime and no registry status")

    age_min = (now - m) / 60.0
    if age_min > stale_min:
        # Inactivity-history axis (§2.2, same axis as unused): silent past the session
        # window. Preserves the pre-F-25 ordering — status never rescued a 48h-silent row —
        # EXCEPT the unused ghost shape: its mtime is frozen at spawn by definition, so the
        # window would auto-hide exactly the rows F-26 exists to surface. While the process
        # is alive an unused ghost stays `unused` (user decision 2026-07-15); death still
        # terminates it via the tier-2 existence axis above.
        if st and _is_unused(st, ev_in):
            return out("unused", 1, "claude-registry",
                       "unused exempt from the stale window (mtime frozen at spawn)")
        return out("stale", 3, "mtime", "no activity for > %d min" % stale_min)
    if st:
        if _is_unused(st, ev_in):
            return out("unused", 1, "claude-registry",
                       "idle refined to unused (no transcript, updatedAt≈startedAt)")
        return out(st, 1, st_source, st_rule)
    # codex/opencode expose no status field → recency heuristic (fresh write == working)
    if age_min * 60.0 < SESSION_WORK_SEC:
        return out("working", 3, "mtime", "activity within %ds" % SESSION_WORK_SEC)
    return out("idle", 3, "mtime", "no activity within %ds" % SESSION_WORK_SEC)


def _classify_plugin_queue_job(ev_in, raw, out):
    """F-50b — one plugin-queue row's state. `out` is classify_job's own settler.

    The plugin's explicit `status` is a tier-1 registry declaration, exactly like a jobs.log
    word. `running` is the only status whose activity axis needs process evidence: a verified
    pid (the job's own node worker, else the queue broker) is tier-2 proof, and with neither
    the row stays `working` as a tier-3 derivation so the render layer marks it `~`.
    """
    if raw == "running":
        verified = ev_in.get("pid_verified")
        if verified == "job":
            return out("working", 2, "plugin-queue",
                       "registry running and the job's own worker pid is alive")
        if verified == "broker":
            return out("working", 2, "plugin-queue",
                       "registry running and the queue broker pid is alive")
        return out("working", 3, "plugin-queue",
                   "registry running, no pid could be verified")
    mapped = PLUGIN_QUEUE_STATES.get(raw)
    if mapped:
        return out(mapped, 1, "plugin-queue", "plugin state.json status=%s" % raw)
    # Unknown third-party word: report the absence, never a guess (F-50a).
    return out("unknown", 1, "plugin-queue",
               "plugin status outside the known vocabulary (raw preserved)")


def classify_job(ev_in, now, key=None):
    """(state, evidence) for a dispatch job. ev_in keys: source, key, is_loop, harness,
    status (raw jobs.log word), elapsed_min, transcript (tier-3 signal string), proc_liveness.
    """
    raw = ev_in.get("status")

    def out(state, tier, source, rule):
        if key is None:
            return state, _evidence(state, tier, source, rule, ev_in, raw_status=raw)
        state, tier, (source, rule), hyst = _settle(key, state, tier, now, source, rule)
        return state, _evidence(state, tier, source, rule, ev_in, raw_status=raw,
                                hysteresis=hyst)

    # F-50b: the plugin queue is a third-party registry with its own vocabulary and its own
    # evidence (a node worker pid / the broker pid), so it never reaches the jobs.log rules
    # below — but it is still decided HERE, by the one classifier.
    if ev_in.get("source") == "plugin-queue":
        return _classify_plugin_queue_job(ev_in, raw, out)

    # tier-2: a live loop process IS the evidence (dispatch.py:305 contract).
    if ev_in.get("source") == "proc" and ev_in.get("is_loop"):
        return out("working", 2, "proc", "loop process alive")

    # tier-1: terminal registry words. `killed`/`cancelled` stay unreachable through
    # collect() — dispatch.py drops those rows BEFORE classification. `done` IS reachable
    # since F-46 (v29): a done row inside the afterglow window survives the merge carrying
    # `afterglow=True`, and lands here to be settled as the tier-1 `done` state. This is a
    # vocabulary contract so the render layer can never reinterpret a raw word.
    if raw == "done":
        return out("done", 1, "registry", "jobs.log status=done")
    if raw == "killed":
        return out("killed", 1, "registry", "jobs.log status=killed")
    if raw == "cancelled":
        # fleet's job vocabulary has no `cancelled`; `killed` carries the same
        # "terminated by outside intervention" axis. The distinction survives in raw_status.
        return out("killed", 1, "registry", "jobs.log status=cancelled → killed (raw preserved)")

    # tier-2: a registry row carrying the canonical (pid, start-time) identity must
    # never be revived by another retry's cwd-wide transcript activity.  A matching
    # process proves the attempt is still alive; a missing/reused process terminates
    # that exact attempt immediately.  Legacy rows without both identity halves keep
    # the mtime fallback below.
    exact = classify_attempt_evidence(ev_in, now)
    if exact:
        state, evidence = out(exact["state"], exact["tier"], exact["source"], exact["rule"])
        evidence["classifier_source"] = exact["classifier_source"]
        evidence["attempt"] = exact
        return state, evidence

    t = ev_in.get("transcript")
    if t == "unknown":
        state, tier, rule = "unknown", 3, "no cwd to resolve a transcript"
    elif t == "working":
        state, tier, rule = "working", 3, "transcript within %d min" % JOB_STALE_MIN
    elif t == "stale":
        state, tier, rule = "stale", 3, "transcript older than %d min" % JOB_STALE_MIN
    else:
        # transcript absent. tier-1 `open` × tier-3 absence cross-rule (F-15c).
        if (ev_in.get("source") == "jobs" and raw == "open"
                and (ev_in.get("elapsed_min") or 0) <= JOB_QUEUED_GRACE_MIN):
            state, tier, rule = "queued", 3, (
                "registry open with no transcript yet, within %d min grace" % JOB_QUEUED_GRACE_MIN)
        else:
            state, tier, rule = "dead", 3, "no transcript"

    # tier-2 override: F-18a drill correlation merged a live proc row's evidence onto
    # this canonical registry row. A live process outranks a mtime-derived verdict.
    pl = ev_in.get("proc_liveness")
    if pl in ("working", "idle") and state in ("queued", "stale", "dead", "unknown"):
        return out(pl, 2, "proc", "live correlated process (F-18a) outranks mtime derivation")
    return out(state, tier, "mtime", rule)
