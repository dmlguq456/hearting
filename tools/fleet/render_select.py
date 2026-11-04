"""Fleet render: peer links, resource rows and other row pieces built from snapshots.

Split out of `render.py`, which re-exports every name defined here.
"""
import re
import time
from .display import label as _user_label
from .model import fmt_min
from .work_titles import (
    readable as _readable_name, resource_name as _resource_name, resource_title_key,
)
from .collectors import compute_hosts as _compute_hosts
from .session_handle import sanitize_title as _sanitize_session_title
from .session_handle import _cell_width as _session_handle_cell_width
from .session_handle import clip_cells as _clip_cells
from .session_handle import resolve_tag as _resolve_session_tag
from .session_handle import session_id_for_address as _session_id_for_address
from .session_handle import session_id_for_derived_name as _session_id_for_derived_name

from .render_cells import (
    _clip_w, _dw,
)

from .render_stages import (
    _aged_text, _is_plugin_agent, _session_name, _text_age,
)

from .render_rows import (
    _clip_segs, _project_for_display, _reserve_age_tail, _short_level,
)


def _selectable_session(s):
    """Kill-eligible session rows, per prd.md:253's two grades (and nothing else).

    Grade 1 (single confirm): unused / stale / dead — a row that is demonstrably not doing
    work. Plus an idle WORKER child (a leftover headless session).
    Grade 2 (warning + double confirm): working, or a registry that says busy.

    A plain interactive `idle` session is deliberately NOT selectable: it is somebody's live
    session sitting between prompts, and it appears in neither grade of the spec's list.
    """
    if getattr(s, "app_server", False):
        return False
    if s.liveness in ("unused", "stale", "dead", "working"):
        return True
    if s.liveness == "idle" and (getattr(s, "is_child", False)
                                 or getattr(s, "mem_worker", False)):
        return True
    return s.status == "busy"


def _select_entry(s, line_idx):
    return {"line": line_idx, "kind": "session", "pid": s.pid,
            "proc_start": getattr(s, "proc_start", None),
            "sid": s.session_id, "state": s.liveness,
            "status": s.status, "cwd": s.cwd, "slug": s.slug,
            "label": _session_name(s), "harness": s.harness, "source": None,
            "is_worker": bool(getattr(s, "is_child", False)
                              or getattr(s, "mem_worker", False))}


def _select_entry_job(j, line_idx):
    return {"line": line_idx, "kind": "job", "pid": j.pid,
            "proc_start": getattr(j, "proc_start", None),
            "sid": None, "state": j.liveness, "status": j.status,
            "cwd": j.cwd, "slug": j.slug,
            "attempt_id": getattr(j, "attempt_id", None),
            "label": j.slug or j.key, "harness": j.harness, "source": j.source,
            "is_worker": bool(getattr(j, "is_child", False))}
_FOLD_CHILD_LIVENESS = {"done"}   # Completed depth-2 rows fold into the owner breadcrumb.
_FOLD_CHILD_ROUTE_STATES = {"done", "reconciling"}
                                  # Queued/idle/unknown remain visible execution identities.


def _fold_completed_child(job):
    """Whether a depth-2 execution row has no state left beyond the route breadcrumb.

    A clean ``done`` row has always folded here.  An exact route projection can also establish
    that a stale/open registry row belongs to a completed node, or to a ``reconciling`` node
    whose worker emitted terminal evidence and exited while the owner has not published its
    completion marker yet.  Keep the route verdict (``check`` or yellow ``...``) on the
    breadcrumb, but do not retain a duplicate dim ``done`` worker row for the whole
    continuation.  Generic stale/dead rows never qualify -- they may be failures and must remain
    visible.
    """
    if getattr(job, "liveness", None) in _FOLD_CHILD_LIVENESS:
        return True
    projection = getattr(job, "work_projection", None)
    return (
        getattr(job, "liveness", None) == "stale"
        and getattr(projection, "source", None) == "route-exact"
        and getattr(projection, "node_state", None) in _FOLD_CHILD_ROUTE_STATES
    )


