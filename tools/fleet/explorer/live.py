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
            "ask": _ask_kind(session) if state == "blocked" else None,
            "age": age, "repo": repo, "repo_id": repo,
            "branch": _text(_get(session, "branch")),
            "pane": _text(_get(session, "herdr_pane", "pane", "pane_id")) or
                    _text(_get(_get(session, "session_identity_evidence"), "pane")) or
                    delivery_panes.get(_text(_get(session, "session_id", "sid", "id")) or ""),
            "generation": None, "last_clear": None,
            "running": _running(session), "gpu": None, "pane_meta": None, "pane_lines": [],
        }
        rows.append(record)
        sid = _text(_get(session, "session_id", "sid", "id"))
        chain = _get(session, "route_chain")
        chain_routes = {_text(node.get("route_id")) for node in (chain.get("nodes") or ())
                        if isinstance(node, Mapping)} if isinstance(chain, Mapping) else set()
        by_key[tag] = {"session": session, "sid": sid, "interaction": interaction,
                       "ids": tuple(i for i in (sid, *(_get(session, "session_aliases") or ())) if _text(i)),
                       "chain": chain_routes - {None}}
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


def _worker_segments(node, resources, state_text=None):
    """``[label, harness segments, state segments]`` of the route's worker, or None."""
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
    segs = [[harness, "hb_" + harness]]
    pair = _model_pair(node.get("model"))
    effort = _effort(node.get("effort"))
    if pair:
        segs += [[" (", _SEG_DIM], [pair[0], pair[1]]]
        if effort:
            segs += [["·", _SEG_DIM], [effort, "eff_" + effort if effort in ("medium", "xhigh") else _SEG_DIM]]
        segs.append([")", _SEG_DIM])
    state = []
    if state_text:
        state.append([state_text, _SEG_DIM])
    elif elapsed:
        state.append([(f"running {elapsed}" if running else f"{elapsed} · worker ended"), _SEG_DIM])
    return ["worker", segs, state]


def _owner_job(jobs):
    owners = [job for job in jobs
              if (_get(job, "depth") == 1 or _get(job, "dispatch_depth") == 1)
              and not _get(job, "route_node")]
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
    """The route's name: its own recorded outcome name, else that of the frame that opened it
    (the nearest earlier ``route-frame`` node), else the nearest earlier named node."""
    index = _chain_position(nodes, rid)
    if index is None:
        return None
    own = _text(_get(nodes[index].get("result"), "name"))
    if own:
        return own
    before = nodes[:index]
    for node in reversed(before):
        if node.get("capability") == "route-frame" and _text(_get(node.get("result"), "name")):
            return node["result"]["name"]
    for node in reversed(before):
        if _text(_get(node.get("result"), "name")):
            return node["result"]["name"]
    return None


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


def _route_title_line(rid, owner, resume):
    harness = _text(owner.get("harness"))
    parts = [[f"  {rid[:11]} · owner ", _SEG_DIM]]
    if harness:
        parts.append([harness, "hb_" + harness])
        model, effort = owner.get("model"), owner.get("effort")
        if model:
            parts += [[" (", _SEG_DIM], [model[0], model[1]], ["·", _SEG_DIM],
                      [effort or "", "eff_" + effort if effort in ("medium", "xhigh") else _SEG_DIM],
                      [")", _SEG_DIM]]
    if resume:
        parts += [["  resumes from ", _SEG_DIM], [resume, "lvl_y"]]
    return parts


