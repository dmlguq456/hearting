"""Claude Code enrichment — passive, read-only (01_tap_mechanics.md §1).

Two on-disk sources per session:
  1. ~/.claude/sessions/<pid>.json  — native claude file: sessionId, status(idle/shell/busy),
     name, cwd. No model/tokens/rate-limit here.
  2. ~/.claude/.statusline/<sid>.json — per-session statusline tap (§5, written by
     statusline.sh). Full telemetry: model, effort, context%, 5h/7d rate limits, cost.
     Absent until §5 has run for that session → those cells stay '—' (graceful).

Liveness signal = newest transcript mtime (projects/<enc-cwd>/*.jsonl), falling back to
sessions/<pid>.json statusUpdatedAt.
"""
import datetime
import json
import os
import re

from . import procscan
from ..model import ContextEvidence, SubAgent


def _home():
    """Runtime config home (projects/sessions/.statusline) — Claude Code config dir."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _enc_cwd(cwd):
    # projects dir encoding: '/', '.', '_' → '-' (matches dispatch-liveness.sh sed).
    return "".join("-" if ch in "/._" else ch for ch in cwd)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _newest_transcript_path(home, cwd, sid):
    """Transcript path for liveness/title extraction: `<sid>.jsonl` when the session id
    is known, else the newest .jsonl in the project dir. Shared by mtime and ai-title
    lookups so both use the same resolved path (one os.listdir scan, not two).

    A known sid whose transcript is MISSING returns None instead of falling back:
    borrowing the newest neighbor .jsonl stamps another same-cwd session's fresh
    mtime/title onto this row (observed 2026-07-15: a 33h-old orphaned Orca-relay
    session rendered as just-active with a stolen title). mtime then degrades to
    the session file's statusUpdatedAt in enrich()."""
    if not cwd:
        return None
    proj = os.path.join(home, "projects", _enc_cwd(cwd))
    if sid:
        p = os.path.join(proj, sid + ".jsonl")
        return p if _mtime(p) is not None else None
    best, best_m = None, None
    try:
        for name in os.listdir(proj):
            if name.endswith(".jsonl"):
                p = os.path.join(proj, name)
                m = _mtime(p)
                if m is not None and (best_m is None or m > best_m):
                    best, best_m = p, m
    except OSError:
        pass
    return best


def _newest_transcript_mtime(home, cwd, sid):
    path = _newest_transcript_path(home, cwd, sid)
    return _mtime(path) if path else None


_TITLE_JUNK_RE = re.compile(r"^(new session\b|\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2})", re.IGNORECASE)


_TITLE_CACHE = {}   # path -> (mtime, size, title) — avoid rescanning an unchanged transcript every tick


