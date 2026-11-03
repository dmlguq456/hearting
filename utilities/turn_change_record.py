#!/usr/bin/env python3
"""Automatic change record: a turn that changes source files without a route leaves one.

Whether a small change left a work record used to depend on the tool the model picked:
an edit tool met the route presence gate (``route_presence_gate.py``) and the model
composed a route, while a shell edit (``sed -i``, a redirection) finished in place with
no record. This records such changes itself, whatever tool made them.

Turn start (Claude and Codex ``UserPromptSubmit``, OpenCode ``chat.message``) notes the
session's git work tree: HEAD and, for every path ``git status`` lists, its status, size
and mtime. Turn end (Claude and Codex ``Stop``, OpenCode ``session.idle``) notes it
again. Paths whose entry changed, plus the files of non-merge commits made during the
turn, are the turn's changes; edits made between turns are not. Artifact roots, the
temp directory and runtime homes are left out exactly as the gate leaves them out.

When the session's route-chain ledger names no route for that folder -- the gate's own
question, answered by the gate's functions -- and the turn changed source paths, the end
step runs the two commands an agent runs for a change already made in place:
``capability-route.py compose --shape direct`` (no ``--start``, so no worktree) and
``finish`` with the changed files and a diff summary as evidence. The result is an
ordinary direct route, closed as completed and shown in Fleet like any other; its
tracked gate values read ``auto-record: ...``. Its compose line puts the folder in the
session's ledger, so a session is recorded once per folder, as the gate asks once.

Hooks only start a detached child (``launch``) and return: nothing blocks, prompts or
prints. A failed record keeps the turn's baseline and is tried again at the next turn
end, at most ``MAX_ATTEMPTS`` times. Changes another process made in the same work tree
during the turn cannot be told apart from the session's own; the evidence names the
other Hearting sessions whose turns overlapped there instead of guessing. A turn the
runtime ends without its end event (an interrupt) is not compared.

``HEARTING_AUTO_RECORD=off`` turns it off. Registered owners and workers, CI and dev
activation are left alone, as by the gate.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILITIES = ROOT / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

import route_presence_gate as gate  # noqa: E402

SWITCH = "HEARTING_AUTO_RECORD"
MARK = "auto-record"
HARNESSES = ("claude", "codex", "opencode")
PHASES = ("start", "end")
SCHEMA = 1
MAX_ENTRIES = 5000
MAX_ATTEMPTS = 3
MAX_LISTED = 200
EVIDENCE_LIMIT = 64 * 1024
OPEN_TURN_SECONDS = 6 * 3600
KEEP_SECONDS = 14 * 86400
END_LOCK_WAIT = 10.0
GIT_TIMEOUT = 30
COMPOSE_TIMEOUT = 600
FINISH_TIMEOUT = 300
CLOCK_SLACK = 2.0
GATE_VALUE = (f"{MARK}: changes observed at turn end and recorded after the fact; "
              "this gate was not assessed")
_SAFE_SID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_NESTED = ("context", "workspace", "session", "payload", "event", "input", "data", "properties")


# ---------------------------------------------------------------------- launch


def _off(env) -> bool:
    return str(env.get(SWITCH, "")).strip().lower() in {"off", "0", "false", "no", "disabled"}


def _field(payload, *keys: str) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for name in _NESTED:
        value = _field(payload.get(name), *keys)
        if value:
            return value
    session = payload.get("session")
    if isinstance(session, dict) and isinstance(session.get("id"), str) and "session_id" in keys:
        return session["id"]
    return ""


def launch(phase: str, harness: str, payload, *, environ=None, popen=None) -> bool:
    """Start the detached turn step and return at once; False when nothing started."""
    env = os.environ if environ is None else environ
    try:
        if phase not in PHASES or harness not in HARNESSES or not isinstance(payload, dict):
            return False
        if popen is None and ("unittest" in sys.modules or "pytest" in sys.modules):
            return False
        if _off(env) or gate.exempt_session(env) or str(env.get("AGENT_SESSION_ROLE", "")).lower() in {
                "owner", "worker", "auxiliary", "reviewer"} or env.get("FLEET_TITLE_REFRESH") == "1":
            return False
        sid = _field(payload, "session_id", "sessionID", "thread_id", "threadID")
        if not _SAFE_SID.match(sid):
            return False
        cwd = _field(payload, "cwd", "working_directory", "workingDirectory") or os.getcwd()
        command = [sys.executable, str(Path(__file__).resolve()), phase, "--harness", harness,
                   "--session", sid, "--cwd", cwd]
        spawn = subprocess.Popen if popen is None else popen
        spawn(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
              cwd=str(ROOT), env=dict(env))
        return True
    except Exception:  # noqa: BLE001 -- a turn never depends on its record
        return False


# ----------------------------------------------------------------------- state


def state_dir(env) -> Path:
    base = env.get("XDG_STATE_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return Path(base) / "hearting" / "turn-changes"


def state_path(env, harness: str, sid: str) -> Path:
    return state_dir(env) / harness / f"{sid}.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("v") == SCHEMA else {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".turn-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({**data, "v": SCHEMA}, stream, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _prune(directory: Path, now: float) -> None:
    with contextlib.suppress(OSError):
        for entry in os.scandir(directory):
            with contextlib.suppress(OSError):
                if now - entry.stat().st_mtime <= KEEP_SECONDS:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    os.unlink(entry.path)


@contextlib.contextmanager
def _locked(path: Path, wait: float):
    """Hold this session's lock, waiting at most ``wait`` seconds; yields False when busy."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.2)
        yield True
    finally:
        os.close(fd)


