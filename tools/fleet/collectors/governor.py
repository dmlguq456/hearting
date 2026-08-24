"""F-28c (governor half — prd.md:311, model-worker-governor lease) — read-only, tolerant.

Consumes `utilities/model-worker-governor.py`'s `state.json` (real code, real live state —
plan §6a's probe distinguishes this from the OTHER half of F-28c, the resource-runner run
registry, which is skipped for lack of a canonical path — see `_internal/carryover.md`).

**Read-only**: this module never writes `state.json` (governor's own reservation/lease
transactions own that file exclusively).  When no root is exported it invokes the canonical
`artifact-root.sh` resolver once per Fleet process and caches the result; no subprocess is
spawned per tick.
"""
import json
import os
import subprocess
from pathlib import Path

from . import procscan

# utilities/model-worker-governor.py:20 DEFAULT_TOTAL_LIMIT / :18 CLASS_LIMITS — duplicated as a
# plain constant rather than imported (that file resolves its own module path via
# `Path(__file__)`, which is a governor-CLI concern, not something fleet should import machinery
# for). The env var name is the SAME one the governor itself honors, so an operator's override
# of the real cap is picked up here too.
DEFAULT_TOTAL_LIMIT = 12

_CACHE = {}   # {abspath: (mtime, size, result)}
_ROOT_CACHE = {}  # {cwd: canonical governor root}


def _artifact_root_resolver():
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "utilities" / "artifact-root.sh"
        if candidate.is_file():
            return candidate
    return None


def _default_root():
    """Mirror the governor writer's canonical env -> artifact resolver chain."""
    explicit = os.environ.get("AGENT_MODEL_GOVERNOR_ROOT")
    if explicit:
        return explicit
    artifact_root = os.environ.get("AGENT_ARTIFACT_ROOT")
    if artifact_root:
        return os.path.join(artifact_root, ".runtime", "model-worker-governor")
    try:
        cwd = os.path.realpath(os.getcwd())
    except OSError:
        cwd = ""
    if cwd in _ROOT_CACHE:
        return _ROOT_CACHE[cwd]
    resolver = _artifact_root_resolver()
    if resolver is not None:
        try:
            resolved = subprocess.run(
                [str(resolver), cwd or os.curdir], check=False,
                capture_output=True, text=True,
            )
            if resolved.returncode == 0 and resolved.stdout.strip():
                root = os.path.join(
                    resolved.stdout.strip(), ".runtime", "model-worker-governor"
                )
                _ROOT_CACHE[cwd] = root
                return root
        except OSError:
            pass
    root = os.path.join(str(Path.home()), ".agent-worker-governor")
    _ROOT_CACHE[cwd] = root
    return root


def _state_path(root=None):
    return os.path.join(root or _default_root(), "state.json")


def clear_cache():
    """Test hermeticity (route.clear_cache() precedent)."""
    _CACHE.clear()
    _ROOT_CACHE.clear()


def collect(root=None):
    """{"active": n, "cap": m, "classes": {cls: n, ...}} | None.

    None = source absent/unreadable (honest gap — prd.md:292, no guessing) — the caller (render)
    simply omits the pulse-adjacent row (F-28c: "healthy 무음"/absent-source both render nothing,
    a v8-precedent contract). Never raises."""
    path = _state_path(root)
    try:
        st = os.stat(path)
        key = (st.st_mtime, st.st_size)
    except OSError:
        return None
    cached = _CACHE.get(path)
    if cached is not None and (cached[0], cached[1]) == key:
        return cached[2]
    result = _read(path)
    _CACHE[path] = (key[0], key[1], result)
    return result


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    leases = data.get("leases")
    if not isinstance(leases, dict):
        return None
    reservations = data.get("reservations", {})
    if not isinstance(reservations, dict):
        return None
    total = int(os.environ.get("AGENT_MODEL_WORKER_TOTAL", DEFAULT_TOTAL_LIMIT))
    # ★ read-only consequence (plan §6a): governor prunes dead leases only at WRITE time
    # (model-worker-governor.py:80-84) — a read-only consumer can NEVER touch state.json, so a
    # dead lease can sit here indefinitely. Apply the SAME judgment governor itself uses
    # (process starttime match — the identical PID-reuse guard F-25/F-27 already rely on) IN
    # MEMORY ONLY, every read, so `active` never over-counts a dead worker as live.
    classes = {}
    active = 0
    for lease in leases.values():
        if not isinstance(lease, dict):
            continue
        pid = lease.get("pid")
        starttime = lease.get("starttime")
        if pid is None or starttime is None:
            continue
        if procscan.read_proc_start(pid) != str(starttime):
            continue   # dead lease — governor would have pruned this at its next write
        active += 1
        cls = lease.get("class") or "?"
        classes[cls] = classes.get(cls, 0) + 1
    # Atomic replica admission occupies real cap before either runner claims
    # its lease. Count only reservations whose exact owner PID/start identity
    # is still live; this mirrors governor pruning without mutating state.
    for reservation in reservations.values():
        if not isinstance(reservation, dict):
            continue
        pid = reservation.get("owner_pid")
        starttime = reservation.get("owner_starttime")
        if pid is None or starttime is None:
            continue
        if procscan.read_proc_start(pid) != str(starttime):
            continue
        active += 1
        cls = reservation.get("class") or "?"
        classes[cls] = classes.get(cls, 0) + 1
    return {"active": active, "cap": total, "classes": classes}
