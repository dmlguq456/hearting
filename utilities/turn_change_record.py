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
turn, are the turn's changes. A turn is compared only against its own start snapshot:
when that snapshot is missing (the start step did not run, a turn the runtime woke
without a prompt), the turn is not attributed, so edits made between turns never are.
Artifact roots, the temp directory and runtime homes are left out exactly as the gate
leaves them out. Generated noise is what git does not track and ignores (it never
appears in ``git status``) plus untracked ``*.pyc``/``__pycache__``; a tracked file
counts wherever it lives.

The unit is one automatic record per session and folder. When the session's route-chain
ledger names no route for the folder -- the gate's own question, answered by the gate's
functions -- the first changing turn runs the two commands an agent runs for a change
already made in place: ``capability-route.py compose --shape direct --unassigned`` (no
``--start``, so no worktree; the unassigned container, so no unrelated campaign and no
campaign default for the session's next compose) and ``finish`` with the changed files
and a diff summary as evidence. The result is an ordinary direct route, closed as
completed and shown in Fleet like any other; its tracked gate values read
``auto-record: ...``. While that record is the session's only route for the folder,
each later changing turn appends its section to ``artifacts/auto-record-turns.md`` in
the same cycle (a closed cycle's changed file, refreshed through the producer's own
checkpoint). A record ``finish`` refused is closed with ``close`` and has no cycle; its
evidence and later turns go to ``.runtime/auto-record/<route id>.md`` beside the route
instead. Once the session composes a route of its own there, its changes belong to that
route. The route-chain line of an automatic record is marked ``automatic``, so Fleet's
per-session chain and the session's latest-route readers keep showing the session's own
routes. The record's child runs with the background cycle review and campaign
titling off, and that review leaves automatic records out, so no model is called.

Hooks only start a detached child (``launch``) and return: nothing blocks, prompts or
prints. Two locks keep a slow record from dropping a turn: the session's state lock is
held only to snapshot and to read or write the state, while composing, closing and
appending run under a separate record lock, so the next turn's start never finds the
session busy for the length of a record, and a turn that ends while a record is running
is queued and written by the same or the next record pass. A queued turn whose record
step died (the session ended) is written at the session's next boundary or, once the
session is proven gone (``session_ended``) and quiet for ``ORPHAN_AFTER``, by the turn-end
step of any session in the same folder, for that folder only and as that session; no
process is added for it. A created route is kept in
the state file until it is closed; a failed close is tried again at every later turn
boundary, so an automatic route never stays open as the session's default. A turn whose record could not be written keeps its own section and is
written at a later boundary, at most ``MAX_ATTEMPTS`` times. Changes another process made
in the same work tree during the turn cannot be told apart from the session's own; the
evidence names the other Hearting sessions whose turns overlapped there instead of
guessing. A turn the runtime ends without its end event (an interrupt) is not compared.
Each hook passes its own event time (``--at``): when the next turn's start step runs before
the previous turn's end step (a prompt queued or injected at once), that late end never
closes the newer turn; it compares its own turn against the start snapshot kept from it,
and the newer turn's start snapshot stands in for its end.

``HEARTING_AUTO_RECORD=off`` turns it off. Registered owners and workers, CI and dev
activation are left alone, as by the gate (``skipped``).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
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
SCHEMA = 2
MAX_ENTRIES = 5000
MAX_ATTEMPTS = 3
MAX_CLOSE_ATTEMPTS = 10
MAX_PENDING = 20
MAX_LISTED = 200
EVIDENCE_LIMIT = 64 * 1024
OPEN_TURN_SECONDS = 6 * 3600
KEEP_SECONDS = 14 * 86400
STATE_LOCK_WAIT = 30.0
RECORD_LOCK_WAIT = 1800.0
MAX_ROUNDS = 10
# A queued turn of a session proven gone (its record step died with it) is left this long
# after the session's last state change before a turn end in the same folder writes it.
ORPHAN_AFTER = 120.0
GIT_TIMEOUT = 30
COMPOSE_TIMEOUT = 600
FINISH_TIMEOUT = 300
CHECKPOINT_TIMEOUT = 120
CLOCK_SLACK = 2.0
APPENDED = "auto-record-turns.md"
# The open turn's start snapshot kept at the next start for its late end step.
PRIOR_KEYS = ("turn_started_at", "top", "root", "head", "entries")
GATE_VALUE = (f"{MARK}: changes observed at turn end and recorded after the fact; "
              "this gate was not assessed")
# Byte code Python writes beside sources; untracked, it is never a reason for a record.
# Everything else that is generated is left to git: ignored files never reach `git status`.
NOISE_SUFFIXES = (".pyc", ".pyo")
# The record's own commands start no model: the background cycle review and campaign titling.
QUIET_ENV = {"HEARTING_WORKFLOW_GROUP_REVIEW": "off", "HEARTING_CAMPAIGN_TITLE_AUTO": "off"}
_SAFE_SID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_NESTED = ("context", "workspace", "session", "payload", "event", "input", "data", "properties")


# ---------------------------------------------------------------------- launch


def _off(env) -> bool:
    return str(env.get(SWITCH, "")).strip().lower() in {"off", "0", "false", "no", "disabled"}


def skipped(env) -> bool:
    """The one exemption judgment, for the hook launcher and the child alike: the switch,
    registered owners and workers, CI (the gate's ``exempt_session``) and helper roles."""
    return (_off(env) or gate.exempt_session(env)
            or str(env.get("AGENT_SESSION_ROLE", "")).lower() in {"owner", "worker", "auxiliary", "reviewer"}
            or env.get("FLEET_TITLE_REFRESH") == "1")


def is_automatic_route(route) -> bool:
    """Whether a route mapping is an automatic change record (its tracked gate values)."""
    try:
        source = route["tracked_gate_evidence"]["artifact_guard"]["source"]
    except (KeyError, TypeError):
        return False
    return isinstance(source, str) and source.startswith(MARK + ":")


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
        if skipped(env):
            return False
        sid = _field(payload, "session_id", "sessionID", "thread_id", "threadID")
        if not _SAFE_SID.match(sid):
            return False
        cwd = _field(payload, "cwd", "working_directory", "workingDirectory") or os.getcwd()
        command = [sys.executable, str(Path(__file__).resolve()), phase, "--harness", harness,
                   "--session", sid, "--cwd", cwd, "--at", f"{time.time():.6f}"]
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


def _state_lock(path: Path) -> Path:
    return path.with_suffix(".lock")


def _record_lock(path: Path) -> Path:
    return path.parent / (path.stem + ".record.lock")


@contextlib.contextmanager
def _locked(lock: Path, wait: float):
    """Hold ``lock``, waiting at most ``wait`` seconds; yields False when it stays busy."""
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
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
        # flock never changes mtime: mark the use, so pruning by age (``_prune``) never
        # removes a lock some long-lived session is holding.
        with contextlib.suppress(OSError):
            os.utime(fd)
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
    try:
        head = _git(top, ["rev-parse", "--verify", "-q", "HEAD"], env)
        status = _git(top, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
                            "--ignore-submodules=dirty"], env, text=False)
    except (OSError, subprocess.SubprocessError):
        return None
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


