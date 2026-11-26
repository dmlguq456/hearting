"""Event injectors: every event goes through the real writer, never a hand imitation.

Each injector returns ``{"kind", "marker", "t_ns", "wall", "t_start_ns", "write_s",
"expect"}``. ``t_ns`` (``time.monotonic_ns``) and ``wall`` (``time.time``) are taken
the moment the event's last write is complete, which is the earliest a reader can
see all of it. ``marker`` is a unique value the event leaves in the state.
``expect`` names the screen values the event changes (session, route id, owner
attempt, stage, tool name, item id): an arrival is counted only when the
reader's own output shows them (``visible.py``), never when something merely
published after the write.

The writers follow the operating ones: the registry changes by whole-file
replacement (``_atomic_registry_replace``, as route completion and launch
identity do), a launch also writes the session route-chain line
(``route_chain.append``), and a worker stream lives where the reader admits it
(``<registry dir>/logs/<name>.<attempt_id>.claude.jsonl``). The writers' own cost
(``write_s``) is not part of any latency.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
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


def _counter(fixture: dict, kind: str) -> int:
    counts = fixture.setdefault("_event_counts", {})
    counts[kind] = counts.get(kind, 0) + 1
    return counts[kind] - 1


def _open_routes(fixture: dict) -> list[dict]:
    """Routes whose owner has neither been closed nor killed by an earlier event."""
    return [r for r in fixture["routes"] if not r.get("_closed") and not r.get("_killed")]


def _pick(fixture: dict, kind: str) -> dict:
    """The route one event acts on. Closing events consume routes from opposite ends
    of the list so two kinds never take the same route; the others rotate."""
    usable = _open_routes(fixture)
    if not usable:
        raise RuntimeError("no open fixture route left")
    count = _counter(fixture, kind)
    if kind == "route-end":
        return usable[0]
    if kind == "owner-dead":
        live = [r for r in usable if r.get("owner_pid")]
        if not live:
            raise RuntimeError("no live fixture owner left")
        return live[-1]
    return usable[count % len(usable)]


def _row_position(lines: list[str], attempt: str) -> int:
    needle = f"attempt_id={attempt}"
    for position in range(len(lines) - 1, -1, -1):
        if needle in lines[position]:
            return position
    raise RuntimeError(f"no registry row for {attempt}")


def _session_chain(fixture: dict, record: dict, session_id: str, route_file: Path, event: str) -> None:
    """The session route-chain line a launch writes (``route_chain.append``, as the original routes got)."""
    from fleet import route_chain
    with applied(fixture):
        line = route_chain.build_line(record, event=event, harness="claude", session_id=session_id,
                                      route_file=route_file, now=time.time())
        if not route_chain.append("claude", session_id, line):
            raise RuntimeError("route-chain line was not written")


def _logs_dir(fixture: dict) -> Path:
    return Path(fixture["jobs"]).parent / "logs"


def route_start(fixture: dict, n: int) -> dict:
    """A launch: route file, session route-chain line, attempt row, then the launch identity.

    The same order the operating launch uses: the route record and its chain line exist
    before the registry claim row is appended; the launch identity (log path) is a
    whole-registry replacement after it. Arrival is the new route's line in the panel.
    """
    import hook_perf_fixture as hpf
    index = 10_000 + n
    session = fixture["sessions"][n % len(fixture["sessions"])]
    attempt = f"att-e2e-owner-{index:05d}"
    route_id = hpf.e2e_route_id(index)
    route_file = Path(fixture["root"]) / "routes" / f"{route_id}.json"
    record = hpf.e2e_route_record(index)
    log_file = _logs_dir(fixture) / f"e2e.{attempt}.claude.jsonl"
    # The launched owner process (a marked fixture sleeper, pinned by pid + start ticks).
    (pid, pid_start), = fx.spawn_sleepers(fixture["marker"], 1, fixture["env"])
    started = time.monotonic_ns()
    _atomic_json(route_file, record)
    log_file.write_text(json.dumps({"type": "system", "subtype": "init", "session_id": session["session_id"]}) + "\n",
                        encoding="utf-8")
    _session_chain(fixture, record, session["session_id"], route_file, "start")
    row = hpf.e2e_open_row(index, jobs_log=Path(fixture["jobs"]), route_file=route_file,
                           session_id=session["session_id"], pid=None, pid_start=None,
                           log_file=None, row_bytes=fx.ROW_BYTES, now=time.time())
    registry = _registry()
    with applied(fixture):
        registry._append_registry_rows(Path(fixture["jobs"]), [row.rstrip("\n")])
        # The launch identity is written by a second, whole-file replacement.
        lines = _lines(fixture["jobs"])
        position = _row_position(lines, attempt)
        lines[position] = lines[position].replace(
            ",launch_started=1",
            f",launch_started=1,pid={pid},pid_start={pid_start},pid_ns=pid:[e2e],log_file={log_file}")
        registry._atomic_registry_replace(Path(fixture["jobs"]), lines)
    return _stamp("route-start", route_id, started, {"expect": {
        "session_id": session["session_id"], "route_id": route_id, "attempt_id": attempt}})


def stage_transition(fixture: dict, n: int) -> dict:
    """The route file's node state moves on and the registry row follows (whole-file replace)."""
    import hook_perf_fixture as hpf
    route = _pick(fixture, "stage-transition")
    stage = "test" if route.get("_stage", "execute") != "test" else "report"
    route["_stage"] = stage
    marker = f"{route['route_id']}:{stage}:{n}"
    registry = _registry()
    with applied(fixture):
        started = time.monotonic_ns()
        _atomic_json(route["route_file"], hpf.e2e_route_record(route["index"], stage=stage))
        lines = _lines(fixture["jobs"])
        position = _row_position(lines, route["attempt_id"])
        lines[position] = re.sub(r"route_node=[A-Za-z0-9_-]+", f"route_node={stage}", lines[position], count=1)
        registry._atomic_registry_replace(Path(fixture["jobs"]), lines)
        return _stamp("stage-transition", marker, started, {"expect": {
            "session_id": route["session_id"], "route_id": route["route_id"],
            "attempt_id": route["attempt_id"], "stage": stage}})


