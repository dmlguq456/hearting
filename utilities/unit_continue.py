"""Continue a unit route's worker: one more turn of the same route and node.

A unit route (`compose --start --unit <unit> …`) runs one registered leg. After it ends and the route
closes, `capability-route.py correct --route <id> --message-file <text>` asks for another turn: a new
attempt of the same route and node that wakes the previous turn's harness session when it can and starts
a fresh one (with the original brief and the previous result) when it cannot. The turn begins its own
artifact cycle, a child of the previous turn's (`artifact_producer.begin(fresh_cycle=…)`): each turn has its
own report folder and terminal evidence. This module holds the meaning those callers share: the kept text, the chain of turns, the one
question "may the next turn start now", the session decision, the one-file session transplant and the
turn's prompt. It owns no process, no poller and no new state beyond the text store.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import NamedTuple, Optional

from dispatch_contract_roots import dispatch_state_root

STORE = "unit-continue"
TEXT_BYTES = 65536                                   # the same limit an owner's answer has
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")     # the same id shape an owner's answer has
REASONS = ("session-missing", "harness-changed", "store-unsupported", "resume-refused")


class ContinueError(ValueError):
    """A typed refusal: `str(error)` is the reason."""


# -- the kept text ------------------------------------------------------------------------------

def _store_path(jobs, route_id) -> Path:
    return dispatch_state_root(jobs) / STORE / f"{route_id}.json"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def entries(jobs, route_id) -> list:
    path = _store_path(jobs, route_id)
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("requests", [])
    except (OSError, ValueError):
        return []


def submit(jobs, route_id, text, request_id=None) -> dict:
    """Keep `text` for the route's next turn: `{"entry": …, "duplicate": bool}`.

    The same id with the same text is a duplicate (nothing new is kept); the same id with other text is
    refused. Without `request_id` the id is a digest of the text, so sending a file twice is one request."""
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > TEXT_BYTES:
        raise ContinueError("correction-text-empty-or-oversized")
    digest = _digest(text)
    request_id = request_id or "input-" + _digest([route_id, digest])[:32]
    if not REQUEST_ID.fullmatch(request_id):
        raise ContinueError("correction-id-invalid")
    path = _store_path(jobs, route_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.is_symlink():
            raise ContinueError("unit-continue-state-unsafe")
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "schema": "unit_continue_v1", "route_id": route_id, "requests": []}
        old = next((item for item in value["requests"] if item["id"] == request_id), None)
        if old is not None:
            if old["digest"] != digest:
                raise ContinueError("correction-id-content-conflict")
            return {"entry": old, "duplicate": True}
        entry = {"id": request_id, "digest": digest, "text": text, "at": round(time.time(), 1)}
        value["requests"].append(entry)
        handle, name = tempfile.mkstemp(prefix=".unit-continue-", dir=path.parent)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
        os.chmod(name, 0o600)
        os.replace(name, path)
        return {"entry": entry, "duplicate": False}
    finally:
        os.close(fd)


# -- the chain of turns ---------------------------------------------------------------------------

def attempt_id_for(route_id, node, request_id) -> str:
    """The attempt of the turn a kept request starts (the first turn's id is `work_start.attempt_id`)."""
    return "att-" + hashlib.sha256(f"{route_id}:{node}:{request_id}".encode()).hexdigest()[:32]


def turn_of(meta) -> int:
    try:
        return max(1, int(meta.get("turn") or 1))
    except (TypeError, ValueError):
        return 1


def chain(rows, route_id, node) -> list:
    """`[(attempt_id, status, meta)]` of the route's turns on `node`, oldest first. `rows` is `work_start._rows`."""
    turns = [(aid, status, meta) for aid, (status, meta) in rows.items()
             if meta.get("route_id") == route_id and meta.get("route_node") == node
             and meta.get("worker_type") != "owner"]
    return sorted(turns, key=lambda item: turn_of(item[2]))      # stable: registry order within a turn


def superseded_count(path) -> int:
    """How many closure records of this route were set aside (each reopening sets one aside)."""
    route_file = Path(path)
    return len(list(route_file.parent.glob(f"{route_file.stem}.superseded-*.outcome.json")))


class Turn(NamedTuple):
    attempt_id: str
    prev_attempt_id: str
    turn: int
    text: str
    request_id: str


def next_turn(jobs, route, node, rows) -> Optional[Turn]:
    """The oldest kept request no turn has started yet, as the turn it would start (None when none)."""
    turns = chain(rows, route["route_id"], node)
    if not turns:
        return None
    for entry in entries(jobs, route["route_id"]):
        aid = attempt_id_for(route["route_id"], node, entry["id"])
        if aid not in rows:
            return Turn(aid, turns[-1][0], turn_of(turns[-1][2]) + 1, entry["text"], entry["id"])
    return None


def waiting_request(jobs, meta) -> bool:
    """Whether a request kept for this unit leg's route has no turn yet (the end notice then names `start`)."""
    from dispatch_completion_join import exact_attempt_row
    route_id, node = meta.get("route_id"), meta.get("route_node")
    if not route_id or not node:
        return False
    for entry in entries(jobs, route_id):
        try:
            exact_attempt_row(Path(jobs), attempt_id_for(route_id, node, entry["id"]))
        except Exception:  # noqa: BLE001 -- no row for it: it still waits
            return True
    return False


def notice_lines(jobs, meta, agent_home=None) -> tuple:
    """`(head, note)` an end notice adds for a unit leg: for a continued turn, which turn it is and its session
    (`head`); for a route with a request still kept, the `start` that runs it (`note`). Either may be ''."""
    if meta.get("capability") != "unit":
        return "", ""
    head = ""
    if meta.get("continues_attempt_id"):
        head = f"Turn {meta.get('turn') or '?'} of this unit route (session: {meta.get('session') or 'unknown'})."
    note = ""
    if jobs and meta.get("route_file") and waiting_request(jobs, meta):
        from parent_next_directive import resume_command
        note = ("A request kept for this unit route is waiting for its next turn; run "
                + resume_command(Path(meta["route_file"]), jobs, agent_home=agent_home or Path(__file__).resolve().parents[1])
                + " to start it.")
    return head, note


def ready_to_continue(jobs, route, path, node, rows) -> Optional[Turn]:
    """The next turn when it may start now, else None: the one judgment `_slot` and `_advance` share.

    It may start when a kept request has no turn yet, the last turn ended, and the route is either closed
    (closure recorded, completion settled, not closed by autoclose) or already reopened for it (a set-aside
    closure record is newer than the last turn's end: the count of them has caught up with the turns).
    A running turn, a turn that ended but whose route is not yet closed, and no request are all None."""
    turn = next_turn(jobs, route, node, rows)
    if turn is None:
        return None
    turns = chain(rows, route["route_id"], node)
    last_id, last_status, last_meta = turns[-1]
    if last_status != "done":
        return None
    from dispatch_notice_state import closed_outcome
    closed = closed_outcome(Path(path), route)
    if closed:
        if closed.get("autoclose") or closed.get("finish_pending"):
            return None
        from dispatch_terminal_commit import owner_completion_state
        if owner_completion_state(jobs, last_status, last_meta).state not in {"complete", "not-applicable"}:
            return None
        return turn
    return turn if superseded_count(path) >= len(turns) else None


def reopen(path) -> None:
    """Set the closure record aside so the route can start its next turn (never deletes it)."""
    from capability_route_close import retire_stale_closure
    retire_stale_closure(Path(path))


# -- the session ------------------------------------------------------------------------------------

class SessionPlan(NamedTuple):
    kind: str                       # "resumed" | "fresh"
    reason: str                     # "" when resumed, else one of REASONS
    session_id: str
    source: str                     # the file to carry to the new home ("" when nothing moves)
    relative: str = ""              # that file's place under a worker home

    @property
    def row_value(self) -> str:
        return "resumed" if self.kind == "resumed" else f"fresh:{self.reason}"


def _log_text(meta) -> str:
    try:
        return Path(meta.get("log_file") or meta.get("log") or "").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def session_id_of(meta) -> str:
    """The harness session a finished turn ran in ('' when it cannot be read)."""
    if meta.get("session_id"):
        return meta["session_id"]
    text = _log_text(meta)
    harness = meta.get("harness")
    pattern = {"codex": r'"thread_id"\s*:\s*"([^"]+)"', "opencode": r'"sessionID"\s*:\s*"([^"]+)"',
               "claude": r'"session_id"\s*:\s*"([^"]+)"'}.get(harness)
    found = re.search(pattern, text) if pattern else None
    return found.group(1) if found else ""


def session_file(meta, session_id) -> Optional[Path]:
    """The one file that holds a turn's session in its worker home (Claude, Codex); None when absent."""
    home = meta.get("runtime_home") or ""
    if not session_id or not home or not os.path.isabs(home):
        return None
    root = Path(home)
    pattern = {"claude": f"projects/*/{session_id}.jsonl",
               "codex": f"sessions/*/*/*/rollout-*{session_id}.jsonl"}.get(meta.get("harness"))
    if pattern is None:
        return None
    return next((p for p in sorted(root.glob(pattern)) if p.is_file()), None)


def opencode_session_exists(session_id) -> bool:
    """OpenCode keeps sessions in one shared store; `session export` answers whether the id is in it."""
    try:
        done = subprocess.run(["opencode", "session", "export", session_id], capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def session_plan(jobs, prev_attempt_id, harness, *, exists=opencode_session_exists) -> SessionPlan:
    """Whether the turn after `prev_attempt_id` wakes its session on `harness`, or starts a fresh one."""
    from dispatch_completion_join import exact_attempt_row
    try:
        meta = exact_attempt_row(Path(jobs), prev_attempt_id).metadata
    except Exception:  # noqa: BLE001 -- an unreadable previous row is a missing session
        return SessionPlan("fresh", "session-missing", "", "")
    if meta.get("harness") != harness:
        return SessionPlan("fresh", "harness-changed", "", "")
    session_id = session_id_of(meta)
    if harness == "opencode":
        from opencode_runtime import is_v2
        if not is_v2():
            return SessionPlan("fresh", "store-unsupported", "", "")
        if session_id and exists(session_id):
            return SessionPlan("resumed", "", session_id, "")
        return SessionPlan("fresh", "session-missing", "", "")
    source = session_file(meta, session_id)
    if source is None:
        return SessionPlan("fresh", "session-missing", "", "")
    return SessionPlan("resumed", "", session_id, str(source), str(source.relative_to(meta["runtime_home"])))


def plan_for_args(args, jobs, harness) -> Optional[SessionPlan]:
    """The launch's session plan (None for a first turn), decided once and kept on `args`."""
    prev = getattr(args, "continues_attempt_id", None)
    if not prev:
        return None
    if getattr(args, "session_plan", None) is None:
        args.session_plan = session_plan(jobs, prev, harness)
    return args.session_plan


def transplant(plan, new_home) -> None:
    """Carry the one session file (and Claude's same-named folder) into the new worker home; never move the source."""
    if plan is None or plan.kind != "resumed" or not plan.source:
        return
    source = Path(plan.source)
    destination = Path(new_home) / plan.relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    sibling = source.with_suffix("")
    if sibling.is_dir():
        shutil.copytree(sibling, destination.with_suffix(""), symlinks=True, dirs_exist_ok=True)
    if not destination.is_file() or destination.stat().st_size != source.stat().st_size:
        raise ContinueError("session-transplant-incomplete")


# -- the turn's prompt ------------------------------------------------------------------------------

def previous_result(jobs, prev_attempt_id) -> str:
    """The previous turn's sealed summary (verdict line, findings, report path), '' when there is none."""
    from dispatch_completion_join import exact_attempt_row
    try:
        import work_intent
        meta = exact_attempt_row(Path(jobs), prev_attempt_id).metadata
        return work_intent.sealed_summary(meta, prev_attempt_id)
    except Exception:  # noqa: BLE001
        return ""


def turn_prompt(args, plan, metadata_block, brief, jobs) -> str:
    """What the continued turn is told. A woken session already holds the brief and the kernel's rules:
    it gets this turn's dispatch facts (the new cycle's output folder) and the text. A fresh session gets
    the original brief and the previous result with the text."""
    number = int(getattr(args, "turn", 0) or 2)
    text = getattr(args, "continuation_text", "") or ""
    previous = previous_result(jobs, args.continues_attempt_id)
    carried = f"Previous turn's result (turn {number - 1}):\n{previous}\n\n" if previous else ""
    if plan is not None and plan.kind == "resumed":
        return (f"This is turn {number} of the same assignment, in the same session. The dispatch facts below "
                f"are this turn's: write this turn's report under its artifact_output_dir, and keep the "
                f"earlier report as it is. End as before.\n\n{metadata_block}\n{carried}"
                f"Request for this turn:\n{text.rstrip()}\n")
    return (f"{brief.rstrip()}\n\nThis is turn {number} of this assignment; the earlier turn's session could "
            f"not be woken ({plan.reason if plan else 'session-missing'}), so you start fresh. Write this "
            f"turn's report under your artifact_output_dir and leave the earlier report as it is.\n\n"
            f"{carried}Request for this turn:\n{text.rstrip()}\n")


# -- how long a finished turn's worker home is kept ---------------------------------------------------

def keep_window(meta, rows, default, main_state="unknown") -> float:
    """Seconds a finished worker home waits before it may be removed (`default` for every home but one kind).

    The one kind is the last turn of a unit route that is still open to another turn: the home holds the
    session the next turn wakes. A turn a later turn already continued, and a route the runtime closed
    after it sat unused, keep the ordinary window. A leg a worker started is kept while that worker's row
    is live; a leg the main session started while that session lives (`main_state` live), for the
    ordinary window once it is gone (dead), and for the route's own unused-route window when it cannot
    be told (unknown). Live is capped at that same unused-route window, so a session left open for days
    does not keep every home it called. The two numbers are `route_autoclose`'s, not new ones."""
    if meta.get("capability") != "unit" or meta.get("workflow_completion") != "runtime-v1":
        return default
    attempt = meta.get("attempt_id")
    if any(other.meta.get("continues_attempt_id") == attempt for other in rows.values()):
        return default
    route_file = meta.get("route_file")
    try:
        closed = json.loads(Path(route_file).with_name(Path(route_file).stem + ".outcome.json").read_text())
    except (OSError, ValueError, TypeError):
        closed = None
    if isinstance(closed, dict) and closed.get("autoclose"):
        return default
    import route_autoclose
    caller = rows.get(meta.get("caller_attempt_id") or "")
    if meta.get("caller_attempt_id"):
        return route_autoclose.IDLE_SECONDS if caller is not None and caller.status in {"open", "running"} else default
    if main_state == "live":
        return route_autoclose.IDLE_SECONDS
    return route_autoclose.IDLE_SECONDS if main_state == "unknown" else route_autoclose.QUIET_SECONDS
