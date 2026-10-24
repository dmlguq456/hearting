"""Compact view-local state and reducer for the Fleet explorer."""

from dataclasses import dataclass, replace
from collections.abc import Mapping


@dataclass(frozen=True)
class NavState:
    view: str = "OVERVIEW"
    scope: str = "-"
    opened: str = ""
    usage_open: bool = False
    scroll: tuple = ()
    title: str = "OVERVIEW"
    status: str = ""
    parents: tuple = ()
    folds: tuple = ()
    drag: tuple = ()

    def fold_map(self):
        return dict(self.folds)

    def opened_for(self, key, default=""):
        return self.fold_map().get(str(key), default)

    @classmethod
    def from_value(cls, value=None):
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        names = set(cls.__dataclass_fields__)
        values = {key: value[key] for key in names if key in value}
        for key in ("folds", "scroll", "drag"):
            values[key] = tuple(tuple(item) for item in values.get(key, ()))
        values["parents"] = tuple(values.get("parents", ()))
        return cls(**values)


VIEWS = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE", "MEMORY", "MODELS", "SYSTEM")


def reduce(state, event, frame=None):
    """Apply one normalized input or hit-map action and preserve its order."""
    before = NavState.from_value(state)
    after, effects = _reduce(before, event, frame)
    folds = after.fold_map()
    if (after.scope, after.view) != (before.scope, before.view):
        # A generation pick belongs to the page it was made on.
        folds = {key: value for key, value in folds.items() if not key.startswith("ui:gen:")}
    # The open OVERVIEW session is a view-local fold; it is not cleared by moving away.
    after = replace(after, opened=str(folds.get("sopen:OVERVIEW") or ""),
                    folds=tuple(sorted(folds.items())))
    return after, effects


