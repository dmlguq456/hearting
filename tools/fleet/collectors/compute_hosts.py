"""Fail-soft bridge from Fleet to the user-owned compute-host inventory.

The inventory/probe utility remains the single source of SSH and hostname
semantics. Fleet invokes its JSON surface with a bounded timeout and exposes the
result unchanged enough for diagnostics; it never edits config or chooses a host.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


COLLECT_TIMEOUT = 5.0


def _tool_argv():
    override = os.environ.get("FLEET_COMPUTE_HOSTS_TOOL")
    if override:
        path = Path(override).expanduser()
        return [sys.executable, str(path)] if path.suffix == ".py" else [str(path)]

    here = Path(__file__).resolve()
    for parent in here.parents:
        path = parent / "utilities" / "compute-hosts.py"
        if path.is_file():
            return [sys.executable, str(path)]

    agent_home = os.environ.get("AGENT_HOME")
    if agent_home:
        path = Path(agent_home).expanduser() / "utilities" / "compute-hosts.py"
        if path.is_file():
            return [sys.executable, str(path)]
    return None


def _configured():
    override = os.environ.get("COMPUTE_HOSTS_CONFIG")
    if override:
        return Path(override).expanduser().is_file()
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return (root / "hearting" / "compute-hosts.yaml").is_file()


def _diagnostic(message, observed_at=None):
    return {
        "configured": True,
        "hosts": [],
        "error": str(message or "compute-host probe failed")[:300],
        "observed_at": observed_at if observed_at is not None else time.time(),
    }


def collect(timeout=COLLECT_TIMEOUT):
    """Return a complete host snapshot, diagnostic, or ``None`` when unconfigured."""
    if not _configured():
        return None
    argv = _tool_argv()
    if not argv:
        return _diagnostic("compute-hosts utility unavailable")
    observed_at = time.time()
    try:
        result = subprocess.run(
            argv + ["list", "--json"], text=True, capture_output=True,
            timeout=max(0.1, float(timeout)),
        )
    except subprocess.TimeoutExpired:
        return _diagnostic("compute-host probe timed out", observed_at)
    except OSError as exc:
        return _diagnostic(exc, observed_at)
    if result.returncode:
        detail = (result.stderr or result.stdout or "compute-host probe failed").strip()
        # A config can disappear between the pre-check and subprocess startup. Treat
        # that race exactly like an initially absent config: no empty/error block.
        if "not initialized" in detail:
            return None
        return _diagnostic(detail, observed_at)
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        return _diagnostic("invalid compute-host JSON", observed_at)
    if not isinstance(payload, dict) or not isinstance(payload.get("hosts"), list):
        return _diagnostic("invalid compute-host payload", observed_at)
    snapshot = dict(payload)
    snapshot["configured"] = True
    snapshot["observed_at"] = max(
        [row.get("observed_at") for row in snapshot["hosts"]
         if isinstance(row, dict) and isinstance(row.get("observed_at"), (int, float))]
        or [observed_at]
    )
    return snapshot
