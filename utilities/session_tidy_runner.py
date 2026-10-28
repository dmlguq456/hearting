#!/usr/bin/env python3
"""session-tidy runner: queue, detached supervisor, registered memory worker, closed apply.

``session_tidy.py enqueue`` calls :func:`enqueue_item`, which writes a queue entry
and starts this file as a detached process (own session, no stdio), then returns at
once.  The detached runner:

1. waits for the server-wide ``runner.lock`` (queueing behind a running tidy, never
   refusing; after 30 minutes it leaves its entry ``queued`` and exits — the next
   scheduled tidy picks the entry up),
2. for every unfinished entry, oldest first: builds ``input_v1.json``, starts one
   *registered* headless worker through the adapter's checked dispatch wrapper,
   waits for its process to end (checked exact-identity state) and then for its output
   file, validates ``actions.json``, runs ``mem tidy-apply``, advances the watermarks and leaves one
   result line for the seat,
3. on any failure leaves the card and the watermarks as they were and leaves one
   failure line.  When the applier wrote, or may have written, part of the batch (its
   undo journal has finished or unconfirmed steps, or cannot be read) the line says so
   and carries ``mem tidy-undo <batch>`` instead of claiming the memory is untouched.  There is no operator step: the next
   scheduled tidy simply sees the same unprocessed conversation again (source keys and
   the duplicate check keep the writes that already landed from being made twice).

State (all under ``session_tidy.state_root()``): ``queue/<id>.json`` (+ ``<id>.pid``),
``runs/<batch>/{input_v1.json,actions.json,runner.log,undo.json,result.json}``,
``runner.lock``.

The worker's own files are the one exception: its sandbox allows the artifact root the
checked wrapper launches it with, not the state folder, so ``input_v1.json`` (a copy of the
exact bytes in ``runs/<batch>``), ``prompt.md`` and the one ``actions.json`` it writes live in
``<artifact root>/.runtime/session-tidy/<batch>/``.  The runner reads that file back (no symlink,
size-capped), validates it, copies the checked result into ``runs/<batch>`` for ``mem tidy-apply``,
and removes the conversation copy when the batch ends (the rest goes with the finished entry).
The worker is registered under the session's own git worktree; a session outside any git project
(``$HOME`` under a managed release, whose root is immutable and not a checkout) is registered under
the seat's home worktree, ``${XDG_DATA_HOME:-~/.local/share}/hearting/home-worktree``, made once
(:func:`home_worktree`); its artifact root is that folder's ``.agent_reports``.

Reading is tail first (``tidy_transcripts.read_pending``): the newest unread range's last whole
rows are the worker's input; only a successful apply removes that range from the unread ones, so a
long record's older front stays pending and a short result line says "일부만 읽음(범위)".  A finished
batch also refreshes the seat card's "참고할 기억" list (``session_tidy.update_memory_refs``).

Test hooks are honoured only inside a test root: ``HEARTING_TIDY_TEST_ROOT`` must
contain the state folder, and an injected executable must live under it.  They are
``HEARTING_TIDY_WORKER_CMD`` / ``HEARTING_TIDY_STATE_CMD`` / ``HEARTING_TIDY_PEER_STEWARD``
(replace the checked commands), ``HEARTING_TIDY_SLUG`` and the timing knobs in :data:`TUNABLES`.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from typing import Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import session_tidy as st  # noqa: E402
import tidy_decisions as td  # noqa: E402
import tidy_transcripts as tt  # noqa: E402

SCHEMA = 1
UNIT = "ops/session-tidy-memory"
CAPABILITY = "session-tidy"
MODEL_PROFILE = "balanced-deep"
MODEL_ROLE = "deep editor"

TUNABLES = {
    "LOCK_WAIT": 30 * 60.0,         # runner.lock wait cap; past it the entry stays queued
    "GOVERNOR_WAIT": 20 * 60.0,     # total retry window while the governor has no free seat
    "WORKER_WAIT": 25 * 60.0,       # wait for the worker's registry row to close
    "POLL": 5.0,
    "GOVERNOR_SLEEP_MIN": 5.0,
    "GOVERNOR_SLEEP_MAX": 120.0,
    "APPLY_TIMEOUT": 40 * 60.0,
}
SESSION_CHUNK_BYTES = 192 * 1024
TOTAL_CHUNK_BYTES = 512 * 1024
MAX_TARGETS = 6
CANDIDATE_MAX = 60
CANDIDATE_BODY_CHARS = 400
CARD_CONTEXT_CHARS = 3000
PENDING_DECISIONS_MAX = 20
DUPLICATE_SIGNAL_MAX = 5
ACTIONS_MAX_BYTES = 1024 * 1024
RELATED_MAX = 20
KEEP_FINISHED_DAYS = 14
KEEP_RUN_DAYS = 60
MAX_ITEMS_PER_RUN = 10

# The worker runs on the caller's own harness where that harness's checked wrapper
# accepts a route-free support tuple under a modeless capability; OpenCode's does not
# (its capability catalog has no mode line for one), so its seats use Claude's.
WORKER_HARNESS = {"claude": "claude", "codex": "codex", "opencode": "claude"}

UNFINISHED = ("queued", "waiting-lock", "assembling", "dispatching", "waiting-worker", "validating", "applying")
ENV_DROP_KEYS = ("AGENT_SESSION_ROLE", "FLEET_TITLE_REFRESH", "MEM_DISTILL", "CLAUDE_CODE_SESSION_ID",
                 "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID")
ENV_DROP_PREFIXES = ("AGENT_DISPATCH_", "OPENCODE_DISPATCH", "AGENT_ROUTE_", "AGENT_ARTIFACT_", "HERDR_")


class RunnerFailure(Exception):
    """One failed step; the message is the reason shown in the failure line."""


# ---------------------------------------------------------------------------
# Test hooks (valid only inside a test root) and small helpers
# ---------------------------------------------------------------------------

def test_root() -> Optional[Path]:
    raw = os.environ.get("HEARTING_TIDY_TEST_ROOT")
    if not raw or not os.path.isabs(raw):
        return None
    try:
        root = Path(raw).resolve()
        state = st.state_root().resolve()
    except OSError:
        return None
    return root if (state == root or root in state.parents) else None


def tunable(name: str) -> float:
    default = TUNABLES[name]
    if test_root() is None:
        return default
    try:
        return float(os.environ["HEARTING_TIDY_" + name])
    except (KeyError, ValueError):
        return default


def injected_executable(name: str) -> Optional[Path]:
    value, root = os.environ.get(name), test_root()
    if not value or root is None:
        return None
    path = Path(value).resolve()
    return path if root in path.parents and path.is_file() else None


def clean_env(environ=None) -> dict:
    """The environment of a detached tidy: no worker marker, session id or pane of the caller."""
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items() if k not in ENV_DROP_KEYS and not k.startswith(ENV_DROP_PREFIXES)}


def _now() -> float:
    return time.time()


def _queue_dir() -> Path:
    return st.state_root() / "queue"


def _runs_dir() -> Path:
    return st.state_root() / "runs"


def _item_path(qid: str) -> Path:
    return _queue_dir() / f"{qid}.json"


def new_id(now: Optional[float] = None) -> str:
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(_now() if now is None else now))
    return f"tidy-{stamp}-{secrets.token_hex(3)}"


def read_item(qid: str) -> Optional[dict]:
    data = st.read_json(_item_path(qid))
    return data if isinstance(data, dict) and data.get("id") == qid else None


def write_item(item: dict) -> None:
    st.atomic_write_json(_item_path(item["id"]), item)


def set_status(item: dict, status: str, **extra) -> None:
    item["status"] = status
    item["updated"] = _now()
    item.update(extra)
    write_item(item)


def list_items() -> list:
    try:
        names = sorted(_queue_dir().iterdir())
    except OSError:
        return []
    items = []
    for path in names:
        if path.suffix == ".json" and not path.name.startswith("."):
            data = st.read_json(path)
            if isinstance(data, dict) and data.get("id") and data.get("status"):
                items.append(data)
    return sorted(items, key=lambda i: (float(i.get("created", 0) or 0), i["id"]))


def _log(run_dir: Path, text: str) -> None:
    with contextlib.suppress(OSError, st.StateError):
        st.ensure_dir(run_dir)
        fd = os.open(run_dir / "runner.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(f"{st.iso_utc(_now())} {text}\n")


def _card_seat(item: dict) -> "st.Seat":
    """Restore the exact logical author beside the queue's unchanged physical seat."""
    seat = st.seat_from_dict(item["seat"])
    return st.session_seat(seat, item.get("harness") or seat.harness,
                           item.get("sid") or seat.sid)


