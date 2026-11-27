"""Cross-harness fleet title sidecars (stdlib only).

Canonical state:
  <state-root>/<harness>/<sid>.json
  {"title": str, "title_ts": float|null, "ts": float, "source": str, "offset": int, "cursor_kind": str,
   "summary": str, "summary_ts": float, "summary_failures": int,
   "summary_error": str, "requested_language": str}  # optional attempt diagnostics

``summary`` is additive (F-16/F-17 merge): the same haiku call that produces the
title also returns a live one-sentence status, written alongside it. Absent on
older sidecars and omitted from the written dict when there is nothing to say —
old readers ignore the unknown key either way.

Beside it, ``<sid>.timeline.jsonl`` is an append-only record of the earlier NOW values
and the person's one-line prompts (``append_timeline`` / ``read_timeline``). It follows
the sidecar's seven-day sweep and is the only place earlier NOW values are kept.

``FLEET_TITLE_STATE_DIR`` overrides the state root. Otherwise the XDG state
location is used: ``${XDG_STATE_HOME:-~/.local/state}/agent-fleet/titles``.
The pre-F-21 Claude path (``~/.claude/.fleet-titles/<sid>.json``) remains a
read-only migration fallback; every new write goes to the neutral state root.
"""
import json
import math
import os
import re
import stat
import tempfile
import time

_FRESH_SEC = 24 * 3600
# F-63 (v48): the subtitle keeps the title's 24h window — honesty moved from hiding a
# stale summary to rendering it with its age. A parked dispatch owner idles for hours
# between child harvests, so the old 15-minute cutoff blanked exactly the rows a user
# watches longest. Render owns the "live vs aged" distinction via the sidecar ts.
_FRESH_SUMMARY_SEC = _FRESH_SEC
_STALE_SWEEP_SEC = 7 * 24 * 3600
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_TITLE_TIME_UNSET = object()
TIMELINE_LINE_MAX = 64 * 1024
TIMELINE_KINDS = ("now", "prompt")
_TIMELINE_READ_MAX = 8 * 1024 * 1024


def previous_title(entity, timestamp, now=None):
    """A subject is previous only when its successful event time proves it."""
    now = time.time() if now is None else now
    if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
            or not math.isfinite(timestamp) or not 0 <= timestamp <= now):
        return False
    if now - timestamp > _FRESH_SEC:
        return True
    current = (getattr(entity, "route_chain", None) or {}).get("current") or {}
    boundary = current.get("ts")
    return (isinstance(boundary, (int, float)) and not isinstance(boundary, bool)
            and math.isfinite(boundary) and timestamp < boundary <= now)


def annotate(entities):
    """Carry the exact successful title's event time, across every harness.

    Never borrow a neighbouring sidecar or attach a time to different text.
    This passive metadata fill runs on the collection/detail worker, not render.
    """
    for entity in entities:
        entity.title_ts = None
        if not getattr(entity, "title", None):
            continue
        harness = getattr(entity, "harness", None)
        if harness not in ("claude", "codex", "opencode"):
            continue
        keys = (getattr(entity, "session_id", None),
                getattr(entity, "_runtime_session_id", None),
                attempt_sid(getattr(entity, "attempt_id", None)))
        for sid in dict.fromkeys(key for key in keys if key):
            data = read(sid, harness=harness) or {}
            # Legacy `ts` also advanced on no-op/failure writes. It cannot
            # establish when a title was actually generated successfully.
            ts = data.get("title_ts")
            if (data.get("title") == entity.title and isinstance(ts, (int, float))
                    and not isinstance(ts, bool) and math.isfinite(ts)):
                entity.title_ts = ts
                break


def _safe_key(value, label):
    value = str(value or "")
    if not value or not _SAFE_KEY_RE.fullmatch(value):     # `$` alone takes a final newline
        raise ValueError("invalid %s" % label)
    return value


