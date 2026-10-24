"""Narrow pane-focus runner for an explicitly selected asking session.

The runner owns no pane discovery and does not synthesize a pane identity. A caller
supplies the current pane graph and a focus operation backed by its terminal adapter;
the exact session-to-pane edge is validated immediately before the operation.
"""
from dataclasses import dataclass
from collections.abc import Mapping


@dataclass(frozen=True)
class PaneFocusResult:
    status: str
    pane_id: str = ""
    reason: str = ""


def pane_for_session(graph, harness, session_id):
    """Return the unique pane id joined by exact harness and session id, else ``None``."""
    matches = []
    for edge in graph or ():
        if not isinstance(edge, Mapping):
            continue
        if edge.get("harness") != harness or edge.get("session_id") != session_id:
            continue
        pane_id = edge.get("pane_id")
        if isinstance(pane_id, str) and pane_id.strip():
            matches.append(pane_id)
    unique = tuple(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


class PaneFocusRunner:
    """Run one focus action for a unique exact session edge in the latest graph."""

    def __init__(self, focus):
        if not callable(focus):
            raise TypeError("focus callback required")
        self._focus = focus

    def run(self, graph, harness, session_id):
        pane_id = pane_for_session(graph, harness, session_id)
        if pane_id is None:
            return PaneFocusResult("unavailable", reason="no unique exact pane edge")
        try:
            result = self._focus(pane_id)
        except Exception as exc:
            return PaneFocusResult("failed", pane_id, type(exc).__name__)
        if result is False:
            return PaneFocusResult("failed", pane_id, "focus rejected")
        return PaneFocusResult("focused", pane_id)