def _reduce(state, event, frame=None):
    if isinstance(event, str):
        event = {"type": "key", "key": event}
    if not isinstance(event, Mapping):
        return state, ()
    kind, key = event.get("type", "key"), event.get("key")
    if kind in ("click", "action"):
        action = tuple(event.get("action", ()))
        if not action:
            return state, ()
        if action[0] in ("view", "tab") and len(action) > 1:
            view = str(action[1])
            return replace(state, view=view, scope="-", opened="", title=view), ()
        if action[0] == "fold" and len(action) > 2:
            slot, identity = str(action[1]), str(action[2])
            folds = state.fold_map()
            folds[slot] = "" if folds.get(slot, "") == identity else identity
            opened = folds[slot] if slot == "sopen:OVERVIEW" else state.opened
            return replace(state, opened=opened, folds=tuple(sorted(folds.items()))), ()
        if action[0] == "ui" and len(action) > 2:
            folds = state.fold_map()
            if action[1] == "sel":
                folds = {key: value for key, value in folds.items()
                         if key not in ("ui:sel", "ui:model", "ui:effort")}
            folds["ui:" + str(action[1])] = str(action[2])
            return replace(state, folds=tuple(sorted(folds.items()))), ()
        if action[0] == "usage":
            return replace(state, usage_open=not state.usage_open), ()
        if action[0] in ("page", "scope") and len(action) > 1:
            scope = str(action[1]) if action[0] == "scope" else f"{action[1]}:{action[2]}"
            if action[0] == "scope" and scope != "-" and ":" not in scope:
                scope = "session:" + scope
            parents = state.parents + (state.scope,)
            return replace(state, scope=scope, opened="", title=scope.split(":", 1)[-1],
                           parents=parents), ()
        if action[0] == "route" and len(action) > 1:
            offsets = dict(state.scroll)
            offsets["rawoff"] = 0
            return replace(state, scope=f"route:{action[1]}", opened="",
                           title=str(action[1]), parents=state.parents + (state.scope,),
                           scroll=tuple(sorted(offsets.items()))), ()
        if action[0] == "pane" and len(action) > 2:
            return state, (("focus", action[1], action[2]),)
        if action[0] == "root" and len(action) > 1:
            view = str(action[1])
            folds = state.fold_map()
            if len(action) > 2 and isinstance(action[2], Mapping):
                opened = str(action[2].get("ropen:" + view, ""))
                if opened:
                    folds["ropen:" + view] = opened
            return replace(state, view=view, scope="-", opened="",
                           title=view, folds=tuple(sorted(folds.items()))), ()
        return state, ()
    if kind != "key":
        if kind == "wheel":
            x, y = int(event.get("x", -1)), int(event.get("y", -1))
            hit = next((item for item in getattr(frame, "hits", ())
                        if item.y == y and item.x0 <= x < item.x1 and item.action[:1] == ("scroll",)), None)
            if hit is None:
                return state, ()
            _name, slot, maximum, *sign = hit.action
            direction = int(event.get("direction", 0))
            amount = 3 * direction * (int(sign[0]) if sign else 1)
            offsets = dict(state.scroll)
            offsets[slot] = max(0, min(int(maximum), int(offsets.get(slot, 0)) + amount))
            return replace(state, scroll=tuple(sorted(offsets.items()))), ()
        if kind == "mouse":
            phase = event.get("phase")
            x, y = int(event.get("x", -1)), int(event.get("y", -1))
            button = int(event.get("button", 0))
            offsets = dict(state.scroll)
            if phase == "release":
                if state.drag and state.drag[0] == "tree":
                    drag = state.drag
                    if x >= 32 or y < 0 or y >= int(getattr(frame, "rows", 0)):
                        return replace(state, drag=()), ()
                    if drag[7] and drag[8]:
                        ordering = event.get("ordering") or {}
                        source = (drag[2] if drag[1] == "repo" else
                                  getattr(ordering.get("tag_keys", {}), "get", lambda _key: None)(drag[2]))
                        target = drag[6]
                        if drag[1] == "session":
                            target = (getattr(ordering.get("tag_keys", {}), "get", lambda _key: None)(target)
                                      if target != "end" else None)
                        repo = (drag[2] if drag[1] == "repo" else
                                getattr(ordering.get("repo_by_tag", {}), "get", lambda _key: None)(drag[2]))
                        if source and repo is not None:
                            return replace(state, drag=()), (("reorder", drag[1], repo, source, target),)
                    if not drag[8]:
                        return reduce(replace(state, drag=()),
                                      {"type": "click", "action": ("scope", drag[3])}, frame)
                    return replace(state, drag=()), ()
                return replace(state, drag=()), ()
            if event.get("motion") and state.drag:
                if state.drag[0] == "tree":
                    drag = state.drag
                    moved = drag[8] or abs(y - drag[5]) >= 1 or abs(x - drag[4]) >= 2
                    if not moved:
                        return state, ()
                    hit = next((item for item in getattr(frame, "hits", ())
                                if item.y == y and item.x0 <= x < item.x1), None)
                    action = hit.action if hit is not None else ()
                    target_kind, target = None, None
                    valid = False
                    if action[:1] == ("scope",) and len(action) > 1:
                        value = str(action[1])
                        if value.startswith("repo:"):
                            target_kind, target = "repo", value[5:]
                        elif ":" not in value:
                            target_kind, target = "session", value
                    elif action[:1] == ("order-end",):
                        target, valid = "end", True
                    elif (hit is None and x < 32 and
                          y > max((item.y for item in getattr(frame, "hits", ())
                                   if item.action[:1] == ("scope",) and len(item.action) > 1 and
                                   (str(item.action[1]).startswith("repo:") or
                                    ":" not in str(item.action[1]))), default=-1)):
                        # Empty rail cells below the last tree row are the end
                        # insertion point; they deliberately have no hit box.
                        target, valid = "end", True
                    if target_kind == drag[1]:
                        if target_kind == "repo":
                            valid = target != drag[2]
                        else:
                            ordering = event.get("ordering") or {}
                            repo_by_tag = ordering.get("repo_by_tag", {})
                            source_repo = repo_by_tag.get(drag[2])
                            valid = target != drag[2] and repo_by_tag.get(target) == source_repo
                    return replace(state, drag=("tree", drag[1], drag[2], drag[3], drag[4],
                                                 drag[5], target if valid else None, valid, moved)), ()
                slot, maximum, origin_y, origin_offset, sign = state.drag
                offsets[slot] = max(0, min(int(maximum), int(origin_offset) +
                                           (y - int(origin_y)) * int(sign)))
                return replace(state, scroll=tuple(sorted(offsets.items()))), ()
            if phase != "press" or button != 0:
                return state, ()
            hit = next((item for item in getattr(frame, "hits", ())
                        if item.y == y and item.x0 <= x < item.x1), None)
            if hit is None:
                return state, ()
            if hit.action[:1] == ("scope",) and len(hit.action) > 1:
                scope = str(hit.action[1])
                if scope.startswith("repo:"):
                    return replace(state, drag=("tree", "repo", scope[5:], scope, x, y,
                                                None, False, False)), ()
                if ":" not in scope:
                    ordering = event.get("ordering") or {}
                    repo = ordering.get("repo_by_tag", {}).get(scope)
                    if repo is not None:
                        return replace(state, drag=("tree", "session", scope, scope, x, y,
                                                    None, False, False)), ()
            if hit.action[:1] == ("scroll",):
                _name, slot, maximum, *sign = hit.action
                return replace(state, drag=(slot, int(maximum), y,
                                            min(int(offsets.get(slot, 0)), int(maximum)),
                                            int(sign[0]) if sign else 1)), ()
            return reduce(state, {"type": "click", "action": hit.action}, frame)
        return state, ()
    if key in ("tab", "shift-tab", "left", "right"):
        if state.scope != "-":
            return replace(state, scope="-", opened=""), ()
        step = -1 if key in ("shift-tab", "left") else 1
        tabs = tuple(str(item) for item in event.get("tabs", VIEWS))
        if not tabs:
            return state, ()
        index = tabs.index(state.view) if state.view in tabs else 0
        view = tabs[(index + step) % len(tabs)]
        return replace(state, view=view, scope="-", opened="", title=view), ()
    if key in ("escape", "esc"):
        if state.drag:
            return replace(state, drag=()), ()
        if state.scope != "-":
            parent = event.get("parent")
            if parent is None:
                parent_hit = next((hit for hit in getattr(frame, "hits", ())
                                   if hit.y == -1 and hit.action[:1] == ("parent",)), None)
                parent = parent_hit.action[1] if parent_hit else (
                    state.parents[-1] if state.parents else "-")
            if parent not in (None, "-") and ":" not in str(parent):
                parent = "session:" + str(parent)
            parents = state.parents[:-1] if state.parents else ()
            return replace(state, scope=str(parent), opened="", parents=parents,
                           title=(str(parent).split(":", 1)[-1] if parent != "-" else state.view)), ()
        return state, ()
    if key in ("backspace", "delete"):
        return reduce(state, {"type": "key", "key": "escape"}, frame)
    if key in ("up", "down"):
        order = tuple(str(item) for item in event.get("order", ()))
        if not order:
            return state, ()
        step = -1 if key == "up" else 1
        index = order.index(state.scope) + step if state.scope in order else (0 if step > 0 else -1)
        scope = order[index % len(order)]
        return replace(state, scope=scope, opened="", title=scope.partition(":")[2] or scope), ()
    if key in ("u", "usage"):
        return replace(state, usage_open=not state.usage_open), ()
    if key in ("/", "F"):
        return state, (("command", "filter" if key == "/" else "feed"),)
    if key in ("enter", "\n"):
        hit = event.get("hit")
        action = hit.action if hasattr(hit, "action") else hit
        if state.scope == "-" and not action:
            return state, ()
        if state.scope != "-":
            tag = state.scope.partition(":")[2]
            pane_hit = next((item for item in getattr(frame, "hits", ())
                             if item.action[:1] == ("pane",) and
                             len(item.action) > 2 and str(item.action[1]) == tag), None)
            pane = pane_hit.action[2] if pane_hit else ""
            return state, (("focus", tag, pane),)
        return reduce(state, {"type": "click", "action": action}, frame)
    return state, ()


def serialize(state):
    """Persist only explorer view state; old B cursors are ignored on read."""
    state = NavState.from_value(state)
    return {"schema_version": 3, "view": state.view, "scope": state.scope,
            "opened": state.opened, "usage_open": state.usage_open,
            "scroll": [list(item) for item in state.scroll], "title": state.title,
            "status": state.status, "parents": list(state.parents),
            "folds": [list(item) for item in state.folds],
            "frame_layout": {"schema_version": 3}}


def restore(value=None):
    return NavState.from_value(value)