def _node_detail(node, now):
    """One stage row of the route page, from the route record's node."""
    state = _stage_state(node)
    harness = _text(node.get("harness"))
    model = _text(node.get("model"))
    effort = _effort(node.get("effort"))
    who = [harness, "·".join(part for part in (model, effort) if part)] if harness else None
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
        "rounds_note": f"R{int(rounds)} · 앞 {int(rounds) - 1}회는 이 기록 밖" if rounds and rounds > 1 else "",
        "unit": " · ".join(p for p in (_text(node.get("unit")), _text(node.get("model_profile"))) if p) or "·",
        "gate": _text(node.get("gate")),
        "out": " · ".join(str(p) for p in (node.get("write_scope") or ())) or "·", "says": "",
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
        stages = [_stage_record(node) for node in view_nodes]
        done = sum(1 for node in view_nodes if node.get("state") == "done")
        failed = any(node.get("state") == "failed" for node in view_nodes)
        active = next((n for n in view_nodes if n.get("state") in ("active", "reconciling", "recovering")), None)
        total = len(view_nodes)
        capability = _text(view.get("capability")) or _text(record.get("capability"))
        if capability is None:
            capability = next((_text(_get(job, "capability_owner", "assigned_contract")) for job in jobs
                               if _text(_get(job, "capability_owner", "assigned_contract"))), None)
        short = _short_capability(capability)
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
        shown_node = active or next((n for n in view_nodes if n.get("state") != "done"), None) \
            or (view_nodes[-1] if view_nodes else None)
        run_children = [r for r in resources if rid in (_text(_get(r, "route_id")), _route_stem(_get(r, "route")))
                        and _get(r, "liveness") == "working"]
        elapsed_values = [v for v in (_num(_get(job, "elapsed_min")) for job in jobs) if v is not None]
        age = _dur(max(elapsed_values)) if elapsed_values else None
        topology = _text(view.get("execution_topology")) or _text(record.get("execution_topology"))
        resource_node = shown_node is not None and shown_node.get("unit") == "_kernel/resource"
        shape = topology
        if topology == "staged" and resource_node:
            shape = "staged+resource"
        leg_status = "failed" if (failed and active is None) or state == "dead" else \
            "done" if state == "done" or (total and done == total) else "active"
        progress = f"{done}/{total}" if total else None
        worker_node, state_text = shown_node, None
        if state == "dead" and shown_node is not None and _stage_state(shown_node) == "todo":
            earlier = [n for n in view_nodes if _stage_state(n) in ("done", "failed")]
            if earlier:                            # the owner ended between stages: its last worker
                worker_node = earlier[-1]
                state_text = f"{_dur(worker_node.get('elapsed_min')) or ''} ⊸ · next " \
                             f"{shown_node.get('id')} · owner ended".strip()
        worker = _worker_segments(worker_node, run_children, state_text)
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
        title = _text(_get(result, "name")) or _chain_title(chain_nodes, rid) or \
            _text(view.get("slug")) or _text(record.get("slug")) or \
            (_text(_get(owner, "slug")) if owner is not None else None)
        if (owner is None or not jobs) and (session_row or {}).get("title"):
            title = title or session_row["title"]
        worker_stage = _text(worker_node.get("id")) if worker_node else None
        worker_unit = _text(worker_node.get("unit")) if worker_node else None
        # Navigation ancestry never authorizes metrics from the parent session.
        # Each attempt uses the same applied-policy/native gauge as a session.
        owner_gauge = _context_gauge(owner) if owner is not None else (None, None)
        worker_contexts = []
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
            worker_contexts.append({"stage": node_id, "harness": _get(job, "harness"),
                                    "model": _model_pair(_get(job, "resolved_model", "model")),
                                    "context": pct, "window": window})
        resume = None
        if state == "dead":
            resume = next((n.get("id") for n in view_nodes if _stage_state(n) == "failed"), None) or \
                next((n.get("id") for n in view_nodes if _stage_state(n) == "todo"), None)
        legs = _chain_legs(chain_nodes, rid, short, leg_status, progress) if chain_nodes else \
            ([[short or "·", leg_status, progress]] if short else [])
        mode = _text(view.get("capability_mode")) or _text(record.get("capability_mode"))
        current = bool(run_children or worker_contexts
                       or owner_live in ("working", "idle", "blocked")
                       and total and done != total and (active is not None or not failed))
        running = current and (active is not None or state == "working" or run_children or worker_contexts)
        route = {
            "id": rid, "repo": repo, "session_tag": owner_tag, "title": title, "state": state,
            "outcome": _text(_get(result, "result", "status")),
            "worker": worker, "age": age, "legs": legs,
            "stages": stages, "resume": resume, "activity": "running" if running else "ended",
            "loops": None, "worktree": worktree, "tree_label": short,
            "harness": owner_harness, "model": owner_model, "effort": owner_effort,
            "mode": mode,
            "intensity": _text(view.get("effective_intensity")) or _text(record.get("effective_intensity")),
            "profile": _text(_get(owner, "model_profile", "profile")) if owner is not None else None,
            "capability": short, "shape": shape, "past_from": None,
            "worker_stage": worker_stage or "·", "worker_unit": worker_unit,
            "worker_stage_key": None, "worker_step_key": None,
            "owner_context": None, "owner_gauge": list(owner_gauge),
            "worker_contexts": worker_contexts, "subworker": None,
            "current": current,
        }
        routes.append(route)
        nodes[rid] = _node_groups(view_nodes, route, rid, legs, now, done, leg_status)
    return routes, nodes


