"""Event injectors: every event goes through the real writer, never a hand imitation.

Each injector returns ``{"kind", "marker", "t_ns", "wall", "t_start_ns", "write_s"}``.
``t_ns`` (``time.monotonic_ns``) and ``wall`` (``time.time``) are taken the moment
the write is complete, which is the earliest a reader can see it. ``marker`` is a
unique value the event leaves in the state, for a reader that wants to find it.
The writers' own cost (``write_s``, the registry rewrite is a full-file rewrite)
is not part of any latency: latency starts at the completed write.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import time

from . import fixture as fx  # noqa: F401  (puts utilities/ and tools/ on sys.path)

KINDS = ("route-start", "stage-transition", "route-end", "worker-tool",
         "answer-wait", "owner-dead")


@contextlib.contextmanager
def applied(fixture: dict):
    """Run a real writer against the fixture: its env applied only for the call."""
    env = fixture["env"]
    keys = ("HOME", "XDG_STATE_HOME", "AGENT_DISPATCH_JOBS", "CLAUDE_CONFIG_DIR", "AGENT_HOME",
            "HARNESS_STATE_ROOT", "AGENT_PEER_LEDGER_ROOT")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.update({key: env[key] for key in keys if key in env})
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _registry():
    import dispatch_contract_registry as registry
    return registry


def _lines(jobs: str) -> list[str]:
    with open(jobs, encoding="utf-8") as handle:
        return handle.read().split("\n")[:-1]


def _atomic_json(path: str | Path, value: dict) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _stamp(kind: str, marker: str, started: int, extra: dict | None = None) -> dict:
    t_ns = time.monotonic_ns()
    return {"kind": kind, "marker": marker, "t_ns": t_ns, "wall": time.time(),
            "t_start_ns": started, "write_s": (t_ns - started) / 1e9, **(extra or {})}


def _route_index(fixture: dict, n: int) -> dict:
    routes = fixture["routes"]
    return routes[n % len(routes)]


def route_start(fixture: dict, n: int) -> dict:
    """A new attempt row is appended, then its launch identity rewrites the registry."""
    import hook_perf_fixture as hpf
    index = 10_000 + n
    session = fixture["sessions"][n % len(fixture["sessions"])]
    marker = f"att-e2e-owner-{index:05d}"
    route_file = Path(fixture["root"]) / "routes" / f"{hpf.e2e_route_id(index)}.json"
    _atomic_json(route_file, hpf.e2e_route_record(index))
    row = hpf.e2e_open_row(index, jobs_log=Path(fixture["jobs"]), route_file=route_file,
                           session_id=session["session_id"], pid=None, pid_start=None,
                           log_file=None, row_bytes=fx.ROW_BYTES, now=time.time())
    registry = _registry()
    with applied(fixture):
        started = time.monotonic_ns()
        registry._append_registry_rows(Path(fixture["jobs"]), [row.rstrip("\n")])
        stamp = _stamp("route-start", marker, started)
        # The launch identity is written by a second, whole-file replacement.
        lines = _lines(fixture["jobs"])
        lines[-1] = lines[-1].replace(",launch_started=1", ",launch_started=1,pid_ns=pid:[e2e]")
        registry._atomic_registry_replace(Path(fixture["jobs"]), lines)
    stamp["done_ns"] = time.monotonic_ns()
    return stamp


def stage_transition(fixture: dict, n: int) -> dict:
    """The route file's node state moves on and the registry row follows (whole-file replace)."""
    import hook_perf_fixture as hpf
    route = _route_index(fixture, n)
    stage = ("test", "report")[(n // len(fixture["routes"])) % 2]
    marker = f"{route['route_id']}:{stage}:{n}"
    registry = _registry()
    with applied(fixture):
        started = time.monotonic_ns()
        record = hpf.e2e_route_record(route["index"], stage=stage)
        _atomic_json(route["route_file"], record)
        lines = _lines(fixture["jobs"])
        needle = f"attempt_id={route['attempt_id']}"
        for position in range(len(lines) - 1, -1, -1):
            if needle in lines[position]:
                lines[position] = lines[position].replace("route_node=execute", f"route_node={stage}")
                break
        registry._atomic_registry_replace(Path(fixture["jobs"]), lines)
        return _stamp("stage-transition", marker, started)


def route_end(fixture: dict, n: int) -> dict:
    """The route completes: route file, ``.outcome.json`` sidecar, then a closing registry row."""
    import hook_perf_fixture as hpf
    route = _route_index(fixture, n)
    marker = f"{route['route_id']}:end:{n}"
    registry = _registry()
    with applied(fixture):
        started = time.monotonic_ns()
        record = hpf.e2e_route_record(route["index"], stage="report", state="done")
        _atomic_json(route["route_file"], record)
        _atomic_json(Path(route["route_file"]).with_suffix(".outcome.json"),
                     {"present": True, "match": True, "terminal_gate_proven": True,
                      "route_id": route["route_id"], "marker": marker})
        lines = _lines(fixture["jobs"])
        needle = f"attempt_id={route['attempt_id']}"
        for position in range(len(lines) - 1, -1, -1):
            if needle in lines[position]:
                fields = lines[position].split("\t")
                fields[0] = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
                fields[1] = "done"
                registry._append_registry_rows(Path(fixture["jobs"]), ["\t".join(fields)])
                break
        return _stamp("route-end", marker, started)


def worker_tool(fixture: dict, n: int) -> dict:
    """The worker stream gains a ``tool_use`` line (Claude stream-json)."""
    route = _route_index(fixture, n)
    tool = f"E2eTool{n:04d}"
    started = time.monotonic_ns()
    line = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"toolu_e2e_{n:04d}", "name": tool,
         "input": {"command": f"echo {tool}"}}]}}
    with open(route["log_file"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return _stamp("worker-tool", tool, started)


def answer_wait(fixture: dict, n: int) -> dict:
    """A real answer-wait signal (``write_signal``); the item id is what the cache then holds."""
    import human_wait_state
    session = fixture["sessions"][n % len(fixture["sessions"])]
    with applied(fixture):
        started = time.monotonic_ns()
        value = human_wait_state.signal_value(
            "claude", session["session_id"], f"e2e-seat-{n % len(fixture['sessions']):03d}",
            f"[답변 필요] e2e wait {n}", sequence=n + 1)
        if value is None or not human_wait_state.write_signal(value):
            raise RuntimeError("answer-wait signal was not written")
        return _stamp("answer-wait", value["id"], started)


def owner_dead(fixture: dict, n: int) -> dict:
    """The owner process of one route (a fixture sleeper) ends: SIGTERM to our own child only."""
    live = [r for r in fixture["routes"] if r.get("owner_pid") and not r.get("_killed")]
    if not live:
        raise RuntimeError("no live fixture owner left")
    route = live[0]
    started = time.monotonic_ns()
    # Only a process this fixture started, pinned by (pid, start ticks), receives the signal.
    if fx._process_start(route["owner_pid"]) != route["owner_pid_start"]:
        raise RuntimeError("fixture owner pid no longer matches its start identity")
    os.kill(route["owner_pid"], signal.SIGTERM)
    route["_killed"] = True
    return _stamp("owner-dead", f"owner-pid-{route['owner_pid']}", started)


def registry_replace(fixture: dict, n: int = 0) -> dict:
    """The whole registry is replaced by the real writer with identical content (new inode).

    Used by the history-scale contrast: only the replacement differs from the
    unchanged state, so the next pass's extra cost is what a replacement costs.
    """
    registry = _registry()
    with applied(fixture):
        started = time.monotonic_ns()
        lines = _lines(fixture["jobs"])
        registry._atomic_registry_replace(Path(fixture["jobs"]), lines)
        return _stamp("registry-replace", f"replace-{n}", started)


INJECTORS = {
    "route-start": route_start, "stage-transition": stage_transition, "route-end": route_end,
    "worker-tool": worker_tool, "answer-wait": answer_wait, "owner-dead": owner_dead,
}


def inject(fixture: dict, kind: str, n: int) -> dict:
    return INJECTORS[kind](fixture, n)