def _noise(relative: str, code: str) -> bool:
    """Untracked Python byte code; a tracked file is never noise."""
    return code == "??" and (relative.endswith(NOISE_SUFFIXES) or "__pycache__" in relative.split("/"))


def changes(before: dict, after: dict, top: Path, root: str, began: float, env):
    """``(source_paths, commits)``: what the turn changed, without artifact, runtime or
    generated paths."""
    prev, cur = before.get("entries") or {}, after.get("entries") or {}
    paths = {path for path in set(prev) | set(cur) if prev.get(path) != cur.get(path)}
    commits, committed = _turn_commits(top, before.get("head", ""), after.get("head", ""), began, env)
    paths |= committed
    kept = []
    for relative in sorted(paths):
        path = Path(os.path.realpath(top / relative))
        code = (cur.get(relative) or prev.get(relative) or [""])[0]
        if (relative not in committed and _noise(relative, code)) or gate._excluded(path, env) \
                or gate._artifact_path(path, top, root):
            continue
        kept.append(relative)
    return kept, commits


# --------------------------------------------------------------------- evidence


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


def _bounded(text: str, limit: int = EVIDENCE_LIMIT) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore") + "\n… (truncated)\n"


def record_header(harness: str, sid: str, top: Path) -> str:
    return "\n".join([
        "# Automatic change record", "",
        "Hearting recorded these changes because this session changed files during a turn",
        "while it held no route for this folder. No agent step was asked for. Later turns of",
        f"this session in this folder are appended to `artifacts/{APPENDED}` in this cycle.", "",
        f"- harness: {harness}", f"- session: {sid}", f"- work tree: {top}",
        "- attribution: changes observed in this work tree between each turn's own start and end",
        "", ""])