def state_root():
    explicit = os.environ.get("FLEET_TITLE_STATE_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "agent-fleet", "titles")


def titles_dir(harness="claude"):
    return os.path.join(state_root(), _safe_key(harness, "harness"))


def sidecar_path(sid, harness="claude"):
    return os.path.join(titles_dir(harness), _safe_key(sid, "session id") + ".json")


def timeline_path(sid, harness="claude"):
    return os.path.join(titles_dir(harness), _safe_key(sid, "session id") + ".timeline.jsonl")


def lock_path(sid, harness="claude"):
    return os.path.join(titles_dir(harness), ".lock-" + _safe_key(sid, "session id"))


def attempt_sid(attempt_id):
    """Sidecar sid the dispatch summary owner (SD-95) writes for one exact
    registered attempt, or ``None`` for a missing/malformed attempt id.

    A registered worker session runs no statusline producer, so its own runtime
    sid never gains a sidecar; the attempt-owned record is the only producer.
    """
    if not isinstance(attempt_id, str) or not _SAFE_KEY_RE.fullmatch(attempt_id):
        return None
    return "dispatch-" + attempt_id


def _legacy_claude_path(sid):
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(home, ".fleet-titles", _safe_key(sid, "session id") + ".json")


def _read_path(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read(sid, harness="claude", allow_legacy=True):
    """Return a sidecar dict or ``None``; malformed/missing state is harmless."""
    if not sid:
        return None
    data = _read_path(sidecar_path(sid, harness=harness))
    if data is not None:
        return data
    if allow_legacy and harness == "claude":
        return _read_path(_legacy_claude_path(sid))
    return None


def fresh_title(sid, now=None, max_age=_FRESH_SEC, harness="claude"):
    """Return a non-empty title only while the sidecar timestamp is fresh."""
    data = read(sid, harness=harness)
    if not data:
        return None
    ts = data.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    now = time.time() if now is None else now
    if now - ts > max_age:
        return None
    title = data.get("title")
    return title if isinstance(title, str) and title.strip() else None


def last_title(sid, harness="claude"):
    """Keep the last successful subject even when refresh is unavailable or stale."""
    data = read(sid, harness=harness) or {}
    title = data.get("title")
    return title if isinstance(title, str) and title.strip() else None


def fresh_summary(sid, harness="claude", now=None, max_age=_FRESH_SUMMARY_SEC,
                  after_offset=None):
    """Return a non-empty summary only while the sidecar timestamp is fresh.

    Same shape as ``fresh_title``. Since F-63 the default window matches the
    title's — an aged summary renders with its elapsed time instead of being
    hidden. ``after_offset`` additionally requires the sidecar cursor to cover
    a newer source boundary.
    """
    return fresh_summary_with_ts(sid, harness=harness, now=now, max_age=max_age,
                                 after_offset=after_offset)[0]


def fresh_summary_with_ts(sid, harness="claude", now=None,
                          max_age=_FRESH_SUMMARY_SEC, after_offset=None):
    """``(summary, ts)`` under the same gates as ``fresh_summary``, else
    ``(None, None)``. The ts travels with the text so render can show its age
    (F-63) instead of the collector deciding staleness for the display layer."""
    data = read(sid, harness=harness)
    if not data:
        return (None, None)
    ts = data.get("summary_ts", data.get("ts"))
    if not isinstance(ts, (int, float)):
        return (None, None)
    now = time.time() if now is None else now
    if now - ts > max_age:
        return (None, None)
    if after_offset is not None:
        offset = data.get("offset")
        if (not isinstance(offset, int) or isinstance(offset, bool)
                or offset <= after_offset):
            return (None, None)
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        return (summary, ts)
    return (None, None)


def write(sid, title, source="refresher", offset=0, now=None, harness="claude", summary=None,
          summary_ts=None, cursor_kind=None, summary_failures=0, summary_error=None,
          title_ts=_TITLE_TIME_UNSET, requested_language=None):
    """Atomically write neutral fleet-owned state. ``title=''`` is allowed.

    ``summary`` is additive and omitted from the written dict when falsy — old
    readers see the same shape they always have.
    """
    write_ts = time.time() if now is None else now
    generated_at = write_ts if title_ts is _TITLE_TIME_UNSET else title_ts
    data = {
        "title": title or "",
        "title_ts": generated_at if (type(generated_at) in (int, float)
                                      and math.isfinite(generated_at)) else None,
        "ts": write_ts,
        "source": source,
        "offset": int(offset),
    }
    if cursor_kind:
        data["cursor_kind"] = str(cursor_kind)
    if isinstance(requested_language, str) and requested_language.strip():
        data["requested_language"] = "".join(ch for ch in requested_language if ch.isprintable())[:80]
    if summary:
        data["summary"] = summary
        data["summary_ts"] = (
            summary_ts
            if isinstance(summary_ts, (int, float)) and not isinstance(summary_ts, bool)
            else write_ts
        )
    if isinstance(summary_failures, int) and not isinstance(summary_failures, bool) and summary_failures > 0:
        data["summary_failures"] = summary_failures
    if isinstance(summary_error, str) and summary_error.strip():
        data["summary_error"] = "".join(ch for ch in summary_error if ch.isprintable())[:180]
    directory = titles_dir(harness)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=directory, prefix="." + _safe_key(sid, "session id"), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, sidecar_path(sid, harness=harness))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def append_timeline(sid, row, harness="claude", now=None):
    """Append one record to the session's timeline with a single ``O_APPEND`` write.

    ``row`` is a dict with a ``kind`` of ``now`` or ``prompt``; ``at`` defaults to now.
    Returns False — quietly — for a bad key or row, a line over ``TIMELINE_LINE_MAX``,
    or any I/O error: the record is a convenience and never fails its caller. The file
    is created ``0600`` like the sidecar.
    """
    try:
        if not isinstance(row, dict) or row.get("kind") not in TIMELINE_KINDS:
            return False
        record = dict(row)
        at = record.get("at")
        if type(at) not in (int, float) or not math.isfinite(at):
            record["at"] = time.time() if now is None else now
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > TIMELINE_LINE_MAX:
            return False
        path = timeline_path(sid, harness=harness)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # A link or a FIFO in its place is not followed or waited on: only a regular file is written.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            return os.write(fd, line) == len(line)
        finally:
            os.close(fd)
    except Exception:
        return False


def _timeline_record(raw):
    """One line's record; a line a half-finished write ran into keeps the whole record after it."""
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        pass
    start = raw.rfind(b'{"kind":"')
    if start <= 0:
        return None
    try:
        return json.loads(raw[start:].decode("utf-8"))
    except Exception:
        return None


def read_timeline(sid, harness="claude"):
    """The session's timeline records in file order, oldest first.

    Each item is a dict with a finite ``at``, a ``kind`` of ``now``/``prompt``, a string
    ``text`` and, when the writer knew them, the cursor fields (``from``/``to`` for a
    NOW, ``from`` for a prompt). A broken line, an unknown ``kind`` and a last line
    that never got its newline (a half-finished write) are skipped; a whole record written
    after a half-finished one on the same line is still read. A missing file or bad key
    reads as ``[]``. Only the last ``_TIMELINE_READ_MAX`` bytes are read.
    """
    try:
        path = timeline_path(sid, harness=harness)
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > _TIMELINE_READ_MAX:
                # One byte more: when it is a newline the window starts on a whole line.
                handle.seek(size - _TIMELINE_READ_MAX - 1)
                data = handle.read(_TIMELINE_READ_MAX + 1).partition(b"\n")[2]
            else:
                data = handle.read(_TIMELINE_READ_MAX)
    except Exception:
        return []
    lines = data.split(b"\n")
    lines.pop()  # the segment after the last newline is empty or half written
    rows = []
    for raw in lines:
        item = _timeline_record(raw)
        if item is None:
            continue
        if (not isinstance(item, dict) or item.get("kind") not in TIMELINE_KINDS
                or not isinstance(item.get("text"), str)):
            continue
        at = item.get("at")
        if type(at) not in (int, float) or not math.isfinite(at):
            continue
        rows.append(item)
    return rows


_TAG_SWEEP_SEC = 30 * 24 * 3600


def tags_dir(harness="claude"):
    """F-100a — sibling of the titles root, NOT under it: ``sweep()`` reaps title
    sidecars by mtime after 7 days, and a session tag is written once and then never
    touched, so a long-lived session would lose its tag on the first sweep."""
    return os.path.join(os.path.dirname(state_root()), "tags", _safe_key(harness, "harness"))


def tag_path(sid, harness="claude"):
    return os.path.join(tags_dir(harness), _safe_key(sid, "session id") + ".json")


def read_tag(sid, harness="claude"):
    """The remembered derived tag for ``sid`` or ``None`` (missing/malformed is harmless)."""
    if not sid:
        return None
    try:
        data = _read_path(tag_path(sid, harness=harness))
    except ValueError:
        return None
    tag = data.get("tag") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) and tag else None


