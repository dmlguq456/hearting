"""opencode enrichment — passive, read-only SQLite (01_tap_mechanics.md §3).

State lives in ~/.local/share/opencode/opencode.db (WAL; opened mode=ro). The `session` table
carries per-session model/cwd/cost/tokens live. argv has no session id, so pid↔session is
matched by /proc/cwd == session.directory, narrowed to the session THIS process created: the
earliest top-level (parent_id IS NULL) session in that directory whose time_created is at/after
the process start (/proc/<pid>/stat field 22 against /proc/stat btime — the same derivation
Codex already trusts). That narrowing matters because several opencode sessions can share one
directory: without it every process in a shared directory collapsed onto whichever session was
most recently updated, so the panes rendered as one. A process that attached to a pre-existing
session has no such candidate and takes the older binding (most recently updated top-level in
that directory) unchanged.

Structurally missing (render '—', PRD §2/§4): rate-limit (no column). effort = model.variant.
context% = last-request prompt size (input + cache.read + cache.write from the latest
assistant message's tokens object) / model context window, the window read from opencode's
own models.dev registry cache (~/.cache/opencode/models.json). The session-table column
tokens_input is a cumulative cost-side aggregate, NOT the current context size.
"""
import json
import os
import re
import sqlite3
import time

from ..model import ContextEvidence, SubAgent
from .. import session_registry
from .. import titles

_COLS = ("id, slug, agent, model, cost, tokens_input, tokens_output, tokens_reasoning, "
         "time_updated, parent_id")

# Variant tokens that name the runtime's own choice rather than a user-set dial. These are
# the same words render drops in `_EMPTY_EFFORT` (render.py) — mirrored here, not imported:
# collectors must not import render at module scope (see session_registry's import
# contract), so the two lists are kept in step by name and test rather than by dependency.
_DEFAULT_VARIANTS = frozenset({"default", "runtime-default", "inherit"})

_REG = {"ts": 0.0, "map": None, "by_provider": None}   # → context window (from models.json)
_REG_TTL = 300.0
# `part` first — the `message` table carries only per-message metadata (role/tokens/cost/
# modelID), never conversational text, so a refresh cursor anchored there advances over
# rows that can never produce a title or summary. Fixed order, no schema guessing.
_MESSAGE_TABLES = ("part", "message", "session_message")


def _attempt_sidecar_fallback(sess):
    """Exact-attempt sidecar fallback (SD-95): a registered dispatch session has
    no statusline producer for its runtime sid; the dispatch summary owner
    writes under the attempt sid. attempt_id is exact env/registry identity."""
    attempt_sid = titles.attempt_sid(getattr(sess, "attempt_id", None))
    if not attempt_sid:
        return
    if not getattr(sess, "title", None):
        sess.title = titles.fresh_title(attempt_sid, harness="opencode")
    if not getattr(sess, "summary", None):
        sess.summary, sess.summary_ts = titles.fresh_summary_with_ts(
            attempt_sid, harness="opencode")


def _message_table(con):
    """Choose one compatible table in fixed order; no schema guessing by recency."""
    for table in _MESSAGE_TABLES:
        try:
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row:
                con.execute("SELECT rowid FROM %s LIMIT 1" % table).fetchone()
                return table
        except Exception:
            continue
    return None


def _observed_cursor(con, table, sid):
    if not table:
        return None
    try:
        row = con.execute("SELECT MAX(rowid) FROM %s WHERE session_id=?" % table, (sid,)).fetchone()
        return int(row[0]) if row and isinstance(row[0], int) else 0
    except Exception:
        return None


