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

from ..model import project_of


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


def _config_path():
    override = os.environ.get("COMPUTE_HOSTS_CONFIG")
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "hearting" / "compute-hosts.yaml"


def _unconfigured(status, path):
    """Guidance-only snapshot: nothing to probe, but the panel says what to do."""
    hint = ("edit the seeded template" if status == "template"
            else "run `harness install` to seed a template")
    return {"configured": False, "status": status, "hosts": [],
            "path": str(path), "hint": hint, "observed_at": time.time()}


def _diagnostic(message, observed_at=None):
    return {
        "configured": True,
        "hosts": [],
        "error": str(message or "compute-host probe failed")[:300],
        "observed_at": observed_at if observed_at is not None else time.time(),
    }


def collect(timeout=COLLECT_TIMEOUT):
    """Return a host snapshot, a probe diagnostic, or an unconfigured guidance block."""
    path = _config_path()
    if not path.is_file():
        return _unconfigured("missing", path)
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
        # A config can disappear between the pre-check and subprocess startup;
        # treat that race exactly like an initially absent config. A seeded but
        # still-commented template is guidance, not a probe failure.
        if "not initialized" in detail:
            return _unconfigured("missing", path)
        if "has no hosts yet" in detail:
            return _unconfigured("template", path)
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


def _pos_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonneg_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _session_key(owner):
    """`(harness, id)` of a valid session owner, else None (same rule as F-88)."""
    if (isinstance(owner, dict) and owner.get("kind") == "session"
            and owner.get("harness") in {"claude", "codex", "opencode"}
            and isinstance(owner.get("id"), str) and owner.get("id")):
        return (owner["harness"], owner["id"])
    return None


def _registered_run_marks(resource_jobs):
    """Identity of every working registered run: (pid, starttime) and process groups."""
    exact, groups = set(), set()
    for job in resource_jobs or ():
        if getattr(job, "liveness", None) != "working":
            continue
        pid = getattr(job, "pid", None)
        if _pos_int(pid):
            exact.add((pid, str(getattr(job, "starttime", None))))
        group = getattr(job, "process_group", None)
        if _pos_int(group):
            groups.add(group)
    return exact, groups


def unregistered_gpu(snapshot, resource_jobs=(), shown_sessions=frozenset(), age_s=0.0):
    """One entry per live GPU process not already shown elsewhere (F-104). No I/O.

    A process is skipped when a working registered resource run owns it (same
    pid+start, or same process group; only on the host Fleet runs on) or when
    the F-88 session GPU line for its `session_owner` is on screen. `cwd` picks
    the project card only; it is never ownership evidence.
    """
    if (not isinstance(snapshot, dict) or not snapshot.get("configured")
            or snapshot.get("error")):
        return []
    hosts = snapshot.get("hosts")
    if not isinstance(hosts, (list, tuple)):
        return []
    run_exact, run_groups = _registered_run_marks(resource_jobs)
    entries = {}
    for host in hosts:
        if not isinstance(host, dict) or host.get("reachable") is not True:
            continue
        host_name = host.get("host") if isinstance(host.get("host"), str) else "?"
        is_self = host.get("self") is True
        gpus = host.get("gpus")
        if not isinstance(gpus, (list, tuple)):
            continue
        for gpu in gpus:
            if not isinstance(gpu, dict):
                continue
            gpu_index = gpu.get("index")
            processes = gpu.get("processes")
            if not _nonneg_int(gpu_index) or not isinstance(processes, (list, tuple)):
                continue
            for process in processes:
                if not isinstance(process, dict) or not _pos_int(process.get("pid")):
                    continue
                pid, proc_start = process["pid"], process.get("proc_start")
                pgid = process.get("pgid")
                if is_self and ((pid, str(proc_start)) in run_exact
                                or (_pos_int(pgid) and pgid in run_groups)):
                    continue
                session_owner = process.get("session_owner")
                if _session_key(session_owner) in shown_sessions:
                    continue
                key = (host_name, pid, proc_start)
                entry = entries.get(key)
                used = process.get("used_memory_mib")
                used = used if _nonneg_int(used) else None
                if entry is None:
                    cwd = process.get("cwd")
                    cwd = cwd if isinstance(cwd, str) and cwd.startswith("/") else None
                    elapsed = process.get("elapsed_s")
                    owner = process.get("owner")
                    entry = {
                        "host": host_name, "self": is_self, "gpu_indexes": [],
                        "gpu_name": gpu.get("name") if isinstance(gpu.get("name"), str) else None,
                        "pid": pid, "proc_start": proc_start,
                        "pgid": pgid if _pos_int(pgid) else None,
                        "used_memory_mib": None,
                        "elapsed_s": (elapsed + max(0, int(age_s or 0)))
                        if _nonneg_int(elapsed) else None,
                        "command": process.get("command")
                        if isinstance(process.get("command"), str) else None,
                        "process_name": process.get("process_name")
                        if isinstance(process.get("process_name"), str) else None,
                        "cwd": cwd,
                        "project": project_of(cwd) if cwd else "(unknown)",
                    }
                    if (isinstance(owner, dict) and owner.get("kind") in {"job", "run"}
                            and isinstance(owner.get("label"), str) and owner.get("label")):
                        entry["owner_kind"] = owner["kind"]
                        entry["owner_label"] = owner["label"]
                    if isinstance(session_owner, dict):
                        entry["session_owner"] = session_owner
                    entries[key] = entry
                if gpu_index not in entry["gpu_indexes"]:
                    entry["gpu_indexes"].append(gpu_index)
                if used is not None:
                    entry["used_memory_mib"] = (entry["used_memory_mib"] or 0) + used
    out = list(entries.values())
    for entry in out:
        entry["gpu_indexes"].sort()
    out.sort(key=lambda e: (e["project"], e["host"], e["gpu_indexes"][0],
                            -(e["used_memory_mib"] or 0), e["pid"]))
    return out
