"""The experiment board (``utilities/experiment_board``) for Fleet's per-project LAB table.

Read-only and best-effort, next to ``project_status``: it finds the artifact root the same way
(file reads only, no git process) and asks ``experiment_board.read`` for that root's board.  The
board keeps its own cache (a stat key plus a 60 s limit), so a Fleet pass that finds nothing
changed costs a few ``stat`` calls.  One call spends at most ``budget`` seconds; a root it does not
reach keeps its last board and is read on a later pass.  Nothing here writes, gates, or asks.
"""
import os
from pathlib import Path
import sys
import time

from . import project_status

_UTILITIES = str(Path(__file__).resolve().parents[3] / 'utilities')
if _UTILITIES not in sys.path:
    sys.path.insert(0, _UTILITIES)

BUDGET = 3.0              # seconds one call may spend reading boards (the rest waits a pass)
_ROOTS = {}               # cwd -> (resolved_at, root or None)
_LAST = {}                # root -> last board


def clear_cache():
    _ROOTS.clear()
    _LAST.clear()
    try:
        import experiment_board
        experiment_board.clear_cache()
    except Exception:
        pass


def collect(cwds, now=None, budget=BUDGET):
    """``{cwd: board or None}`` for the given session folders; never raises."""
    now = time.time() if now is None else now
    deadline = time.monotonic() + budget
    result = {}
    try:
        import experiment_board
    except Exception:
        return {str(cwd): None for cwd in cwds or () if cwd}
    for cwd in dict.fromkeys(str(cwd) for cwd in cwds or () if cwd):
        try:
            resolved = _ROOTS.get(cwd)
            if resolved is None or now - resolved[0] >= project_status.INTERVAL:
                resolved = (now, project_status.artifact_root(cwd))
                _ROOTS[cwd] = resolved
            root = resolved[1]
            if root and time.monotonic() < deadline:
                _LAST[root], _watch = experiment_board.read(Path(root), now=now)
            result[cwd] = _LAST.get(root) if root else None
        except Exception:
            result[cwd] = _LAST.get(resolved[1]) if resolved and resolved[1] else None
    return result