def _fold_finished_frame_child(job):
    """Whether one frame leg is finished (fold-candidate, not the fold decision).

    F-105 (backlog O2, user 2026-10-01): a completed frame branch (frame /
    frame-alternative) kept a full dispatch row on the parent session card,
    sitting next to the next round's live rows. Only genuinely finished legs
    qualify — a terminal `done` registry word, or a stale row the exact route
    projection settles as done / reconciling. Anything else (working, queued,
    unknown, or stale without route proof) may be live or failed work and is
    never a candidate. Non-frame rows (owners, stages) never qualify here; they
    keep their own cards. Whether a candidate actually folds is decided per
    route group by `_finished_frame_fold_ids`, and `a` / --all reveals folded
    legs, like every other fold.
    """
    if getattr(job, "worker_type", None) != "frame":
        return False
    if getattr(job, "liveness", None) in _FOLD_CHILD_LIVENESS:
        return True
    projection = getattr(job, "work_projection", None)
    return (
        getattr(job, "liveness", None) == "stale"
        and getattr(projection, "source", None) == "route-exact"
        and getattr(projection, "node_state", None) in _FOLD_CHILD_ROUTE_STATES
    )


def _finished_frame_fold_ids(kids):
    """Ids of frame legs to fold, grouped by route.

    A finished leg folds only together with its whole route group: while any
    leg of the same route still runs, the pair stays on screen (that is the
    phase the operator watches). Once every leg of the route is finished, the
    whole group folds at once — so a previous round never sits next to the
    next round's live rows, and an owner card never shares its card with stale
    frame boxes. A killed leg leaves no registry row, so an absent sibling
    never blocks the fold; a stale leg without route proof does (it may be a
    failure and keeps its context visible).
    """
    by_route = {}
    for kid in kids:
        if _is_plugin_agent(kid) or getattr(kid, "worker_type", None) != "frame":
            continue
        by_route.setdefault(getattr(kid, "route_id", None), []).append(kid)
    fold = set()
    for group in by_route.values():
        if group and all(_fold_finished_frame_child(kid) for kid in group):
            fold.update(id(kid) for kid in group)
    return fold
# The detached (owner-off-screen) variant of the fold above. Wider than in-card `done` on
# purpose: inside a card, `stale`/`dead` still read against their owner's context, but with
# the owner gone there is nothing to monitor and no card to belong to — done, stale and dead
# alike are finished residue. Queued/idle/unknown stay recoverable: they may still run.
_DETACHED_FINISHED_LIVENESS = {"done", "stale", "dead"}

# F-29 (v9, prd.md:290-295) — sub-agent observation rows. `⚡` is the PRD-specified glyph.
# Reads distinctly from dispatch's `🚀`/`↳` so the two nested-row kinds never visually merge.
# Single point of ASCII-degrade if double-width alignment ever breaks in a real terminal.
_ICON_SUBAGENT = "⚡"
_SUBAGENT_IND = "        "  # strip indent: pure inset, no connector glyph, 8 cells for a
                          # session-owned strip — at/past the dispatch-depth-2 arrow column so the
                          # strip reads as INSIDE the row above, never as a sibling (사용자
                          # 2026-07-16 "들여쓰기 레벨을 충분히 안쪽으로"; the 2-cell then 4-cell
                          # insets both read too shallow). 6→8 with the F-64 (v49) 3-cell dispatch
                          # ladder so the anchor keeps clearing the deeper arrows. Dispatch-owned
                          # strips add 2 more cells per depth (see _subagent_strip). The
                          # per-session ⚡N name-zone badge this used to pair with stays retired.


def _conn_indent(depth=0, in_card=False):
    """Shared pure inset for the ordered session connection strips."""
    shown_depth = min(depth, 1) if in_card else depth
    return _SUBAGENT_IND + "  " * max(0, shown_depth)


def _fit_strip(builders, width):
    """Pick the first bounded strip variant; preserve legacy unbounded calls."""
    if not width:
        return builders[0]()
    segs = None
    for build in builders:
        segs = build()
        if sum(_dw(text) for text, _key in segs) <= width:
            return segs
    return _clip_segs(_reserve_age_tail(segs, width), width)[0]


def _subagent_elapsed_min(sa):
    """Runtime minutes: an active entry keeps counting; a completed entry with an
    observed completion time STOPS there (사용자 2026-07-29 — before this, a done
    sub-agent's elapsed kept growing forever, conflating runtime with idle)."""
    started = getattr(sa, "started_at", None)
    if not started:
        return None
    end = getattr(sa, "ended_at", None) if not sa.active else None
    return max(0, int(((end or time.time()) - started) / 60))


def _subagent_idle_min(sa):
    """Minutes since a completed entry finished — the '잠든 시간' tail. None when
    active or when no completion time was observed (honest gap)."""
    ended = getattr(sa, "ended_at", None)
    if sa.active or not ended:
        return None
    return max(0, int((time.time() - ended) / 60))


# The icon says WHAT the relation is, the arrow says which WAY it points. User decision
# 2026-09-09 ("위 아래로 표시하면 앞뒤 화살표는 왜 있는거임?") — the two must not encode
# the same thing twice, so no glyph ever varies by direction.
_ICON_PEER = "✉"