def turn_section(harness, sid, top: Path, source, commits, before, after, began, ended,
                 overlap, env) -> str:
    prev, cur = before.get("entries") or {}, after.get("entries") or {}
    listed = source[:MAX_LISTED]
    rows = []
    for path in listed:
        code = (cur.get(path) or [""])[0].strip() or ("committed" if path not in prev else "clean")
        rows.append(f"{code:<9} {path}")
    earlier = [path for path in listed if path in prev]
    lines = [f"## Turn {_iso(began)} to {_iso(ended)}", ""]
    if overlap:
        lines.append("- concurrent sessions in this work tree: " + ", ".join(overlap)
                     + " (some of these changes may be theirs)")
    else:
        lines.append("- concurrent sessions in this work tree: none observed")
    if earlier:
        lines.append("- already changed before the turn (the diff below is against HEAD and "
                     "includes those earlier edits): " + ", ".join(earlier[:20]))
    lines += ["", f"### Changed files ({len(source)})", "", "```", *rows]
    if len(source) > len(listed):
        lines.append(f"… and {len(source) - len(listed)} more")
    lines += ["```", ""]
    if after.get("head"):
        try:
            stat = _git(top, ["diff", "--stat=160", "--no-color", "HEAD", "--",
                              *(f":(literal){path}" for path in listed)], env)
        except (OSError, subprocess.SubprocessError):
            stat = None
        if stat is not None and stat.returncode == 0 and stat.stdout.strip():
            lines += ["### Diff summary (against HEAD)", "", "```", stat.stdout.rstrip(), "```", ""]
    new_files = [path for path in listed if (cur.get(path) or [""])[0] == "??"]
    if new_files:
        lines += ["### New untracked files", "", "```"]
        lines += [f"{max((cur.get(path) or ['', 0])[1], 0):>9} bytes  {path}" for path in new_files]
        lines += ["```", ""]
    if commits:
        lines += ["### Commits made during the turn", "", "```"]
        lines += [f"{commit[:12]}  {subject}" for commit, subject in commits]
        lines += ["```", ""]
    return _bounded("\n".join(lines), EVIDENCE_LIMIT // 2)


# --------------------------------------------------------------------- commands


def _child_env(env, harness: str, sid: str) -> dict:
    """The hook's environment, naming this session the way its own commands see it, with
    the record's background model triggers off."""
    child = {**env, "AGENT_DISPATCH_CALLER_HARNESS": harness, **QUIET_ENV}
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


def _run(args, env, timeout):
    try:
        return subprocess.run([sys.executable, *args], capture_output=True, text=True,
                              timeout=timeout, check=False, env=env, cwd=str(ROOT))
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(exc.cmd, 124, "", f"timeout after {timeout} s")
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, "", type(exc).__name__)


def _route_command(*args) -> list[str]:
    return [str(UTILITIES / "capability-route.py"), *args]


def _outcome_file(route_file: str) -> Path:
    return Path(route_file).with_suffix(".outcome.json")


def kept_record(route_file: str) -> Path:
    """Where a record without a cycle (closed by ``close``) keeps its evidence and later turns."""
    route = Path(route_file)
    return route.parent.parent / "auto-record" / f"{route.stem}.md"


def _keep(route_file: str, text: str) -> None:
    target = kept_record(route_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as stream:
        stream.write(text)


def _work(env, harness: str, sid: str, root: str) -> Path:
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]
    return state_path(env, harness, sid).with_suffix(f".{digest}.record")


def settle(route_file: str, work: Path, env) -> dict:
    """Close the composed route: ``finish`` with the evidence, else ``close`` (recorded
    unproven). Nothing here raises; ``closed`` says whether it is closed now."""
    if _outcome_file(route_file).is_file():
        return {"closed": True, "closed_by": "already"}
    evidence, summary = work / "evidence.md", work / "summary.txt"
    finished = None
    if evidence.is_file() and summary.is_file():
        finished = _run(_route_command("finish", "--route", route_file, "--evidence", str(evidence),
                                       "--summary-file", str(summary)), env, FINISH_TIMEOUT)
        if finished.returncode == 0:
            return {"closed": True, "closed_by": "finish",
                    "cycle_id": _last_json(finished.stdout).get("cycle_id")}
    try:
        line = summary.read_text(encoding="utf-8").strip() or "Automatic record"
    except OSError:
        line = "Automatic record"
    closed = _run(_route_command("close", "--route", route_file, "--summary", line), env, FINISH_TIMEOUT)
    if closed.returncode == 0 or _outcome_file(route_file).is_file():
        # No cycle took the evidence: keep it beside the route, where later turns are appended.
        try:
            if not kept_record(route_file).exists():
                _keep(route_file, evidence.read_text(encoding="utf-8") + "\n")
        except OSError:
            pass
        return {"closed": True, "closed_by": "close", "kept": str(kept_record(route_file)),
                "finish_refusal": ((finished.stderr or finished.stdout) if finished else "").strip()[-300:]}
    return {"closed": False, "reason": "settle-failed",
            "detail": (closed.stderr or closed.stdout or (finished.stderr if finished else "")).strip()[-300:]}


