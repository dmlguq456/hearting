"""Narrow native display reads between the existing complete Fleet collections.

Only a background reader touches files. Its result can replace display cells in
an already admitted roster; it never changes census, readiness, action bindings,
detail generations or saved complete observations.
"""
from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import fields as dataclass_fields
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

from . import native_display, session_registry
from .collectors import claude, codex, opencode
from .collectors.procscan import read_proc_start


_LIMIT = 512
_CELLS = ("model", "effort", "context", "window", "context_detail")


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _identity(session):
    return tuple(str(_get(session, key) or "") for key in
                 ("harness", "session_id", "pid", "proc_start"))


def _signature(path, limit=None):
    if not path:
        return None
    try:
        info = Path(path).lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or limit is not None and info.st_size > limit):
            return None
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except OSError:
        return None


def _attachment_index():
    utilities = str(Path(__file__).resolve().parents[2] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    import session_shell
    result = {}
    for row in session_shell.bindings():
        key = (row["harness"], row["sid"], str(row["native_pid"]), row["native_start"])
        # Conflicting attachments are unknown, just as in exact_binding().
        result[key] = None if key in result else row
    return result


def _feed(binding, identity):
    if not binding:
        return None
    directory = Path(binding["socket"]).parent
    try:
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            return None
        path = directory / "native-display.json"
        before = _signature(path, 4096)
        if before is None:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if stamp != before or info.st_mode & 0o022:
                return None
            value = json.loads(os.read(fd, 4097))
            if _signature(path, 4096) != before:
                return None
        finally:
            os.close(fd)
        if (not isinstance(value, dict) or value.get("schema") != "hearting-native-display-v1"
                or (value.get("harness"), value.get("sid"), str(value.get("pid")), value.get("start")) != identity
                or value.get("host_pid") != binding["host_pid"]
                or value.get("host_start") != binding["host_start"]
                or value.get("incarnation") != binding["incarnation"]
                or read_proc_start(binding["host_pid"]) != binding["host_start"]):
            return None
        fields = value.get("values")
        if not isinstance(fields, dict):
            return None
        result = {key: fields[key] for key in ("model", "effort")
                  if key in fields and (fields[key] is None or isinstance(fields[key], str)
                                        and len(fields[key]) <= 128)}
        percent = fields.get("context_percent")
        if "context_percent" in fields and (percent is None or native_display.percentage(percent) is not None):
            result["context_percent"] = percent
        return result
    except (OSError, ValueError, TypeError):
        return None


class NativeDisplayReader:
    """Retain only current roster entries and unchanged source generations."""

    def __init__(self):
        self._snapshot = None
        self._attachments = {}
        self._entries = {}
        self._latest = ()
        self._event_sources = (), (), ()

    def invalidate(self):
        # An exact selection/attachment change may arrive before the next
        # complete census. Re-read that binding; never infer a new roster.
        self._snapshot = None

    def event_sources(self):
        return self._event_sources

    def read(self, snapshot):
        if snapshot is not self._snapshot:
            self._snapshot = snapshot
            try:
                self._attachments = _attachment_index()
            except (OSError, ValueError, TypeError):
                self._attachments = {}
        entries, result = {}, []
        event_paths, event_patterns, event_processes = set(), set(), set()
        for session in tuple(_get(snapshot, "sessions", ()) or ())[:_LIMIT]:
            identity = _identity(session)
            harness, sid, pid, start = identity
            if harness not in {"claude", "codex", "opencode"} or not sid or not pid or not start:
                continue
            binding = self._attachments.get(identity)
            source = _get(session, "_refresh_source") or {}
            source = source if isinstance(source, Mapping) else {}
            native_path = (_get(session, "_transcript_path") or source.get("path")) if harness == "codex" else None
            tap = Path(claude._home()) / ".statusline" / (sid + ".json") if harness == "claude" else None
            db = source.get("db_path") or opencode._db() if harness == "opencode" else None
            feed_path = Path(binding["socket"]).parent / "native-display.json" if binding else None
            try:
                native_live = read_proc_start(int(pid)) == start
                registry = session_registry.registry_path(harness, int(pid))
            except (TypeError, ValueError):
                continue
            event_paths.update(str(path) for path in (native_path, tap, db,
                str(db) + '-wal' if db else None, feed_path, registry) if path)
            event_patterns.add((str(Path(session_registry.state_root()) / harness), 'session-shell-'))
            event_processes.add((int(pid), start))
            if binding:
                event_processes.add((binding['host_pid'], binding['host_start']))
            host_live = not binding or read_proc_start(binding["host_pid"]) == binding["host_start"]
            signature = tuple(_signature(path) for path in
                              (native_path, tap, db, str(db) + "-wal" if db else None, feed_path, registry)) + (
                                  native_live, host_live,
                                  tuple(binding.get(key) for key in ('host_pid', 'host_start', 'incarnation', 'socket'))
                                  if binding else None)
            # Policy/value changes in the complete base invalidate a conversion,
            # without making that complete base a new display authority.
            base = repr(tuple(_get(session, key) for key in
                             ("model", "effort", "effort_default", "ctx_pct", "active_context_tokens",
                              "context_window_tokens", "context_display_window_tokens", "context_policy", "compaction_pressure", "liveness")))
            previous = self._entries.get(identity)
            if previous is not None and previous[:2] == (signature, base):
                entry = previous
            else:
                try:
                    fields = (self._read(session, identity, binding if host_live else None, tap, native_path, db)
                              if native_live else dict.fromkeys(_CELLS))
                except (OSError, ValueError, TypeError):
                    fields = None
                entry = signature, base, fields
            entries[identity] = entry
            if entry[2] is not None:
                result.append((identity, entry[2]))
        self._entries = entries
        self._event_sources = (tuple(sorted(event_paths)), tuple(sorted(event_patterns)),
                               tuple(sorted(event_processes)))
        value = tuple(result)
        if value != self._latest:
            self._latest = value
        return self._latest

    @staticmethod
    def _read(session, identity, binding, tap, native_path, db):
        from .explorer.live import _context_gauge, _session_model_effort
        harness, sid, pid, start = identity
        if read_proc_start(int(pid)) != start:
            return dict.fromkeys(_CELLS)
        current = session_registry.read(harness, int(pid))
        if (current and str(current.get("procStart")) == start
                and current.get("sessionId") and current["sessionId"] != sid):
            return dict.fromkeys(_CELLS)
        observed = False
        row = SimpleNamespace(**session) if isinstance(session, Mapping) else copy.copy(session)
        base_model, base_effort = _session_model_effort(row, _get(row, "liveness"))
        base_context, base_window = _context_gauge(row)
        from .token_budget import context_display_detail
        base_cells = dict(model=base_model, effort=base_effort, context=base_context, window=base_window,
                          context_detail=context_display_detail(row))
        feed = _feed(binding, identity)
        if harness == "claude":
            callback = native_display.claude_callback(tap, sid, int(pid), start)
            if callback is not None:
                claude._apply_statusline(row, callback)
                observed = True
        elif harness == "codex" and native_path and codex._sid(native_path) == sid:
            model, effort = codex._rollout_model_effort(native_path)
            if model:
                row.model = model
                observed = True
            if effort:
                row.effort = effort
            token = codex._tail_token_count(native_path)
            if token:
                codex._apply_token_count(row, token, native_path)
                observed = True
        elif harness == "opencode" and db and feed is None:
            from .session_view import observe
            values, _paths = observe(harness, int(pid), start, sid)
            if values:
                row.model, row.effort = values.get("model"), values.get("effort")
                if not isinstance(row.context_policy, Mapping) or row.context_policy.get("state") != "applied":
                    row.ctx_pct = None
                    row.context_display_window_tokens = None
                    row.context_policy = {**(row.context_policy or {}), "raw_percentage": values.get("ctx_pct")}
                observed = True
        if feed is not None:
            for key in ("model", "effort"):
                if key in feed:
                    setattr(row, key, feed[key])
            # A native percent is display evidence, not an admitted compaction
            # trigger. Existing policy/token parsers keep an applied gauge.
            if ("context_percent" in feed and
                    (not isinstance(row.context_policy, Mapping) or row.context_policy.get("state") != "applied")):
                row.ctx_pct = None
                row.context_display_window_tokens = None
                row.context_policy = {**(row.context_policy or {}), "raw_percentage": feed["context_percent"]}
                if feed["context_percent"] is None:
                    row.active_context_tokens = None
            observed = True
        if read_proc_start(int(pid)) != start:
            return dict.fromkeys(_CELLS)
        if not observed:
            return None
        state = _get(row, "liveness")
        model, effort = _session_model_effort(row, state)
        context, window = _context_gauge(row)
        return dict(model=model, effort=effort, context=context, window=window,
                    context_detail=context_display_detail(row), _base=base_cells)


class NativeDisplayLayer:
    """Pure cell overlay. Keep complete publication/readiness metadata untouched."""

    def __init__(self):
        self._values = ()
        self._model = self._shown = None
        self._session_inputs = None

    def update(self, values):
        if values is not self._values:
            self._values = values
            self._model = self._shown = None
            self._session_inputs = None
            return True
        return False

    def apply(self, model):
        if model is None or not self._values:
            return model
        if model is self._model:
            return self._shown
        source = model.source_records or {}
        bindings = source.get("_detail_bindings", {})
        tags = source.get("_detail_tags", {})
        inputs = (model.sessions, bindings, tags)
        # Clock and account-usage updates share these deeply frozen inputs.
        # Reuse their display rows without revisiting every session or losing
        # the incoming model's fresh usage, readiness or action metadata.
        if self._session_inputs is not None and all(
                left is right for left, right in zip(inputs, self._session_inputs)):
            self._model = model
            self._shown = self._shown.with_updates(**{
                field.name: getattr(model, field.name)
                for field in dataclass_fields(model) if field.name != "sessions"})
            return self._shown
        fields = {}
        for identity, values in self._values:
            ref = identity[0] + ":" + identity[1]
            binding = bindings.get(ref)
            if binding and tuple(str(part) for part in binding[:4]) == identity and ref in tags:
                fields[tags[ref]] = values
        from .explorer.view_model import freeze
        rows = []
        for row in model.sessions:
            values = fields.get(row.get("tag"))
            if values is None or row.get("state") in {"dead", "stale"}:
                rows.append(row)
                continue
            base = values.get("_base")
            # A read started against an older complete base cannot overwrite a
            # cell the new base already changed while that read was in flight.
            updates = {key: freeze(values[key]) for key in _CELLS if key in values
                       and (base is None or freeze(base.get(key)) == row.get(key))
                       and freeze(values[key]) != row.get(key)}
            rows.append(dict(row, **updates) if updates else row)
        self._model, self._shown = model, model.with_updates(sessions=rows)
        self._session_inputs = inputs
        return self._shown
