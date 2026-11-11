"""Read-only conversion of already collected Fleet records into the explorer view model.

Every section below turns facts a Fleet reader already decided (liveness, identity, route
projection, usage, compute hosts, memory, peers) into records of exactly the shape the checked
fixture uses. A value the source does not carry is ``None`` (drawn ``·``), a section with no source
is omitted, and nothing is guessed. There is no I/O here: the slow per-selection reads stay in the
background focus reader and arrive through ``accepted_detail``.
"""
from collections.abc import Mapping
import datetime
import math
import os
import re
import time

from .view_model import ExplorerViewModel
from ..config import ui_text
from ..display import codex_credits
from ..model import current_attempt_rank, exec_child_is_wait
from .. import route_visibility

HARNESSES = ("claude", "codex", "opencode")
_SEG_DIM = "dim"


def _get(value, *keys, default=None):
    for key in keys:
        candidate = value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
        if candidate is not None:
            return candidate
    return default


def _records(value):
    return tuple(value or ())


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _text(value):
    return value if isinstance(value, str) and value else None


def _dur(minutes):
    """Whole minutes -> the board's duration text; None when unknown."""
    value = _num(minutes)
    if value is None or value < 0:
        return None
    value = int(value)
    if value < 60:
        return f"{value}m"
    hours, minute = divmod(value, 60)
    if hours < 24:
        return f"{hours}h {minute}m"
    days, hour = divmod(hours, 24)
    return f"{days}d {hour}h"


def _since(epoch, now):
    value = _num(epoch)
    if value is None or now is None:
        return None
    return _dur(max(0, (now - value) / 60))


def _iso_age(value, now):
    """``12m ago`` for an ISO timestamp (or epoch seconds); None when it cannot be read."""
    if isinstance(value, str):
        try:
            stamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        value = stamp.timestamp()
    age = _since(value, now)
    return f"{age} ago" if age else None


def _clock(epoch):
    value = _num(epoch)
    if value is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(value).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def _model_pair(model):
    """``[text, colour key]`` for a runtime model name, or None when it is unknown."""
    name = _text(model)
    if name is None:
        return None
    try:
        from .. import render
        text = render._clean_model(render._dispatch_display_model(name)) or name
        key = render._model_key(name)
    except Exception:
        text = name.split(" (", 1)[0]
        key = "fam_other"
    return [text, key]


def _effort(value):
    text = _text(value)
    if text is None or text.strip().lower() in ("runtime-default", "inherit", "unknown", "default"):
        return None
    return text


def _session_model_effort(session, state):
    """``(model pair, effort)`` the way the default board's harness cell resolves them: a stale or
    dead row has no live telemetry (bare harness); the runtime's own default effort is ``default``."""
    if state in ("stale", "dead"):
        return None, None
    model = _model_pair(_get(session, "model", "m"))
    effort = _effort(_get(session, "effort", "e"))
    if effort is None and model is not None and _get(session, "effort_default"):
        effort = "default"
    return model, effort


def _short_capability(name):
    if not isinstance(name, str):
        return None
    return name[len("autopilot-"):] if name.startswith("autopilot-") else name


FRAME_ROUTE_CAPABILITY = "route-frame"
FRAME_ROUTE_SHAPE = "framed"


def _is_frame_route(capability, record, topology):
    """Whether the board draws this route as a frame route.

    A sealed frame route carries ``capability: route-frame`` and records the
    framed selection in ``selection.shape``; its ``execution_topology`` stays
    the ordinary ``staged``. The earlier synthetic shape (``capability: frame``
    or ``execution_topology: framed``) still renders through the same branch.
    Only reads the record; ``capability_mode`` and the topology stay untouched
    so collectors, assignment, pins and fold inputs see the source values.
    """
    selection = _get(record, "selection")
    return (capability == FRAME_ROUTE_CAPABILITY
            or _short_capability(capability) == "frame"
            or _text(_get(selection, "shape")) == FRAME_ROUTE_SHAPE
            or topology == FRAME_ROUTE_SHAPE)


# -- sessions -----------------------------------------------------------------------------

def _is_main(session):
    if _get(session, "is_child") or _get(session, "mem_worker"):
        return False
    return _get(session, "process_role") != "registered-worker"


PENDING_TAG = "?"                # an unconfirmed row's key prefix: never a number another session wears


def _shared_tag(harness, sid):
    """The one badge rule every surface draws (Herdr title, peer trailer, Fleet collector)."""
    try:
        from ..session_handle import resolve_tag
        return _text(resolve_tag(harness, sid))
    except Exception:
        return None


def _tag_for(session, used):
    """The collector's tag, else the shared rule for the native session id, else ``None``.

    Explorer mints no number of its own: a row whose native session id is not confirmed, or whose
    shared tag already belongs to another row, gets no number here (the caller shows it as pending)
    so it can never take or shadow the number of a different live session."""
    tag = _text(_get(session, "session_tag", "tag"))
    if tag is not None:
        return tag
    sid = _text(_get(session, "session_id", "sid", "id"))
    if sid is None:
        return None
    tag = _shared_tag(_text(_get(session, "harness", "adapter", "h")), sid)
    return tag if tag is not None and tag not in used else None


def _resolver(snapshot):
    resolver = _get(snapshot, "project_of")
    if callable(resolver):
        return resolver
    display = _get(snapshot, "display_snapshot")
    try:
        from ..model import project_of
    except Exception:
        project_of = None
    if display is not None and hasattr(display, "project_for") and project_of is not None:
        return lambda cwd: display.project_for(cwd, project_of)
    return project_of


UNKNOWN_REPO = ""               # a row whose cwd gives no project: grouped, never labelled (the default board hides it too)
NO_PROJECT_LABELS = frozenset({"(unknown)", "(root)"})


def _shown_repo(label):
    """The project label to draw: the default board's hidden labels are not names, so they become no name."""
    text = _text(label)
    return UNKNOWN_REPO if text is None or text in NO_PROJECT_LABELS else text


def _home_forms():
    forms = {os.path.expanduser("~")}
    try:
        forms.add(os.path.realpath(os.path.expanduser("~")))
    except OSError:
        pass
    return tuple(sorted(f.rstrip("/") for f in forms if f and f != "/"))


_HOME_FORMS = _home_forms()
HOME_LABEL = "~"


def _home_folder(label, cwd):
    """Whether ``label`` is the home folder itself: the user's folder name, and ``cwd`` lies inside a
    folder of that name (``$HOME`` or the data root that carries the same name)."""
    names = {os.path.basename(home) for home in _HOME_FORMS}
    parts = [part for part in str(cwd).split("/") if part]
    return label in names and label in parts


def _path_label(cwd):
    try:
        from ..display_snapshot import path_label
        return _shown_repo(path_label(cwd))
    except Exception:
        return _shown_repo(os.path.basename(str(cwd).rstrip("/")))


def _repo_of(resolver, cwd, fallback=None):
    """The project group of a row; a row with no usable cwd is grouped under the nameless group. A folder
    inside the home directory is never grouped under the home folder itself (a git repository at
    ``$HOME`` would make every such session one ``<user>/`` project): it keeps its own folder name."""
    if _text(cwd) is None:
        return _shown_repo(fallback)
    label = None
    if callable(resolver):
        try:
            label = _text(resolver(cwd))
        except Exception:
            label = None
    if label is None:
        label = _text(fallback) or os.path.basename(str(cwd).rstrip("/")) or UNKNOWN_REPO
    label = _shown_repo(label)
    if _home_folder(label, cwd):
        last = [part for part in str(cwd).split("/") if part][-1:]
        return HOME_LABEL if last == [label] else _path_label(cwd)
    return label


def _context_gauge(session):
    """``(used %, window in millions of tokens)`` exactly as the default board's context row resolves
    them: the resolved projection when the row has one, else the runtime's own gauge. A stale or dead
    row draws no context."""
    if _get(session, "liveness", "status", "state", "st") in ("stale", "dead"):
        return None, None
    context = _get(session, "context")
    if context is not None and not isinstance(context, (int, float)):
        pct = _num(_get(context, "used_pct"))
    else:
        pct = _num(context)
        if pct is None:
            for key in ("context_pct", "ctx_pct", "ctx"):
                pct = _num(_get(session, key))
                if pct is not None:
                    break
    window = _num(_get(session, "context_display_window_tokens"))
    raw_window = _num(_get(session, "context_window_tokens"))
    if pct is None and window is None:
        try:
            from .. import render
            native_pct, native_window = render._native_context_gauge(session)
        except Exception:
            native_pct, native_window = None, raw_window
        pct = _num(native_pct)
        raw_window = _num(native_window) or raw_window
    tokens = window or raw_window
    if tokens:
        scaled = round(tokens / 1_000_000, 3)
    else:
        scaled = _num(_get(session, "window", "win"))
    return (None if pct is None else int(round(pct))), scaled


def _running(session):
    """The ``running:`` line: the tool or child process the session is executing, with how long."""
    parts = []
    tool = _get(session, "exec_tool")
    if isinstance(tool, Mapping) and _text(tool.get("name")):
        parts.append(["⚙ " + tool["name"], "soft"])
        etime = _num(tool.get("etime_s"))
        if etime is not None:
            parts.append([" · " + (_dur(etime / 60) or ""), _SEG_DIM])
    child = _get(session, "exec_child")
    if not parts and isinstance(child, Mapping) and _text(child.get("comm")):
        parts.append(["⚙ " + child["comm"], "soft"])
        etime = _num(child.get("leaf_etime_s", child.get("etime_s")))
        if etime is not None:
            parts.append([" · " + (_dur(etime / 60) or ""), _SEG_DIM])
    subagents = _get(session, "subagents")
    if subagents:
        parts.append([f"{len(subagents)} subagent", _SEG_DIM])
    return parts or None


_ASK_WORDS = {"decision": "answer needed", "question": "answer needed", "answer": "answer needed",
              "permission": "approval needed", "approval": "approval needed"}


def _ask_kind(session):
    """The words the row shows for a session waiting on a person (any kind of wait reads as a wait)."""
    state = _get(session, "interaction_state")
    kind = _text(_get(state, "kind")) if state is not None else None
    return _ASK_WORDS.get(kind, kind)


def _delivery_panes(snapshot):
    """``{session id: pane}`` from the panes peer messages were delivered to (newest message wins)."""
    records = _get(_get(snapshot, "peer_messages"), "records", default=()) or ()
    found = {}
    for message in sorted((m for m in records if isinstance(m, Mapping)),
                          key=lambda m: _epoch_of(m.get("ts")) or 0.0):
        target = message.get("to") or {}
        sid, pane = _text(_get(target, "session_id")), _text(_get(target, "pane"))
        if sid and pane:
            found[sid] = pane
    return found


def herdr_pane_server():
    """``herdr:<endpoint>``: the Herdr server whose panes Fleet's collectors observe."""
    try:
        from .focus import utility
        return "herdr:" + str(utility("herdr_observation").endpoint_name())
    except Exception:
        return "herdr:default"


def _session_pane(session, delivery_panes):
    """``{"pane", "pane_server"}``: the session's pane and the server it was observed on.

    Herdr's pane census (identity evidence) is read on Fleet's Herdr endpoint and the tmux
    census on the tmux server Fleet reads. A pane known only from a delivered message has
    no known server, so pressing it moves nothing rather than a same-numbered pane elsewhere.
    """
    own = _text(_get(session, "herdr_pane", "pane", "pane_id"))
    if own:
        server = _text(_get(session, "pane_server")) or None
        return {"pane": own, "pane_server": server, "pane_from": "census" if server else None}
    evidence = _text(_get(_get(session, "session_identity_evidence"), "pane"))
    if evidence:
        return {"pane": evidence, "pane_server": herdr_pane_server(), "pane_from": "census"}
    tmux = _text(_get(session, "tmux_pane"))
    if tmux:
        return {"pane": tmux, "pane_server": "tmux:" + (_text(_get(session, "tmux_server")) or ""),
                "pane_from": "census"}
    delivered = delivery_panes.get(_text(_get(session, "session_id", "sid", "id")) or "")
    return {"pane": delivered, "pane_server": None, "pane_from": "peer" if delivered else None}


def settle_panes(rows, seen, questions=()):
    """The one rule for every session's ``(pane, pane_server)`` pair, shared by every publication.

    1. a pair the collectors' census gave (``pane_from == "census"``) stays;
    2. otherwise a pane the current pane map or pane read observed for the same bound
       session (``seen``: ``{tag: pane}``) takes Fleet's Herdr server;
    3. otherwise a pane known only from a peer delivery keeps no server, so it moves nothing.

    A question then opens the same pair as its session (``session_tag``/``open_tag``).
    """
    by_tag = {row["tag"]: row for row in rows}
    for tag, pane in seen.items():
        row = by_tag.get(tag)
        if row is not None and pane and row.get("pane_from") != "census":
            row["pane"], row["pane_server"], row["pane_from"] = pane, herdr_pane_server(), "seen"
    for question in questions:
        owner = by_tag.get(question.get("session_tag") or question.get("open_tag") or question.get("tag") or "")
        if owner and owner.get("pane"):
            question["pane"], question["pane_server"] = owner["pane"], owner.get("pane_server")


def _last_input(display, now):
    """Presentation of a decided frontier, with a fresh age and no text classification."""
    from ..prompt_line import last_input
    return last_input(display, now)


def _received_inputs(display, tags=None):
    display = display if isinstance(display, Mapping) else {}
    rows = []
    for item in reversed(tuple(display.get("received") or ())[-8:]):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("sender") or "runtime")
        tag = re.search(r"\[([0-9a-f]{2})\]", name, re.I)
        harness, sid = item.get("sender_harness"), item.get("sender_session_id")
        sender = (tags or {}).get((harness, sid)) if isinstance(harness, str) and isinstance(sid, str) else None
        sender = sender or (tag.group(1) if tag else name)
        rows.append("⇄ [%s] %s %s" % (sender, _clock(item.get("at")) or "·", item.get("text") or "·"))
    return rows


def _session_records(snapshot, now, resolver):
    used, rows, by_key = set(), [], {}
    delivery_panes = _delivery_panes(snapshot)
    sessions = [session for session in _records(_get(snapshot, "sessions", default=())) if _is_main(session)]
    # A collector tag is authoritative wherever its row sits: a tag minted for an earlier row must
    # never take it, or the row that owns it is dropped as a duplicate below.
    reserved = {tag for tag in (_text(_get(session, "session_tag", "tag")) for session in sessions) if tag}
    pending = 0
    for session in sessions:
        tag = _tag_for(session, used | reserved)
        if tag is None:
            # A process row whose native session is not confirmed stays visible as pending under a
            # key no session tag can equal, so it neither borrows nor hides a real session's number.
            if _text(_get(session, "session_id", "sid", "id")) is None and (
                    _get(session, "pid") is None or _get(session, "proc_start", "started_at") is None):
                continue
            pending += 1
            tag = f"{PENDING_TAG}{pending}"
        elif tag in used:
            continue
        used.add(tag)
        harness = _text(_get(session, "harness", "adapter", "h"))
        cwd = _get(session, "cwd", "project")
        repo = _repo_of(resolver, cwd, _get(session, "repo", "slug"))
        state = _text(_get(session, "liveness", "status", "state", "st"))
        interaction = _get(session, "interaction_state")
        elapsed = _get(session, "elapsed_min")
        age = _dur(elapsed) if _num(elapsed) is not None else _text(_get(session, "age"))
        model, effort = _session_model_effort(session, state)
        context, window = _context_gauge(session)
        record = {
            "tag": tag, "harness": harness, "model": model, "effort": effort,
            "state": state, "steward": bool(_get(session, "steward", default=False)),
            "title": _text(_get(session, "title", "name")),
            "context": context, "window": window,
            "now": _text(_get(session, "summary", "now")),
            "last_input": _last_input(_get(session, "_prompt_display"), now),
            "received_inputs": [],
            "ask": _ask_kind(session) if state == "blocked" else None,
            "age": age, "repo": repo, "repo_id": repo,
            "branch": _text(_get(session, "branch")),
            **_session_pane(session, delivery_panes),
            "generation": None, "last_clear": None,
            "running": _running(session), "gpu": None, "pane_meta": None, "pane_lines": [],
        }
        if (_get(session, "_resume_attention") == "stopped"
                and state not in ("working", "dead", "stale") and not record["ask"]):
            record["restart"] = "stopped"
        from ..token_budget import context_display_detail
        detail = context_display_detail(session)
        if detail is not None:
            record["context_detail"] = detail
        rows.append(record)
        sid = _text(_get(session, "session_id", "sid", "id"))
        chain = _get(session, "route_chain")
        chain_routes = {_text(node.get("route_id")) for node in (chain.get("nodes") or ())
                        if isinstance(node, Mapping)} if isinstance(chain, Mapping) else set()
        by_key[tag] = {"session": session, "sid": sid, "interaction": interaction,
                       "ids": tuple(i for i in (sid, *(_get(session, "session_aliases") or ())) if _text(i)),
                       "chain": chain_routes - {None}}
    tags = {(info["session"].harness if hasattr(info["session"], "harness") else
             _get(info["session"], "harness"), sid): tag
            for tag, info in by_key.items() for sid in info["ids"]}
    for row in rows:
        row["received_inputs"] = _received_inputs(
            _get(by_key[row["tag"]]["session"], "_prompt_display"), tags)
    return rows, by_key


# -- routes -------------------------------------------------------------------------------

_NODE_STAGE = {"done": "done", "active": "active", "reconciling": "active", "recovering": "active",
               "failed": "failed"}


def _route_entities(snapshot):
    entities = []
    for key in ("sessions", "jobs", "route_entities"):
        entities.extend(_records(_get(snapshot, key, default=())))
    return entities


def _work(entity):
    return _get(entity, "work_projection")


def _collect_routes(snapshot):
    """route id -> {"view", "record", "jobs", "sessions"} in first-seen order."""
    found = {}
    for entity in _route_entities(snapshot):
        work = _work(entity)
        rid = _text(_get(work, "route_id")) if work is not None else None
        rid = rid or _text(_get(entity, "route_id")) or _text(_get(entity, "owner_route_id"))
        if rid is None:
            continue
        slot = found.setdefault(rid, {"view": None, "record": None, "jobs": [], "sessions": []})
        route_view = _get(work, "_route_view") if work is not None else None
        if isinstance(route_view, Mapping):
            if slot["view"] is None and isinstance(route_view.get("view"), Mapping):
                slot["view"] = route_view["view"]
            if slot["record"] is None and isinstance(route_view.get("record"), Mapping):
                slot["record"] = route_view["record"]
        if _get(entity, "key") is not None or hasattr(entity, "parent_sid"):
            slot["jobs"].append(entity)
        else:
            slot["sessions"].append(entity)
    return found


def _stage_state(node):
    return _NODE_STAGE.get(node.get("state"), "todo")


def _stage_model(model):
    """A stage's model as the route record names it (``opus`` stays ``opus``): ``[name, colour key]``."""
    name = _text(model)
    if name is None:
        return None
    try:
        from .. import render
        return [name, render._model_key(name)]
    except Exception:
        return [name, "fam_other"]


def _stage_record(node):
    harness = _text(node.get("harness"))
    stage = {
        "name": _text(node.get("id")) or "·", "st": _stage_state(node),
        "el": _dur(node.get("elapsed_min")), "h": harness,
        "m": _stage_model(node.get("model")), "e": _effort(node.get("effort")),
        "gate": bool(node.get("gate_passed")),
    }
    rounds = _num(node.get("route_attempt_round")) or _num(node.get("attempt_round"))
    if rounds and rounds > 1:
        stage["r"] = int(rounds)
    if stage["st"] == "active" and node.get("unit") == "_kernel/resource":
        stage["host"] = None
    return stage


