"""Stable reference-state inventory for parity captures."""
from __future__ import annotations

import hashlib
import json

from tools.fleet.explorer.state import NavState


def enumerate_states(reference=None, vm=None):
    """Enumerate stable view/page/selection states from the current source identities.

    ``reference`` may be a frozen manifest or a mapping with a ``states`` list. Stable ids
    include the semantic selection key; ordering is deterministic and independent of hash order.
    """
    values = vm.to_mapping() if hasattr(vm, "to_mapping") else (vm or {})
    rows = []
    def add(state_id, state):
        rows.append((state_id, state))

    views = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE",
             "MEMORY", "MODELS", "SYSTEM")
    for view in views:
        add(view, NavState(view=view, title=view))
    if reference is not None and hasattr(reference, "PROJECTS"):
        repos = [(str(row[0]), [session.get("tag") for session in row[3]])
                 for row in reference.PROJECTS]
        for name, tags in repos:
            add("repo:" + name, NavState(scope="repo:" + name, title=name))
            for tag in tags:
                if tag:
                    add("session:" + str(tag), NavState(scope="session:" + str(tag), title=str(tag)))
        for route in getattr(reference, "ROUTE_ROWS", ()):
            pick = route.get("pick") if isinstance(route, dict) else None
            if pick:
                add("route:" + str(pick), NavState(scope="route:" + str(pick), title=str(pick)))
                add("ROUTES-open:" + str(pick), NavState(view="ROUTES", title="ROUTES",
                    folds=(("ropen:ROUTES", str(pick)),)))
    else:
        for repo in values.get("repos") or ():
            rid = repo.get("id") or repo.get("name")
            if rid:
                add("repo:" + str(rid), NavState(scope="repo:" + str(rid), title=str(rid)))
        for session in values.get("sessions") or ():
            tag = session.get("tag")
            if tag:
                add("session:" + str(tag), NavState(scope="session:" + str(tag), title=str(tag)))
        for route in values.get("routes") or ():
            rid = route.get("id")
            if rid:
                add("route:" + str(rid), NavState(scope="route:" + str(rid), title=str(rid)))
                add("ROUTES-open:" + str(rid), NavState(view="ROUTES", title="ROUTES",
                    folds=(("ropen:ROUTES", str(rid)),)))
    # The committed reference README defines additional interaction states that
    # are not tied to whichever entities happen to be in today's source.
    add("usage-open", NavState(view="OVERVIEW", title="OVERVIEW", usage_open=True))
    for view, key, identity in (("QUESTIONS", "qshut:QUESTIONS", "bb"),
                                ("HISTORY", "hopen:HISTORY", "0c"),
                                ("ROUTES", "ropen:ROUTES", "f5"),
                                ("OVERVIEW", "sopen:OVERVIEW", "f5")):
        add(f"{view}-fold:{identity}", NavState(view=view, title=view,
            opened=identity if key == "sopen:OVERVIEW" else "",
            folds=((key, identity),)))
    for view in views:
        add(f"{view}-scroll:1", NavState(view=view, title=view, scroll=((view, "1"),)))
    add("HISTORY-generation:0", NavState(view="HISTORY", title="HISTORY",
         folds=(("hopen:HISTORY", "0c"), ("ui:gen:f5", "0"))))
    questions = values.get("questions") or ()
    for question in questions:
        tag = question.get("tag") if isinstance(question, dict) else None
        if tag:
            add("QUESTIONS-fold:" + str(tag), NavState(view="QUESTIONS", title="QUESTIONS",
                folds=(("qshut:QUESTIONS", str(tag)),)))
    for seat in values.get("seats") or ():
        key = seat[0] if isinstance(seat, (list, tuple)) and seat else None
        if key:
            add("HISTORY-open:" + str(key), NavState(view="HISTORY", title="HISTORY",
                folds=(("hopen:HISTORY", str(key)),)))
    peers = values.get("peer_nodes") or ()
    for _repo, columns in peers:
        for column in columns:
            for tag in column:
                add("PEERS-pick:" + str(tag), NavState(view="PEERS", title="PEERS",
                    folds=(("pfocus:PEERS", str(tag)),)))
    # A supplied manifest contributes state labels as coverage hints, not authority to remove
    # states derived from current live entities.
    manifest_states = reference.get("states", ()) if isinstance(reference, dict) else ()
    known = {key for key, _ in rows}
    for label in manifest_states:
        if label not in known:
            add("manifest:" + str(label), NavState(view="OVERVIEW", title=str(label)))
    unique = {key: state for key, state in rows}
    result = [(key, unique[key]) for key in sorted(unique)]
    fingerprint = hashlib.sha256(json.dumps([key for key, _ in result], separators=(",", ":")).encode()).hexdigest()
    return [{"id": key, "state": state, "fingerprint": fingerprint} for key, state in result]


# --- Reference-driven reachable states -------------------------------------------------
#
# A reachable state is what the approved viewer holds after a sequence of clicks: a scope,
# a view, the usage block, and the UI map its hit actions write. The walk below follows the
# reference's own hit actions (the same transitions view.py applies), so the inventory is
# never a hand-written list.

FOLD_KEYS = ("sopen", "ropen", "qshut", "hopen", "aopen", "pfocus")
SKIPPED_ACTIONS = ("scroll", "parent", "pane")


def base_state():
    return {"scope": None, "view": "OVERVIEW", "uopen": False, "ui": {}, "kinds": {}, "path": ()}