def _load_model_registry():
    """Build (provider-scoped, provider-agnostic) context-window maps. Cached 5 min."""
    now = time.time()
    if _REG["map"] is not None and now - _REG["ts"] <= _REG_TTL:
        return _REG["by_provider"] or {}, _REG["map"] or {}
    scoped, flat = {}, {}
    path = os.environ.get("OPENCODE_MODELS") or os.path.expanduser("~/.cache/opencode/models.json")
    try:
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
        for pkey, prov in (reg.items() if isinstance(reg, dict) else []):
            models = prov.get("models") if isinstance(prov, dict) else None
            if not isinstance(models, dict):
                continue
            pid = (prov.get("id") if isinstance(prov, dict) else None) or pkey
            for mkey, mdef in models.items():
                lim = mdef.get("limit") if isinstance(mdef, dict) else None
                ctx = lim.get("context") if isinstance(lim, dict) else None
                if not isinstance(ctx, (int, float)) or ctx <= 0:
                    continue
                for k in (mkey, mkey.split("/")[-1]):   # bare id or provider/org-prefixed
                    scoped.setdefault((pid, k), int(ctx))
                    if flat.get(k, 0) < ctx:
                        flat[k] = int(ctx)
    except Exception:
        scoped, flat = {}, {}
    _REG.update(ts=now, map=flat, by_provider=scoped)
    return scoped, flat


def _model_ctx_limit(model_id, provider=None):
    """Context window for a model id, from opencode's models.dev registry cache
    (~/.cache/opencode/models.json — the same source opencode's own TUI uses for context%).
    None when unavailable → ctx% stays '—'. Cached 5 min.

    The same model id is published by many providers at different window sizes (one id
    in this registry spans a 48k spread across two providers), so the session's own
    providerID decides. The provider-agnostic max is only a last resort for an unknown
    provider — an over-large window understates ctx%, the safer direction to be wrong.
    """
    if not model_id:
        return None
    scoped, flat = _load_model_registry()
    leaf = model_id.split("/")[-1]
    if provider:
        for key in ((provider, model_id), (provider, leaf)):
            if key in scoped:
                return scoped[key]
    return flat.get(model_id) or flat.get(leaf)


def _db():
    return os.environ.get("OPENCODE_DB") or os.path.expanduser(
        "~/.local/share/opencode/opencode.db")


def _process_started_ms(sess):
    """This process's start as epoch MILLIseconds, or None when unknown.

    ``Session.proc_start`` is ``/proc/<pid>/stat`` field 22 — clock ticks since boot.
    ``/proc/stat``'s ``btime`` is that same boot epoch in seconds, so the pair converts
    exactly; the same derivation Codex already trusts for its own process-start match
    (``collectors/codex.py`` ``_process_started_at``). Missing/non-Linux/unreadable
    evidence stays None so the caller keeps the previous, weaker binding.
    """
    try:
        ticks = int(sess.proc_start)
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        if ticks < 0 or clock_ticks <= 0:
            return None
        with open("/proc/stat", encoding="ascii", errors="replace") as handle:
            boot = next(
                int(line.split()[1]) for line in handle
                if line.startswith("btime ")
            )
    except (AttributeError, OSError, StopIteration, TypeError, ValueError):
        return None
    return int((boot + ticks / clock_ticks) * 1000)


SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]{1,252}$")
TUI_SELECTION_SCHEMA = "hearting-tui-selection-v1"
TUI_SELECTION_MAX_BYTES = 1024
# Words that make an `opencode` command line something other than an interactive pane.
_NON_PANE_COMMANDS = frozenset(("run", "serve", "attach", "web", "auth", "agent", "models", "stats",
                                "export", "import", "session", "upgrade", "uninstall", "mcp", "acp",
                                "debug", "completion"))


def _proc_start_ticks(pid):
    try:
        with open("/proc/%d/stat" % int(pid), encoding="ascii", errors="replace") as handle:
            stat = handle.read()
        fields = stat[stat.rindex(") ") + 2:].split()
        return fields[19] if fields[19].isdigit() else None
    except (OSError, ValueError, IndexError):
        return None


