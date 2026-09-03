"""collect_all() — backbone process scan → per-harness enrichment → liveness → dispatch jobs.

Assembled incrementally: procscan is the backbone (session existence). Enrichment,
liveness, and dispatch modules are imported defensively so a partial checkout / a failing
enricher never drops backbone rows (PRD §1: enrichment fills fields; it does not decide existence).
"""
import importlib
import os
import time as _time

from . import procscan


def _same_path(a, b):
    if not a or not b:
        return False
    return a == b or os.path.realpath(a) == os.path.realpath(b)


def _mark_dispatch_child_sessions(sessions, jobs):
    """Hide runtime session rows that are already represented by dispatch jobs.

    Claude exposes an env marker for child sessions at procscan time, but Codex/OpenCode
    headless runs only become identifiable after jobs.log is collected. Match active child
    jobs by runtime+cold cwd, while explicitly protecting the parent session cwd/id.
    """
    child_jobs = []
    for j in jobs:
        if not getattr(j, 'cwd', None) or not getattr(j, 'harness', None):
            continue
        # F-50c: a plugin-queue row is its job's ONLY visible surface, and its cwd is the
        # plugin workspace — reconciling it against a same-cwd session would reclassify an
        # unrelated interactive session as that job's child (misattribution, not enrichment).
        if getattr(j, 'source', None) == 'plugin-queue':
            continue
        if not (getattr(j, 'is_child', False) or getattr(j, 'parent_sid', None)
                or getattr(j, 'parent_cwd', None) or getattr(j, 'parent_slug', None)):
            continue
        child_jobs.append(j)
    if not child_jobs:
        return
    # A procscan-marked child is stronger evidence than cwd reconciliation. Once a
    # runtime child already represents a job in the same harness/cwd, do not let that
    # job's shared cwd reclassify interactive root sessions as children as well.
    represented = {
        (s.harness, os.path.realpath(s.cwd))
        for s in sessions
        if getattr(s, 'is_child', False) and getattr(s, 'cwd', None)
    }
    for s in sessions:
        if getattr(s, 'is_child', False) or getattr(s, 'app_server', False) or not getattr(s, 'cwd', None):
            continue
        for j in child_jobs:
            if s.harness != j.harness:
                continue
            if not _same_path(s.cwd, j.cwd):
                continue
            if (j.harness, os.path.realpath(j.cwd)) in represented:
                continue
            # L1 (F-80): a session with no observed session_id cannot be proven to differ
            # from j.parent_sid. Absence of proof is not proof of absence — fail-closed and
            # never reclassify it as this job's child, rather than reading the missing id
            # as "not the parent."
            if getattr(j, 'parent_sid', None) and not getattr(s, 'session_id', None):
                continue
            if j.parent_sid and s.session_id and j.parent_sid == s.session_id:
                continue
            if getattr(j, 'parent_cwd', None) and _same_path(s.cwd, j.parent_cwd):
                continue
            s.is_child = True
            break


def resolve_parent_edges(sessions, jobs):
    """F-80 L2a/L2b: resolve each dispatch job's parent-session edge exactly once.

    Sets ``j._parent_edge_sid`` (the sid to attach under, confirmed or grace-held, else
    None) and ``j._parent_edge_promoted_orphan`` (True only when this tick's verdict is a
    fresh project-level orphan promotion) on every job with ``is_child`` and a
    ``parent_sid``. render and any ``--json`` consumer read these instead of re-deriving
    orphan status from a display-filtered session list (C6/C13b) — a single decision point,
    not a parallel copy. Both ephemeral attributes follow the existing leading-underscore,
    non-dataclass-field convention (e.g. ``_runtime_session_id`` in dispatch.py) so they
    never leak into ``--json`` output on their own.
    """
    from .. import model
    sessions_by_sid = {s.session_id: s for s in sessions if getattr(s, "session_id", None)}
    for j in jobs:
        parent_sid = getattr(j, "parent_sid", None)
        if not (getattr(j, "is_child", False) and parent_sid):
            continue
        parent_session = sessions_by_sid.get(parent_sid)
        if parent_session is None:
            # Complete collector-side absence — cannot be told apart from a real registry
            # gap, so it gets grace like any other non-visible reason (F-80 L2c "완전 부재").
            parent_visible = None
            dead_evidence = False
        else:
            parent_visible = model.session_parent_visible(parent_session)
            dead_evidence = getattr(parent_session, "liveness", None) == "dead"
        edge_sid, promoted = model.parent_edge_resolve(
            j.slug, parent_sid, parent_visible, dead_evidence)
        j._parent_edge_sid = edge_sid
        j._parent_edge_promoted_orphan = promoted


