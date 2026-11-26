"""Cross-harness session registry — reader + writer (F-26b, plan.md §7.1).

Claude Code has always had a tier-1 identity file (``~/.claude/sessions/<pid>.json``,
written by the runtime itself) that gives a session its name, status, and PID-reuse
guard. Codex and OpenCode had no equivalent, so their rows fell back to weaker
tier-2/tier-3 evidence. This module is the harness-neutral contract both sides
share: Claude stays runtime-native and read-only here; a hearting-managed Codex
launch (``utilities/codex-managed-entry.py`` + ``codex-managed-gateway.py``) is the
first writer; OpenCode has no writer yet (``writer_support("opencode") ==
"not-implemented"``).

Hearting-owned records live under ``FLEET_SESSION_REGISTRY_DIR`` when set, else
``${XDG_STATE_HOME:-~/.local/state}/agent-fleet/sessions/<harness>/<pid>.json`` — the
same state-root shape as ``tools/fleet/interaction.py``, deliberately NOT the
``AGENT_DISPATCH_JOBS``-anchored resolver chain that peer-message ledgers used
(that coupling is exactly what split the peer ledger into two roots, plan.md §2.2).

Every reader call returns every key in ``FIELDS`` — an absent value is ``None``,
never synthesized. A missing, foreign-owned, oversized, symlinked, or malformed
file is silence (``None``), matching ``interaction.py``'s validation posture.

Import contract: this module must stay importable with only the repository's
``tools`` directory on ``sys.path`` (``from fleet import session_registry``,
mirroring ``peer-message.py:412``'s lazy sys.path insert for non-Fleet
consumers such as ``utilities/peer-steward.py``). It therefore never imports
``render``, ``model``, or ``collectors`` at module scope — only
``fleet.session_handle`` is imported lazily, inside ``apply_to_session``, and
only for the Claude branch.
"""
import errno
import json
import os
import re
import stat
import tempfile
import threading
from collections import OrderedDict

HARNESSES = ("claude", "codex", "opencode")
FIELDS = ("pid", "sessionId", "cwd", "startedAt", "procStart", "version", "kind",
          "entrypoint", "name", "nameSource", "nameSince", "status", "updatedAt",
          "statusUpdatedAt", "harness", "parkedJobId")
STATUSES = ("idle", "busy", "shell", "exited")
WRITER_SUPPORT = {"claude": "runtime-native",
                  "codex": "hearting-managed",
                  "opencode": "not-implemented"}
# Where a resumed session's PRIOR ids can be read from, per harness. Claude Code keeps
# both on the live process: `--session-id <new>` beside `--resume <…>/<old>.jsonl`, so the
# pair is derivable with no state of our own. Codex interactive threads live behind one
# shared `codex app-server` (no per-session process) and `codex-managed-entry.py` writes
# its registry record before any thread id exists, so today there is nothing to read;
# OpenCode has no writer at all. Both are declared gaps, not silent failures.
ALIAS_SUPPORT = {"claude": "proc-argv",
                 "codex": "not-implemented",
                 "opencode": "not-implemented"}

_MAX_BYTES = 64 * 1024
# Absent or refused (symlink, socket, permission) is something a read observed.
_SETTLED_ERRNOS = frozenset((errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENXIO,
                             errno.EACCES, errno.EPERM))


def unread_error(error):
    """True when an OSError means the source was not read, not that it is absent."""
    return isinstance(error, OSError) and error.errno not in _SETTLED_ERRNOS


class PassiveReadCache:
    """Validated metadata for passive display readers; process truth is separate."""
    def __init__(self):
        self.entries = OrderedDict()
        self.bytes = 0
        self.lock = threading.RLock()

    def get(self, path, generation):
        with self.lock:
            held = self.entries.get(path)
            if held is not None and held[0] == generation:
                self.entries.move_to_end(path)
                return dict(held[2])
        return None

    def put(self, path, generation, size, record):
        if not all(value is None or isinstance(value, (str, int, float, bool))
                   for value in record.values()):
            return
        with self.lock:
            old = self.entries.pop(path, None)
            if old is not None:
                self.bytes -= old[1]
            self.entries[path] = (generation, size, dict(record))
            self.bytes += size
            while len(self.entries) > 2048 or self.bytes > 16 * 1024 * 1024:
                _key, old = self.entries.popitem(last=False)
                self.bytes -= old[1]