def _tail_ai_title(path, chunk=8192, max_scan=None):
    """Last `ai-title` line's aiTitle value, scanning backward from EOF in growing
    windows (chunk, ×8 each step) until an ai-title line is seen or the whole file
    is covered. Long sessions keep appending messages after the title lines, so a
    fixed tail window misses them (observed titles can sit 31–100KB before EOF) —
    the growing scan keeps short-session cost at one small read while still
    reaching early titles. A transcript can carry several ai-title lines appended
    over the session's life (renamed/refined) — the last one wins. tolerant:
    malformed json lines are skipped, a missing/empty/placeholder ("New session …",
    bare ISO timestamp) title → None so the caller falls back to slug. Results are
    memoized per (mtime, size) so unchanged files are not re-read on every tick."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _TITLE_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    sz = st.st_size
    limit = sz if max_scan is None else min(sz, max_scan)
    window = chunk
    title = None
    found_line = False
    while True:
        start = max(0, sz - window)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read().decode("utf-8", "replace")
        except OSError:
            return None
        lines = data.splitlines()
        if start > 0 and lines:
            lines = lines[1:]                       # drop the partial first line
        for ln in lines:
            if '"ai-title"' not in ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if "aiTitle" not in d:
                continue
            found_line = True
            t = d.get("aiTitle")
            title = t.strip() if isinstance(t, str) and t.strip() else None
        if found_line or window >= limit:
            break
        window = min(window * 8, limit)
    if title and _TITLE_JUNK_RE.match(title):
        title = None
    _TITLE_CACHE[path] = (st.st_mtime, st.st_size, title)
    return title


_SUBAGENT_CACHE = {}   # path -> (mtime, size, [SubAgent,...]) — separate from _TITLE_CACHE;
                        # same (mtime, size) key pattern, independent invalidation.

# Async Agent lifecycle markers (see _tail_subagents docstring). The ack text and the
# notification tag are harness-emitted fixed phrases; ids are compared verbatim.
_ASYNC_LAUNCH_MARK = "Async agent launched successfully"
_ASYNC_AGENT_ID_RE = re.compile(r"agentId:\s*([A-Za-z0-9_-]+)")
_TASK_NOTIF_ID_RE = re.compile(r"<task-id>([A-Za-z0-9_-]+)</task-id>")


def _ts_to_epoch(ts):
    if not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _tail_subagents(path, chunk=8192, max_scan=None):
    """Sub-agent rows from Task/Agent tool_use/tool_result pairing (prd.md:292 Claude source,
    `isSidechain: true` marks the spawned sub-agent's own turns; the pairing itself lives
    on the tool_use/tool_result pair in the PARENT thread — current runtimes emit the
    tool_use name as "Agent"; "Task" is kept for older transcript compatibility). Grows backward exactly
    like `_tail_ai_title` — same window, same guarantee (R3-1): if a tool_use is inside the
    scanned window, any tool_result answering it is necessarily ALSO inside it (an
    append-only log only grows forward), so "unpaired tool_use" found here is structurally
    ACTIVE, never a scan-window artifact. The converse (a tool_result whose tool_use fell
    outside the window) is silently dropped — that pairing describes a COMPLETED sub-agent,
    which is hidden by default anyway (prd.md:293), so missing it costs nothing.

    ASYNC agents (user 2026-07-16: the active ● state never showed live): a background
    Agent launch answers its tool_use IMMEDIATELY with a launch acknowledgment
    ("Async agent launched successfully … agentId: <id>") while the agent keeps running —
    that ack is NOT completion. Such a call stays ACTIVE until a `<task-notification>`
    carrying the same id as `<task-id>` appears later in the transcript (the harness
    injects it when the agent stops). The R3-1 window guarantee carries over: the
    notification can only appear AFTER the tool_use, so it is inside any window that
    contains the tool_use.

    Tolerant by contract (F-3): any malformed line is skipped. Returns None only when the
    file itself cannot be read (honest "no source", prd.md:292 — never a guess).
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _SUBAGENT_CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    sz = st.st_size
    limit = sz if max_scan is None else min(sz, max_scan)
    window = chunk
    calls = {}      # tool_use_id -> SubAgent
    resolved = set()
    ended = {}      # tool_use_id -> epoch of the resolving tool_result (완료 시각)
    async_ids = {}  # tool_use_id -> background agentId (launch ack seen, not completion)
    notified = set()  # agentIds whose stop notification has appeared
    notif_at = {}   # agentId -> epoch of that stop notification
    while True:
        start = max(0, sz - window)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read().decode("utf-8", "replace")
        except OSError:
            return None
        lines = data.splitlines()
        if start > 0 and lines:
            lines = lines[1:]           # drop the partial first line
        for ln in lines:
            if "task-notification" in ln and "<task-id>" in ln:
                ids = _TASK_NOTIF_ID_RE.findall(ln)
                if ids:
                    notified.update(ids)
                    try:
                        ts = _ts_to_epoch(json.loads(ln).get("timestamp"))
                    except Exception:
                        ts = None
                    if ts:
                        for aid in ids:
                            notif_at[aid] = ts
            if "tool_use" not in ln and "tool_result" not in ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            msg = d.get("message") if isinstance(d, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use" and c.get("name") in ("Task", "Agent"):
                    tid = c.get("id")
                    if not tid or tid in calls:
                        continue
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    calls[tid] = SubAgent(agent_type=inp.get("subagent_type"), active=True,
                                          started_at=_ts_to_epoch(d.get("timestamp")),
                                          source="claude-sidechain")
                elif c.get("type") == "tool_result":
                    tid = c.get("tool_use_id")
                    if not tid:
                        continue
                    blob = c.get("content")
                    text = blob if isinstance(blob, str) else (json.dumps(blob) if blob else "")
                    if _ASYNC_LAUNCH_MARK in text:
                        m = _ASYNC_AGENT_ID_RE.search(text)
                        if m:
                            async_ids[tid] = m.group(1)   # launch ack ≠ completion
                            continue
                        # marker without a parseable id: untrackable — fall through to the
                        # pre-async pairing (completed) rather than showing active forever.
                    resolved.add(tid)
                    ts = _ts_to_epoch(d.get("timestamp"))
                    if ts:
                        ended[tid] = ts
        if window >= limit:
            break
        window = min(window * 8, limit)
    out = []
    for tid, sa in calls.items():
        aid = async_ids.get(tid)
        if aid is not None:
            sa.active = aid not in notified
            if not sa.active:
                sa.ended_at = notif_at.get(aid)
        elif tid in resolved:
            sa.active = False
            sa.ended_at = ended.get(tid)
        out.append(sa)
    _join_subagent_budget(path, calls)
    out.sort(key=lambda sa: sa.started_at or 0, reverse=True)
    _SUBAGENT_CACHE[path] = (st.st_mtime, st.st_size, out)
    return out


_SUBAGENT_BUDGET_CACHE = {}   # subagent jsonl path -> (mtime, size, (model, effort, last_ts))


def _subagent_budget(jsonl_path, chunk=65536):
    """Last assistant (model, effort, last-activity epoch) from one sub-agent
    transcript tail.

    The per-turn `effort` field and `message.model` sit on every assistant
    record, so the newest one in the tail window is the current budget. The
    third element is the transcript's mtime — its last activity, used as the
    completion-time fallback when the parent transcript carries no resolving
    timestamp. Any read/parse failure returns (None, None, None) — honest gap,
    never a guess."""
    try:
        st = os.stat(jsonl_path)
    except OSError:
        return (None, None, None)
    cached = _SUBAGENT_BUDGET_CACHE.get(jsonl_path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    detail = (None, None, st.st_mtime)
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, st.st_size - chunk))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return (None, None, None)
    for ln in reversed(lines):
        if '"assistant"' not in ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
            continue
        msg = d.get("message")
        if isinstance(msg, dict) and msg.get("model"):
            eff = d.get("effort")
            detail = (msg.get("model"), eff if isinstance(eff, str) else None,
                      st.st_mtime)
            break
    _SUBAGENT_BUDGET_CACHE[jsonl_path] = (st.st_mtime, st.st_size, detail)
    return detail