def _cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as handle:
            raw = handle.read(32769)
    except (OSError, ValueError):
        return None
    if len(raw) > 32768:
        return None
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _argv_session(argv):
    """The `--session`/`-s` id an `opencode` pane was started on, or None."""
    if not argv or os.path.basename(argv[0]) != "opencode":
        return None
    found = None
    for index, arg in enumerate(argv[1:], 1):
        if arg in _NON_PANE_COMMANDS:
            return None
        key, equal, value = arg.partition("=")
        if key not in ("--session", "-s"):
            continue
        if not equal:
            value = argv[index + 1] if index + 1 < len(argv) else ""
        if found is not None or not SESSION_ID_RE.match(value or ""):
            return None
        found = value
    return found


def _tui_selection(pid, start, environ=None):
    """The session the pane's own TUI says it shows (PR190 record), or None.

    The TUI writes `<state>/hearting/tui-identity/<pid>-<start>.json` for its own
    process and removes it on the home screen, so a record naming this exact pid
    and start time is the current selection."""
    env = os.environ if environ is None else environ
    home = env.get("HOME") or ""
    base = env.get("XDG_STATE_HOME") or (os.path.join(home, ".local", "state") if home else "")
    if not base or not start:
        return None
    path = os.path.join(base, "hearting", "tui-identity", "%d-%s.json" % (int(pid), start))
    try:
        with open(path, "rb") as handle:
            raw = handle.read(TUI_SELECTION_MAX_BYTES + 1)
        row = json.loads(raw.decode("utf-8")) if len(raw) <= TUI_SELECTION_MAX_BYTES else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(row, dict) or row.get("schema") != TUI_SELECTION_SCHEMA
            or row.get("pid") != int(pid) or str(row.get("start")) != str(start)
            or not SESSION_ID_RE.match(str(row.get("sessionID") or ""))):
        return None
    return row["sessionID"]


def session_of_process(pid, environ=None):
    """`(session_id, source)` an OpenCode process proves about itself, or `(None, "")`.

    Its TUI's own selection record first (it follows `/new` and session switches),
    then the `--session` it was started with. Never the database's newest session,
    which is a guess shared by every pane in the directory."""
    start = _proc_start_ticks(pid)
    if start is None:
        return None, ""
    selected = _tui_selection(pid, start, environ)
    if selected:
        return selected, "opencode-tui-selection"
    started_on = _argv_session(_cmdline(pid))
    if started_on and _proc_start_ticks(pid) == start:
        return started_on, "opencode-argv"
    return None, ""


def session_id_of_process(pid, environ=None):
    return session_of_process(pid, environ)[0]


def prepare_tick(sessions):
    """`{pid: next same-directory opencode start (ms) | None}` for this tick.

    A process owns the top-level sessions created between its own start and the next
    same-directory opencode process's start; that window is what separates panes that
    share a checkout without pinning a pane to the first session it ever opened.
    """
    starts = {}
    for sess in sessions:
        if getattr(sess, "harness", None) != "opencode" or not getattr(sess, "cwd", None):
            continue
        started = _process_started_ms(sess)
        if started is not None:
            starts.setdefault(sess.cwd, []).append((started, sess.pid))
    until = {}
    for rows in starts.values():
        rows.sort()
        for index, (_started, pid) in enumerate(rows):
            until[pid] = rows[index + 1][0] if index + 1 < len(rows) else None
    return until


def _keeper_key(sess):
    """Sort key for one member of a shared session id: a row its own process proved
    (its TUI selection record) first, then window-bound rows that created the
    session (exact ownership), then earliest process start (the creator
    started before any attacher or helper), then lowest pid. Missing evidence
    sorts last — never used as a fact."""
    kind = getattr(sess, "_opencode_bind_kind", None)
    try:
        started = int(getattr(sess, "proc_start", None))
    except (TypeError, ValueError):
        started = None
    try:
        pid = int(getattr(sess, "pid", None))
    except (TypeError, ValueError):
        pid = None
    return (
        {"process": 0, "window": 1}.get(kind, 2),
        started if started is not None and started >= 0 else float("inf"),
        pid if pid is not None else float("inf"),
    )


