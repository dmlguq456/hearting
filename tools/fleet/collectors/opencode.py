"""opencode enrichment — passive, read-only SQLite (01_tap_mechanics.md §3).

State lives in ~/.local/share/opencode/opencode.db (WAL; opened mode=ro). The `session` table
carries per-session model/cwd/cost/tokens live. argv has no session id, so pid↔session is
matched by /proc/cwd == session.directory; among sessions in that directory we take the most
recently updated top-level (parent_id IS NULL) session.

Structurally missing (render '—', PRD §2/§4): rate-limit (no column). effort = model.variant.
context% = last-request prompt size (input + cache.read + cache.write from the latest
assistant message's tokens object) / model context window, the window read from opencode's
own models.dev registry cache (~/.cache/opencode/models.json). The session-table column
tokens_input is a cumulative cost-side aggregate, NOT the current context size.
"""
import json
import os
import sqlite3
import time

from ..model import ContextEvidence, SubAgent
from .. import titles

_COLS = ("id, slug, agent, model, cost, tokens_input, tokens_output, tokens_reasoning, "
         "time_updated, parent_id")

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


def _query(cur, cwd):
    # prefer a top-level session; fall back to any session in the directory
    for extra in ("AND parent_id IS NULL ", ""):
        row = cur.execute(
            "SELECT %s FROM session WHERE directory=? %s"
            "ORDER BY time_updated DESC LIMIT 1" % (_COLS, extra),
            (cwd,),
        ).fetchone()
        if row:
            return row
    return None


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


def enrich(sess):
    db = _db()
    if not sess.cwd or not os.path.exists(db):
        return
    con = None
    last_ctx = None
    subagents = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1.0)
        row = _query(con.cursor(), sess.cwd)
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
    # rl_5h / rl_7d: opencode has no rate-limit column → left None ('—').