# ------------------------------------------------------------------------- git


def _git(top: Path, args, env, *, text=True):
    return subprocess.run(["git", "-C", str(top), *args], capture_output=True, text=text,
                          timeout=GIT_TIMEOUT, check=False,
                          env={**env, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})


def snapshot(top: Path, env):
    """``{"head", "entries"}`` of work tree ``top``; ``entries`` is None when too large.
    None when git cannot answer."""
    head = _git(top, ["rev-parse", "--verify", "-q", "HEAD"], env)
    status = _git(top, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
                        "--ignore-submodules=dirty"], env, text=False)
    if status.returncode != 0:
        return None
    entries = {}
    for record in status.stdout.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            return None
        if len(entries) >= MAX_ENTRIES:
            return {"head": head.stdout.strip(), "entries": None}
        relative = os.fsdecode(record[3:])
        code = record[:2].decode("ascii", "replace")
        try:
            meta = os.lstat(top / relative)
            entries[relative] = [code, meta.st_size, meta.st_mtime_ns]
        except OSError:
            entries[relative] = [code, -1, 0]
    return {"head": head.stdout.strip() if head.returncode == 0 else "", "entries": entries}


def _turn_commits(top: Path, before: str, after: str, began: float, env):
    """``(commits, files)`` for non-merge commits made during the turn on top of ``before``.
    A pull brings older commits and merges; those are not the turn's own work."""
    if not before or not after or before == after:
        return [], set()
    if _git(top, ["merge-base", "--is-ancestor", before, after], env).returncode != 0:
        return [], set()
    log = _git(top, ["log", "--no-merges", "--format=%H%x09%ct%x09%s", f"{before}..{after}"], env)
    commits, files = [], set()
    for line in log.stdout.splitlines()[:50]:
        commit, _, rest = line.partition("\t")
        stamp, _, subject = rest.partition("\t")
        try:
            if float(stamp) < began - CLOCK_SLACK:
                continue
        except ValueError:
            continue
        commits.append((commit, subject))
        names = _git(top, ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit], env,
                     text=False)
        files.update(os.fsdecode(name) for name in names.stdout.split(b"\0") if name)
    return commits, files


def changes(before: dict, after: dict, top: Path, root: str, began: float, env):
    """``(source_paths, commits)``: what the turn changed, without artifact or runtime paths."""
    prev, cur = before.get("entries") or {}, after.get("entries") or {}
    paths = {path for path in set(prev) | set(cur) if prev.get(path) != cur.get(path)}
    commits, committed = _turn_commits(top, before.get("head", ""), after.get("head", ""), began, env)
    paths |= committed
    kept = []
    for relative in sorted(paths):
        path = Path(os.path.realpath(top / relative))
        if gate._excluded(path, env) or gate._artifact_path(path, top, root):
            continue
        kept.append(relative)
    return kept, commits


