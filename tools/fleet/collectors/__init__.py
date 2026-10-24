"""collect_all() — backbone process scan → per-harness enrichment → liveness → dispatch jobs.

Assembled incrementally: procscan is the backbone (session existence). Enrichment,
liveness, and dispatch modules are imported defensively so a partial checkout / a failing
enricher never drops backbone rows (PRD §1: enrichment fills fields; it does not decide existence).
"""
import importlib
import copy
from dataclasses import replace
import os
from pathlib import Path
import sys
import time as _time

from . import procscan
from ..refresh import LiveSnapshot


_JOB_OBSERVATIONS = {}
_OBSERVATION_SUCCESSES = {}
FIRST_FRAME_USAGE_WAIT = 5.0


def _observation_health(key, source, error=None):
    """The same health vocabulary as RefreshPump, bound to this publication."""
    now = _time.monotonic()
    cache_key = (key, source)
    if error is None:
        _OBSERVATION_SUCCESSES[cache_key] = now
        if len(_OBSERVATION_SUCCESSES) > 32:
            _OBSERVATION_SUCCESSES.pop(next(iter(_OBSERVATION_SUCCESSES)))
    success = _OBSERVATION_SUCCESSES.get(cache_key)
    return {"state": "failed" if error else "idle", "last_error": error,
            "last_success_at": success, "age": None if success is None else now - success}


def _same_path(a, b):
    if not a or not b:
        return False
    return a == b or os.path.realpath(a) == os.path.realpath(b)