_PEER_NAME_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


_PEER_NAME_PANE_RE = re.compile(r"^w[0-9A-Za-z]{1,3}:p[0-9A-Za-z]{1,3}$")
_PEER_NAME_HEX_RE = re.compile(r"^[0-9a-f]{12,}$", re.I)
_PEER_NAME_MAX_CELLS = 26


def _peer_name_is_readable(name):
    """True when the ledger's `name` is something a person can act on.

    The name slot is whatever the sending side wrote, and older send paths put machine
    addresses in it — the board printed `✉ → uds:/run/user/1000/fixture-sockets/1234.sock`
    from a real record. A transport address or a bare session id names nothing the user
    can look up, so it is worse than the `claude:0100000b` fallback: it is the same
    non-information, but longer and pretending to be a name.

    Deliberately a small denylist, not a guess at what a "real" name looks like: session
    names here are free-form and often Korean prose, so anything not recognizably a
    machine address has to pass.
    """
    if not isinstance(name, str):
        return False
    text = name.strip()
    if not text:
        return False
    if _PEER_NAME_UUID_RE.match(text) or _PEER_NAME_HEX_RE.match(text):
        return False                       # a raw id pasted into the name slot
    if _PEER_NAME_PANE_RE.match(text):
        return False                       # `wB:p5` — where it runs, not which session
    if text.startswith("/") or "://" in text:
        return False                       # a path or a URL
    scheme = text.split(":", 1)[0]
    if scheme and scheme.isascii() and scheme.isalpha() and ":/" in text:
        return False                       # uds:/…, unix:/…, and friends
    # Length is NOT a rejection: `fleet-stall-codex-parity-10` is a real name that happens
    # to be long, and a clipped real name beats `claude:a8e66aa4`. Only prose gets dropped
    # — a whole sentence in the name slot (a session title, from an older send path) is not
    # a name and clipping it just yields a truncated sentence.
    return " " not in text or _session_handle_cell_width(text) <= _PEER_NAME_MAX_CELLS


def _session_repo_label(session, display_snapshot=None):
    cwd = getattr(session, "cwd", None)
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return ""
    repo = _project_for_display(cwd, display_snapshot)
    return _sanitize_session_title(repo) if repo not in {"(unknown)", "(root)"} else ""


def _peer_endpoint_segs(harness, session_id, name, tag_by_key, repo_by_key=None):
    """Name the session at the other end of a relation, best evidence first.

    `hearting [46]` when that session's repository is observed — the badge is the thing
    the user can actually look up. Otherwise the ledger's own display name, and only then
    the short session ID. Never the full session id: `← 0100000b-0000-7000-8000-000000000013`
    is what the board printed before this (the collector used to paste the raw id into the
    name slot), and it names nothing a person can act on.
    """
    harness = str(harness or "").lower()
    sid = session_id if isinstance(session_id, str) and session_id else None
    if not sid:
        # Rows written before the ledger learned to resolve it keep the peer's socket
        # address in the name slot. The address names a PID, and the PID names a session,
        # so the badge is recoverable for history too — 222 such rows were on the board.
        try:
            sid = _session_id_for_address(name) or _session_id_for_derived_name(name)
        except Exception:
            sid = None
    tag = (tag_by_key or {}).get((harness, sid)) if sid else None
    if not tag and sid:
        # The badge is the whole point, so an off-board peer gets one too when the shared
        # record can still produce it. Without this the same session reads `[67] claude`
        # while it happens to be on screen and something else entirely once it scrolls off.
        try:
            tag = _resolve_session_tag(harness, sid)
        except Exception:
            tag = None
    repo = (repo_by_key or {}).get((harness, sid)) if sid else None
    if tag:
        segs = [("[", "dim"), (str(tag), "tag"), ("]", "dim")]
        if repo:
            segs.insert(0, (_clip_cells(repo, _PEER_NAME_MAX_CELLS) + " ", "dim"))
        return segs
    if _peer_name_is_readable(name):
        return [(_clip_cells(name.strip(), _PEER_NAME_MAX_CELLS), "dim")]
    if sid:
        return [((repo + ":" if repo else "") + sid[:8], "dim")]
    return [(harness, "dim")] if harness else []


# The gap between the two halves of a relation line. Wide enough that `→ …  ← …` reads
# as two facts, not one run-on: the board has horizontal room to spare, and the user
# 2026-09-10 chose spending it over spending vertical lines.
_RELATION_GAP = "   "