def _adopt_child_titles(sessions, jobs):
    """Atomically associate title, NOW, context/exec, and sub-agents from one exact child."""
    children = [s for s in sessions if getattr(s, 'is_child', False)]
    by_identity = {}
    by_session_id = {}
    by_cwd = {}
    for child in children:
        identity = (getattr(child, 'harness', None), getattr(child, 'pid', None),
                    getattr(child, 'proc_start', None))
        if all(item is not None for item in identity):
            by_identity.setdefault(identity, []).append(child)
        if child.harness and child.session_id:
            by_session_id.setdefault((child.harness, child.session_id), []).append(child)
        if child.cwd and child.harness:
            by_cwd.setdefault((child.harness, os.path.realpath(child.cwd)), []).append(child)
    for job in jobs:
        # F-50c: a plugin-queue row already carries the plugin's own name/summary and has no
        # runtime child session of its own — there is nothing to adopt, and a cwd-wide match
        # would borrow another session's title.
        if getattr(job, 'source', None) == 'plugin-queue':
            continue
        source = None
        ambiguity = None
        identity = (getattr(job, 'harness', None), getattr(job, 'pid', None),
                    getattr(job, 'proc_start', None))
        if all(item is not None for item in identity):
            candidates = by_identity.get(identity, [])
            if len(candidates) == 1:
                source = candidates[0]
            elif len(candidates) > 1:
                ambiguity = "multiple-child-identity-candidates"
        runtime_sid = getattr(job, '_runtime_session_id', None)
        if source is None and ambiguity is None and runtime_sid and getattr(job, 'harness', None):
            candidates = by_session_id.get((job.harness, runtime_sid), [])
            if len(candidates) == 1:
                source = candidates[0]
            elif len(candidates) > 1:
                ambiguity = "multiple-child-session-id-candidates"
        has_exact_binding = all(item is not None for item in identity) or bool(runtime_sid)
        if (source is None and ambiguity is None and not has_exact_binding
                and getattr(job, 'cwd', None) and getattr(job, 'harness', None)):
            candidates = by_cwd.get((job.harness, os.path.realpath(job.cwd)), [])
            if len(candidates) == 1:
                source = candidates[0]
            elif len(candidates) > 1:
                ambiguity = "multiple-child-cwd-candidates"
        if source is None:
            if ambiguity:
                job.association_ambiguity = ambiguity
                # Refuse the ambiguous Session join, but retain an independently
                # attempt-owned fallback summary when dispatch already supplied one.
                if not getattr(job, "_summary_sid", None):
                    job.summary = None
                    job.summary_ts = None
            continue
        job._child_session_associated = True
        # Identity association alone is not enough to suppress the attempt-log
        # fallback.  `claude -p --no-session-persistence` exposes a session id and
        # process but no persistent transcript, so only a child with an actual
        # refresh input can own title/NOW generation.
        job._child_refresh_associated = bool(
            getattr(source, "_transcript_path", None)
            or getattr(source, "_refresh_source", None)
        )
        # Values cross the boundary as one association decision. Attempt-stream
        # telemetry/sub-agents already attached to the job are stronger and stay
        # authoritative.  This fallback never borrows from the parent session.
        if not getattr(job, 'title', None):
            job.title = getattr(source, 'title', None)
        if not getattr(job, 'summary', None):
            job.summary = getattr(source, 'summary', None)
            job.summary_ts = getattr(source, 'summary_ts', None)
        if getattr(job, 'subagents', None) is None:
            job.subagents = getattr(source, 'subagents', None)
        if not getattr(job, '_dispatch_context_owned', False):
            for field_name in (
                    'ctx_pct', 'active_context_tokens', 'context_window_tokens',
                    'session_input_tokens', 'session_cached_input_tokens',
                    'session_output_tokens', 'session_reasoning_output_tokens',
                    'session_total_tokens', 'exec_child', 'exec_tool'):
                setattr(job, field_name, getattr(source, field_name, None))
            job._context_evidence = getattr(source, '_context_evidence', None)
            job._dispatch_context_owned = True