def _unbind_shadow(sess):
    """Return a duplicate-bound row to anonymous-process state.

    The process row stays (existence is the backbone's decision, PRD §1) but
    carries no session identity, so the exact herdr/steward/peer joins and the
    tag mint — all keyed on session_id — answer on the keeper only. mtime is
    kept: the transcript really is updating, and liveness must stay truthful
    about the live process. slug is restored to the scan-time cwd basename.
    """
    sess.session_id = None
    sess.session_tag = None
    cwd = getattr(sess, "cwd", None)
    sess.slug = os.path.basename(cwd.rstrip("/")) if cwd else None
    sess.title = None
    sess.summary = None
    sess.summary_ts = None
    sess.subagents = None
    sess.model = None
    sess.effort = None
    sess.effort_default = False
    sess.cost = None
    sess.tokens = None
    sess.session_input_tokens = None
    sess.session_output_tokens = None
    sess.session_reasoning_output_tokens = None
    sess.session_total_tokens = None
    sess.active_context_tokens = None
    sess.context_window_tokens = None
    sess.ctx_pct = None
    sess._context_evidence = None
    sess._refresh_source = None
    sess._opencode_bind_kind = None


def collapse_duplicate_sids(sessions):
    """One session id → one row.

    Measured 2026-10-05: a session leader plus its arg-less helper child bound
    the same sid through the directory fallback and rendered as two identical
    working rows (issue #158). The 2026-09-29 window binding only separates
    processes that each created a session; every other process in the directory
    shares the one fallback guess, so the losers must be unbound here, after
    all bindings are known — a per-row claim inside enrich cannot do this,
    because a lower-pid fallback row enriches before the higher-pid process
    that actually owns the session through its window.
    """
    groups = {}
    for sess in sessions:
        if getattr(sess, "harness", None) != "opencode":
            continue
        sid = getattr(sess, "session_id", None)
        if not sid:
            continue
        groups.setdefault(sid, []).append(sess)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=_keeper_key)
        for loser in members[1:]:
            _unbind_shadow(loser)


def _query_window(cur, cwd, proc_start_ms, until_ms=None):
    """The exact branch: the top-level session this process created.

    Most recently updated among the sessions created inside this process's
    window [own start, next same-directory start). Returns None when this
    process created nothing (attached to a pre-existing session, or its own
    session row does not exist yet) — the caller then falls back.
    """
    bound = "AND time_created<? " if until_ms is not None else ""
    args = (cwd, proc_start_ms) + ((until_ms,) if until_ms is not None else ())
    return cur.execute(
        "SELECT %s FROM session WHERE directory=? AND parent_id IS NULL "
        "AND time_created>=? %sORDER BY time_updated DESC LIMIT 1" % (_COLS, bound),
        args,
    ).fetchone()


def _query_fallback(cur, cwd):
    """The guess branch: most recently updated top-level session in the
    directory, else any session there. Shared by every process that created
    nothing itself — which is exactly why two such processes can land on one
    session id (see collapse_duplicate_sids)."""
    for extra in ("AND parent_id IS NULL ", ""):
        row = cur.execute(
            "SELECT %s FROM session WHERE directory=? %s"
            "ORDER BY time_updated DESC LIMIT 1" % (_COLS, extra),
            (cwd,),
        ).fetchone()
        if row:
            return row
    return None


def _query(cur, cwd, proc_start_ms=None, until_ms=None):
    # N opencode processes can share one directory, and one process can open several
    # sessions over its life (/new). Bind to the most recently updated top-level session
    # created inside this process's window [own start, next same-directory start): the
    # window keeps panes apart, and "most recently updated" follows the session the pane
    # is on now instead of the first one it ever created. A process that attached to a
    # pre-existing session has no candidate here and keeps the older binding below.
    if proc_start_ms is not None:
        row = _query_window(cur, cwd, proc_start_ms, until_ms)
        if row:
            return row
    # prefer a top-level session; fall back to any session in the directory
    return _query_fallback(cur, cwd)


def _context_tokens_from_payload(payload):
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    total = 0
    for value in (tokens.get("input"), cache.get("read"), cache.get("write")):
        if isinstance(value, (int, float)):
            total += value
    return total or None