def _effective_job_parent(job):
    """Display parent from the shared handover interpreter; never rewrite the row."""
    registered = getattr(job, "parent_sid", None)
    metadata, registry = getattr(job, "_registry_metadata", None), getattr(job, "_registry_path", None)
    if not isinstance(metadata, dict) or not registry:
        return registered, ""
    try:
        import sys
        from pathlib import Path
        utilities = str(Path(__file__).resolve().parents[3] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        from dispatch_seat_handover import effective_parent, effective_parent_harness
        return (effective_parent(metadata, jobs=registry) or registered,
                effective_parent_harness(metadata, jobs=registry))
    except ImportError:
        return registered, ""


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
            # A cwd-only dispatch record cannot identify an anonymous/root session:
            # absent parent identity is missing evidence, not proof that this session
            # is the runtime child. Direct procscan child markers and exact session
            # joins elsewhere remain the stronger association evidence.
            if not getattr(j, 'parent_sid', None):
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
            parent_sid, _parent_harness = _effective_job_parent(j)
            if parent_sid and s.session_id and parent_sid == s.session_id:
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
    sessions_by_sid = {}
    for session in sessions:
        if getattr(session, "session_id", None):
            sessions_by_sid.setdefault(session.session_id, []).append(session)
    managed_parents = model.unique_managed_parents(sessions)
    for j in jobs:
        parent_sid = getattr(j, "parent_sid", None)
        if not parent_sid and getattr(j, "caller_sid", None):
            # Caller identity is observational: an exact SID or a verified session
            # continuation may link it. A pane/cwd alone never selects another user.
            caller_sid, harness = j.caller_sid, getattr(j, "caller_harness", None)
            candidates = [s for s in sessions if harness == s.harness
                          and caller_sid in [s.session_id, *(getattr(s, "session_aliases", None) or [])]
                          and model.session_parent_visible(s) and not getattr(s, "is_child", False)]
            j._parent_edge_sid = candidates[0].session_id if len(candidates) == 1 else None
            j._parent_edge_promoted_orphan = False
            continue
        if not (getattr(j, "is_child", False) and parent_sid):
            continue
        parent_sid, parent_harness = _effective_job_parent(j)
        candidates = sessions_by_sid.get(parent_sid, [])
        if parent_harness:
            candidates = [s for s in candidates if s.harness == parent_harness]
        visible = [s for s in candidates if model.session_parent_visible(s)
                   and not getattr(s, "is_child", False)]
        parent_session = (visible[0] if len(visible) == 1 else
                          candidates[0] if len(candidates) == 1 else None)
        resolved_sid = parent_sid
        directory = getattr(j, "parent_managed_dir", None)
        # A managed app-server can know the current fork while its visible TUI
        # still carries the original thread. Resolve the exact client here,
        # before grace/orphan classification, not later in the renderer.
        hidden_companion = (parent_session is not None
                            and getattr(parent_session, "app_server", False)
                            and getattr(parent_session, "managed_dir", None) == directory
                            and getattr(parent_session, "liveness", None) != "dead")
        if (not visible and (not candidates or hidden_companion) and directory
                and getattr(j, "source", None) != "plugin-queue"):
            client = managed_parents.get(os.path.normpath(directory))
            if client is not None and getattr(client, "session_id", None):
                parent_session = client
                resolved_sid = client.session_id
        if parent_session is None:
            # Complete collector-side absence — cannot be told apart from a real registry
            # gap, so it gets grace like any other non-visible reason (F-80 L2c "완전 부재").
            parent_visible = None
            dead_evidence = False
        else:
            parent_visible = model.session_parent_visible(parent_session)
            dead_evidence = getattr(parent_session, "liveness", None) == "dead"
        edge_sid, promoted = model.parent_edge_resolve(
            j.slug, parent_sid, parent_visible, dead_evidence, display_sid=resolved_sid)
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
        source_attempt = getattr(source, "attempt_id", None)
        job_attempt = getattr(job, "attempt_id", None)
        source_sid = getattr(source, "session_id", None)
        runtime_sid = getattr(job, "_runtime_session_id", None)
        if (source_sid and (runtime_sid is None or runtime_sid == source_sid)
                and source_attempt == job_attempt):
            from . import shell_jobs
            job.shell_jobs = shell_jobs.merge_jobs(
                getattr(job, "shell_jobs", None), getattr(source, "shell_jobs", None))
            current_observation = getattr(job, "shell_job_observation", None)
            if (not isinstance(current_observation, dict)
                    or current_observation.get("state") == "unavailable"):
                job.shell_job_observation = getattr(source, "shell_job_observation", None)
        if not getattr(job, '_dispatch_context_owned', False):
            for field_name in (
                    'ctx_pct', 'active_context_tokens', 'context_window_tokens',
                    'context_display_window_tokens', 'context_policy',
                    'session_input_tokens', 'session_cached_input_tokens',
                    'session_output_tokens', 'session_reasoning_output_tokens',
                    'session_total_tokens', 'exec_tool'):
                setattr(job, field_name, getattr(source, field_name, None))
            job._context_evidence = getattr(source, '_context_evidence', None)
            job._dispatch_context_owned = True


def apply_session_aliases(sessions):
    """Attach each row's prior session ids (resume/fork), display-join use only.

    Must run before the peer and steward joins — both key on `(harness, session_id)` and
    both can be addressed by an id the session no longer uses.
    """
    from .. import session_registry
    live_ids = {(str(getattr(s, "harness", "") or "").lower(), s.session_id)
                for s in sessions if getattr(s, "session_id", None)}
    for s in sessions:
        harness = str(getattr(s, "harness", "") or "").lower()
        if not getattr(s, "pid", None) or harness not in session_registry.HARNESSES:
            continue
        try:
            aliases = session_registry.session_aliases(harness, s.pid)
        except Exception:
            continue
        # A `--fork-session` parent can still be running under its own row. Adopting its
        # id here would make two rows answer to one key and hand the parent's messages to
        # the child, so an id that is still someone's current identity is never an alias.
        aliases = [a for a in aliases if (harness, a) not in live_ids]
        existing = getattr(s, "session_aliases", None) or []
        merged = list(dict.fromkeys(
            alias for alias in (*existing, *aliases) if (harness, alias) not in live_ids
        ))
        if merged:
            s.session_aliases = merged


def apply_peer_rows(sessions, by_key):
    """Project the ledger's per-session rows onto the Session objects.

    Joins over every id a row answers to, not just its current one: a resumed session's
    receipts are split across its old and new id, and only the older key carries the
    sender identity (measured 2026-09-09 — the newer key held a bare `notice` whose
    `from.session_id` was empty). Counters sum; each "last" takes the freshest entry
    (`age_min` counts minutes since, so smaller is newer).
    """
    from .. import session_registry
    for s in sessions:
        if not getattr(s, "session_id", None):
            continue
        rows = [by_key[key] for key in session_registry.session_join_keys(s) if key in by_key]
        if rows:
            s.peer_sent_1h = sum(row.get("sent_1h", 0) for row in rows)
            s.peer_recv_1h = sum(row.get("recv_1h", 0) for row in rows)
        def _age(entry):
            # A missing or non-numeric age must not win the "freshest" comparison — `or 0`
            # would rank an unparseable entry ahead of every real one.
            age = entry.get("age_min")
            return age if isinstance(age, (int, float)) and not isinstance(age, bool) else float("inf")

        for field, attr in (("last_recv", "peer_last_recv"), ("last_sent", "peer_last_sent")):
            entries = [row[field] for row in rows if row.get(field)]
            if entries:
                setattr(s, attr, min(entries, key=_age))
        duties = [entry for row in rows for entry in row.get("pending_obligations", [])]
        if duties:
            s.peer_obligations = sorted(
                duties, key=lambda item: (item.get("kind", ""), item.get("age_min", 0))
            )[:8]


def _collect_all(harness_filter=None, jobs_path=None, usage="cache-only", fast_first=False):
    """Return (sessions, jobs).

    harness_filter: optional iterable of harness names (fleet + dispatch both honor it).
    jobs_path:      override for .dispatch/jobs.log (else env / default).
    usage:          only exact ``refresh`` may schedule a background usage fetch;
                    all other values are cache-only.
    fast_first:     internal basic-live observation hint. Exact classification,
                    NOW and resource inputs stay attached; display details fill
                    independently. ``--once``/JSON keep the full pass.
    """
    job_key = (str(jobs_path or os.environ.get("AGENT_DISPATCH_JOBS") or ""),
               tuple(sorted(harness_filter or ())))
    observations = {}
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
            codex_tick = prepare(sessions, wait_timeout=0.0 if fast_first else 0.2)
    except Exception:
        pass
    for s in sessions:
        s._fast_first = bool(fast_first)
        fn = enrichers.get(s.harness)
        if s.harness == "claude":
            continue  # resolve once after the existing foreground pane observation
        if fn:
            try:
                if s.harness == "codex" and codex_tick is not None:
                    fn(s, tick=codex_tick)
                else:
                    fn(s)
            except Exception:
                pass  # enrichment failure never removes the backbone row
    # Managed Codex: the hidden app-server row shares the tag its TUI client minted,
    # joined on the exact managed state dir (additive; never touches an existing tag).
    try:
        share_tags = getattr(modules.get("codex"), "share_managed_tags", None)
        if share_tags and codex_tick is not None:
            share_tags(sessions)
    except Exception:
        pass
    # B-2c: server → client copy of tier-1 session_registry state (sessionId/status),
    # the same managed_dir pairing as share_tags above but the opposite direction.
    try:
        share_registry_state = getattr(modules.get("codex"), "share_managed_registry_state", None)
        if share_registry_state and codex_tick is not None:
            share_registry_state(sessions)
    except Exception:
        pass
    # One exact OpenCode session id → one row. If multiple process rows prove
    # the same current selection, keep the strongest binding before the exact
    # herdr/steward/peer joins below, which all key on session_id.
    try:
        collapse = getattr(modules.get("opencode"), "collapse_duplicate_sids", None)
        if collapse and any(s.harness == "opencode" and getattr(s, "session_id", None)
                            for s in sessions):
            collapse(sessions)
    except Exception:
        pass

    # F-100b: herdr attachment — one `herdr agent list` per snapshot, exact session-id
    # match; additive enrichment that never touches liveness or row existence.
    try:
        from . import herdr as _herdr
        observed = _herdr.enrich(sessions, wait_timeout=0.0 if fast_first else 0.2)
        if isinstance(observed, dict):
            observations["herdr"] = _observation_health(
                job_key, "herdr", observed.get("last_error") if not observed.get("complete") else None)
    except Exception as exc:
        observations["herdr"] = _observation_health(job_key, "herdr", "%s: %s" % (type(exc).__name__, exc))
    for s in sessions:
        if s.harness == "claude" and enrichers.get("claude"):
            try:
                enrichers["claude"](s, tick={s.pid: getattr(s, "_pane_session_claim", None)})
            except Exception:
                pass
    # Session directories describe work; the foreground observation above used
    # process launch directories. Do not change those matching inputs mid-pass.
    from .. import session_cwd
    session_cwd.project(sessions)
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

    # Resume/fork aliases must be resolved BEFORE the two ledger joins below, which both
    # key on `(harness, session_id)`: a resumed session's older receipts and any steward
    # marker written before the resume still name the id it used to have.
    try:
        apply_session_aliases(sessions)
    except Exception:
        pass

    # The journal wait joins after exact resume aliases and before the one liveness pass.
    try:
        from . import interaction as _interaction
        for failed_session, reason in _interaction.enrich_human_gates(sessions, jobs_path=jobs_path) or ():
            identity = "%s:%s" % (getattr(failed_session, "harness", "unknown"),
                                   getattr(failed_session, "session_id", "unknown"))
            observations["human-gates:" + identity] = _observation_health(
                identity, "human-gates", reason)
    except Exception as exc:
        observations["human-gates"] = _observation_health(
            job_key, "human-gates", "%s: %s" % (type(exc).__name__, str(exc)[:160]))

    # One shared live assignment pass; peer trailers and Herdr read the same snapshot.
    tag_metadata = []
    try:
        from ..session_tags import assign
        from ..session_handle import resolve_tag
        tag_metadata = assign(sessions)
        for s in sessions:
            if s.harness in {"codex", "opencode"} and s.session_id:
                s.session_tag = resolve_tag(s.harness, s.session_id)
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
        for harness in ("claude", "codex", "opencode"):
            if harness_filter is not None and harness not in set(harness_filter):
                continue
            snap = usage_cache.account_usage(harness, usage=usage if usage == "refresh" else "cache-only")
            usage_snapshots[harness] = snap
            usage_meta[harness] = {k: snap.get(k) for k in ("freshness", "observed_at")}
            freshness = snap.get("freshness")
            if freshness == "fresh":
                observations["usage:" + harness] = {"state": "ok"}
            elif usage == "refresh" and usage_cache.refresh_in_flight(
                    harness, max_age=FIRST_FRAME_USAGE_WAIT):
                observations["usage:" + harness] = {
                    "state": "pending", "reason": "usage-refresh-in-flight"}
            else:
                observations["usage:" + harness] = {
                    "state": "unavailable", "reason": "usage-not-fresh"}
            if harness == "opencode":
                try:
                    utilities = Path(__file__).resolve().parents[3] / "utilities"
                    if str(utilities) not in sys.path:
                        sys.path.insert(0, str(utilities))
                    import opencode_runtime
                    opencode_v2 = opencode_runtime.is_v2()
                    if opencode_v2:
                        selected = opencode_runtime.selected_go_usage_observation()
                        if selected.state == "unavailable":
                            go_scope = opencode_scope = entitlement_scope = None
                        else:
                            go_scope = selected.provider_scope or "unscoped"
                            opencode_scope = selected.opencode_scope or "unscoped"
                            entitlement_scope = selected.entitlement_scope
                    else:
                        go_observation = opencode_runtime.account_scope_observation("opencode-go")
                        opencode_observation = opencode_runtime.account_scope_observation("opencode")
                        go_scope = go_observation.scope
                        opencode_scope = opencode_observation.scope
                        entitlement_scope = opencode_scope
                    openai_observation = opencode_runtime.account_scope_observation("openai")
                    openai_scope = openai_observation.scope
                    scopes = {scope for scope in (go_scope, opencode_scope, openai_scope)
                              if scope is not None}
                    providers = usage_cache.provider_records("opencode", account_scopes=scopes)
                    snap["providers"] = providers
                    usage_meta[harness]["providers"] = providers
                    payload = snap.get("payload")
                    availability = payload.get("provider_availability") if isinstance(payload, dict) else None
                    if isinstance(availability, dict):
                        current_scope = {
                            "opencode-go": go_scope,
                            "opencode-go-entitlement": entitlement_scope,
                            "opencode": opencode_scope,
                            "openai": openai_scope,
                        }
                        payload["provider_availability"] = {
                            provider: row for provider, row in availability.items()
                            if isinstance(row, dict)
                            and row.get("account_scope") == current_scope.get(provider)
                        }
                    payload_scope = payload.get("account_scope") if isinstance(payload, dict) else None
                    if opencode_v2 and isinstance(payload, dict):
                        if not isinstance(payload_scope, str):
                            prior_scopes = {
                                row.get("account_scope")
                                for provider, row in (availability or {}).items()
                                if provider in {"opencode-go", "opencode-go-entitlement"}
                                and isinstance(row, dict)
                                and isinstance(row.get("account_scope"), str)
                            }
                            payload_scope = next(iter(prior_scopes)) if len(prior_scopes) == 1 else None
                    if (opencode_v2 and isinstance(payload, dict)
                            and (go_scope is None or payload_scope != go_scope)):
                        # The account can switch while the usage request is in
                        # flight. Keep its observation tagged to its source, but
                        # do not render its quota or denial as the new account's.
                        for key in ("rl_5h", "rl_7d", "rl_ms", "rs_5h", "rs_7d",
                                    "rl_windows", "windows", "error", "detail", "reason",
                                    "credential_integration_id", "credential_source",
                                    "credential_type"):
                            payload.pop(key, None)
                except Exception:
                    usage_meta[harness]["providers"] = []
            payload = snap.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if payload.get("error"):
                usage_meta[harness]["error"] = payload.get("error")
            account_windows = payload.get("rl_windows")
            if account_windows is None:
                account_windows = payload.get("windows")
            for s in sessions:
                if s.harness != harness or (harness == "claude" and s.is_child):
                    continue
                for key in ("rl_5h", "rl_7d", "rl_ms"):
                    value = payload.get(key)
                    # B-6/2: when the account snapshot lacks a key, the session field must
                    # go to None too — a missing account value means "unknown", not
                    # "keep whatever this session's stale rollout attribution left behind".
                    setattr(s, key, value if value not in (None, []) else None)
                if account_windows:
                    s.rl_windows = account_windows
                elif harness == "codex" and (
                        payload.get("rl_5h") is not None or payload.get("rl_7d") is not None):
                    # Account data is newer than rollout enrichment.  Do not let an old
                    # dynamic window (for example a former 5h primary) override its labels.
                    s.rl_windows = None
                if payload.get("rs_5h") or payload.get("rs_7d"):
                    s.rl_rs = (payload.get("rs_5h"), payload.get("rs_7d"))
                if payload.get("error"):
                    # Fetch-level failure class (e.g. opencode-go {"error": "auth"} —
                    # key present but rejected). Render shows the cause instead of a
                    # silently blank gauge.
                    s._usage_error = payload.get("error")
                s._usage_freshness = snap.get("freshness")
                s._usage_observed_at = snap.get("observed_at")
    except Exception:
        for harness in ("claude", "codex", "opencode"):
            observations.setdefault("usage:" + harness, {
                "state": "unavailable", "reason": "usage-collection-failed"})
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
    previous = _JOB_OBSERVATIONS.get(job_key)
    job_error = None
    node_evidence, degradations, pending_delivery, malformed = {}, {}, None, 0
    try:
        from . import dispatch
        jobs = dispatch.collect(jobs_path=jobs_path, harness_filter=harness_filter,
                                session_rows=sessions,
                                **({"fast_first": True} if fast_first else {}))
        # Capture the collector's output at its boundary, never in render or
        # from the independent detail worker. Older readers keep the attributes.
        node_evidence = copy.deepcopy(getattr(dispatch.collect, "last_route_nodes", {}) or {})
        degradations = copy.deepcopy(getattr(dispatch.collect, "last_degradations", {}) or {})
        pending_delivery = copy.deepcopy(getattr(dispatch.collect, "last_pending_delivery", None))
        malformed = getattr(dispatch.collect, "last_malformed", 0)
    except Exception as exc:
        job_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        if previous is not None:
            jobs = copy.deepcopy(previous.jobs)
            node_evidence = copy.deepcopy(previous.node_evidence)
            degradations = copy.deepcopy(previous.degradations)
            pending_delivery, malformed = previous.pending_delivery, previous.malformed
            for job in jobs:
                job._last_observed_liveness = job.liveness
                job.liveness = "unknown"
                job._observation_stale = True
    dispatch_rows = list(jobs)

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
        from ..process_identity import finalize_process_roles
        finalize_process_roles(sessions, jobs)
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
        # This joins already observed exact child fields without further IO.
        # NOW/context/exec must be available in the basic snapshot too.
        _adopt_child_titles(sessions, jobs)
        from .. import titles
        titles.annotate(sessions + jobs)
    except Exception:
        pass

    try:
        from .. import route_chain as _route_chain
        _route_chain.enrich(sessions, jobs=jobs,
                             node_evidence=node_evidence,
                             now=_time.time(), **({"fast_first": True} if fast_first else {}))
    except Exception:
        pass

    # Resources remain a separate source/count, but their exact parent edge feeds
    # the common projection before any group/process/JSON consumer sees the snapshot.
    resource_jobs = []
    try:
        from . import resource_runs
        resource_jobs = resource_runs.collect()
    except Exception:
        pass
    collect_all.last_resource_jobs = resource_jobs
    resource_diagnostics = list(getattr(
        resource_runs.collect, "last_diagnostics", [])) if "resource_runs" in locals() else []
    collect_all.last_resource_diagnostics = resource_diagnostics

    # v16: all surfaces receive one projection after evidence collection and association.
    try:
        from ..projection import attach_projections
        # F-28a's terminal-row node evidence (dispatch.py's _scan_route_nodes) is the only
        # place a route node's `done`/`failed` state survives once its live job has already
        # gone terminal — thread it through so the projection never regresses to "pending"
        # for a node the registry already resolved.
        attach_projections(sessions, jobs, artifact_root=os.environ.get("AGENT_ARTIFACT_ROOT"),
                           now=_time.time(),
                           node_evidence=node_evidence,
                           degradations=degradations,
                           resources=resource_jobs, **({"fast_first": True} if fast_first else {}))
    except Exception:
        # Projection failure is fail-closed at the row boundary, never a reason to drop data.
        from ..model import WorkProjection
        for entity in sessions + jobs:
            if getattr(entity, "work_projection", None) is None:
                entity.work_projection = WorkProjection(source="none", ambiguity="projection-error")

    if not fast_first:
        from ..work_titles import annotate
        annotate(sessions, jobs, resource_jobs)

    # F-98: read-only peer-message ledger projection. Additive and fail-soft — a missing
    # or unreadable ledger must leave every Session field at its default so the rendered
    # snapshot is byte-identical to a pre-SD-122 board.
    peer = None
    try:
        from . import peer_messages
        peer = None if fast_first else peer_messages.collect()
        apply_peer_rows(sessions, (peer or {}).get("by_session") or {})
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

    observations["jobs"] = _observation_health(job_key, "jobs", job_error)
    from ..display_snapshot import build as build_display_snapshot
    display_snapshot = build_display_snapshot(sessions, jobs, resource_jobs)
    snapshot = LiveSnapshot(
        sessions=sessions, jobs=jobs, resources=resource_jobs,
        usage_snapshots=usage_snapshots, node_evidence=node_evidence,
        degradations=degradations, pending_delivery=pending_delivery,
        malformed=malformed,
        resource_diagnostics=copy.deepcopy(resource_diagnostics),
        observations=observations, tag_metadata=copy.deepcopy(tag_metadata),
        display_snapshot=display_snapshot,
    )
    if not sessions and not jobs and node_evidence:
        from ..projection import terminal_route_entities
        snapshot.route_entities = terminal_route_entities(node_evidence, degradations)
    if job_error is None:
        # Cache projected rows, not a reference later mutated by another reader.
        _JOB_OBSERVATIONS[job_key] = copy.deepcopy(replace(
            snapshot, sessions=[], jobs=dispatch_rows, resources=[], usage_snapshots={}, tag_metadata=[]))
        if len(_JOB_OBSERVATIONS) > 16:
            _JOB_OBSERVATIONS.pop(next(iter(_JOB_OBSERVATIONS)))
    return snapshot


def collect_all(harness_filter=None, jobs_path=None, usage="cache-only", fast_first=False):
    """Run one Fleet observation with shared artifact, admission and registry inputs."""
    from contextlib import ExitStack
    from . import dispatch
    import artifact_producer
    import dispatch_registry_cache
    with ExitStack() as stack:
        if dispatch.artifact_reader is not None:
            stack.enter_context(dispatch.artifact_reader.read_scope(
                dispatch._ARTIFACT_READ_CACHE))
        stack.enter_context(artifact_producer.artifact_index_observation_scope())
        stack.enter_context(dispatch_registry_cache.registry_read_scope())
        return _collect_all(harness_filter, jobs_path, usage, fast_first)


collect_all.last_resource_jobs = []
collect_all.last_resource_diagnostics = []
collect_all.last_usage = {}
collect_all.last_usage_snapshots = {}
collect_all.last_peer_messages = None
