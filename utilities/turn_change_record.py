#!/usr/bin/env python3
"""Automatic current-worktree records shared by all three adapters.

Existing detached start/end steps compare each turn's own Git snapshots and
append changed paths, bounded native command outcomes and the final reply to
one automatic direct record per session/folder. COMPLETE means record saved,
not a semantic task verdict. Active explicit workflows retain their procedure;
proven closed historical routes do not suppress a new automatic record.
The existing locks, queues, retry limits and off/worker exemptions apply.
A missing start/end is never invented; late ends retain their own native cursor.
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
try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test  # noqa: E402
except ImportError:  # a partial tree without it (e.g. a fixture release): no record
    def end_with_test(child):
        return child

SWITCH = "HEARTING_AUTO_RECORD"
MARK = "auto-record"
HARNESSES = ("claude", "codex", "opencode")
PHASES = ("start", "end")
SCHEMA = 2
MAX_ENTRIES = 5000
MAX_LISTED = 200
MAX_ATTEMPTS = 3
MAX_CLOSE_ATTEMPTS = 10
MAX_PENDING = 20
EVIDENCE_LIMIT = 64 * 1024
HELD_LIMIT = EVIDENCE_LIMIT * 4
HELD_TRUNCATION = b"# Supplementary history truncated (oldest first; latest observations retained)\n\n"
ENTRY_BYTES = EVIDENCE_LIMIT * 4
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
PRIOR_KEYS = ("turn_started_at", "top", "root", "head", "entries", "native_anchor", "entries_total", "source_total", "entries_truncated", "tail_digest")
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
    payload_fd = None
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
        # Anonymous bounded handoff: no transcript read, pipe wait, or new hook.
        hints = {key: _field(payload, key).encode("utf-8")[:cap].decode("utf-8", "ignore") for key, cap in (
            ("transcript_path", 4096), ("turn_id", 256),
            ("last_assistant_message", 8192), ("hook_event_name", 128)) if _field(payload, key)}
        if 'transcript_path' not in hints and _field(payload, 'transcriptPath'):
            hints['transcript_path'] = _field(payload, 'transcriptPath').encode('utf-8')[:4096].decode('utf-8', 'ignore')
        if payload.get("interrupted"):
            hints["interrupted"] = True
        if hints:
            payload_fd = tempfile.TemporaryFile()
            payload_fd.write(json.dumps(hints, ensure_ascii=False).encode("utf-8"))
            payload_fd.seek(0)
        end_with_test(spawn(command, stdin=payload_fd if payload_fd is not None else subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
              cwd=str(ROOT), env=dict(env)))
        return True
    except Exception:  # noqa: BLE001 -- a turn never depends on its record
        return False
    finally:
        if payload_fd is not None:
            payload_fd.close()


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


def _sample_paths(paths, *, limit=MAX_LISTED, budget=EVIDENCE_LIMIT // 8):
    sampled, used = [], 0
    for path in paths:
        size = len(json.dumps(path, ensure_ascii=True).encode('ascii'))
        if len(sampled) >= limit or used + size > budget:
            break
        sampled.append(path)
        used += size
    return sampled


def _compact_state(state):
    """Keep old v2 payloads readable while bounding every subsequent state write."""
    for snap in (state, state.get('prior_turn') or {}):
        entries = snap.get('entries')
        if not isinstance(entries, dict):
            continue
        total = max(snap.get('entries_total') or 0, len(entries))
        kept, used = {}, 0
        for path, meta in entries.items():
            size = len(json.dumps([path, meta], ensure_ascii=True).encode('ascii'))
            if len(kept) >= MAX_ENTRIES or used + size > ENTRY_BYTES:
                break
            kept[path] = meta
            used += size
        snap.update(entries=kept, entries_total=total, entries_truncated=total-len(kept))
    for item in state.get('pending') or []:
        paths = item.get('source') or []
        item.setdefault('source_count', len(paths))
        item.setdefault('source_known', len(paths))
        item['source'] = _sample_paths(paths)
        item['fresh'] = _sample_paths(item.get('fresh') or [])
        observation = _bounded(item.get('observations') or '', EVIDENCE_LIMIT // 2)
        section = item.get('section') or ''
        if observation and not section.startswith(observation):
            section = observation + '\n' + section
        item['observations'] = observation
        item['section'] = _bounded(section)
    del (state.get('pending') or [])[:-MAX_PENDING]
    return state


def _change_meta(before, after, source):
    tail_unknown = bool(before.get('entries_truncated') or after.get('entries_truncated')) and \
        before.get('tail_digest') != after.get('tail_digest')
    count = len(source)
    if tail_unknown:
        count = None
        if before.get('head') == after.get('head') and before.get('source_total') == 0:
            count = after.get('source_total')
        elif before.get('head') == after.get('head') and after.get('source_total') == 0:
            count = before.get('source_total')
    return {'source_count': count, 'source_known': len(source), 'tail_unknown': tail_unknown,
            'dirty_before': before.get('source_total', len(before.get('entries') or {})),
            'dirty_after': after.get('source_total', len(after.get('entries') or {}))}


def _batch_evidence(items):
    items = sorted(items, key=lambda item: item.get('began') or 0)
    summaries = [f"- Turn {_iso(i.get('began'))}: changed total {i.get('source_count', len(i['source']))}; "
                 f"listed {len(i['source'])}; unknown/truncated tail {bool(i.get('tail_unknown'))}"
                 for i in items]
    # The latest reply remains visible if a backlog cannot fit in one bounded write.
    return _bounded('Turn counts (None = unknown):\n' + '\n'.join(summaries) + '\n\n' +
                    '\n'.join(i['section'] for i in reversed(items)))


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".turn-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({**_compact_state(data), "v": SCHEMA}, stream, ensure_ascii=False, sort_keys=True)
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
    """``{"head", "entries"}`` of work tree ``top``; dirty path counts never classify scope.
    None when git cannot answer."""
    try:
        head = _git(top, ["rev-parse", "--verify", "-q", "HEAD"], env)
        status = _git(top, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
                            "--ignore-submodules=dirty"], env, text=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if status.returncode != 0:
        return None
    entries, total, used = {}, 0, 0
    tail = hashlib.sha256()
    root = gate.folder_root(top, env)
    for record in status.stdout.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            return None
        relative = os.fsdecode(record[3:])
        code = record[:2].decode("ascii", "replace")
        path = Path(os.path.realpath(top / relative))
        if _noise(relative, code) or gate._excluded(path, env, git_project=True) or gate._artifact_path(path, top, root):
            continue
        try:
            meta = os.lstat(top / relative)
            entry = [code, meta.st_size, meta.st_mtime_ns]
        except OSError:
            entry = [code, -1, 0]
        raw = json.dumps([relative, entry], ensure_ascii=True).encode('ascii')
        total += 1
        if len(entries) < MAX_ENTRIES and used + len(raw) <= ENTRY_BYTES:
            entries[relative] = entry
            used += len(raw)
        else:
            tail.update(raw)
    return {"head": head.stdout.strip() if head.returncode == 0 else "", "entries": entries,
            "entries_total": total, "source_total": total, "entries_truncated": total-len(entries),
            "tail_digest": tail.hexdigest()}


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
        if (relative not in committed and _noise(relative, code)) or gate._excluded(path, env, git_project=True) \
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
    marker = "\n… (truncated; use native/Git references)\n".encode('utf-8')
    if limit <= len(marker):
        return marker[:limit].decode('utf-8', 'ignore')
    return raw[:limit-len(marker)].decode('utf-8', 'ignore') + marker.decode('utf-8')


def _append_held(target: Path, text: str) -> None:
    """Under the session state lock, atomically retain a bounded tail, including old logs.
    Read at most the total cap even when a legacy file has grown much larger."""
    addition = (_bounded(text, EVIDENCE_LIMIT - 1) + "\n").encode("utf-8")
    try:
        with target.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - HELD_LIMIT))
            history = stream.read(HELD_LIMIT)
    except FileNotFoundError:
        size, history = 0, b""
    history += addition
    if size + len(addition) > HELD_LIMIT:
        history = history[-(HELD_LIMIT - len(HELD_TRUNCATION)):]
        # Drop the oldest partial line too; UTF-8 and newer observations stay intact.
        history = HELD_TRUNCATION + history.partition(b"\n")[2]
    fd, temporary = tempfile.mkstemp(prefix=".held-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(history)
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _turn_key(state):
    return tuple(state.get(key) for key in ("turn_started_at", "top", "root"))


def record_header(harness: str, sid: str, top: Path) -> str:
    return "\n".join([
        "# Automatic change record", "",
        "Hearting recorded source changes observed during this session's turn.",
        "This evidence does not complete or take over an explicit workflow. No agent step was asked for.",
        f"Later automatic observations use `artifacts/{APPENDED}` or a bounded route-side record.", "",
        f"- harness: {harness}", f"- session: {sid}", f"- work tree: {top}",
        "- attribution: changes observed in this work tree between each turn's own start and end",
        "", ""])


def turn_section(harness, sid, top: Path, source, commits, before, after, began, ended,
                 overlap, env, meta=None) -> str:
    meta = _change_meta(before, after, source) if meta is None else meta
    prev, cur = before.get("entries") or {}, after.get("entries") or {}
    listed = _sample_paths(source)
    rows = []
    for path in listed:
        code = (cur.get(path) or [""])[0].strip() or ("committed" if path not in prev else "clean")
        rows.append(f"{code:<9} {path}")
    earlier = [path for path in listed if path in prev]
    count = meta['source_count']
    label = str(count) if count is not None else f"unknown (at least {len(source)} observed)"
    lines = [f"## Turn {_iso(began)} to {_iso(ended)}", "",
             f"- dirty source paths: before {meta['dirty_before']}, after {meta['dirty_after']}",
             f"- HEAD references: before {before.get('head') or 'unknown'}, after {after.get('head') or 'unknown'}",
             f"- path coverage: listed {len(listed)}; truncated snapshot tail {meta['tail_unknown']}"]
    if overlap:
        lines.append("- concurrent sessions in this work tree: " + ", ".join(overlap)
                     + " (some of these changes may be theirs)")
    else:
        lines.append("- concurrent sessions in this work tree: none observed")
    if earlier:
        lines.append("- already changed before the turn (the diff below is against HEAD and "
                     "includes those earlier edits): " + ", ".join(earlier[:20]))
    lines += ["", f"### Changed files ({label})", "", "```", *rows]
    if len(source) > len(listed) or meta['tail_unknown']:
        omitted = count-len(listed) if count is not None else 'unknown'
        lines.append(f"… {omitted} paths omitted (truncated; counts/HEAD/native references above)")
    lines += ["```", ""]
    if after.get("head"):
        try:
            stat = _git(top, ["diff", "--stat=160", "--no-color", "HEAD", "--",
                              *(f":(literal){path}" for path in listed)], env)
        except (OSError, subprocess.SubprocessError):
            stat = None
        if stat is not None and stat.returncode == 0 and stat.stdout.strip():
            lines += ["### Diff summary (against HEAD)", "", "```", _bounded(stat.stdout.rstrip(), EVIDENCE_LIMIT // 2), "```", ""]
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


def observation_section(harness, sid, before, payload=None, until=None):
    from tidy_transcripts import turn_observations
    facts = turn_observations(harness, sid, before.get("native_anchor") or {}, payload, until=until)
    lines = ["### Observed checks and final reply", "",
             "Record saved is not a test or task PASS. Exit codes are command facts only.",
             f"- native boundary: {facts['terminal']}; turn id: {facts.get('turn_id') or 'unknown'}",
             f"- native coverage: {facts['coverage']}; source: {facts['source'] or 'unknown'}"]
    if facts['error']:
        lines.append(f"- observation unknown: {facts['error']}")
    lines += ["", "Last assistant reply (bounded, credentials masked):", "",
              facts['reply'] or "unknown", "", "Reference: " + facts.get('reply_reference', 'unknown'), ""]
    for item in facts['commands']:
        lines.append(f"- {item['tool']} {item['id']}: {item['state']}; exit codes {item['exit_codes']}; "
                     f"{item['command']}; reference {item.get('result_reference') or item['reference']}")
    if not facts['commands']:
        lines.append("- command outcomes: unknown (none observed in the bounded native range)")
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
        stream.write(_bounded(text))


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
    (work / "evidence.md").write_text(
        _bounded(record_header(harness, sid, top) + _batch_evidence(items)), encoding="utf-8")
    shown = ", ".join(source[:5]) + (", …" if len(source) > 5 else "")
    (work / "summary.txt").write_text(
        f"Automatic record: turn change counts {[i.get('source_count', len(i['source'])) for i in items]} "
        f"({len(source)} sampled paths) during a {harness} turn without a route "
        f"({shown})\n", encoding="utf-8")
    (work / "request.md").write_text(
        f"Intent: Hearting automatic record of the files this {harness} session changed in this "
        f"folder during turns without a route.\nScope: {shown}\n"
        "Done: the changed files and a diff summary are kept as this route's evidence.\n",
        encoding="utf-8")
    child_env = _child_env(env, harness, sid)
    fresh = [path for item in items for path in item.get("fresh", [])] or source
    slug = gate._slug_word("auto-" + (Path(fresh[0]).stem if fresh else "truncated-turn"))
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
    sections = _bounded(_batch_evidence(items), EVIDENCE_LIMIT-1) + "\n"
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
        stream.write(_bounded(head + _batch_evidence(items), EVIDENCE_LIMIT-1) + "\n")
    _run([str(UTILITIES / "artifact_producer.py"), "checkpoint", "--trigger", "explicit",
          "--artifact-root", root, "--cycle", cycle_id], _child_env(env, harness, sid), CHECKPOINT_TIMEOUT)
    return {"ok": True, "cycle_id": cycle_id, "file": str(target)}


# ----------------------------------------------------------------------- turns


def _top(cwd: str, env):
    top = gate._work_tree(Path(os.path.realpath(cwd or "/")))
    if top is None or gate._excluded(Path(os.path.realpath(top)), env, git_project=True):
        return None
    return Path(os.path.realpath(top))


def _mode(harness: str, sid: str, top: Path, state: dict, env):
    """Record outside active explicit workflows; unknown route state stays explicit.
    A proven closed historical route does not disable future tiny-fix records.
    """
    # The ledger's `automatic` lines are this record's own routes, not the session's: they are
    # left out, so a turn that starts while the record's route is being created still records.
    routes = gate.held_routes(harness, sid, env, own_only=True)
    root = gate.folder_root(top, env) if routes is not None else ""
    if not root:
        return "skip", ""
    automatic = (state.get("records") or {}).get(root, {}).get("route_id")
    from fleet.route import load_outcome
    for rid in routes.get(root, set()) - {automatic}:
        route_file = Path(root) / ".runtime" / "routes" / f"{rid}.json"
        outcome = load_outcome(str(route_file), rid)
        if not (outcome.get("present") and outcome.get("match") and outcome.get("closed_at")
                and not outcome.get("finish_pending")):
            return "route-held", root  # unknown is not closed
    return "record", root


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
                        entry = records_now.get(root)
                        root_items = [item for item in pending if item['root'] == root and item['id'] in ids]
                        if entry and root_items:
                            item = max(root_items, key=lambda row: row.get('began') or 0)
                            entry['last_turn'] = _last_turn(item)
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


def _end_event(now, payload):
    """Bound native hints retained on an existing turn while its capture is pending."""
    hints = {}
    for key, cap in (("transcript_path", 4096), ("transcriptPath", 4096), ("turn_id", 256),
                     ("last_assistant_message", 8192), ("hook_event_name", 128)):
        value = _field(payload or {}, key)
        if value:
            if key == "last_assistant_message":
                from work_intent import redact_secrets
                value = redact_secrets(value)
            hints[key] = value.encode("utf-8")[:cap].decode("utf-8", "ignore")
    if (payload or {}).get("interrupted"):
        hints["interrupted"] = True
    return {"at": now, "payload": hints}


def _ready_ends(state, before=None):
    """At most the retained prior turn and current turn; no new queue or waiter."""
    for turn in (state.get("prior_turn") or {}, state):
        event = turn.get("deferred_end")
        if (not isinstance(event, dict) or not isinstance(event.get("at"), (int, float))
                or turn.get("start_pending") or turn.get("entries") is None):
            continue
        if before is not None and event["at"] >= before:
            continue
        if turn is not state and (state.get("start_pending") or state.get("entries") is None
                or turn.get("top") != state.get("top") or turn.get("root") != state.get("root")
                or event["at"] >= (state.get("turn_started_at") or 0)):
            continue
        yield event


def _resume_ends(path, harness, sid, cwd, env, recorder, appender, before=None):
    return [turn_end(harness, sid, cwd, env, now=event["at"], payload=event.get("payload"),
                     recorder=recorder, appender=appender)
            for event in _ready_ends(_read(path), before)]


def turn_start(harness: str, sid: str, cwd: str, env, *, now=None, recorder=None, appender=None, payload=None) -> dict:
    """Publish this boundary before off-lock Git I/O, then attach only its own capture."""
    now = time.time() if now is None else now
    path = state_path(env, harness, sid)
    result = {"state": "busy"}

    def reserve(state):
        latest = max(state.get("turn_started_at") or 0, state.get("turn_ended_at") or 0)
        if latest > now:
            result["state"] = "late-start"
            return
        if not state:
            _prune(path.parent, now)
            _prune_markers(env)
        if (state.get("turn_ended_at") is None and isinstance(state.get("turn_started_at"), (int, float))
                and (state.get("entries") is not None and state.get("root") or state.get("start_pending"))):
            state["prior_turn"] = {key: state.get(key) for key in PRIOR_KEYS}
            for key in ("start_pending", "deferred_end"):
                if key in state:
                    state["prior_turn"][key] = state[key]
        else:
            state.pop("prior_turn", None)
        state.pop("deferred_end", None)
        state.update(harness=harness, session_id=sid, top=None, root="", head="", entries=None,
                     native_anchor=None, turn_started_at=now, turn_ended_at=None, start_pending=True)
        result["state"] = "start-pending"

    if _update(path, reserve) is None or result["state"] != "start-pending":
        return result
    # Even _top and native reads are outside the lock. An end arriving here sees
    # the reservation and retains its own event instead of taking a mixed snapshot.
    top = _top(cwd, env)
    mode, captured_root = _mode(harness, sid, top, _read(path), env) if top else ("skip", "")
    taken, anchor = None, None
    if mode in ("record", "route-held"):
        from tidy_transcripts import turn_anchor
        native = payload or {}
        anchor = turn_anchor(harness, sid, native.get("transcript_path") or native.get("transcriptPath"), at=now)
        taken = snapshot(top, env)

    def publish(state):
        slot = state if state.get("turn_started_at") == now else state.get("prior_turn") or {}
        current = slot is state
        result["state"] = "late-start"
        if slot.get("turn_started_at") != now or not slot.get("start_pending"):
            return
        slot.pop("start_pending", None)
        slot.update(top=str(top) if top else None, native_anchor=anchor)
        status = "no-work-tree"
        if top is not None:
            mode, root = _mode(harness, sid, top, state, env)
            status = mode
            if mode in ("record", "route-held"):
                status = "unreadable"
                if taken is not None and root == captured_root:
                    slot.update(**taken, root=root)
                    status = "baseline" if mode == "record" else "route-held-baseline"
        if current:
            result["state"] = status

    if _update(path, publish) is None:
        return {"state": "busy"}
    _resume_ends(path, harness, sid, cwd, env, recorder, appender)
    state = _read(path)
    if state.get("pending") or any(not entry.get("closed") for entry in (state.get("records") or {}).values()):
        _record_pass(path, harness, sid, env, recorder or record, appender or append, now, 0.0)
    return result


def turn_end(harness: str, sid: str, cwd: str, env, *, now=None, recorder=None, appender=None, payload=None) -> dict:
    """Compare with this turn's own start snapshot, queue its source changes, then write the
    queue into the session's automatic record for the folder."""
    now = time.time() if now is None else now
    recorder = record if recorder is None else recorder
    appender = append if appender is None else appender
    path = state_path(env, harness, sid)
    result = {"state": "busy"}
    end_event = _end_event(now, payload)  # sanitize/limit outside the state lock too
    # A prior end whose start boundary became available after its child stopped
    # can be drained by the next existing boundary too; never replay this call.
    _resume_ends(path, harness, sid, cwd, env, recorder, appender, before=now)
    observed = _read(path)
    after, captured_root = None, ""
    began, top = observed.get("turn_started_at"), observed.get("top")
    if (observed.get("turn_ended_at") is None and isinstance(began, (int, float))
            and began <= now and top and observed.get("entries") is not None and Path(top).is_dir()):
        mode, captured_root = _mode(harness, sid, Path(top), observed, env)
        if mode in ("record", "route-held") and captured_root == observed.get("root"):
            after = snapshot(Path(top), env)
            if after is not None and after.get("entries") is None:
                after = None

    def queue(state, before, after, top, root, began, until=None, mode="record"):
        """Queue the source changes between ``before`` and ``after`` as one turn."""
        top_path = Path(top)
        source, commits = changes(before, after, top_path, root, began, env)
        result["state"] = "unchanged"
        meta = _change_meta(before, after, source)
        if source or meta['tail_unknown']:
            overlap = concurrent_sessions(env, harness, sid, top, began, now)
            pending = state.setdefault("pending", [])
            observations = observation_section(harness, sid, before, payload, until)
            item = {
                "id": f"{began:.6f}", "root": root, "top": top, "began": began, "ended": now,
                **meta, "source": _sample_paths(source), "observations": observations,
                "fresh": _sample_paths(p for p in source if p not in (before.get("entries") or {})),
                "section": _bounded(observations + "\n" + turn_section(harness, sid, top_path, source, commits,
                    before, after, began, now, overlap, env, meta))}
            if mode == 'route-held':
                target = Path(root)/'.runtime/auto-record'/f'{harness}-{sid}.held.md'
                saved, error = False, ''
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    header = '' if target.exists() else record_header(harness, sid, top_path)
                    section = f"Supplement to existing routes; no completion or takeover. Turn {_iso(began)}.\n" + item['section']
                    _append_held(target, header + section)
                    saved = True
                except OSError as exc:
                    error = type(exc).__name__
                state['held_turn'] = {'root': root, 'top': top, 'reference': str(target),
                    'record_saved': saved, 'error': error, **_last_turn(item)}
                result['state'] = 'route-observed'
                return
            pending.append(item)
            del pending[:-MAX_PENDING]
            _mark_pending(env, state, harness, sid)
            result["state"] = "changed"

    def finish_turn(state):
        if not state:
            result["state"] = "no-start-snapshot"
            return
        began, top = state.get("turn_started_at"), state.get("top")
        prior = state.get("prior_turn")
        if state.get("start_pending") and isinstance(began, (int, float)):
            target = prior if now < began else state
            result["state"] = "late-end"
            if (isinstance(target, dict) and isinstance(target.get("turn_started_at"), (int, float))
                    and target["turn_started_at"] <= now):
                target["deferred_end"] = end_event
                result["state"] = "start-pending"
            return
        if isinstance(began, (int, float)) and now < began:
            # This end happened before the open turn began: the next turn's start step ran
            # first. The open turn is not this one's to close. This turn's own start
            # snapshot was kept; the newer turn's start snapshot stands in for its end.
            result["state"] = "late-end"
            if (isinstance(prior, dict) and isinstance(prior.get("turn_started_at"), (int, float))
                    and prior["turn_started_at"] > now):
                return  # a later turn's kept snapshot: that turn's own late end still needs it
            if isinstance(prior, dict) and prior.get("start_pending"):
                prior["deferred_end"] = end_event
                result["state"] = "start-pending"
                return
            state.pop("prior_turn", None)
            if (isinstance(prior, dict) and isinstance(prior.get("turn_started_at"), (int, float))
                    and prior["turn_started_at"] <= now and prior.get("entries") is not None
                    and state.get("entries") is not None and prior.get("top") == top
                    and prior.get("root") == state.get("root") and top and Path(top).is_dir()):
                mode, root = _mode(harness, sid, Path(top), state, env)
                result["root"] = root
                if mode in ("record", "route-held") and root == prior["root"]:
                    queue(state, prior, {key: state.get(key) for key in ("head", "entries", "entries_total", "source_total", "entries_truncated", "tail_digest")},
                          top, root, prior["turn_started_at"],
                          (state.get("native_anchor") or {}).get("cursor"), mode)
            return
        if (observed.get("start_pending") and observed.get("turn_started_at") == began
                and state.get("turn_ended_at") is None):
            state["deferred_end"] = end_event
            result["state"] = "start-pending"
            return
        if _turn_key(state) != _turn_key(observed):
            result["state"] = "superseded-end"
            return  # the capture belongs to another turn; keep the newer state intact
        state.pop("prior_turn", None)
        opened = (state.get("turn_ended_at") is None and isinstance(began, (int, float)) and top
                  and Path(top).is_dir())
        result["state"] = "no-start-snapshot"
        if opened:
            top_path = Path(top)
            mode, root = _mode(harness, sid, top_path, state, env)
            result["state"], result["root"] = mode, root
            if mode in ("record", "route-held"):
                # Only this turn's own start and the matching off-lock end are usable.
                has_start = state.get("entries") is not None and root == state.get("root")
                result["state"] = "unreadable" if has_start else "no-start-snapshot"
                if has_start and root == captured_root and after is not None:
                    queue(state, state, after, top, root, began, mode=mode)
        state.pop("deferred_end", None)
        state.update(turn_ended_at=now, entries=None, head="")

    if _update(path, finish_turn) is None:
        return result
    if result["state"] == "start-pending":
        resumed = _resume_ends(path, harness, sid, cwd, env, recorder, appender)
        if resumed:
            return resumed[-1]
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