def _last_request_context(con, sid):
    """Latest assistant step prompt size, excluding output/cumulative session totals."""
    for table in ("message", "part", "session_message"):
        try:
            rows = con.execute(
                "SELECT data FROM %s WHERE session_id=? ORDER BY time_updated DESC LIMIT 50" % table,
                (sid,),
            )
        except Exception:
            continue
        for (data,) in rows:
            try:
                payload = json.loads(data) or {}
            except Exception:
                continue
            ctx = _context_tokens_from_payload(payload)
            if ctx:
                return ctx
    return None


def _child_sessions(con, sid):
    """F-29 (v9, prd.md:292 source #1 — already SELECTing agent/parent_id, previously
    discarded at the `_query` filter). None on any read failure (honest gap, not a guess);
    [] when the query succeeds and finds no children.

    No completion signal exists in this schema (unlike claude's tool_use/tool_result
    pairing) — every row found here is reported active=True; that is not a guess, it is
    the absence of evidence to the contrary, and the absence renders as '—'-adjacent
    (nothing hidden) rather than a fabricated 'done'.
    """
    try:
        rows = con.execute(
            "SELECT id, agent, time_updated FROM session WHERE parent_id=? "
            "ORDER BY time_updated DESC", (sid,),
        ).fetchall()
    except Exception:
        return None
    out = []
    for _cid, agent, tupd in rows:
        out.append(SubAgent(agent_type=agent or None, active=True,
                            started_at=(tupd / 1000.0) if isinstance(tupd, (int, float))
                                      else None,
                            source="opencode-db"))
    return out