# ---------------------------------------------------------------------------
# enqueue: write the entry, start the detached runner, return
# ---------------------------------------------------------------------------

def _existing_operation(operation_id: str) -> Optional[dict]:
    matches = [item for item in list_items() if item.get("operation_id") == operation_id]
    return min(matches, key=lambda item: (float(item.get("created", 0) or 0), item["id"])) if matches else None


def operation_queue_id(operation_id: str) -> str:
    """Return the stable queue identity shared with a compact reservation."""
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:32]
    return f"tidy-op-{digest}"


def _start_queued(item: dict) -> dict:
    """Best-effort launch; unfinished stages resume under the same queue/batch identity."""
    if item.get("status") not in UNFINISHED:
        return item
    pid = item.get("runner_pid")
    started = str(item.get("runner_pid_start") or "")
    if isinstance(pid, int) and pid > 0:
        try:
            current = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            current = current[current.rfind(")") + 2:].split()[19]
            if not started or current == started:
                return item
        except (OSError, IndexError, ValueError):
            pass
    try:
        pid = start_detached(item["id"])
        item.pop("runner_start_error", None)
        if pid:
            item["runner_pid"] = int(pid)
            try:
                raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
                item["runner_pid_start"] = raw[raw.rfind(")") + 2:].split()[19]
            except (OSError, IndexError, ValueError):
                item["runner_pid_start"] = ""
        write_item(item)
    except (OSError, subprocess.SubprocessError) as exc:
        item["runner_start_error"] = type(exc).__name__
        write_item(item)
    return item


def enqueue_item(seat: "st.Seat", harness: str, sid: str, cwd: str, transcript: str = "", *,
                 operation_id: Optional[str] = None, queue_id: Optional[str] = None,
                 start: bool = True) -> dict:
    """Durably enqueue one memory operation, optionally idempotent across hook retries.

    ``operation_id`` is used by automatic compact/idle events. Re-observing that event returns
    the same queue row and never creates a second apply. A saved queued row survives a detached
    runner launch failure so a later normal hook can start it again.
    """
    if operation_id:
        existing = _existing_operation(operation_id)
        if existing:
            return _start_queued(existing) if start else existing
    queue_seat = st.session_seat(seat, harness, sid)
    if queue_id is None and operation_id:
        queue_id = operation_queue_id(operation_id)
    if queue_id:
        existing = read_item(queue_id)
        if existing:
            if operation_id and existing.get("operation_id") not in (None, operation_id):
                raise RunnerFailure("queue-id-operation-mismatch")
            if operation_id and not existing.get("operation_id"):
                existing["operation_id"] = operation_id
                write_item(existing)
            return _start_queued(existing) if start else existing
    item = {"schema": SCHEMA, "id": queue_id or new_id(), "created": _now(), "status": "queued", "updated": _now(),
            "seat": queue_seat.as_dict(), "harness": harness, "sid": sid, "cwd": cwd, "transcript": transcript,
            "attempts": 0}
    if operation_id:
        item["operation_id"] = operation_id
    with contextlib.suppress(BaseException):
        card = st.read_latest_card(_card_seat(item))
        if card:
            item["card_generation"] = int(card.get("generation", 0) or 0)   # the card this batch was queued under
    write_item(item)
    return _start_queued(item) if start else item


def reconcile_queued(seat: "st.Seat", harness: str, sid: str) -> int:
    """Restart durable queued rows for this exact session after an earlier launch miss."""
    resumed = 0
    for item in list_items():
        if item.get("status") not in UNFINISHED or (item.get("harness"), item.get("sid")) != (harness, sid):
            continue
        if st.seat_state_key(_card_seat(item)) != st.seat_state_key(
                st.session_seat(seat, harness, sid)):
            continue
        before = item.get("runner_start_error")
        started = _start_queued(item)
        if before and not started.get("runner_start_error"):
            resumed += 1
    return resumed


def reconcile_reservation_queue(seat: "st.Seat") -> bool:
    """Restart the accepted queued operation carried by this seat's clear reservation."""
    try:
        import session_tidy_clear as clear
        req = clear.read_reservation(seat.key)
    except BaseException:
        return False
    if not req or (req.get("seat") or {}).get("key") != seat.key:
        return False
    qid = str(req.get("queue_id") or "")
    item = read_item(qid) if qid else None
    if (not item or (item.get("seat") or {}).get("key") != seat.key
            or item.get("operation_id") != req.get("operation_id")
            or (item.get("harness"), item.get("sid")) != (req.get("harness"), req.get("sid"))):
        return False
    before = str(item.get("runner_start_error") or "")
    started = _start_queued(item)
    return bool(before and not started.get("runner_start_error"))


def start_detached(qid: str) -> int:
    """Start ``run`` in its own session so neither /clear, a closing pane nor a harness
    process-group cleanup of the caller can take it down."""
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "run", "--queue-id", qid],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True, env=clean_env(), cwd=str(st.ensure_dir(st.state_root())))
    return proc.pid


# ---------------------------------------------------------------------------
# The server-wide lock
# ---------------------------------------------------------------------------

def acquire_runner_lock(wait: float) -> Optional[int]:
    path = st.state_root() / "runner.lock"
    st.ensure_dir(path.parent)
    st._reject_symlink(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.25)


# ---------------------------------------------------------------------------
# Input bundle
# ---------------------------------------------------------------------------

def _mem_cli(*args, cwd: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "tools" / "memory" / "mem.py"), *args], cwd=cwd,
                          env=clean_env(), capture_output=True, text=True, timeout=timeout)


_SNAP_LINE = re.compile(r"^\[([^\]\s]+)\](?: [^:]*)? :: (.*)$")


def parse_snapshot(text: str) -> tuple[list, list]:
    """(candidates, allowlist ids) from ``mem curate-snapshot`` output: durable + working only."""
    section, rows, ids = "", [], []
    for line in text.splitlines():
        if line.startswith("PROTECTED PENDING"):
            section = "pending"
        elif line.startswith("DURABLE"):
            section = "durable"
        elif line.startswith("WORKING"):
            section = "working"
        elif line.startswith("ORPHAN") or line.startswith("SIGNALS"):
            section = ""
        elif line.startswith("IDS:"):
            ids = line[4:].split()
        elif section in ("durable", "working"):
            match = _SNAP_LINE.match(line)
            if match:
                rows.append({"id": match.group(1), "tier": section, "headline": match.group(2).strip()})
    allowed = set(ids)
    return [r for r in rows if r["id"] in allowed], ids


def _excerpts(ids: list, cwd: str) -> dict:
    """First part of each record body, read without touching the records (best effort)."""
    out: dict = {}
    try:
        mem = td.load_mem()
        if not mem.DB.exists():
            return out
        con = mem.get_con()
        try:
            for rid in ids:
                row = mem._visible_record(con, rid)
                if row is not None:
                    _meta, body = mem._row_to_meta(row)
                    out[rid] = " ".join(str(body).split())[:CANDIDATE_BODY_CHARS]
        finally:
            con.close()
    except BaseException:  # noqa: BLE001 - excerpts are a nicety, never a reason to fail
        return out
    return out


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[0-9A-Za-z가-힣_]{3,}", text.lower())}