def _last_turn(item):
    count = item.get('source_count', len(item['source']))
    return {'ended': item['ended'], 'changed_files': count if count is not None else item.get('source_known', 0),
            'changed_files_exact': count is not None, 'truncated': bool(item.get('tail_unknown')) or
                count is None or count > len(item['source']),
            'dirty_before': item.get('dirty_before'), 'dirty_after': item.get('dirty_after'),
            'summary': _bounded(f"Changed total: {count}; known paths: {item.get('source_known', len(item['source']))}; "
                f"dirty before/after: {item.get('dirty_before')}/{item.get('dirty_after')}; truncated: {bool(item.get('tail_unknown'))}\n" +
                (item.get('observations') or item['section'][-6000:]), 6000)}


def supplementary_records(harness, sid, cwd, env=None):
    """Read-only evidence for one exact native SID; never a route default/authority."""
    env = os.environ if env is None else env
    if not _SAFE_SID.match(sid or ""):
        return []
    state = _read(state_path(env, harness, sid))
    top = gate._work_tree(Path(os.path.realpath(cwd or "/")))
    if top is None or state.get('top') != str(top):
        return []
    rows = []
    root = gate.folder_root(top, env)
    for entry in [(state.get('records') or {}).get(root, {})]:
        if entry.get('last_turn'):
            rows.append({'harness': harness, 'session_id': sid, 'route_id': entry.get('route_id'),
                         'reference': entry.get('route_file'), 'record_saved': bool(entry.get('closed')),
                         **entry['last_turn']})
    held = state.get('held_turn') or {}
    if held.get('root') == root and held.get('top') == str(top):
        rows.append({'harness': harness, 'session_id': sid, 'route_id': None, **held})
    return sorted(rows, key=lambda row: row.get('ended', 0))[-3:]