def record(harness, sid, top: Path, root: str, items, env, remember=None) -> dict:
    """Compose the session's automatic direct route for this folder in place and close it,
    with the turns' sections as evidence. ``remember(route_file)`` is called as soon as the
    route exists, before it is closed, so a close that fails or never runs is kept."""
    work = _work(env, harness, sid, root)
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = list(dict.fromkeys(path for item in items for path in item.get("source", [])))
    (work / "evidence.md").write_text(_bounded(
        record_header(harness, sid, top) + "\n".join(item["section"] for item in items)), encoding="utf-8")
    shown = ", ".join(source[:5]) + (", …" if len(source) > 5 else "")
    (work / "summary.txt").write_text(
        f"Automatic record: {len(source)} file(s) changed during a {harness} turn without a route "
        f"({shown})\n", encoding="utf-8")
    (work / "request.md").write_text(
        f"Intent: Hearting automatic record of the files this {harness} session changed in this "
        f"folder during turns without a route.\nScope: {shown}\n"
        "Done: the changed files and a diff summary are kept as this route's evidence.\n",
        encoding="utf-8")
    child_env = _child_env(env, harness, sid)
    fresh = [path for item in items for path in item.get("fresh", [])] or source
    slug = gate._slug_word("auto-" + Path(fresh[0]).stem)
    composed = _run(_route_command("compose", "--shape", "direct", "--cwd", str(top), "--unassigned",
                                   "--slug", slug, "--parent-harness", harness,
                                   "--prompt-file", str(work / "request.md"),
                                   "--spec-read", GATE_VALUE, "--drift-verdict", GATE_VALUE,
                                   "--artifact-guard", GATE_VALUE), child_env, COMPOSE_TIMEOUT)
    route_file = _last_json(composed.stdout).get("route_file")
    if not isinstance(route_file, str) or not route_file or not Path(route_file).is_file():
        return {"ok": False, "reason": "compose-failed",
                "detail": (composed.stderr or composed.stdout).strip()[-300:]}
    if remember is not None:
        remember(route_file)
    settled = settle(route_file, work, child_env)
    if settled.get("closed"):
        shutil.rmtree(work, ignore_errors=True)  # the cycle or the kept record holds the evidence
    return {"ok": True, "route_file": route_file, "route_id": Path(route_file).stem, **settled}


def _cycle_dir(root: str, entry: dict):
    import artifact_producer as producer
    cycle_id = entry.get("cycle_id")
    if not cycle_id:
        route = json.loads(Path(entry["route_file"]).read_text(encoding="utf-8"))
        found = producer.route_cycle_for(Path(root), route) or {}
        cycle_id = found.get("cycle_id")
    row = producer.read_cycle_record(Path(root), cycle_id) if cycle_id else None
    if not row:
        return None, None
    return cycle_id, producer.cycle_dir(Path(root), row["campaign_id"], cycle_id, row)