def _resource_run_label(run):
    """``host:gpu · launcher`` of a resource run: where it runs and what it was launched with."""
    placement = _get(run, "local_placement")
    host = _text(_get(placement, "hostname")) if placement is not None else None
    host = host.split(".", 1)[0] if host else None
    devices = [d for d in (_get(placement, "requested_devices", default=()) or ()) if isinstance(d, str)]
    gpu = next((d for d in devices if d.isdigit()), None)
    command = _get(run, "command", default=()) or ()
    script = os.path.basename(str(command[-1])) if command and isinstance(command[-1], str) else None
    where = f"{host}:{gpu}" if host and gpu is not None else host
    return " · ".join(part for part in (where, script) if part) or None


FRAME_NODES = ("frame", "frame-alternative")


def _is_frame_leg(node):
    """A frame/frame-alternative leg, named by the node itself or its anchored job."""
    if node is None:
        return False
    if _text(node.get("id")) in FRAME_NODES:
        return True
    if _text(node.get("worker_type")) == "frame":
        return True
    anchor = node.get("job")
    return (_text(_get(anchor, "worker_type")) == "frame"
            or _text(_get(anchor, "route_node")) in FRAME_NODES)


def _worker_segments(node, resources, state_text=None, owner=None):
    """``[label, harness segments, state segments]`` of the route's worker, or None.

    ``owner`` is the route's depth-1 owner attempt when it runs this very node (a
    one-shot route): the line then reads ``owner`` with the owner's own model, never
    ``worker``, which names only a depth-2 attempt the owner launched."""
    if node is None:
        return None
    harness = _text(node.get("harness"))
    unit = _text(node.get("unit")) or ""
    elapsed = _dur(node.get("elapsed_min"))
    running = node.get("state") == "active"
    if unit == "_kernel/resource" and resources:
        run = resources[0]
        name = _resource_run_label(run) or _text(_get(run, "display_title")) or \
            _text(_get(run, "route_node", "node", "run_id"))
        host = [[name, "now_sub"]] if name else [["resource", _SEG_DIM]]
        state = [(f"running {elapsed}" if running and elapsed else "resource", _SEG_DIM)]
        return ["resource", host, [list(s) for s in state]]
    if harness is None:
        return None
    # The session shell reads these segments too; Fleet draws them in its MODEL cell words.
    segs = [[harness, "hb_" + harness]]
    pair = _model_pair(node.get("model")) or (
        _model_pair(_get(owner, "resolved_model", "model")) if owner is not None else None)
    effort = _effort(node.get("effort")) or (_effort(_get(owner, "effort")) if owner is not None else None)
    if pair:
        segs += [[" (", _SEG_DIM], [pair[0], pair[1]]]
        if effort:
            segs += [["·", _SEG_DIM], [effort, "eff_" + effort if effort in ("medium", "xhigh") else _SEG_DIM]]
        segs.append([")", _SEG_DIM])
    state = []
    if state_text:
        state.append([state_text, _SEG_DIM])
    elif elapsed:
        role = "owner" if owner is not None else "worker"
        state.append([(f"running {elapsed}" if running else f"{elapsed} · {role} ended"), _SEG_DIM])
    if owner is not None:
        return ["owner", segs, state]
    return ["frame" if _is_frame_leg(node) else "worker", segs, state]


def _owner_jobs(jobs):
    """The route's depth-1 owner attempts. A one-shot route's owner runs its single
    ``one-shot`` node itself, so it carries that node id with ``worker_type=owner``;
    a frame leg's depth-1 attempt (``worker_type=frame``) is never the owner."""
    return [job for job in jobs
            if (_get(job, "depth") == 1 or _get(job, "dispatch_depth") == 1)
            and (not _get(job, "route_node") or _get(job, "worker_type") == "owner"
                 or _get(job, "route_node") == "one-shot")]


def _runs_node(owner, node):
    """True when the route's owner attempt is the one running ``node`` (one-shot)."""
    if owner is None or node is None or not _get(owner, "route_node"):
        return False
    attempt = _text(node.get("attempt_id")) or _text(_get(node.get("job"), "attempt_id"))
    return (_get(owner, "route_node") == node.get("id")
            and (attempt is None or attempt == _text(_get(owner, "attempt_id"))))


def _owner_job(jobs):
    owners = _owner_jobs(jobs)
    return owners[0] if len(owners) == 1 else None


def _route_stem(path):
    text = _text(path)
    return os.path.splitext(os.path.basename(text))[0] if text else None


def _chain_nodes(session_index, rid):
    """The owner session's route chain (oldest first) that holds route ``rid``."""
    for info in session_index.values():
        chain = _get(info["session"], "route_chain")
        nodes = [n for n in (_get(chain, "nodes", default=()) or ()) if isinstance(n, Mapping)] \
            if chain is not None else []
        if any(_text(n.get("route_id")) == rid for n in nodes):
            return nodes
    return []


def _chain_position(nodes, rid):
    return next((i for i, n in enumerate(nodes) if _text(n.get("route_id")) == rid), None)


def _chain_title(nodes, rid):
    """Only this route's recorded name proves its subject, not a previous task's name."""
    index = _chain_position(nodes, rid)
    if index is None:
        return None
    result = nodes[index].get("result")
    return _text(_get(result, "name")) if _get(result, "route_id") in (None, rid) else None


def _route_subject(rid, record, view, owner, result, chain_nodes):
    """Consume existing route-bound subject sources; never generate or prettify a slug."""
    from ..work_titles import readable
    locators = {_text(_get(item, "slug")) for item in (record, view, owner)} - {None}
    locators |= {readable(value) for value in locators}

    def subject(value):
        value = _text(value)
        return value if value and value.strip() and value not in locators else None

    recorded = subject(_get(result, "name")) if _get(result, "route_id") in (None, rid) else None
    recorded = recorded or subject(_chain_title(chain_nodes, rid))
    if recorded:
        return recorded
    # The record arrived through the existing verified projection. Do not use
    # a session's evolving title to name its unrelated historical routes.
    if record.get("route_id") != rid:
        return None
    named = subject(record.get("display_title")) or subject(record.get("title"))
    if named:
        return named
    work = _work(owner)
    attempt = _text(_get(owner, "attempt_id"))
    expected = _text(record.get("owner_attempt_id"))
    exact_owner = (
        attempt and expected in (None, "-", attempt)
        and _get(work, "source") == "route-exact" and _get(work, "route_id") == rid
        and record.get("route_hash") and _get(work, "route_hash") == record["route_hash"]
        and _get(owner, "_summary_sid") == "dispatch-" + attempt
        and not _get(owner, "association_ambiguity")
        and (not _get(owner, "_child_session_associated") or _get(owner, "_runtime_session_id"))
    )
    if exact_owner and subject(_get(owner, "title")):
        return subject(_get(owner, "title"))
    request = _get(record.get("work_request"), "text")
    if isinstance(request, str):
        # A sealed request's own first line is a literal fallback, not a model
        # summary or an extraction from accumulated session conversation.
        first = next((line.strip() for line in request.splitlines() if line.strip()), "")
        return subject(re.sub(r"^#{1,6}\s+", "", first))
    return None


def _selected_worker_job(node, jobs):
    """Resolve the body row to one job in this observation, never by native SID alone."""
    if not node or node.get("unit") == "_kernel/resource":
        return None
    anchor = node.get("job")
    attempt = _text(_get(anchor, "attempt_id")) or _text(node.get("attempt_id"))
    if not attempt:
        return None
    matches = [job for job in jobs if _get(job, "attempt_id") == attempt
               and _get(job, "route_node") == node.get("id")]
    if len(matches) != 1:
        return None
    job = matches[0]
    if node.get("harness") and node["harness"] != _get(job, "harness"):
        return None
    if node.get("unit") and _get(job, "worker_mode") and node["unit"] != _get(job, "worker_mode"):
        return None
    if anchor is not None:
        # Even a copied anchor must describe the same attempt, native session,
        # role and process generation. Conflicting/unknown observations stay separate.
        for field in ("route_id", "route_hash", "harness", "_runtime_session_id", "session_id",
                      "worker_type", "worker_mode", "depth", "pid", "proc_start"):
            if _get(anchor, field) != _get(job, field):
                return None
    return job


def _chain_leg_name(node):
    capability = _text(node.get("capability")) or ""
    return "frame" if capability == "route-frame" else _short_capability(capability) or "·"


def _chain_legs(nodes, rid, own, status, progress):
    """``[[leg, status, progress], ...]``: the frame that opened the route, the latest finished leg
    of every other capability since it, and the route's own leg."""
    index = _chain_position(nodes, rid)
    if index is None:
        return [[own, status, progress]] if own else []
    before = nodes[:index]
    frame = next((i for i in range(len(before) - 1, -1, -1)
                  if before[i].get("capability") == "route-frame"), None)
    legs = []
    if frame is not None:
        legs.append(["frame", "done" if before[frame].get("state") == "done" else "active", None])
    latest = {}
    for node in before[(frame + 1 if frame is not None else 0):]:
        name = _chain_leg_name(node)
        if name in ("frame", own):
            continue
        latest.pop(name, None)
        latest[name] = node
    for name, node in latest.items():
        legs.append([name, "done" if node.get("state") == "done" else "active", None])
    if own:
        legs.append([own, status, progress])
    return legs


def _clock_ago(now, minutes):
    """The clock time ``minutes`` before ``now`` (a route record counts minutes since the event)."""
    value = _num(minutes)
    if value is None or now is None:
        return None
    return _clock(now - value * 60)


def _model_segments(harness, model, effort):
    """``claude Opus 5.5 xhigh`` as segments: the session row's MODEL cell words (one function)."""
    from .tables import model_cell_parts
    return [[text, key] for text, key in model_cell_parts(harness, model, effort)]


def _model_words(model_name, effort):
    """``Opus 5.5 high``: the model and effort words of a MODEL cell, after the harness."""
    from .tables import _effort_word
    return " ".join(part for part in (model_name, _effort_word(effort) if effort else None) if part)


def _route_title_line(rid, owner, resume, role="owner"):
    harness = _text(owner.get("harness"))
    parts = [[f"  {rid[:11]} · {role} ", _SEG_DIM]]
    if harness:
        model, effort = owner.get("model"), owner.get("effort")
        parts += _model_segments(harness, model or None, effort if model else None)
    if resume:
        parts += [["  resumes from ", _SEG_DIM], [resume, "lvl_y"]]
    return parts


def _node_detail(node, now):
    """One stage row of the route page, from the route record's node."""
    state = _stage_state(node)
    harness = _text(node.get("harness"))
    model = _text(node.get("model"))
    effort = _effort(node.get("effort"))
    who = [harness, _model_words(model, effort)] if harness else None
    if state == "done":
        verdict = "PASS" if node.get("gate_passed") else "done"
    else:
        verdict = "worker ended" if node.get("note") == "dead-worker-blocked" else \
            {"failed": "failed", "active": "running", "todo": "pending"}[state]
    deps = [str(d) for d in (node.get("depends_on") or ())]
    clock = _clock_ago(now, node.get("elapsed_min")) if state != "todo" else None
    if clock:
        when = {"done": "done ", "failed": "ended ", "active": "since "}[state] + clock
    else:
        when = "after " + ", ".join(deps) if deps and state == "todo" else "·"
    rounds = _num(node.get("route_attempt_round")) or _num(node.get("attempt_round"))
    result = {
        "n": _text(node.get("id")) or "·", "st": state, "verdict": verdict, "who": who,
        "when": when, "rounds": [],
        "rounds_note": (ui_text("fleet.route.rounds_note", round=int(rounds), earlier=int(rounds) - 1)
                        if rounds and rounds > 1 else ""),
        "unit": " · ".join(p for p in (_text(node.get("unit")), _text(node.get("model_profile"))) if p) or "·",
        "gate": _text(node.get("gate")),
        "out": " · ".join(str(p) for p in (node.get("write_scope") or ())) or "·",
        "says": _text(node.get("says")),
    }
    if "depends_on" in node or node.get("parallel_group") is not None:
        result["graph_known"] = True
        result["depends_on"] = tuple(deps)
    if node.get("parallel_group") is not None:
        result["parallel_group"] = str(node.get("parallel_group"))
    return result


def _parallel_stage_details(view_nodes, now):
    details = [_node_detail(node, now) for node in view_nodes]
    if not any(item.get("graph_known") for item in details):
        return details
    index_by_id = {str(node.get("id")): index for index, node in enumerate(view_nodes)
                   if _text(node.get("id"))}
    levels = [None] * len(view_nodes)
    for _ in range(len(view_nodes) + 1):
        changed = False
        for index, detail in enumerate(details):
            deps = [index_by_id[dep] for dep in detail.get("depends_on", ())
                    if dep in index_by_id]
            if not deps:
                level = 0
            elif all(levels[dep] is not None for dep in deps):
                level = 1 + max(levels[dep] for dep in deps)
            else:
                continue
            if levels[index] != level:
                levels[index] = level
                changed = True
        if not changed:
            break
    levels = [0 if level is None else level for level in levels]
    by_level = {}
    parallel = {}
    for index, (node, detail, level) in enumerate(zip(view_nodes, details, levels)):
        by_level.setdefault(level, []).append(index)
        group = _text(node.get("parallel_group"))
        if group:
            parallel.setdefault((level, group), []).append(index)
    group_at = {indices[0]: indices for indices in parallel.values() if len(indices) == 2}
    consumed, ordered = set(), []
    for level in sorted(by_level):
        for index in by_level[level]:
            if index in consumed:
                continue
            members = group_at.get(index)
            if members:
                consumed.update(members)
                child_stages = []
                for member in members:
                    child = dict(details[member])
                    child["level"] = level
                    child["members"] = (child["n"],)
                    child_stages.append(child)
                deps = tuple(dict.fromkeys(dep for child in child_stages
                                           for dep in child.get("depends_on", ())))
                ordered.append({"group": tuple(child_stages), "members": tuple(
                    child["n"] for child in child_stages), "depends_on": deps,
                    "parallel_group": child_stages[0].get("parallel_group"),
                    "graph_known": True, "level": level})
            else:
                consumed.add(index)
                stage = dict(details[index])
                stage["level"] = level
                stage["members"] = (stage["n"],)
                ordered.append(stage)
    return ordered


def _frame_leg_job_rank(job):
    live = 1 if _get(job, "liveness") in ("working", "idle", "blocked") else 0
    round_value = _num(_get(job, "route_attempt_round")) or _num(_get(job, "attempt_round")) or 0
    elapsed = _num(_get(job, "elapsed_min"))
    return (live, round_value, -(elapsed if elapsed is not None else 0))


def _frame_leg_displays(jobs):
    """Display-only harness/model/effort per frame leg, from the leg's own job (newest live wins)."""
    found = {}
    for job in jobs:
        node_id = _text(_get(job, "route_node"))
        if not (node_id in FRAME_NODES or _text(_get(job, "worker_type")) == "frame"):
            continue
        key = node_id if node_id in FRAME_NODES else "frame"
        rank = _frame_leg_job_rank(job)
        if key not in found or rank > found[key][0]:
            found[key] = (rank, {"harness": _text(_get(job, "harness")),
                                 "model": _model_pair(_get(job, "resolved_model", "model")),
                                 "effort": _effort(_get(job, "effort")),
                                 "rank": rank})
    return {key: value for key, (_rank, value) in found.items()}


def _display_nodes(nodes, frame_displays):
    """Copy frame leg nodes that lack telemetry and fill it from the leg's job, for display only."""
    if not frame_displays:
        return nodes
    out = []
    for node in nodes:
        info = frame_displays.get(_text(node.get("id")))
        if info and (not _text(node.get("harness")) or node.get("model") is None):
            merged = dict(node)
            if info["harness"] and not _text(merged.get("harness")):
                merged["harness"] = info["harness"]
            if info["model"] is not None and merged.get("model") is None:
                merged["model"] = info["model"][0]
            if info["effort"] and not _effort(merged.get("effort")):
                merged["effort"] = info["effort"]
            out.append(merged)
        else:
            out.append(node)
    return out


def _context_tokens(job):
    """Exact active-context tokens of one attempt, or None (never derived from a percent)."""
    value = _get(job, "active_context_tokens")
    return value if type(value) is int and value >= 0 else None


def _route_doer(worker, owner, dead):
    """Who does the route's work now, for its one-line row: harness, model, effort, context
    (percent, window, or the exact token count when no window is known), the open tool and
    the live sub-agent count. The live worker of the shown stage wins; otherwise the owner
    attempt (a one-shot route's owner is its worker). A dead route keeps the harness only."""
    job = worker if worker is not None else owner
    if job is None:
        return None
    harness = _text(_get(job, "harness"))
    if dead:
        return {"harness": harness, "model": None, "effort": None, "context": None,
                "window": None, "tokens": None, "tool": None, "agents": 0}
    pct, window = _context_gauge(job)
    tool = _get(job, "exec_tool")
    tool_name = _text(tool.get("name")) if isinstance(tool, Mapping) else None
    live = _get(job, "liveness") == "working"
    agents = sum(1 for item in (_get(job, "subagents") or ())
                 if _get(item, "active") is True)
    return {"harness": harness, "model": _model_pair(_get(job, "resolved_model", "model")),
            "effort": _effort(_get(job, "effort")), "context": pct, "window": window,
            "tokens": _context_tokens(job) if pct is None else None,
            "tool": tool_name if live else None, "agents": agents if live else 0}


