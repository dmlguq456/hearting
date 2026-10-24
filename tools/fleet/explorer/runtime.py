"""Runtime seam between collected Fleet data and the pure explorer renderer.

The initial implementation deliberately preserves the existing view model and
input behavior. Collection, ordering, and persistence policy can be supplied by
the live adapter without moving I/O into the drawing code.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import time


class ExplorerRuntime:
    """Behaviour-preserving runtime hooks for the explorer."""

    def __init__(self):
        self._identity_generation = object()
        self._identity_detail = None
        self._identity_tags = None
        self._identity_by_tag = None
        self._order_path = None
        self._order_signature = object()
        self._order_value = None

    @staticmethod
    def _file_signature(path):
        try:
            info = path.stat()
        except OSError:
            return None
        return (info.st_dev, info.st_ino, info.st_size,
                getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9)),
                getattr(info, "st_ctime_ns", int(info.st_ctime * 1e9)))

    def _load_order(self, order):
        path = order.order_path()
        signature = self._file_signature(path)
        if path != self._order_path or signature != self._order_signature:
            self._order_value = order.load_order(path)
            self._order_path = path
            self._order_signature = signature
        return self._order_value

    def prepare(self, snapshot, detail, *, vm, generation, now):
        """Return the already-built model and collection readiness for this generation."""
        model = vm
        try:
            from . import live, order
            tags = tuple(str(row.get("tag", "")) for row in model.sessions)
            cached = (generation == self._identity_generation and detail is self._identity_detail
                      and tags == self._identity_tags)
            if not cached:
                resolver = live._resolver(snapshot)
                _rows, by_tag = live._session_records(snapshot, now or time.time(), resolver)
                source_rows = tuple(live._records(live._get(snapshot, "sessions", default=())))
                for source in source_rows:
                    explicit = live._text(live._get(source, "session_tag", "tag"))
                    sid = live._text(live._get(source, "session_id", "sid", "id"))
                    if explicit:
                        by_tag.setdefault(explicit, {"session": source, "sid": sid})
                    if sid:
                        by_tag.setdefault(sid, {"session": source, "sid": sid})
                identity_by_tag = {}
                for row in model.sessions:
                    tag = str(row.get("tag", ""))
                    identity = by_tag.get(tag, {})
                    source = identity.get("session")
                    sid = identity.get("sid")
                    harness = str(row.get("harness") or "")
                    if sid:
                        stable = "sid:" + harness + ":" + str(sid)
                    elif source is not None:
                        stable = "process:" + ":".join(str(live._get(source, key, default=""))
                                                           for key in ("harness", "pid", "proc_start", "managed_dir"))
                    else:
                        # Unknown identity remains ephemeral for this generation and is never
                        # persisted as a display-tag key.
                        stable = "unidentified:" + harness + ":" + tag
                    identity_by_tag[tag] = stable
                self._identity_generation = generation
                self._identity_detail = detail
                self._identity_tags = tags
                self._identity_by_tag = identity_by_tag
            sessions = []
            tag_keys, repo_by_tag, tags_by_key = {}, {}, {}
            for row in model.sessions:
                tag = str(row.get("tag", ""))
                stable = self._identity_by_tag[tag]
                session = dict(row)
                session["order_key"] = stable
                sessions.append(session)
                tag_keys[tag] = stable
                repo_by_tag[tag] = str(row.get("repo_id", row.get("repo", "")))
                tags_by_key[stable] = tag
            saved = self._load_order(order)
            reconciled = order.reconcile_order(saved, model.repos, sessions)
            ordered_sessions = reconciled["sessions"]
            ordered_tags = [str(row.get("tag", "")) for row in ordered_sessions]
            ordering = {
                "saved": {"repos": list(reconciled["repo_order"]),
                          "sessions": reconciled["session_order"]},
                "repo_keys": list(reconciled["repo_order"]),
                "session_keys": {key: list(value) for key, value in reconciled["session_order"].items()},
                "tag_keys": tag_keys, "repo_by_tag": repo_by_tag,
                "tags_by_key": tags_by_key,
                "tag_cycle": ["session:" + tag for tag in ordered_tags],
            }
            model = replace(model, repos=tuple(reconciled["repos"]),
                            sessions=tuple(ordered_sessions), ordering=ordering)
        except (AttributeError, TypeError, ValueError):
            # Preserve a valid frame if a third-party snapshot violates an optional
            # ordering identity shape; malformed order files are handled by load_order.
            pass
        readiness = getattr(model, "readiness", None)
        if readiness is None:
            observations = getattr(snapshot, "observations", None)
            if observations is None and isinstance(snapshot, Mapping):
                observations = snapshot.get("observations")
            observations = observations if isinstance(observations, Mapping) else {}
            source_states = {}
            for name, item in observations.items():
                state = item.get("state") if isinstance(item, Mapping) else getattr(item, "state", None)
                reason = item.get("reason") if isinstance(item, Mapping) else getattr(item, "reason", None)
                error = item.get("error") if isinstance(item, Mapping) else getattr(item, "error", None)
                source_states[str(name)] = {"state": state or "unknown", "reason": reason, "error": error}
            pending = tuple(sorted(name for name, item in source_states.items()
                                   if item["state"] == "pending"))
            # A missing/disabled source is complete unknown; only an explicit
            # pending observation delays the first complete frame.
            readiness = {"ready": not pending, "generation": generation,
                         "pending": pending, "sources": source_states,
                         "observed_at": now if now is not None else time.time()}
        return model, readiness

    def apply_effects(self, effects, vm):
        """Apply persisted ordering effects after the pure reducer accepts a drop."""
        from . import order
        ordering = getattr(vm, "ordering", None)
        if not isinstance(ordering, Mapping):
            return None
        saved = {"repos": list(ordering.get("repo_keys", ())),
                 "sessions": {str(key): list(value) for key, value in
                              ordering.get("session_keys", {}).items()}}
        for effect in effects or ():
            if not effect or effect[0] != "reorder" or len(effect) < 5:
                continue
            _name, kind, repo, source, target = effect[:5]
            if kind == "repo":
                saved = order.move_repo(saved, source, target)
            elif kind == "session":
                saved = order.move_session(saved, repo, source, target)
            order.save_order(saved)
            # A drag writes the authoritative order. Reload on the next compose so
            # the cached view follows the same disk-backed path as external changes.
            self._order_signature = object()
        return None

    def loading_frame(self, cols, rows):
        """Return a small terminal-safe loading frame while a source is pending."""
        from .frame import CellFrame
        from .grid import CellGrid
        grid = CellGrid(cols, rows)
        if rows > 0:
            label = "불러오는 중"
            grid.put(max(0, (cols - len(label)) // 2), max(0, rows // 2), label, "dim")
        return CellFrame(grid.snapshot(), tuple(), int(cols), int(rows), "OVERVIEW", "loading")

    def collection_options(self, first_complete):
        """Collect full route/detail facts before publishing the first complete frame."""
        return {"fast_first": not bool(first_complete)}


def tag_cycle(ordering):
    """Read a supplied ordering cycle without imposing an ordering policy."""
    if isinstance(ordering, Mapping):
        value = ordering.get("tag_cycle", ())
    else:
        value = getattr(ordering, "tag_cycle", ())
    if callable(value):
        value = value()
    return tuple(value or ())
