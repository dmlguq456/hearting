"""Immutable input records for the Fleet explorer renderer."""
from __future__ import annotations

from dataclasses import dataclass, fields
from types import MappingProxyType
from collections.abc import Mapping
import math
import re
import unicodedata


PERSISTED_VM_FIELDS = frozenset({
    "clock", "hosts", "usage", "census", "repos", "sessions", "routes",
    "compute", "ordering",
})

_PERSISTED_ROW_FIELDS = {
    "host": frozenset({"mark", "name", "key"}),
    "usage": frozenset({"harness", "text", "key", "windows"}),
    "repo": frozenset({"id", "name", "done", "age"}),
    # Display identifiers and short labels are useful in the stale view. Process,
    # pane, cwd, source, environment, and transcript values are deliberately absent.
    "session": frozenset({"tag", "harness", "model", "effort", "state", "steward",
                          "title", "context", "window", "now", "age", "repo",
                          "repo_id", "branch", "generation", "last_clear"}),
    "route": frozenset({"id", "repo", "session_tag", "title", "state", "worker",
                        "age", "legs", "stages", "harness", "model", "effort",
                        "mode", "intensity", "profile", "capability", "shape",
                        "worker_stage"}),
    "stage": frozenset({"name", "st", "el", "h", "m", "e", "gate", "r"}),
}

_SECRET_TEXT = re.compile(
    r"(?i)(?:\b(?:bearer|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"password|passwd|secret|authorization)(?:[_ -]?(?:sentinel|value|token|key))?\s*[:=]|"
    r"\b(?:sk-[a-z0-9]{16,}|gh[pousr]_[a-z0-9]{20,})\b)"
)
_ORDER_KEYS = frozenset({"saved", "repo_keys", "session_keys", "tag_keys",
                         "repo_by_tag", "tags_by_key", "tag_cycle"})