def _route_records(snapshot, session_rows, session_index, resolver, now):
    collected = _collect_routes(snapshot)
    resources = _records(_get(snapshot, "resources", default=()))
    tag_by_sid = {}
    for tag, info in session_index.items():
        for known in info["ids"]:               # the live id and the ids it answers to after a resume
            tag_by_sid.setdefault(known, tag)
    by_tag = {row["tag"]: row for row in session_rows}
    routes, nodes = [], {}
    for rid, slot in collected.items():
        view = slot["view"] or {}
        record = slot["record"] or {}
        jobs = slot["jobs"]
        expected_owner = _text(record.get("owner_attempt_id"))
        if expected_owner == "-":
            expected_owner = None
        owner_jobs = [job for job in jobs
                      if not expected_owner or _get(job, "attempt_id") == expected_owner]
        owner = _owner_job(owner_jobs)
        if owner is None and expected_owner:
            exact_sessions = [session for session in slot["sessions"]
                              if _get(session, "attempt_id") == expected_owner
                              and _get(_work(session), "source") == "route-exact"]
            if len(exact_sessions) == 1:
                owner = exact_sessions[0]
        owner_tag = None
        for session in slot["sessions"]:
            tag = _text(_get(session, "session_tag"))
            if tag in by_tag:
                owner_tag = tag
                break
        if owner_tag is None:
            owner_tag = next((tag for tag, info in session_index.items() if rid in info["chain"]), None)
        if owner_tag is None and owner is not None:
            for key in ("parent_sid", "caller_sid"):
                tag = tag_by_sid.get(_text(_get(owner, key)))
                if tag:
                    owner_tag = tag
                    break
        session_row = by_tag.get(owner_tag) if owner_tag else None
        view_nodes = [n for n in (view.get("nodes") or ()) if isinstance(n, Mapping)]
        frame_displays = _frame_leg_displays(jobs)
        view_nodes = _display_nodes(view_nodes, frame_displays)
        stages = [_stage_record(node) for node in view_nodes]
        done = sum(1 for node in view_nodes if node.get("state") == "done")
        failed = any(node.get("state") == "failed" for node in view_nodes)
        active = next((n for n in view_nodes if n.get("state") in ("active", "reconciling", "recovering")), None)
        total = len(view_nodes)
        capability = _text(view.get("capability")) or _text(record.get("capability"))
        if capability is None:
            capability = next((_text(_get(job, "capability_owner", "assigned_contract")) for job in jobs
                               if _text(_get(job, "capability_owner", "assigned_contract"))), None)
        # The display identity of a frame route is derived here, once: KIND, tree,
        # opened-route shape and the framed PROGRESS all read these route fields.
        framed_route = _is_frame_route(
            capability, record,
            _text(view.get("execution_topology")) or _text(record.get("execution_topology")))
        short = "frame" if framed_route else _short_capability(capability)
        cwd = _text(view.get("cwd")) or _text(record.get("cwd")) or _text(_get(owner, "cwd"))
        repo = (session_row or {}).get("repo") or _repo_of(resolver, cwd)
        owner_state = (session_row or {}).get("state")
        owner_live = _text(_get(owner, "liveness")) if owner is not None else None
        if owner_live in ("working", "idle", "blocked", "dead", "done"):
            state = owner_live                    # the route is as alive as the attempt that owns it
        elif active is not None:
            state = "working"
        elif total and done == total:
            state = "done"
        elif failed:
            state = "dead"
        elif owner_state == "blocked":
            state = "blocked"
        else:
            state = "idle"
        # The route reader's own verdict that a route still marked in flight has nothing of it
        # running (``route.route_liveness``): shown as no response, never as working, never closed.
        no_response = state in ("working", "idle") and _text(view.get("liveness")) == "unknown"
        if no_response:
            state = "stale"
        shown_node = active or next((n for n in view_nodes if n.get("state") != "done"), None) \
            or (view_nodes[-1] if view_nodes else None)
        run_children = [r for r in resources if rid in (_text(_get(r, "route_id")), _route_stem(_get(r, "route")))
                        and _get(r, "liveness") == "working"]
        elapsed_values = [v for v in (_num(_get(job, "elapsed_min")) for job in jobs) if v is not None]
        age = _dur(max(elapsed_values)) if elapsed_values else None
        topology = _text(view.get("execution_topology")) or _text(record.get("execution_topology"))
        resource_node = shown_node is not None and shown_node.get("unit") == "_kernel/resource"
        shape = FRAME_ROUTE_SHAPE if framed_route else topology
        if not framed_route and topology == "staged" and resource_node:
            shape = "staged+resource"
        leg_status = "failed" if (failed and active is None) or state == "dead" else \
            "done" if state == "done" or (total and done == total) else "active"
        progress = f"{done}/{total}" if total else None
        worker_node, state_text = shown_node, None
        if no_response:
            state_text = "no work running"
        if state == "dead" and shown_node is not None and _stage_state(shown_node) == "todo":
            earlier = [n for n in view_nodes if _stage_state(n) in ("done", "failed")]
            if earlier:                            # the owner ended between stages: its last worker
                worker_node = earlier[-1]
                state_text = f"{_dur(worker_node.get('elapsed_min')) or ''} ⊸ · next " \
                             f"{shown_node.get('id')} · owner ended".strip()
        worker = _worker_segments(worker_node, run_children, state_text,
                                  owner if _runs_node(owner, worker_node) else None)
        dead = state in ("dead", "stale")
        owner_model = None if dead else (
            _model_pair(_get(owner, "resolved_model", "model")) if owner is not None else None)
        owner_harness = _text(_get(owner, "harness")) if owner is not None else None
        owner_effort = None if dead else (
            _effort(_get(owner, "effort")) if owner is not None else None)
        worktree = None
        if cwd and ("-wt" in cwd or "/.worktrees/" in cwd):
            worktree = os.path.basename(cwd.rstrip("/")) or None
        result = _get(_get(owner, "work_projection"), "result") if owner is not None else None
        if result is None:
            result = _get(record, "result")
        chain_nodes = _chain_nodes(session_index, rid)
        if result is None:
            match = next((node for node in chain_nodes if _get(node, "route_id") == rid), None)
            if match is not None and _get(match, "result") is not None:
                result = _get(match, "result")
        title = _route_subject(rid, record, view, owner, result, chain_nodes)
        worker_stage = _text(worker_node.get("id")) if worker_node else None
        worker_unit = _text(worker_node.get("unit")) if worker_node else None
        # Navigation ancestry never authorizes metrics from the parent session.
        # Each attempt uses the same applied-policy/native gauge as a session.
        owner_gauge = _context_gauge(owner) if owner is not None else (None, None)
        owner_tokens = (_context_tokens(owner) if owner is not None and owner_gauge[0] is None
                        and _get(owner, "liveness", "status", "state", "st") not in ("stale", "dead")
                        else None)
        worker_contexts = []
        context_jobs = []
        selected_worker = _selected_worker_job(worker_node, jobs)
        worker_context_index = None
        for job in jobs:
            node_id = _text(_get(job, "route_node"))
            if not node_id or _get(job, "liveness") not in ("working", "idle", "blocked"):
                continue
            node = next((n for n in view_nodes if n.get("id") == node_id), None)
            if node is None or node.get("state") not in ("active", "reconciling", "recovering"):
                continue
            attempt = _text(_get(job, "attempt_id"))
            anchor = next((item for item in jobs
                           if _get(item, "attempt_id") == node.get("attempt_id")), None)
            anchor_meta = _get(anchor, "_registry_metadata") or {}
            job_meta = _get(job, "_registry_metadata") or {}
            parallel_sibling = (
                isinstance(anchor_meta, dict) and isinstance(job_meta, dict)
                and bool(anchor_meta.get("session_chain_id"))
                and bool(anchor_meta.get("parent_attempt_id"))
                and all(meta.get("subsession_mode") == "parallel"
                        and meta.get("stage_authority") == "0"
                        and meta.get("route_id") == rid and meta.get("route_node") == node_id
                        for meta in (anchor_meta, job_meta))
                and all(anchor_meta.get(key) == job_meta.get(key)
                        for key in ("session_chain_id", "parent_attempt_id"))
                and (not expected_owner or job_meta.get("parent_attempt_id") == expected_owner)
            )
            if not parallel_sibling and (
                    node.get("attempt_id") and node.get("attempt_id") != attempt
                    or node.get("harness") and node.get("harness") != _get(job, "harness")):
                continue
            # Multiple registered sub-sessions are individual rows, never one
            # latest/aggregate context value for the whole stage.
            pct, window = _context_gauge(job) if attempt else (None, None)
            if job is selected_worker:
                worker_context_index = len(worker_contexts)
            context_jobs.append(job)
            worker_contexts.append({"stage": node_id, "harness": _get(job, "harness"),
                                    "model": _model_pair(_get(job, "resolved_model", "model")),
                                    "context": pct, "window": window})
        resume = None
        if state == "dead":
            resume = next((n.get("id") for n in view_nodes if _stage_state(n) == "failed"), None) or \
                next((n.get("id") for n in view_nodes if _stage_state(n) == "todo"), None)
        legs = _chain_legs(chain_nodes, rid, short, leg_status, progress) if chain_nodes else \
            ([[short or "·", leg_status, progress]] if short else [])
        # Borrow a frame leg's identity only while the route has no owner job at all: several
        # owner attempts (a resumed route without a recorded owner_attempt_id) keep their own
        # display instead of borrowing a frame leg's harness.
        borrowed_frame_identity = bool(framed_route and not _owner_jobs(owner_jobs)
                                       and frame_displays)
        if borrowed_frame_identity:
            preferred = max(frame_displays.values(), key=lambda item: item.get("rank", ()))
            owner_harness = preferred.get("harness") or owner_harness
            owner_model = preferred.get("model") or owner_model
            owner_effort = preferred.get("effort") or owner_effort
        mode = _text(view.get("capability_mode")) or _text(record.get("capability_mode"))
        intensity = _text(view.get("effective_intensity")) or _text(record.get("effective_intensity"))
        if framed_route:
            # KIND reads `frame`: a frame route's default capability mode and inherited
            # intensity are not display axes. The source record keeps both values.
            mode = intensity = None
        current = bool(run_children or worker_contexts
                       or owner_live in ("working", "idle", "blocked")
                       and total and done != total and (active is not None or not failed))
        running = current and (active is not None or state == "working" or run_children or worker_contexts)
        # 3cffaf: while a live worker holds the stage, the route row shows the owner's own
        # state. An owner that is idle, or whose running call is only a wait, has delegated
        # and is waiting; one doing its own work still reads as running.
        owner_self = owner_live if owner is not None else owner_state
        owner_raw = owner if owner is not None else (session_index.get(owner_tag) or {}).get("session")
        owner_waits = bool(running and (worker_contexts or run_children) and (
            owner_self == "idle"
            or owner_self == "working" and exec_child_is_wait(_get(owner_raw, "exec_child"))))
        route = {
            "id": rid, "repo": repo, "session_tag": owner_tag, "title": title, "state": state,
            "outcome": _text(_get(result, "result", "status")),
            "result_at": _num(_get(result, "at")),
            "worker": worker, "age": age, "legs": legs,
            "stages": stages, "resume": resume, "activity": "running" if running else "ended",
            "loops": None, "worktree": worktree, "tree_label": short,
            "harness": owner_harness, "model": owner_model, "effort": owner_effort,
            "mode": mode,
            "intensity": intensity,
            "profile": _text(_get(owner, "model_profile", "profile")) if owner is not None else None,
            "capability": short, "shape": shape, "past_from": None,
            "worker_stage": worker_stage or "·", "worker_unit": worker_unit,
            "worker_stage_key": None, "worker_step_key": None,
            # Without a gauge, the owner's exact token count (no window: nothing is estimated).
            "owner_context": ([owner_tokens, None] if owner_tokens is not None else None),
            "owner_gauge": list(owner_gauge),
            "worker_contexts": worker_contexts, "subworker": None,
            "current": current, "owner_waits": owner_waits,
        }
        if worker_context_index is not None:
            route["worker_context_index"] = worker_context_index
        doer_job = (selected_worker if worker_context_index is not None
                    else context_jobs[0] if len(context_jobs) == 1 else None)
        doer = _route_doer(doer_job, owner, dead)
        if doer is not None:
            route["doer"] = doer
        if record:
            # Display only: shape and its basis, stages, verification and next step, read
            # from this route's own record, Fleet's view and its outcome sidecar.
            # A solo route's owner is its one-shot node's own attempt, not a separate row.
            depth_one = [job for job in owner_jobs
                         if _get(job, "depth") == 1 or _get(job, "dispatch_depth") == 1]
            shown_owner = owner if owner is not None else (
                max(depth_one, key=current_attempt_rank) if depth_one else None)
            route["visibility"] = route_visibility.bounded(route_visibility.describe(
                record, view, owner=route_visibility.owner_from_job(shown_owner)))
            # A route its supervisor closed (``capability-route close``, the outcome sidecar's
            # ``closed_at``) is over even when its owner ended without a passed gate: it is
            # neither a stopped leg to count nor one to resume.
            codes = route["visibility"].get("codes") if isinstance(route["visibility"], Mapping) else None
            verification = codes.get("verification") if isinstance(codes, Mapping) else None
            if isinstance(verification, Mapping) and verification.get("closed_at"):
                route["closed"] = True
                route["resume"] = None
        routes.append(route)
        nodes[rid] = _node_groups(view_nodes, route, rid, legs, now, done, leg_status,
                                  borrowed_frame_identity)
    return routes, nodes


def _node_groups(view_nodes, route, rid, legs, now, done, leg_status, borrowed_identity=False):
    """One leg group holding the route's stages: the detail the route page prints per stage."""
    if not view_nodes:
        return []
    detail = _parallel_stage_details(view_nodes, now)
    short, mode = route.get("capability"), route.get("mode")
    name = f"leg {len(legs) or 1} · {short or '·'}{' ' + mode if mode else ''}"
    frame_nodes = [node for node in view_nodes if _is_frame_leg(node)]
    if frame_nodes and (short == "frame" or route.get("shape") == FRAME_ROUTE_SHAPE):
        lines = []
        for node in frame_nodes:
            node_id = str(node.get("id") or "frame")
            short_id = "alt" if node_id == "frame-alternative" else node_id
            line = [[short_id + "  ", "dim"]]
            harness = _text(node.get("harness"))
            if harness:
                model = _stage_model(node.get("model"))
                effort = _effort(node.get("effort"))
                line += _model_segments(harness, model or None, effort if model else None)
            lines.append(line)
        frame_name = " ∥ ".join(str(node.get("id") or "frame") for node in frame_nodes)
        # The band's identity can be a borrowed frame leg; only a real owner attempt
        # is called `owner`, the borrowed case uses the leg's own role word.
        role = "frame" if borrowed_identity else "owner"
        return [{
            "kind": "frame", "n": frame_name, "st": leg_status,
            "verdict": f"{done}/{len(view_nodes)}", "lines": lines, "detail": detail,
            "title": [[frame_name, "head"]] + _route_title_line(rid, route, route.get("resume"), role),
            "loops": [],
        }]
    owner_line = [["owner  ", "dim"]]
    if route.get("harness"):
        owner_line.append([route["harness"], "hb_" + route["harness"]])
    model = route.get("model")
    if model:
        owner_line.append([" " + _model_words(model[0], route.get("effort")), "soft"])
    if route.get("age"):
        owner_line.append([" · " + route["age"], "dim"])
    return [{
        "kind": "leg", "n": name, "st": leg_status, "verdict": f"{done}/{len(view_nodes)}",
        "lines": [owner_line], "detail": detail,
        "title": [[name, "head"]] + _route_title_line(rid, route, route.get("resume")), "loops": [],
    }]


# -- repos / questions / peers --------------------------------------------------------------