def append(harness, sid, top: Path, root: str, entry: dict, items, env) -> dict:
    """Append later turns to the session's closed automatic record for this folder: one
    file in the same cycle, then the producer's own checkpoint publishes it; a record
    without a cycle (closed by ``close``) keeps them beside its route."""
    sections = "\n".join(item["section"] for item in items) + "\n"
    directory = None
    if entry.get("closed_by") != "close":
        try:
            cycle_id, directory = _cycle_dir(root, entry)
        except Exception:  # noqa: BLE001 -- a record without a readable cycle keeps its turns beside the route
            directory = None
    if directory is None or not directory.is_dir():
        try:
            _keep(entry["route_file"], sections)
        except OSError as exc:
            return {"ok": False, "reason": "kept-record-unwritable", "detail": type(exc).__name__}
        return {"ok": True, "file": str(kept_record(entry["route_file"]))}
    target = directory / "artifacts" / APPENDED
    target.parent.mkdir(parents=True, exist_ok=True)
    head = "" if target.exists() else (record_header(harness, sid, top).split("\n\n", 1)[0] + "\n\n"
                                       + f"Later turns of route {entry.get('route_id')}.\n\n")
    with open(target, "a", encoding="utf-8") as stream:
        stream.write(head + "\n".join(item["section"] for item in items) + "\n")
    _run([str(UTILITIES / "artifact_producer.py"), "checkpoint", "--trigger", "explicit",
          "--artifact-root", root, "--cycle", cycle_id], _child_env(env, harness, sid), CHECKPOINT_TIMEOUT)
    return {"ok": True, "cycle_id": cycle_id, "file": str(target)}


# ----------------------------------------------------------------------- turns


def _top(cwd: str, env):
    top = gate._work_tree(Path(os.path.realpath(cwd or "/")))
    if top is None or gate._excluded(Path(os.path.realpath(top)), env):
        return None
    return Path(os.path.realpath(top))


def _mode(harness: str, sid: str, top: Path, state: dict, env):
    """``("record", root)`` when the session's only routes for this folder are its own
    automatic record (or none); ``("route-held", root)`` once it holds a route of its own;
    ``("skip", "")`` under dev activation, without an identity or a resolvable root."""
    # The ledger's `automatic` lines are this record's own routes, not the session's: they are
    # left out, so a turn that starts while the record's route is being created still records.
    routes = gate.held_routes(harness, sid, env, own_only=True)
    root = gate.folder_root(top, env) if routes is not None else ""
    if not root:
        return "skip", ""
    automatic = (state.get("records") or {}).get(root, {}).get("route_id")
    return ("record" if routes.get(root, set()) <= {automatic} else "route-held"), root


def _pending_marker(env, root: str, harness: str, sid: str) -> Path:
    """One empty file per session with queued turns for this folder, so a record pass in the
    same folder finds them without reading every session's state."""
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]
    return state_dir(env) / "pending" / digest / f"{harness}--{sid}"


def _mark_pending(env, state: dict, harness: str, sid: str) -> None:
    roots = {row.get("root") for row in state.get("pending") or [] if row.get("root")}
    for root in roots:
        marker = _pending_marker(env, root, harness, sid)
        with contextlib.suppress(OSError):
            marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            marker.touch()
    for root in set(state.get("pending_roots") or []) - roots:
        marker = _pending_marker(env, root, harness, sid)
        with contextlib.suppress(OSError):
            marker.unlink()
        with contextlib.suppress(OSError):
            marker.parent.rmdir()  # only when no other session has turns queued there
    state["pending_roots"] = sorted(roots)


def _prune_markers(env) -> None:
    """Drop markers whose session no longer has turns queued for that folder, and empty
    folders; run when a new session first starts, like the state pruning."""
    base = state_dir(env) / "pending"
    with contextlib.suppress(OSError):
        for folder in os.scandir(base):
            with contextlib.suppress(OSError):
                for marker in os.scandir(folder.path):
                    harness, _, sid = marker.name.partition("--")
                    state = _read(state_path(env, harness, sid)) if harness in HARNESSES and _SAFE_SID.match(sid) else {}
                    digests = {hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]
                               for root in state.get("pending_roots") or []
                               if any(row.get("root") == root for row in state.get("pending") or [])}
                    if folder.name not in digests:
                        os.unlink(marker.path)
                os.rmdir(folder.path)


def _update(path: Path, change, wait=None):
    """Read, change and write the session state under its lock; the changed state, or None
    when the lock stayed busy."""
    with _locked(_state_lock(path), STATE_LOCK_WAIT if wait is None else wait) as held:
        if not held:
            return None
        state = _read(path)
        change(state)
        _write(path, state)
        return state


def _close_pass(records: dict, harness: str, sid: str, env) -> dict:
    """Try to close every automatic route of this session that is still open (H1);
    ``{root: result}`` for the ones tried. Runs outside the state lock."""
    tried = {}
    for root, entry in records.items():
        if entry.get("closed") or int(entry.get("close_attempts") or 0) >= MAX_CLOSE_ATTEMPTS:
            continue
        tried[root] = settle(entry["route_file"], _work(env, harness, sid, root), _child_env(env, harness, sid))
        if tried[root].get("closed"):
            shutil.rmtree(_work(env, harness, sid, root), ignore_errors=True)
    return tried