def _read_generation(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, json.load)


class RegistryWriteUnsupported(RuntimeError):
    """Raised by write()/remove() for a harness with no hearting-managed writer."""


def _check_harness(harness):
    if harness not in HARNESSES:
        raise ValueError("unknown harness: %r" % (harness,))


def writer_support(harness):
    _check_harness(harness)
    return WRITER_SUPPORT[harness]


def alias_support(harness):
    _check_harness(harness)
    return ALIAS_SUPPORT[harness]


_MAX_ALIASES = 8
_SID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _proc_argv(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as handle:
            raw = handle.read(_MAX_BYTES)
    except Exception:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def session_aliases(harness, pid, *, argv=None):
    """Session ids this live process ALSO answers to, newest-known first, or ``[]``.

    A `/resume` or `--fork-session` mints a brand-new session id while the peer ledger,
    a steward marker, or a pending completion may still address the conversation by the
    id it had before. Nothing on disk records the equivalence — but the running process
    still carries both halves in its own argv, so the mapping is derived, never stored.
    That matters: an alias file would need a TTL, a PID-reuse guard, and a sweeper, and
    would go stale exactly when the session it describes exits.

    Display-join use only. An alias must never address a ledger write, a
    `report-agent-session` call, or a completion/wake recipient — those stay exact.
    """
    _check_harness(harness)
    if ALIAS_SUPPORT[harness] != "proc-argv":
        return []
    parts = _proc_argv(pid) if argv is None else list(argv)
    if not parts:
        return []
    current = None
    aliases = []
    for index, part in enumerate(parts):
        if part == "--session-id" and index + 1 < len(parts):
            current = _SID_RE.fullmatch(parts[index + 1].strip())
            current = current.group(0).lower() if current else None
        elif part in ("--resume", "-r", "--continue-from") and index + 1 < len(parts):
            value = parts[index + 1].strip()
            # `--resume` takes either a bare id or a transcript path whose stem is one.
            stem = value.rsplit("/", 1)[-1]
            if stem.endswith(".jsonl"):
                stem = stem[:-len(".jsonl")]
            match = _SID_RE.fullmatch(stem)
            if match:
                aliases.append(match.group(0).lower())
    # The process's own current id is not an alias of itself.
    return [sid for sid in dict.fromkeys(aliases) if sid != current][:_MAX_ALIASES]


# --- Claude's "send to background" link (M1) ------------------------------------------
#
# `/bg` (or an empty prompt + the left arrow) does not move a conversation: Claude Code
# clones it under a new session id into a daemon-run process (argv `--session-id NEW
# --fork-session --resume <OLD>.jsonl`) and leaves the original process as a viewer.
# Nothing public says which viewer shows which background session, but two internal
# facts of the running processes agree when it is so:
#   (a) the live viewer's record ``sessions/<pid>.json`` carries ``parkedJobId`` and a live
#       ``kind=bg`` record carries the same ``jobId``;
#   (b) that background process's argv ``--resume`` names the viewer's ``sessionId``.
# Both are Claude's own files and argv, not a documented contract: this is the ONE place
# that reads them, and any change of shape falls to "not linked" (``None``). Replace it with
# an official signal when Claude Code offers one. The link is never stored: every call
# decides it again from the live processes, so it ends the moment either one does.
# Every reader asks here: the shell panel and peer delivery (:func:`parked_successor`), Fleet's
# row, number and pane header (one decision per pass, :func:`steady_parked_links`), and a pane
# proof (:func:`holds_parked_job`).
_MAX_RECORDS = 512


def _proc_start(pid):
    """Live ``/proc/<pid>/stat`` field 22 as a string (the PID-reuse guard), else None."""
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as handle:
            tail = handle.read(4096).rsplit(b")", 1)[1].split()
    except (OSError, ValueError, IndexError):
        return None
    if len(tail) < 20 or tail[0] in (b"Z", b"X"):
        return None
    return tail[19].decode("ascii", "replace")


def _live_claude_records(home, undecided=None):
    """Every live Claude process record, or raise ``OSError`` when the folder was not read.

    A record whose process is gone or reused (``procStart`` differs) is not live. ``undecided``
    (a set) collects the pids of live processes whose record exists but did not parse (half
    written, or not a record): such a read says nothing about that process either way."""
    directory = _dir_for("claude", home=home)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    if len(names) > _MAX_RECORDS:
        raise OSError("session record folder too large")
    found = []
    for name in names:
        match = re.fullmatch(r"([0-9]{1,10})\.json", name)
        if not match:
            continue
        pid = int(match.group(1))
        record = read("claude", pid, home=home, raise_unread=True, extra=("jobId",))
        if (record and str(record.get("procStart") or "") and _proc_start(pid) == str(record["procStart"])
                and record.get("pid") in (None, pid, str(pid))):
            record["pid"] = pid
            found.append(record)
        elif record is None and undecided is not None and _proc_start(pid) is not None:
            undecided.add(pid)
    return found


def _usable_sid(value):
    return value.lower() if isinstance(value, str) and _SID_RE.fullmatch(value) else None


def holds_parked_job(record):
    """True when a process record says its window holds a backgrounded job (one fact only).

    Enough to stop treating that window's ``sessionId`` as the conversation it shows (a pane
    proof withdraws on doubt); never enough to link it to another session — that needs both facts
    (:func:`parked_successor`). Any value counts here: this side only ever withdraws."""
    return isinstance(record, dict) and bool(record.get("parkedJobId"))


def _parked_from_viewer(records, viewer_sid, viewer_pid, viewer_start):
    same = [item for item in records if _usable_sid(item.get("sessionId")) == viewer_sid]
    # One live process answers to this session id, and it is a viewer holding a job.
    if len(same) != 1:
        return None
    viewer = same[0]
    job = viewer.get("parkedJobId")
    if (viewer.get("kind") == "bg" or not isinstance(job, str) or not job
            or viewer_pid is not None and viewer["pid"] != int(viewer_pid)
            or viewer_start is not None and str(viewer["procStart"]) != str(viewer_start)):
        return None
    # No other live process points at the same job, and exactly one background process is it.
    if any(item is not viewer and item.get("parkedJobId") == job for item in records):
        return None
    background = [item for item in records if item.get("kind") == "bg" and item.get("jobId") == job]
    if len(background) != 1:
        return None
    other = background[0]
    sid = _usable_sid(other.get("sessionId"))
    if sid is None or sid == viewer_sid or session_aliases("claude", other["pid"]) != [viewer_sid]:
        return None
    started = _ms_to_sec(other.get("startedAt"))
    return {"harness": "claude", "session_id": sid, "pid": other["pid"],
            "proc_start": str(other["procStart"]), "started_at": started,
            "viewer_sid": viewer_sid, "viewer_pid": viewer["pid"],
            "viewer_start": str(viewer["procStart"]), "job_id": job}


def parked_successor(harness, sid, *, viewer_pid=None, viewer_start=None, home=None):
    """The background session a live viewer of ``sid`` holds, or ``None``.

    ``{"session_id", "pid", "proc_start", "started_at", "viewer_sid", "viewer_pid", "job_id"}``
    only when both facts above agree for exactly one live viewer and one live background
    process. One fact alone (a deliberate ``--fork-session``, a background session started
    elsewhere), a dead or reused process, an unreadable or broken record, a second process
    for either side, or a viewer that holds no job anymore all give ``None``. Read-only."""
    try:
        sid = _usable_sid(sid)
        if harness != "claude" or sid is None:
            return None
        return _parked_from_viewer(_live_claude_records(home), sid, viewer_pid, viewer_start)
    except Exception:
        return None


def _a_record_holds_a_job(home):
    """False only when the record folder was read and no process record names ``parkedJobId``.

    A cheap look at the raw bytes first: no viewer holds a job in the ordinary case, and then
    nothing needs a process check. Any doubt answers True and the full read decides."""
    directory = _dir_for("claude", home=home)
    try:
        names = os.listdir(directory)
        if len(names) > _MAX_RECORDS:
            return True
        for name in names:
            if re.fullmatch(r"[0-9]{1,10}\.json", name):
                with open(os.path.join(directory, name), "rb") as handle:
                    if b"parkedJobId" in handle.read(65537):
                        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def parked_links(harness="claude", *, home=None):
    """Every link :func:`parked_successor` and :func:`parked_origin` would find, from ONE read of
    the live process records: ``{"successor": {viewer_sid: link}, "origin": {held_sid: link}}``.

    A caller that asks about many sessions in one pass (a scan of the pending rows) reads the
    record folder once and looks each session up. Nothing is held and nothing is stored: the
    table is as live as the read, and it is empty (so a lookup costs nothing more) unless some
    live viewer holds a job. Any doubt or read failure gives an empty table."""
    try:
        if harness != "claude" or not _a_record_holds_a_job(home):
            return {"successor": {}, "origin": {}}
        return _links_table(_live_claude_records(home))
    except Exception:
        return {"successor": {}, "origin": {}}


def _links_table(records):
    table = {"successor": {}, "origin": {}}
    for item in records:
        job = item.get("parkedJobId")
        viewer_sid = _usable_sid(item.get("sessionId"))
        if viewer_sid is None or item.get("kind") == "bg" or not isinstance(job, str) or not job:
            continue
        found = _parked_from_viewer(records, viewer_sid, None, None)
        if found is None:
            continue
        held = found["session_id"]
        if sum(1 for other in records if other.get("kind") == "bg"
               and _usable_sid(other.get("sessionId")) == held) != 1:
            continue
        table["successor"][viewer_sid] = found
        table["origin"][held] = found
    return table


# The links Fleet's last pass decided, in this process only (never stored), and for each link
# kept without a confirming read, how many passes in a row it has been kept so.
_STEADY = {}
_STEADY_UNCONFIRMED = {}
_STEADY_LOCK = threading.Lock()
# A link is kept through at most this many passes in a row that could not confirm it: Fleet asks
# every 2 s by default, so about 10 s. A half-written record lasts a moment and a failed read a
# pass or two; anything longer is not passing, and doubt draws two rows.
STEADY_KEEP_PASSES = 5


def _record_allows(pid, sid, start, *, home, bg, job):
    """Whether a process's own record, read by its pid (no folder listing), leaves the link
    standing: an unreadable or half-written record says nothing (True); a record that reads and
    names another session, another process start, another job or the other kind says no."""
    record = read("claude", pid, home=home, extra=("jobId",))
    if record is None:
        return True
    return (_usable_sid(record.get("sessionId")) == sid and str(record.get("procStart") or "") == start
            and (record.get("kind") == "bg") == bg
            and record.get("jobId" if bg else "parkedJobId") == job)


def steady_parked_links(harness="claude", *, home=None):
    """:func:`parked_links` for a caller that asks again on every pass (Fleet's rows and numbers).

    A read that could not decide is not "no link": when the record folder was not read, or one
    of a link's two records exists but did not parse (half written), a link the previous pass
    decided is kept, if both of its processes are still the same live processes and each one's
    own record, read again by its pid, either still says so or cannot be read. A read that does
    decide — the folder read, or that pid read (the window's record holds no job or another job,
    a record names another session, a process ended or is another process) — ends it at once.
    The keeping lasts at most ``STEADY_KEEP_PASSES`` passes in a row; a link nobody decided before
    is never made from a failed read. Process-local; a new process starts with no link kept."""
    empty = {"successor": {}, "origin": {}}
    if harness != "claude":
        return empty
    with _STEADY_LOCK:
        kept = dict(_STEADY)
        undecided, failed = set(), False
        try:
            table = (_links_table(_live_claude_records(home, undecided))
                     if kept or _a_record_holds_a_job(home) else empty)
        except Exception:
            table, failed = {"successor": {}, "origin": {}}, True
        unconfirmed = {}
        for viewer_sid, link in kept.items():
            if (viewer_sid in table["successor"] or link["session_id"] in table["origin"]
                    or not (failed or {link["viewer_pid"], link["pid"]} & undecided)):
                continue
            passes = _STEADY_UNCONFIRMED.get(viewer_sid, 0) + 1
            if passes > STEADY_KEEP_PASSES:
                continue
            start = link.get("viewer_start")
            if (_proc_start(link["viewer_pid"]) == start and _proc_start(link["pid"]) == link["proc_start"]
                    and _record_allows(link["viewer_pid"], viewer_sid, start, home=home, bg=False,
                                       job=link["job_id"])
                    and _record_allows(link["pid"], link["session_id"], link["proc_start"], home=home,
                                       bg=True, job=link["job_id"])):
                table["successor"][viewer_sid] = link
                table["origin"][link["session_id"]] = link
                unconfirmed[viewer_sid] = passes
        _STEADY.clear()
        _STEADY.update(table["successor"])
        _STEADY_UNCONFIRMED.clear()
        _STEADY_UNCONFIRMED.update(unconfirmed)
        return table


def parked_origin(harness, sid, *, home=None):
    """The same link read from the background side: the viewer a background session ``sid`` is
    held by, as :func:`parked_successor` of that viewer (its ``viewer_sid`` is the original id)."""
    try:
        sid = _usable_sid(sid)
        if harness != "claude" or sid is None:
            return None
        records = _live_claude_records(home)
        mine = [item for item in records if item.get("kind") == "bg" and _usable_sid(item.get("sessionId")) == sid]
        if len(mine) != 1:
            return None
        origins = session_aliases("claude", mine[0]["pid"])
        if len(origins) != 1:
            return None
        found = _parked_from_viewer(records, origins[0], None, None)
        return found if found and found["session_id"] == sid else None
    except Exception:
        return None


def session_join_keys(sess):
    """Every ``(harness, session_id)`` a rendered row answers to — its own id first, then
    its resume aliases. The one definition: the peer ledger join, the steward marker join,
    and the renderer's badge map must not each decide this for themselves, or they drift
    into disagreeing about which row a relation belongs to."""
    harness = str(getattr(sess, "harness", "") or "").lower()
    keys = []
    sid = getattr(sess, "session_id", None)
    if sid:
        keys.append((harness, sid))
    for alias in (getattr(sess, "session_aliases", None) or []):
        if alias and (harness, alias) not in keys:
            keys.append((harness, alias))
    return keys


def state_root():
    explicit = os.environ.get("FLEET_SESSION_REGISTRY_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "agent-fleet", "sessions")


def _dir_for(harness, home=None):
    if harness == "claude":
        base = home or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        return os.path.join(base, "sessions")
    return os.path.join(state_root(), harness)


def registry_path(harness, pid, *, home=None):
    _check_harness(harness)
    return os.path.join(_dir_for(harness, home=home), "%d.json" % int(pid))


def read(harness, pid, *, home=None, validation_cache=None, raise_unread=False, extra=()):
    """dict with every FIELDS key present (absent → None), or None on any failure.

    With ``raise_unread`` an I/O failure that is not absence (``unread_error``)
    raises instead, so a caller can keep what it showed rather than read None.
    ``extra`` names further keys one reader needs (Claude's ``waitingFor``); they are
    read from the file itself and never kept in the shared cache.
    """
    if extra:
        validation_cache = None
    _check_harness(harness)
    path = registry_path(harness, pid, home=home)
    try:
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            return None
        if metadata.st_size > _MAX_BYTES:
            return None
        generation = _read_generation(metadata)
        if validation_cache is not None:
            held = validation_cache.get(path, generation)
            if held is not None:
                held['harness'] = harness  # a shared/aliased directory still declares its caller's harness
                return held
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as error:
        if raise_unread and unread_error(error):
            raise
        return None
    if not isinstance(payload, dict):
        return None
    record = {field: payload.get(field) for field in (*FIELDS, *extra)}
    record["harness"] = harness   # directory-declared identity wins over file content
    if validation_cache is not None:
        try:
            if generation == _read_generation(os.lstat(path)):
                validation_cache.put(path, generation, metadata.st_size, record)
        except OSError:
            pass
    return record


def _prepare_directory(directory):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    metadata = os.lstat(directory)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise OSError("session registry directory must be an owner directory")
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def write(harness, pid, fields, *, home=None):
    """Merge-update the record for (harness, pid); tmp → os.replace, 0600."""
    _check_harness(harness)
    if WRITER_SUPPORT[harness] != "hearting-managed":
        raise RegistryWriteUnsupported(harness)
    for key in fields:
        if key not in FIELDS:
            raise ValueError("unknown session registry field: %r" % (key,))
    path = registry_path(harness, pid, home=home)
    directory = os.path.dirname(path)
    _prepare_directory(directory)
    existing = read(harness, pid, home=home) or {}
    merged = dict(existing)
    merged.update(fields)
    merged["pid"] = int(pid)
    merged["harness"] = harness
    descriptor, tmp_path = tempfile.mkstemp(dir=directory, prefix=".session-", suffix=".tmp")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True


def remove(harness, pid, *, home=None):
    _check_harness(harness)
    if WRITER_SUPPORT[harness] != "hearting-managed":
        raise RegistryWriteUnsupported(harness)
    path = registry_path(harness, pid, home=home)
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _ms_to_sec(value):
    """registry epoch-ms → epoch-sec; anything non-numeric (or bool) → None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value / 1000.0


def apply_to_session(sess, record, harness):
    """Load every tier-1 registry field onto the Session (moved verbatim from the
    former claude.py:443 ``_apply_registry`` — F-26b generalizes it across harnesses).
    Each key is independently optional: a fresh row carrying only pid/sessionId must
    not lose the ones it has. The only harness-specific branch is the derived `<xx>`
    tag: Claude reads it from its own `derived` registry name; Codex/OpenCode
    read their confirmed shared assignment elsewhere and must not have this
    branch overwrite it.
    """
    sess.session_id = record.get("sessionId") or sess.session_id
    sess.status = record.get("status")               # idle | shell | busy | (absent → None)
    name = record.get("name")
    if name:
        sess.slug = name                              # friendly name disambiguates same-cwd sessions
        sess.registry_name = name                     # explicit link in the name chain (F-26)
        # F-99a ① — the registry `name` is a runtime-exposed user-set name only when
        # `nameSource` says the user actually set it (not the default derived label).
        if record.get("nameSource") != "derived":
            sess.runtime_name = name
        elif harness == "claude":
            # F-100a — the derived `<basename>-<xx>` name is the only carrier of the
            # 2-hex tag; read it while the record still says "derived".
            try:
                from fleet.session_handle import derived_tag
                sess.session_tag = derived_tag(name)
            except Exception:
                sess.session_tag = None
    kind = record.get("kind")
    if isinstance(kind, str):
        sess.kind = kind
    proc_start = record.get("procStart")
    if proc_start is not None and not isinstance(proc_start, bool):
        sess.registry_proc_start = str(proc_start)    # compared against /proc in the classifier
    sess.started_at = _ms_to_sec(record.get("startedAt"))
    sess.updated_at = _ms_to_sec(record.get("updatedAt"))


def name_for_session_id(session_id, harness, *, home=None):
    """The registry `name` for a session id, or None — used by peer-steward (C-3)
    to name a Codex/OpenCode sender the same way claude_session_name() already
    names a Claude one."""
    if not session_id:
        return None
    _check_harness(harness)
    directory = _dir_for(harness, home=home)
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for entry in names:
        if not entry.endswith(".json"):
            continue
        try:
            pid = int(entry[:-len(".json")])
        except ValueError:
            continue
        record = read(harness, pid, home=home)
        if record and record.get("sessionId") == session_id:
            name = record.get("name")
            return name if isinstance(name, str) and name else None
    return None
