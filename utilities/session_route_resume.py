#!/usr/bin/env python3
"""A reopened session takes over the routes it left open, with no hand command.

`session_resume.register` calls `request` once per resume generation. A detached
`run` then does what a person did by hand for each still-open route of this
session: `capability-route.py start --route <file>`. Every existing gate (verify,
route-chain record, start receipt, human-gate) runs unchanged; nothing here
decides ownership. Failures are recorded for diagnosis and never reach the
resumed session or the existing notice flow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types

try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test
except ImportError:  # a partial tree without it: no record
    def end_with_test(child):
        return child

HARNESSES = ("claude", "codex", "opencode")
EVENT = "route-resume"
START_TIMEOUT = 120
# Markers that make a child look like a dispatched worker or another harness's session.
_WORKER_ENV = ("AGENT_DISPATCH_DEPTH", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_CURRENT_HARNESS",
               "AGENT_SESSION_ROLE")


def _utilities():
    return Path(__file__).resolve().parent


def _tools():
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)


def open_routes(harness, sid):
    """Unique route files of this session's ledger, oldest first, that have no outcome yet.

    Every ledger line counts whatever its event: a route this session composed and
    started has only the compose line (`_record_route_chain` skips a repeated start).
    """
    _tools()
    from fleet import route_chain
    import capability_route_close
    session = types.SimpleNamespace(harness=harness, session_id=sid, session_aliases=[])
    seen, found = set(), []
    for line in route_chain.session_lines(session, full_history=True):
        path = line.get("route_file")
        if not isinstance(path, str) or path in seen:
            continue
        seen.add(path)
        if Path(path).is_file() and not capability_route_close.outcome_path(path).is_file():
            found.append(path)
    return found


def child_env(harness, sid, env=None):
    """The environment a depth-0 `start` needs to anchor on exactly this session."""
    from harness_capabilities import session_env
    declared = session_env()
    child = dict(os.environ if env is None else env)
    for name in _WORKER_ENV:
        child.pop(name, None)
    for names in declared.values():
        for name in names:
            child.pop(name, None)
    child[declared[harness][0]] = sid
    child["AGENT_DISPATCH_CALLER_HARNESS"] = harness
    return child


def request(harness, sid, *, resume, env=None):
    """Spawn the detached takeover once per resume generation. Never raises.

    `env` is the caller's marker environment, already checked by `register`
    (`tidy.is_worker`); the child starts from this process's own environment.
    """
    try:
        if harness not in HARNESSES or not isinstance(sid, str) or not sid:
            return False
        if not open_routes(harness, sid):
            return False
        import session_tidy as tidy
        import session_resume
        seat = session_resume._seat(harness, sid)
        with tidy.session_state_lock(seat):
            _seat_key, _raw, rows = session_resume._ledger(harness, sid)
            if any(r.get("event") == EVENT and (r.get("harness"), r.get("sid")) == (harness, sid)
                   and r.get("session_seq") == resume.get("session_seq") for r in rows):
                return False
            tidy._append_ledger_entry(seat, {"event": EVENT, "harness": harness, "sid": sid,
                                             "ts": resume.get("ts"), "session_seq": resume.get("session_seq")})
            end_with_test(subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "run", "--harness", harness, "--sid", sid],
                env=child_env(harness, sid), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True))
        return True
    except Exception:
        return False


def _result_path(harness, sid):
    import session_tidy as tidy
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]
    return tidy.state_root() / "route-resume" / f"{harness}-{digest}.json"


def _record(harness, sid, results):
    """Diagnostic only; nothing reads it back."""
    import session_tidy as tidy
    path = _result_path(harness, sid)
    tidy.ensure_dir(path.parent)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"harness": harness, "routes": results}, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _start_one(route_file):
    command = [sys.executable, str(_utilities() / "capability-route.py"), "start", "--route", route_file]
    entry = {"route_file": route_file}
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=START_TIMEOUT)
        entry["returncode"] = done.returncode
        entry["state"] = "started" if done.returncode == 0 else "refused"
        if done.returncode != 0:
            entry["reason"] = (done.stderr or done.stdout or "").strip()[-300:]
    except subprocess.TimeoutExpired:
        entry.update(returncode=None, state="timeout", reason=f"start exceeded {START_TIMEOUT}s")
    except OSError as exc:
        entry.update(returncode=None, state="error", reason=str(exc)[:300])
    return entry


def run(harness, sid):
    """Start each open route, oldest first, so the newest receipt is saved last."""
    routes = open_routes(harness, sid)
    if not routes:
        return []
    results = []
    for route_file in routes:
        entry = _start_one(route_file)
        try:
            entry["route_id"] = json.loads(Path(route_file).read_text()).get("route_id")
        except (OSError, ValueError, AttributeError):
            entry["route_id"] = None
        results.append(entry)
    try:
        _record(harness, sid, results)
    except Exception:
        pass
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--harness", required=True, choices=HARNESSES)
    runner.add_argument("--sid", required=True)
    args = parser.parse_args(argv)
    try:
        run(args.harness, args.sid)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(_utilities()))
    raise SystemExit(main())