def _peer_half_segs(entry, direction, tag_by_key, include_kind=True, include_age=True,
                    repo_by_key=None):
    """One direction of peer messaging, without the leading icon."""
    if not entry:
        return []
    if direction == "sent":
        arrow, harness = "→", entry.get("to_harness")
        sid, name = entry.get("to_session_id"), entry.get("to_name")
    else:
        arrow, harness = "←", entry.get("from_harness")
        sid, name = entry.get("from_session_id"), entry.get("from_name")
    endpoint = _peer_endpoint_segs(harness, sid, name, tag_by_key, repo_by_key)
    if not endpoint:
        return []          # a record naming no endpoint at all is not a relation
    segs = [(arrow, "dim"), (" ", None)] + list(endpoint)
    kind = entry.get("kind") or ""
    if include_kind and kind:
        segs.append((" · " + _user_label(kind), "dim"))
    if include_age:
        segs.append((" · " + _text_age(time.time() - (entry.get("age_min") or 0) * 60), "dim"))
    return segs


def _peer_link_strip(sent=None, recv=None, tag_by_key=None, term_width=None, depth=0,
                     in_card=False, repo_by_key=None):
    """Both directions of peer messaging on ONE line: `✉ → [32] BC   ← [b0] hearting`.

    Drawn whenever the ledger has the relation — NOT only when the other endpoint also
    happens to be rendered this tick. That visibility gate is what made the same received
    message appear on one tick and vanish on the next (user 2026-09-09: "받는 세션도 좀
    이상하긴하네 일관성이 없고"); the endpoint's presence now only decides how it is
    LABELLED, never whether the relation exists. Exact `(harness, session_id)` remains the
    ledger's own identity key — this is the display layer relaxing, not the contract.

    One line, not two: sent and received are the same fact seen from two ends, and a
    session with both used to spend two rows saying so (user 2026-09-10 — "2줄로 줄여
    어차피 횡으로 여유 많은데"). The icon still says WHAT the relation is and each arrow
    says which WAY that half points, so nothing is encoded twice.
    """
    indent = _conn_indent(depth, in_card)

    def build(include_kind=True, include_age=True):
        halves = [h for h in (_peer_half_segs(sent, "sent", tag_by_key,
                                              include_kind, include_age, repo_by_key),
                              _peer_half_segs(recv, "recv", tag_by_key,
                                              include_kind, include_age, repo_by_key)) if h]
        if not halves:
            return []
        segs = [(indent, None), (_ICON_PEER, "dim"), (" ", None)]
        for i, half in enumerate(halves):
            if i:
                segs.append((_RELATION_GAP, None))
            segs += half
        return segs

    if not build():
        return []
    return [_fit_strip([lambda: build(True, True), lambda: build(True, False),
                        lambda: build(False, False)], term_width)]


