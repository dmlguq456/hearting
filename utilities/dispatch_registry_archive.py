"""Move settled rows out of the live dispatch registry into the dispatch archive.

The live registry (`jobs.log`) is read whole by registrations, Fleet, peer-steward and
the retention passes, so every row it keeps is paid for on every read. Rows of work that
ended long ago are history. This pass moves them, byte-exact, into the dispatch archive
the registry inventory already reads (`<state root>/archive/registry-<YYYY-MM>/rows.jsonl`,
shown by `dispatch-registry.py inventory --include-archive`) and leaves every other row.

Rows that share any attempt or route id form one group, and a group moves only as a
whole, when each of its rows
  - is terminal and ended at least ARCHIVE_AFTER_NS ago (its end is the last write of its
    attempt log, remembered while the log exists; the registration time when no later
    write was seen),
  - is bound to a route whose exact closure is at least ARCHIVE_AFTER_NS old,
  - owes no parent delivery (none, or resolved),
  - if it ever started, has its route's completion markers on disk (whether a continuation
    ran is read from its started rows or its markers; the markers then still answer),
  - has no attempt log left: the attempt-log retention, which needs the row to
    judge the log, retires a settled log first, and
  - is valid UTF-8, one line by every reader's line rules, and appears once.
Unknown evidence keeps a row: a reader that starts from a row still in the live file
finds every row linked to it there.

The rows are written to the archive and synced before the live file changes. The live
rewrite takes the existing jobs lock, reads the registry again, keeps any group named by
a row written since the selection, and replaces the file through the registry's own
atomic replace, so a registration or close that ran meanwhile keeps its row. A crash
between the two steps leaves a row in both places, never in neither, and a later pass
does not archive it twice. The pass runs from the dispatch reap watch, beside the
attempt-log retention, at most once every DUE_SECONDS.

Readers that decide from a row's absence find moved rows: a replay of a closed route puts
its rows back (`restore`), route and attempt ids resolve through `archived_rows`, and a
registration whose preparation spanned a move does not count the moved rows as a change
(`moved_only`). `python3 dispatch_registry_archive.py restore [--id <route or attempt id>]`
puts archived rows back into the registry in time order; a later pass moves rows that
still qualify again.
"""

from __future__ import annotations

from collections import Counter
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

ARCHIVE_AFTER_NS = 14 * 86400 * 1_000_000_000   # the attempt-log retention window
DUE_SECONDS = 6 * 3600
MAX_SELECT_SECONDS = 120
LOCK_WAIT_SECONDS = 10
STATE_NAME = "registry-archive.json"
MOVED_NAME = "registry-archive.moved.json"   # the attempts the last rewrite moved
LATE_END_NS = 3600 * 1_000_000_000   # a log written this long after registration is remembered
LOCK_NAME = "registry-archive.lock"
SEGMENT_PREFIX = "registry-"
SCHEMA_VERSION = 1   # the dispatch archive row schema (dispatch_registry_inventory)
TERMINAL = frozenset({"done", "killed", "cancelled"})
_ID_RE = re.compile(r"(?<![A-Za-z0-9_])(?:att|rt)-[A-Za-z0-9_-]+")
_MONTH_RE = re.compile(r"\d{4}-\d{2}")


