"""Immutable input records for the Fleet explorer renderer."""
from __future__ import annotations

from dataclasses import dataclass, fields
from types import MappingProxyType
from collections.abc import Mapping


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