def _peer_inline_options(session, tag_by_key=None):
    """Compact direction labels for the existing session identity row.

    The display label is deliberately weaker than the ledger join: only exact
    `(harness, full session_id)` pairs can combine into a bidirectional arrow.
    Names, cwd/repository labels and resolver reads are not used here.
    """
    sent = getattr(session, "peer_last_sent", None) or None
    recv = getattr(session, "peer_last_recv", None) or None

    def competing_keys(identity):
        canonical = getattr(tag_by_key, "canonical_by_key", {})
        owner = canonical.get(identity, identity)
        return {key for key in (tag_by_key or {})
                if isinstance(key, tuple) and len(key) == 2
                and canonical.get(key, key) != owner}

    def endpoint(entry, direction, peer_ids):
        if not entry:
            return [], []
        harness = str(entry.get("to_harness" if direction == "sent" else
                                "from_harness") or "").lower()
        sid = entry.get("to_session_id" if direction == "sent" else
                        "from_session_id")
        prefix = {"claude": "CL", "codex": "CX", "opencode": "OC"}.get(harness, "??")
        if not isinstance(sid, str) or not sid:
            label = [(prefix + "/?", "dim")]
            return label, label

        identity = (harness, sid)
        tag = (tag_by_key or {}).get((harness, sid))
        if isinstance(tag, str) and tag:
            competitors = competing_keys(identity)
            collisions = {key for key in competitors
                          if isinstance(tag_by_key.get(key), str)
                          and tag_by_key[key][:2] == tag[:2]}
            if collisions:
                label = [(prefix, "dim"), ("[", "dim"), (tag[:2], "tag"), ("]", "dim")]
                same_harness = [owner_sid for owner_harness, owner_sid in collisions
                                if owner_harness == harness and isinstance(owner_sid, str)]
                if same_harness:
                    peers = [other_sid for other_harness, other_sid in peer_ids
                             if other_harness == harness and other_sid != sid]
                    peers.extend(owner_sid for owner_sid in same_harness if owner_sid != sid)
                    suffix = _peer_unique_sid_suffix(sid, peers)
                    label.append(("~" + suffix, "dim"))
                return label, label
            label = [("[", "dim"), (tag[:2], "tag"), ("]", "dim")]
            return label, label

        peers = [other_sid for other_harness, other_sid in peer_ids
                 if other_harness == harness and other_sid != sid]
        peers.extend(owner_sid for owner_harness, owner_sid in competing_keys(identity)
                     if owner_harness == harness and isinstance(owner_sid, str)
                     and owner_sid != sid)
        peers = list(dict.fromkeys(peers))
        full_sid = sid[:6]
        short_sid = sid[:4]
        if any(other.startswith(full_sid) for other in peers):
            full_sid += "~" + _peer_unique_sid_suffix(sid, peers)
        if any(other.startswith(short_sid) for other in peers):
            short_sid += "~" + _peer_unique_sid_suffix(sid, peers)
        return [("%s/%s" % (prefix, full_sid), "dim")], \
               [("%s/%s" % (prefix, short_sid), "dim")]

    def key(entry, direction):
        if not isinstance(entry, dict):
            return None
        harness = entry.get("to_harness" if direction == "sent" else
                            "from_harness")
        sid = entry.get("to_session_id" if direction == "sent" else
                        "from_session_id")
        harness = str(harness or "").lower()
        if harness not in {"claude", "codex", "opencode"} or not isinstance(sid, str) or not sid:
            return None
        return harness, sid

    peer_ids = [identity for identity in (key(sent, "sent"), key(recv, "recv"))
                if identity is not None]

    if not sent and not recv:
        return []
    sent_key = key(sent, "sent")
    same = sent_key is not None and sent_key == key(recv, "recv")
    if same:
        endpoint_segs, _minimal = endpoint(sent, "sent", peer_ids)
        full = [(" ↔ ", None)] + endpoint_segs
        compact = [(" ↔", None)] + endpoint_segs
        minimal = [(" ↔ ", None)] + _minimal
    else:
        entries = []
        if sent:
            entries.append(("→", "sent", sent))
        if recv:
            entries.append(("←", "recv", recv))
        full, compact, minimal = [], [], []
        for index, (arrow, direction, entry) in enumerate(entries):
            label, min_label = endpoint(entry, direction, peer_ids)
            if index:
                full.append((" ", None))
                compact.append((" ", None))
            full.extend([(" " if index == 0 else "", None), (arrow, "dim"),
                         (" ", None)] + label)
            compact.extend([(" " if index == 0 else "", None), (arrow, "dim")] + label)
            minimal.extend([(" ", None), (arrow, "dim"),
                            ("", None)] + min_label)
    # The shortest form still names each harness/peer token; only endpoint detail
    # is shed at narrow widths, never the direction or the whole peer identity.
    return [option for option in (full, compact, minimal) if option]


def _peer_unique_sid_suffix(session_id, others):
    """Shortest trailing SID fragment that separates this token from its peers."""
    for length in range(1, len(session_id) + 1):
        suffix = session_id[-length:]
        if all(other == session_id or not other.endswith(suffix) for other in others):
            return suffix
    return session_id


class _PeerTagMap(dict):
    """Visible endpoint tags with aliases mapped back to their owning session."""

    def __init__(self):
        super().__init__()
        self.canonical_by_key = {}


def _peer_inline_fit(session, tag_by_key, available):
    options = _peer_inline_options(session, tag_by_key)
    if not options:
        return [], 0
    if available is None:
        chosen = options[0]
    else:
        chosen = next((option for option in options
                       if sum(_dw(text) for text, _key in option) <= max(0, available)),
                      options[-1])
    return chosen, sum(_dw(text) for text, _key in chosen)


def _peer_obligation_strip(obligations=None, term_width=None, depth=0, in_card=False):
    """Show bounded accepted peer follow-through without message content."""
    counts = {}
    for item in obligations or []:
        kind = item.get("kind")
        if kind in {"message", "delay", "retire", "watch"}:
            counts[kind] = counts.get(kind, 0) + 1
    if not counts:
        return []
    labels = (("message", "msg"), ("delay", "delay"),
              ("retire", "retire"), ("watch", "watch"))
    parts = [f"{label} {counts[kind]}" for kind, label in labels if counts.get(kind)]
    segs = [(_conn_indent(depth, in_card), None), ("↻ pending ", "lvl_y")]
    for index, part in enumerate(parts):
        if index:
            segs.append((" · ", "dim"))
        segs.append((part, "lvl_y"))
    return [_fit_strip([lambda: segs], term_width)]