def existing_records(cwd: str, conversation: str) -> tuple[list, list]:
    """(related active records of this project, duplicate-group signals), both read only."""
    try:
        snap = _mem_cli("curate-snapshot", cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return [], []
    if snap.returncode != 0:
        return [], []
    rows, ids = parse_snapshot(snap.stdout)
    wanted = _tokens(conversation)
    scored = sorted(rows, key=lambda r: -len(wanted & _tokens(r["headline"])))[:CANDIDATE_MAX]
    bodies = _excerpts([r["id"] for r in scored], cwd)
    for row in scored:
        row["excerpt"] = bodies.get(row["id"], "")
    groups: list = []
    try:
        life = _mem_cli("lifecycle", cwd=cwd)
        allowed = set(ids)
        for line in life.stdout.splitlines():
            if "[dup-flag]" in line:
                try:
                    group = ast.literal_eval(line.split("[dup-flag]", 1)[1].split("  (", 1)[0].strip())
                except (ValueError, SyntaxError):
                    continue
                group = [g for g in group if isinstance(g, str) and g in allowed]
                if len(group) > 1:
                    groups.append(group)
    except (OSError, subprocess.SubprocessError):
        groups = []
    return scored, groups[:DUPLICATE_SIGNAL_MAX]


def _pending_decisions(project_key: str) -> list:
    out = []
    for _path, payload in td.list_pending():
        try:
            if st.project_key_for(payload.get("cwd") or "") != project_key:
                continue
        except BaseException:  # noqa: BLE001
            continue
        out.append({"source": payload.get("source"), "question": payload.get("question"),
                    "answers": payload.get("answers"), "note": payload.get("note", "")})
    return out[:PENDING_DECISIONS_MAX]


class Bundle:
    """The assembled input.  ``exchange`` (the worker's folder under the artifact root), ``allowed_ids``
    (what ``related_existing_ids`` may name), ``unread`` (ranges left unread) and ``related`` (the
    checked ids the worker named) are filled in later and default to "none"."""

    def __init__(self, path: Path, digest: str, cursors: list, empty: bool, unread: Optional[list] = None,
                 allowed_ids: Optional[set] = None):
        self.path, self.digest, self.cursors, self.empty = path, digest, cursors, empty
        self.unread = unread or []
        self.allowed_ids = allowed_ids or set()
        self.exchange: Optional[Path] = None
        self.related: list = []
        self.unreadable: list = []      # sessions whose record could not be read (never "nothing new")


def _target_sessions(item: dict, seat) -> list:
    caller = {"harness": item["harness"], "sid": item["sid"], "role": "caller",
              "transcript": item.get("transcript") or ""}
    if not caller["transcript"]:
        located = tt.locate_transcript(caller["harness"], caller["sid"])
        caller["transcript"] = str(located) if located else ""
    targets = [caller]
    try:
        recent = tt.select_recent_sessions(st.physical_seat(seat), cwd=item["cwd"])
    except BaseException:  # noqa: BLE001
        recent = []
    for row in recent:
        if (row["harness"], row["sid"]) != (caller["harness"], caller["sid"]):
            targets.append({"harness": row["harness"], "sid": row["sid"], "role": "recent",
                            "transcript": row.get("transcript") or ""})
    return targets[:MAX_TARGETS]


MAX_SILENT_HOPS = 16


def _unread_entry(target: dict, chunk) -> dict:
    return {"harness": target["harness"], "sid": target["sid"], "role": target["role"],
            "coverage": tt.describe_coverage(chunk), "blocked": chunk.blocked, "remaining": tt.unread_total(chunk)}


def assemble(item: dict, batch: str, run_dir: Path) -> Bundle:
    seat = st.physical_seat(st.seat_from_dict(item["seat"]))
    card_seat = _card_seat(item)
    project_key = st.project_key_for(item["cwd"])
    sessions, cursors, choices, skipped, unread = [], [], [], [], []
    remaining = TOTAL_CHUNK_BYTES
    for target in _target_sessions(item, seat):
        if remaining <= 0:
            break
        if not target["transcript"]:
            skipped.append({"harness": target["harness"], "sid": target["sid"], "reason": "no-record"})
            continue
        for hop in range(MAX_SILENT_HOPS + 1):
            chunk = tt.read_pending(target["harness"], target["sid"], target["transcript"],
                                    limit_bytes=min(SESSION_CHUNK_BYTES, remaining))
            if chunk.error:
                skipped.append({"harness": target["harness"], "sid": target["sid"], "reason": chunk.error})
                break
            if chunk.skipped_oversize:
                skipped.append({"harness": target["harness"], "sid": target["sid"], "reason": "oversize-row"})
            if chunk.cursor_to == chunk.cursor_from:
                if chunk.pending_after:                 # blocked at an open question: nothing readable yet
                    unread.append(_unread_entry(target, chunk))
                break                                   # (or nothing unread at all)
            if not chunk.text and not chunk.choices:
                # Nothing to tidy in this range (tool output only): it counts as read, then look further back.
                tt.mark_applied(target["harness"], target["sid"], chunk)
                if chunk.pending_after and hop < MAX_SILENT_HOPS:
                    continue
                if chunk.pending_after:
                    unread.append(_unread_entry(target, chunk))
                break
            remaining -= len(chunk.text.encode("utf-8"))
            sessions.append({"harness": target["harness"], "sid": target["sid"], "role": target["role"],
                             "unit": chunk.unit, "cursor_from": chunk.cursor_from, "cursor_to": chunk.cursor_to,
                             "total": chunk.total, "eof": chunk.eof, "pending_after": chunk.pending_after,
                             "rows": chunk.rows, "skipped_oversize": chunk.skipped_oversize,
                             "blocked": chunk.blocked, "text": chunk.text})
            cursors.append({"harness": target["harness"], "sid": target["sid"], "cursor": chunk.cursor_to,
                            "source": target["transcript"], "chunk": chunk})
            for choice in chunk.choices:
                choices.append({**choice, "session": target["sid"]})
            if chunk.pending_after:
                unread.append(_unread_entry(target, chunk))
            break
    pending = _pending_decisions(project_key)
    conversation = "\n".join(s["text"] for s in sessions)
    records, groups = existing_records(item["cwd"], conversation) if (sessions or pending or choices) else ([], [])
    card = st.read_latest_card(card_seat)
    doc = {"schema_version": 1, "batch_id": batch, "created": st.iso_utc(_now()), "cwd": item["cwd"],
           "project_key": project_key, "seat": {"kind": seat.kind, "key": seat.key},
           "caller": {"harness": item["harness"], "sid": item["sid"]},
           "card": (card or {}).get("body", "")[:CARD_CONTEXT_CHARS],
           "sessions": sessions, "skipped_sessions": skipped, "user_choices": choices,
           "pending_decisions": pending, "existing_records": records, "duplicate_groups": groups,
           "limits": {"max_new_records": 10, "max_duplicate_groups": DUPLICATE_SIGNAL_MAX}}
    path = run_dir / "input_v1.json"
    st.atomic_write_json(path, doc)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    empty = not (sessions or choices or pending)
    bundle = Bundle(path, digest, cursors, empty, unread, {r["id"] for r in records})
    bundle.unreadable = [s for s in skipped if str(s.get("reason", "")).startswith("unreadable")]
    return bundle


def coverage_note(bundle: Bundle) -> str:
    """"일부만 읽음(범위)" when part of a record was left unread, else ""; never reads as "nothing to tidy"."""
    if not bundle.unread:
        return ""
    first = bundle.unread[0]
    more = f" 외 기록 {len(bundle.unread) - 1}개도 일부만 읽음" if len(bundle.unread) > 1 else ""
    why = "열린 질문 앞까지, " if first.get("blocked") else ""
    return f"일부만 읽음({why}{first['coverage']}){more} — 앞부분은 다음 정리에서 계속"


# ---------------------------------------------------------------------------
# Registered worker
# ---------------------------------------------------------------------------

def write_prompt(run_dir: Path, batch: str, bundle: Bundle) -> Path:
    where = bundle.exchange or run_dir          # the worker's own folder when there is one
    actions = where / "actions.json"
    text = (
        "Session-tidy memory worker. Follow the unit instructions (ops/session-tidy-memory).\n\n"
        f"batch_id: {batch}\n"
        f"input (read only): {where / 'input_v1.json' if bundle.exchange else bundle.path}\n"
        f"input_digest: {bundle.digest}\n"
        f"output (write exactly this one file): {actions}\n\n"
        "Read the input, write actions.json (JSON only), then stop. Do not call any memory write command.\n\n"
        "Final message: exactly the three handoff lines with `artifact: -`, then `verdict: PASS` (or FAIL) and\n"
        "`blocker: none` (or one line). The output file above is a private handoff to the runner, not a\n"
        "durable artifact, so its path does not go in the artifact field.\n")
    if bundle.exchange:
        path = bundle.exchange / "prompt.md"
        _write_private(path, text.encode("utf-8"))
    else:
        path = run_dir / "prompt.md"
        st.atomic_write(path, text.encode("utf-8"))
    return path


HOME_WORKTREE_PARTS = ("hearting", "home-worktree")
HOME_WORKTREE_README = (
    "# hearting home worktree\n\n"
    "The stand-in git worktree for sessions that run outside any git project (for example in $HOME).\n"
    "The session-tidy memory worker is registered under it, and its exchange folder lives in\n"
    "`.agent_reports/.runtime/session-tidy/` here; everything but this file is ignored by git.\n"
    "Nothing else is written or committed. It is safe to delete; the next tidy makes it again.\n")
HOME_WORKTREE_IGNORE = "*\n!.gitignore\n!README.md\n"


def _is_git_worktree(path: str) -> bool:
    try:
        done = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and bool(done.stdout.strip())


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share"))


def home_worktree() -> str:
    """``${XDG_DATA_HOME:-~/.local/share}/hearting/home-worktree``: the seat's stand-in worktree for a
    session that runs outside any git project, when the harness root is not one either (a managed release
    is immutable and not a checkout).  Made once -- ``git init`` plus one commit, so the checked wrapper
    can resolve a top level and a HEAD -- and ignored by git below its README, so the worker's exchange
    folder under its artifact root never makes it dirty.  A folder that cannot be made ends the tidy with
    its one failure line."""
    path = data_home().joinpath(*HOME_WORKTREE_PARTS)
    if _is_git_worktree(str(path)):
        return str(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        git = ["git", "-C", str(path), "-c", "user.name=hearting", "-c", "user.email=hearting@localhost",
               "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
        subprocess.run(git + ["init", "-q"], check=True, capture_output=True, text=True, timeout=30)
        for name, text in (("README.md", HOME_WORKTREE_README), (".gitignore", HOME_WORKTREE_IGNORE)):
            if not (path / name).exists():
                (path / name).write_text(text, encoding="utf-8")
        subprocess.run(git + ["add", "--", "README.md", ".gitignore"], check=True, capture_output=True,
                       text=True, timeout=30)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "hearting home worktree"],
                       check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        detail = ((exc.stderr or exc.stdout or "").strip().splitlines() or ["git failed"])[-1][:60]
        raise RunnerFailure(f"cannot make the home worktree: {detail}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerFailure(f"cannot make the home worktree: {type(exc).__name__}") from exc
    if not _is_git_worktree(str(path)):
        raise RunnerFailure(f"cannot make the home worktree: not a git worktree after init: {path}")
    return str(path)


def _git_worktree(cwd: str) -> str:
    """The worktree the worker is registered under: the session's own project, else the harness checkout
    (dev activation), else the seat's home worktree (``home_worktree``)."""
    for candidate in (cwd, str(ROOT)):
        if _is_git_worktree(candidate):
            return candidate
    return home_worktree()


def launch_worktree(item: dict) -> str:
    """The ``--worktree`` the checked wrapper registers the worker under (it derives its artifact root from it)."""
    return item["cwd"] if injected_executable("HEARTING_TIDY_WORKER_CMD") else _git_worktree(item["cwd"])


def artifact_root_for(worktree: str) -> Path:
    """The artifact root the wrapper launches that worktree's worker with: the same ``artifact-root.sh``
    on the same worktree, in the same cleaned environment.  Nothing else is ever substituted; a root
    that cannot be resolved ends the tidy with its one failure line."""
    try:
        done = subprocess.run([str(ROOT / "utilities" / "artifact-root.sh"), worktree], env=clean_env(),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerFailure(f"no artifact root for the worker's files: {type(exc).__name__}") from exc
    value = done.stdout.strip()
    if done.returncode != 0 or not value or not os.path.isabs(value):
        detail = (done.stderr or done.stdout or "unresolved").strip().splitlines()[-1][:80]
        raise RunnerFailure(f"no artifact root for the worker's files: {detail}")
    return Path(value)


EXCHANGE_PARTS = (".runtime", "session-tidy")


def _write_private(path: Path, data: bytes) -> None:
    """Atomic 0600 write of a file in the worker's folder (outside the state root, so not ``st.atomic_write``)."""
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def prepare_exchange(item: dict, batch: str, bundle: Bundle) -> Path:
    """``<artifact root>/.runtime/session-tidy/<batch>/`` (0700) holding an exact copy of the input.

    The folder is where the worker may read and write; the state folder is not.  A symlink on the way
    is refused.  The root is created when the project has none yet (the wrapper's own launch needs it too).
    """
    root = artifact_root_for(launch_worktree(item))
    try:
        root.mkdir(parents=True, exist_ok=True)
        current = root
        for part in (*EXCHANGE_PARTS, batch):
            current = current / part
            if current.is_symlink():
                raise RunnerFailure(f"the worker's folder is a symlink: {current}")
            current.mkdir(mode=0o700, exist_ok=True)
            if part != EXCHANGE_PARTS[0]:
                os.chmod(current, 0o700)
        _write_private(current / "input_v1.json", bundle.path.read_bytes())
    except OSError as exc:
        raise RunnerFailure(f"cannot prepare the worker's folder: {type(exc).__name__}") from exc
    bundle.exchange = current
    return current


def remove_exchange(path, only_conversation: bool = False) -> None:
    """Delete a batch's worker folder (or just the conversation copy and the prompt) if it is the real one."""
    if not path:
        return
    folder = Path(path)
    if folder.parent.name != EXCHANGE_PARTS[1] or folder.parent.parent.name != EXCHANGE_PARTS[0] \
            or folder.is_symlink() or not folder.is_dir():
        return
    if only_conversation:
        for name in ("input_v1.json", "prompt.md"):
            with contextlib.suppress(OSError):
                os.unlink(folder / name)
    else:
        shutil.rmtree(folder, ignore_errors=True)


def launch_command(item: dict, prompt: Path, slug: str) -> list:
    injected = injected_executable("HEARTING_TIDY_WORKER_CMD")
    harness = WORKER_HARNESS.get(item["harness"], "claude")
    if injected:
        base = [str(injected)]
    else:
        base = [sys.executable, str(ROOT / "adapters" / harness / "bin" / "dispatch-headless.py")]
    return base + [
        "--start", "--worktree", launch_worktree(item),
        "--slug", slug, "--capability", CAPABILITY, "--capability-mode", "default",
        "--intensity", "standard", "--dispatch-depth", "1", "--worker-type", "support",
        "--unit", UNIT, "--owner", CAPABILITY, "--assigned-contract", "session-tidy-memory",
        "--model-profile", MODEL_PROFILE, "--model-role", MODEL_ROLE, "--prompt-file", str(prompt)]


def parse_receipt(text: str) -> dict:
    fields: dict = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith(("#", " ")):
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    return fields


def dispatch_worker(item: dict, batch: str, run_dir: Path, bundle: Bundle) -> dict:
    prompt = write_prompt(run_dir, batch, bundle)
    slug = os.environ.get("HEARTING_TIDY_SLUG") if test_root() else None
    slug = re.sub(r"[^a-z0-9-]", "-", (slug or f"session-{batch}").lower())[:64]
    command = launch_command(item, prompt, slug)
    deadline = time.monotonic() + tunable("GOVERNOR_WAIT")
    attempt = 0
    while True:
        try:
            done = subprocess.run(command, env=clean_env(), cwd=str(run_dir), capture_output=True,
                                  text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerFailure(f"worker launch failed: {exc}") from exc
        receipt = parse_receipt(done.stdout)
        _log(run_dir, f"launch rc={done.returncode} reason={receipt.get('reason', '-')} "
                      f"registered={receipt.get('registered', '-')} started={receipt.get('started', '-')}")
        if (done.returncode == 0 and receipt.get("registered") == "1" and receipt.get("started") == "1"
                and receipt.get("child_spawned") == "1" and receipt.get("attempt_id", "-") != "-"):
            record_caller_observation(item, receipt)
            return receipt
        reason = receipt.get("reason") or (done.stderr.strip().splitlines() or ["unknown"])[-1]
        if reason == "model-worker-governor-denied" or receipt.get("retryable") == "1":
            if time.monotonic() >= deadline:
                raise RunnerFailure("no free worker seat within the wait limit (governor)")
            try:
                hint = float(receipt.get("retry_after_seconds", ""))
            except ValueError:
                hint = 15.0 * (2 ** min(attempt, 4))
            time.sleep(min(max(hint, tunable("GOVERNOR_SLEEP_MIN")), tunable("GOVERNOR_SLEEP_MAX"),
                           max(0.0, deadline - time.monotonic())))
            attempt += 1
            continue
        raise RunnerFailure(f"worker was not started: {reason}")


def record_caller_observation(item: dict, receipt: dict) -> None:
    """Publish the known caller without granting parent/delivery authority.

    Failure to publish display metadata must not fail a successfully launched tidy.
    The exact attempt annotation leaves all execution and completion fields alone.
    """
    from dispatch_contract import annotate_attempt_row
    if not receipt.get("job_registry") or not receipt.get("attempt_id"):
        return
    values = {"caller_harness": item.get("harness") or "", "caller_sid": item.get("sid") or "",
              "caller_pane": (item.get("seat") or {}).get("pane") or "",
              "caller_cwd": item.get("cwd") or ""}
    try:
        annotate_attempt_row(Path(receipt["job_registry"]), receipt["attempt_id"], values)
    except (OSError, ValueError):
        pass


def attempt_state(receipt: dict) -> str:
    """The worker's process state from the checked exact-identity classifier
    (``dispatch-registry.py attempt-state``): ``working``, ``dead``, ``done`` or ``unknown``."""
    injected = injected_executable("HEARTING_TIDY_STATE_CMD")
    base = [str(injected)] if injected else [sys.executable, str(ROOT / "utilities" / "dispatch-registry.py")]
    command = base + ["--jobs", receipt["job_registry"], "--attempt", receipt["attempt_id"],
                      "--pid", receipt.get("child_pid", ""), "--pid-start", receipt.get("child_pid_start", ""),
                      "attempt-state"]
    try:
        done = subprocess.run(command, env=clean_env(), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    for line in done.stdout.splitlines():
        if line.startswith("state="):
            return line.partition("=")[2].strip() or "unknown"
    return "unknown"


def wait_worker(receipt: dict, run_dir: Path, actions: Optional[Path] = None) -> None:
    """Wait until the registered worker's process is gone (checked exact-identity state, polled
    with a limit), then require its output file.  The model's stdout is never read.

    A route-free support worker has no terminal commit, so its registry row is not closed by
    the checked join (``settle_finished_attempt`` answers ``not-route-bound``); the process
    state is the end signal and ``actions.json`` (``actions``, else the one in ``run_dir``) is the result.
    """
    if not (receipt.get("job_registry") and receipt.get("child_pid") and receipt.get("child_pid_start")):
        raise RunnerFailure("the launch receipt carries no exact worker identity")
    deadline = time.monotonic() + tunable("WORKER_WAIT")
    while True:
        state = attempt_state(receipt)
        if state in ("dead", "done"):
            break
        if time.monotonic() >= deadline:
            raise RunnerFailure("worker did not finish within the wait limit")
        time.sleep(tunable("POLL"))
    _log(run_dir, f"worker process state={state}")
    if not (actions or run_dir / "actions.json").is_file():
        raise RunnerFailure("worker finished without writing actions.json")


# ---------------------------------------------------------------------------
# actions.json check, apply, finish
# ---------------------------------------------------------------------------

def _read_worker_file(path: Path, limit: int) -> bytes:
    """The worker's output, read without following a symlink and only if it is a regular file of sane size."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise RunnerFailure(f"actions.json cannot be read: {type(exc).__name__}") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RunnerFailure("actions.json is not a regular file")
        if info.st_size > limit:
            raise RunnerFailure("actions.json is too large")
        return handle.read(limit + 1)


def validate_actions(run_dir: Path, batch: str, bundle: Bundle) -> None:
    """Check the worker's ``actions.json`` (in its folder when there is one) and leave the checked
    result in ``run_dir`` for ``mem tidy-apply``; the worker's copy is never what gets applied."""
    source = (bundle.exchange / "actions.json") if bundle.exchange else run_dir / "actions.json"
    raw = _read_worker_file(source, ACTIONS_MAX_BYTES)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RunnerFailure(f"actions.json is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("actions"), list):
        raise RunnerFailure("actions.json must be an object with an actions list")
    if any(not isinstance(a, dict) for a in doc["actions"]):
        raise RunnerFailure("every action must be an object")
    # The runner owns the identity of the batch; the worker's copies are not trusted.
    doc["schema_version"], doc["batch_id"], doc["input_digest"] = 1, batch, bundle.digest
    # Optional: which existing records of this project the conversation relates to (ids only).
    named = doc.get("related_existing_ids")
    related: list = []
    for rid in named if isinstance(named, list) else []:
        if isinstance(rid, str) and rid in bundle.allowed_ids and rid not in related:
            related.append(rid)
    doc["related_existing_ids"] = related[:RELATED_MAX]
    bundle.related = doc["related_existing_ids"]
    st.atomic_write_json(run_dir / "actions.json", doc)


def apply_actions(item: dict, run_dir: Path, bundle: Bundle) -> str:
    try:
        done = _mem_cli("tidy-apply", str(run_dir / "actions.json"), "--input", str(bundle.path),
                        "--cwd", item["cwd"], cwd=item["cwd"], timeout=tunable("APPLY_TIMEOUT"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerFailure(f"apply {'stopped' if any(applied_writes(run_dir)) else 'did not run'}: {exc}") from exc
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if ln.startswith("[tidy]")), "")
    _log(run_dir, f"apply rc={done.returncode} {summary or (done.stderr.strip().splitlines() or [''])[-1]}")
    if done.returncode != 0:
        detail = summary or (done.stderr.strip().splitlines() or ["apply failed"])[-1]
        raise RunnerFailure(detail.removeprefix("[tidy]").strip())
    return summary or "[tidy] 정리를 마쳤습니다."


def advance_watermarks(bundle: Bundle) -> None:
    """Take the ranges this batch covered off the unread ones (and nothing more)."""
    for entry in bundle.cursors:
        if entry.get("chunk") is not None:
            tt.mark_applied(entry["harness"], entry["sid"], entry["chunk"])
        else:
            tt.write_watermark(entry["harness"], entry["sid"], entry["cursor"], source=entry["source"])


def notify(item: dict, text: str) -> None:
    with contextlib.suppress(BaseException):
        stable = str(item.get("id") or item.get("operation_id") or "")
        st.write_notice(_card_seat(item), text, author_harness=item["harness"],
                        author_sid=item["sid"], notice_id=f"tidy:{stable}" if stable else "")


def applied_writes(run_dir: Path) -> tuple:
    """``(finished, unconfirmed)`` writes of this batch, read from its undo journal.

    The applier records each step before taking it, so no journal means nothing was
    written: ``(0, 0)``.  A step left as ``intent`` may have landed before the journal
    could say so, and an unreadable journal is ``(0, -1)`` -- both unknown, never
    "untouched"; ``mem tidy-undo`` checks such steps against the store itself.
    """
    path = run_dir / "undo.json"
    if not os.path.lexists(path):
        return 0, 0
    try:
        ops = st.read_json(path)["ops"]
        return (sum(1 for op in ops if op.get("state") == "done"),
                sum(1 for op in ops if op.get("state") == "intent"))
    except Exception:  # noqa: BLE001 - an unreadable journal is "unknown", not "nothing"
        return 0, -1


def journal_ids(run_dir: Path) -> list:
    """``[(id, kind)]`` of what this batch's undo journal says really landed, new and changed first."""
    doc = st.read_json(run_dir / "undo.json")
    ops = [op for op in (doc.get("ops") if isinstance(doc, dict) else None) or []
           if isinstance(op, dict) and op.get("state") == "done"]
    out = [(op["id"], "새") for op in ops if op.get("op") == "add" and op.get("id")]
    out += [(op["new_id"], "갱신") for op in ops if op.get("op") == "supersede" and op.get("new_id")]
    out += [(op["id"], "강화") for op in ops if op.get("op") == "reinforce" and op.get("id")]
    return out


def _current_headlines(ids: list) -> dict:
    """``{id: headline}`` of the active records among ``ids`` (read only; best effort, never raises)."""
    out: dict = {}
    if not ids:
        return out
    try:
        mem = td.load_mem()
        if not mem.DB.exists():
            return out
        con = mem.get_con()
        try:
            for rid in ids:
                row = con.execute("SELECT headline, status FROM records WHERE id=?", (rid,)).fetchone()
                if row and row[1] == "active":
                    out[rid] = " ".join(str(row[0] or "").split())[:120]
        finally:
            con.close()
    except BaseException:  # noqa: BLE001 - the list is a nicety, never a reason to fail the tidy
        return out
    return out


def memory_refs(run_dir: Path, bundle: Optional[Bundle]) -> tuple:
    """``(refs, more)``: records this batch touched (from the journal, so only what landed) first,
    then the existing records the worker named as related; at most 8 and 1,200 UTF-8 bytes."""
    entries = list(journal_ids(run_dir))
    seen = {rid for rid, _ in entries}
    entries += [(rid, "관련") for rid in (bundle.related if bundle else []) if rid not in seen]
    heads = _current_headlines([rid for rid, _ in entries])
    refs, used, total = [], 0, 0
    for rid, kind in entries:
        if rid not in heads:
            continue
        total += 1
        line = f"- {rid} [{kind}] {heads[rid]}"
        size = len(line.encode("utf-8")) + 1
        if len(refs) < st.MEMORY_REFS_MAX and used + size <= st.MEMORY_REFS_BYTES:
            refs.append({"id": rid, "kind": kind, "headline": heads[rid]})
            used += size
    return refs, total - len(refs)


def publish_memory(item: dict, batch: str, run_dir: Path, bundle: Optional[Bundle], status: str,
                   detail: str = "") -> None:
    """Refresh the seat card's "참고할 기억" list (never touches the card itself); best effort."""
    with contextlib.suppress(BaseException):
        refs, more = memory_refs(run_dir, bundle) if status != "failed" or journal_ids(run_dir) else ([], 0)
        st.update_memory_refs(_card_seat(item), batch=batch, status=status, refs=refs, more=more,
                              coverage=coverage_note(bundle) if bundle else "", detail=detail,
                              result_path=str(run_dir / "result.json"),
                              source_generation=item.get("card_generation"))


def failure_line(reason: str, batch: str = "", applied: tuple = (0, 0)) -> str:
    reason = " ".join(str(reason).split(" — 되돌리기")[0].split())[:80]
    done, unconfirmed = applied
    if batch and (done or unconfirmed):
        # Something may have landed: say so and keep the undo command; never claim memory is untouched.
        landed = ("반영 여부를 확인하지 못했습니다" if unconfirmed < 0
                  else f"{done}건 반영" + (f", {unconfirmed}건은 반영 여부 불명" if unconfirmed else ""))
        return (f"[정리] 기억 정리가 중간에 멈췄습니다({landed}). 나머지는 다음 정리 때 이어서 처리됩니다. "
                f"(사유: {reason}) — 되돌리기: mem tidy-undo {batch}")
    return f"[정리] 기억 정리를 끝내지 못했습니다. 카드와 기존 기억은 그대로이고 다음 정리 때 이어서 처리됩니다. (사유: {reason})"


def _with_coverage(line: str, note: str) -> str:
    """Put the coverage note before the undo command so a cut never takes the undo away."""
    if not note:
        return line
    head, sep, undo = line.partition(" — 되돌리기")
    return f"{head} · {note}{sep}{undo}"


def process_item(item: dict) -> None:
    attempts = int(item.get("attempts", 0))
    # An interrupted operation keeps the same tidy-apply batch. The apply journal and
    # source keys then make replay after an unknown completion idempotent.
    batch = str(item.get("batch") or item["id"])
    item["attempts"] = attempts + 1
    run_dir = st.ensure_dir(_runs_dir() / batch)
    set_status(item, "assembling", batch=batch)
    bundle = None
    finished = False
    try:
        bundle = assemble(item, batch, run_dir)
        if bundle.empty and bundle.unreadable:
            # A record that could not be read says nothing about what is in it.
            raise RunnerFailure("대화 기록을 읽지 못했습니다(" + str(bundle.unreadable[0].get("reason", "unreadable")) + ")")
        if bundle.empty:
            # The notice goes first: a terminal status means "the notice was left".
            note = coverage_note(bundle)
            # Unread ranges left means the read part had nothing to tidy, never "nothing new".
            notify(item, f"[정리] 읽은 범위에는 정리할 대화가 없었습니다 · {note}" if note
                   else "[정리] 새로 정리할 대화가 없습니다.")
            set_status(item, "notified", result="nothing-in-range" if note else "nothing-new")
            finished = True
            return
        set_status(item, "dispatching", exchange=str(prepare_exchange(item, batch, bundle)))
        receipt = dispatch_worker(item, batch, run_dir, bundle)
        set_status(item, "waiting-worker", attempt_id=receipt["attempt_id"])
        wait_worker(receipt, run_dir, bundle.exchange / "actions.json")
        set_status(item, "validating")
        validate_actions(run_dir, batch, bundle)
        set_status(item, "applying")
        line = apply_actions(item, run_dir, bundle)
        result = st.read_json(run_dir / "result.json")
        over = int(result.get("discarded_over_budget") or 0) if isinstance(result, dict) else 0
        if over:
            # Proposals beyond the batch's ten were dropped: that conversation is read again next time.
            line = _with_coverage(line, "상한을 넘은 제안은 다음 정리에서 다시 봅니다")
        else:
            advance_watermarks(bundle)
            line = _with_coverage(line, coverage_note(bundle))
        publish_memory(item, batch, run_dir, bundle, "applied" if not over else "partial",
                       "상한 초과 제안 폐기" if over else "")
        notify(item, line)
        set_status(item, "notified", result="applied")
        finished = True
    except RunnerFailure as exc:
        _log(run_dir, f"failed: {exc}")
        applied = applied_writes(run_dir)
        publish_memory(item, batch, run_dir, bundle, "failed", str(exc)[:80])
        notify(item, failure_line(str(exc), batch, applied))
        set_status(item, "failed", error=str(exc), applied=list(applied))
    except BaseException as exc:  # noqa: BLE001 - nothing may leave an entry half-done
        _log(run_dir, f"failed: internal {type(exc).__name__}: {exc}")
        applied = applied_writes(run_dir)
        publish_memory(item, batch, run_dir, bundle, "failed", f"internal {type(exc).__name__}")
        notify(item, failure_line(f"internal {type(exc).__name__}", batch, applied))
        set_status(item, "failed", error=f"internal {type(exc).__name__}", applied=list(applied))
    finally:
        for name in ("input_v1.json", "prompt.md"):
            with contextlib.suppress(OSError):
                os.unlink(run_dir / name)
        # The conversation copy and the prompt go in every case; the rest of the worker's folder goes
        # with a clean finish (a failed batch keeps its actions.json until the entry is pruned).
        remove_exchange(item.get("exchange"), only_conversation=not finished)
        if finished:
            remove_exchange(item.get("exchange"))


def prune_finished() -> None:
    cutoff = _now() - KEEP_FINISHED_DAYS * 86400
    for item in list_items():
        if item["status"] in UNFINISHED or float(item.get("updated", _now()) or 0) >= cutoff:
            continue
        remove_exchange(item.get("exchange"))
        for suffix in (".json", ".pid"):
            with contextlib.suppress(OSError):
                os.unlink(_queue_dir() / f"{item['id']}{suffix}")
    run_cutoff = _now() - KEEP_RUN_DAYS * 86400
    with contextlib.suppress(OSError):
        for path in _runs_dir().iterdir():
            if path.is_dir() and not path.is_symlink() and path.stat().st_mtime < run_cutoff:
                shutil.rmtree(path, ignore_errors=True)


def run(qid: str) -> int:
    st.atomic_write(_queue_dir() / f"{qid}.pid", f"{os.getpid()}\n".encode("ascii"))
    fd = acquire_runner_lock(tunable("LOCK_WAIT"))
    if fd is None:
        return 0        # the entry stays queued; the next tidy continues from the queue
    try:
        for _ in range(MAX_ITEMS_PER_RUN):
            todo = [i for i in list_items() if i["status"] in UNFINISHED]
            if not todo:
                break
            process_item(todo[0])
        prune_finished()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return 0


# ---------------------------------------------------------------------------
# handoff: deliver the card to a peer session through peer-steward only
# ---------------------------------------------------------------------------

def _handoff_steward():
    import importlib.util
    spec = importlib.util.spec_from_file_location("tidy_handoff_steward", HERE / "peer-steward.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_handoff_receiver(row, caller, *, steward=None):
    """Read-only check shared by receiver guidance and explicit acceptance."""
    try:
        seat, harness, sid = caller
    except (TypeError, ValueError):
        raise ValueError("handoff-successor-mismatch") from None
    steward = steward or _handoff_steward()
    peer = steward.peer_message
    context, predecessor, successor, requester, target = _handoff_context(row, peer=peer)
    recipient = peer.pending_delivery_recipient(row)
    if (not isinstance(recipient, dict)
            or (recipient.get("harness"), recipient.get("session_id")) != (harness, sid)
            or (successor.get("harness"), successor.get("session_id")) != (harness, sid)):
        raise ValueError("handoff-successor-mismatch")
    def valid_pane(value):
        return (isinstance(value, str) and 0 < len(value) <= 512
                and not any(ord(ch) < 32 for ch in value))
    def valid_server(value):
        return isinstance(value, str) and bool(value) and not any(ord(ch) < 32 for ch in value)

    if (not isinstance(predecessor, dict) or not isinstance(successor, dict)
            or not isinstance(requester, dict) or not valid_pane(target)
            or not valid_pane(predecessor.get("pane"))
            or not valid_server(predecessor.get("server"))
            or predecessor.get("pane") != target
            or (predecessor.get("harness"), predecessor.get("session_id"))
               != ((row.get("from") or {}).get("harness"), (row.get("from") or {}).get("session_id"))
            or not peer._valid_transfer_endpoint({"harness": predecessor.get("harness"),
                                                  "session_id": predecessor.get("session_id")})
            or not valid_pane(successor.get("pane"))
            or not valid_server(successor.get("server"))
            or (successor.get("harness"), successor.get("session_id")) != (harness, sid)
            or successor.get("pane") == predecessor.get("pane")
            or not peer._valid_transfer_endpoint({"harness": successor.get("harness"),
                                                  "session_id": successor.get("session_id")})
            or (requester.get("harness"), requester.get("session_id"),
                requester.get("pane"), requester.get("server"))
               != (successor.get("harness"), successor.get("session_id"),
                   successor.get("pane"), successor.get("server"))):
        raise ValueError("handoff-context-invalid")

    if not steward._handoff_lifetime_valid(predecessor.get("foreground")):
        raise ValueError("handoff-context-invalid")

    if (getattr(seat, "kind", None) != "pane" or seat.pane != successor["pane"]
            or (steward._HERDR_SESSION or "default") != successor["server"]):
        raise ValueError("handoff-successor-seat-mismatch")
    if not steward._handoff_lifetime_valid(successor.get("foreground")):
        raise ValueError("handoff-successor-lifetime-unverified")
    old_server = steward._HERDR_SESSION
    steward._HERDR_SESSION = None if successor["server"] == "default" else successor["server"]
    try:
        target_observation = steward._retire_target(successor["pane"])
        current, reason = steward._prompt_current_candidate(
            successor["pane"], expected_harness=harness, expected_sid=sid,
            require_process_session=True, target_observation=target_observation)
        if current is None:
            raise ValueError(reason or "handoff-successor-lifetime-unverified")
        foreground = steward._retire_foreground(
            successor["pane"], harness, strict=False, info=current["_pane_info"])
        observed_identity = dict(current["_identity"], session_id=current["session_id"])
        observed = steward._retire_request_identity(observed_identity, foreground)
    except (KeyError, TypeError, ValueError, OSError):
        raise ValueError("handoff-successor-lifetime-unverified") from None
    finally:
        steward._HERDR_SESSION = old_server
    for identity in (successor, observed):
        foreground = identity.get("foreground")
        if (not steward._handoff_lifetime_valid(foreground)
                or not steward._pid_identity_ok(foreground["pid"], foreground["start"])):
            raise ValueError("handoff-successor-lifetime-unverified")
    if any(observed.get(k) != successor.get(k)
           for k in ("server", "pane", "harness", "session_id", "foreground")):
        raise ValueError("handoff-successor-lifetime-mismatch")
    return context, successor


def _handoff_context(row, *, peer):
    """Validate the stored typed relation without observing a current pane."""
    context = row.get("handoff") if isinstance(row, dict) else None
    original_from = row.get("from") if isinstance(row, dict) else None
    original_to = row.get("to") if isinstance(row, dict) else None
    if not isinstance(context, dict) or not isinstance(original_from, dict) or not isinstance(original_to, dict):
        raise ValueError("handoff-successor-mismatch")
    predecessor = context.get("predecessor")
    successor = context.get("successor")
    requester = context.get("requester")
    target = context.get("target")
    if not isinstance(predecessor, dict) or not isinstance(successor, dict) or not isinstance(requester, dict):
        raise ValueError("handoff-context-invalid")
    def valid_pane(value):
        return (isinstance(value, str) and 0 < len(value) <= 512
                and not any(ord(ch) < 32 for ch in value))
    def valid_server(value):
        return isinstance(value, str) and bool(value) and not any(ord(ch) < 32 for ch in value)
    pane_bound = peer._valid_pane_bound_endpoint(original_to)
    if peer._valid_transfer_endpoint(original_to):
        transfer_recipient = original_to
    elif pane_bound:
        transfer_recipient = peer.pending_delivery_recipient(row)
        proof = peer.pending_first_input_process(row)
        foreground = successor.get("foreground") or {}
        if (not isinstance(transfer_recipient, dict) or proof is None
                or proof.get("harness") != original_to.get("harness")
                or proof.get("pane_id") != original_to.get("pane_id")
                or transfer_recipient.get("pane_id") != original_to.get("pane_id")
                or (foreground.get("pid"), str(foreground.get("start") or ""))
                   != (proof.get("pid"), proof.get("start"))):
            raise ValueError("handoff-context-invalid")
    else:
        raise ValueError("handoff-successor-mismatch")
    if (not valid_pane(target) or not valid_pane(predecessor.get("pane"))
            or not valid_server(predecessor.get("server"))
            or predecessor.get("pane") != target
            or not valid_pane(successor.get("pane"))
            or not valid_server(successor.get("server"))
            or successor.get("pane") == predecessor.get("pane")
            or not peer._valid_transfer_endpoint(original_from)
            or not isinstance(transfer_recipient, dict)
            or not peer._valid_transfer_endpoint(transfer_recipient)
            or (predecessor.get("harness"), predecessor.get("session_id"))
               != (original_from.get("harness"), original_from.get("session_id"))
            or (successor.get("harness"), successor.get("session_id"))
               != (transfer_recipient.get("harness"), transfer_recipient.get("session_id"))
            or (requester.get("harness"), requester.get("session_id"),
                requester.get("pane"), requester.get("server"))
               != (successor.get("harness"), successor.get("session_id"),
                   successor.get("pane"), successor.get("server"))):
        raise ValueError("handoff-context-invalid")
    if pane_bound and (successor.get("harness"), successor.get("pane")) != (
            transfer_recipient.get("harness"), transfer_recipient.get("pane_id")):
        raise ValueError("handoff-context-invalid")
    return context, predecessor, successor, requester, target


def _settle_accepted_handoff(ref, row, context, successor, *, steward):
    """Replay the existing accepted row through its writer, reply, and retire duties."""
    peer = steward.peer_message
    acceptance = dict(row.get("acceptance") or {})
    if acceptance.get("read_and_accepted") is not True:
        raise ValueError("handoff-acceptance-not-recorded")
    if (not steward._handoff_lifetime_valid(context["predecessor"].get("foreground"))
            or not steward._handoff_lifetime_valid(successor.get("foreground"))):
        raise ValueError("handoff-context-invalid")
    if steward._schedule_message_obligation(row, successor["pane"], successor["pane"],
                                           handoff_acceptance=True) is None:
        raise ValueError("handoff-acceptance-continuation-unavailable")

    import dispatch_seat_handover as handover
    seat_env = steward._herdr_env_for_server(successor["server"])
    successor_seat = st._pane_seat_of(
        successor["pane"], endpoint=st._endpoint_scope(seat_env),
        socket_path=seat_env.get("HERDR_SOCKET_PATH", ""))
    if (getattr(successor_seat, "kind", None) != "pane"
            or successor_seat.pane != successor["pane"]):
        raise ValueError("handoff-successor-seat-mismatch")
    try:
        handover.record_retire_handover(
            predecessor_sid=context["predecessor"]["session_id"],
            predecessor_harness=context["predecessor"]["harness"],
            successor_sid=successor["session_id"], successor_harness=successor["harness"],
            env=os.environ, successor_seat=successor_seat)
    except handover.RegistryUnreadable as exc:
        # The acceptance itself is already durable. Its binding handover is
        # unknown, not empty, so leave the continuation unsettled: the existing
        # accepted-duty replay retries it on the next observation.
        raise ValueError("handoff-registry-unreadable") from exc

    acceptance["reply_ref"] = peer.schedule_acceptance_reply(ref, row["to"], row["from"])
    row["acceptance"] = acceptance
    peer._save_pending(row)
    reply = peer._read_pending(acceptance["reply_ref"])
    if steward._schedule_message_obligation(reply, context["target"], context["predecessor"]["pane"]) is None:
        raise ValueError("handoff-reply-continuation-unavailable")
    acceptance["retire_id"] = steward.reserve_handoff_retire(
        context["target"], context["predecessor"], context["requester"])
    acceptance["settled"] = True
    row["acceptance"] = acceptance
    peer._save_pending(row)
    return acceptance


def resume_handoff_acceptance(ref, *, steward=None):
    """Finish a durable explicit acceptance after interruption, without a new identity claim."""
    steward = steward or _handoff_steward()
    peer = steward.peer_message
    with peer.pending_lock(ref):
        row = peer._read_pending(ref)
        context, _predecessor, successor, _requester, _target = _handoff_context(row, peer=peer)
        acceptance = row.get("acceptance") or {}
        if acceptance.get("read_and_accepted") is not True:
            raise ValueError("handoff-acceptance-not-recorded")
        return _settle_accepted_handoff(ref, row, context, successor, steward=steward)


def accept_handoff(ref, caller, *, steward=None):
    """The agent explicitly asserts it read the card/docs and accepts this transfer."""
    seat, harness, sid = caller
    steward = steward or _handoff_steward()
    peer = steward.peer_message
    with peer.pending_lock(ref):
        row = peer._read_pending(ref)
        context, successor = validate_handoff_receiver(row, caller, steward=steward)
        # Receipt callbacks never set this. Keep progress in the existing row.
        # Register recovery before recording acceptance. Until that explicit
        # record exists the runner waits; it can never infer agent approval.
        if steward._schedule_message_obligation(row, successor["pane"], successor["pane"],
                                                handoff_acceptance=True) is None:
            raise ValueError("handoff-acceptance-continuation-unavailable")
        acceptance = dict(row.get("acceptance") or {}, read_and_accepted=True)
        row["acceptance"] = acceptance
        peer._save_pending(row)
        acceptance = _settle_accepted_handoff(ref, row, context, successor, steward=steward)
    # Courier/retire runner owns delivery and safe idle execution, separately.
    return "accepted=true reply=scheduled retire=reserved request_id=" + acceptance["retire_id"], 0


def handoff(seat: "st.Seat", target: str, *, caller=None) -> tuple[str, int]:
    """(one report line, exit code); the verdict is peer-steward's own typed answer."""
    card = st.read_latest_card(seat)
    if not card or not st.sanitize_body(card.get("body", "")):
        return "prompted=false reason=no-card", 1
    path = st.state_root() / "handoff" / f"{new_id()}.md"
    st.atomic_write(path, (card["body"].rstrip() + "\n").encode("utf-8"))
    injected = injected_executable("HEARTING_TIDY_PEER_STEWARD")
    prepared_refs = []
    try:
        if injected:
            done = subprocess.run([str(injected), "prompt", target, "--body-file", str(path)],
                                  capture_output=True, text=True, timeout=120)
        else:
            import io
            steward = _handoff_steward()
            _, predecessor, _, _ = steward._retire_target(seat.pane)
            successor_candidate, identity_error, _panes = steward._resolve_prompt_target(target)
            if successor_candidate is None:
                return f"prompted=false reason={identity_error or 'target-identity-unverified'}", 5
            successor = dict(successor_candidate["_identity"],
                             session_id=successor_candidate["session_id"])
            sender_sid, sender_harness = steward._current_session_identity()
            if (predecessor.get("session_id"), predecessor.get("harness")) != (sender_sid, sender_harness):
                raise ValueError("handoff-predecessor-mismatch")
            successor_foreground = steward._retire_foreground(
                successor["pane"], successor["harness"], strict=False,
                info=successor_candidate["_pane_info"])
            if not steward._handoff_lifetime_valid(successor_foreground):
                return "prompted=false reason=target-identity-unverified", 5
            context = {"card_id": card.get("id"), "target": seat.pane,
                "predecessor": steward._retire_request_identity(predecessor,
                    steward._retire_foreground(seat.pane, sender_harness, strict=False)),
                "successor": steward._retire_request_identity(successor, successor_foreground),
                "requester": {"harness": successor["harness"], "session_id": successor["session_id"],
                    "pane": successor["pane"], "server": (steward._HERDR_SESSION or "default")}}
            def prepared(ref):
                peer = steward.peer_message
                with peer.pending_lock(ref):
                    row = peer._read_pending(ref)
                    if row.get("handoff") not in (None, context):
                        raise ValueError("handoff-transfer-conflict")
                    row["handoff"] = context
                    peer._save_pending(row)
                    prepared_refs.append(ref)
            args = argparse.Namespace(target=target, body_file=str(path), body_stdin=False,
                text=None, no_verify=False, no_trailer=False, wait_idle_ms=0,
                verify_timeout_ms=5000, ref=[])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = steward.cmd_prompt(args, expected_recipient=(successor["harness"], successor["session_id"]),
                                          on_prepared=prepared, handoff=context,
                                          resolved_recipient=successor_candidate)
            done = subprocess.CompletedProcess([], code, output.getvalue(), "")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"prompted=failed reason=peer-steward-unavailable detail={type(exc).__name__}", 1
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    lines = [ln.strip() for ln in done.stdout.splitlines() if ln.strip()]
    verdict = next((ln for ln in lines if ln.startswith("prompted=")), lines[-1] if lines else "prompted=unknown")
    outcome = verdict.split()[0]
    retained = False
    if not injected and outcome in ("prompted=queued", "prompted=unverified"):
        for ref in prepared_refs:
            try:
                row = steward.peer_message._read_pending(ref)
            except (OSError, ValueError):
                row = None
            if (row and row.get("handoff") == context
                    and all(row["from"].get(k) == predecessor.get(k) for k in ("harness", "session_id"))
                    and all(row["to"].get(k) == successor.get(k) for k in ("harness", "session_id"))):
                retained = True
                break
    if (done.returncode == 0 and outcome == "prompted=true") or retained:
        st.mark_card_handed_off(seat)
    return f"{verdict} exit={done.returncode}", done.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="session_tidy_runner.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="process the queue (started detached by enqueue)")
    runner.add_argument("--queue-id", required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.queue_id)
    except BaseException:  # noqa: BLE001 - a detached runner has nobody to tell
        return 0


if __name__ == "__main__":
    sys.exit(main())