def enrich(sess, tick=None):
    # B-5: no hearting-managed writer exists yet for OpenCode (`writer_support ==
    # "not-implemented"`), so this always reads None today — the call is here so the
    # harness-parity contract (D) and a future writer both have one call site, matching
    # claude/codex's tier-1 read at the top of their own `enrich`.
    try:
        rec = session_registry.read("opencode", sess.pid)
        if rec:
            session_registry.apply_to_session(sess, rec, "opencode")
    except Exception:
        pass
    db = _db()
    if not sess.cwd or not os.path.exists(db):
        return
    con = None
    last_ctx = None
    subagents = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1.0)
        cur = con.cursor()
        start_ms = _process_started_ms(sess)
        row, sess._opencode_bind_kind = None, None
        proven, source = session_of_process(sess.pid) if getattr(sess, "pid", None) else (None, "")
        if proven and source == "opencode-tui-selection":
            row = cur.execute("SELECT %s FROM session WHERE id=? LIMIT 1" % _COLS, (proven,)).fetchone()
            if row:
                sess._opencode_bind_kind = "process"
        if row is None and start_ms is not None:
            row = _query_window(cur, sess.cwd, start_ms, (tick or {}).get(sess.pid))
            if row:
                sess._opencode_bind_kind = "window"
        if row is None:
            row = _query_fallback(cur, sess.cwd)
            if row:
                sess._opencode_bind_kind = "fallback"
        if row and row[0]:
            table = _message_table(con)
            cursor = _observed_cursor(con, table, row[0])
            sess._refresh_source = {
                "kind": "opencode-db", "harness": "opencode", "db_path": db,
                "session_id": row[0], "table": table,
                "cursor_kind": "opencode-rowid-v1:%s" % table if table else None,
                "observed_cursor": cursor,
            }
            last_ctx = _last_request_context(con, row[0])
            try:
                tr = con.execute(
                    "SELECT title FROM session WHERE id=? LIMIT 1", (row[0],)).fetchone()
                if tr and tr[0] and str(tr[0]).strip():
                    sess.title = str(tr[0]).strip()
            except Exception:
                pass   # older DB without a title column → title stays None (tolerant, F-3)
            # R3-2: only query children when `row` is genuinely top-level (parent_id IS NULL,
            # row[-1] here) — the `_query` fallback clause can hand back a CHILD session, and
            # querying ITS children would surface grandchildren under the wrong parent.
            if row[-1] is None:
                subagents = _child_sessions(con, row[0])
    except Exception:
        return
    finally:
        if con is not None:
            con.close()
    if not row:
        _attempt_sidecar_fallback(sess)
        return
    sid, slug, agent, model_j, cost, ti, to, tr, tupd, _parent = row
    sidecar_title = titles.fresh_title(sid, harness="opencode")
    sidecar_summary, sidecar_summary_ts = titles.fresh_summary_with_ts(
        sid, harness="opencode")
    if sidecar_title:
        sess.title = sidecar_title
    if sidecar_summary:
        sess.summary = sidecar_summary
        sess.summary_ts = sidecar_summary_ts
    _attempt_sidecar_fallback(sess)
    sess.subagents = subagents
    if sid:
        sess.session_id = sid
        # F-100b — OpenCode has no derived session name either (Q-3: sqlite title/slug
        # only), so the `[xx]` badge tag is minted from the session id the same way.
        from fleet.session_handle import minted_tag
        sess.session_tag = minted_tag(sid)
    if slug:
        sess.slug = slug
    provider = None
    if model_j:
        try:
            mj = json.loads(model_j) or {}
            sess.model = mj.get("id") or model_j
            provider = mj.get("providerID") or None
            # opencode reasoning effort = model JSON 'variant' (e.g. high/low) — user 2026-07-01
            if mj.get("variant"):
                # A real `default` is the runtime naming its own choice, not an unset
                # dial, so it is kept OUT of `effort` (render's `_EMPTY_EFFORT` treats
                # those words as carrying no information) and recorded separately —
                # otherwise "the runtime defaulted" and "we observed nothing" render
                # as the same blank.
                if str(mj["variant"]).strip().lower() in _DEFAULT_VARIANTS:
                    sess.effort_default = True
                else:
                    sess.effort = mj.get("variant")
        except Exception:
            sess.model = model_j
    if isinstance(cost, (int, float)):
        sess.cost = cost
    toks = sum(x for x in (ti, to, tr) if isinstance(x, (int, float)))
    sess.tokens = toks or None
    sess.session_input_tokens = int(ti) if isinstance(ti, (int, float)) else None
    sess.session_output_tokens = int(to) if isinstance(to, (int, float)) else None
    sess.session_reasoning_output_tokens = int(tr) if isinstance(tr, (int, float)) else None
    sess.session_total_tokens = toks or None
    # context% = current-context size (last API request's prompt ~ what the model
    # actually saw as context) / model window (registry). NOT session.tokens_input,
    # which is cumulative API input across all requests in the session — cost-side,
    # not context-side. The real last-request context size lives in the data JSON of
    # the latest assistant message: tokens.input + tokens.cache.read +
    # tokens.cache.write. Falls back to session.tokens_input only when per-message
    # tokens are unavailable.
    ctx_for_pct = last_ctx if last_ctx else (ti if isinstance(ti, (int, float)) else None)
    if isinstance(ctx_for_pct, (int, float)) and ctx_for_pct:
        sess.active_context_tokens = int(ctx_for_pct)
        lim = _model_ctx_limit(sess.model, provider)
        if lim:
            sess.context_window_tokens = int(lim)
            sess.ctx_pct = min(99, round(100 * ctx_for_pct / lim))
            sess._context_evidence = ContextEvidence(
                used_pct=sess.ctx_pct, source="opencode-db", sequence=(tupd or 0, sess._refresh_source.get("observed_cursor", 0) if sess._refresh_source else 0),
                source_head_sequence=(tupd or 0, sess._refresh_source.get("observed_cursor", 0) if sess._refresh_source else 0),
                observed_at=(tupd / 1000.0 if isinstance(tupd, (int, float)) else None),
                fresh_until=(tupd / 1000.0 + 86400 if isinstance(tupd, (int, float)) else None),
            )
    if isinstance(tupd, (int, float)):
        sess.mtime = tupd / 1000.0                  # ms → s
    # rl_5h / rl_7d: the Go plan account quota rides the usage cache (collectors
    # __init__ account-usage loop) — nothing per-session here stays None.