# Past nodes recede (dim); a failure or an unverifiable state keeps its alarm hue.
_ROUTE_CHAIN_MARK = {"done": ("✓", "dim"), "failed": ("✕", "lvl_r"), "unknown": ("?", "lvl_y")}


def _route_chain_node_knobs(node, with_knobs):
    """The node's paren detail: `mode·intensity` (the same knobs as the single-route tag)
    when `with_knobs`, plus its retry round `R<n>`, which is kept even when the knobs fold."""
    items = []
    if with_knobs:
        mode = node.get("capability_mode")
        if mode and mode != "default":
            items.append(str(mode).replace(",", "·"))
        level = _short_level(node.get("intensity"))
        if level:
            items.append(level)
    round_n = node.get("round")
    if isinstance(round_n, int) and not isinstance(round_n, bool) and round_n >= 2:
        items.append("R%d" % round_n)
    return "·".join(items)


# User 2026-09-18 ("가장 마지막 라우팅 3개까지만 고정으로", then "최대 4개로 하고, 계획도 바로
# 앞에꺼 1개까지는 보이게"): the cell always shows a window of at most this many chain nodes —
# the node right after the current one (the next planned step) when there is one, and the
# current node with as much history before it as the rest of the window holds. Later plan
# steps and older routes are simply out of the window.
_ROUTE_CHAIN_WINDOW = 4


def _route_chain_window(nodes, current_idx):
    """`(start, window)`: at most `_ROUTE_CHAIN_WINDOW` nodes ending one past the current
    node when a next node exists, else at the current node."""
    ahead = 1 if current_idx + 1 < len(nodes) else 0
    start = max(0, current_idx - (_ROUTE_CHAIN_WINDOW - 1 - ahead))
    return start, nodes[start:current_idx + 1 + ahead]


_SUMMARY_FALLBACK_W = 60   # hermetic/no-terminal-width callers (mirrors the dim-snippet clip
                           # convention used elsewhere, e.g. the memory-row snippet cells)


def _summary_row(summary, depth=0, term_width=None, start_col=None, summary_ts=None):
    """One dim subtitle row directly under a session/dispatch row (F-16/F-17 merge,
    사용자 확정 2026-07-19): the last recorded summary from the SAME haiku call that
    produced the title. Pure inset — no connector/icon, `_SUBAGENT_IND` + the same
    per-depth ladder `_subagent_strip` uses, so it reads as INSIDE its owner row and
    never collides with the sub-agent strip's own indent. Caller gates presence
    (summary truthy, row not dead/stale — F-13) and ordering (this row before the
    owner's `⚡` strip, never after)."""
    indent = _SUBAGENT_IND + "  " * max(0, depth)
    target_col = int(start_col or 0)
    if term_width:
        target_col = min(target_col, max(_dw(indent), term_width - 1))
    padding = max(0, target_col - _dw(indent))
    used = _dw(indent) + padding
    maxw = max(1, term_width - used) if term_width else _SUMMARY_FALLBACK_W
    segs = [(indent, None)]
    if padding:
        segs.append((" " * padding, None))
    segs.append((_aged_text(summary, summary_ts, maxw), "now_sub"))
    return [segs]


def _report_verification_detail_row(job, depth=1, term_width=None, in_card=False):
    """Render only the read-time payload attached by the detail collector."""
    projection = getattr(job, "work_projection", None)
    payload = getattr(projection, "report_verification", None)
    if not isinstance(payload, dict):
        return []
    verification = payload.get("verification") or {}
    completion = payload.get("completion") or {}
    inputs = payload.get("required_input_observation") or {}
    reasons = inputs.get("reasons") or []
    reason = reasons[0] if reasons else verification.get("reason")
    if (reason == "report-cycle-unadmitted"
            and (payload.get("report_observation") or {}).get("state") == "present"):
        detail = "report present · verify/close pending"
    else:
        verdict = {"PASS": "PASS", "FAIL": "FAIL"}.get(
            verification.get("verdict"), "verify unknown")
        state = {"complete": "complete", "pending": "close pending", "blocked": "close blocked",
                 "not-applicable": "close n/a"}.get(completion.get("state"), "close unknown")
        input_state = {"confirmed": "input checked", "failed": "input failed"}.get(
            inputs.get("state"), "input unknown")
        detail = "report · " + " · ".join((verdict, state, input_state))
        if reason:
            detail += " · " + {
                "report-cycle-unadmitted": "verify/close pending",
                "route-hash-binding-mismatch": "report binding mismatch",
                "required-input-digest-mismatch": "input changed",
                "artifact-revision-stale": "report changed",
                "report-source-kind-invalid": "check report path",
            }.get(reason, "check report")
    text = _conn_indent(depth, in_card) + "↳ " + detail
    if term_width:
        text = _clip_w(text, max(1, term_width - 2))
    return [[(text, "dim")]]