def link_promotion(route, env=None, *, source_cwd=None):
    """Attach this session's open turn to an explicit compose; never mutate source."""
    env = os.environ if env is None else env
    if is_automatic_route(route):
        return None
    try:
        chain = gate._route_chain()
        identity = chain.writer_identity(env)
        if not identity:
            return None
        harness, sid = identity
        state = _read(state_path(env, harness, sid))
        if not (state.get("entries") is not None and state.get("turn_ended_at") is None
                and state.get("top") == str(_top(source_cwd or route.get("cwd", ""), env))):
            return None
        top = Path(state["top"])
        after = snapshot(top, env)
        if not after or after.get("entries") is None:
            return None
        now = time.time()
        source, commits = changes(state, after, top, state['root'], state['turn_started_at'], env)
        destination = str(_top(route['cwd'], env) or Path(route['cwd']).resolve())
        guidance = (f"Original worktree: {top}\nPrepared worktree: {destination}\n" +
            ("Dirty edits already remain in this same worktree; continue in place.\n" if destination == str(top) else
             "Dirty edits remain in the original worktree; the prepared worktree starts at HEAD. "
             "Read task-scoped diffs there and reapply only the task's changes in the prepared worktree. "
             "Do not copy unrelated user changes or stash/reset/overwrite either worktree.\n"))
        start_snapshot = _bounded(json.dumps({key: state.get(key) for key in PRIOR_KEYS}, ensure_ascii=False), EVIDENCE_LIMIT//4)
        evidence = _bounded(record_header(harness, sid, top) + guidance + observation_section(harness, sid, state) +
            '\nStart snapshot (bounded status, size, mtime; counts above):\n```json\n' + start_snapshot + '\n```\n' +
            turn_section(harness, sid, top, source, commits, state, after, state['turn_started_at'], now, [], env))
        target = Path(route['artifact_root']) / '.runtime' / 'auto-record' / f"{route['route_id']}.promotion.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(evidence, encoding='utf-8')
        return str(target)
    except Exception:
        return None  # observation must never block promotion


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
        try:
            payload = json.loads(sys.stdin.buffer.read(24 * 1024))
            if not isinstance(payload, dict):
                payload = {}
        except (ValueError, OSError):
            payload = {}
        step(args.harness, args.session, args.cwd, os.environ, now=at, payload=payload)
    except Exception:  # noqa: BLE001 -- detached and silent: a failure leaves only its state
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