# --------------------------------------------------------------------- record


def _iso(stamp) -> str:
    try:
        return datetime.fromtimestamp(float(stamp), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def concurrent_sessions(env, harness: str, sid: str, top: str, began: float, ended: float):
    """Other sessions whose turns overlapped this one in the same work tree."""
    found = []
    for other in HARNESSES:
        directory = state_dir(env) / other
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".json") or (other, entry.name[:-5]) == (harness, sid):
                continue
            try:
                if entry.stat().st_mtime < began - OPEN_TURN_SECONDS:
                    continue
            except OSError:
                continue
            state = _read(Path(entry.path))
            start, end = state.get("turn_started_at"), state.get("turn_ended_at")
            if state.get("top") != top or not isinstance(start, (int, float)):
                continue
            if end is None and ended - start > OPEN_TURN_SECONDS:
                continue
            if start <= ended and (end is None or end >= began):
                found.append(f"{other}:{entry.name[:-5]}")
    return sorted(found)


def _bounded(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= EVIDENCE_LIMIT:
        return text
    return raw[:EVIDENCE_LIMIT].decode("utf-8", "ignore") + "\n… (truncated)\n"


def evidence_text(harness, sid, top: Path, source, commits, before, after, began, ended,
                  overlap, env) -> str:
    prev, cur = before.get("entries") or {}, after.get("entries") or {}
    listed = source[:MAX_LISTED]
    rows = []
    for path in listed:
        code = (cur.get(path) or [""])[0].strip() or ("committed" if path not in prev else "clean")
        rows.append(f"{code:<9} {path}")
    earlier = [path for path in listed if path in prev]
    lines = ["# Automatic change record", "",
             "Hearting recorded these changes because this session changed files during a turn",
             "while it held no route for this folder. No agent step was asked for.", "",
             f"- harness: {harness}", f"- session: {sid}", f"- work tree: {top}",
             f"- turn: {_iso(began)} to {_iso(ended)}",
             "- attribution: changes observed in this work tree between the turn's start and end"]
    if overlap:
        lines.append("- concurrent sessions in this work tree: " + ", ".join(overlap)
                     + " (some of these changes may be theirs)")
    else:
        lines.append("- concurrent sessions in this work tree: none observed")
    if earlier:
        lines.append("- already changed before the turn (the diff below is against HEAD and "
                     "includes those earlier edits): " + ", ".join(earlier[:20]))
    lines += ["", f"## Changed files ({len(source)})", "", "```", *rows]
    if len(source) > len(listed):
        lines.append(f"… and {len(source) - len(listed)} more")
    lines += ["```", ""]
    head = after.get("head")
    if head:
        stat = _git(top, ["diff", "--stat=160", "--no-color", "HEAD", "--",
                          *(f":(literal){path}" for path in listed)], env)
        if stat.returncode == 0 and stat.stdout.strip():
            lines += ["## Diff summary (against HEAD)", "", "```", stat.stdout.rstrip(), "```", ""]
    new_files = [path for path in listed if (cur.get(path) or [""])[0] == "??"]
    if new_files:
        lines += ["## New untracked files", "", "```"]
        lines += [f"{max((cur.get(path) or ['', 0])[1], 0):>9} bytes  {path}" for path in new_files]
        lines += ["```", ""]
    if commits:
        lines += ["## Commits made during the turn", "", "```"]
        lines += [f"{commit[:12]}  {subject}" for commit, subject in commits]
        lines += ["```", ""]
    return _bounded("\n".join(lines))


def _identity_env(env, harness: str, sid: str) -> dict:
    """The hook's environment, naming this session the way its own commands see it."""
    child = {**env, "AGENT_DISPATCH_CALLER_HARNESS": harness}
    try:
        from session_identity import session_env
        for name in session_env().get(harness, ()):
            child[name] = sid
    except Exception:  # noqa: BLE001 -- the caller harness alone still names the session
        pass
    return child


def _last_json(text: str):
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            with contextlib.suppress(ValueError):
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
    return {}


def _route_command(*args) -> list[str]:
    return [sys.executable, str(UTILITIES / "capability-route.py"), *args]


def _settle(route_file: str, work: Path, env) -> dict:
    """Close the composed route: ``finish`` with the evidence, else ``close`` (recorded unproven)."""
    finished = subprocess.run(
        _route_command("finish", "--route", route_file, "--evidence", str(work / "evidence.md"),
                       "--summary-file", str(work / "summary.txt")),
        capture_output=True, text=True, timeout=FINISH_TIMEOUT, check=False, env=env, cwd=str(ROOT))
    if finished.returncode == 0:
        return {"ok": True, "closed_by": "finish"}
    summary = (work / "summary.txt").read_text(encoding="utf-8").strip()
    closed = subprocess.run(_route_command("close", "--route", route_file, "--summary", summary),
                            capture_output=True, text=True, timeout=FINISH_TIMEOUT, check=False,
                            env=env, cwd=str(ROOT))
    if closed.returncode == 0:
        return {"ok": True, "closed_by": "close",
                "finish_refusal": (finished.stderr or finished.stdout).strip()[-300:]}
    return {"ok": False, "reason": "settle-failed",
            "detail": (closed.stderr or closed.stdout or finished.stderr).strip()[-300:]}


def record(harness, sid, top: Path, root: str, source, commits, before, after, began, ended,
           env) -> dict:
    """Compose a direct route in place for the turn's changes and close it as completed."""
    work = state_path(env, harness, sid).with_suffix(".record")
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    overlap = concurrent_sessions(env, harness, sid, str(top), began, ended)
    (work / "evidence.md").write_text(
        evidence_text(harness, sid, top, source, commits, before, after, began, ended, overlap, env),
        encoding="utf-8")
    shown = ", ".join(source[:5]) + (", …" if len(source) > 5 else "")
    (work / "summary.txt").write_text(
        f"Automatic record: {len(source)} file(s) changed during a {harness} turn without a route "
        f"({shown})\n", encoding="utf-8")
    (work / "request.md").write_text(
        f"Intent: Hearting automatic record of the files this {harness} session changed during one "
        f"turn without a route.\nScope: {shown}\n"
        "Done: the changed files and a diff summary are kept as this route's evidence.\n",
        encoding="utf-8")
    child_env = _identity_env(env, harness, sid)
    fresh = [path for path in source if path not in (before.get("entries") or {})]
    slug = gate._slug_word("auto-" + Path((fresh or source)[0]).stem)
    composed = subprocess.run(
        _route_command("compose", "--shape", "direct", "--cwd", str(top),
                       "--campaign-key", gate._campaign_key(root, top), "--slug", slug,
                       "--parent-harness", harness, "--prompt-file", str(work / "request.md"),
                       "--spec-read", GATE_VALUE, "--drift-verdict", GATE_VALUE,
                       "--artifact-guard", GATE_VALUE),
        capture_output=True, text=True, timeout=COMPOSE_TIMEOUT, check=False, env=child_env,
        cwd=str(ROOT))
    route_file = _last_json(composed.stdout).get("route_file")
    if composed.returncode != 0 or not isinstance(route_file, str) or not route_file:
        return {"ok": False, "reason": "compose-failed",
                "detail": (composed.stderr or composed.stdout).strip()[-300:]}
    settled = _settle(route_file, work, child_env)
    if settled.get("closed_by") == "finish":
        shutil.rmtree(work, ignore_errors=True)  # finish kept the evidence in the route's cycle
    return {**settled, "route_file": route_file, "files": len(source), "concurrent": overlap}


# ----------------------------------------------------------------------- turns


def _top(cwd: str, env):
    top = gate._work_tree(Path(os.path.realpath(cwd or "/")))
    if top is None or gate._excluded(Path(os.path.realpath(top)), env):
        return None
    return Path(os.path.realpath(top))


def _unrouted(harness: str, sid: str, top: Path, env) -> str:
    roots = gate.held_roots(harness, sid, env)
    return "" if roots is None else gate.unrouted_root(top, roots, env)


def turn_start(harness: str, sid: str, cwd: str, env, *, now=None) -> dict:
    """Note the work tree as this turn's baseline (git only when no route is held there)."""
    now = time.time() if now is None else now
    path = state_path(env, harness, sid)
    with _locked(path, 0.0) as held:
        if not held:
            return {"state": "busy"}
        state = _read(path)
        if not state:
            _prune(path.parent, now)
        top = _top(cwd, env)
        if top is None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return {"state": "no-work-tree"}
        if state.get("retry") and state.get("top") == str(top) and state.get("entries") is not None:
            # A record that failed keeps its baseline for the next attempt.
            _write(path, {**state, "turn_ended_at": None})
            return {"state": "retry-kept"}
        base = {"harness": harness, "session_id": sid, "top": str(top), "turn_started_at": now,
                "turn_ended_at": None, "recorded": state.get("recorded")}
        if not _unrouted(harness, sid, top, env):
            _write(path, {**base, "entries": None})
            return {"state": "route-held"}
        taken = snapshot(top, env)
        _write(path, {**base, "head": (taken or {}).get("head", ""),
                      "entries": (taken or {}).get("entries")})
        return {"state": "baseline" if taken and taken.get("entries") is not None else "unreadable"}


def turn_end(harness: str, sid: str, cwd: str, env, *, now=None, recorder=None) -> dict:
    """Compare with the turn's baseline and record the turn's source changes when unrouted."""
    now = time.time() if now is None else now
    recorder = record if recorder is None else recorder
    path = state_path(env, harness, sid)
    with _locked(path, END_LOCK_WAIT) as held:
        if not held:
            return {"state": "busy"}
        state = _read(path)
        top = Path(state["top"]) if state.get("top") else _top(cwd, env)
        if top is None or not top.is_dir():
            return {"state": "no-work-tree"}
        began = state.get("turn_started_at") if state.get("top") == str(top) else None
        base = {**state, "harness": harness, "session_id": sid, "top": str(top),
                "turn_started_at": began if isinstance(began, (int, float)) else now,
                "turn_ended_at": now}
        root = _unrouted(harness, sid, top, env)
        if not root:
            _write(path, {**base, "entries": None, "retry": 0})
            return {"state": "route-held"}
        after = snapshot(top, env)
        if after is None or after.get("entries") is None:
            _write(path, {**base, "entries": None, "retry": 0})
            return {"state": "unreadable"}
        if not isinstance(began, (int, float)) or state.get("entries") is None:
            # No baseline from this turn's start: this end is the next turn's baseline.
            _write(path, {**base, **after, "retry": 0})
            return {"state": "baseline"}
        source, commits = changes(state, after, top, root, began, env)
        if not source:
            _write(path, {**base, **after, "retry": 0})
            return {"state": "unchanged"}
        try:
            outcome = recorder(harness, sid, top, root, source, commits, state, after, began, now, env)
        except Exception as exc:  # noqa: BLE001 -- kept as a failure for the next attempt
            outcome = {"ok": False, "reason": type(exc).__name__}
        if outcome.get("ok"):
            recorded = {key: outcome.get(key) for key in ("route_file", "closed_by", "files", "concurrent")}
            _write(path, {**base, "entries": None, "retry": 0, "last_failure": None,
                          "recorded": {**recorded, "at": now}})
            return {"state": "recorded", **outcome}
        attempts = int(state.get("retry") or 0) + 1
        failure = {"reason": outcome.get("reason"), "detail": outcome.get("detail"), "at": now}
        if attempts >= MAX_ATTEMPTS:
            _write(path, {**base, **after, "retry": 0, "last_failure": {**failure, "gave_up": True}})
            return {"state": "gave-up", **outcome}
        _write(path, {**base, "retry": attempts, "last_failure": failure})
        return {"state": "retry", **outcome}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("--harness", choices=HARNESSES, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    args = parser.parse_args(argv)
    if not _SAFE_SID.match(args.session) or _off(os.environ):
        return 0
    try:
        step = turn_start if args.phase == "start" else turn_end
        step(args.harness, args.session, args.cwd, os.environ)
    except Exception:  # noqa: BLE001 -- detached and silent: a failure leaves only its state
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