def _node_groups(view_nodes, route, rid, legs, now, done, leg_status):
    """One leg group holding the route's stages: the detail the route page prints per stage."""
    if not view_nodes:
        return []
    detail = _parallel_stage_details(view_nodes, now)
    short, mode = route.get("capability"), route.get("mode")
    name = f"leg {len(legs) or 1} · {short or '·'}{' ' + mode if mode else ''}"
    owner_line = [["owner  ", "dim"]]
    if route.get("harness"):
        owner_line.append([route["harness"], "hb_" + route["harness"]])
    model = route.get("model")
    if model:
        owner_line.append([f" {model[0]}·{route['effort']}" if route.get("effort") else f" {model[0]}", "soft"])
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
        else:
            finished = next((r for r in routes if r["repo"] == repo and r["state"] == "done" and r["title"]), None)
            done, age = (finished["title"] if finished else None), None
        repos.append({"id": repo, "name": repo, "done": done, "age": age})
    return repos


def _question_records(snapshot, session_rows, session_index, now):
    """Project only current, body-free records from the shared answer-wait cache."""
    waits = _get(snapshot, "answer_waits")
    if not isinstance(waits, Mapping):
        return [], {"state": "unknown", "total": None, "older": None}

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
        questions.append({
            "id": ident, "tag": tag, "h": session["harness"] if session else harness,
            "ref": ref,
            "older": older,
            "kind": _text(item.get("kind")) or source or "wait",
            "source": source, "wait": _since(item.get("since"), now),
            "project": session["repo"] if session else None,
            "session_tag": session["tag"] if session else None,
            "open_tag": session["tag"] if session else None,
            "pane": session["pane"] if session else None,
            "marker": "◑" if source == "S2" else "●",
        })
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
    summary = {"state": state, "total": total if state == "complete" else None,
               "older": older if state == "complete" else None}
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
    watches = []
    for link in links:
        if link[2].startswith("watch") and isinstance(link[0], list):
            count = re.match(r"(\d+) sessions", str(link[1]))
            watches.append({"repo": repo_of.get(link[0][0]),
                            "text": f"watch {int(count.group(1)) if count else 1}" +
                                    (" · ended" if str(link[4]).endswith("ended)") or link[4] == "ended" else "")})
    if watches:
        edges["watches"] = watches
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

    def endpoint(sid, harness):
        tag = tag_by_sid.get(sid)
        if tag:
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
        if targets:
            names, ended, stamps = [], True, []
            for target in targets:
                sid = _text(_get(target, "session_id"))
                names.append(_text(_get(target, "name")) or (sid[:8] if sid else "·"))
                if sid in tag_by_sid:
                    ended = False
                    linked.add(tag_by_sid[sid])
                stamp = _epoch_of(_get(target, "start_ts", "ts"))
                if stamp:
                    stamps.append(stamp)
            if len(names) > 1:
                note = " · ".join(names[:2]) + (" · …" if len(names) > 2 else "") + (" (ended)" if ended else "")
            else:
                note = "ended" if ended else ""
            when = datetime.datetime.fromtimestamp(min(stamps)).strftime("%m-%d") if stamps else None
            add([me, f"{len(names)} sessions" if len(names) > 1 else names[0], "watch ⚑", when, note], 0)
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
        key = (repr(link[0]), repr(link[1]))
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
    nodes = []
    for repo, tags in by_repo.items():
        columns, used = [], set()
        for tag in tags:
            if tag in used:
                continue
            pair = next((lane for lane in side_lanes if tag in lane), None)
            column = list(pair) if pair else [tag]
            used.update(column)
            columns.append(column)
        nodes.append([repo, columns])
    return nodes, {"links": unique, "cross": [cross] if cross else [], "side_lanes": side_lanes}


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