def _apply_close(state: dict, tried: dict) -> None:
    for root, settled in tried.items():
        entry = (state.get("records") or {}).get(root)
        if entry is None:
            continue
        if settled.get("closed"):
            entry.update(closed=True, closed_by=settled.get("closed_by"))
            if settled.get("cycle_id"):
                entry["cycle_id"] = settled["cycle_id"]
        else:
            entry["close_attempts"] = int(entry.get("close_attempts") or 0) + 1
            entry["last_failure"] = {"reason": settled.get("reason"), "detail": settled.get("detail")}


def _record_pass(path: Path, harness: str, sid: str, env, recorder, appender, now: float, wait: float,
                 *, only_root: str = "", carried: bool = False) -> str:
    """Under the record lock: close open automatic routes and write every queued turn into
    the session's record for its folder, round after round until nothing is queued. A turn
    queued while this runs is taken by a later round, or by the pass its own end starts once
    this one lets go of the lock. ``only_root`` keeps the pass to one folder; a ``carried``
    pass (another session writing an ended session's turns) leaves that session's attempts
    as they were."""
    result = ""
    with _locked(_record_lock(path), wait) as held:
        if not held:
            return "busy"
        for _ in range(MAX_ROUNDS):
            state = _update(path, lambda state: None)
            if state is None:
                break
            records = {root: dict(entry) for root, entry in (state.get("records") or {}).items()
                       if not only_root or root == only_root}
            pending = [item for item in state.get("pending") or [] if not only_root or item.get("root") == only_root]
            tried = _close_pass(records, harness, sid, env)
            if not pending:
                _update(path, lambda state: _apply_close(state, tried))
                break
            outcomes = []
            for root in dict.fromkeys(item["root"] for item in pending):
                items = [item for item in pending if item["root"] == root]
                ids = {item["id"] for item in items}
                top = Path(items[0]["top"])
                entry = records.get(root)
                try:
                    if entry is None:
                        def remember(route_file, root=root, ids=ids, count=len(items)):
                            # The route exists: keep it, and the turns it carries, before it closes.
                            def keep(state):
                                state.setdefault("records", {})[root] = {
                                    "route_file": route_file, "route_id": Path(route_file).stem,
                                    "closed": False, "turns": count, "at": now, "close_attempts": 0}
                                state["pending"] = [row for row in state.get("pending") or []
                                                    if row.get("id") not in ids]
                            _update(path, keep)

                        outcome = recorder(harness, sid, top, root, items, env, remember=remember)
                    else:
                        outcome = appender(harness, sid, top, root, entry, items, env)
                except Exception as exc:  # noqa: BLE001 -- kept for the next pass
                    outcome = {"ok": False, "reason": type(exc).__name__}
                outcomes.append((root, ids, len(items), entry is None, outcome))

            def apply(state):
                nonlocal result
                _apply_close(state, tried)
                records_now = state.setdefault("records", {})
                for root, ids, count, created, outcome in outcomes:
                    if outcome.get("ok"):
                        state["pending"] = [row for row in state.get("pending") or [] if row.get("id") not in ids]
                        if created and outcome.get("route_file"):
                            entry = records_now.setdefault(root, {"route_file": outcome["route_file"], "turns": count,
                                                                  "at": now, "close_attempts": 0})
                            entry.update(route_id=Path(outcome["route_file"]).stem,
                                         closed=bool(outcome.get("closed")),
                                         closed_by=outcome.get("closed_by"), cycle_id=outcome.get("cycle_id"))
                        elif not created and root in records_now:
                            records_now[root]["turns"] = int(records_now[root].get("turns") or 0) + count
                        result = "recorded" if created else "appended"
                    elif carried:
                        state["last_carry_failure"] = {"reason": outcome.get("reason"),
                                                       "detail": outcome.get("detail"), "at": now}
                        result = "retry"
                    else:
                        state["attempts"] = int(state.get("attempts") or 0) + 1
                        state["last_failure"] = {"reason": outcome.get("reason"),
                                                 "detail": outcome.get("detail"), "at": now}
                        result = "retry"
                if not state.get("pending"):
                    state["attempts"] = 0
                elif int(state.get("attempts") or 0) >= MAX_ATTEMPTS:
                    state["last_failure"] = {**(state.get("last_failure") or {}), "gave_up": True,
                                             "dropped_turns": len(state["pending"])}
                    state["pending"], state["attempts"], result = [], 0, "gave-up"
                _mark_pending(env, state, harness, sid)
            _update(path, apply)
            if not any(outcome.get("ok") for *_rest, outcome in outcomes):
                break
    return result


