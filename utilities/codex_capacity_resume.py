#!/usr/bin/env python3
"""Continue an interactive Codex main after a turn ended on transient model capacity.

Codex ends a turn without retrying when the server reports `server_is_overloaded`
("Selected model is at capacity") and gives no Retry-After, and it ends a turn
with `responseTooManyFailedAttempts` once its own stream retries have spent the
server's advice on 429/503. Codex runs no Stop hook on that path, so the thread
just sits idle until the person types "continue".

The Codex prompt hook calls `launch()` for each main-thread turn. It starts one
detached observer for that exact (thread, turn) that only reads the turn's own
rollout tail and exits when the turn ends. When the turn ended failed with a
transient capacity code, the observer waits a bounded backoff, then asks the
app server: is this failed turn still the latest, is the native queue empty, and
is no question or approval waiting? Only then does it add one continuation to
the same thread through the existing native queue carrier, under a client id
derived from the failed turn, so the same failure is never continued twice. The
queued turn runs with the thread's current model and settings; nothing here
chooses a model or touches config, credentials or trust.

Usage limits, auth errors, an interrupt, any newer turn (the person typed, a
peer message arrived), pending queue input or a human wait leave the thread
alone. Consecutive continuations that made no progress stop after
`len(DELAYS)`; a continuation that did real work before failing again starts a
new chain.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
import session_supervisor_decisions as supervisor_decisions

# Wait before the n-th consecutive no-progress continuation. Codex has already
# spent any server Retry-After advice before it ends a turn on these errors.
DELAYS = supervisor_decisions.TRANSIENT_RETRY_DELAYS
POLL_SECONDS = 5.0
MAX_WATCH_SECONDS = 12 * 3600
LOCK_WAIT_SECONDS = 120.0
CLIENT_ID_PREFIX = "delivery-capacity-resume-"
TRANSIENT_HTTP_STATUS = frozenset({429, 503})
_MARKERS = (b'"task_started"', b'"task_complete"', b'"turn_aborted"', b'"item_completed"')
# Items that do not count as work done by a turn.
_NO_PROGRESS_ITEMS = frozenset({"UserMessage", "Reasoning", "ContextCompaction"})
_MAX_LINE = 1024 * 1024
_MAX_READ = 8 * 1024 * 1024


def transient_capacity(info) -> bool:
    """True only for Codex's structured transient-capacity turn errors.

    `info` is CodexErrorInfo as the rollout (snake_case) or the app server
    (camelCase) spells it. Usage limits, auth and other failures are False.
    """
    if info in ("server_overloaded", "serverOverloaded"):
        return True
    if not isinstance(info, dict) or len(info) != 1:
        return False
    (key, value), = info.items()
    if key not in ("response_too_many_failed_attempts", "responseTooManyFailedAttempts"):
        return False
    status = value.get("http_status_code", value.get("httpStatusCode")) if isinstance(value, dict) else None
    return isinstance(status, int) and not isinstance(status, bool) and status in TRANSIENT_HTTP_STATUS


def _payload_string(payload: dict, *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def launch(payload: dict, *, environ=None, popen=None) -> bool:
    """Start the observer for this exact main-thread turn without blocking the hook."""
    if not isinstance(payload, dict):
        return False
    thread_id = _payload_string(payload, "session_id", "thread_id")
    turn_id = _payload_string(payload, "turn_id")
    if not thread_id or not turn_id:
        return False
    argv = [sys.executable, str(Path(__file__).resolve()), "watch",
            "--thread", thread_id, "--turn", turn_id]
    transcript = _payload_string(payload, "transcript_path")
    if transcript:
        try:
            # The turn cannot have ended before its own prompt hook ran.
            argv += ["--transcript", transcript, "--offset", str(os.stat(transcript).st_size)]
        except OSError:
            pass
    spawn = subprocess.Popen if popen is None else popen
    try:
        spawn(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
              cwd=str(ROOT), env=dict(os.environ if environ is None else environ))
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return True


def _state_dir(environ) -> Path:
    # Same precedence as dispatch_contract.state_root, without its import cost
    # in a process that lives as long as the turn.
    if environ.get("HARNESS_STATE_ROOT"):
        root = Path(environ["HARNESS_STATE_ROOT"])
    elif environ.get("XDG_STATE_HOME"):
        root = Path(environ["XDG_STATE_HOME"]) / "hearting"
    else:
        root = Path(environ.get("HOME") or Path.home()) / ".local" / "state" / "hearting"
    return root / "codex-capacity-resume"


def _rollout(thread_id: str, transcript: str | None) -> Path | None:
    sys.path.insert(0, str(ROOT / "utilities"))
    from session_summary_trigger import _codex_transcript, _validated_codex_transcript
    if transcript:
        return _validated_codex_transcript(thread_id, transcript)
    return _codex_transcript(thread_id)


def _main_thread(rollout: Path) -> bool:
    """A top-level thread; native subagent and internal threads carry a structured source."""
    try:
        with rollout.open("rb") as handle:
            first = handle.readline(4 * _MAX_LINE)
        meta = json.loads(first)
    except (OSError, ValueError):
        return False
    payload = meta.get("payload") if isinstance(meta, dict) else None
    return (isinstance(payload, dict) and meta.get("type") == "session_meta"
            and isinstance(payload.get("source"), str)
            and payload.get("thread_source") in (None, "user"))


class _Tail:
    """New turn-boundary events appended to one rollout file."""

    def __init__(self, path: Path, offset: int):
        self.path = path
        self.offset = offset
        self.partial = b""
        self.skipping = False

    def events(self) -> list[dict]:
        size = os.stat(self.path).st_size
        if size <= self.offset:
            return []
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            data = handle.read(min(size - self.offset, _MAX_READ))
        self.offset += len(data)
        *lines, rest = (self.partial + data).split(b"\n")
        if self.skipping and lines:
            lines, self.skipping = lines[1:], False
        self.partial = rest if len(rest) <= _MAX_LINE else b""
        self.skipping = self.skipping or len(rest) > _MAX_LINE
        events = []
        for line in lines:
            if len(line) > _MAX_LINE or not any(marker in line for marker in _MARKERS):
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if (isinstance(value, dict) and value.get("type") == "event_msg"
                    and isinstance(value.get("payload"), dict)):
                events.append(value["payload"])
        return events


def _await_end(tail: _Tail, turn_id: str, *, sleep, now, deadline: float):
    """`(outcome, error_info, progressed)` for the exact turn.

    outcome is failed, completed, aborted, superseded, timeout or gone; progressed
    says the turn completed real work items before it ended.
    """
    progressed = False
    while now() < deadline:
        try:
            events = tail.events()
        except OSError:
            return "gone", None, progressed
        for event in events:
            kind, event_turn = event.get("type"), event.get("turn_id")
            if kind == "task_started" and event_turn != turn_id:
                return "superseded", None, progressed
            if event_turn != turn_id:
                continue
            if kind == "item_completed":
                item = event.get("item")
                progressed = progressed or (isinstance(item, dict)
                                            and item.get("type") not in _NO_PROGRESS_ITEMS)
            elif kind == "turn_aborted":
                return "aborted", None, progressed
            elif kind == "task_complete":
                error = event.get("error")
                if isinstance(error, dict):
                    return "failed", error.get("codex_error_info"), progressed
                return "completed", None, progressed
        sleep(POLL_SECONDS)
    return "timeout", None, progressed


def _quiet(tail: _Tail, seconds: float, *, sleep, now) -> bool:
    """Wait `seconds`; False as soon as any new turn starts on the thread."""
    deadline = now() + seconds
    while True:
        try:
            if any(event.get("type") == "task_started" for event in tail.events()):
                return False
        except OSError:
            return False
        left = deadline - now()
        if left <= 0:
            return True
        sleep(min(POLL_SECONDS, left))


def client_id(thread_id: str, turn_id: str) -> str:
    digest = hashlib.sha256(f"{thread_id}\0{turn_id}".encode()).hexdigest()[:32]
    return CLIENT_ID_PREFIX + digest


def message(attempt: int, info) -> str:
    code = info if isinstance(info, str) else next(iter(info), "capacity") if isinstance(info, dict) else "capacity"
    return (f"Automatic continuation {attempt}/{len(DELAYS)}: the previous turn stopped because "
            f"the selected model was temporarily at capacity ({code}). Continue the same task "
            "from where it stopped. Commands from the stopped turn may already have run or may "
            "still be running; check their results before repeating any of them.")


def _read_record(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_record(path: Path, record: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _record(thread_id, turn_id, info, outcome, cid, chain, now) -> dict:
    return {"schema": "codex-capacity-resume-v1", "thread_id": thread_id,
            "failed_turn_id": turn_id, "error": info, "outcome": outcome,
            "client_id": cid, "chain": chain,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))}


def _chain(turn: dict, record: dict, progressed: bool) -> int:
    """Continuations in a row that made no progress, counting the one this turn came from."""
    items = turn.get("items") if isinstance(turn.get("items"), list) else []
    ours = record.get("client_id")
    from_us = bool(ours) and any(isinstance(item, dict) and item.get("clientId") == ours for item in items)
    chain = record.get("chain")
    return chain if from_us and not progressed and isinstance(chain, int) and chain > 0 else 0


class NativeCarrier:
    """The Codex app server's thread history, native queue and Fleet wait marker."""

    def __init__(self, environ):
        sys.path.insert(0, str(ROOT / "utilities"))
        sys.path.insert(0, str(ROOT / "tools"))
        import codex_queue_delivery
        from codex_queue_dispatch import resolve_queue_socket
        self.delivery = codex_queue_delivery
        self.socket = resolve_queue_socket(environ)
        self.session = codex_queue_delivery.DeliverySession(self.socket)

    def latest_turn(self, thread_id: str) -> dict | None:
        response = self.session.request(self.socket, "thread/turns/list", {
            "threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "summary",
        })
        turns = response.get("data")
        return turns[0] if isinstance(turns, list) and turns and isinstance(turns[0], dict) else None

    def queue_pending(self, thread_id: str) -> bool:
        return bool(self.delivery.list_queue(self.socket, thread_id, session=self.session))

    def human_wait(self, thread_id: str) -> bool:
        from fleet import interaction
        return interaction.pending_wait(thread_id, "codex") is not None

    def send(self, thread_id: str, client_message_id: str, text: str) -> dict:
        return self.delivery.send_at_least_once(
            self.socket, thread_id=thread_id, client_message_id=client_message_id,
            message=text, allow_restart=False, session=self.session)

    def close(self) -> None:
        self.session.close()