def _ended_ns(stamp: str):
    from datetime import datetime, timezone
    try:
        ended = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    delta = ended.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def _log_retired(path) -> bool:
    """The row's attempt log is gone (retired, or never written); an unreadable path keeps the row."""
    if not path or not os.path.isabs(path):
        return False
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _groups(lines):
    """Group index of each line: lines that share any attempt or route id are one group."""
    parent = list(range(len(lines)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    first = {}
    for index, line in enumerate(lines):
        for token in set(_ID_RE.findall(line)):
            other = first.setdefault(token, index)
            if other != index:
                a, b = find(index), find(other)
                if a != b:
                    parent[a] = b
    return [find(index) for index in range(len(lines))]


def _log_written_ns(path):
    """Last write of the attempt log, or None when it is gone or unreadable."""
    if not path or not os.path.isabs(path):
        return None
    try:
        return os.lstat(path).st_mtime_ns
    except OSError:
        return None


def _markers_present(root: Path):
    """Whether a route's completion markers exist (`completion/<route id>` is not empty)."""
    seen = {}

    def present(route_id):
        if route_id not in seen:
            try:
                with os.scandir(root / "completion" / route_id) as entries:
                    seen[route_id] = any(True for _entry in entries)
            except OSError:
                seen[route_id] = False
        return seen[route_id]

    return present


def select(lines, *, now_ns, route_closed_ns, delivery_settled, deadline=None, ended=None,
           markers_present=None):
    """The indexes of `lines` whose whole groups may move to the archive.

    `route_closed_ns(metadata)` is the exact closure time of the row's route, or None when
    it is open, pending or unknown; `delivery_settled(metadata)` says the row owes no parent
    delivery; `markers_present(route_id)` says the route's completion markers exist. `ended`
    maps an attempt to its remembered end (the last write of its log seen well after
    registration); this call adds what it sees. Past `deadline` (monotonic) no further row
    is judged, so it stays."""
    from dispatch_contract import parse_registry_metadata
    from route_authority import continuation_attempt_state
    horizon = now_ns - ARCHIVE_AFTER_NS
    ended = {} if ended is None else ended
    counts = Counter(lines)
    eligible = [False] * len(lines)
    for index, line in enumerate(lines):
        if deadline is not None and time.monotonic() > deadline:
            break
        fields = line.split("\t")
        if (len(fields) != 6 or fields[1] not in TERMINAL or counts[line] != 1
                or len(line.splitlines()) != 1):
            continue
        registered = _ended_ns(fields[0])
        metadata = parse_registry_metadata(fields[5])
        attempt = metadata.get("attempt_id")
        if registered is None or not attempt:
            continue
        written = _log_written_ns(metadata.get("log_file"))
        if written is not None and written > registered + LATE_END_NS:
            ended[attempt] = max(written, ended.get(attempt, 0))
        if max(registered, ended.get(attempt, 0)) > horizon or not _log_retired(metadata.get("log_file")):
            continue
        closed = route_closed_ns(metadata)
        if closed is None or closed > horizon or not delivery_settled(metadata):
            continue
        if markers_present is not None and continuation_attempt_state(metadata) != "unstarted" and not all(
                markers_present(metadata[key]) for key in ("route_id", "route", "owner_route_id")
                if metadata.get(key)):
            continue   # its started rows are the only proof this route's work ran
        eligible[index] = True
    groups = _groups(lines)
    blocked = {group for index, group in enumerate(groups) if not eligible[index]}
    return [index for index, group in enumerate(groups) if group not in blocked]


def _route_closure(jobs: Path, proven: dict):
    """Closure time of the row's exact route; proven closures are kept across passes."""
    from worker_home_retention import _route_evidence
    seen = {}

    def closed(metadata):
        binding = "\x1f".join(metadata.get(name, "") for name in (
            "route_file", "route_id", "route_hash", "owner_route_file", "owner_route_id", "owner_route_hash"))
        key = (f"{metadata.get('route_id') or metadata.get('owner_route_id') or '-'}:"
               + hashlib.sha256(binding.encode("utf-8", "surrogatepass")).hexdigest()[:32])
        if key in proven:
            seen[key] = proven[key]
        if key not in seen:
            try:
                state, mtime, _cause = _route_evidence(jobs, metadata)
            except Exception:  # noqa: BLE001 -- unreadable evidence keeps the row
                state, mtime = "unknown", 0
            seen[key] = mtime if state == "closed" and mtime > 0 else None
        return seen[key]

    return closed, seen


def _delivery_settled(root: Path):
    from worker_home_retention import _delivery_state

    def settled(metadata):
        try:
            state, _activity = _delivery_state(root, metadata)
        except Exception:  # noqa: BLE001 -- unreadable evidence keeps the row
            return False
        return state in ("none", "resolved")

    return settled


def _canonical(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _segment_rows(path: Path):
    """Raw lines already in one archive segment (a torn last line is not a row)."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return set()
    raws = set()
    for line in data.split(b"\n"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("raw"), str):
            raws.add(row["raw"])
    return raws


def _archive(root: Path, jobs: Path, raws, now_ns: int) -> int:
    """Append these rows to their month segments and sync them; returns how many were new."""
    segments = {}
    for raw in raws:
        month = raw[:7] if _MONTH_RE.fullmatch(raw[:7]) else "undated"
        segments.setdefault(SEGMENT_PREFIX + month, []).append(raw)
    archive = root / "archive"
    written = 0
    for archive_id, rows in sorted(segments.items()):
        directory = archive / archive_id
        created = not directory.is_dir()
        directory.mkdir(parents=True, exist_ok=True)
        provenance = directory / "provenance.json"
        if not provenance.exists():
            temporary = directory / "provenance.json.tmp"
            temporary.write_text(json.dumps({
                "schema_version": SCHEMA_VERSION, "archive_id": archive_id, "kind": "registry-rolling",
                "source_path": str(jobs)}, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, provenance)
        path = directory / "rows.jsonl"
        present = _segment_rows(path)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ns / 1e9))
        payload = "".join(_canonical({"schema_version": SCHEMA_VERSION, "archive_id": archive_id,
                                      "archived_at": stamp, "raw": raw}) + "\n"
                          for raw in rows if raw not in present).encode("utf-8")
        if payload:
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)   # read: the last byte
            try:
                if os.fstat(fd).st_size and os.pread(fd, 1, os.fstat(fd).st_size - 1) != b"\n":
                    payload = b"\n" + payload   # a torn earlier append stays its own unreadable line
                view = memoryview(payload)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            written += sum(1 for raw in rows if raw not in present)
        if created or payload:
            _fsync_directory(directory)
    if written:
        _fsync_directory(archive)
    return written


def _lock_registry(lock, deadline: float) -> bool:
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _read_lines(jobs: Path):
    """The registry's exact lines, or None when it is not whole UTF-8 rows."""
    data = jobs.read_bytes()
    if data and not data.endswith(b"\n"):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.split("\n")[:-1] if text else []


def _remove(jobs: Path, archived: dict, snapshot: set) -> dict:
    """Under the jobs lock: drop the archived groups no later row names; keep every other row."""
    from dispatch_contract import _atomic_registry_replace
    with open(f"{jobs}.lock", "a", encoding="utf-8") as lock:
        if not _lock_registry(lock, time.monotonic() + LOCK_WAIT_SECONDS):
            return {"removed": 0, "skipped": "registry-busy"}
        lines = _read_lines(jobs)
        if lines is None:
            return {"removed": 0, "skipped": "registry-not-whole-utf8"}
        counts = Counter(lines)
        named = set()
        for line in lines:
            if line not in snapshot:
                named.update(_ID_RE.findall(line))   # written since the selection
        members = {}
        for raw, (group, ids) in archived.items():
            members.setdefault(group, (ids, []))[1].append(raw)
        remove = set()
        for ids, raws in members.values():
            if not ids & named and all(counts[raw] == 1 for raw in raws):
                remove.update(raws)
        if not remove:
            return {"removed": 0}
        kept = [line for line in lines if line not in remove]
        if not kept:
            return {"removed": 0, "skipped": "registry-would-be-empty"}   # its replace writes one blank line
        _record_moved(jobs.parent, remove)
        _atomic_registry_replace(jobs, kept)
        return {"removed": len(lines) - len(kept)}


def _attempt_of(row) -> str:
    """The attempt id of a raw registry line or a parsed `(*fields[:5], metadata items)` value."""
    if isinstance(row, str):
        fields = row.split("\t")
        pipe = fields[5] if len(fields) == 6 else ""
        values = dict(part.split("=", 1) for part in pipe.split(",") if "=" in part)
    elif isinstance(row, tuple) and len(row) == 6 and isinstance(row[5], tuple):
        values = dict(row[5])
    else:
        return ""
    return values.get("attempt_id", "")


def _status_of(row) -> str:
    if isinstance(row, str):
        fields = row.split("\t")
        return fields[1] if len(fields) == 6 else ""
    return row[1] if isinstance(row, tuple) and len(row) == 6 else ""


def _record_moved(root: Path, raws) -> None:
    """Name the attempts this rewrite moves, for a registration whose preparation spans it."""
    moved = sorted({_attempt_of(raw) for raw in raws} - {""})
    temporary = root / (MOVED_NAME + ".tmp")
    temporary.write_text(json.dumps({"schema_version": 1, "attempt_ids": moved}) + "\n", encoding="utf-8")
    os.replace(temporary, root / MOVED_NAME)


def moved_only(jobs, prepared, fresh) -> bool:
    """`fresh` is `prepared` less rows the last archive rewrite moved, and nothing else changed.

    A registration pins the registry rows its preparation read; moving settled rows to the
    archive changes none of their facts, so it is not a change of those dependencies. Both
    sides are the pin's values: raw lines or `(*fields[:5], metadata items)`."""
    fresh, prepared = tuple(fresh), tuple(prepared)
    if len(fresh) >= len(prepared):
        return False
    try:
        record = json.loads((Path(jobs).expanduser().resolve(strict=False).parent
                             / MOVED_NAME).read_text(encoding="utf-8"))
        moved = set(record["attempt_ids"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    position = 0
    for row in prepared:
        if position < len(fresh) and fresh[position] == row:
            position += 1
        elif _status_of(row) not in TERMINAL or _attempt_of(row) not in moved:
            return False
    return position == len(fresh)


def _pass(jobs: Path, root: Path, state: dict, now_ns: int) -> dict:
    lines = _read_lines(jobs)
    if lines is None:
        return {"skipped": "registry-not-whole-utf8"}
    proven = state.get("closed_routes") if isinstance(state.get("closed_routes"), dict) else {}
    ended = {key: value for key, value in (state.get("ended") or {}).items()
             if isinstance(key, str) and isinstance(value, int)}
    closed, seen = _route_closure(jobs, proven)
    indexes = select(lines, now_ns=now_ns, route_closed_ns=closed,
                     delivery_settled=_delivery_settled(root),
                     deadline=time.monotonic() + MAX_SELECT_SECONDS, ended=ended,
                     markers_present=_markers_present(root))
    # Proven closures are final; keep those of routes the registry still names, and the
    # remembered ends of attempts it still holds.
    named = set(_ID_RE.findall("\n".join(lines)))
    state["closed_routes"] = {key: value for key, value in {**proven, **seen}.items()
                              if value is not None and key.split(":", 1)[0] in named}
    state["ended"] = {key: value for key, value in ended.items() if key in named}
    if not indexes:
        return {"archived": 0, "removed": 0, "rows": len(lines)}
    groups = _groups(lines)
    ids = {}
    for index in indexes:
        ids.setdefault(groups[index], set()).update(_ID_RE.findall(lines[index]))
    archived = {lines[index]: (groups[index], ids[groups[index]]) for index in indexes}
    written = _archive(root, jobs, list(archived), now_ns)
    result = _remove(jobs, archived, set(lines))
    return {"archived": written, "rows": len(lines), **result}


def _read_state(root: Path) -> dict:
    try:
        value = json.loads((root / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(root: Path, state: dict) -> None:
    temporary = root / (STATE_NAME + ".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, root / STATE_NAME)


@contextlib.contextmanager
def _pass_lock(root: Path):
    with open(root / LOCK_NAME, "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def sweep(jobs, *, now_ns=None, force=False) -> dict:
    """One best-effort pass over the canonical registry; a failure changes no row and is
    recorded as the pass's `last_result` in the state file."""
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    jobs = Path(jobs).expanduser().resolve(strict=False)
    if jobs.name != "jobs.log" or not jobs.is_file():
        return {"skipped": "registry-missing"}
    root = jobs.parent
    with _pass_lock(root) as locked:
        if not locked:
            return {"skipped": "busy"}
        state = _read_state(root)
        last = state.get("last_pass_ns")
        if (not force and isinstance(last, int) and last <= now_ns
                and now_ns - last < DUE_SECONDS * 1_000_000_000):
            return {"skipped": "not-due"}
        state["last_pass_ns"] = now_ns
        _write_state(root, state)   # one pass per due window, whatever this one meets
        try:
            result = _pass(jobs, root, state, now_ns)
        except Exception as exc:  # noqa: BLE001 -- kept in the state, where a later look finds it
            result = {"skipped": "error", "error": type(exc).__name__, "detail": str(exc)[:200]}
        state["last_result"] = result
        _write_state(root, state)
        return result


def sweep_best_effort(jobs):
    try:
        return sweep(jobs)
    except Exception:  # noqa: BLE001 -- never change the caller's result
        return None


def archived_rows(jobs, needle: str = "", *, needles=()):
    """Raw registry lines moved to the archive, oldest first (a segment keeps its appends in
    order), each once; with `needle` or `needles` (route or attempt ids, or any text) only those
    that contain one. History, id resolution and closed-route replays read these; liveness,
    terminal and marker judgments never do (the archive holds only settled rows)."""
    archive = Path(jobs).expanduser().resolve(strict=False).parent / "archive"
    found, seen = [], set()
    texts = [text for text in (needle, *needles) if text] or [""]
    encoded = [text.encode("utf-8") for text in texts]
    try:
        segments = sorted(archive.glob(SEGMENT_PREFIX + "*"))
    except OSError:
        return found
    for segment in segments:
        try:
            data = (segment / "rows.jsonl").read_bytes()
        except OSError:
            continue
        for line in data.split(b"\n"):
            if not any(text in line for text in encoded):
                continue
            try:
                raw = json.loads(line).get("raw")
            except (ValueError, AttributeError):
                continue
            if isinstance(raw, str) and any(text in raw for text in texts) and raw not in seen:
                seen.add(raw)
                found.append(raw)
    return found


def restore(jobs, needle: str = "") -> int:
    """Put archived rows back into the registry in time order; returns how many came back.

    With `needle` (a route or attempt id), only the archived groups that name it. An attempt
    comes back once, as its last archived version, and never while a row of it is live (a
    replay may have marked the row it brought back). The archive keeps its copy, so a later
    pass moves rows that still qualify again. Runs under the registry's own lock and atomic
    replace."""
    from dispatch_contract import _atomic_registry_replace
    jobs = Path(jobs).expanduser().resolve(strict=False)
    if needle:
        # The group: rows that name the needle, then rows that name their ids, until it closes.
        # Each round reads the archive once and parses only the lines that name an id.
        ids, archived = {needle}, []
        while True:
            archived = archived_rows(jobs, needles=sorted(ids))
            grown = ids | {token for raw in archived for token in _ID_RE.findall(raw)}
            if grown == ids:
                break
            ids = grown
    else:
        archived = archived_rows(jobs)
    latest = {}
    for raw in archived:   # oldest first: a later version of an attempt replaces an earlier one
        latest[_attempt_of(raw) or raw] = raw
    if not latest:
        return 0
    with open(f"{jobs}.lock", "a", encoding="utf-8") as lock:
        if not _lock_registry(lock, time.monotonic() + LOCK_WAIT_SECONDS):
            raise OSError("registry-busy")
        lines = _read_lines(jobs)
        if lines is None:
            raise OSError("registry-not-whole-utf8")
        present = set(lines)
        live = {_attempt_of(line) for line in lines} - {""}
        back = sorted((raw for key, raw in latest.items() if raw not in present and key not in live),
                      key=lambda raw: raw.split("\t", 1)[0])
        if not back:
            return 0
        merged, position = [], 0
        for line in lines:
            stamp = line.split("\t", 1)[0]
            while position < len(back) and back[position].split("\t", 1)[0] <= stamp:
                merged.append(back[position])
                position += 1
            merged.append(line)
        merged.extend(back[position:])
        _atomic_registry_replace(jobs, merged)
        return len(back)


# The post-exit retention passes, in order. Each keeps its own due gate and lock, so a
# caller may run them as often as it likes; a pass that is not due returns at once.
RETENTION_PASSES = ("worker_home_retention", "dispatch_log_retention",
                    "reusable_runtime_retention", "dispatch_registry_archive")


def retention_passes(jobs) -> None:
    """Run every post-exit retention pass once, each best effort and independent.

    The dispatch reap watch runs them when an attempt's watch ends; the peer obligation
    runner's maintenance pass starts them too, so they run even while nothing dispatches."""
    import importlib
    for name in RETENTION_PASSES:
        try:
            importlib.import_module(name).sweep_best_effort(jobs)
        except Exception:  # noqa: BLE001 -- one pass never stops the next
            pass


def _main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Put archived dispatch registry rows back.")
    parser.add_argument("action", choices=("restore", "retention"))
    parser.add_argument("--jobs", default="", help="registry path (default: the canonical jobs.log)")
    parser.add_argument("--id", default="", help="only the archived groups that name this route or attempt id")
    args = parser.parse_args(argv)
    jobs = args.jobs or os.environ.get("AGENT_DISPATCH_JOBS", "")
    if not jobs:
        from dispatch_contract import stable_state_root
        jobs = stable_state_root(os.environ) / "jobs.log"
    if args.action == "retention":
        # Started by the peer obligation runner: hand the passes to a grandchild so the
        # runner reaps its child at once, and run them at a lower priority.
        if os.fork():
            os._exit(0)
        with contextlib.suppress(OSError):
            os.nice(10)
        retention_passes(jobs)
        return 0
    print(f"restored={restore(jobs, args.id)}")
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(_main())
