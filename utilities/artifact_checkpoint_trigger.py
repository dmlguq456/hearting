#!/usr/bin/env python3
"""Best-effort launcher for cycle checkpoints.

Automatic triggers -- a route node completing, a supervisor poll, a session
turn ending (Claude Stop, Codex Stop, OpenCode `session.idle`), a `begin` naming a
parent or resuming a cycle -- call `launch_for_route` or `launch_for_session`.  They never
raise and never wait: a per-trigger-key stamp keeps a caller from spawning more
than once per interval, and the detached `artifact_producer.py checkpoint`
child applies the authoritative per-cycle gates.  For an open cycle those are
open cycle, route not closed, interval, size limits and staleness; for a closed
one the child looks for files changed since the close (§45 D-124, bounded by one
walk/hash/time budget and holding no lock while it reads), then spends what is
left of the budget on the root's other closed cycles in cursor order.

`AGENT_ARTIFACT_CHECKPOINT=off` disables every automatic trigger; the explicit
`artifact_producer.py checkpoint` command is unaffected.  Nothing launches for a
root whose producer cutover is inactive (no cycles exist there), from a process
that imported unittest or pytest, or -- for suites that drive the CLI in a
subprocess -- when that suite sets `AGENT_ARTIFACT_CHECKPOINT=off`: fixtures
complete nodes in temporary roots they delete, and a detached child writing
there would race that cleanup.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Optional, Tuple
try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test
except ImportError:  # loaded by path without utilities/ on sys.path
    def end_with_test(child):
        return child

UTILITIES = Path(__file__).resolve().parent
PRODUCER = UTILITIES / "artifact_producer.py"
CUTOVER_REL = Path(".runtime/artifact-producer/v1/cutover.json")
DISABLE_ENV = "AGENT_ARTIFACT_CHECKPOINT"
INTERVAL_ENV = "AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL"
DEFAULT_INTERVAL_SECONDS = 900.0
AUTOMATIC_TRIGGERS = ("stage-complete", "supervisor-poll", "turn-end", "begin")
HARNESSES = ("claude", "codex", "opencode")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
STAMP_MAX_AGE_SECONDS = 7 * 86400.0


def in_test_process() -> bool:
    return "unittest" in sys.modules or "pytest" in sys.modules


def disabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(DISABLE_ENV, "")).strip().lower() in {"off", "0", "false", "no", "disabled"}


def interval_seconds(env: Optional[Mapping[str, str]] = None) -> float:
    env = os.environ if env is None else env
    try:
        value = float(env.get(INTERVAL_ENV) or DEFAULT_INTERVAL_SECONDS)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return value if value >= 0 else DEFAULT_INTERVAL_SECONDS


def stamp_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "hearting" / "artifact-checkpoint"


def _claim(key: str, env: Mapping[str, str], now: float) -> bool:
    """True when `key` launched no checkpoint within the interval; records this launch."""
    path = stamp_dir(env) / f"{key}.stamp"
    try:
        last = path.stat().st_mtime
    except FileNotFoundError:
        last = None
    except OSError:
        return False
    if last is not None and now - last < interval_seconds(env):
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (now, now))
    except OSError:
        return False
    if last is None:
        _prune_stamps(path.parent, now)
    return True


def _prune_stamps(directory: Path, now: float) -> None:
    """A new key is rare (one per route or session); drop week-old stamps then."""
    try:
        for entry in directory.iterdir():
            if entry.suffix == ".stamp" and now - entry.stat().st_mtime > STAMP_MAX_AGE_SECONDS:
                entry.unlink()
    except OSError:
        pass


def launch(
    *,
    trigger: str,
    key: str,
    artifact_root: Optional[str] = None,
    cycle_id: Optional[str] = None,
    route: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
) -> bool:
    """Spawn one detached checkpoint child; False when nothing was launched."""
    child_env = dict(os.environ if env is None else env)
    if (disabled(child_env) or in_test_process() or trigger not in AUTOMATIC_TRIGGERS
            or not _SAFE_KEY.match(key)):
        return False
    if not ((artifact_root and cycle_id) or route):
        return False
    if artifact_root and not (Path(artifact_root) / CUTOVER_REL).is_file():
        return False
    if not _claim(key, child_env, time.time() if now is None else now):
        return False
    argv = [sys.executable, str(PRODUCER), "checkpoint", "--trigger", trigger]
    if artifact_root:
        argv += ["--artifact-root", str(artifact_root)]
    if cycle_id:
        argv += ["--cycle", cycle_id]
    else:
        argv += ["--route", str(route)]
    try:
        end_with_test(subprocess.Popen(
            argv, cwd=str(UTILITIES), env=child_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        ))
    except OSError:
        return False
    return True


def launch_for_route(route: object, *, trigger: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """`route` is a sealed route mapping (capability-route, workflow-supervisor)."""
    try:
        if not isinstance(route, Mapping):
            return False
        root, route_id = route.get("artifact_root"), route.get("route_id")
        if not isinstance(root, str) or not isinstance(route_id, str) or not root or not route_id:
            return False
        return launch(trigger=trigger, key=f"route-{route_id}-{trigger}", artifact_root=root,
                      route=route_id, env=env)
    except Exception:  # noqa: BLE001 -- a trigger never fails its caller
        return False


def session_route(harness: str, session_id: str) -> Optional[Tuple[str, str]]:
    """(artifact_root, route_file) of the session's latest route-chain line."""
    tools = UTILITIES.parent / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    from fleet import route_chain  # noqa: E402

    lines = route_chain.own_lines(route_chain.read_tail(harness, session_id))
    if not lines:
        return None
    last = max(lines, key=lambda line: line.get("ts") or 0)
    root, route_file = last.get("artifact_root"), last.get("route_file")
    if not isinstance(root, str) or not isinstance(route_file, str) or not root or not route_file:
        return None
    return root, route_file


def launch_for_session(harness: str, session_id: str, *, env: Optional[Mapping[str, str]] = None) -> bool:
    """Turn-end trigger: a dispatched worker names its cycle in its environment;
    an interactive session is resolved through its route-chain ledger."""
    try:
        env = os.environ if env is None else env
        if disabled(env) or harness not in HARNESSES:
            return False
        root, cycle_id = env.get("AGENT_ARTIFACT_ROOT"), env.get("AGENT_ARTIFACT_CYCLE_ID")
        if root and cycle_id:
            return launch(trigger="turn-end", key=f"cycle-{cycle_id}", artifact_root=root,
                          cycle_id=cycle_id, env=env)
        if not session_id or not _SAFE_KEY.match(session_id):
            return False
        found = session_route(harness, session_id)
        if found is None:
            return False
        return launch(trigger="turn-end", key=f"session-{harness}-{session_id}",
                      artifact_root=found[0], route=found[1], env=env)
    except Exception:  # noqa: BLE001 -- a trigger never fails its caller
        return False


def _hook_session_id(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("session_id", "sessionID", "thread_id", "threadID"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def main(argv: Optional[list] = None) -> int:
    """`turn-end --harness H`: read a hook payload on stdin; always silent, exit 0."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("event", choices=["turn-end"])
    parser.add_argument("--harness", choices=HARNESSES, required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        payload = {}
    launch_for_session(args.harness, _hook_session_id(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