def _lock(path: Path, *, sleep, now):
    handle = path.open("a+")
    deadline = now() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES) or now() >= deadline:
                handle.close()
                return None
        sleep(1.0)


def _blocker(carrier, thread_id: str, turn_id: str) -> tuple[str | None, dict | None]:
    latest = carrier.latest_turn(thread_id)
    if latest is None or latest.get("id") != turn_id:
        return "superseded", latest
    error = latest.get("error") if isinstance(latest.get("error"), dict) else {}
    if latest.get("status") != "failed" or not transient_capacity(error.get("codexErrorInfo")):
        return "not-transient", latest
    if carrier.queue_pending(thread_id):
        return "queue-pending", latest
    if carrier.human_wait(thread_id):
        return "human-wait", latest
    return None, latest


def watch(thread_id: str, turn_id: str, *, transcript: str | None = None, offset: int | None = None,
          environ=None, carrier_factory=NativeCarrier, sleep=time.sleep, now=time.time) -> str:
    """Observe one turn and continue it once if it failed on transient capacity."""
    environ = os.environ if environ is None else environ
    rollout = _rollout(thread_id, transcript)
    if rollout is None:
        return "rollout-unavailable"
    if not _main_thread(rollout):
        return "not-main-thread"
    try:
        start = os.stat(rollout).st_size if offset is None else offset
    except OSError:
        return "rollout-unavailable"
    tail = _Tail(rollout, start)
    outcome, info, progressed = _await_end(tail, turn_id, sleep=sleep, now=now,
                                           deadline=now() + MAX_WATCH_SECONDS)
    if outcome != "failed":
        return outcome
    if not transient_capacity(info):
        return "not-transient"
    state = _state_dir(environ)
    record_path = state / f"{thread_id}.json"
    carrier = None
    lock = None
    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        carrier = carrier_factory(environ)
        blocker, latest = _blocker(carrier, thread_id, turn_id)
        if blocker:
            return blocker
        chain = _chain(latest, _read_record(record_path), progressed)
        if chain < len(DELAYS):
            # Hold no connection while waiting; the session reconnects on demand.
            carrier.close()
            if not _quiet(tail, DELAYS[chain], sleep=sleep, now=now):
                return "superseded"
            lock = _lock(state / f"{thread_id}.lock", sleep=sleep, now=now)
            if lock is None:
                return "busy"
            blocker, _latest = _blocker(carrier, thread_id, turn_id)
            if blocker:
                return blocker
            cid = client_id(thread_id, turn_id)
            # Recorded first: the continuation's own observer reads it if that turn fails.
            _write_record(record_path, _record(thread_id, turn_id, info, "sending", cid, chain + 1, now))
            result = carrier.send(thread_id, cid, message(chain + 1, info))
            outcome = "queued" if result.get("status") == "queued" else "consumed"
            _write_record(record_path, _record(thread_id, turn_id, info, outcome, cid, chain + 1, now))
        else:
            outcome = "chain-exhausted"
            _write_record(record_path, _record(thread_id, turn_id, info, outcome, None, chain, now))
        return outcome
    except Exception as exc:  # The carrier is native and optional; fail closed.
        return f"carrier-error:{type(exc).__name__}"
    finally:
        if carrier is not None:
            carrier.close()
        if lock is not None:
            lock.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="codex_capacity_resume.py")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("watch")
    run.add_argument("--thread", required=True)
    run.add_argument("--turn", required=True)
    run.add_argument("--transcript")
    run.add_argument("--offset", type=int)
    args = parser.parse_args(argv)
    print(watch(args.thread, args.turn, transcript=args.transcript, offset=args.offset))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