def freeze(value):
    """Recursively freeze JSON-like data without inventing defaults."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze(item) for item in value)
    return value


def thaw(value):
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, frozenset)):
        return [thaw(item) for item in value]
    return value


def _safe_text(value, *, limit=512):
    if not isinstance(value, str) or len(value) > limit:
        return None
    if any(unicodedata.category(char) == "Cc" for char in value):
        return None
    if _SECRET_TEXT.search(value):
        return None
    return value


def _safe_scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 2**53 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return _safe_text(value)


def _safe_display(value, *, depth=0):
    """Bound the small numeric/text arrays used for quota and compute display."""
    if depth > 8:
        return None
    scalar = _safe_scalar(value)
    if scalar is not None or value is None:
        return scalar
    if isinstance(value, (list, tuple)):
        return [_safe_display(item, depth=depth + 1) for item in value[:512]]
    if isinstance(value, Mapping):
        safe = {}
        allowed = {"name", "label", "text", "key", "state", "unit", "value", "used",
                   "total", "pct", "count", "index", "period", "reset", "window",
                   "limit", "remaining", "mark", "harness", "rows", "actual", "target"}
        denied = ("raw", "source", "config", "dialogue", "pane", "attempt", "question",
                  "secret", "token", "password", "auth", "credential", "environment",
                  "transcript", "content", "lines", "env")
        for key, item in list(value.items())[:128]:
            name = _safe_text(str(key), limit=80)
            lowered = name.lower() if name is not None else ""
            if name is None or any(word in lowered for word in denied) or lowered not in allowed:
                continue
            clean = _safe_display(item, depth=depth + 1)
            if clean is not None:
                safe[name] = clean
        return safe
    return None


def _safe_row(value, kind):
    if not isinstance(value, Mapping):
        return None
    allowed = _PERSISTED_ROW_FIELDS[kind]
    row = {}
    for key in allowed:
        if key not in value:
            continue
        item = value[key]
        if kind == "route" and key == "stages":
            stages = []
            for stage in item if isinstance(item, (list, tuple)) else ():
                safe_stage = _safe_row(stage, "stage")
                if safe_stage is not None:
                    stages.append(safe_stage)
            row[key] = stages
        elif key in {"model", "worker", "legs"}:
            row[key] = _safe_display(item)
        elif key == "windows":
            row[key] = _safe_display(item)
        else:
            clean = _safe_scalar(item)
            if clean is not None:
                row[key] = clean
    return row


def _safe_ordering(value):
    if not isinstance(value, Mapping):
        return None
    result = {}
    for key in _ORDER_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key in {"repo_keys", "tag_cycle"}:
            clean = _safe_display(item)
        elif key in {"session_keys", "tag_keys", "repo_by_tag", "tags_by_key"}:
            clean = _safe_identifier_map(item)
        elif key == "saved" and isinstance(item, Mapping):
            clean = _safe_ordering({
                "repo_keys": item.get("repos", ()),
                "session_keys": item.get("sessions", {}),
            })
        else:
            clean = None
        if clean is not None:
            if key == "saved":
                result[key] = {"repos": clean.get("repo_keys", []),
                               "sessions": clean.get("session_keys", {})}
            else:
                result[key] = clean
    return result


def _safe_identifier_map(value):
    if not isinstance(value, Mapping):
        return None
    safe = {}
    for key, item in list(value.items())[:10000]:
        name = _safe_text(str(key), limit=256)
        if name is None:
            continue
        clean = _safe_display(item)
        if clean is not None:
            safe[name] = clean
    return safe


def persistable_view_model(value):
    """Return only allowlisted, bounded first-screen data suitable for private persistence."""
    vm = ExplorerViewModel.from_mapping(value)
    source = vm.to_mapping()
    result = {}
    for key in PERSISTED_VM_FIELDS:
        if key not in source:
            continue
        item = source[key]
        if key == "clock":
            clean = _safe_scalar(item)
            if isinstance(clean, bool) or not isinstance(clean, (int, float)):
                clean = None
        elif key in {"hosts", "usage", "repos", "sessions", "routes"}:
            kind = {"hosts": "host", "usage": "usage", "repos": "repo",
                    "sessions": "session", "routes": "route"}[key]
            clean = [_safe_row(row, kind) for row in item] if isinstance(item, (list, tuple)) else []
            clean = [row for row in clean if row is not None]
        elif key == "ordering":
            clean = _safe_ordering(item)
        else:
            clean = _safe_display(item)
        if clean is not None:
            result[key] = clean
    return result


def restore_persisted_view_model(value):
    """Validate the cache payload strictly; unknown or unsafe nested fields invalidate it."""
    if not isinstance(value, Mapping) or set(value) - PERSISTED_VM_FIELDS:
        raise ValueError("persisted view model has unknown fields")
    clean = persistable_view_model(value)
    if clean != dict(value):
        raise ValueError("persisted view model is not in the allowlisted form")
    # Saved display rows carry no current credential admission. Validate the old
    # allowlist first, then replace only Claude usage; the rest remains a stale view.
    from .live import current_claude_usage
    import time
    return current_claude_usage(ExplorerViewModel.from_mapping(clean), {}, time.time())


def decode_fixture_values(value):
    """Expand the compact long-string encoding used by the checked JSON fixture."""
    if isinstance(value, Mapping):
        if set(value) == {"__fixture_zlib_b85__"}:
            import base64
            import zlib
            raw = base64.b85decode(value["__fixture_zlib_b85__"].encode("ascii"))
            return zlib.decompress(raw).decode("utf-8")
        return {str(key): decode_fixture_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [decode_fixture_values(item) for item in value]
    return value


@dataclass(frozen=True)
class ExplorerViewModel:
    """Typed, deeply immutable projection; absent fields stay absent/unknown."""

    identity: object = None
    hearting: object = None
    clock: object = None
    hosts: tuple = ()
    usage: tuple = ()
    census: object = None
    repos: tuple = ()
    sessions: tuple = ()
    routes: tuple = ()
    nodes: tuple = ()
    attempts: tuple = ()
    questions: tuple = ()
    answered: tuple = ()
    seats: tuple = ()
    generations: tuple = ()
    cards: tuple = ()
    dialogue: tuple = ()
    peer_nodes: tuple = ()
    peer_edges: object = None
    compute: tuple = ()
    memory: object = None
    models: object = None
    config: object = None
    system: object = None
    events: tuple = ()
    pane_graph: tuple = ()
    source_records: object = None
    ordering: object = None
    readiness: object = None

    def __post_init__(self):
        if self.peer_edges is None:
            object.__setattr__(self, "peer_edges", {})
        for field in fields(self):
            object.__setattr__(self, field.name, freeze(getattr(self, field.name)))

    @classmethod
    def from_mapping(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("view model must be a mapping or ExplorerViewModel")
        known = {field.name for field in fields(cls)}
        return cls(**{key: value[key] for key in known if key in value})

    def to_mapping(self):
        return {field.name: thaw(getattr(self, field.name)) for field in fields(self)}


def load_fixture(path):
    """Load a checked fixture as an immutable view model."""
    import json
    from pathlib import Path

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, Mapping) and "values" in data:
        data = decode_fixture_values(data["values"])
    return ExplorerViewModel.from_mapping(data)