def turn_start(harness: str, sid: str, cwd: str, env, *, now=None, recorder=None, appender=None) -> dict:
    """Take this turn's start snapshot (git only while the session records this folder),
    then close any automatic route still open."""
    now = time.time() if now is None else now
    path = state_path(env, harness, sid)
    result = {"state": "busy"}

    def begin(state):
        if not state:
            _prune(path.parent, now)
            _prune_markers(env)
        # The previous turn is still open: its end step may simply not have run yet (the
        # next prompt came at once). Keep its start snapshot for that late end; an
        # interrupted turn's is dropped at the next start or end.
        if (state.get("turn_ended_at") is None and isinstance(state.get("turn_started_at"), (int, float))
                and state.get("entries") is not None and state.get("root")):
            state["prior_turn"] = {key: state.get(key) for key in PRIOR_KEYS}
        else:
            state.pop("prior_turn", None)
        top = _top(cwd, env)
        state.update(harness=harness, session_id=sid, top=str(top) if top else None,
                     turn_started_at=now, turn_ended_at=None, head="", entries=None, root="")
        result["state"] = "no-work-tree"
        if top is None:
            return
        mode, root = _mode(harness, sid, top, state, env)
        result["state"] = mode
        if mode == "record" and state.get("skip_top") == str(top):
            result["state"] = "too-large"
        elif mode == "record":
            taken = snapshot(top, env)
            if taken is None:
                result["state"] = "unreadable"
            elif taken["entries"] is None:
                state["skip_top"], result["state"] = str(top), "too-large"
            else:
                state.update(head=taken["head"], entries=taken["entries"], root=root)
                result["state"] = "baseline"

    _update(path, begin)
    state = _read(path)
    if state.get("pending") or any(not entry.get("closed") for entry in (state.get("records") or {}).values()):
        # A queued turn whose record step did not run (killed) is written at the next boundary.
        _record_pass(path, harness, sid, env, recorder or record, appender or append, now, 0.0)
    return result


def turn_end(harness: str, sid: str, cwd: str, env, *, now=None, recorder=None, appender=None) -> dict:
    """Compare with this turn's own start snapshot, queue its source changes, then write the
    queue into the session's automatic record for the folder."""
    now = time.time() if now is None else now
    recorder = record if recorder is None else recorder
    appender = append if appender is None else appender
    path = state_path(env, harness, sid)
    result = {"state": "busy"}

    def queue(state, before, after, top, root, began):
        """Queue the source changes between ``before`` and ``after`` as one turn."""
        top_path = Path(top)
        source, commits = changes(before, after, top_path, root, began, env)
        result["state"] = "unchanged"
        if source:
            overlap = concurrent_sessions(env, harness, sid, top, began, now)
            pending = state.setdefault("pending", [])
            pending.append({
                "id": f"{began:.6f}", "root": root, "top": top, "began": began, "ended": now,
                "source": source,
                "fresh": [p for p in source if p not in (before.get("entries") or {})],
                "section": turn_section(harness, sid, top_path, source, commits, before, after,
                                        began, now, overlap, env)})
            del pending[:-MAX_PENDING]
            _mark_pending(env, state, harness, sid)
            result["state"] = "changed"

    def finish_turn(state):
        if not state:
            result["state"] = "no-start-snapshot"
            return
        began, top = state.get("turn_started_at"), state.get("top")
        prior = state.pop("prior_turn", None)
        if isinstance(began, (int, float)) and now < began:
            # This end happened before the open turn began: the next turn's start step ran
            # first. The open turn is not this one's to close. This turn's own start
            # snapshot was kept; the newer turn's start snapshot stands in for its end.
            result["state"] = "late-end"
            if (isinstance(prior, dict) and isinstance(prior.get("turn_started_at"), (int, float))
                    and prior["turn_started_at"] <= now and prior.get("entries") is not None
                    and state.get("entries") is not None and prior.get("top") == top
                    and prior.get("root") == state.get("root") and top and Path(top).is_dir()):
                mode, root = _mode(harness, sid, Path(top), state, env)
                result["root"] = root
                if mode == "record" and root == prior["root"]:
                    queue(state, prior, {"head": state.get("head") or "", "entries": state["entries"]},
                          top, root, prior["turn_started_at"])
            return
        opened = (state.get("turn_ended_at") is None and isinstance(began, (int, float)) and top
                  and Path(top).is_dir())
        result["state"] = "no-start-snapshot"
        if opened:
            top_path = Path(top)
            mode, root = _mode(harness, sid, top_path, state, env)
            result["state"], result["root"] = mode, root
            after = None
            if mode == "record":
                # Only this turn's own start snapshot is compared; without it nothing is attributed.
                has_start = state.get("entries") is not None and root == state.get("root")
                after = snapshot(top_path, env) if has_start else None
                result["state"] = "unreadable" if has_start else "no-start-snapshot"
                if after is not None and after.get("entries") is None:
                    after = None
            if after is not None:
                queue(state, state, after, top, root, began)
        state.update(turn_ended_at=now, entries=None, head="")

    if _update(path, finish_turn) is None:
        return result
    state = _read(path)
    flushed = ""
    if state.get("pending") or any(not entry.get("closed") for entry in (state.get("records") or {}).values()):
        flushed = _record_pass(path, harness, sid, env, recorder, appender, now, RECORD_LOCK_WAIT)
    adopted = _adopt_orphans(result.get("root") or "", harness, sid, env, now, recorder, appender)
    state = _read(path)
    return {"state": flushed if flushed and flushed != "busy" else result["state"],
            "records": state.get("records") or {}, "pending": len(state.get("pending") or []),
            "adopted": adopted}