def route_end(fixture: dict, n: int) -> dict:
    """The route completes the way the operating completion writes it: route file and
    ``.outcome.json`` sidecar, then the attempt's own row closed in place by a whole-registry
    replacement (``capability_route_completion`` -> ``_atomic_registry_replace``)."""
    import hook_perf_fixture as hpf
    route = _pick(fixture, "route-end")
    route["_closed"] = True
    marker = f"{route['route_id']}:end:{n}"
    registry = _registry()
    with applied(fixture):
        started = time.monotonic_ns()
        _atomic_json(route["route_file"], hpf.e2e_route_record(route["index"], stage="report", state="done"))
        _atomic_json(Path(route["route_file"]).with_suffix(".outcome.json"),
                     {"terminal_gate_proven": True, "marker": marker,
                      "closed_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                      "route_id": route["route_id"], "route_hash": hpf.e2e_route_hash(route["index"])})
        lines = _lines(fixture["jobs"])
        position = _row_position(lines, route["attempt_id"])
        fields = lines[position].split("\t")
        fields[1] = "done"
        fields[5] += ",failure_class=pass"
        lines[position] = "\t".join(fields)
        registry._atomic_registry_replace(Path(fixture["jobs"]), lines)
        return _stamp("route-end", marker, started, {"expect": {
            "session_id": route["session_id"], "route_id": route["route_id"],
            "attempt_id": route["attempt_id"]}})


def worker_tool(fixture: dict, n: int) -> dict:
    """The worker stream gains a ``tool_use`` line (Claude stream-json), at the path the reader admits."""
    route = _pick(fixture, "worker-tool")
    tool = f"E2eTool{n:04d}"
    started = time.monotonic_ns()
    line = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"toolu_e2e_{n:04d}", "name": tool,
         "input": {"command": f"echo {tool}"}}]}}
    with open(route["log_file"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return _stamp("worker-tool", tool, started, {"expect": {
        "session_id": route["session_id"], "route_id": route["route_id"],
        "attempt_id": route["attempt_id"], "tool": tool}})


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
        return _stamp("answer-wait", value["id"], started, {"expect": {
            "session_id": session["session_id"], "item_id": value["id"]}})


def owner_dead(fixture: dict, n: int) -> dict:
    """The owner process of one route (a fixture sleeper) ends: SIGTERM to our own child only."""
    route = _pick(fixture, "owner-dead")
    started = time.monotonic_ns()
    # Only a process this fixture started, pinned by (pid, start ticks), receives the signal.
    if fx._process_start(route["owner_pid"]) != route["owner_pid_start"]:
        raise RuntimeError("fixture owner pid no longer matches its start identity")
    os.kill(route["owner_pid"], signal.SIGTERM)
    route["_killed"] = True
    return _stamp("owner-dead", f"owner-pid-{route['owner_pid']}", started, {"expect": {
        "session_id": route["session_id"], "route_id": route["route_id"],
        "attempt_id": route["attempt_id"]}})


def registry_replace(fixture: dict, n: int = 0) -> dict:
    """The whole registry is replaced by the real writer with ONE line's content changed.

    The line is the last one (a recent attempt row, where an operating update lands). A
    byte inside its free-text pad flips, so the file's digest and that line's bytes are new
    while every other line, and the common prefix before it, is unchanged. An identical
    rewrite would let a digest cache answer without parsing and hide what a replacement costs.
    """
    registry = _registry()
    with applied(fixture):
        started = time.monotonic_ns()
        lines = _lines(fixture["jobs"])
        last = lines[-1]
        pad = last.rfind(",note=")
        flip = "yzabcd"[n % 6]
        if pad >= 0 and len(last) > pad + len(",note=") + 1:
            at = pad + len(",note=")
            lines[-1] = last[:at] + (flip if last[at] != flip else "q") + last[at + 1:]
        else:
            lines[-1] = last + f",e2e_flip={n}"
        registry._atomic_registry_replace(Path(fixture["jobs"]), lines)
        return _stamp("registry-replace", f"replace-{n}", started)


INJECTORS = {
    "route-start": route_start, "stage-transition": stage_transition, "route-end": route_end,
    "worker-tool": worker_tool, "answer-wait": answer_wait, "owner-dead": owner_dead,
}


def inject(fixture: dict, kind: str, n: int) -> dict:
    return INJECTORS[kind](fixture, n)
