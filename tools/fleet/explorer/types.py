"""Typed source-reader requests and results consumed by the immutable view model."""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class FocusKey:
    harness: str
    sid: str
    generation: str = ""


@dataclass(frozen=True)
class Health:
    state: str = "unknown"
    last_success_at: Optional[float] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ModelCatalog:
    """A bounded, read-only runtime catalog observation for one harness."""
    harness: str
    state: str = "unknown"
    source: Optional[str] = None
    models: Tuple[str, ...] = ()
    observed_at: Optional[float] = None


@dataclass(frozen=True)
class AttemptRow:
    """One exact registry attempt and its already-projected source facts."""
    attempt_id: str
    line: int
    status: str
    slug: str
    harness: str
    worker_type: str = ""
    depth: int = 0
    route_id: Optional[str] = None
    owner_route_id: Optional[str] = None
    route_node: Optional[str] = None
    note: str = ""
    timestamp: str = ""
    parent_sid: str = ""
    liveness: str = "unknown"
    decision: Optional[Mapping[str, Any]] = field(default=None, compare=False)
    meta: Mapping[str, str] = field(default_factory=dict, compare=False)
    registered_only: bool = False   # the runtime's row_is_registered_only: not launched yet

    @property
    def outcome(self) -> Optional[str]:
        return (self.decision or {}).get("outcome") if self.decision else None

    @property
    def rank(self):
        """Current-attempt order within one route node: a started attempt outranks one that
        is only registered (a pre-registered chain successor), then the later registry line."""
        return (not self.registered_only, self.line)


@dataclass(frozen=True)
class FocusRequest:
    key: FocusKey
    epoch: int
    snapshot_generation: int
    expanded_sessions: Tuple[str, ...] = ()
    wants: Tuple[str, ...] = ()        # extra bounded reads for what is open: seat, pane, cards, dialogue:<i>, raw:<route>


@dataclass(frozen=True)
class FocusResult:
    key: FocusKey
    epoch: int
    snapshot_generation: int
    output: Tuple[Any, ...] = ()
    output_events: Tuple[Any, ...] = ()
    health: Health = Health()
    obligations: Optional[Tuple[Mapping[str, Any], ...]] = None
    events: Tuple[Mapping[str, Any], ...] = ()
    sources: Tuple[Tuple[str, Health], ...] = ()
    output_title: str = ""
    output_notes: Tuple[str, ...] = ()
    output_health: Optional[Health] = None
    output_more: bool = False
    lineage: Optional[Tuple[Mapping[str, Any], ...]] = None
    lineage_health: Optional[Health] = None
    source_records: Mapping[str, Any] = field(default_factory=dict, compare=False)


class SelectionReader:
    """Adapt a selected-scope provider to the common immutable result shape."""

    def __init__(self, providers):
        self.providers = providers

    def read(self, request):
        result = self.providers(request)
        if isinstance(result, FocusResult):
            return result
        return FocusResult(request.key, request.epoch, request.snapshot_generation,
                           output=tuple(result.get("output", ())),
                           events=tuple(result.get("events", ())),
                           health=Health("success"),
                           source_records=result.get("source_records", {}))


@dataclass(frozen=True)
class ExplorerDetail:
    """What the explorer feed hands the view-model builder: the newest accepted read of each kind.

    ``focus`` is the selected session's result; ``seats`` maps ``harness:sid`` to that session's seat
    history (the list views), ``panes`` maps a session id to its herdr pane, ``config`` is the cached
    model/dispatch observation. Every part is the
    last good value; a part that was never read stays ``None``/empty.
    """
    focus: Optional[FocusResult] = None
    seats: Mapping[str, Any] = field(default_factory=dict, compare=False)
    config: Optional[Mapping[str, Any]] = field(default=None, compare=False)
    panes: Mapping[str, str] = field(default_factory=dict, compare=False)
    catalogs: Mapping[str, ModelCatalog] = field(default_factory=dict, compare=False)
    source_records: Mapping[str, Any] = field(default_factory=dict, compare=False)