def _usage_records(snapshot, session_rows, now):
    best = {}
    for session in _records(_get(snapshot, "sessions", default=())):
        harness = _text(_get(session, "harness"))
        if harness not in HARNESSES:
            continue
        windows = _get(session, "rl_windows")
        r5, r7, rms = _get(session, "rl_5h"), _get(session, "rl_7d"), _get(session, "rl_ms")
        if windows or r5 is not None or r7 is not None or rms:
            mtime = _num(_get(session, "mtime")) or 0
            if harness not in best or mtime > best[harness][0]:
                resets = _get(session, "rl_rs") or (None, None)
                best[harness] = (mtime, r5, r7, rms, resets, windows)
    for harness, snap in (_get(snapshot, "usage_snapshots", default={}) or {}).items():
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
            usage.append({"text": harness, "key": "hb_" + harness, "windows": shown})
    return usage


def _parse_clock(ts):
    if not isinstance(ts, str):
        return None
    try:
        stamp = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp.strftime("%H:%M")


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


def _memory_records(snapshot, accepted_detail):
    detail = _get(accepted_detail, "memory")
    if detail is not None and not isinstance(detail, Mapping):
        return detail
    memory = _get(snapshot, "memory")
    if not isinstance(memory, Mapping):
        return None
    if memory.get("journal_available") is False:
        return None
    by_repo = memory.get("by_repo")
    if not isinstance(by_repo, Mapping):
        return None
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
            action = "saved" if action_name in _SAVED else "changed"
            snippet = _memory_text(event.get("snippet"))
            project_source = _text(event.get("project"))
            if project_source and "/" in project_source:
                project = _gpu_repo(project_source)
            else:
                project = project_source or _gpu_repo(_get(event, "cwd")) or _shown_repo(repo)
            grouped.setdefault(project, []).append((_epoch_of(event.get("ts")) or 0.0,
                [_parse_clock(event.get("ts")), action, _text(event.get("type")), snippet]))
    return [[repo, [row for _stamp, row in sorted(rows, key=lambda item: -item[0])]]
            for repo, rows in sorted(grouped.items()) if rows]


def _refers_to_route(route_id, references):
    if not route_id:
        return False
    for reference in references or ():
        text = str(reference)
        if text == route_id or re.search(r"(?<![A-Za-z0-9])" + re.escape(route_id) +
                                         r"(?![A-Za-z0-9])", text):
            return True
    return False