def session_ended(harness: str, sid: str) -> bool:
    """Proof that a session is gone, from the evidence the runtime already uses for a
    steward's end (``tools/fleet/collectors/steward.session_liveness``: herdr rows, the
    Claude process census, open Codex rollouts, visible OpenCode sessions, the registry
    record's process). Only ``ended`` counts; ``live`` and ``unknown`` do not."""
    try:
        tools = str(ROOT / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.collectors import steward
        return steward.session_liveness(harness, sid) == "ended"
    except Exception:  # noqa: BLE001 -- unknown is not ended
        return False


def _adopt_orphans(root: str, harness: str, sid: str, env, now: float, recorder, appender) -> list:
    """Write other sessions' queued turns for this folder -- this folder only -- when that
    session is proven gone (``session_ended``), its record lock is free and its state has not
    moved for ``ORPHAN_AFTER``. Each is written as that session (its identity, its own
    record), through the same record pass kept to this folder; a session whose pass is
    running keeps its lock and is skipped, and a failed carry leaves its attempts alone. No
    process is added: this runs inside a turn-end step that is already running."""
    adopted = []
    if not root:
        return adopted
    directory = _pending_marker(env, root, harness, sid).parent
    try:
        markers = [entry.name for entry in os.scandir(directory)]
    except OSError:
        return adopted
    for name in markers:
        other_harness, _, other_sid = name.partition("--")
        if (other_harness, other_sid) == (harness, sid) or other_harness not in HARNESSES \
                or not _SAFE_SID.match(other_sid):
            continue
        other = state_path(env, other_harness, other_sid)
        try:
            idle = now - other.stat().st_mtime
        except OSError:
            with contextlib.suppress(OSError):
                (directory / name).unlink()
            continue
        if idle < ORPHAN_AFTER:
            continue
        if not any(row.get("root") == root for row in _read(other).get("pending") or []):
            with contextlib.suppress(OSError):
                (directory / name).unlink()
            continue
        if not session_ended(other_harness, other_sid):
            continue  # idle, alive or unknown: its own next boundary writes it
        outcome = _record_pass(other, other_harness, other_sid, env, recorder, appender, now, 0.0,
                               only_root=root, carried=True)
        adopted.append(f"{other_harness}:{other_sid}:{outcome or 'none'}")
    return adopted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("--harness", choices=HARNESSES, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    # The hook's own event time; the detached steps of two turns may run out of order.
    parser.add_argument("--at", type=float, default=None)
    args = parser.parse_args(argv)
    if not _SAFE_SID.match(args.session) or skipped(os.environ):
        return 0
    at = args.at
    if at is not None and not (0 < at <= time.time() + CLOCK_SLACK):
        at = None
    try:
        step = turn_start if args.phase == "start" else turn_end
        step(args.harness, args.session, args.cwd, os.environ, now=at)
    except Exception:  # noqa: BLE001 -- detached and silent: a failure leaves only its state
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