def collect_all(harness_filter=None, jobs_path=None, usage="cache-only"):
    """Return (sessions, jobs).

    harness_filter: optional iterable of harness names (fleet + dispatch both honor it).
    jobs_path:      override for .dispatch/jobs.log (else env / default).
    usage:          only exact ``refresh`` may schedule a background usage fetch;
                    all other values are cache-only.
    """
    sessions = procscan.scan(harness_filter=harness_filter)

    # --- per-harness passive enrichment (each enricher self-resolves its home from env) ---
    enrichers = {}
    modules = {}
    for name in ("claude", "codex", "opencode"):
        try:
            mod = importlib.import_module("." + name, __package__)
            modules[name] = mod
            enrichers[name] = mod.enrich
        except Exception:
            pass
    # Reserve strong process-owned identities before any PID-ordered fallback.
    # Codex uses this to prevent one same-cwd rollout from labeling two TUIs.
    codex_tick = None
    try:
        prepare = getattr(modules.get("codex"), "prepare_tick", None)
        if prepare and any(s.harness == "codex" for s in sessions):
            codex_tick = prepare(sessions)
    except Exception:
        pass
    for s in sessions:
        fn = enrichers.get(s.harness)
        if fn:
            try:
                if s.harness == "codex" and codex_tick is not None:
                    fn(s, tick=codex_tick)
                else:
                    fn(s)
            except Exception:
                pass  # enrichment failure never removes the backbone row

    # Exact Fleet-owned decision/approval waits are additive enrichment. Run
    # after harness identity resolution and before the single liveness verdict.
    try:
        from . import interaction as _interaction
        interaction_now = _time.time()
        for s in sessions:
            try:
                _interaction.enrich(s, now=interaction_now)
            except Exception:
                pass
    except Exception:
        pass

    # F-100b: herdr attachment — one `herdr agent list` per snapshot, exact session-id
    # match; additive enrichment that never touches liveness or row existence.
    try:
        from . import herdr as _herdr
        _herdr.enrich(sessions)
    except Exception:
        pass
    # F-100c: steward flag — exact (harness, session_id) join on the ledger's markers.
    try:
        from . import steward as _steward
        _steward.enrich(sessions)
    except Exception:
        pass

    # F-51d: JSON telemetry projection — cache-only lookup (never schedules a background
    # `git rev-list`; the live render path's own ahead_behind() calls are what populate the
    # cache over ticks). Absence stays None (F-51d "absence is normal"), never synthesized 0.
    try:
        from .. import gitinfo
        for s in sessions:
            counts = gitinfo.cached_ahead_behind(getattr(s, "cwd", None))
            if counts:
                s.branch_ahead, s.branch_behind = counts
    except Exception:
        pass

    # --- account usage: cache snapshot on every path; only live may request refresh ---
    usage_meta = {}
    usage_snapshots = {}
    try:
        from . import usage_cache
        for harness in ("claude", "codex"):
            if harness_filter is not None and harness not in set(harness_filter):
                continue
            snap = usage_cache.account_usage(harness, usage=usage if usage == "refresh" else "cache-only")
            usage_snapshots[harness] = snap
            usage_meta[harness] = {k: snap.get(k) for k in ("freshness", "observed_at")}
            payload = snap.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            account_windows = payload.get("rl_windows")
            if account_windows is None:
                account_windows = payload.get("windows")
            for s in sessions:
                if s.harness != harness or (harness == "claude" and s.is_child):
                    continue
                for key in ("rl_5h", "rl_7d", "rl_ms"):
                    value = payload.get(key)
                    if value is not None and value != []:
                        setattr(s, key, value)
                if account_windows:
                    s.rl_windows = account_windows
                elif harness == "codex" and (
                        payload.get("rl_5h") is not None or payload.get("rl_7d") is not None):
                    # Account data is newer than rollout enrichment.  Do not let an old
                    # dynamic window (for example a former 5h primary) override its labels.
                    s.rl_windows = None
                if payload.get("rs_5h") or payload.get("rs_7d"):
                    s.rl_rs = (payload.get("rs_5h"), payload.get("rs_7d"))
                s._usage_freshness = snap.get("freshness")
                s._usage_observed_at = snap.get("observed_at")
    except Exception:
        usage_meta = {}
        usage_snapshots = {}
    collect_all.last_usage = usage_meta
    # Account quota belongs to the harness, not to an individual process row.  Keep the
    # complete cache snapshot available even when session cleanup leaves no visible row.
    collect_all.last_usage_snapshots = usage_snapshots

    # --- liveness → 4-state ---
    try:
        from . import liveness
        now = _time.time()
        for s in sessions:
            s.liveness = liveness.classify(s, now)
    except Exception:
        pass

    # --- dispatch section ---
    jobs = []
    try:
        from . import dispatch
        jobs = dispatch.collect(jobs_path=jobs_path, harness_filter=harness_filter)
    except Exception:
        jobs = []

    # F-50 (v33): the openai-codex plugin queue is a SEPARATE runtime surface (F-35e), so its
    # rows are appended, never merged or deduped against jobs.log attempts — no plugin job is
    # currently reachable through both. Defensive import like every other enricher: an absent
    # or broken plugin state tree costs the plugin rows, never the dispatch section.
    try:
        from . import codex_companion
        jobs = jobs + codex_companion.collect()
    except Exception:
        pass

    try:
        _mark_dispatch_child_sessions(sessions, jobs)
    except Exception:
        pass

    # F-80 L2a/L2b: a dispatch job's parent-session edge, resolved through the shared
    # parent_visible predicate + ParentEdgeTracker so --json and render consume one
    # decision (C6/C13b). Sessions/liveness are already settled above; jobs are already
    # collected, so every input this needs is final for this tick.
    try:
        resolve_parent_edges(sessions, jobs)
    except Exception:
        pass

    try:
        from ..projection import normalize_context, _evidence, _is_live
        now = _time.time()
        for session in sessions:
            # liveness is already classified above, so F-62's live exemption is decidable here.
            session.context, session._context_evidence = normalize_context(
                _evidence(session), now=now, live=_is_live(session))
    except Exception:
        pass

    try:
        _adopt_child_titles(sessions, jobs)
    except Exception:
        pass

    # v16: all surfaces receive one projection after evidence collection and association.
    try:
        from ..projection import attach_projections
        # F-28a's terminal-row node evidence (dispatch.py's _scan_route_nodes) is the only
        # place a route node's `done`/`failed` state survives once its live job has already
        # gone terminal — thread it through so the projection never regresses to "pending"
        # for a node the registry already resolved.
        attach_projections(sessions, jobs, artifact_root=os.environ.get("AGENT_ARTIFACT_ROOT"),
                           now=_time.time(),
                           node_evidence=getattr(dispatch.collect, "last_route_nodes", None),
                           degradations=getattr(dispatch.collect, "last_degradations", None))
    except Exception:
        # Projection failure is fail-closed at the row boundary, never a reason to drop data.
        from ..model import WorkProjection
        for entity in sessions + jobs:
            if getattr(entity, "work_projection", None) is None:
                entity.work_projection = WorkProjection(source="none", ambiguity="projection-error")

    # F-59: resource/lab jobs are a separate source and never enter dispatch
    # association, projections, or jobs.log counts.
    resource_jobs = []
    try:
        from . import resource_runs
        resource_jobs = resource_runs.collect()
    except Exception:
        resource_jobs = []
    collect_all.last_resource_jobs = resource_jobs
    collect_all.last_resource_malformed = getattr(
        resource_runs.collect, "last_malformed", 0) if "resource_runs" in locals() else 0

    # F-98: read-only peer-message ledger projection. Additive and fail-soft — a missing
    # or unreadable ledger must leave every Session field at its default so the rendered
    # snapshot is byte-identical to a pre-SD-122 board.
    peer = None
    try:
        from . import peer_messages
        peer = peer_messages.collect()
        by_key = (peer or {}).get("by_session") or {}
        for s in sessions:
            row = by_key.get((str(getattr(s, "harness", "") or "").lower(),
                              s.session_id)) if s.session_id else None
            if not row:
                continue
            s.peer_sent_1h = row.get("sent_1h", 0)
            s.peer_recv_1h = row.get("recv_1h", 0)
            s.peer_last_recv = row.get("last_recv")
    except Exception:
        peer = None
    collect_all.last_peer_messages = peer

    # F-25: drop cross-tick hysteresis entries for rows that no longer exist. Runs after
    # BOTH sessions and jobs are classified — sweeping earlier would evict live job keys.
    try:
        from .. import model
        model.tracker_sweep()
        model.parent_edge_sweep()
    except Exception:
        pass

    return sessions, jobs


collect_all.last_resource_jobs = []
collect_all.last_resource_malformed = 0
collect_all.last_usage = {}
collect_all.last_usage_snapshots = {}
collect_all.last_peer_messages = None