def _memory_backlog(snapshot, routes):
    memory = _get(snapshot, "memory")
    database = _get(memory, "database") if isinstance(memory, Mapping) else None
    if not isinstance(database, Mapping):
        return {"state": "unknown", "by_repo": {}, "limited": False}
    state = _text(database.get("state")) or "unknown"
    if state != "success":
        last_good = database.get("last_good_by_repo")
        return {"state": state,
                "by_repo": last_good if isinstance(last_good, Mapping) else {},
                "limited": bool(database.get("limited")),
                "stale": bool(database.get("last_good"))}
    source = database.get("by_repo")
    if not isinstance(source, Mapping):
        return {"state": "incomplete", "by_repo": {}, "limited": False}
    grouped = {}
    seen = set()
    for repo, records in source.items():
        for record in records or ():
            if not isinstance(record, Mapping):
                continue
            if str(record.get("status") or "active") == "superseded":
                continue
            tags = tuple(str(tag).casefold() for tag in (record.get("tags") or ()))
            rtype = str(record.get("type") or "").casefold()
            if "backlog" not in tags and rtype not in ("backlog", "task", "todo", "obligation"):
                continue
            canonical = _text(record.get("canonical_id")) or _text(record.get("id"))
            if canonical and canonical in seen:
                continue
            if canonical:
                seen.add(canonical)
            refs = tuple(record.get("links") or ()) + tuple(record.get("artifact_refs") or ())
            linked = [route for route in routes if _refers_to_route(str(route.get("id") or ""), refs)]
            current = next((route for route in linked
                            if route.get("state") in ("working", "blocked") or
                            any(stage.get("st") == "active" for stage in route.get("stages") or ())), None)
            settled = next((route for route in linked if route.get("state") == "done"
                            and route.get("outcome") == "success"), None)
            progress = "DOING" if current else "DONE" if settled else "TODO"
            item = {
                "id": _text(record.get("id")), "canonical_id": canonical,
                "title": _text(record.get("headline")) or _text(record.get("source")) or
                         _text(record.get("type")) or "backlog item",
                "progress": progress,
                "route_id": (current or settled or {}).get("id"),
                "updated": _text(record.get("updated")),
            }
            grouped.setdefault(str(repo), []).append(item)
    for items in grouped.values():
        items.sort(key=lambda item: (item["progress"] != "DOING", item["title"].casefold()))
    return {"state": "success", "by_repo": grouped, "stale": False,
            "limited": bool(database.get("limited"))}


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
    memory = _get(snapshot, "memory")
    if isinstance(memory, Mapping):
        today = memory.get("today") if isinstance(memory.get("today"), Mapping) else {}
        all_events = [(repo, event) for repo, events in (memory.get("by_repo") or {}).items()
                      for event in (events or ()) if isinstance(event, Mapping)]
        local_day = datetime.datetime.fromtimestamp(now).date() if _num(now) is not None else None
        added = sum(1 for _repo, event in all_events
                    if event.get("action") == "add" and local_day is not None
                    and (_epoch_of(event.get("ts")) is not None)
                    and datetime.datetime.fromtimestamp(_epoch_of(event.get("ts"))).date() == local_day)
        expired = sum(1 for _repo, event in all_events if event.get("action") == "lifecycle-expire")
        summary = {"added_today": _num(today.get("added")) if today.get("added") is not None else added,
                   "expired": _num(today.get("expired")) if today.get("expired") is not None else expired,
                   "projects": len(memory.get("by_repo") or {})}
        expired_at = max((_epoch_of(event.get("ts")) or 0.0 for _repo, event in all_events
                          if event.get("action") == "lifecycle-expire"), default=0.0)
        if expired_at:
            summary["expired_at"] = _clock(expired_at)
        system["memory_summary"] = summary
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


def _answered_records(snapshot):
    """Decision records the user answered, per repo, newest first: ``[repo, [[HH:MM, text], ...]]``."""
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
                    [_parse_clock(event.get("ts")), re.sub(r"^\[결정\]\s*", "", event["snippet"])]))
    return [[repo, [row for _epoch, row in sorted(rows, key=lambda item: -item[0])]]
            for repo, rows in sorted(grouped.items()) if rows]


# -- detail (the selection pump's reads) ----------------------------------------------------------

def _detail_parts(detail):
    """``(focus result, seats by harness:sid, config, panes)`` of whatever the feed handed over."""
    if detail is None:
        return None, {}, None, {}
    if hasattr(detail, "seats") and hasattr(detail, "focus"):
        return detail.focus, dict(detail.seats or {}), detail.config, dict(getattr(detail, "panes", {}) or {})
    return detail, {}, None, {}


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


def _seat_view(tag, repo, harness, sid, entries, now, now_text=None):
    """``(seat record, generation records, cards or None)`` of one session's seat history."""
    from .nav import generation_chain
    chain = generation_chain(entries, harness, sid)
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


def _detail_sections(rows, session_index, accepted_detail, now):
    """Seat, generation, card, dialogue, pane and raw-output sections from the pump's results."""
    focus, seats, _config, _panes = _detail_parts(accepted_detail)
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
        seat, gens, card_block = _seat_view(row["tag"], row["repo"], row["harness"],
                                            session_index[row["tag"]]["sid"], entries, now, row.get("now"))
        seat_rows.append(seat)
        generations[row["tag"]] = gens
        if card_block is not None:
            cards[row["tag"]] = card_block
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