def remember_tag(sid, tag, harness="claude", now=None):
    """Persist the derived tag ONCE per session (a rename replaces the runtime record's
    name, which is the only place the tag lives). Rewrites only when the stored value
    differs, so the steady-state cost is one small read per tick, never a write."""
    if not sid or not isinstance(tag, str) or not tag:
        return False
    if read_tag(sid, harness=harness) == tag:
        return False
    directory = tags_dir(harness)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=directory, prefix="." + _safe_key(sid, "session id"), suffix=".tmp"
        )
    except Exception:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"tag": tag, "ts": time.time() if now is None else now}, f)
        os.replace(tmp, tag_path(sid, harness=harness))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def sweep_tags(now=None, max_age=_TAG_SWEEP_SEC):
    """Reap tag records untouched for ``max_age`` (30 days — a session tag is written
    once, so mtime is first-sight time; no live interactive session runs that long)."""
    now = time.time() if now is None else now
    removed = 0
    root = os.path.join(os.path.dirname(state_root()), "tags")
    try:
        harnesses = os.listdir(root)
    except OSError:
        return 0
    for harness in harnesses:
        directory = os.path.join(root, harness)
        if not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not (name.endswith(".json") or name.endswith(".tmp")):
                continue
            path = os.path.join(directory, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
    return removed


def sweep(now=None, max_age=_STALE_SWEEP_SEC):
    """Delete stale neutral sidecars/temp files across harness namespaces (and, on its
    own 30-day window, the F-100a tag records beside them)."""
    now = time.time() if now is None else now
    removed = sweep_tags(now=now)
    try:
        harnesses = os.listdir(state_root())
    except OSError:
        return removed
    for harness in harnesses:
        directory = os.path.join(state_root(), harness)
        if not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not (name.endswith(".json") or name.endswith(".tmp")
                    or name.endswith(".timeline.jsonl")):
                continue
            path = os.path.join(directory, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
    return removed