def state_key(state):
    return json.dumps([state["scope"], state["view"], bool(state["uopen"]),
                       sorted(state["ui"].items(), key=lambda item: item[0])],
                      sort_keys=True, default=str, separators=(",", ":"))


def key_kind(key):
    return "fold" if str(key).split(":", 1)[0] in FOLD_KEYS else "ui"


def apply_action(state, action):
    """Apply one reference hit action as view.py does; None when it changes no state."""
    action = json.loads(json.dumps(action))
    nxt = {"scope": state["scope"], "view": state["view"], "uopen": state["uopen"],
           "ui": dict(state["ui"]), "kinds": dict(state["kinds"]),
           "path": tuple(state["path"]) + (_label(action),)}
    kind, ui, kinds = action[0], nxt["ui"], nxt["kinds"]
    if kind == "scope":
        nxt["scope"] = None if action[1] == "-" else action[1]
    elif kind == "root":
        nxt["scope"], nxt["view"] = None, action[1]
        for key, value in (action[2] if len(action) > 2 else {}).items():
            ui[key], kinds[key] = value, "fold"
    elif kind == "tab":
        nxt["view"], nxt["scope"] = action[1], None
    elif kind == "route":
        nxt["scope"] = "route:" + action[1]
        ui["rawoff"], kinds["rawoff"] = 0, "scroll"
    elif kind == "ui":
        if action[1] == "apply":
            return None
        if action[1] == "sel":
            for key in ("sel", "model", "effort"):
                ui.pop(key, None)
                kinds.pop(key, None)
        ui[action[1]], kinds[action[1]] = action[2], "ui"
    elif kind == "fold":
        if ui.get(action[1]) == action[2]:
            ui.pop(action[1], None)
            kinds.pop(action[1], None)
        else:
            ui[action[1]], kinds[action[1]] = action[2], "fold"
    elif kind == "usage":
        nxt["uopen"] = not nxt["uopen"]
    else:
        return None
    return nxt


def with_scroll(state, key, offset):
    nxt = {"scope": state["scope"], "view": state["view"], "uopen": state["uopen"],
           "ui": dict(state["ui"]), "kinds": dict(state["kinds"]),
           "path": tuple(state["path"]) + (f"scroll:{key}={offset}",)}
    nxt["ui"][key], nxt["kinds"][key] = offset, "scroll"
    return nxt


def _label(action):
    return ":".join(str(part) if not isinstance(part, dict) else
                    ",".join(f"{k}={v}" for k, v in sorted(part.items())) for part in action)


def state_id(state):
    return "/".join(state["path"]) or "OVERVIEW"


def reference_arguments(state):
    """The freezer capture arguments for a state."""
    return {"view": state["view"], "scope": state["scope"], "uopen": state["uopen"],
            "ui": dict(state["ui"])}


def nav_state(state):
    """The same state expressed as Fleet's NavState."""
    scope = state["scope"]
    if scope and ":" not in scope:
        scope = "session:" + scope
    folds, scroll = {}, {}
    for key, value in state["ui"].items():
        kind = state["kinds"].get(key) or key_kind(key)
        if kind == "scroll":
            scroll[key] = int(value)
        elif kind == "fold":
            folds[key] = str(value)
        else:
            folds["ui:" + key] = str(value)
    return NavState(view=state["view"], scope=scope or "-", opened=folds.get("sopen:OVERVIEW", ""),
                    usage_open=bool(state["uopen"]), title=state["view"],
                    scroll=tuple(sorted(scroll.items())), folds=tuple(sorted(folds.items())))


def readme_states(cases):
    """Reference README states as walk states: ``cases`` maps name -> (scope, view, ui, uopen)."""
    out = []
    for name, (scope, view, ui, uopen) in cases.items():
        state = base_state()
        state.update(scope=None if scope == "-" else scope, view=view, uopen=bool(uopen),
                     ui=dict(ui), kinds={key: ("ui" if str(key).startswith("gen:") else key_kind(key))
                                         for key in ui},
                     path=("readme:" + name,))
        out.append(state)
    return out


def action_depth(state):
    """How many clicks reach the state; a README seed has no path, so it is counted from its parts."""
    path = state["path"]
    if path and path[0].startswith("readme:"):
        return (state["view"] != "OVERVIEW") + (state["scope"] is not None) + len(state["ui"]) + \
            bool(state["uopen"])
    return len(path)


def walk_next(state, actions):
    """Children of a state: every reference hit action, plus the first and last scroll offsets.

    A third click is limited to in-view interactions (fold / ui) on a view reached by one
    navigation and one such interaction; wider chains only repeat independent per-view keys.
    """
    depth = action_depth(state)
    if depth >= 3 or (state["path"] and state["path"][-1].startswith("scroll:")):
        return []
    children = []
    for action in actions:
        if action[0] == "scroll":
            if len(action) >= 3:
                key, maximum = str(action[1]), max(0, int(action[2]))
                children.extend(with_scroll(state, key, offset)
                                for offset in sorted({1, maximum}) if offset > 0)
            continue
        if action[0] in SKIPPED_ACTIONS:
            continue
        if depth == 2:
            kinds = [part.split(":", 1)[0] for part in state["path"]]
            seeded = kinds and kinds[0] == "readme"
            if not (action[0] in ("fold", "ui") and (seeded or (
                    len(kinds) == 2 and kinds[0] in ("root", "tab") and kinds[1] in ("fold", "ui")))):
                continue
        child = apply_action(state, action)
        if child is not None:
            children.append(child)
    return children