def _join_subagent_budget(path, calls):
    """Join `<session>/subagents/agent-*.meta.json` onto the tool_use pairing.

    meta.json carries the exact `toolUseId` of the spawning Agent/Task call, so
    the join is by identity, never inference. The sibling `.jsonl` supplies the
    resolved model and observed effort; the meta `model` alias is only a
    fallback when the transcript has no assistant turn yet. A missing
    subagents/ dir (older runtimes, no spawns) leaves every field None."""
    if not path.endswith(".jsonl"):
        return
    sub_dir = os.path.join(path[:-len(".jsonl")], "subagents")
    try:
        names = os.listdir(sub_dir)
    except OSError:
        return
    for name in names:
        if not name.endswith(".meta.json"):
            continue
        mpath = os.path.join(sub_dir, name)
        try:
            with open(mpath, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        sa = calls.get(meta.get("toolUseId"))
        if sa is None:
            continue
        model, effort, last_ts = _subagent_budget(mpath[:-len(".meta.json")] + ".jsonl")
        raw_alias = meta.get("model")
        sa.model = model or (raw_alias if isinstance(raw_alias, str) else None)
        sa.effort = effort
        if not sa.active and sa.ended_at is None and last_ts:
            sa.ended_at = last_ts     # 완료 시각 폴백: 자기 transcript의 마지막 활동


def _apply_statusline(sess, d):
    m = d.get("model") or {}
    sess.model = m.get("display_name") or m.get("id") or sess.model
    eff = (d.get("effort") or {}).get("level")
    if eff:
        sess.effort = eff
    cw = d.get("context_window") or {}
    up = cw.get("used_percentage")
    if isinstance(up, (int, float)):
        sess.ctx_pct = min(99, round(up))
    current = cw.get("current_usage") or {}
    if isinstance(current, dict):
        active_parts = [
            current.get("input_tokens"),
            current.get("cache_creation_input_tokens"),
            current.get("cache_read_input_tokens"),
        ]
        if any(isinstance(value, (int, float)) for value in active_parts):
            sess.active_context_tokens = int(sum(
                value for value in active_parts if isinstance(value, (int, float))))
    window = cw.get("context_window_size")
    if isinstance(window, (int, float)) and window > 0:
        sess.context_window_tokens = int(window)
    ti, to = cw.get("total_input_tokens"), cw.get("total_output_tokens")
    if isinstance(ti, (int, float)) or isinstance(to, (int, float)):
        sess.tokens = int((ti or 0) + (to or 0))
        sess.session_input_tokens = int(ti) if isinstance(ti, (int, float)) else None
        sess.session_output_tokens = int(to) if isinstance(to, (int, float)) else None
        sess.session_total_tokens = sess.tokens
    rl = d.get("rate_limits") or {}

    def pct(k):
        v = (rl.get(k) or {}).get("used_percentage")
        return round(v) if isinstance(v, (int, float)) else None

    p5, p7 = pct("five_hour"), pct("seven_day")
    if p5 is not None:
        sess.rl_5h = p5
    if p7 is not None:
        sess.rl_7d = p7
    # per-model buckets → rl_ms [["fable", 57], ...]. Two schema shapes (2.1.198 bundle):
    #  1. named siblings: seven_day_opus / seven_day_sonnet / seven_day_overage_included
    #     ("Fable 5 limit" label) / seven_day_oauth_apps — same {used_percentage} shape as 5h/7d
    #  2. model_scoped array: [{display_name:"Fable", utilization:0..1, resets_at:str}]
    ms = []
    for k, lbl in (("seven_day_opus", "opus"), ("seven_day_sonnet", "sonnet"),
                   ("seven_day_overage_included", "fable"), ("seven_day_oauth_apps", "apps")):
        v = pct(k)
        if v is not None:
            ms.append([lbl, v])
    for e in (rl.get("model_scoped") or []):
        if isinstance(e, dict) and isinstance(e.get("utilization"), (int, float)):
            lbl = (e.get("display_name") or "model").split()[0].lower()
            if not any(x[0] == lbl for x in ms):     # named bucket wins over a duplicate scoped row
                ms.append([lbl, round(e["utilization"] * 100)])
    if ms:
        sess.rl_ms = ms
    cost = d.get("cost") or {}
    cv = cost.get("total_cost_usd") if isinstance(cost, dict) else None
    if isinstance(cv, (int, float)):
        sess.cost = cv


def read_registry(pid, home=None):
    """~/.claude/sessions/<pid>.json → dict, or None.

    F-26 promotes this file from an incidental status lookup to a first-class tier-1
    source: it is the runtime declaring its own identity, name, and activity window.
    Tolerant by contract — a session file written milliseconds ago may not carry
    `status`/`updatedAt` yet, and a missing/corrupt file is simply silence (None).
    """
    try:
        with open(os.path.join(home or _home(), "sessions", "%d.json" % int(pid))) as f:
            d = json.load(f)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _ms_to_sec(v):
    """registry epoch-ms → epoch-sec; anything non-numeric (or bool) → None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v / 1000.0


def _apply_registry(sess, sj):
    """Load every tier-1 registry field onto the Session. Each key is independently
    optional: a fresh row carrying only pid/sessionId must not lose the ones it has."""
    sess.session_id = sj.get("sessionId") or sess.session_id
    sess.status = sj.get("status")                # idle | shell | busy | (absent → None)
    name = sj.get("name")
    if name:
        sess.slug = name                          # friendly name disambiguates same-cwd sessions
        sess.registry_name = name                 # explicit link in the name chain (F-26)
    kind = sj.get("kind")
    if isinstance(kind, str):
        sess.kind = kind
    ps = sj.get("procStart")
    if ps is not None and not isinstance(ps, bool):
        sess.registry_proc_start = str(ps)        # compared against /proc in the classifier
    sess.started_at = _ms_to_sec(sj.get("startedAt"))
    sess.updated_at = _ms_to_sec(sj.get("updatedAt"))


def _tap_sid_by_pid(home, pid, proc_start):
    """Tier-2 sid recovery from the per-session statusline tap (§5, F-25).

    The pid registry can vanish while its process lives (observed 2026-07-20: three
    long-lived teammate sessions lost `sessions/<pid>.json`, leaving their rows
    sid-less for hours). The tap keeps updating for every live interactive session
    and now carries the owning claude `pid` + `/proc` `proc_start` (statusline.sh),
    so a tap whose BOTH halves match this row's live process identity recovers the
    sid. proc_start absent or mismatched on either side → refuse: a recycled pid
    would misattribute a neighbor's whole identity (F-26 — misattribution is worse
    than absence). Newest matching tap wins; every failure path is silence."""
    if pid is None or not proc_start:
        return None
    sldir = os.path.join(home, ".statusline")
    try:
        names = os.listdir(sldir)
    except OSError:
        return None
    best_sid, best_m = None, None
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        path = os.path.join(sldir, name)
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("pid") is None:
            continue
        if str(d.get("pid")) != str(pid) or str(d.get("proc_start") or "") != str(proc_start):
            continue
        sid = d.get("session_id") or name[:-5]
        m = _mtime(path)
        if sid and m is not None and (best_m is None or m > best_m):
            best_sid, best_m = sid, m
    return best_sid


def enrich(sess):
    home = _home()

    # 1) native per-pid registry file — tier-1 source (F-26)
    sj = read_registry(sess.pid, home)
    if sj is not None:
        _apply_registry(sess, sj)

    # 1a) tap-based sid recovery — only when the registry stayed silent about the sid;
    # a present registry sessionId always wins the F-25 tier order over the tap.
    if not sess.session_id:
        recovered = _tap_sid_by_pid(home, sess.pid, sess.proc_start)
        if recovered:
            sess.session_id = recovered

    # 1b) L3 (F-80): a dispatch worker session has no statusline tap (only interactive
    # sessions write one), so the tap recovery above is structurally unreachable for it —
    # a lost registry row leaves it sid-less with no §5 fallback. Recover from the owning
    # process's own environment, the same CLAUDE_CODE_SESSION_ID a registered attempt
    # already trusts for the parent link (§4 R2). Tap failure only — never overrides a tap
    # hit — and read_environ() is /proc-scoped to the same uid, so a foreign process's
    # environ is simply unreadable rather than misattributed.
    if not sess.session_id and sess.pid is not None:
        env_sid = procscan.read_environ(sess.pid).get("CLAUDE_CODE_SESSION_ID")
        if env_sid:
            sess.session_id = env_sid

    # 2) per-session statusline tap (§5) — telemetry; absent → '—'
    sid = sess.session_id
    if sid:
        try:
            with open(os.path.join(home, ".statusline", sid + ".json")) as f:
                tj = json.load(f)
            if isinstance(tj, dict):
                _apply_statusline(sess, tj)
        except Exception:
            pass

    # 3) Liveness mtime and title. Priority: fresh sidecar, AI title, then slug.
    path = _newest_transcript_path(home, sess.cwd, sid)
    if path:
        sess._transcript_path = path              # ephemeral: live title scheduler, not --json
        sess._refresh_source = {"kind": "transcript", "harness": "claude",
                                "session_id": sid, "path": path,
                                "cursor_kind": "byte-offset-v1"}
    # Transcript presence is the §2.2 `unused` refinement input: a session that has NEVER
    # been prompted has no transcript at all. Ephemeral (leading underscore) — evidence for
    # the classifier, not a --json field.
    sess._has_transcript = bool(path)
    if path:
        subs = _tail_subagents(path)
        if subs is not None:
            sess.subagents = subs
    m = _mtime(path) if path else None
    if m is None and isinstance(sj, dict):
        su = sj.get("statusUpdatedAt") or sj.get("updatedAt")
        if isinstance(su, (int, float)):
            m = su / 1000.0                        # ms → s
            # This mtime is the registry's own clock, not real activity — a tier-3 stand-in.
            sess._mtime_from_registry = True
    sess.mtime = m
    if path and sess.ctx_pct is not None:
        try:
            st = os.stat(path)
            sequence = (st.st_mtime_ns, st.st_size)
            sess._context_evidence = ContextEvidence(
                used_pct=sess.ctx_pct, source="claude-transcript",
                sequence=sequence, source_head_sequence=sequence,
                observed_at=st.st_mtime, fresh_until=st.st_mtime + 900)
        except OSError:
            pass
    # 3a) A fresh neutral sidecar overrides the AI title; failures pass through safely.
    from fleet import titles                      # Deferred import; no cycle, standard library only.
    st = titles.fresh_title(sid, harness="claude") if sid else None
    # 3a') Exact-attempt fallback: a registered dispatch session (depth-1 owner or
    # depth-2 worker) runs no statusline producer, so its runtime sid has no
    # sidecar — the SD-95 dispatch summary owner writes under the attempt sid
    # instead. attempt_id is exact env/registry identity, never a cwd/pid guess.
    attempt_sid = titles.attempt_sid(sess.attempt_id)
    if not st and attempt_sid:
        st = titles.fresh_title(attempt_sid, harness="claude")
    if st:
        sess.title = st
    # 3b) F-14 ai-title fallback — own `<sid>.jsonl` only. A sid-less row's `path` is the
    # newest NEIGHBOR transcript (liveness heuristic), and adopting its ai-title stamps
    # another session's name onto this row (observed 2026-07-20: every registry-less
    # same-cwd row wore the incident session's Korean title). The name falls to registry
    # name → slug instead (F-26 chain); the mtime borrow above is deliberately unchanged.
    elif path and sid:
        t = _tail_ai_title(path)
        if t:
            sess.title = t
    # Otherwise render falls back from a missing title to the registry name, then the slug.
    # 3c) F-16/F-17 merge — live one-sentence subtitle from the same sidecar/haiku call.
    sess.summary, sess.summary_ts = (
        titles.fresh_summary_with_ts(sid, harness="claude") if sid else (None, None))
    if sess.summary is None and attempt_sid:
        sess.summary, sess.summary_ts = titles.fresh_summary_with_ts(
            attempt_sid, harness="claude")

    # 4) provenance (F-26) — resolved LAST, and only for a session that has no title. A titled
    # session already says what it is; "who launched this?" is only an open question for a row
    # with no self-description (the never-prompted ghost being the motivating case). Gating on
    # title also keeps the tag off every ordinary row, where it would just eat the name zone.
    # Best-effort by contract: any failure leaves None and renders no tag (PRD F-26 —
    # misattribution is worse than absence).
    if sess.provenance is None and not sess.title:
        try:
            sess.provenance = procscan.provenance(sess.pid)
        except Exception:
            sess.provenance = None