def build_view_model(snapshot, accepted_detail=None, cached_config=None, now=None):
    """Project facts only; identity and liveness decisions stay with their source builders."""
    fixture_values = _get(snapshot, "values")
    if isinstance(fixture_values, Mapping):
        return ExplorerViewModel.from_mapping(fixture_values)
    if isinstance(snapshot, Mapping) and "schema" in snapshot and "sessions" in snapshot:
        return ExplorerViewModel.from_mapping(snapshot)
    clock = time.time() if now is None else now
    resolver = _resolver(snapshot)
    session_rows, session_index = _session_records(snapshot, clock, resolver)
    order = _get(snapshot, "session_order")
    if order:
        position = {str(sid): i for i, sid in enumerate(order)}
        sid_of = {row["tag"]: session_index[row["tag"]]["sid"] for row in session_rows}
        session_rows.sort(key=lambda row: position.get(str(sid_of.get(row["tag"])), len(position)))
    routes, nodes = _route_records(snapshot, session_rows, session_index, resolver, clock)
    repos = _repo_records(session_rows, routes, _repo_results(snapshot, resolver, clock), clock)
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
    questions, answer_wait_summary = _question_records(snapshot, session_rows, session_index, clock)
    peer_nodes, peer_edges = _peer_records(session_rows, session_index, clock, snapshot)
    hosts, compute = _host_records(snapshot, session_index)
    gpu_lines = _session_gpu_lines(snapshot, session_index)
    for row in session_rows:
        row["gpu"] = gpu_lines.get(row["tag"])
    usage = _usage_records(snapshot, session_rows, clock)
    by_repo = {}
    for row in session_rows:
        by_repo.setdefault(row["repo_id"], []).append(row)
    session_rows = [row for repo in repos for row in by_repo.get(repo["id"], ())]
    focus, _seats, detail_config, panes = _detail_parts(accepted_detail)
    for row in session_rows:                          # a pane id the herdr read found for this session
        if not row.get("pane"):
            info = session_index[row["tag"]]
            row["pane"] = next((panes[i] for i in info["ids"] if i in panes), None)
    seat_rows, generations, cards, dialogue, pane_read, raw = _detail_sections(
        session_rows, session_index, accepted_detail, clock)
    peer_nodes, peer_edges = _finish_peers(peer_nodes, peer_edges, session_rows, session_index, routes,
                                           _handover_links(snapshot, session_index, seat_rows, clock),
                                           _routeless_jobs(snapshot, session_index))
    by_tag = {row["tag"]: row for row in session_rows}
    for seat in seat_rows:
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
        by_tag[tag]["pane"] = by_tag[tag]["pane"] or _text(read.get("pane"))
        by_tag[tag]["pane_meta"] = "visible screen" + (f" · {_clock(read['at'])}" if _clock(read.get("at")) else "")
        by_tag[tag]["pane_lines"] = [[str(line), "soft"] for line in read.get("lines") or ()]
    for question in questions:
        if not question.get("pane"):
            question["pane"] = by_tag.get(question["tag"], {}).get("pane")
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
    answered = _answered_records(snapshot)
    system = _system_record(snapshot, session_rows, compute, accepted_detail, usage, clock)
    system["answer_waits"] = answer_wait_summary
    observations = _get(snapshot, "observations", default={}) or {}
    peer_observation = observations.get("optional:peers") if isinstance(observations, Mapping) else None
    system["peer_observation"] = {
        "state": _text(_get(peer_observation, "state")) or "unknown",
        "last_good": bool(_get(peer_observation, "last_good", default=False)),
    }
    system["memory_backlog"] = _memory_backlog(snapshot, routes)
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
        "memory": _memory_records(snapshot, accepted_detail),
        "models": detail_config if detail_config is not None else _get(cached_config, "models", "model_config"),
        "config": cached_config,
        "system": system,
        "pane_graph": _records(_get(snapshot, "pane_graph", "pane_edges", default=())),
        "source_records": explicit if isinstance(explicit, Mapping) else None,
    }
    return ExplorerViewModel.from_mapping(data)


def from_mapping(value):
    return ExplorerViewModel.from_mapping(value)