def _resource_progress_tail(child, room=None):
    """Format only the shared reader's normalized counters, on the existing row."""
    progress = getattr(child, "progress", None)
    if not isinstance(progress, dict):
        return ""
    try:
        count = str(progress["completed"])
        if "total" in progress:
            count += "/" + str(progress["total"])
        count += " " + progress["unit"]
        age_s = progress["age_s"]
        suffix = " · " + _text_age(time.time() - age_s)
    except (KeyError, TypeError, ValueError, OverflowError):
        return ""
    if room is not None:
        count_room = room - _dw(suffix)
        if count_room < 1:
            # At a small viewport retain the observation's age even when the
            # counter cannot fit beside the complete short work name.
            age_only = suffix.removeprefix(" · ")
            return age_only if _dw(age_only) <= room else ""
        count = _clip_w(count, count_room)
    return count + suffix


def _resource_verified_process_location(child):
    """Use existing exact local identity/placement, never GPU selection or log age."""
    if type(child.pid) is not int or child.pid <= 0 or not str(child.starttime).isdigit():
        return ""
    placement = getattr(child, "local_placement", None) or {}
    placed = any(isinstance(p, dict) and p.get("pid") == child.pid
                 and str(p.get("starttime")) == str(child.starttime)
                 for p in placement.get("processes", ()))
    evidence = getattr(child, "state_evidence", None) or {}
    current = evidence.get("current_identity") or {}
    verified = (evidence.get("reason") == "exact-identity-match"
                and current.get("pid") == child.pid
                and str(current.get("starttime")) == str(child.starttime))
    if child.liveness != "working" or not (placed or verified):
        return ""
    host = _gpu_safe_text(placement.get("hostname")) if placed else ""
    return (host + " · " if host else "") + "PID %s" % child.pid


def _resource_gpu_suffix(children, room=None):
    """Resource identity once at the GPU line end; working is already implicit."""
    unique = {child.run_id: child for child in children}
    title_groups = {}
    for child in unique.values():
        key = resource_title_key(child)
        if key:
            title_groups[key] = title_groups.get(key, 0) + 1
    shown_titles = set()
    parts = []
    for child in unique.values():
        node = _gpu_safe_text(_resource_name(child))
        key = resource_title_key(child)
        if title_groups.get(key, 0) > 1:
            # A shared cycle title names the work once; each execution still
            # keeps its own short node, state, elapsed time and counters.
            short_node = _gpu_safe_text(_readable_name(child.route_node or child.node))
            short_node = "" if short_node == node else short_node
            if key not in shown_titles:
                shown_titles.add(key)
                node += " · " + short_node if short_node else ""
            else:
                node = short_node
        state = "" if child.liveness == "working" else " " + _gpu_safe_text(child.liveness)
        parts.append([child, (node + state + " " + fmt_min(child.elapsed_min)).strip()])
    base = "".join(" · " + text for _child, text in parts)
    extra = max(0, room - _dw(base)) if room is not None else None
    out = ""
    for child, text in parts:
        progress = _resource_progress_tail(child, max(0, extra - 3) if extra is not None else None)
        if progress:
            text += " · " + progress
            if extra is not None:
                extra -= _dw(" · " + progress)
        out += " · " + text
    return _clip_w(out, room) if room is not None else out