def _ago(epoch, now):
    """``41m ago`` / ``6h ago`` / ``3d ago`` (the default board's age words); None when unknown."""
    value = _num(epoch)
    if value is None or now is None or not 0 <= value <= now:
        return None
    minutes = int((now - value) // 60)
    if minutes >= 1440:
        return f"{minutes // 1440}d ago"
    if minutes >= 60:
        return f"{minutes // 60}h ago"
    return f"{minutes}m ago"


def _repo_results(snapshot, resolver, now):
    """repo -> its newest recorded result ``{"name", "result", "at"}``, the way the default board's
    project header finds it (work projection result, route chain node results, resource exits)."""
    try:
        from .. import render
        group_last_result = render._group_last_result
    except Exception:
        return {}
    groups = {}
    sessions = _records(_get(snapshot, "sessions", default=()))
    jobs = _records(_get(snapshot, "jobs", default=()))
    resources = _records(_get(snapshot, "resources", default=()))
    repo_of_sid = {}
    for session in sessions:
        repo = _repo_of(resolver, _get(session, "cwd", "project"), _get(session, "repo", "slug"))
        for known in (_get(session, "session_id", "sid", "id"), *(_get(session, "session_aliases") or ())):
            if _text(known):
                repo_of_sid[known] = repo
        groups.setdefault(repo, ([], [], []))[0].append(session)
    for job in jobs:
        repo = repo_of_sid.get(_text(_get(job, "parent_sid"))) or _repo_of(
            resolver, _get(job, "parent_cwd", "cwd"), None)
        groups.setdefault(repo, ([], [], []))[1].append(job)
    for resource in resources:
        repo = _repo_of(resolver, _get(resource, "cwd"), _get(resource, "project"))
        groups.setdefault(repo, ([], [], []))[2].append(resource)
    found = {}
    for repo, (g_sessions, g_jobs, g_resources) in groups.items():
        try:
            result = group_last_result(g_sessions, g_jobs, g_resources)
        except Exception:
            result = None
        if result:
            found[repo] = result
    return found


def stewards_first(rows):
    """A stewarding session leads its repository; every other row keeps its order.

    The monitor board has led each repo group with its steward since F-100c (user
    2026-09-03, again 2026-10-06: "감독 세션을 fleet에서 가장 상단에"). The explorer
    kept only first-seen order, so the rule is applied here once for every view.
    """
    rows = list(rows)
    return ([row for row in rows if row.get("steward")]
            + [row for row in rows if not row.get("steward")])


def _repo_records(session_rows, routes, results=None, now=None):
    order = []
    for row in session_rows:
        if row["repo_id"] not in order:
            order.append(row["repo_id"])
    for route in routes:
        if route["repo"] is not None and route["repo"] not in order:
            order.append(route["repo"])
    order.sort(key=lambda repo: repo == UNKNOWN_REPO)      # the nameless group goes last
    repos = []
    for repo in order:
        result = (results or {}).get(repo)
        if result and result.get("result") != "success":
            result = None
        if result:
            done, age = _text(result.get("name")), _ago(result.get("at"), now)
            done_at = _num(result.get("at"))
        else:
            finished = next((r for r in routes if r["repo"] == repo and r["state"] == "done" and r["title"]), None)
            done, age = (finished["title"] if finished else None), None
            done_at = (finished or {}).get("result_at")
        repos.append({"id": repo, "name": repo, "done": done, "age": age,
                      "done_at": done_at,
                      "done_result": result.get("result") if result else (finished or {}).get("outcome")})
    return repos


STATUS_STALE_DAYS = 3          # an older status document gets its age in front of its line


def _repo_status(repos, snapshot, session_rows, session_index, routes, questions, results, now):
    """Attach each named project's status line (D4 #3) to its repo record as ``status``.

    The project's status document (``collectors.project_status``, read by the detail pass)
    gives the line, the count waiting on the person and the next step. Without one, the
    facts Fleet already holds stand in: routes with live work (``current``, the same test
    as a session row's ``route N``), questions waiting, and the newest completed result.
    A project with none of these gets no ``status``.
    """
    documents = _get(snapshot, "project_status")
    documents = documents if isinstance(documents, Mapping) else {}
    waits = _get(snapshot, "answer_waits")
    raw_waits = _get(waits, "items", default=()) if isinstance(waits, Mapping) else ()
    raw_by_id = {str(item.get("id")): item for item in raw_waits
                 if isinstance(item, Mapping) and item.get("id") and not item.get("older")}
    cwds = {}
    for row in session_rows:
        cwd = _text(_get(session_index[row["tag"]]["session"], "cwd"))
        if cwd:
            cwds.setdefault(row["repo_id"], []).append(cwd)
    for repo in repos:
        name = repo["id"]
        if name == UNKNOWN_REPO:
            continue
        doc = None
        for cwd in cwds.get(name, ()):
            found = documents.get(cwd)
            if (isinstance(found, Mapping) and _num(found.get("mtime")) is not None
                    and (doc is None or found["mtime"] > doc["mtime"])):
                doc = found
        open_routes = sum(1 for route in routes if route["repo"] == name and route.get("current"))
        asked = sum(1 for item in questions if item.get("project") == name and not item.get("older"))
        result = (results or {}).get(name) or {}
        done_at = _num(result.get("at")) if result.get("result") == "success" else None
        if doc is None and not (open_routes or asked or repo.get("done")):
            continue
        status = {"routes": open_routes, "questions": asked,
                  "done": repo.get("done"), "done_age": repo.get("age")}
        question_lines = []
        for question in questions:
            if question.get("project") != name or question.get("older"):
                continue
            source = raw_by_id.get(str(question.get("id")))
            value = source.get("question") if isinstance(source, Mapping) else None
            if not isinstance(value, str) and isinstance(source, Mapping):
                detail = source.get("detail")
                listed = detail.get("questions") if isinstance(detail, Mapping) else None
                value = (listed[0].get("question") if isinstance(listed, (list, tuple))
                         and listed and isinstance(listed[0], Mapping) else None)
            first = str(value or "").splitlines()[0:1]
            if first and first[0].strip():
                question_lines.append(first[0].strip()[:100])
        if question_lines:
            status["question_lines"] = question_lines[:3]
        if open_routes:
            by_state = route_state_counts(route for route in routes if route["repo"] == name)
            status.update({"routes_running": by_state["running"],
                           "routes_waiting": by_state["waiting"],
                           "routes_stopped": by_state["stopped"]})
        if doc is None:
            status["changed"] = repo.get("age")
        else:
            decisions = [str(item) for item in doc.get("decisions") or () if _text(item)]
            following = [str(item) for item in doc.get("next") or () if _text(item)]
            doc_at = doc["mtime"]
            stale = now is not None and now - doc_at >= STATUS_STALE_DAYS * 86400
            status.update({
                "line": _text(doc.get("line")) or _text(doc.get("title")) or doc.get("name"),
                "decisions": len(decisions), "decision": decisions[0] if decisions else None,
                "next": following[0] if following else None,
                "doc": _text(doc.get("relative")) or _text(doc.get("name")),
                "doc_age": _ago(doc_at, now),
                "stale": bool(stale),
                "changed": _ago(max(doc_at, done_at or 0), now)})
            status["campaigns"] = [dict(item, doc_age=_ago(item.get("mtime"), now),
                stale=bool(now is not None and _num(item.get("mtime")) is not None
                           and now - item["mtime"] >= STATUS_STALE_DAYS * 86400))
                for item in tuple(doc.get("campaigns") or ())[:3]]
        repo["status"] = status


_OWNER_STATES = frozenset(("live", "successor", "none", "unknown"))


def _clock_or_date(epoch, now):
    """``21:40`` today, ``10-26 21:40`` on another day; None when unreadable."""
    value = _num(epoch)
    if value is None:
        return None
    try:
        when = datetime.datetime.fromtimestamp(value)
        today = datetime.datetime.fromtimestamp(now).date() if _num(now) is not None else None
    except (OverflowError, OSError, ValueError):
        return None
    return when.strftime("%H:%M" if when.date() == today else "%m-%d %H:%M")


def _question_records(snapshot, session_rows, session_index, now, routes=(), resolver=None):
    """Project only current, body-free records from the shared answer-wait cache.

    Each record says who answers it (the session row, or why there is none), when it began, which
    route it belongs to and which gate (``node``) it waits on. ``owner_state`` and ``fold`` are the
    answer-wait producer's own verdicts; nothing here judges a wait by its age.
    """
    waits = _get(snapshot, "answer_waits")
    if not isinstance(waits, Mapping):
        return [], {"state": "unknown", "total": None, "older": None}
    route_by_id = {str(route.get("id")): route for route in routes or ()
                   if isinstance(route, Mapping) and route.get("id")}
    tag_by_sid = {known: tag for tag, info in session_index.items()
                  for known in (info.get("ids") or ())}

    complete = waits.get("complete") is True
    health = tuple(item for item in (waits.get("health") or ()) if isinstance(item, Mapping))
    failed = any(str(item.get("state", "")).lower() in ("failed", "error") for item in health)
    state = "complete" if complete and not failed else "failed" if failed else "incomplete"
    by_identity = {}
    for row in session_rows:
        row["ask"] = None
        row["answer_wait_marker"] = None
        info = session_index.get(row["tag"], {})
        sid = _text(info.get("sid"))
        harness = _text(_get(info.get("session"), "harness", "adapter", "h"))
        if sid and harness:
            by_identity[(harness, sid)] = row

    questions, seen = [], set()
    for index, item in enumerate(waits.get("items", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        stale = item.get("stale")
        older = stale == "older"
        source = _text(item.get("source"))
        if stale not in (False, "current") and not older:
            continue
        ident = _text(item.get("id"))
        if ident and ident in seen:
            continue
        if ident:
            seen.add(ident)
        owner = item.get("owner") if isinstance(item.get("owner"), Mapping) else {}
        harness = _text(item.get("harness")) or _text(owner.get("harness"))
        sid = _text(item.get("session_id")) or _text(owner.get("session_id"))
        session = by_identity.get((harness, sid)) if harness and sid else None
        tag = ident or f"current-{index}"
        ref = {"id": ident} if ident else {}
        for name, value in (("harness", harness), ("session_id", sid),
                            ("request_id", item.get("request_id")),
                            ("turn_id", item.get("turn_id")),
                            ("route_id", item.get("route_id")),
                            ("question_id", item.get("question_id"))):
            if name == "request_id" and type(value) is int:
                ref[name] = value
                continue
            if name == "request_id" and not isinstance(value, str):
                continue
            value = _text(value)
            if value:
                ref[name] = value
        owner_state = _text(item.get("owner_state"))
        owner_state = owner_state if owner_state in _OWNER_STATES else None
        fold = _text(item.get("fold"))
        fold = fold if older and fold in ("older", "ownerless") else ("older" if older else None)
        route_id = _text(item.get("route_id"))
        route = route_by_id.get(route_id) if route_id else None
        cwd = _text(item.get("cwd"))
        project = (session["repo"] if session else
                   _repo_of(resolver, cwd) if cwd and resolver is not None else None)
        registered_sid = _text(item.get("registered_session_id"))
        registered_tag = tag_by_sid.get(registered_sid) if registered_sid else None
        # The original asker by its own tag; within the same seat (a cleared session) by its id.
        registered = (f"[{registered_tag}]" if registered_tag and (not session or registered_tag != session["tag"])
                      else f"session {registered_sid[:8]}" if registered_sid else None)
        record = {
            "id": ident, "tag": tag, "h": session["harness"] if session else harness,
            "m": session.get("model") if session else None,
            "e": session.get("effort") if session else None,
            "ref": ref,
            "older": older,
            "kind": _text(item.get("kind")) or source or "wait",
            "source": source, "wait": _since(item.get("since"), now),
            "project": project,
            "session_tag": session["tag"] if session else None,
            "open_tag": session["tag"] if session else None,
            "pane": session["pane"] if session else None,
            "pane_server": session.get("pane_server") if session else None,
            "marker": "◌" if owner_state == "none" else "◑" if source == "S2" else "●",
            "node": _text(item.get("node")),
            "since_at": _clock_or_date(item.get("since"), now),
            "route_id": route_id,
            "route_title": _text(route.get("title")) if route else None,
            "owner_state": owner_state,
            "owner_sid": sid[:8] if sid and not session else None,
            "registered": registered if owner_state == "successor" else None,
            "fold": fold,
        }
        questions.append(record)
        if session and not older:
            session["ask"] = "answer needed"
            marker = "◑" if source == "S2" else "●"
            if session.get("answer_wait_marker") != "●":
                session["answer_wait_marker"] = marker

    total = waits.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = None
    older = waits.get("older")
    if isinstance(older, bool) or not isinstance(older, int) or older < 0:
        older = None
    ownerless = waits.get("ownerless")
    if isinstance(ownerless, bool) or not isinstance(ownerless, int) or ownerless < 0:
        ownerless = None
    summary = {"state": state, "total": total if state == "complete" else None,
               "older": older if state == "complete" else None}
    if ownerless is not None and state == "complete":
        summary["ownerless"] = ownerless
    return questions, summary


_RECEIPTS = frozenset({"herdr steer received", "Claude native context received"})
_HANDOVER_RECEIPT = re.compile(r"old_session=([0-9a-f-]+).*?new_session=([0-9a-f-]+)")


def _handover_links(snapshot, session_index, seat_rows, now):
    """``gen N → [tag]`` handover links: a clear that continued the work in a live session."""
    observations = _get(snapshot, "observations", default={}) or {}
    joined = _get(observations, "replay_sources", default={}) or {}
    records = _get(_get(joined, "peer_messages", default={}) or {}, "records", default=()) or ()
    tag_by_sid = {known: tag for tag, info in session_index.items() for known in info["ids"]}
    generation = {seat[0]: seat[2] for seat in seat_rows}
    links = []
    for message in records:
        if not isinstance(message, Mapping) or not (_text(message.get("summary")) or "").startswith(
                "[notice] action=continue"):
            continue
        receipt = _text(_get(message.get("delivery"), "receipt")) or ""
        found = _HANDOVER_RECEIPT.search(receipt)
        tag = tag_by_sid.get(found.group(2)) if found else None
        if not tag:
            continue
        count = generation.get(tag)
        source = f"gen {count - 1}" if count else found.group(1)[:8]
        words = (_text(message.get("summary")) or "").split(" true ", 1)[-1].strip()
        links.append((_epoch_of(message.get("ts")) or 0.0, [
            source, [tag, _text(_get(session_index[tag]["session"], "harness")) or "claude"], "handover",
            _iso_age(message.get("ts"), now),
            f"clear → {words} ({found.group(1)[:8]} → {found.group(2)[:8]})".replace("  ", " ")]))
    newest = {}
    for stamp, link in links:
        key = link[1][0]
        if key not in newest or stamp > newest[key][0]:
            newest[key] = (stamp, link)
    return list(newest.values())


_JOB_STATE = {"done": "done", "working": "working", "idle": "idle", "dead": "dead", "blocked": "blocked"}


def _routeless_jobs(snapshot, session_index):
    """Dispatch jobs outside any route (a session-tidy run) with the live session that called them."""
    tag_by_sid = {known: tag for tag, info in session_index.items() for known in info["ids"]}
    found = []
    for job in _records(_get(snapshot, "jobs", default=())):
        if _text(_get(_get(job, "work_projection"), "route_id")) or _text(_get(job, "route_id", "owner_route_id")):
            continue
        tag = tag_by_sid.get(_text(_get(job, "caller_sid", "parent_sid")))
        label = _text(_get(job, "capability_owner", "key"))
        if tag and label and _get(job, "afterglow", "liveness") is not None:
            found.append((tag, label, job))
    return found


def _job_events(snapshot, session_index, session_rows, now):
    """Feed records for finished route-less jobs: ``[time, tag, repo, 'label ✓', 'done', summary]``."""
    repo_of = {row["tag"]: row["repo"] for row in session_rows}
    events = []
    for tag, label, job in _routeless_jobs(snapshot, session_index):
        elapsed = _num(_get(job, "elapsed_min"))
        if _get(job, "liveness") == "done" and elapsed is not None:
            stamp = now - elapsed * 60
            events.append((stamp, [_clock(stamp), tag, repo_of.get(tag), f"{label} ✓", "done",
                                   _text(_get(job, "summary")) or _text(_get(job, "title")) or ""]))
    return events


def _age_minutes(text):
    """Minutes in a duration text such as ``1h 51m`` or ``1d 1h``; unknown ages sort last."""
    if not isinstance(text, str):
        return float("inf")
    parts = {unit: int(value) for value, unit in re.findall(r"(\d+)([dhm])", text)}
    return parts.get("d", 0) * 1440 + parts.get("h", 0) * 60 + parts.get("m", 0) if parts else float("inf")


def _clear_times(snapshot, session_index):
    """``{tag: epoch}`` of the newest clear that started each session, from handover records and the
    clear/continue notices peers exchanged (for sessions whose seat has no card history)."""
    observations = _get(snapshot, "observations", default={}) or {}
    joined = _get(observations, "replay_sources", default={}) or {}
    tag_by_sid8 = {str(known)[:8]: tag for tag, info in session_index.items() for known in info["ids"]}
    found = {}

    def note(sid, stamp):
        tag = tag_by_sid8.get(str(sid)[:8]) if sid else None
        if tag and stamp and stamp > found.get(tag, 0):
            found[tag] = stamp
    for handover in _get(joined, "history_handovers", default=()) or ():
        if isinstance(handover, Mapping):
            note(handover.get("to"), _num(handover.get("ts")) or _epoch_of(handover.get("ts")))
    records = _get(_get(joined, "peer_messages", default={}) or {}, "records", default=()) or ()
    for message in records:
        if isinstance(message, Mapping) and (_text(message.get("summary")) or "").startswith("[notice] action=continue"):
            match = _HANDOVER_RECEIPT.search(_text(_get(message.get("delivery"), "receipt")) or "")
            if match:
                note(match.group(2), _epoch_of(message.get("ts")))
    return found


def _finish_peers(nodes, edges, session_rows, session_index, routes, handovers, jobs=()):
    """Add what needs the seats and routes: handover links, owner-stage rows per card, steward watches."""
    links = list(edges["links"]) if edges else []
    links += [link for _stamp, link in handovers]
    if not links:
        return nodes, edges
    edges = dict(edges or {"cross": [], "side_lanes": []}, links=links)
    repo_of = {row["tag"]: row["repo"] for row in session_rows}
    linked = {end[0] for link in links for end in link[:2] if isinstance(end, list) and end}
    present = {tag for _repo, columns in nodes for column in columns for tag in column}
    nodes = [[repo, [list(column) for column in columns]] for repo, columns in nodes]
    for row in session_rows:
        if row["tag"] in linked and row["tag"] not in present:
            match = next((entry for entry in nodes if entry[0] == row["repo"]), None)
            if match is None:
                match = [row["repo"], []]
                nodes.append(match)
            match[1].append([row["tag"]])
    rank = {row["repo"]: index for index, row in enumerate(session_rows)}
    nodes.sort(key=lambda entry: rank.get(entry[0], len(rank)))
    cards = {}
    for route in sorted(routes, key=lambda item: _age_minutes(item.get("age"))):      # youngest route first
        tag = route.get("session_tag")
        if tag in linked:
            cards.setdefault(tag, {"owners": []})["owners"].append({
                "state": route.get("state"), "label": route.get("tree_label") or route.get("capability") or "·",
                "stages": [{"name": stage["name"], "state": stage["st"]} for stage in route.get("stages") or ()]})
    for tag, label, job in jobs:
        if tag in linked:
            state = _JOB_STATE.get(_get(job, "liveness"), "idle")
            cards.setdefault(tag, {"owners": []})["owners"].append({
                "state": state, "label": label,
                "stages": [{"name": _text(_get(job, "key")) or label,
                            "state": "done" if state == "done" else "active" if state == "working" else "todo"}]})
    if cards:
        edges["cards"] = cards
    watches = {}
    for link in links:
        if link[2].startswith("watch") and isinstance(link[0], list):
            steward = str(link[0][0])
            repo = repo_of.get(steward)
            entry = watches.setdefault(repo, {"live": 0, "ended": 0})
            ended = str(link[4]) == "ended" if len(link) > 4 else False
            entry["ended" if ended else "live"] += 1
    if watches:
        edges["watches"] = [
            {"repo": repo,
             "text": f"watch {counts['live']}" +
                     (f" · ended {counts['ended']}" if counts["ended"] else "")}
            for repo, counts in watches.items()]
    return nodes, edges


def _peer_records(session_rows, session_index, now, snapshot=None):
    tag_by_sid = {known: tag for tag, info in session_index.items() for known in info["ids"]}
    observations = _get(snapshot, "observations", default={}) or {}
    joined = _get(observations, "replay_sources", default={}) or {}
    tag_by_sid.update(_get(joined, "historical_tags", default={}) or {})
    harness_of = {row["tag"]: row["harness"] for row in session_rows}
    candidates, linked = [], set()

    def add(link, stamp=None):
        candidates.append((float(stamp or 0), link))

    def endpoint(sid, harness, live_only=False):
        tag = tag_by_sid.get(sid)
        if tag and (not live_only or tag in harness_of):
            return [tag, harness_of.get(tag) or harness]
        return str(sid)[:8] if _text(sid) and _text(harness) else None      # an ended session: its short id

    for row in session_rows:
        session = session_index[row["tag"]]["session"]
        me = [row["tag"], row["harness"]]
        received = _get(session, "peer_last_recv")
        if isinstance(received, Mapping):
            other = endpoint(received.get("from_session_id"), received.get("from_harness"))
            if other and _text(received.get("kind")):
                age = _dur(received.get("age_min"))
                add([other, me, received["kind"], f"{age} ago" if age else None, ""],
                    _epoch_of(received.get("ts", received.get("at"))) or
                    -float(_num(received.get("age_min")) or 0))
        sent = _get(session, "peer_last_sent")
        if isinstance(sent, Mapping):
            other = endpoint(sent.get("to_session_id"), sent.get("to_harness"))
            if other and _text(sent.get("kind")):
                age = _dur(sent.get("age_min"))
                add([me, other, sent["kind"], f"{age} ago" if age else None, ""],
                    _epoch_of(sent.get("ts", sent.get("at"))) or
                    -float(_num(sent.get("age_min")) or 0))
        targets = [t for t in (_get(session, "steward_targets") or ()) if isinstance(t, Mapping) or hasattr(t, "get")]
        for target in targets:
            sid = _text(_get(target, "session_id"))
            # Only a current session row is a live watch: a name the target had before it
            # resumed (observations.replay_sources.historical_tags) is history, not a card.
            other = endpoint(sid, _get(target, "harness"), live_only=True)
            if other is None:
                continue
            raw_ts = _get(target, "ts", "start_ts")
            stamp = _epoch_of(raw_ts)
            when = _iso_age(raw_ts, now)
            if when is None and stamp is not None:
                when = datetime.datetime.fromtimestamp(stamp).strftime("%m-%d")
            live_tag = tag_by_sid.get(sid)
            live_tag = live_tag if live_tag in harness_of else None
            if live_tag:
                linked.add(live_tag)
            add([me, other, "watch ⚑", when, "" if live_tag else "ended"], stamp or 0)
    peer_messages = _get(joined, "peer_messages", default={}) or {}
    for message in _get(peer_messages, "records", default=()) or ():
        if not isinstance(message, Mapping):
            continue
        source, target = message.get("from") or {}, message.get("to") or {}
        summary = _text(message.get("summary")) or ""
        if summary in _RECEIPTS or summary.startswith("[notice] action="):
            continue                          # delivery receipts and clear/continue notices are not conversations
        src = endpoint(_get(source, "session_id"), _get(source, "harness"))
        dst = endpoint(_get(target, "session_id"), _get(target, "harness"))
        if src and dst:
            add([src, dst, _text(message.get("kind")) or "notice",
                 _iso_age(message.get("ts"), now), _text(message.get("summary")) or ""],
                _epoch_of(message.get("ts")))
    for handover in _get(joined, "history_handovers", default=()) or ():
        if not isinstance(handover, Mapping):
            continue
        src = endpoint(_get(handover, "frm"), _get(handover, "frm_h"))
        dst = endpoint(_get(handover, "to"), _get(handover, "to_h"))
        if src and dst:
            add([src, dst, "handover", _iso_age(handover.get("ts", handover.get("at")), now), ""],
                _epoch_of(handover.get("ts", handover.get("at"))))
    # Keep the newest observation for each directed endpoint pair and kind; a link needs a live
    # session at one end (two ended sessions talking is history, not the fleet) and two different ends.
    def live_end(end):
        return end[0] if isinstance(end, list) and end and end[0] in harness_of else None
    newest_by_pair = {}
    for stamp, link in candidates:
        if not (live_end(link[0]) or live_end(link[1])) or link[0] == link[1]:
            continue
        key = (repr(link[0]), repr(link[1]), str(link[2]))
        if key not in newest_by_pair or stamp > newest_by_pair[key][0]:
            newest_by_pair[key] = (stamp, link)
    ranked = sorted(newest_by_pair.values(), key=lambda item: -item[0])
    unique = [pair[1] for pair in ranked]
    stamp_of = {id(pair[1]): pair[0] for pair in ranked}
    for link in unique:
        for end in link[:2]:
            if live_end(end):
                linked.add(end[0])
    if not unique:
        return [], None                       # no peer record: the section is omitted
    repo_of = {row["tag"]: row["repo"] for row in session_rows}
    sent = {}                                  # (from, to) -> newest steer, live ends only
    for link in unique:
        if link[2] == "steer" and live_end(link[0]) and live_end(link[1]):
            sent.setdefault((link[0][0], link[1][0]), link)
    side_lanes, paired = [], set()
    for (a, b) in sent:
        if (b, a) in sent and repo_of.get(a) == repo_of.get(b) and a not in paired and b not in paired:
            side_lanes.append(sorted((a, b)))             # the lower tag sits below the higher one
            paired.update((a, b))
    cross = next(([a, b, re.sub(r" ago$", "", link[3] or "")] for (a, b), link in sent.items()
                  if repo_of.get(a) != repo_of.get(b) and link[3]), None)
    by_repo = {}
    for row in session_rows:
        by_repo.setdefault(row["repo"], []).append(row["tag"])
    steward_targets = {}
    for row in session_rows:
        tag = row["tag"]
        session = session_index[tag]["session"]
        targets = []
        for target in (_get(session, "steward_targets") or ()):
            sid = _text(_get(target, "session_id"))
            other = tag_by_sid.get(sid)
            if (other and other in harness_of and other != tag
                    and repo_of.get(other) == row["repo"] and other not in targets):
                targets.append(other)
        if targets:
            steward_targets[tag] = targets
    mutual = {(a, b) for (a, b) in sent if (b, a) in sent}
    nodes, steward_columns, ordered_lanes = [], [], []
    for repo, tags in by_repo.items():
        columns, used = [], set()

        def walk(start):
            """One steward column: the head first, then its targets depth-first."""
            column = []

            def visit(tag):
                if tag in used or tag in column:
                    return                    # a placed tag never appears twice
                column.append(tag)
                for target in steward_targets.get(tag, ()):
                    visit(target)

            visit(start)
            return column

        # A column starts at a head (a steward nobody in this repo watches) and follows the
        # steward graph, so the stack does not depend on session order. A pure cycle has no
        # head; it starts at its lowest tag. Heads keep the sessions' own order.
        watched = {target for targets in steward_targets.values() for target in targets}
        heads = [tag for tag in tags if tag in steward_targets and tag not in watched]
        pending = []
        starts = list(heads)
        while True:
            while starts:
                column = walk(starts.pop(0))
                used.update(column)
                columns.append(column)
                if len(column) > 1:
                    steward_columns.append(column)
            pending = sorted(tag for tag in tags if tag not in used and tag in steward_targets)
            if not pending:
                break
            starts = [pending[0]]
        for tag in tags:
            if tag in used:
                continue
            pair = next((lane for lane in side_lanes
                         if tag in lane and not (set(lane) & used)), None)
            column = list(pair) if pair else [tag]
            used.update(column)
            columns.append(column)
        nodes.append([repo, columns])
        for column in columns:
            for upper, lower in zip(column, column[1:]):
                if (upper, lower) in mutual or (lower, upper) in mutual:
                    ordered_lanes.append([upper, lower])
    return nodes, {"links": unique, "cross": [cross] if cross else [],
                   "side_lanes": ordered_lanes, "steward_columns": steward_columns}


# -- hosts / compute / usage / memory / system / events ------------------------------------

def _host_blocks(snapshot):
    block = _get(snapshot, "compute_hosts")
    hosts = _get(block, "hosts") if block is not None else None
    return list(hosts) if isinstance(hosts, (list, tuple)) else None


def _gib(mib):
    value = _num(mib)
    return None if value is None else round(value / 1024, 1)


def _gpu_label(name):
    """The short card name the explorer draws: no vendor, product-line or edition words."""
    text = _text(name)
    if text is None:
        return None
    for word in ("NVIDIA ", "GeForce ", " Generation"):
        text = text.replace(word, "")
    return text.strip() or None


def _gpu_key(name):
    """The colour class of a card name (``gpu_ada``, ``gpu_5090``...); None for a card with none."""
    text = (_text(name) or "").lower()
    if "ada" in text.split():
        return "gpu_ada"
    found = re.search(r"\b(5090|4090)\b", text)
    if found:
        return "gpu_" + found.group(1)
    return "gpu_legacy" if re.search(r"\b(gtx|titan)\b", text) else None


def _host_records(snapshot, session_index):
    hosts = _host_blocks(snapshot)
    if hosts is None:
        return [], []
    owner_tag = {}
    for tag, info in session_index.items():
        if info["sid"]:
            owner_tag[(_get(info["session"], "harness"), info["sid"])] = tag
    labels, compute = [], []
    for host in hosts:
        if not isinstance(host, Mapping):
            continue
        name = _text(host.get("host")) or "·"
        base = host.get("self") is True
        labels.append({"mark": "⌂" if base else "◇", "name": name, "key": "base_host" if base else "host"})
        if host.get("reachable") is False:
            compute.append([name, base, None, None, None, []])
            continue
        thread_pct = host.get("cpu_thread_utilization_pct")
        if isinstance(thread_pct, (list, tuple)) and thread_pct:
            samples = [_num(value) for value in thread_pct]
            samples = [value for value in samples if value is not None]
            cpu = [round(sum(samples) / 100, 1), len(samples)] if samples else None
        else:
            cpu_pct, cpu_n = _num(host.get("cpu_utilization_pct")), _num(host.get("cpu_count"))
            cpu = [round(cpu_n * cpu_pct / 100, 1), cpu_n] if cpu_pct is not None and cpu_n else None
        mem_total, mem_used = _gib(host.get("memory_total_mib")), _gib(host.get("memory_used_mib"))
        mem = [mem_used, mem_total] if mem_total and mem_used is not None else None
        swap_total = _gib(host.get("swap_total_mib"))
        swap_used = _gib(host.get("swap_used_mib"))
        swap = [swap_used, swap_total] if swap_total and swap_used is not None else None
        gpus = []
        for gpu in host.get("gpus") or ():
            if not isinstance(gpu, Mapping):
                continue
            processes = [p for p in (gpu.get("processes") or ()) if isinstance(p, Mapping)]
            users = []
            for process in processes:
                owner = process.get("session_owner")
                tag = owner_tag.get((_get(owner, "harness"),
                                     _get(owner, "session_id", "id"))) if owner else None
                if tag and tag not in users:
                    users.append(tag)
            used, total = _gib(gpu.get("memory_used_mib")), _gib(gpu.get("memory_total_mib"))
            util = _num(gpu.get("utilization_gpu_pct"))
            gpus.append([_num(gpu.get("index")), "working" if processes else "idle",
                         _gpu_label(gpu.get("name")), _gpu_key(gpu.get("name")),
                         None if util is None else int(util), used, total, users])
        compute.append([name, base, cpu, mem, swap, gpus])
    return labels, compute


def _dur_coarse(minutes):
    """A process age at the grain the GPU rows use: ``42m``, ``19h``, ``1d 6h``."""
    text = _dur(minutes)
    if text and "h " in text and "d" not in text:
        return text.split(" ", 1)[0]
    return text


def _session_gpu_lines(snapshot, session_index):
    """``{tag: [segment rows]}``: one row per host:gpu the session's processes occupy.

    A plain process row reads ``● host:gpu  card  used GB  process[ ×N]  · age``; a process owned
    by a registered job reads ``● host:gpu  card  util%  process GB  job label``.
    """
    hosts = sorted((h for h in _host_blocks(snapshot) or [] if isinstance(h, Mapping)),
                   key=lambda h: _text(h.get("host")) or "")
    owner_tag = {}
    for tag, info in session_index.items():
        for known in info["ids"]:
            owner_tag[(_get(info["session"], "harness"), known)] = tag
    lines = {}
    for host in hosts:
        if not isinstance(host, Mapping) or host.get("reachable") is False:
            continue
        name = _text(host.get("host")) or "·"
        for gpu in host.get("gpus") or ():
            if not isinstance(gpu, Mapping):
                continue
            by_tag = {}
            for process in gpu.get("processes") or ():
                if not isinstance(process, Mapping):
                    continue
                owner = process.get("session_owner")
                tag = owner_tag.get((_get(owner, "harness"), _get(owner, "session_id", "id"))) if owner else None
                if tag:
                    by_tag.setdefault(tag, []).append(process)
            for tag, processes in by_tag.items():
                head = [["●", "spin"], [f" {name}:{_num(gpu.get('index'))}", "host"],
                        ["  " + (_gpu_label(gpu.get("name")) or "·"), "gpu_ada"]]
                job = next((p for p in processes if _get(p.get("owner"), "kind") == "job"), None)
                if job is not None:
                    util = _num(gpu.get("utilization_gpu_pct"))
                    used = sum(_num(p.get("used_memory_mib")) or 0 for p in processes)
                    label = _text(_get(job.get("owner"), "label")) or "·"
                    label = label.split(":", 1)[1] if label.startswith("job:") else label
                    row = head + ([[f"  {int(util)}%", "soft"]] if util is not None else []) + \
                        [[f"  {_gib(used)} GB", _SEG_DIM], ["  " + label, "now_sub"]]
                else:
                    used = _gib(sum(_num(p.get("used_memory_mib")) or 0 for p in processes))
                    ages = [_dur_coarse((_num(p.get("elapsed_s")) or 0) / 60) for p in processes
                            if _num(p.get("elapsed_s")) is not None]
                    comm = _text(processes[0].get("process_name")) or "·"
                    row = head + [[f"  {used} GB", "soft"],
                                  ["  " + comm + (f" ×{len(processes)}" if len(processes) > 1 else ""), _SEG_DIM]]
                    if ages:
                        row.append(["  · " + " · ".join(ages), _SEG_DIM])
                lines.setdefault(tag, []).append(row)
    return lines


_UNIT = {"5h": "5h", "7d": "week", "wk": "week", "mo": "month"}


def _claude_usage_payload(snapshot, now):
    """Consume the public admitted payload only, with its original quota age."""
    payload = _get(snapshot, "payload")
    freshness = _get(snapshot, "freshness")
    observed = _num(_get(snapshot, "observed_at"))
    now = _num(now)
    if (not isinstance(payload, Mapping) or freshness not in ("fresh", "stale")
            or observed is None or now is None or not 0 <= observed <= now
            or now - observed > 900):
        return None, "unknown", None

    def percent(value):
        return value if type(value) is int and 0 <= value <= 100 else None

    clean = {key: percent(payload.get(key)) for key in ("rl_5h", "rl_7d")}
    clean.update({key: value if (value := _num(payload.get(key))) is not None and value >= 0 else None
                  for key in ("rs_5h", "rs_7d")})
    models = payload.get("rl_ms")
    clean["rl_ms"] = [(item[0], item[1]) for item in models
                      if isinstance(item, (list, tuple)) and len(item) == 2
                      and isinstance(item[0], str) and 0 < len(item[0]) <= 80
                      and item[0].isprintable() and percent(item[1]) is not None
                     ] if isinstance(models, (list, tuple)) else []
    if not any(clean[key] is not None for key in ("rl_5h", "rl_7d")) and not clean["rl_ms"]:
        return None, "unknown", None
    return clean, "stale" if freshness == "stale" or now - observed > 180 else "fresh", observed


def _claude_usage_unavailable(snapshot, now, *, quota_observed=None, quota_freshness="stale"):
    """Display only the backend's safe observation; never resolve credentials or an account here."""
    observation = _get(snapshot, "error_observation")
    observation = observation if isinstance(observation, Mapping) else {}
    error = _text(observation.get("error"))
    errors = {"rate-limit": "rate limit", "auth": "auth", "missing-credential": "no credential",
              "timeout": "timeout", "network": "network", "response-invalid": "response invalid",
              "unknown": None}
    reasons = {"credential-not-found": "no credential", "credential-invalid": None,
               "credential-store-read-failed": None, "credential-changed": None,
               "authentication-rejected": "auth", "usage-rate-limited": "rate limit",
               "usage-timeout": "timeout", "usage-unreachable": "network",
               "usage-response-invalid": "response invalid", "usage-http-error": None,
               "usage-unreadable": None}
    reason = (errors.get(error) or reasons.get(_text(observation.get("reason")))) if error in errors else None
    observed = _num(observation.get("observed_at"))
    now = _num(now)
    observed = observed if observed is not None and now is not None and 0 <= observed <= now else None
    retry = _num(observation.get("retry_at"))
    until = (_clock(retry) if error in errors and observed is not None
             and retry is not None and retry > now else None)
    status = "unavailable"
    if until:
        # A long backend cooldown outlives quota freshness; keep its absolute deadline.
        remaining = _dur((retry - now) / 60)
        status = f"wait {remaining} ({until})" if retry - now >= 86400 else f"wait {until}"
    if reason:
        status += " · " + reason
    if quota_observed is not None:
        # Quota age belongs to the admitted payload, independently of fetch/retry time.
        age = quota_freshness + " · " + _since(quota_observed, now) + " ago"
        status = age + " · API " + status if until else age + (" · " + status if reason else "")
    elif observed is not None:
        status += " · " + _since(observed, now) + " ago"
    return {"text": "claude " + status, "key": "hb_claude",
            "windows": [None, None, None]}


def _claude_usage_annotation(snapshot, now, freshness, quota_observed):
    """Display status alongside admitted windows; it cannot admit quota itself."""
    status = _claude_usage_unavailable(snapshot, now)
    error_at = _num(_get(_get(snapshot, "error_observation"), "observed_at"))
    if quota_observed is not None and (freshness == "stale" or (
            error_at is not None and error_at >= quota_observed
            and status["text"].startswith("claude wait "))):
        return _claude_usage_unavailable(snapshot, now, quota_observed=quota_observed,
                                        quota_freshness=freshness)["text"].removeprefix("claude ")
    return ""


def _usage_records(snapshot, session_rows, now):
    best = {}
    for session in _records(_get(snapshot, "sessions", default=())):
        harness = _text(_get(session, "harness"))
        if harness not in HARNESSES or harness == "claude":
            continue
        windows = _get(session, "rl_windows")
        r5, r7, rms = _get(session, "rl_5h"), _get(session, "rl_7d"), _get(session, "rl_ms")
        if windows or r5 is not None or r7 is not None or rms:
            mtime = _num(_get(session, "mtime")) or 0
            if harness not in best or mtime > best[harness][0]:
                resets = _get(session, "rl_rs") or (None, None)
                best[harness] = (mtime, r5, r7, rms, resets, windows)
    snapshots = _get(snapshot, "usage_snapshots", default={})
    snapshots = snapshots if isinstance(snapshots, Mapping) else {}
    claude_snapshot = snapshots.get("claude")
    payload, freshness, observed = _claude_usage_payload(claude_snapshot, now)
    annotation = _claude_usage_annotation(claude_snapshot, now, freshness, observed)
    if payload is not None:
        best["claude"] = (observed, payload["rl_5h"], payload["rl_7d"], payload["rl_ms"],
                          (payload["rs_5h"], payload["rs_7d"]), None)
    claude_present = "claude" in snapshots or any(
        _get(session, "harness") == "claude" for session in _records(_get(snapshot, "sessions", default=())))
    for harness, snap in snapshots.items():
        if harness == "claude":
            continue
        payload = snap.get("payload") if isinstance(snap, Mapping) else None
        if isinstance(payload, Mapping) and harness in HARNESSES and (
                payload.get("rl_windows") or payload.get("windows") or payload.get("rl_5h") is not None
                or payload.get("rl_7d") is not None or payload.get("rl_ms")):
            best[harness] = (_num(snap.get("observed_at")) or 0, payload.get("rl_5h"), payload.get("rl_7d"),
                             payload.get("rl_ms"), (payload.get("rs_5h"), payload.get("rs_7d")),
                             payload.get("rl_windows") or payload.get("windows"))
    usage = []
    for harness in HARNESSES:
        if harness not in best:
            if harness == "claude" and claude_present:
                usage.append(_claude_usage_unavailable(claude_snapshot, now,
                             quota_observed=observed if freshness == "stale" else None))
            continue
        _mt, r5, r7, rms, resets, windows = best[harness]
        raw = list(windows) if windows else [("5h", r5, resets[0]), ("week", r7, resets[1])]
        raw += [(label, value, None) for label, value in (rms or ())]
        shown = [None, None, None]            # slots: 5h, week, then the model or monthly window
        for item in raw:
            label, pct, reset = (tuple(item) + (None, None))[:3]
            if _num(pct) is None:
                continue
            reset_text = None
            if _num(reset) is not None and now is not None:
                reset_text = _dur(max(0, (reset - now) / 60))
            unit = _UNIT.get(str(label), str(label))
            slot = {"5h": 0, "week": 1}.get(unit, 2)
            if shown[slot] is None:
                shown[slot] = [unit, int(pct), reset_text]
        if any(shown):
            text = harness + (" " + annotation if harness == "claude" and annotation else "")
            item = {"text": text, "key": "hb_" + harness, "windows": shown}
            if harness == "codex":
                snap = (_get(snapshot, "usage_snapshots", default={}) or {}).get(harness)
                credit = codex_credits(snap.get("payload") if isinstance(snap, Mapping) else None)
                if credit:
                    item["credits"] = credit
            usage.append(item)
        elif harness == "claude" and claude_snapshot is not None:
            usage.append(_claude_usage_unavailable(claude_snapshot, now))
    return usage


def current_session_activity(model, candidate, snapshot):
    """Paint fresh basic activity over retained details for the same process only.

    Complete publication, roster/order, detail and action provenance remain the
    admitted model's. A persisted replay has no process bindings to transfer.
    """
    from ..refresh import _BASIC_COMPLETE_STATES
    if model is None or candidate is None:
        return model
    observations = _get(snapshot, "observations", default={}) or {}
    basic = _get(observations, "basic", default={}) or {}
    if str(_get(basic, "state", default="")).lower() not in _BASIC_COMPLETE_STATES:
        return model
    previous = model.source_records or {}
    current = candidate.source_records or {}
    old_bindings, new_bindings = previous.get("_detail_bindings", {}), current.get("_detail_bindings", {})
    old_tags, new_tags = previous.get("_detail_tags", {}), current.get("_detail_tags", {})
    candidates = {row["tag"]: row for row in candidate.sessions}
    updates = {}
    for ref, binding in old_bindings.items():
        new = new_bindings.get(ref)
        row = candidates.get(new_tags.get(ref))
        if (isinstance(binding, (list, tuple)) and isinstance(new, (list, tuple))
                and len(binding) >= 4 and tuple(binding[:4]) == tuple(new[:4])
                and ref in old_tags and row is not None):
            updates[old_tags[ref]] = row
    rows = []
    changed = False
    for row in model.sessions:
        current = updates.get(row.get("tag"))
        values = ({key: current.get(key) for key in ("state", "ask", "restart", "running")}
                  if current is not None else {})
        values = {key: value for key, value in values.items() if row.get(key) != value}
        rows.append(dict(row, **values) if values else row)
        changed = changed or bool(values)
    return model.with_updates(sessions=rows) if changed else model


# Display cells one collection may fail to observe; groups move together.
_UNOBSERVED_CELL_GROUPS = (("model", "effort"), ("context", "window", "context_detail"), ("title",))


def retain_unobserved_cells(candidate, published):
    """Keep a same-process row's shown cells when the new ready pass did not observe them.

    A required source being ready says nothing about model/context/title. A
    real new value replaces the old one; a changed SID, PID or process birth,
    a removed row or a dead row receives nothing from the earlier publication.
    """
    if candidate is None or published is None:
        return candidate
    old_records, new_records = published.source_records or {}, candidate.source_records or {}
    old_bindings, new_bindings = old_records.get("_detail_bindings", {}), new_records.get("_detail_bindings", {})
    old_tags, new_tags = old_records.get("_detail_tags", {}), new_records.get("_detail_tags", {})
    if not old_bindings or not new_bindings:
        return candidate
    shown = {row.get("tag"): row for row in published.sessions}
    previous = {}
    for ref, binding in new_bindings.items():
        old = old_bindings.get(ref)
        if (isinstance(binding, (list, tuple)) and isinstance(old, (list, tuple))
                and len(binding) >= 4 and tuple(binding[:4]) == tuple(old[:4])
                and ref in new_tags and old_tags.get(ref) in shown):
            previous[new_tags[ref]] = shown[old_tags[ref]]
    rows, retained = [], {}
    for row in candidate.sessions:
        old = previous.get(row.get("tag"))
        updates = {}
        if old is not None and row.get("state") not in {"dead", "stale"}:
            for group in _UNOBSERVED_CELL_GROUPS:
                if row.get(group[0]) is None and old.get(group[0]) is not None:
                    updates.update({key: old[key] for key in group if key in old})
        if updates:
            retained[row["tag"]] = tuple(sorted(updates))
            rows.append(dict(row, **updates))
        else:
            rows.append(row)
    if not retained:
        return candidate
    return candidate.with_updates(sessions=rows,
                                  source_records={**new_records, "_retained_cells": retained})


def current_claude_usage(model, snapshot, now):
    """Rebind only Claude usage at precomputed/replay and pending-draw boundaries."""

    def claude(row):
        return (row.get("harness") == "claude" or row.get("key") == "hb_claude"
                or str(row.get("text", "")).split(" ", 1)[0] == "claude")

    snapshots = _get(snapshot, "usage_snapshots")
    current = _usage_records({
        "usage_snapshots": {"claude": snapshots["claude"]}
        if isinstance(snapshots, Mapping) and "claude" in snapshots else {},
        "sessions": [session for session in _records(_get(snapshot, "sessions", default=()))
                     if _get(session, "harness") == "claude"],
    }, (), now)
    row = next((item for item in current if claude(item)), None)
    usage, replaced = [], False
    for old in model.usage:
        if claude(old):
            if not replaced:
                usage.append(row or _claude_usage_unavailable(None, now))
                replaced = True
        else:
            usage.append(old)
    if row is not None and not replaced:
        usage.insert(0, row)
    if (replaced or row is not None) and tuple(usage) != model.usage:
        return model.with_updates(usage=usage)
    return model


def _clock_stamp(ts):
    """The local datetime a journal/route timestamp names, or None when unparsable."""
    if not isinstance(ts, str):
        return None
    try:
        stamp = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp


def _parse_clock(ts):
    stamp = _clock_stamp(ts)
    return stamp.strftime("%H:%M") if stamp else None


def _memory_when(ts, clock=None):
    """TIME cell for a memory row: ``HH:MM`` on the current local date, else ``MM-DD``.

    The memory columns are 7 cells wide, so a full date would break the table; a
    month-day is the same width as a clock and still says "not today".
    """
    stamp = _clock_stamp(ts)
    if stamp is None:
        return None
    today = datetime.datetime.fromtimestamp(time.time() if clock is None else clock).date()
    return stamp.strftime("%H:%M" if stamp.date() == today else "%m-%d")


_SAVED = frozenset({"add", "note", "tidy-add", "decision-record"})
_BOOKKEEPING = frozenset({"supersede", "reinforce", "lifecycle-expire", "delete"})


def _memory_text(snippet):
    """A journal snippet without its ``2026-10-21 [0c](claude 442122b8):`` provenance header."""
    text = _text(snippet)
    if not text:
        return text
    text = re.sub(r"^\d{4}-\d\d-\d\d\s+(?:\S+\s+)?\[[^]]+\]\([^)]*\):\s*", "", text)
    text = re.sub(r"^(?:\[[0-9a-f]{2}(?:/[0-9a-f]{2})?\]\s*)+", "", text)
    return re.sub(r"^\d{4}-\d\d-\d\d(?:\s+\d\d:\d\d)?(?:\s*KST)?\s*[.:]?\s+", "", text) or text


def _work_journal_rows(snapshot):
    rows, seen = [], set()
    for session in _records(_get(snapshot, "sessions", default=())):
        chain = _get(session, "route_chain") or {}
        for row in chain.get("automatic_records") or ():
            identity = (row.get("artifact_id"), row.get("revision_id"))
            if row.get("kind") != "journal-entry" or identity in seen:
                continue
            seen.add(identity)
            rows.append({**row, "project": _gpu_repo(_get(session, "cwd") or "")})
    return rows


def _journal_groups(snapshot, clock=None):
    """Journal rows per project, newest first: ``{project: [(epoch, [when, action, type, text])]}``.

    The text is the record's stored headline when the collector found one, else the journal
    snippet. None when no journal was read.
    """
    memory = _get(snapshot, "memory")
    by_repo = {key: list(value or []) for key, value in (memory.get("by_repo") or {}).items()} if isinstance(memory, Mapping) else {}
    # Work records are a read-only supplement; they are never inserted into
    # the memory DB or reclassified as decisions.
    for row in _work_journal_rows(snapshot):
        repo = row["project"] or UNKNOWN_REPO
        by_repo.setdefault(repo, []).append({"id": row["artifact_id"] + ":" + row["revision_id"],
            "ts": row["at"], "action": "work-record", "type": "journal-entry",
            "headline": row["title"], "project": repo})
    if not by_repo:
        return {} if isinstance(memory, Mapping) and memory.get("journal_available") else None
    grouped = {}
    seen = set()
    for repo, events in by_repo.items():
        for event in events or ():
            if not isinstance(event, Mapping):
                continue
            action_name = event.get("action")
            if action_name in _BOOKKEEPING:           # pointers and counters carry no sentence of their own
                continue
            identity = _text(event.get("id"))
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            action = "recorded" if action_name == "work-record" else "saved" if action_name in _SAVED else "changed"
            snippet = _memory_text(event.get("headline") or event.get("snippet"))
            project_source = _text(event.get("project"))
            if project_source and "/" in project_source:
                project = _gpu_repo(project_source)
            else:
                # The collector's key is the board's own project grouping (git-aware), so a
                # row written from a subfolder stays with its project and its DB records.
                project = (project_source or _shown_repo(repo) or _gpu_repo(_get(event, "cwd"))
                           or UNKNOWN_REPO)
            grouped.setdefault(project, []).append((_epoch_of(event.get("ts")) or 0.0,
                [_memory_when(event.get("ts"), clock), action, _text(event.get("type")), snippet]))
    for rows in grouped.values():
        rows.sort(key=lambda item: -item[0])
    return {project: rows for project, rows in grouped.items() if rows}


def _memory_records(journal, accepted_detail):
    """MEMORY journal rows ``[repo, [[when, action, type, text], ...]]``; a replayed detail wins."""
    detail = _get(accepted_detail, "memory")
    if detail is not None and not isinstance(detail, Mapping):
        return detail
    if journal is None:
        return None
    return [[repo, [row for _stamp, row in rows]] for repo, rows in sorted(journal.items())]


# One classifier shared with the session block (fleet/session_work.py).
from ..collectors.memory import OPEN_STAGES, open_stage as _open_stage  # noqa: E402


def _headline_text(text):
    """A stored headline without its session tag, date or ``결정:`` lead (the row already says it)."""
    text = _memory_text(text)
    return re.sub(r"^(?:\[결정\]|결정\s*[:：])\s*", "", text) if text else text


def _day_of(updated):
    text = _text(updated) or ""
    return text[5:10] if re.match(r"^\d{4}-\d\d-\d\d", text) else None


def _day_epoch(updated):
    try:
        return datetime.datetime.strptime((_text(updated) or "")[:10], "%Y-%m-%d").timestamp()
    except ValueError:
        return None


def _empty_project():
    return {"last": None, "decisions": [], "open": [], "limited": False,
            "counts": {name: 0 for name in OPEN_STAGES}}


def _project_memory(snapshot, routes, session_rows, journal):
    """Per project: newest decisions, open work and the time of the last change.

    Decisions and open work come from the read-only DB summary (stored headlines only);
    the last change is the newer of the journal row and the record date. Projects with a
    live session come first, then the most recently changed.
    """
    memory = _get(snapshot, "memory")
    database = _get(memory, "database") if isinstance(memory, Mapping) else None
    state, stale, source = "unknown", False, {}
    if isinstance(database, Mapping):
        state = _text(database.get("state")) or "unknown"
        if state == "success":
            source = database.get("by_repo")
        else:
            source = database.get("last_good_by_repo")
            stale = bool(database.get("last_good"))
        source = source if isinstance(source, Mapping) else {}
    projects, last = {}, {}
    for repo, rows in (journal or {}).items():
        last[repo] = (rows[0][0], rows[0][1][0])
        projects[repo] = _empty_project()
    decisions_today = doing = 0
    for repo, entry in source.items():
        if not isinstance(entry, Mapping):
            continue
        repo = str(repo)
        project = projects.setdefault(repo, _empty_project())
        project["decisions"] = [[_day_of(record.get("updated")),
                                 _headline_text(record.get("headline")) or _text(record.get("id"))]
                                for record in entry.get("decisions") or () if isinstance(record, Mapping)]
        decisions_today += int(_num(entry.get("decisions_today")) or 0)
        items = []
        for record in entry.get("open") or ():
            if not isinstance(record, Mapping):
                continue
            stage = _open_stage(record, routes)
            if stage is None:
                continue
            items.append([stage, _day_of(record.get("updated")),
                          _headline_text(record.get("headline")) or _text(record.get("id")),
                          _text(record.get("id"))])
        # Stable sort: the collector's newest-first order stays within each stage.
        items.sort(key=lambda item: OPEN_STAGES.index(item[0]))
        project["open"] = items
        project["limited"] = bool(entry.get("limited"))
        project["counts"] = {name: sum(1 for item in items if item[0] == name) for name in OPEN_STAGES}
        doing += project["counts"]["doing"]
        stamp = _day_epoch(entry.get("updated"))
        if stamp is not None and stamp > last.get(repo, (0.0, None))[0]:
            last[repo] = (stamp, _day_of(entry.get("updated")))
    for repo, (_stamp, text) in last.items():
        projects[repo]["last"] = text
    live = {row.get("repo_id") for row in session_rows or ()}
    order = sorted(projects, key=lambda repo: (repo not in live, -last.get(repo, (0.0, None))[0], repo))
    known = state == "success" or stale
    return {"state": state, "stale": stale,
            "decisions_today": decisions_today if known else None,
            "doing": doing if known else None,
            "order": order, "projects": projects}


def _current_task_summary(accepted_detail, session_rows, session_index, routes):
    focus, _seats, _config, _panes = _detail_parts(accepted_detail)
    key = _get(focus, "key")
    harness, sid = _text(_get(key, "harness")), _text(_get(key, "sid"))
    if focus is None or not harness or not sid:
        return {"state": "unknown", "session_tag": None, "by_route": {},
                "todo": [], "doing": [], "done": []}
    tag = next((tag for tag, info in session_index.items()
                if _get(info.get("session"), "harness", "adapter", "h") == harness
                and sid in info.get("ids", ())), None)
    if tag is None:
        return {"state": "unknown", "session_tag": None, "by_route": {},
                "todo": [], "doing": [], "done": []}
    by_route, todo, doing, done = {}, [], [], []
    for route in routes:
        if route.get("session_tag") != tag or route.get("state") in ("done", "dead"):
            continue
        route_id = str(route.get("id") or "")
        stages = tuple(route.get("stages") or ())
        progress = {
            "todo": [str(stage.get("name")) for stage in stages if stage.get("st") == "todo"],
            "doing": [str(stage.get("name")) for stage in stages if stage.get("st") == "active"],
            "done": [str(stage.get("name")) for stage in stages if stage.get("st") == "done"],
        }
        if any(progress.values()):
            by_route[route_id] = progress
            todo.extend(progress["todo"])
            doing.extend(progress["doing"])
            done.extend(progress["done"])
    obligations = _get(focus, "obligations")
    for item in obligations or ():
        if not isinstance(item, Mapping):
            continue
        state = str(item.get("state") or "").casefold()
        label = "registered work" + (" · " + str(item.get("attempt_id"))[:8]
                                      if item.get("attempt_id") else "")
        if state in ("running", "claimed"):
            doing.append(label)
        elif state not in ("complete", "cancelled"):
            todo.append(label)
    health = _get(_get(focus, "health"), "state") or "success"
    return {"state": str(health), "session_tag": tag, "by_route": by_route,
            "todo": todo[:8], "doing": doing[:8], "done": done[:8]}


def _system_record(snapshot, session_rows, compute, accepted_detail, usage, now=None):
    system = {}
    governor = _get(snapshot, "governor")
    jobs = _records(_get(snapshot, "jobs", default=()))
    resources = _records(_get(snapshot, "resources", default=()))
    snapshot_summary = _get(snapshot, "summary", default={}) or {}
    dispatch = {"jobs": sum(1 for job in jobs if _get(job, "status") in ("open", "running")),
                "resource_running": sum(1 for r in resources if _get(r, "liveness") == "working"),
                "resource_exited": int(_num(_get(snapshot_summary, "resource_exited")) or
                                        sum(1 for r in resources if _get(r, "liveness") == "exited"))}
    if isinstance(governor, Mapping):
        active, cap = _num(governor.get("active")), _num(governor.get("cap"))
        if active is not None:
            dispatch["governor_active"] = int(active)
        if cap is not None:
            dispatch["governor_cap"] = int(cap)
    system["dispatch"] = dispatch
    system["dispatch_count"] = sum(1 for job in jobs if _get(job, "liveness") == "working")
    delivery = _get(snapshot, "pending_delivery")
    peer_delivery = {}
    if isinstance(delivery, Mapping):
        pending = _num(delivery.get("pending", delivery.get("total")))
        if pending is not None:
            peer_delivery["pending"] = int(pending)
        no_recipient = delivery.get("no_recipient")
        if isinstance(no_recipient, int) and not isinstance(no_recipient, bool):
            peer_delivery["no_recipient"] = no_recipient
    peer_messages = _get(snapshot, "peer_messages")
    records = _get(peer_messages, "records") if peer_messages is not None else None
    if isinstance(records, (list, tuple)):
        peer_delivery["records"] = len(records)
    if peer_delivery:
        system["peer_delivery"] = peer_delivery
    observations = _get(snapshot, "observations")
    if isinstance(observations, Mapping):
        collectors = [name for name, value in observations.items()
                      if isinstance(value, Mapping) and "state" in value and not name.startswith("replay_")]
        if collectors:
            system["collectors"] = collectors
    gpu_processes, unregistered_gpu, gpu_summary, gpu_owner = _gpu_process_records(snapshot)
    if gpu_processes:
        system["gpu_processes"] = gpu_processes
        system["gpu_process_summary"] = {"count": gpu_summary["count"],
                                         "unregistered": gpu_summary["unregistered"]}
    if unregistered_gpu:
        system["unregistered_gpu_processes"] = unregistered_gpu
        system["unregistered_gpu_summary"] = {"count": gpu_summary["unregistered"],
            "remaining": gpu_summary["remaining"], "owner": gpu_owner}
    install = _install_record(snapshot)
    if install is not None:
        system["install"] = install
    runtimes = _get(_get(snapshot, "hearting"), "runtimes")
    if isinstance(runtimes, (list, tuple)) and runtimes:
        system["runtimes"] = [str(name) for name in runtimes]
    if usage:
        observed = [_num(snap.get("observed_at")) for snap in (_get(snapshot, "usage_snapshots", default={}) or {}).values()
                    if isinstance(snap, Mapping)]
        source_usage = _get(snapshot, "usage", default={}) or {}
        source_observed = _num(_get(source_usage, "observed_at"))
        stamp = _clock(max((v for v in observed if v is not None), default=source_observed or now))
        if stamp:
            system["usage_api"] = {"state": "off" if source_usage.get("api_disabled") else "on",
                                    "freshness": stamp}
    return system


def _epoch_of(value):
    """Epoch seconds of an ISO text or number, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            stamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return stamp.timestamp()          # a stamp without a zone is local time, as the journal writes it
    return None


def _memory_events(snapshot, tag_of_sid, tag_of_cwd, limit=24):
    """The journal's newest events as feed records ``[time, tag, source, kind, key, text]``."""
    memory = _get(snapshot, "memory")
    recent = memory.get("recent") if isinstance(memory, Mapping) else None
    events = []
    for event in recent or ():
        if not isinstance(event, Mapping):
            continue
        epoch = _epoch_of(event.get("ts"))
        text = _memory_text(event.get("snippet"))
        if epoch is None or text is None or event.get("action") in _BOOKKEEPING:
            continue
        action = "saved" if event.get("action") in _SAVED else "changed"
        tag = tag_of_sid.get(_text(event.get("sid"))) or tag_of_cwd.get(_text(event.get("cwd"))) or "·"
        events.append((epoch, [_clock(epoch), tag, "hearting", f"mem {action}", "dim", text]))
    events.sort(key=lambda item: -item[0])
    return events[:limit]


def _gpu_repo(cwd):
    parts = [part for part in str(cwd or "").split("/") if part]
    for index, part in enumerate(parts):
        if part.endswith("-wt"):
            return part[:-3]
    return parts[-1] if parts else None


def _short_gpu_command(command):
    if not isinstance(command, str) or not command.strip():
        return None
    parts = [part.rsplit("/", 1)[-1] for part in command.split()]
    first_script = next((i for i, part in enumerate(parts) if part.endswith(".py")), 0)
    shown = []
    for part in parts[first_script:]:
        if re.fullmatch(r"[0-9a-fA-F]{12,}", part.split("=")[-1]):
            if shown and shown[-1].startswith("-") and "=" not in shown[-1]:
                shown.pop()
            continue
        shown.append(part)
    return " ".join(shown) or None


def _gpu_process_records(snapshot):
    hosts = _host_blocks(snapshot) or ()
    unregistered = _records(_get(snapshot, "unregistered_gpu", default=()))
    unregistered_keys = {(str(item.get("host") or ""), str(item.get("pid") or ""))
                         for item in unregistered if isinstance(item, Mapping)}
    session_tags = {}
    for session in _records(_get(snapshot, "sessions", default=())):
        sid = _text(_get(session, "session_id", "sid"))
        tag = _text(_get(session, "session_tag", "tag"))
        harness = _text(_get(session, "harness"))
        if sid and tag and harness:
            session_tags[(harness, sid)] = tag
    processes = []
    for host in hosts:
        if not isinstance(host, Mapping):
            continue
        for gpu in host.get("gpus") or ():
            if not isinstance(gpu, Mapping):
                continue
            for process in gpu.get("processes") or ():
                if not isinstance(process, Mapping):
                    continue
                owner = process.get("session_owner") or {}
                owner_sid = _text(_get(owner, "session_id", "id"))
                owner_harness = _text(_get(owner, "harness"))
                elapsed = _num(process.get("elapsed_s"))
                gib = _gib(process.get("used_memory_mib"))
                processes.append({"host": host.get("host"), "gpu": gpu.get("index"),
                    # Keep the source identity for JSON/evidence, and carry only an exact
                    # current-board tag for the display. A departed or unmatched owner has
                    # no visible session tag; the full ID is not a display label.
                    "session_id": owner_sid,
                    "session_tag": session_tags.get((owner_harness, owner_sid)),
                    "cwd": _gpu_repo(process.get("cwd")),
                    "command": _short_gpu_command(process.get("command")),
                    "vram": (f"{gib:.1f} GB" if gib is not None else None),
                    "age": _dur(elapsed / 60) if elapsed is not None else None,
                    "registered": (str(host.get("host") or ""), str(process.get("pid") or ""))
                                  not in unregistered_keys})
    unregistered_rows = []
    for item in unregistered:
        if not isinstance(item, Mapping):
            continue
        elapsed = _num(item.get("elapsed_s"))
        unregistered_rows.append([item.get("host"), item.get("gpu_indexes", [item.get("gpu_name")])[0],
            _text(item.get("process_name")) or _short_gpu_command(item.get("command")),
            _gib(item.get("used_memory_mib")),
            _dur_coarse(elapsed / 60) if elapsed is not None else None])
    unregistered_rows.sort(key=lambda row: (-(row[3] or 0), str(row[0]), str(row[1])))
    owner_sid = next((_text(_get(item.get("session_owner"), "session_id", "id"))
                      for item in unregistered if isinstance(item, Mapping)
                      and item.get("session_owner")), None)
    owner = next((session_tags.get((_text(_get(session, "harness")), owner_sid))
                  for session in _records(_get(snapshot, "sessions", default=())) if owner_sid
                  and _text(_get(session, "session_id", "sid")) == owner_sid), None)
    count = len(unregistered_rows)
    return processes, unregistered_rows[:4], {"count": len(processes), "unregistered": count,
        "remaining": max(0, count - 4), "owner": owner}, owner


def _registry_events(focus, tag):
    """The focused session's newest registry lines as feed records."""
    events = []
    for event in _records(_get(focus, "events", default=())):
        if isinstance(event, Mapping) and event.get("kind") == "registry":
            epoch = _epoch_of(event.get("time_kst"))
            if epoch is None:
                continue
            what = " ".join(part for part in (event.get("worker_type"), event.get("route_node"),
                                              event.get("note")) if part)
            events.append((epoch, [_clock(epoch), event.get("tag") or tag or "·", "registry",
                                   str(event.get("status") or "·"), "dim", what or str(event.get("harness") or "·")]))
        elif isinstance(event, Mapping):
            events.append((0.0, [event.get("time"), event.get("tag"), event.get("source"),
                                 event.get("kind"), event.get("key"), event.get("text")]))
        else:
            events.append((0.0, list(event)))
    return events


def _peer_events(snapshot, session_index):
    observations = _get(snapshot, "observations", default={}) or {}
    sources = _get(observations, "replay_sources", default={}) or {}
    messages = _get(_get(sources, "peer_messages", default={}) or {}, "records", default=()) or ()
    tag_by_sid = {sid: tag for tag, info in session_index.items() for sid in info["ids"]}
    result = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        sender = message.get("from") or {}
        stamp = _epoch_of(message.get("ts"))
        if stamp is None:
            continue
        tag = tag_by_sid.get(_text(_get(sender, "session_id"))) or "·"
        text = _text(message.get("summary")) or _text(message.get("kind")) or "notice"
        result.append((stamp, [_clock(stamp), tag, "peer", _text(message.get("kind")) or "notice",
                                   "dim", text]))
    return sorted(result, key=lambda item: -item[0])[:5]


def _answered_records(snapshot, clock=None):
    """Decision records the user answered, per repo, newest first: ``[repo, [[HH:MM|MM-DD, text], ...]]``."""
    memory = _get(snapshot, "memory")
    by_repo = memory.get("by_repo") if isinstance(memory, Mapping) else None
    if not isinstance(by_repo, Mapping):
        return []
    grouped = {}
    for repo, events in by_repo.items():
        for event in events or ():
            if isinstance(event, Mapping) and event.get("action") == "decision-record" and \
                    event.get("type") == "decision" and _text(event.get("snippet")):
                project = _text(event.get("project"))
                if project and "/" in project:
                    project = _gpu_repo(project)
                project = project or _gpu_repo(_get(event, "cwd")) or _shown_repo(repo)
                grouped.setdefault(project, []).append((_epoch_of(event.get("ts")) or 0.0,
                    [_memory_when(event.get("ts"), clock), re.sub(r"^\[결정\]\s*", "", event["snippet"])]))
    return [[repo, [row for _epoch, row in sorted(rows, key=lambda item: -item[0])]]
            for repo, rows in sorted(grouped.items()) if rows]


# -- detail (the selection pump's reads) ----------------------------------------------------------

def _detail_parts(detail):
    """``(focus result, seats by harness:sid, config, panes)`` of whatever the feed handed over."""
    if detail is None:
        return None, {}, None, {}
    if hasattr(detail, "config"):
        return (_get(detail, "focus"), dict(_get(detail, "seats", default={}) or {}),
                detail.config, dict(_get(detail, "panes", default={}) or {}))
    return detail, {}, None, {}


def detail_bindings(snapshot):
    """Live process and route incarnations, never short badges or cwd guesses."""
    result = {}
    jobs = _records(_get(snapshot, "jobs", default=()))
    for session in _records(_get(snapshot, "sessions", default=())):
        harness = _text(_get(session, "harness"))
        sid = _text(_get(session, "session_id", "sid"))
        pid, start = _get(session, "pid"), _get(session, "proc_start")
        if not _is_main(session) or not harness or not sid or pid is None or start is None:
            continue
        routes = tuple(sorted((str(_get(job, "owner_route_id", "route_id") or ""),
                               str(_get(job, "attempt_id") or ""))
                              for job in jobs if _get(job, "parent_sid") == sid))
        result[f"{harness}:{sid}"] = (harness, sid, str(pid), str(start), routes,
            str(_get(session, "route_id") or ""), str(_get(session, "attempt_id") or ""))
    return result


def detail_for_bindings(detail, bindings, *, strict=False):
    """Keep accepted reads only for the process incarnation they observed."""
    from dataclasses import replace
    from .types import ExplorerDetail
    focus, seats, config, panes = _detail_parts(detail)
    results = _get(detail, "results", default={}) or {}

    def matches(result, ref):
        request = _get(result, "request")
        if request is None or not request.bindings:
            return not strict
        observed = dict(request.bindings)
        return ref in bindings and observed.get(ref) == bindings[ref]

    key = _get(focus, "key")
    ref = f"{_get(key, 'harness')}:{_get(key, 'sid')}"
    if focus is not None and not matches(focus, ref):
        focus = None
    seats = {ref: value for ref, value in seats.items()
             if ref == ENDED_SEATS or matches(results.get("seats"), ref)}
    panes = {sid: pane for sid, pane in panes.items()
             if any(value[1] == sid and matches(results.get("panes"), ref)
                    for ref, value in bindings.items())} if strict or results else panes
    auxiliary = _get(results.get("panes"), "source_records", default={}) or {}
    aux = auxiliary.get("aux")
    key = _get(aux, "key")
    if aux is not None and matches(aux, f"{_get(key, 'harness')}:{_get(key, 'sid')}"):
        if focus is None:
            focus = aux
        elif aux.key == focus.key:
            records = dict(aux.source_records)
            records.update({name: value for name, value in focus.source_records.items()
                            if name in ("seat", "dialogue", "lineage")})
            focus = replace(focus, source_records=records, obligations=aux.obligations,
                            events=aux.events, sources=focus.sources + aux.sources,
                            health=aux.health if aux.health.state != "success" else focus.health)
    return ExplorerDetail(focus=focus, seats=seats, config=config, panes=panes, results=results)


def _peer_snapshot(snapshot, detail, bindings):
    """Join the list worker's peer-only observation to the exact captured roster."""
    import copy
    from ..details import _PEER_FIELDS
    result = (_get(detail, "results", default={}) or {}).get("seats")
    observed = (_get(result, "source_records", default={}) or {}).get("peers")
    request = _get(result, "request")
    if (observed is None or request is None or not bindings or dict(request.bindings) != bindings
            or detail_bindings(observed.snapshot) != bindings):
        return snapshot
    fresh = (observed.snapshot.observations or {}).get("optional:peers", {})
    current = (_get(snapshot, "observations", default={}) or {}).get("optional:peers", {})
    if (fresh.get("observed_at") or 0) < (current.get("observed_at") or 0):
        return snapshot
    def process_key(session):
        return (_get(session, "harness"), _get(session, "session_id", "sid"),
                str(_get(session, "pid")), str(_get(session, "proc_start")))
    old = {process_key(s): s for s in observed.snapshot.sessions}
    value = copy.copy(snapshot)
    sessions = []
    for session in _get(snapshot, "sessions", default=()):
        row = copy.copy(session)
        prior = old.get(process_key(row))
        if prior is not None:
            for field in _PEER_FIELDS:
                field_value = copy.deepcopy(getattr(prior, field, None))
                if isinstance(row, dict):
                    row[field] = field_value
                else:
                    setattr(row, field, field_value)
        sessions.append(row)
    observations = dict(_get(snapshot, "observations", default={}) or {})
    observations["optional:peers"] = dict(fresh)
    if isinstance(value, Mapping):
        value = dict(value, sessions=sessions, observations=observations)
    else:
        value.sessions, value.observations = sessions, observations
    return value


def selected_detail(model, detail, *, peer_model=None):
    """Apply local selected reads to a complete base, preserving its rows and ordering."""
    if model is None:
        return model
    from .view_model import freeze
    provenance = model.source_records or {}
    bindings = dict(provenance.get("_detail_bindings", {}))
    candidate_sources = _get(peer_model, "source_records", default={}) or {}
    # Snapshot peers are independent of HISTORY's selected body. Only the observed
    # graph may advance over a pending base; no rows, targets or readiness travel.
    if (bindings and bindings == candidate_sources.get("_detail_bindings")
            and provenance.get("_detail_tags") == candidate_sources.get("_detail_tags")
            and {row["tag"]: row.get("repo_id", row.get("repo")) for row in model.sessions} ==
                {row["tag"]: row.get("repo_id", row.get("repo")) for row in peer_model.sessions}
            and (candidate_sources.get("_peer_observed_at") or 0) >=
                (provenance.get("_peer_observed_at") or 0)):
        observation = (_get(peer_model, "system", default={}) or {}).get("peer_observation", {})
        if candidate_sources.get("_peer_observed_at") is not None:
            changes = {}
            if observation.get("state") == "success":
                changes.update(peer_nodes=peer_model.peer_nodes, peer_edges=peer_model.peer_edges)
            else:
                prior = (model.system or {}).get("peer_observation", {})
                observation = dict(observation, last_good=bool(observation.get("last_good") or
                    prior.get("state") == "success" or prior.get("last_good")))
            provenance = {**provenance, "_peer_observed_at": candidate_sources.get("_peer_observed_at")}
            model = model.with_updates(**changes, system=freeze({**(model.system or {}),
                            "peer_observation": observation}), source_records=provenance)
    if detail is None:
        return model
    detail = detail_for_bindings(detail, bindings, strict=True)
    focus, seats, config, panes = _detail_parts(detail)
    sources = _get(focus, "source_records", default={}) or {}
    tag_of = provenance.get("_detail_tags", {})
    rows = [dict(row) for row in model.sessions]
    index = {row["tag"]: {"sid": ref.partition(":")[2]}
             for ref, tag in tag_of.items() for row in rows if row["tag"] == tag}
    bound_rows = [row for row in rows if row["tag"] in index]
    seat_rows, generations, cards, dialogue, pane_read, raw = _detail_sections(
        bound_rows, index, detail, time.time())
    touched = {tag_of[ref] for ref in seats if ref in tag_of}
    key = _get(focus, "key")
    ref = f"{_get(key, 'harness')}:{_get(key, 'sid')}"
    tag = tag_of.get(ref)
    if tag and "seat" in sources:
        touched.add(tag)
    old_seats = [seat for seat in model.seats if seat[0] not in touched]
    new_generations = {key: value for key, value in (model.generations or {}).items() if key not in touched}
    new_cards = {key: value for key, value in (model.cards or {}).items() if key not in touched}
    new_generations.update(generations)
    new_cards.update(cards)
    old_dialogue = dict(model.dialogue or {})
    logs = dict(old_dialogue.get("logs", {}))
    if tag and "seat" in sources and not sources["seat"]:
        logs = {key: value for key, value in logs.items() if not key.startswith(tag + ":")}
    logs.update((dialogue or {}).get("logs", {}))
    old_dialogue["logs"] = logs
    by_tag = {row["tag"]: row for row in rows}
    for current_tag in touched:
        by_tag[current_tag]["generation"] = by_tag[current_tag]["last_clear"] = None
    seen = {current_tag: panes[ref.partition(":")[2]] for ref, current_tag in tag_of.items()
            if current_tag in by_tag and ref.partition(":")[2] in panes}
    if ENDED_SEATS in seats:
        old_seats = [seat for seat in old_seats if not ended_seat(seat)]
    for seat in seat_rows:
        if ended_seat(seat):
            continue
        by_tag[seat[0]]["generation"] = seat[2]
        by_tag[seat[0]]["last_clear"] = None if seat[3] == "·" else seat[3].split(" · ")[0]
    panes_result = detail.results.get("panes")
    pane_request = _get(panes_result, "request")
    if tag in by_tag and pane_request is not None and "pane" in pane_request.wants:
        # Last-good belongs to the feed's exact request, not to an older
        # selection embedded in the complete base. A failed new request has no
        # observed body; a failed refresh carries its retained body below.
        by_tag[tag]["pane_lines"] = []
    if pane_read:
        current_tag, read = pane_read
        if _text(read.get("pane")):
            seen.setdefault(current_tag, _text(read.get("pane")))
        by_tag[current_tag]["pane_lines"] = [[str(line), "soft"] for line in read.get("lines") or ()]
        by_tag[current_tag]["pane_meta"] = "visible screen"
    health_results = dict(detail.results)
    if panes_result is not None:
        health_results["panes:aux"] = panes_result.source_records.get("aux")
    system = dict(model.system or {})
    events = model.events
    if focus is not None:
        session_index = {tag_of[ref]: {"session": {"harness": binding[0]}, "ids": (binding[1],)}
                         for ref, binding in bindings.items() if ref in tag_of}
        system["task_summary"] = _current_task_summary(detail, rows, session_index, model.routes)
        other_events = provenance.get("_detail_other_events")
        if other_events is not None:
            feed = list(other_events) + _registry_events(focus, tag)
            feed.sort(key=lambda item: -item[0])
            events = tuple(freeze(event) for _epoch, event in feed[:5])
    questions = [dict(question) for question in model.questions or ()]
    settle_panes(rows, seen, questions)
    return model.with_updates(sessions=rows, questions=tuple(freeze(row) for row in questions),
                   seats=tuple(freeze(row) for row in old_seats + seat_rows),
                   generations=freeze(new_generations), cards=freeze(new_cards),
                   dialogue=freeze(old_dialogue), attempts=freeze(raw) if raw is not None else model.attempts,
                   models=freeze(config) if config is not None else model.models,
                   system=freeze(system), events=events,
                   source_records={**provenance, "_detail_health": {
                       kind: {"state": result.health.state, "error": result.health.error}
                       for kind, result in health_results.items() if result is not None}})


def reorder_view_model(model, effects):
    """Reorder the displayed identities after their ordering effect succeeded."""
    if model is None or not isinstance(model.ordering, Mapping):
        return model
    from . import order
    ordering = dict(model.ordering)
    saved = {"repos": list(ordering.get("repo_keys", ())),
             "sessions": {repo: list(keys) for repo, keys in ordering.get("session_keys", {}).items()}}
    for _name, kind, repo, source, target in effects:
        saved = (order.move_repo(saved, source, target) if kind == "repo" else
                 order.move_session(saved, repo, source, target))
    rows = [dict(row, order_key=ordering.get("tag_keys", {}).get(row["tag"], ""))
            for row in model.sessions]
    ordered = order.reconcile_order(saved, model.repos, rows)
    ordering.update(saved=saved, repo_keys=ordered["repo_order"], session_keys=ordered["session_order"],
                    tag_cycle=["session:" + row["tag"] for row in ordered["sessions"]])
    return model.with_updates(repos=tuple(ordered["repos"]), sessions=tuple(ordered["sessions"]), ordering=ordering)


def _stamp(epoch, now):
    value = _num(epoch)
    if value is None:
        value = _epoch_of(epoch)
    if value is None:
        return None
    try:
        when = datetime.datetime.fromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return None
    return when.strftime("%m-%d %H:%M")


def _clear_text(entries, now):
    """When the seat was last cleared: ``HH:MM`` today (``· Nm ago`` within the hour), else ``MM-DD HH:MM``."""
    cleared = [entry for entry in entries if entry.get("source") == "clear" and _num(entry.get("started_at"))]
    if not cleared:
        return None
    epoch = max(float(entry["started_at"]) for entry in cleared)
    try:
        when = datetime.datetime.fromtimestamp(epoch)
        today = datetime.datetime.fromtimestamp(now).date()
    except (OverflowError, OSError, ValueError):
        return None
    if when.date() != today:
        return when.strftime("%m-%d %H:%M")
    minutes = int(max(0, now - epoch) // 60)
    return when.strftime("%H:%M") + (f" · {minutes}m ago" if minutes < 60 else "")


def _seat_view(tag, repo, harness, sid, entries, now, now_text=None, ended=False):
    """``(seat record, generation records, cards or None)`` of one session's seat history."""
    from .nav import generation_chain
    chain = generation_chain(entries, harness, sid, ended=ended)
    full = any("lines" in card for entry in entries for card in (entry.get("cards") or ()))
    generations, marks, card_rows, prior_rows = [], [], [], []
    latest_title = ""
    for item in chain:
        card = item["card"] or {}
        title = _text(card.get("title")) or (_text(now_text) if item["now"] and not item["card"] else "") or ""
        if title and item["card"]:
            latest_title = title
        generations.append({
            "harness": _text(item["harness"]), "sid": str(item["sid"] or "")[:8],
            "at": "now" if item["now"] else (_stamp(card.get("at"), now) or ""),
            "kind": item["kind"], "connection": item["connection"], "note": None,
            "card": bool(item["card"] and item["card"].get("status") == "available"),
            "detail": full, "title": title})
        record_entry = item.get("entry") or next((entry for entry in entries
            if entry.get("sid") == item.get("sid") and entry.get("harness") == item.get("harness")), {})
        if record_entry.get("journal_records"):
            generations[-1]["journal_records"] = record_entry["journal_records"]
            generations[-1]["detail"] = True
        marks.append([_text(item["harness"]) or "·", item["kind"]])
        if full and item["card"] and "lines" in card:
            row = {"gen": card.get("generation") or len(generations), "harness": _text(item["harness"]),
                   "sid": str(item["sid"] or "")[:8], "at": _stamp(card.get("at"), now) or "",
                   "chars": int(card.get("chars") or 0), "lines": list(card["lines"])}
            (prior_rows if (item["entry"] or {}).get("prior") else card_rows).append(row)
    has_card = any(g["card"] for g in generations)
    seat = [tag, repo, len(chain), _clear_text(entries, now) or "·", has_card, latest_title, marks]
    seat_id = next((_text(entry.get("seat")) for entry in entries if _text(entry.get("seat"))), tag)
    handovers = next((list(entry["handovers"]) for entry in entries if entry.get("handovers")), [])
    cards = {"seat": seat_id, "cards": card_rows, "handovers": handovers,
             "prior_cards": prior_rows} if full else None
    return seat, generations, cards


ENDED_SEATS = "ended"                     # the seats key HISTORY's ended seats travel under


def ended_seat(seat):
    """The ``ended`` record of a HISTORY seat row, or None for a live seat."""
    extra = seat[7] if len(seat) > 7 else None
    return extra if isinstance(extra, Mapping) and extra.get("ended") else None


def _ended_seat_rows(ended, seat_rows, session_index, now, resolver):
    """HISTORY rows for seats whose session ended: short SID and ``ended``, never a freed tag.

    A seat whose chain continues in a live session that does not list it is drawn under that
    seat with the PEERS handover words (``gen N → [tag]``).
    """
    tag_by_sid = {known: tag for tag, info in session_index.items()
                  for known in (*info.get("ids", ()), info.get("sid")) if known}
    generation_count = {seat[0]: seat[2] for seat in seat_rows}
    items = [item for item in ended or ()
             if isinstance(item, Mapping) and item.get("entries") and item.get("sid")]
    shown = _short_ids([str(item["sid"]) for item in items])
    rows, generations, cards = [], {}, {}
    for item in items:
        harness, sid = _text(item.get("harness")) or "·", str(item["sid"])
        key = "~" + harness + ":" + sid             # the whole id: short prefixes repeat
        seat, gens, card_block = _seat_view(key, _repo_of(resolver, _text(item.get("cwd"))),
                                            harness, sid, list(item["entries"]), now, ended=True)
        successor = item.get("successor") or ()
        tag = tag_by_sid.get(successor[1]) if len(successor) == 2 else None
        count = generation_count.get(tag)
        seat.append({"ended": True, "sid": shown[sid], "harness": harness,
                     "at": _ended_text(_num(item.get("ended_at")), now),
                     "successor": tag, "gen": count - 1 if count else None})
        rows.append(seat)
        generations[key] = gens
        if card_block is not None:
            cards[key] = card_block
    return rows, generations, cards


def _short_ids(sids, width=8):
    """Display ids: the first ``width`` characters, longer only where two would read the same.

    OpenCode ids share their ``ses_`` start and Codex ids a time-ordered prefix, so the cut
    grows per id until it is unique among the ids shown together.
    """
    shown = {}
    for sid in sids:
        cut = width
        while cut < len(sid) and any(other != sid and other[:cut] == sid[:cut] for other in sids):
            cut += 1
        shown[sid] = sid[:cut]
    return shown


def _ended_text(epoch, now):
    if epoch is None:
        return "ended"
    try:
        when = datetime.datetime.fromtimestamp(epoch)
        today = datetime.datetime.fromtimestamp(now).date()
    except (OverflowError, OSError, ValueError):
        return "ended"
    return "ended " + when.strftime("%H:%M" if when.date() == today else "%m-%d")


def _detail_sections(rows, session_index, accepted_detail, now, resolver=None):
    """Seat, generation, card, dialogue, pane and raw-output sections from the pump's results."""
    focus, seats, _config, _panes = _detail_parts(accepted_detail)
    seats = dict(seats)
    ended = seats.pop(ENDED_SEATS, None)
    by_ref = {}
    for row in rows:
        info = session_index[row["tag"]]
        by_ref[f"{row['harness']}:{info['sid']}"] = row
    seat_rows, generations, cards, dialogue = [], {}, {}, {}
    sources = _get(focus, "source_records") if focus is not None else None
    sources = sources if isinstance(sources, Mapping) else {}
    focus_key = _get(focus, "key")
    focus_ref = f"{_get(focus_key, 'harness')}:{_get(focus_key, 'sid')}" if focus_key is not None else None
    merged = dict(seats)
    if focus_ref and sources.get("seat") is not None:
        merged[focus_ref] = sources["seat"]
    for row in rows:                       # list order is the view's order
        ref = f"{row['harness']}:{session_index[row['tag']]['sid']}"
        entries = merged.get(ref)
        if entries is None:
            continue
        if not entries:
            generations[row["tag"]] = []
            continue
        seat, gens, card_block = _seat_view(row["tag"], row["repo"], row["harness"],
                                            session_index[row["tag"]]["sid"], entries, now, row.get("now"))
        seat_rows.append(seat)
        generations[row["tag"]] = gens
        if card_block is not None:
            cards[row["tag"]] = card_block
    if ended is not None:
        from ..model import project_of
        ended_rows, ended_generations, ended_cards = _ended_seat_rows(
            ended, seat_rows, session_index, now, resolver or project_of)
        seat_rows.extend(ended_rows)
        generations.update(ended_generations)
        cards.update(ended_cards)
    pane_read, raw, spoken = None, None, None
    if focus_ref in by_ref:
        tag = by_ref[focus_ref]["tag"]
        if isinstance(sources.get("pane_read"), Mapping):
            pane_read = (tag, sources["pane_read"])
        if isinstance(sources.get("dialogue"), Mapping):
            spoken = (tag, sources["dialogue"])
        if isinstance(sources.get("raw"), Mapping):
            raw = sources["raw"]
    if spoken:
        tag, log = spoken
        dialogue = {"logs": {f"{tag}:{int(log['index'])}": {
            "h": _text(log.get("harness")), "sid": str(log.get("sid") or "")[:8],
            "lines": list(log.get("lines") or ())}}, "generation_text": {}}
    recorded = sources.get("logs")                    # every generation's conversation the pump has read
    if isinstance(recorded, Mapping) and recorded:
        logs = dict(dialogue.get("logs", {})) if dialogue else {}
        for key, entry in recorded.items():
            if isinstance(entry, Mapping) and key not in logs:
                logs[str(key)] = {"h": _text(entry.get("h", entry.get("harness"))),
                                  "sid": str(entry.get("sid") or "")[:8],
                                  "lines": list(entry.get("lines") or ())}
        dialogue = {"logs": logs, "generation_text": dict((dialogue or {}).get("generation_text", {}))}
    return seat_rows, generations, cards, dialogue, pane_read, raw


def route_marker_state(route):
    """The one route marker every Fleet route row draws: ``working`` (⠏), ``idle`` (● waits),
    ``blocked`` (● answer), ``dead`` (✕), ``done`` (✓) or ``stale`` (◌ no response).

    A route whose worker still runs reads as working; an owner that only waits on its
    worker reads as waiting, the worker's own line being the one that spins."""
    if route.get("owner_waits"):
        return "idle"
    if route.get("activity") == "running":
        return "working"
    state = str(route.get("state") or "")
    return state if state in ("working", "blocked", "dead", "done", "stale") else "idle"


def route_state_counts(routes):
    """``{"running", "waiting", "stopped"}`` over current routes, by the shared marker state."""
    counts = {"running": 0, "waiting": 0, "stopped": 0}
    for route in routes:
        if route.get("current") is not True:
            continue
        state = route_marker_state(route)
        if state == "working":
            counts["running"] += 1
        elif state in ("idle", "blocked"):
            counts["waiting"] += 1
        elif state == "dead":
            counts["stopped"] += 1
    return counts


def current_route_counts(routes):
    """The one OVERVIEW title count/open rule, shared by session-local displays."""
    counts, opened = {}, set()
    for route in routes:
        tag = route.get("session_tag")
        if tag and route.get("current") is True:
            counts[tag] = counts.get(tag, 0) + 1
            if route.get("state") not in ("done", "stale"):
                opened.add(tag)
    return counts, opened


def _owner_text(route):
    """``codex (GPT-5.5·high)``: the route owner exactly as Fleet's HARNESS column reads it."""
    harness = _text(route.get("harness"))
    if not harness:
        return ""
    pair, effort = route.get("model"), _text(route.get("effort"))
    name = _text(pair[0]) if isinstance(pair, (list, tuple)) and pair else None
    if name:
        return f"{harness} ({name}{'·' + effort if effort else ''})"[:160]
    return (harness + ("·" + effort if effort else ""))[:160]


def _worker_pieces(worker):
    """Fleet's route body line ``[label, harness segments, state segments]`` as flat text pieces:
    ``owner codex (GPT-5.5·high) · running 5m`` / ``worker codex · running 3m``."""
    if not isinstance(worker, (list, tuple)) or not worker:
        return []
    pieces = [[str(worker[0])[:16], "dim"]]
    segments = worker[1] if len(worker) > 1 and isinstance(worker[1], (list, tuple)) else ()
    states = worker[2] if len(worker) > 2 and isinstance(worker[2], (list, tuple)) else ()
    texts = [str(part[0]) for part in segments if isinstance(part, (list, tuple)) and part]
    if texts:
        pieces.append([" " + "".join(texts)[:120], "dim"])
    for index, part in enumerate(part for part in states if isinstance(part, (list, tuple)) and part):
        pieces.append([(" · " if index == 0 else " ") + str(part[0])[:120], "dim"])
    return pieces[:8]


def session_route_status(snapshot, now=None, *, include_details=False):
    """Question-free labels from the same route records OVERVIEW consumes."""
    now = time.time() if now is None else now
    resolver = _resolver(snapshot)
    rows, index = _session_records(snapshot, now, resolver)
    routes, _ = _route_records(snapshot, rows, index, resolver, now)
    counts, opened = current_route_counts(routes)
    result = {}
    for tag in opened:
        stages = []
        for route in routes:
            if (route.get("session_tag") != tag or route.get("current") is not True
                    or route.get("state") in ("done", "stale")):
                continue
            stage = route.get("worker_stage")
            # Route ids are source text. Never pass terminal control sequences
            # or unbounded content into a human-visible shell/TUI label.
            if isinstance(stage, str) and re.fullmatch(r"[A-Za-z0-9_./-]{1,64}", stage):
                if stage not in stages:
                    stages.append(stage)
        label = f"route {counts[tag]}"
        if stages:
            label += " · " + ", ".join(stages[:3])
            if len(stages) > 3:
                label += f" +{len(stages) - 3}"
        session = index[tag]["session"]
        harness = _get(session, "harness")
        for sid in index[tag]["ids"]:
            result[str(harness) + "\0" + sid] = label
    if not include_details:
        return result
    # Reuse this very interpretation for the shell. No second collection or
    # authority store: bounded display fields in the existing projection.
    details = {}
    for tag, info in index.items():
        selected = [r for r in routes if r.get("session_tag") == tag and r.get("current") is True]
        recent = sorted((r for r in routes if r.get("session_tag") == tag and not r.get("current")
                         and r.get("state") == "done" and r.get("outcome") and r.get("result_at") is not None),
                        key=lambda r: r["result_at"], reverse=True)
        selected += recent[:2]
        records = []
        for route in selected[:8]:
            row = {key: str(route.get(key) or "")[:320] for key in
                   ("id", "title", "state", "shape", "worker_stage", "outcome", "repo", "capability")}
            row["stages"] = [{"name": str(stage.get("name") or "")[:64],
                              "st": str(stage.get("st") or "")[:24]}
                             for stage in (route.get("stages") or ())[:16]]
            row["stage_more"] = max(0, len(route.get("stages") or ()) - 16)
            if type(route.get("result_at")) in (int, float):
                row["result_at"] = route["result_at"]
            row["worker"] = _worker_pieces(route.get("worker"))
            row["owner"] = _owner_text(route)
            visibility = route.get("visibility")
            if isinstance(visibility, dict) and visibility.get("schema") == "route_visibility_v1" and visibility.get("route_id") == row["id"]:
                row["visibility"] = {"summary": str(visibility.get("summary") or "")[:160],
                                     "next": str(visibility.get("next") or "")[:120],
                                     "lines": [[str(line[0])[:12], str(line[1])[:200], str(line[2])[:8]]
                                               for line in (visibility.get("lines") or ())[:14]
                                               if isinstance(line, (list, tuple)) and len(line) == 3]}
                # Already bounded by the shared reader. Keep its codes intact;
                # presentation translates them, never reinterprets report prose.
                codes = visibility.get("codes")
                if isinstance(codes, dict) and codes.get("schema") == "route_visibility_codes_v1":
                    row["visibility"]["codes"] = codes
            records.append(row)
        for sid in info["ids"]:
            details[str(_get(info["session"], "harness")) + "\0" + sid] = records
    return result, details


def session_index(snapshot, now=None):
    """``{tag: {"harness", "sid", "ids", "session", "repo"}}`` for the main sessions, with the tags the
    explorer draws; the feed uses it to turn what the person opened back into the session to read."""
    clock = time.time() if now is None else now
    resolver = _resolver(snapshot)
    rows, index = _session_records(snapshot, clock, resolver)
    by_tag = {row["tag"]: row for row in rows}
    return {tag: {"harness": by_tag[tag]["harness"], "sid": info["sid"], "ids": info["ids"],
                  "session": info["session"], "repo": by_tag[tag]["repo"], "title": by_tag[tag]["title"]}
            for tag, info in index.items()}


# -- entry --------------------------------------------------------------------------------

def _install_record(snapshot):
    install = _get(snapshot, "hearting")
    if not isinstance(install, Mapping) or not _text(install.get("version")):
        return None
    version = str(install["version"])
    try:
        from .. import render
        segments = render._hearting_version_segments(version)
        version = "".join(text for text, _key in segments)
    except Exception:
        pass
    record = {"version": version if version[:1] == "v" or not version[:1].isdigit() else "v" + version}
    for key in ("managed", "pinned", "distribution"):
        if key in install:
            record[key] = install[key]
    method = _text(install.get("install_method"))
    if method:                                   # "managed/pinned" says both at once
        parts = method.split("/")
        record.setdefault("managed", "managed" in parts)
        record.setdefault("pinned", "pinned" in parts)
    if _text(install.get("source")):
        record.setdefault("distribution", install["source"] == "distribution")
    return record


def _header_identity(snapshot):
    """Keep the collector's already validated public install identity for the Explorer header."""
    install = _get(snapshot, "hearting")
    if not isinstance(install, Mapping):
        return None
    return {
        "version": _text(install.get("version")) or "unknown",
        "install_method": _text(install.get("install_method")) or "unmanaged",
    }


def build_view_model(snapshot, accepted_detail=None, cached_config=None, now=None, *, previous=None):
    """Project facts only; identity and liveness decisions stay with their source builders."""
    clock = time.time() if now is None else now
    fixture_values = _get(snapshot, "values")
    if isinstance(fixture_values, Mapping):
        return current_claude_usage(ExplorerViewModel.from_mapping(fixture_values, previous=previous), snapshot, clock)
    if isinstance(snapshot, Mapping) and "schema" in snapshot and "sessions" in snapshot:
        return current_claude_usage(ExplorerViewModel.from_mapping(snapshot, previous=previous), snapshot, clock)
    bindings = detail_bindings(snapshot)
    # Global view-only memory is independent of the selected session's incarnation.
    journal = _journal_groups(snapshot, clock)
    memory = _memory_records(journal, accepted_detail)
    accepted_detail = detail_for_bindings(accepted_detail, bindings)
    snapshot = _peer_snapshot(snapshot, accepted_detail, bindings)
    resolver = _resolver(snapshot)
    session_rows, session_index = _session_records(snapshot, clock, resolver)
    order = _get(snapshot, "session_order")
    if order:
        position = {str(sid): i for i, sid in enumerate(order)}
        sid_of = {row["tag"]: session_index[row["tag"]]["sid"] for row in session_rows}
        session_rows.sort(key=lambda row: position.get(str(sid_of.get(row["tag"])), len(position)))
    routes, nodes = _route_records(snapshot, session_rows, session_index, resolver, clock)
    results = _repo_results(snapshot, resolver, clock)
    repos = _repo_records(session_rows, routes, results, clock)
    if not order:
        latest_by_repo, mtime_by_tag = {}, {}
        for row in session_rows:
            info = session_index[row["tag"]]
            stamp = _num(_get(info["session"], "mtime")) or 0
            latest_by_repo[row["repo_id"]] = max(latest_by_repo.get(row["repo_id"], 0), stamp)
            mtime_by_tag[row["tag"]] = stamp
        repos.sort(key=lambda row: (-latest_by_repo.get(row["id"], 0), row["id"]))
        repo_rank = {row["id"]: index for index, row in enumerate(repos)}
        session_rows.sort(key=lambda row: (repo_rank.get(row["repo_id"], len(repo_rank)),
                                           -mtime_by_tag.get(row["tag"], 0), row["tag"]))
    questions, answer_wait_summary = _question_records(snapshot, session_rows, session_index, clock,
                                                       routes=routes, resolver=resolver)
    _repo_status(repos, snapshot, session_rows, session_index, routes, questions, results, clock)
    peer_nodes, peer_edges = _peer_records(session_rows, session_index, clock, snapshot)
    hosts, compute = _host_records(snapshot, session_index)
    gpu_lines = _session_gpu_lines(snapshot, session_index)
    for row in session_rows:
        row["gpu"] = gpu_lines.get(row["tag"])
    usage = _usage_records(snapshot, session_rows, clock)
    by_repo = {}
    for row in session_rows:
        by_repo.setdefault(row["repo_id"], []).append(row)
    session_rows = [row for repo in repos for row in stewards_first(by_repo.get(repo["id"], ()))]
    focus, _seats, detail_config, panes = _detail_parts(accepted_detail)
    seen = {}                                         # panes the current herdr map found per session
    for row in session_rows:
        found = next((panes[i] for i in session_index[row["tag"]]["ids"] if i in panes), None)
        if found:
            seen[row["tag"]] = found
    seat_rows, generations, cards, dialogue, pane_read, raw = _detail_sections(
        session_rows, session_index, accepted_detail, clock, resolver)
    peer_nodes, peer_edges = _finish_peers(peer_nodes, peer_edges, session_rows, session_index, routes,
                                           _handover_links(snapshot, session_index, seat_rows, clock),
                                           _routeless_jobs(snapshot, session_index))
    by_tag = {row["tag"]: row for row in session_rows}
    for seat in seat_rows:
        if ended_seat(seat):
            continue
        row = by_tag[seat[0]]
        row["generation"] = seat[2]
        row["last_clear"] = None if seat[3] == "·" else seat[3].split(" · ")[0]
    cleared = _clear_times(snapshot, session_index)
    for row in session_rows:
        if row["last_clear"] is None and row["tag"] in cleared and not any(seat[0] == row["tag"] for seat in seat_rows):
            when = datetime.datetime.fromtimestamp(cleared[row["tag"]])
            today = datetime.datetime.fromtimestamp(clock).date()
            row["last_clear"] = when.strftime("%H:%M" if when.date() == today else "%m-%d %H:%M")
    if pane_read:
        tag, read = pane_read
        if _text(read.get("pane")):
            seen.setdefault(tag, _text(read.get("pane")))
        by_tag[tag]["pane_meta"] = "visible screen" + (f" · {_clock(read['at'])}" if _clock(read.get("at")) else "")
        by_tag[tag]["pane_lines"] = [[str(line), "soft"] for line in read.get("lines") or ()]
    settle_panes(session_rows, seen, questions)
    tag_of_sid = {info["sid"]: tag for tag, info in session_index.items() if info["sid"]}
    tag_of_cwd = {}
    for tag, info in session_index.items():
        cwd = _text(_get(info["session"], "cwd"))
        if cwd:
            tag_of_cwd[cwd] = None if cwd in tag_of_cwd else tag
    tag_of_cwd = {cwd: tag for cwd, tag in tag_of_cwd.items() if tag}
    focus_tag = None
    focus_key = _get(focus, "key")
    if focus_key is not None:
        focus_tag = tag_of_sid.get(_get(focus_key, "sid"))
    feed = (_memory_events(snapshot, tag_of_sid, tag_of_cwd) +
            _registry_events(focus, focus_tag) + _peer_events(snapshot, session_index) +
            _job_events(snapshot, session_index, session_rows, clock))
    feed.sort(key=lambda item: -item[0])
    feed = feed[:5]
    explicit = _get(focus, "source_records")
    answered = _answered_records(snapshot, clock)
    system = _system_record(snapshot, session_rows, compute, accepted_detail, usage, clock)
    system["answer_waits"] = answer_wait_summary
    observations = _get(snapshot, "observations", default={}) or {}
    peer_observation = observations.get("optional:peers") if isinstance(observations, Mapping) else None
    system["peer_observation"] = {
        "state": _text(_get(peer_observation, "state")) or "unknown",
        "last_good": bool(_get(peer_observation, "last_good", default=False)),
    }
    system["project_memory"] = _project_memory(snapshot, routes, session_rows, journal)
    work_records = _work_journal_rows(snapshot)
    if work_records:
        system["journal_records"] = work_records
    system["task_summary"] = _current_task_summary(
        accepted_detail, session_rows, session_index, routes)
    if answered:
        system["answered_total"] = sum(len(rows) for _repo, rows in answered)
    data = {
        "identity": _get(snapshot, "generation", "identity"),
        "hearting": _header_identity(snapshot),
        "clock": clock,
        "hosts": hosts, "usage": usage, "census": None,
        "repos": repos, "sessions": session_rows, "routes": routes, "nodes": nodes,
        "attempts": raw,
        "questions": questions, "answered": answered,
        "seats": seat_rows, "generations": generations, "cards": cards or None,
        "dialogue": dialogue or None,
        "peer_nodes": peer_nodes, "peer_edges": peer_edges,
        "events": [event for _epoch, event in feed],
        "compute": compute,
        "memory": memory,
        "models": detail_config if detail_config is not None else _get(cached_config, "models", "model_config"),
        "config": cached_config,
        "system": system,
        "pane_graph": _records(_get(snapshot, "pane_graph", "pane_edges", default=())),
        "source_records": {**(explicit if isinstance(explicit, Mapping) else {}),
                           "_detail_bindings": bindings,
                           "_detail_other_events": tuple((epoch, event) for epoch, event in feed
                                                         if event[2] != "registry"),
                           "_peer_observed_at": _get(peer_observation, "observed_at"),
                           "_detail_tags": {f"{row['harness']}:{session_index[row['tag']]['sid']}": row["tag"]
                                            for row in session_rows
                                            if f"{row['harness']}:{session_index[row['tag']]['sid']}" in bindings}},
    }
    return ExplorerViewModel.from_mapping(data, previous=previous)


def from_mapping(value):
    return ExplorerViewModel.from_mapping(value)