def _resource_gpu_resources(child, snapshot, commands=False, excluded_processes=()):
    """Reuse local PID/start/group marks, exact run id or remote parent attempt."""
    if not isinstance(snapshot, dict):
        return []
    exact, groups = _compute_hosts._registered_run_marks([child])
    exact = {(pid, start) for pid, start in exact if start.isdigit()}
    if type(child.pid) is int and child.pid > 0 and str(child.starttime).isdigit():
        exact.add((child.pid, str(child.starttime)))
    for process in (getattr(child, "local_placement", None) or {}).get("processes", ()):
        if type(process.get("pid")) is int and str(process.get("starttime", "")).isdigit():
            exact.add((process["pid"], str(process["starttime"])))
    resources = []
    for host in snapshot.get("hosts") or ():
        if not isinstance(host, dict) or host.get("reachable") is not True:
            continue
        for gpu in host.get("gpus") or ():
            if not isinstance(gpu, dict) or type(gpu.get("index")) is not int:
                continue
            matched = []
            for process in gpu.get("processes") or ():
                if not isinstance(process, dict):
                    continue
                process_key = (host.get("host") or "?", process.get("pid"),
                               str(process.get("proc_start")))
                if process_key in excluded_processes:
                    continue
                owner = process.get("owner")
                local = host.get("self") is True and (
                    (process.get("pid"), str(process.get("proc_start"))) in exact
                    or process.get("pgid") in groups)
                run = isinstance(owner, dict) and owner.get("kind") == "run" and owner.get("id") == child.run_id
                parent = (host.get("self") is False and isinstance(owner, dict)
                          and owner.get("kind") == "job" and child.parent_attempt_id
                          and owner.get("id") == child.parent_attempt_id)
                if local or run or parent:
                    matched.append(process)
            if matched:
                used = [process.get("used_memory_mib") for process in matched
                        if type(process.get("used_memory_mib")) is int]
                resources.append({"host": _gpu_safe_text(host.get("host") or "?"),
                    "index": gpu["index"], "model": _gpu_safe_text(gpu.get("name")).replace("NVIDIA ", ""),
                    "process_count": len(matched),
                    "processes": [{"pid": process.get("pid"),
                        "proc_start": process.get("proc_start"),
                        "command": _gpu_safe_text(process.get("command")
                                                   or process.get("process_name") or "process")}
                        for process in matched] if commands else [],
                    "_process_keys": [(host.get("host") or "?", process.get("pid"),
                                       str(process.get("proc_start"))) for process in matched],
                    "_process_memory": {(host.get("host") or "?", process.get("pid"),
                                         str(process.get("proc_start"))):
                        process.get("used_memory_mib") if type(process.get("used_memory_mib")) is int else None
                        for process in matched},
                    "has_memory": bool(used), "used_memory_mib": sum(max(0, value) for value in used)})
    return resources


def _job_gpu_resources(job, session_by_identity, gpu_resources):
    resources = _gpu_resources_for_session(job, gpu_resources or {})
    if not resources:
        session = _session_for_job(session_by_identity, job)
        resources = _gpu_resources_for_session(session, gpu_resources or {}) if session else []
    return resources


def _drawn_gpu_processes(jobs, session_by_identity, gpu_resources, snapshot):
    keys = set()
    for job in jobs:
        for resource in _job_gpu_resources(job, session_by_identity, gpu_resources):
            keys.update(resource.get("_process_keys") or ())
        for child in getattr(job, "resource_children", ()):
            for resource in _resource_gpu_resources(child, snapshot):
                keys.update(resource["_process_keys"])
    return keys


def _gpu_safe_text(value):
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(value or ""))


def _gpu_session_keys(session):
    harness = getattr(session, "harness", None)
    session_id = (getattr(session, "session_id", None)
                  or getattr(session, "_runtime_session_id", None))
    if harness not in {"claude", "codex", "opencode"} or not session_id:
        return []
    ids = [session_id] + list(getattr(session, "session_aliases", None) or ())
    ids += list(getattr(session, "_gpu_session_aliases", None) or ())
    return [(harness, sid) for sid in dict.fromkeys(ids) if isinstance(sid, str) and sid]


def _gpu_resources_for_session(session, resource_index):
    resources = {}
    for session_key in _gpu_session_keys(session):
        for source in resource_index.get(session_key, ()):
            key = (source["host"], source["index"])
            if key not in resources:
                resources[key] = {**source, "processes": list(source.get("processes") or ()),
                                  "_process_keys": list(source.get("_process_keys") or ()),
                                  "_process_memory": dict(source.get("_process_memory") or {})}
                continue
            resource = resources[key]
            resource["process_count"] += source["process_count"]
            resource["used_memory_mib"] += source["used_memory_mib"]
            resource["has_memory"] |= source["has_memory"]
            resource["telemetry_unknown"] |= source.get("telemetry_unknown", False)
            resource["access_only"] &= source.get("access_only", False)
            resource["processes"].extend(source.get("processes") or ())
            resource["_process_keys"].extend(source.get("_process_keys") or ())
            resource["_process_memory"].update(source.get("_process_memory") or {})
    return [resources[key] for key in sorted(resources)]


def _session_for_job(session_by_identity, job):
    if not job or getattr(job, "pid", None) is None:
        return None
    key = (job.pid, getattr(job, "proc_start", None))
    return session_by_identity.get(key)
