#!/usr/bin/env python3
"""session-tidy: seat state, session cards, session ledger, result notices, hook text.

One entry point for the parts of ``session-tidy`` that a session touches directly:

    session_tidy.py card [--harness H] [--session-id S] [--cwd D] [--file F | --text T]
        Write the calling session's card (body from stdin, a file or --text).
        Prints one line: ``card=<path> seat=<key>``.  Nothing is required.
    session_tidy.py tidy [--harness H] [--session-id S] [--cwd D] [--file F | --text T]
                         [--no-clear | --no-continue] [--handoff TARGET]
        ``card`` then, only when the card was written, ``enqueue`` (and ``handoff``)
        for the same caller in one process, so a tidy is one tool call.  The last
        line ``say=<text>`` is the one line to show the user.
    session_tidy.py hook --harness H --event start|prompt|compact --session-id S
                         [--source X] [--transcript P] [--cwd D] [--reread]
        Record the session in the seat ledger and print the injection text for a
        card / result notice this session is due, or nothing at all.  A hook never
        fails: any problem is silent and exits 0.
    session_tidy.py status [--json] [--cwd D]
        Print where the state lives and what is waiting.

    session_tidy.py enqueue [--harness H] [--session-id S] [--cwd D] [--no-clear | --no-continue]
        Queue the memory tidy and return at once (one line); the detached runner
        ``session_tidy_runner.py`` does the work and leaves a result notice.  Inside
        herdr it also books the window's auto-clear and continuation after it
        (``session_tidy_clear.py``); ``--no-clear`` keeps the window and cancels a pending
        booking, ``--no-continue`` clears without a continuation action.
    session_tidy.py handoff <target> [--harness H] [--session-id S] [--cwd D]
        Deliver this seat's card to a peer session through ``peer-steward.py prompt``
        and report its structured delivery verdict and exit code on one line. The
        card is marked as handed off: this window is not continued from it.

State lives in ``${XDG_STATE_HOME:-~/.local/state}/hearting/session-tidy/``
(directories 0700, files 0600, temp file + rename, symlinks refused):

    cards/<card>.json|.md latest card (canonical JSON + readable text)
    card-history/<card>/   bounded older cards
    consumed/<card>.json   receipts by card key: pane/native use the seat key; project seats use the author session
    project-latest/<seat>.json  newest project-seat card's author
    sessions/<seat>.jsonl   seat ledger: one line per hook/card event
    reread/<harness>-<sid>  the injection a session just consumed (OpenCode re-emits)
    notices/<seat>.json     result lines waiting for the next start/prompt
    locks/<seat>.lock       flock for the read-modify-write above
    prompt-seq/<seat>.json  count of real prompts submitted at the seat (card/enqueue compare it)
    clear/<seat>.json       the one pending auto-clear reservation (``session_tidy_clear.py``)
    watermarks/  decisions/pending/  runs/  queue/  runner.lock   (other slices)

The seat is the herdr pane only when ``pane_ownership`` verifies its live connection,
else harness + project. ``HERDR_PANE_ID`` is only a lookup hint.
A Codex caller's ``CODEX_THREAD_ID`` supplies its exact thread; pane_ownership maps that
thread to the foreground TUI PID and confirms herdr's pane process info. If no unique pane
can be proved, the project seat is used quietly.
A worker marker is checked before the pane: a worker that inherited its
supervisor's pane id gets no card, ledger line or notice.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
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
from typing import Callable, Iterator, Optional

SCHEMA = 1
HARNESSES = ("claude", "codex", "opencode")
CARD_BODY_MAX_BYTES = 8 * 1024
COMPACT_SUMMARY_MAX_BYTES = 256 * 1024
COMPACT_RECENT_MAX_BYTES = 8 * 1024
COMPACT_METADATA_MAX_BYTES = 1024
COMPACT_CARD_BODY_MAX_BYTES = COMPACT_SUMMARY_MAX_BYTES + COMPACT_RECENT_MAX_BYTES + COMPACT_METADATA_MAX_BYTES
INJECTION_MAX_BYTES = 2400
MEMORY_REFS_MAX = 8                 # "참고할 기억" (card layer B): records listed
MEMORY_REFS_BYTES = 1200            # ... and the bytes their lines may take
CARD_FIRST_REPLY_GUIDANCE = (
    "[첫 답] 이 카드로 이어받은 첫 답 맨 앞에, 도구 호출 전에, 사용자의 언어로 "
    "'하던 일 · 남은 일 · 다음 할 일' 세 머리말을 써서 5줄 안팎 요약을 한 번 보여 준 뒤 "
    "이어서 하세요. 사용자가 이어서하기 대신 다른 지시를 먼저 주면 요약을 생략하고 그 지시만 따르세요."
    " 요약에는 대상의 원천 설명과 구별 조건을 먼저 쓰고 원래 번호를 덧붙이세요. "
    "미완료·중단 상태와 다음 행동을 유지하고, 뜻이 확인되지 않은 식별자는 추측하지 마세요."
)
# Keep the previous body allowance when memory references ride along.
CARD_MIN_WHEN_BUNDLED = 900 + len(CARD_FIRST_REPLY_GUIDANCE.encode("utf-8")) + 1
NOTICE_MAX_BYTES = 500
CARD_HISTORY_KEEP = 20
LEDGER_FOLD_LINES = 500
LEDGER_KEEP_RAW = 100
HANDOVER_KEEP_ROWS = 40
TIDY_CLAIM_KEEP_ROWS = 64
PROMPT_LEDGER_THROTTLE_SEC = 20
COMPACT_DEDUPE_SEC = 60
AUTHOR_STALE_SEC = 10 * 60
COMPACT_LOCK_WAIT_SEC = 1.0         # a compact hook waits this long for the seat lock, never longer
COMPACT_HOOK_BUDGET_SEC = 3.5       # worker-state bridge has a hard 4 s timeout
COMPACT_CARD_SEAT_KINDS = ("pane", "native")   # seats of one session; never the shared project seat
# The runtime compaction summary doubles as the seat card (capabilities/session-tidy.md):
# the card's fields and order, in the user's language.
COMPACT_CARD_INSTRUCTIONS = (
    "Write this summary as a session handoff card in the user's language, with these six "
    "fields in this order: work in progress; waiting decisions; next actions; related "
    "references (paths, PRs, sessions); today's work (with PR, installed version or result "
    "file pointers); user instructions (brief quotes, each marked finished, remaining or "
    "deferred). Keep the details needed to continue the work inside those fields, within "
    "about 90 lines."
    " Carry the source description and distinguishing comparison condition before the "
    "original identifier; preserve document/revision and checkpoint references when they "
    "affect meaning. Keep failed, interrupted and unfinished states with their next action, "
    "and leave unexplained identifiers unresolved."
)


class StateError(Exception):
    """The state folder is unsafe (a symlink) or unusable."""


# ---------------------------------------------------------------------------
# State folder primitives
# ---------------------------------------------------------------------------

def state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "hearting" / "session-tidy"


def _reject_symlink(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise StateError(f"refusing symlink: {path}")


def ensure_dir(path: Path) -> Path:
    """Create ``path`` (0700) below the state root; refuse symlinks on the way."""
    root = state_root()
    path = Path(path)
    if path != root and root not in path.parents:
        raise StateError(f"outside the state root: {path}")
    chain = [root] + [p for p in reversed(path.parents) if root in p.parents] + ([path] if path != root else [])
    for part in chain:
        _reject_symlink(part)
        if not part.exists():
            part.mkdir(mode=0o700, parents=True, exist_ok=True)
        if part == root or root in part.parents:
            with contextlib.suppress(OSError):
                os.chmod(part, 0o700)
    return path


def atomic_write(path: Path, data: bytes) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    _reject_symlink(path)
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


def atomic_write_json(path: Path, value) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8"))


def read_bytes(path: Path) -> Optional[bytes]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def read_json(path: Path):
    raw = read_bytes(path)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


@contextlib.contextmanager
def seat_lock(seat_key: str, *, wait: Optional[float] = None) -> Iterator[None]:
    """Serialize hook consumption, ledger appends and card writes for one seat.

    ``wait`` bounds the wait in seconds (``None``: as long as it takes); a lock still
    held after it raises ``BlockingIOError``.
    """
    lock_dir = ensure_dir(state_root() / "locks")
    path = lock_dir / f"{seat_key}.lock"
    _reject_symlink(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if wait is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def now_epoch() -> float:
    return time.time()


def iso_utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _digest(*parts: str, size: int = 20) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:size]


def _endpoint_scope(env=None) -> str:
    """A pane id is local to its Herdr endpoint; keep named sockets in separate seats."""
    env = os.environ if env is None else env
    server = str(env.get("AGENT_HERDR_SESSION") or "").strip()
    socket_path = str(env.get("HERDR_SOCKET_PATH") or "").strip()
    if socket_path:
        # The socket is the concrete endpoint identity shared by direct and
        # deferred reporters; a configured server name may be absent there.
        return _digest("herdr-endpoint", "socket", os.path.realpath(socket_path))
    if not server:
        return ""                 # retain the original default-server key
    return _digest("herdr-endpoint", "session", server)


# ---------------------------------------------------------------------------
# Worker check, harness / session identity, seat
# ---------------------------------------------------------------------------

def is_worker(env=None) -> bool:
    """Same markers as hooks/mem-recall-inject.sh; checked before any pane."""
    env = os.environ if env is None else env
    if env.get("AGENT_SESSION_ROLE") == "worker" or env.get("AGENT_DISPATCH_CHILD") == "1":
        return True
    if env.get("AGENT_DISPATCH_DEPTH"):
        return True
    if env.get("FLEET_TITLE_REFRESH") == "1" or env.get("MEM_DISTILL") == "1":
        return True
    return any(key.startswith("OPENCODE_DISPATCH") and value for key, value in env.items())


def session_from_env(harness: Optional[str] = None, env=None) -> tuple[Optional[str], Optional[str]]:
    """(harness, session id) from the harness-provided variables, or (harness, None).

    Read by `session_identity`; with several harnesses' ids inherited and none
    named, the first in its claude, codex, opencode order answers.
    """
    live_process = env is None
    env = os.environ if env is None else env
    from session_identity import identity, session_ids
    sessions = session_ids(env)
    if live_process and env.get("HERDR_PANE_ID"):
        named = env.get("AGENT_DISPATCH_CALLER_HARNESS") or env.get("AGENT_DISPATCH_CURRENT_HARNESS")
        if (harness in (None, "codex") and (not named or named == "codex")
                and sessions.get("codex")):
            from pane_ownership import current_codex_thread_for_caller
            current = current_codex_thread_for_caller(env.get("HERDR_PANE_ID"))
            if current is not None:
                return "codex", current or None
    if harness:
        session_id = sessions.get(harness, (None, None))[0]
        return harness, session_id.strip() if session_id else None
    found = identity(env)
    if found.known and found.session_id:
        return found.harness, found.session_id.strip()
    for name, (session_id, _variable) in sessions.items():
        return name, session_id.strip()
    return None, None


_PROJECT_KEYS: dict[str, str] = {}
PROJECT_KEY_TTL_SEC = 6 * 3600


def _project_key_cache() -> Path:
    return state_root() / "project-keys.json"


def project_key_for(cwd) -> str:
    """The memory tool's project key (origin, common root, marker), never raising.

    ``mem.project_key`` runs three ``git`` calls (slow on a loaded NFS box), and a
    pane-less prompt hook needs it every turn, so the answer is cached per cwd for
    a few hours next to the rest of the state.
    """
    cwd = str(cwd or os.getcwd())
    if cwd in _PROJECT_KEYS:
        return _PROJECT_KEYS[cwd]
    now = now_epoch()
    cache = read_json(_project_key_cache())
    hit = cache.get(cwd) if isinstance(cache, dict) else None
    if isinstance(hit, dict) and hit.get("key") and now - float(hit.get("at", 0)) < PROJECT_KEY_TTL_SEC:
        _PROJECT_KEYS[cwd] = str(hit["key"])
        return _PROJECT_KEYS[cwd]
    key = ""
    try:
        memory_dir = str(Path(__file__).resolve().parents[1] / "tools" / "memory")
        if memory_dir not in sys.path:
            sys.path.insert(0, memory_dir)
        import mem  # type: ignore

        key = str(mem.project_key(cwd, seed=False) or "")
    except BaseException:  # noqa: BLE001 - a hook never fails because of the key
        key = ""
    if not key:
        with contextlib.suppress(OSError):
            key = "cwd:" + str(Path(cwd).resolve())
        key = key or "cwd:" + cwd
    _PROJECT_KEYS[cwd] = key
    with contextlib.suppress(Exception):
        fresh = {c: v for c, v in (cache.items() if isinstance(cache, dict) else [])
                 if isinstance(v, dict) and now - float(v.get("at", 0)) < PROJECT_KEY_TTL_SEC}
        fresh[cwd] = {"key": key, "at": now}
        atomic_write_json(_project_key_cache(), dict(list(fresh.items())[-200:]))
    return key


@dataclasses.dataclass(frozen=True)
class Seat:
    kind: str            # "pane" | "native" | "project"
    key: str             # hash used in file names
    pane: str = ""
    harness: str = ""
    project_key: str = ""
    native: Optional[dict] = None
    sid: str = ""
    endpoint: str = ""
    socket_path: str = ""   # raw transport is private; endpoint remains the key hash

    def as_dict(self) -> dict:
        result = {"kind": self.kind, "key": self.key, "pane": self.pane,
                  "harness": self.harness, "project_key": self.project_key}
        if self.native is not None:
            result["native"] = self.native
        if self.sid:
            result["sid"] = self.sid
        if self.endpoint:
            result["endpoint"] = self.endpoint
        return result


def seat_from_dict(data: dict) -> Seat:
    return Seat(str(data.get("kind") or ""), str(data.get("key") or ""),
                str(data.get("pane") or ""), str(data.get("harness") or ""),
                str(data.get("project_key") or ""), data.get("native"),
                str(data.get("sid") or ""), str(data.get("endpoint") or ""))


def seat_for_project(harness: str, project_key: str) -> Seat:
    return Seat("project", _digest("project", harness, project_key), "", harness, project_key)


HERDR_LOOKUP_TIMEOUT_SEC = 2.0      # a hook has a few seconds in total


def _herdr_executable() -> Optional[str]:
    try:
        import session_tidy_clear
        return session_tidy_clear.herdr_command()
    except BaseException:  # noqa: BLE001 - no herdr, no lookup
        return shutil.which("herdr")


def _herdr_codex_agents() -> Optional[list[dict]]:
    """The Codex entries of ``herdr agent list``; None when herdr cannot be asked (missing, slow,
    unreadable).  Read-only: nothing is typed or changed."""
    exe = _herdr_executable()
    if not exe:
        return None
    try:
        server = os.environ.get("AGENT_HERDR_SESSION")
        command = [exe]
        if server:
            command += ["--session", server]
        command += ["agent", "list"]
        done = subprocess.run(command, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=HERDR_LOOKUP_TIMEOUT_SEC)
        agents = (json.loads(done.stdout or "").get("result") or {}).get("agents")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    if not isinstance(agents, list):
        return None
    return [a for a in agents if isinstance(a, dict) and a.get("agent") == "codex" and a.get("pane_id")]


def _agent_session(agent: dict) -> str:
    session = agent.get("agent_session")
    return str(session.get("value") or "") if isinstance(session, dict) else ""


def _real(path) -> str:
    with contextlib.suppress(OSError, TypeError, ValueError):
        return os.path.realpath(str(path))
    return ""


def _pane_seat_of(pane: str, *, endpoint: Optional[str] = None,
                  socket_path: Optional[str] = None) -> Seat:
    if endpoint is None:
        endpoint = _endpoint_scope()
    if socket_path is None:
        socket_path = str(os.environ.get("HERDR_SOCKET_PATH") or "")
    key = _digest("pane", endpoint, pane) if endpoint else _digest("pane", pane)
    return Seat("pane", key, pane, "codex", "", endpoint=endpoint, socket_path=socket_path)


def pane_seat_from_binding(binding: dict) -> Seat:
    """Create a Codex seat only from one exact live TUI binding."""
    pane = str(binding.get("pane") or "")
    socket_path = str(binding.get("socket_path") or "")
    if (not pane or not binding.get("pid") or not binding.get("pid_start")
            or not binding.get("session_id")):
        raise ValueError("incomplete Codex pane binding")
    endpoint = _endpoint_scope({"HERDR_SOCKET_PATH": socket_path})
    key = _digest("pane", endpoint, pane) if endpoint else _digest("pane", pane)
    return Seat("pane", key, pane, "codex", "", sid=str(binding["session_id"]),
                endpoint=endpoint, socket_path=socket_path)


def ledger_precedes(seat: Seat, older: str, newer: str, harness: str = "codex") -> bool:
    """True when this seat's own ledger saw session ``older`` strictly before session ``newer``."""
    try:
        rows = session_summary(seat)
        a, b = rows.get((harness, older)), rows.get((harness, newer))
        return bool(a and b and float(a["first_seen"]) < float(b["first_seen"]))
    except Exception:  # noqa: BLE001
        return False


def _successor_pane(sid: str, agents: list[dict], cwd: str) -> list[str]:
    """The window whose auto-clear is under way, for the first hook of the session it started.

    herdr keeps showing the cleared session, and the shared daemon gives the TUI no rollout to
    read, so the new thread cannot be proven.  It is taken only when everything points at one
    window: exactly one Codex window of herdr works in this directory, its booking is still
    waiting for the successor, and herdr shows the booked session there or an older one of the seat."""
    here = [a for a in agents if _real(a.get("foreground_cwd") or a.get("cwd") or "") == _real(cwd)]
    if len(here) != 1:
        return []
    pane = str(here[0]["pane_id"])
    seat = _pane_seat_of(pane)
    try:
        import session_tidy_clear
        booking = session_tidy_clear.read_reservation(seat.key)
    except BaseException:  # noqa: BLE001
        return []
    ok = bool(booking) and booking.get("harness") == "codex" and booking.get("status") in ("reserved", "unverified") \
        and booking.get("sid") not in (sid, "") \
        and (_agent_session(here[0]) == booking.get("sid")
             or ledger_precedes(seat, _agent_session(here[0]), str(booking.get("sid")))) \
        and _real(booking.get("cwd") or "") == _real(cwd) \
        and (booking.get("seat") or {}).get("pane") == pane
    return [pane] if ok else []


def _cleared_into(sid: str) -> list[str]:
    """The windows whose confirmed auto-clear read exactly ``sid`` off their own screen as the
    thread it started (``peer-steward.py clear``).  Read from the bookings alone: a thread id is
    unique, and herdr may be slow or gone just when that thread's first hook runs."""
    try:
        import session_tidy_clear
        keys = [path.stem for path in session_tidy_clear.clear_dir().glob("*.json")]
    except BaseException:  # noqa: BLE001
        return []
    panes = []
    for key in keys:
        try:
            booking = session_tidy_clear.read_reservation(key)
        except BaseException:  # noqa: BLE001
            continue
        pane = str(((booking or {}).get("seat") or {}).get("pane") or "")
        booked_seat = (booking or {}).get("seat") if isinstance((booking or {}).get("seat"), dict) else {}
        endpoint = booked_seat.get("endpoint") if isinstance(booked_seat, dict) else None
        if booking and booking.get("harness") == "codex" and booking.get("status") == "cleared" \
                and booking.get("new_session") == sid and pane \
                and _pane_seat_of(pane, endpoint=str(endpoint or "")).key == key:
            panes.append(pane)
    return panes


def codex_pane_for_session(sid: str, cwd: str = "", source: str = "") -> str:
    """The herdr pane of a Codex session that has no ``HERDR_PANE_ID`` -- exactly one, else "".

    In order: the pane whose herdr session is this thread; the pane whose seat ledger already
    knows this thread (herdr's value can lag a ``/clear`` for good); the pane whose confirmed
    auto-clear started exactly this thread (no herdr needed); for the first hook after a clear
    (``source=clear``) the one window whose auto-clear is waiting for its successor."""
    if not sid or sid == "-":
        return ""
    agents = _herdr_codex_agents()
    panes: list[str] = []
    if agents:
        panes = [str(a["pane_id"]) for a in agents if _agent_session(a) == sid]
        if not panes:
            panes = [str(a["pane_id"]) for a in agents
                     if ("codex", sid) in session_summary(_pane_seat_of(str(a["pane_id"])))]
    if not panes:
        panes = _cleared_into(sid)
    if not panes and agents and source == "clear" and cwd:
        panes = _successor_pane(sid, agents, cwd)
    return panes[0] if len(set(panes)) == 1 else ""


def resolve_seat(harness: Optional[str], cwd=None, env=None, sid: Optional[str] = None,
                 source: str = "", *, project: bool = True) -> Seat:
    """The pane, native or project seat; ``project=False`` skips the project key (an empty one)."""
    env = os.environ if env is None else env
    live_caller = env is os.environ
    pane = ((env.get("HERDR_PANE_ID") or "").strip()
            if live_caller or harness != "codex" else "")
    server = env.get("AGENT_HERDR_SESSION") if live_caller else None
    socket_value = env.get("HERDR_SOCKET_PATH", "") if live_caller else ""
    endpoint_unproven = bool(harness == "codex" and server and not socket_value)
    if live_caller and harness == "codex" and sid:
        from pane_ownership import _caller_runtime, codex_caller_binding
        caller, service = _caller_runtime(os.getpid(), "codex")
        if caller is not None and service:
            binding = codex_caller_binding(sid, environ=env)
            if binding:
                return pane_seat_from_binding(binding)
            # A shared daemon's inherited pane values belong to no caller.
            pane = ""
            server = None
            socket_value = ""
            endpoint_unproven = True
    if live_caller and not endpoint_unproven and not pane and harness == "codex" and sid:
        pane = codex_pane_for_session(sid, str(cwd or ""), source)
    from pane_ownership import verified_pane
    socket_path = (env.get("HERDR_SOCKET_PATH", "")
                   if live_caller and harness == "codex" else None)
    if (live_caller or harness != "codex") and not endpoint_unproven:
        pane = verified_pane(pane, harness, sid, executable=_herdr_executable(),
                             server=server, socket_path=socket_path)
    if pane:
        endpoint = _endpoint_scope(env if live_caller else {})
        key = _digest("pane", endpoint, pane) if endpoint else _digest("pane", pane)
        return Seat("pane", key, pane, harness or "", "", endpoint=endpoint)
    from session_tidy_native import caller_target
    native = caller_target(harness, env)
    if native:
        return Seat("native", _digest("native", harness, native["home"], native["job"], native["start"]),
                    harness=harness or "", native=native)
    if not project:
        return Seat("project", "", harness=harness or "", sid=sid or "")
    found = seat_for_project(harness or "unknown", project_key_for(cwd))
    return dataclasses.replace(found, sid=sid or "")


# ---------------------------------------------------------------------------
# Seat ledger
# ---------------------------------------------------------------------------

def _ledger_path(seat: Seat) -> Path:
    return state_root() / "sessions" / f"{seat.key}.jsonl"


def _read_ledger_lines(seat: Seat) -> list[dict]:
    raw = read_bytes(_ledger_path(seat))
    entries = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("sid"):
            entries.append(item)
    return entries


def _append_ledger_entry(seat: Seat, entry: dict) -> None:
    """Append a caller-locked ledger row and fold without dropping tidy provenance."""
    path = _ledger_path(seat)
    ensure_dir(path.parent)
    _reject_symlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    _fold_ledger(seat)


def record_tidy_claim(seat: Seat, *, harness: str, sid: str, claim_id: str, nonce: str,
                      token: str, digest: str, origin: str, operation_id: str,
                      sent_seq: int, state: str, now: Optional[float] = None,
                      arrival_seq: Optional[int] = None, target_sid: str = "",
                      acceptance: Optional[dict] = None) -> None:
    """Persist one claim transition in the existing seat ledger (caller holds its lock)."""
    if not claim_id or not nonce or not token or not digest or harness not in HARNESSES or not sid:
        return
    entry = {"ts": now_epoch() if now is None else now, "harness": harness, "sid": sid,
             "event": "tidy-claim", "claim_id": claim_id[:96], "nonce": nonce[:64],
             "token": token[:64], "digest": digest[:64], "origin": origin[:24],
             "operation_id": operation_id[:96], "sent_seq": max(0, int(sent_seq)),
             "state": state[:24]}
    if target_sid:
        entry["target_sid"] = target_sid[:160]
    if arrival_seq is not None and not isinstance(arrival_seq, bool) and int(arrival_seq) >= 0:
        entry["arrival_seq"] = int(arrival_seq)
    if isinstance(acceptance, dict):
        entry["acceptance"] = acceptance
    _append_ledger_entry(seat, entry)


def tidy_claim_by_id(seat: Seat, claim_id: str) -> Optional[dict]:
    if not claim_id:
        return None
    return next((row for row in reversed(_read_ledger_lines(seat))
                 if row.get("event") == "tidy-claim" and row.get("claim_id") == claim_id), None)


def matching_tidy_claim(seat: Seat, harness: str, sid: str, prompt: str, sequence: int) -> Optional[dict]:
    """Find an exact, still-classifiable request claim for this session and prompt boundary."""
    if not prompt or harness not in HARNESSES or not sid:
        return None
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    latest: dict[str, dict] = {}
    for row in _read_ledger_lines(seat):
        if row.get("event") == "tidy-claim" and row.get("claim_id"):
            latest[str(row["claim_id"])] = row
    candidates = [row for row in latest.values()
                  if row.get("harness") == harness and row.get("digest") == digest
                  and row.get("state") not in {"released", "superseded"}
                  and int(row.get("sent_seq", sequence) or 0) < sequence
                  and row.get("target_sid") in (None, "", sid)
                  and (row.get("sid") == sid or row.get("target_sid") == sid
                       or row.get("origin") in ("status-only", "continue") and row.get("sid") == "-")]
    return max(candidates, key=lambda row: (int(row.get("sent_seq", 0) or 0), float(row.get("ts", 0) or 0)),
               default=None)


def real_prompt_frontier(seat: Seat, harness: str, sid: str) -> Optional[dict]:
    """Latest actual user prompt for this exact session, independent of ledger throttling."""
    rows = [row for row in _read_ledger_lines(seat)
            if row.get("event") == "real-prompt" and row.get("harness") == harness and row.get("sid") == sid]
    return max(rows, key=lambda row: int(row.get("sequence", 0) or 0), default=None)


def record_real_prompt(seat: Seat, harness: str, sid: str, sequence: int, now: float) -> None:
    """Persist user activity after prompt-source classification (caller holds the seat lock)."""
    _append_ledger_entry(seat, {"ts": now, "harness": harness, "sid": sid,
                                "event": "real-prompt", "sequence": max(0, int(sequence))})


def mark_prompt_source(seat: Seat, harness: str, sid: str, sequence: int, source: str,
                       claim_id: str = "") -> None:
    """Tag the current physical prompt boundary after its exact source has been classified."""
    path = _prompt_seq_path(seat)
    current = read_json(path)
    if (not isinstance(current, dict) or int(current.get("seq", -1)) != sequence
            or current.get("harness") != harness or current.get("sid") != sid):
        return
    current["source"] = source
    if claim_id:
        current["claim_id"] = claim_id[:96]
    else:
        current.pop("claim_id", None)
    atomic_write_json(path, current)


def _record_tidy_clear_succession_locked(seat: Seat, harness: str, sid: str, now: float) -> None:
    """Record the confirmed session edge from a matching tidy-clear reservation only."""
    if seat.kind not in ("pane", "native"):
        return
    try:
        import session_tidy_clear
        booking = session_tidy_clear.read_reservation(seat.key)
    except Exception:
        return
    if not isinstance(booking, dict) or booking.get("harness") != harness:
        return
    old = booking.get("sid")
    observed = booking.get("observed")
    observed_sid = observed.get("sid") if isinstance(observed, dict) else None
    if (not isinstance(old, str) or not old or old == sid
            or not (observed_sid == sid or booking.get("new_session") == sid)):
        return
    if isinstance(observed, dict) and observed.get("harness") not in (None, harness):
        return
    if any(row.get("event") == "succession" and row.get("harness") == harness
           and row.get("sid") == sid and row.get("from") == old
           for row in _read_ledger_lines(seat)):
        return
    _append_ledger_entry(seat, {"ts": now, "harness": harness, "sid": sid,
                                "event": "succession", "from": old, "source": "tidy-clear"})


def prompt_state(seat: Seat) -> Optional[dict]:
    data = read_json(_prompt_seq_path(seat))
    return data if isinstance(data, dict) else None


def session_summary(seat: Seat) -> dict[tuple[str, str], dict]:
    """Fold the ledger: ``(harness, sid) -> first_seen, last_seen, epoch, transcript, cwd, ...``."""
    out: dict[tuple[str, str], dict] = {}
    for item in _read_ledger_lines(seat):
        if item.get("event") in {"handover", "succession", "steward-release", "tidy-claim", "real-prompt"}:
            # Relations, revocation and prompt provenance are not session activity.
            continue
        key = (str(item.get("harness") or ""), str(item["sid"]))
        row = out.setdefault(key, {"harness": key[0], "sid": key[1], "first_seen": item.get("first_seen", item.get("ts", 0)),
                                   "last_seen": 0, "epoch": 0, "transcript": "", "cwd": "",
                                   "last_event": "", "last_event_at": 0, "last_compact_at": 0})
        ts = float(item.get("ts", 0) or 0)
        row["first_seen"] = min(float(row["first_seen"] or ts), float(item.get("first_seen", ts) or ts))
        if ts >= row["last_seen"]:
            row["last_seen"] = ts
            row["last_event"] = item.get("event", "")
            row["last_event_at"] = ts
        row["epoch"] = max(row["epoch"], int(item.get("epoch", 0) or 0))
        if item.get("transcript"):
            row["transcript"] = item["transcript"]
        if item.get("cwd"):
            row["cwd"] = item["cwd"]
        if item.get("event") == "compact" or item.get("source") == "compact":
            row["last_compact_at"] = max(row["last_compact_at"], ts)
    return out


def latest_session(seat: Seat, harness: Optional[str] = None) -> Optional[dict]:
    rows = [r for r in session_summary(seat).values() if not harness or r["harness"] == harness]
    return max(rows, key=lambda r: r["last_seen"]) if rows else None


def _project_seat_lineage(rows: list[dict], cards: dict, locations: dict,
                          *, harness=None, sid=None) -> list[dict]:
    """Pure projection of already-read seat rows, cards and transcript locations."""
    grouped = {}
    starts = {}
    edges = {}
    for row in rows:
        name, session = str(row.get("harness") or ""), str(row.get("sid") or "")
        if not name or not session:
            continue
        key = (name, session)
        if row.get("event") in {"handover", "succession"}:
            source = row.get("from")
            if isinstance(source, dict):
                source = source.get("sid") if source.get("harness") == name else None
            if isinstance(source, str) and source and source != session:
                edges.setdefault(key, set()).add(source)
            continue
        item = grouped.setdefault(key, {"sid": session, "harness": name,
                                        "started_at": None, "source": "unknown",
                                        "last_seen": 0})
        seen = row.get("first_seen", row.get("ts"))
        if isinstance(seen, (int, float)) and not isinstance(seen, bool) and seen > 0:
            item["started_at"] = seen if item["started_at"] is None else min(item["started_at"], seen)
        ts = row.get("ts", row.get("last_seen", 0))
        if isinstance(ts, (int, float)) and ts >= item["last_seen"]:
            item["last_seen"] = ts
            source = (row.get("source") if row.get("event") == "start"
                      else row.get("latest_start_source", row.get("start_source")))
            if isinstance(source, str) and source in {
                    "clear", "compact", "startup", "resume", "select", "fork"}:
                starts[key] = (float(ts), source)
    ordered = sorted(grouped.values(), key=lambda item: item["last_seen"], reverse=True)
    if sid:
        ordered.sort(key=lambda item: (item["sid"] != sid, -float(item["last_seen"])))
    output = []
    for item in ordered:
        key = (item["harness"], item["sid"])
        if key in starts:
            item["source"] = starts[key][1]
        card_rows = cards.get(key, [])
        item["cards"] = card_rows
        item["card"] = (card_rows[-1] if card_rows else
                        {"generation": None, "path": None, "status": "none"})
        item["transcript"] = locations.get(key) or {"path": None, "status": "missing"}
        item["relation"] = "unknown"
        item.pop("last_seen", None)
        if harness and item["harness"] != harness:
            continue
        output.append(item)
    for index, item in enumerate(output):
        key = (item["harness"], item["sid"])
        predecessors = edges.get(key, set())
        item["predecessor"] = None
        if len(predecessors) == 1:
            item["relation"] = "confirmed"
            item["predecessor"] = {"harness": item["harness"], "sid": next(iter(predecessors))}
        elif len(predecessors) > 1:
            item["relation"] = "unknown"
        elif index + 1 < len(output) and output[index + 1]["harness"] == item["harness"]:
            item["relation"] = "chronological"
    return output


def _lineage_card_status(path: Path) -> str:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return "unreadable"
        with path.open("rb") as handle:
            json.loads(handle.read(256 * 1024).decode("utf-8"))
        return "available"
    except FileNotFoundError:
        return "expired"
    except (OSError, ValueError, UnicodeError):
        return "unreadable"


def _lineage_transcript(harness: str, sid: str, hint: str) -> dict:
    try:
        import tidy_transcripts
        path = None
        if hint:
            hinted = Path(hint).expanduser()
            try:
                info = hinted.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                return {"path": str(hinted), "status": "unreadable"}
            else:
                if not stat.S_ISREG(info.st_mode):
                    return {"path": str(hinted), "status": "unreadable"}
                path = hinted
        path = path or tidy_transcripts.locate_transcript(harness, sid, hint or None)
        if path is None:
            return {"path": None, "status": "missing"}
        info = Path(path).lstat()
        if not stat.S_ISREG(info.st_mode):
            return {"path": str(path), "status": "unreadable"}
        with Path(path).open("rb") as handle:
            handle.read(1)
        return {"path": str(path), "status": "available"}
    except FileNotFoundError:
        return {"path": None, "status": "missing"}
    except (OSError, ValueError, TypeError):
        return {"path": None, "status": "unreadable"}


def seat_lineage(seat: Seat, *, harness=None, sid=None) -> list[dict]:
    """Read the seat's bounded existing lineage without updating any state."""
    rows = _read_ledger_lines(seat)
    sessions = session_summary(seat)
    cards, locations = {}, {}
    for key, summary in sessions.items():
        name, session = key
        if harness and name != harness:
            continue
        author_seat = dataclasses.replace(seat, harness=name, sid=session)
        current_path = card_json_path(author_seat)
        history_path = state_root() / "card-history" / card_key(author_seat)
        found = []
        current = read_json(current_path)
        if isinstance(current, dict) and (current.get("author") or {}).get("sid") == session:
            found.append(current)
        elif _lineage_card_status(current_path) == "unreadable":
            found.append({"generation": None, "author": {"harness": name, "sid": session},
                          "_path": current_path, "_status": "unreadable"})
        try:
            for path in sorted(history_path.glob("*.json")):
                card = read_json(path)
                if (isinstance(card, dict) and (card.get("author") or {}).get("harness") == name
                        and (card.get("author") or {}).get("sid") == session):
                    found.append(card)
                elif _lineage_card_status(path) == "unreadable":
                    with contextlib.suppress(ValueError):
                        generation = int(path.stem)
                        found.append({"generation": generation, "author": {"harness": name, "sid": session},
                                      "_path": path, "_status": "unreadable"})
        except OSError:
            pass
        found.sort(key=lambda card: int(card.get("generation", 0) or 0))
        cards[key] = [{"generation": (int(card.get("generation", 0) or 0)
                                      if card.get("generation") is not None else None),
                       "path": str(card.get("_path") or
                                   (current_path if card is current else
                                    history_path / f"{int(card.get('generation', 0)):08d}.json")),
                       "status": str(card.get("_status") or "available")} for card in found]
        if not found:
            generations = [row.get("card_generation") for row in rows
                           if row.get("sid") == session and row.get("harness") == name
                           and row.get("event") == "card"]
            generations = [value for value in generations
                           if isinstance(value, int) and not isinstance(value, bool)]
            cards[key] = ([{"generation": max(generations), "path": None, "status": "expired"}]
                          if generations else [])
        locations[key] = _lineage_transcript(name, session, str(summary.get("transcript") or ""))
    return _project_seat_lineage(rows, cards, locations, harness=harness, sid=sid)


def _confirmed_predecessor(lineage: list[dict], harness: str, sid: str) -> Optional[dict]:
    """The session an explicit handover edge names as ``sid``'s predecessor; never a neighbour."""
    current = next((row for row in lineage
                    if row.get("harness") == harness and row.get("sid") == sid), None)
    identity = (current or {}).get("predecessor") if (current or {}).get("relation") == "confirmed" else None
    if not isinstance(identity, dict) or not identity.get("harness") or not identity.get("sid"):
        return None
    name, session = identity["harness"], identity["sid"]
    found = next((row for row in lineage
                  if row.get("harness") == name and row.get("sid") == session), None)
    return found or {"harness": name, "sid": session,
                     "transcript": _lineage_transcript(name, session, "")}


def _lineage_row_matches_seat(row: dict, seat: Seat) -> bool:
    if "pane" in row and row.get("pane") != seat.pane:
        return False
    endpoint = row.get("endpoint")
    if "endpoint" in row and endpoint not in (None, seat.endpoint):
        return False
    metadata = row.get("seat")
    if not isinstance(metadata, dict):
        return True
    if "kind" in metadata and metadata.get("kind") != "pane":
        return False
    if "pane" in metadata and metadata.get("pane") != seat.pane:
        return False
    if "key" in metadata and metadata.get("key") != seat.key:
        return False
    endpoint = metadata.get("endpoint")
    return "endpoint" not in metadata or endpoint in (None, seat.endpoint)


def _pane_repository_map(rows: list[dict], seat: Seat, resolve_gitdir) -> dict[tuple[str, str], str]:
    repositories: dict[tuple[str, str], set[str]] = {}
    conflicts: set[tuple[str, str]] = set()
    resolved_cwds = {}
    for row in rows:
        harness, sid = row.get("harness"), row.get("sid")
        if not isinstance(harness, str) or not isinstance(sid, str) or not harness or not sid:
            continue
        identity = (harness, sid)
        if not _lineage_row_matches_seat(row, seat):
            conflicts.add(identity)
            continue
        cwd = row.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        if cwd not in resolved_cwds:
            try:
                result = resolve_gitdir(cwd)
                resolved_cwds[cwd] = (result[1] if isinstance(result, (list, tuple))
                                      and len(result) > 1 else "")
            except Exception:
                resolved_cwds[cwd] = ""
        repository = resolved_cwds[cwd]
        if repository:
            repositories.setdefault(identity, set()).add(str(repository))
    return {identity: next(iter(values)) for identity, values in repositories.items()
            if len(values) == 1 and identity not in conflicts}


def _pane_lineage_inputs(harness: str, sid: str, pane: str, cwd: str, *, endpoint=None):
    if harness not in HARNESSES or not sid or not pane or not cwd:
        return None
    try:
        tools = str(Path(__file__).resolve().parents[1] / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.gitinfo import resolve_gitdir
        resolved = resolve_gitdir(cwd)
        repository = resolved[1] if isinstance(resolved, (list, tuple)) and len(resolved) > 1 else ""
        if not repository:
            return None
        current = _pane_seat_of(pane, endpoint=endpoint)
        current_rows = _read_ledger_lines(current)
        current_repositories = _pane_repository_map(current_rows, current, resolve_gitdir)
        if current_repositories.get((harness, sid)) != repository:
            return None
        inputs = [(current, current_rows, current_repositories)]
        if current.endpoint:
            # Endpoint-free keys are legacy only. Never enumerate or scan another endpoint.
            legacy = Seat("pane", _digest("pane", pane), pane, harness)
            legacy_rows = _read_ledger_lines(legacy)
            legacy_repositories = _pane_repository_map(legacy_rows, legacy, resolve_gitdir)
            if legacy_repositories.get((harness, sid)) == repository:
                inputs.append((legacy, legacy_rows, legacy_repositories))
        return repository, inputs, resolve_gitdir
    except Exception:
        return None


def _previous_distinct_start(rows: list[dict], index: int, seat: Seat, harness: str,
                             successor: str, repositories: dict, repository: str) -> str:
    raw_seen = False
    previous = ""
    for row in rows[:index]:
        if (row.get("harness") != harness or row.get("event") != "start"
                or not _lineage_row_matches_seat(row, seat)):
            continue
        old = row.get("sid")
        if not isinstance(old, str) or not old:
            continue
        raw_seen = True
        if old != successor:
            previous = old
    if raw_seen:
        return previous

    # Folding retains each session's last native-start provenance. Use it only
    # when no raw start remains in this ledger; never compare timestamps across keys.
    current_row = rows[index]
    current_at = current_row.get("ts")
    if not isinstance(current_at, (int, float)) or isinstance(current_at, bool):
        return ""
    candidates: dict[str, float] = {}
    for row in rows:
        if (row.get("harness") != harness or row.get("event") != "summary"
                or not _lineage_row_matches_seat(row, seat)):
            continue
        old = row.get("sid")
        source = row.get("latest_start_source", row.get("start_source"))
        started = row.get("latest_start_at", row.get("start_at"))
        if (not isinstance(old, str) or not old or old == successor
                or source not in {"startup", "resume", "select", "clear", "compact", "fork"}
                or not isinstance(started, (int, float)) or isinstance(started, bool)
                or started <= 0 or started >= current_at
                or repositories.get((harness, old)) != repository):
            continue
        candidates[old] = float(started)
    if not candidates:
        return ""
    latest = max(candidates.values())
    winners = [old for old, started in candidates.items() if started == latest]
    return winners[0] if len(winners) == 1 else ""


def _confirmed_clear_start_edges(seat: Seat, rows: list[dict], repositories: dict,
                                 harness: str, repository: str, resolve_gitdir) -> set[tuple[str, str]]:
    starts = [(index, row) for index, row in enumerate(rows)
              if row.get("event") == "start" and row.get("source") == "clear"
              and row.get("harness") == harness and row.get("sid")
              and _lineage_row_matches_seat(row, seat)]
    if not starts:
        return set()
    try:
        import session_tidy_clear
        booking = session_tidy_clear.read_reservation(seat.key)
    except Exception:
        return set()
    if not isinstance(booking, dict):
        return set()
    booked_seat = booking.get("seat")
    observed = booking.get("observed")
    old = booking.get("sid")
    successor = booking.get("new_session")
    if (booking.get("schema") != 1 or booking.get("status") != "cleared"
            or booking.get("harness") != harness or not isinstance(old, str) or not old
            or not isinstance(successor, str) or not successor or old == successor
            or not isinstance(booked_seat, dict) or booked_seat.get("kind") != "pane"
            or booked_seat.get("key") != seat.key or booked_seat.get("pane") != seat.pane
            or ("endpoint" in booked_seat
                and booked_seat.get("endpoint") not in (None, seat.endpoint))
            or not isinstance(observed, dict) or observed.get("harness") != harness
            or observed.get("sid") != successor):
        return set()
    booking_cwd = booking.get("cwd")
    if not isinstance(booking_cwd, str) or not booking_cwd:
        return set()
    try:
        resolved = resolve_gitdir(booking_cwd)
        booking_repository = resolved[1] if isinstance(resolved, (list, tuple)) and len(resolved) > 1 else ""
    except Exception:
        return set()
    if booking_repository != repository:
        return set()

    edges: set[tuple[str, str]] = set()
    for index, row in starts:
        if row.get("sid") != successor:
            continue
        previous = _previous_distinct_start(rows, index, seat, harness, successor,
                                            repositories, repository)
        if (previous == old and repositories.get((harness, previous)) == repository
                and repositories.get((harness, successor)) == repository):
            edges.add((previous, successor))
    return edges


def _confirmed_predecessors_from(inputs, harness: str, sid: str, repository: str,
                                 resolve_gitdir) -> list[str]:
    edges: set[tuple[str, str]] = set()
    for seat, rows, repositories in inputs:
        for row in rows:
            if (row.get("event") != "handover" or row.get("harness") != harness
                    or row.get("source") not in {"clear", "reservation", "first-message"}
                    or not _lineage_row_matches_seat(row, seat)):
                continue
            successor = row.get("sid")
            predecessor = row.get("from")
            if isinstance(predecessor, dict):
                if predecessor.get("harness") != harness:
                    continue
                predecessor = predecessor.get("sid")
            if (not isinstance(predecessor, str) or not predecessor
                    or not isinstance(successor, str) or not successor or predecessor == successor):
                continue
            if (repositories.get((harness, predecessor)) == repository
                    and repositories.get((harness, successor)) == repository):
                edges.add((predecessor, successor))
        edges.update(_confirmed_clear_start_edges(
            seat, rows, repositories, harness, repository, resolve_gitdir))

    chain = []
    visited = {sid}
    current = sid
    while True:
        predecessors = {old for old, new in edges if new == current}
        if len(predecessors) != 1:
            break
        predecessor = next(iter(predecessors))
        if predecessor in visited:
            break
        visited.add(predecessor)
        chain.append(predecessor)
        current = predecessor
    return chain


def confirmed_pane_predecessors(harness: str, sid: str, pane: str, cwd: str, *, endpoint=None) -> list[str]:
    """Return only confirmed same-harness clear/handover predecessors for display."""
    state = _pane_lineage_inputs(harness, sid, pane, cwd, endpoint=endpoint)
    if state is None:
        return []
    repository, inputs, resolver = state
    return _confirmed_predecessors_from(inputs, harness, sid, repository, resolver)


def pane_session_aliases(harness: str, sid: str, pane: str, cwd: str, *, endpoint=None) -> list[str]:
    """Compatibility aliases plus confirmed predecessors, for display only.

    The compatibility join keeps its eight-recent-ID bound. Confirmed lineage is
    separate and may extend beyond that bound; neither result changes execution or
    notification identity.
    """
    state = _pane_lineage_inputs(harness, sid, pane, cwd, endpoint=endpoint)
    if state is None:
        return []
    repository, inputs, resolver = state
    compatibility = []
    for seat, _rows, repositories in inputs:
        summaries = session_summary(seat)
        compatibility.append(sorted(
            ((row["sid"], float(row.get("last_seen", 0) or 0))
             for key, row in summaries.items()
             if key[0] == harness and key[1] != sid and repositories.get(key) == repository),
            key=lambda item: item[1], reverse=True))
    ordered = []
    seen = {sid}
    for local in compatibility:
        for old, _last_seen in local:
            if old not in seen:
                seen.add(old)
                ordered.append(old)
                if len(ordered) == 8:
                    break
        if len(ordered) == 8:
            break
    predecessors = _confirmed_predecessors_from(inputs, harness, sid, repository, resolver)
    return list(dict.fromkeys([*ordered, *predecessors]))


def session_start_source(harness: str, sid: str, pane: str, *, endpoint=None) -> Optional[str]:
    """The recorded native start source, not a new or inferred lifecycle event."""
    return session_start_context(harness, sid, pane, endpoint=endpoint).get("session_start_source")


def session_start_context(harness: str, sid: str, pane: str, *, endpoint=None) -> dict:
    """Return the latest native start details for this exact pane seat and session."""
    if harness not in HARNESSES or not sid or not pane:
        return {}
    for row in reversed(_read_ledger_lines(_pane_seat_of(pane, endpoint=endpoint))):
        if row.get("harness") != harness or row.get("sid") != sid:
            continue
        if row.get("event") != "start" and not row.get("start_at"):
            continue
        source = (row.get("source") if row.get("event") == "start"
                  else row.get("latest_start_source", row.get("start_source")))
        context = {}
        if source in {"startup", "resume", "select", "clear", "compact", "fork"}:
            context["session_start_source"] = source
        sequence = row.get("session_seq", row.get("latest_start_seq", row.get("start_seq")))
        if sequence is None and context.get("session_start_source"):
            started = (row.get("ts") if row.get("event") == "start"
                       else row.get("latest_start_at", row.get("start_at")))
            if isinstance(started, (int, float)) and not isinstance(started, bool) and started > 0:
                sequence = int(started * 1_000_000)
        if type(sequence) is int and sequence > 0:
            context["session_seq"] = sequence
        transcript = row.get("transcript")
        if isinstance(transcript, str) and transcript:
            context["transcript"] = transcript
        wire = None
        resume_argv = row.get("resume_argv")
        if isinstance(resume_argv, list) and resume_argv:
            try:
                tools = str(Path(__file__).resolve().parents[1] / "tools")
                if tools not in sys.path:
                    sys.path.insert(0, tools)
                from fleet.process_identity import native_resume_wire_argv
                wire = native_resume_wire_argv(resume_argv, harness, sid)
                if wire is not None:
                    context["resume_argv"] = wire
            except Exception:
                wire = None
        if wire is not None:
            launch_cwd = row.get("launch_cwd")
            if isinstance(launch_cwd, str) and launch_cwd:
                context["launch_cwd"] = launch_cwd
        return context
    return {}


def record_event(seat: Seat, harness: str, sid: str, event: str, *, source: str = "",
                 transcript: str = "", cwd: str = "", now: Optional[float] = None,
                 bump_epoch: bool = False, compact_identity: str = "",
                 resume_argv=None, launch_cwd: str = "",
                 compact_context: Optional[dict] = None,
                 card_generation: Optional[int] = None) -> dict:
    """Append one ledger line (caller holds the seat lock). Returns the session row."""
    now = now_epoch() if now is None else now
    summary = session_summary(seat).get((harness, sid))
    epoch = summary["epoch"] if summary else 0
    if event == "prompt" and summary and not transcript \
            and now - summary["last_seen"] < PROMPT_LEDGER_THROTTLE_SEC and not bump_epoch:
        return summary
    if bump_epoch:
        epoch += 1
    entry = {"ts": now, "harness": harness, "sid": sid, "event": event, "epoch": epoch}
    if source:
        entry["source"] = source
    if event == "start" and source in {"startup", "resume", "select", "clear", "compact", "fork"}:
        entry["session_seq"] = max(1, int(now * 1_000_000))
    if event == "start" and isinstance(resume_argv, list):
        try:
            tools = str(Path(__file__).resolve().parents[1] / "tools")
            if tools not in sys.path:
                sys.path.insert(0, tools)
            from fleet.process_identity import native_resume_wire_argv
            wire = native_resume_wire_argv(resume_argv, harness, sid)
        except Exception:
            wire = None
        if wire is not None:
            entry["resume_argv"] = wire
            if isinstance(launch_cwd, str) and launch_cwd.startswith("/"):
                entry["launch_cwd"] = launch_cwd
    if transcript:
        entry["transcript"] = transcript
    if event == "card" and isinstance(card_generation, int) and not isinstance(card_generation, bool):
        entry["card_generation"] = max(1, card_generation)
    if cwd:
        entry["cwd"] = cwd
    if event == "compact" and compact_identity:
        entry["compact_identity"] = compact_identity
    if event == "compact" and compact_context:
        limits = {"identity": 64, "trigger": 8, "transcript": 440, "transcript_identity": 64,
                  "turn_id": 96, "summary_id": 96, "parent_id": 96, "tail_start_id": 96,
                  "compaction_part_id": 96}
        compact = {key: _cut_bytes(str(compact_context[key]), limit)[0]
                   for key, limit in limits.items() if compact_context.get(key) is not None}
        for key in ("boundary_offset", "prompt_seq", "card_generation"):
            value = compact_context.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                compact[key] = value
        compact["card_request_required"] = compact_context.get("card_request_required") is True
        entry["compact"] = compact
    if summary is None:
        entry["first_seen"] = now
    _append_ledger_entry(seat, entry)
    return session_summary(seat).get((harness, sid), entry)


def _fold_ledger(seat: Seat) -> None:
    """Keep the ledger small: old sessions become one summary line each."""
    lines = _read_ledger_lines(seat)
    if len(lines) <= LEDGER_FOLD_LINES:
        return
    # Registered handovers keep the existing bounded retention. Steward lineage is
    # retained by generation/watch identity so a marker-less holder and a release
    # remain resolvable after ordinary session rows fold.
    handovers = [(index, row) for index, row in enumerate(lines)
                 if row.get("event") in {"handover", "succession"}]
    keep_relation_indices = {index for index, _row in handovers[-HANDOVER_KEEP_ROWS:]}
    latest_role_rows, latest_watch_rows = {}, {}
    release_rows = []
    for index, item in enumerate(lines):
        if item.get("event") == "steward-release":
            release_rows.append((index, item))
        steward = item.get("steward") if isinstance(item.get("steward"), dict) else {}
        for relation in steward.get("roles") or ():
            if isinstance(relation, dict) and relation.get("generation"):
                latest_role_rows[(str(relation.get("seat_key") or item.get("seat_key") or ""),
                                  str(relation["generation"]))] = index
        for watch in steward.get("watches") or ():
            if isinstance(watch, dict) and watch.get("watch_id"):
                latest_watch_rows[str(watch["watch_id"])] = (index, watch)
    keep_relation_indices.update(latest_role_rows.values())
    for watch_id, (index, watch) in latest_watch_rows.items():
        storage_root = watch.get("storage_root")
        if storage_root:
            ack = Path(str(storage_root)) / "peer-watches" / f"{watch_id}.ack.json"
            if ack.exists():
                continue
        keep_relation_indices.add(index)
    # A release can suppress an origin marker that remains on its old session
    # after the role moved. Keep these compact tombstones in the same ledger.
    keep_relation_indices.update(index for index, _row in release_rows)
    relations = [row for index, row in enumerate(lines) if index in keep_relation_indices
                 and row.get("event") in {"handover", "succession", "steward-release"}]
    claims: dict[str, dict] = {}
    real_frontiers: dict[tuple[str, str], dict] = {}
    activity = [l for l in lines if l.get("event") not in {
        "handover", "succession", "steward-release", "tidy-claim", "real-prompt"}]
    for item in lines:
        if item.get("event") == "tidy-claim" and item.get("claim_id"):
            claims[str(item["claim_id"])] = item
        elif item.get("event") == "real-prompt":
            key = (str(item.get("harness") or ""), str(item.get("sid") or ""))
            prior = real_frontiers.get(key)
            if prior is None or int(item.get("sequence", 0) or 0) >= int(prior.get("sequence", 0) or 0):
                real_frontiers[key] = item
    claim_rows = sorted(claims.values(), key=lambda row: float(row.get("ts", 0) or 0))[-TIDY_CLAIM_KEEP_ROWS:]
    provenance = claim_rows + list(real_frontiers.values())
    recent = activity[-LEDGER_KEEP_RAW:]
    older = activity[:-LEDGER_KEEP_RAW]
    folded: dict[tuple[str, str], dict] = {}
    for item in older:
        key = (str(item.get("harness") or ""), str(item["sid"]))
        row = folded.setdefault(key, {"harness": key[0], "sid": key[1], "event": "summary", "epoch": 0,
                                      "first_seen": item.get("first_seen", item.get("ts", 0)), "ts": 0})
        row["first_seen"] = min(row["first_seen"], item.get("first_seen", item.get("ts", 0)))
        row["ts"] = max(row["ts"], item.get("ts", 0))
        row["epoch"] = max(row["epoch"], int(item.get("epoch", 0) or 0))
        origin_source = item.get("source") if item.get("event") == "start" else item.get("start_source")
        origin_at = item.get("ts") if item.get("event") == "start" else item.get("start_at")
        if (origin_source in {"startup", "resume", "select", "clear", "compact", "fork"}
                and isinstance(origin_at, (int, float)) and not isinstance(origin_at, bool) and origin_at > 0
                and (not row.get("start_at") or origin_at < float(row["start_at"]))):
            row.update(start_source=origin_source, start_at=origin_at)

        latest_source = (item.get("source") if item.get("event") == "start"
                         else item.get("latest_start_source", item.get("start_source")))
        latest_at = (item.get("ts") if item.get("event") == "start"
                     else item.get("latest_start_at", item.get("start_at")))
        if (latest_source in {"startup", "resume", "select", "clear", "compact", "fork"}
                and isinstance(latest_at, (int, float)) and not isinstance(latest_at, bool) and latest_at > 0
                and latest_at >= float(row.get("latest_start_at", 0) or 0)):
            row.update(latest_start_source=latest_source, latest_start_at=latest_at,
                       start_seq=item.get("session_seq", item.get("start_seq")))
            for field in ("resume_argv", "launch_cwd"):
                row.pop(field, None)
                if field in item:
                    row[field] = item[field]
        for field in ("transcript", "cwd"):
            if item.get(field):
                row[field] = item[field]
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                    for r in list(folded.values()) + relations + provenance + recent)
    atomic_write(_ledger_path(seat), body.encode("utf-8"))


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def card_key(seat: Seat) -> str:
    """Return the storage key for this author; sid-less project calls retain the shared legacy key."""
    if seat.kind != "project" or not seat.sid:
        return seat.key
    keyed = _digest("project-card", seat.key, seat.sid)
    if (state_root() / "cards" / f"{keyed}.json").exists():
        return keyed
    # Keep the old shared file and its history/receipts with its original author.
    legacy = read_json(state_root() / "cards" / f"{seat.key}.json")
    author = legacy.get("author") if isinstance(legacy, dict) else None
    if isinstance(legacy, dict) and legacy.get("schema") == SCHEMA and isinstance(legacy.get("body"), str) \
            and isinstance(author, dict) \
            and (author.get("harness"), author.get("sid")) == (seat.harness, seat.sid):
        return seat.key
    return keyed


def card_json_path(seat: Seat) -> Path:
    return state_root() / "cards" / f"{card_key(seat)}.json"


def card_text_path(seat: Seat) -> Path:
    return state_root() / "cards" / f"{card_key(seat)}.md"


def _project_latest_path(seat: Seat) -> Path:
    return state_root() / "project-latest" / f"{seat.key}.json"


def read_latest_project_card(seat: Seat) -> tuple[Optional[dict], Seat]:
    """Read the pointed-to project card in constant time, or the pre-pointer shared card."""
    path = _project_latest_path(seat)
    pointer = read_json(path)
    if path.exists() or path.is_symlink():
        if not isinstance(pointer, dict):
            return None, seat
        harness, sid = pointer.get("harness"), pointer.get("sid")
        if not isinstance(harness, str) or not harness or not isinstance(sid, str) or not sid:
            return None, seat
        chosen = dataclasses.replace(seat, harness=harness, sid=sid)
        card = read_latest_card(chosen)
        author = (card or {}).get("author") or {}
        if not card or (author.get("harness"), author.get("sid")) != (harness, sid):
            return None, seat
        return card, chosen
    # One fixed legacy path is the only fallback; never enumerate author card files.
    card = read_latest_card(dataclasses.replace(seat, sid=""))
    if not card:
        return None, seat
    author = card.get("author") or {}
    harness, sid = author.get("harness") or seat.harness, author.get("sid") or ""
    return card, dataclasses.replace(seat, harness=harness, sid=sid)


def _normalize_body(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())
    return text.strip()


def sanitize_body(text: str) -> str:
    return _cut_bytes(_normalize_body(text), CARD_BODY_MAX_BYTES)[0]


def _cut_bytes(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", "ignore"), True


def read_latest_card(seat: Seat) -> Optional[dict]:
    card = read_json(card_json_path(seat))
    if isinstance(card, dict) and card.get("schema") == SCHEMA and isinstance(card.get("body"), str):
        return card
    return None


def _write_card_pair(seat: Seat, card: dict, *, rollback_on_error: bool = False) -> None:
    json_path, text_path = card_json_path(seat), card_text_path(seat)
    old_json = read_bytes(json_path) if rollback_on_error else None
    old_text = read_bytes(text_path) if rollback_on_error else None
    try:
        atomic_write_json(json_path, card)
        atomic_write(text_path, render_card_text(card).encode("utf-8"))
    except BaseException:
        if rollback_on_error:
            for path, old in ((json_path, old_json), (text_path, old_text)):
                try:
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, old)
                except OSError:
                    pass
        raise


def write_card(seat: Seat, harness: str, sid: str, body: str, *, cwd: str = "",
               now: Optional[float] = None, prompt_seq: Optional[int] = None,
               origin: Optional[str] = None, compact_context: Optional[dict] = None) -> dict:
    """Write this author's newest card (caller holds the shared seat lock)."""
    now = now_epoch() if now is None else now
    compact = None
    if origin == "runtime-compact":
        summary_text = _normalize_body(body)
        if not summary_text:
            raise ValueError("empty compact summary")
        if len(summary_text.encode("utf-8")) > COMPACT_SUMMARY_MAX_BYTES:
            raise ValueError("compact summary exceeds admission limit")
        compact = dict(compact_context or {})
        metadata_limits = {"trigger": 8, "transcript": 440, "summary_id": 96,
                           "parent_id": 96, "tail_start_id": 96, "compaction_part_id": 96,
                           "transcript_identity": 64, "identity": 64}
        for key, limit in metadata_limits.items():
            value = compact.get(key)
            if value is not None:
                compact[key] = _cut_bytes(str(value), limit)[0]
        boundary = compact.get("boundary_offset")
        if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0:
            compact.pop("boundary_offset", None)
        compact.update(summary=summary_text, recent="", recent_partial=False)
        compact["source"] = compact.get("transcript") or ""
        body = _compact_body(compact)
    else:
        body = sanitize_body(body)
    summary = session_summary(seat).get((harness, sid))
    previous = read_latest_card(seat)
    generation = int(previous.get("generation", 0)) + 1 if previous else 1
    card = {
        "schema": SCHEMA,
        "seat": seat.as_dict(),
        "author": {"harness": harness, "sid": sid, "epoch": summary["epoch"] if summary else 0},
        "authored_at": iso_utc(now),
        "authored_at_epoch": now,
        "generation": generation,
        "cwd": cwd,
        "body": body,
    }
    if compact is not None:
        card["compact"] = compact
    if origin:
        card["origin"] = origin
    if prompt_seq is not None:
        card["prompt_seq"] = int(prompt_seq)    # lets enqueue notice a prompt typed after the card
    if previous and isinstance(previous.get("memory_refs"), dict):
        card["memory_refs"] = previous["memory_refs"]   # layer B outlives the card: only its revision moves it
    _write_card_pair(seat, card, rollback_on_error=compact is not None)
    if previous:
        try:
            history = ensure_dir(state_root() / "card-history" / card_key(seat))
            atomic_write_json(history / f"{int(previous.get('generation', 0)):08d}.json", previous)
            for old in sorted(history.glob("*.json"))[:-CARD_HISTORY_KEEP]:
                with contextlib.suppress(OSError):
                    old.unlink()
        except (OSError, StateError):
            if compact is None:
                raise
    if seat.kind == "project" and seat.sid:
        atomic_write_json(_project_latest_path(seat), {"harness": seat.harness or harness, "sid": seat.sid})
    return card


def _compact_body(compact: dict) -> str:
    summary_text = str(compact.get("summary") or "")
    recent = str(compact.get("recent") or "")
    source = _cut_bytes(_normalize_body(str(compact.get("source") or compact.get("transcript") or "")),
                        COMPACT_METADATA_MAX_BYTES)[0]
    partial_note = "\n[최근 대화의 일부만 보존됨; 원본 기록을 확인하세요.]" if compact.get("recent_partial") else ""
    footer = ("\n\n[압축 뒤 이어진 대화]" + partial_note + "\n" + recent) if recent \
        else ("\n\n" + partial_note.strip()) if partial_note else ""
    caution = "\n\n이 요약이 보지 못한 최근 대화가 있을 수 있습니다. 이어서 진행하기 전에 확인하세요."
    path = f"\n원본 대화 기록: {source}" if source else "\n원본 대화 기록 경로를 확인할 수 없습니다."
    result = summary_text + caution + path + footer
    if len(result.encode("utf-8")) > COMPACT_CARD_BODY_MAX_BYTES:
        raise ValueError("compact card exceeds bounded size")
    return result


def update_compact_card_locked(seat: Seat, generation: int, identity: str, *, recent: str,
                               source: str = "", recent_partial: bool = False) -> bool:
    """Idempotently add a bounded transcript tail without changing card identity."""
    card = read_latest_card(seat)
    compact = (card or {}).get("compact")
    if (not card or int(card.get("generation", 0)) != int(generation)
            or not isinstance(compact, dict) or compact.get("identity") != identity):
        return False
    compact["recent"] = _cut_bytes(_normalize_body(recent), COMPACT_RECENT_MAX_BYTES)[0]
    compact["recent_partial"] = bool(recent_partial)
    if source:
        compact["source"] = _cut_bytes(_normalize_body(source), COMPACT_METADATA_MAX_BYTES)[0]
    card["body"] = _compact_body(compact)
    _write_card_pair(seat, card, rollback_on_error=True)
    return True


def mark_card_handed_off(seat: Seat) -> None:
    """The latest card went to a peer session: this seat's window is not continued from it
    (takes the seat lock; a newer card starts unmarked)."""
    with seat_lock(seat.key):
        card = read_latest_card(seat)
        if card and not card.get("handed_off"):
            card["handed_off"] = True
            atomic_write_json(card_json_path(seat), card)


def render_card_text(card: dict) -> str:
    """The readable card file: header, the card body (layer A), and the "참고할 기억" section (layer B)."""
    author = card.get("author") or {}
    header = (f"# session card {card.get('generation')} — {card.get('authored_at')} "
              f"({author.get('harness', '')} {str(author.get('sid', ''))[:8]})\n\n")
    text = header + str(card.get("body", "")) + "\n"
    refs = card.get("memory_refs")
    if isinstance(refs, dict) and refs.get("revision"):
        text += "\n" + build_memory_injection(refs, MEMORY_REFS_BYTES + 600) + "\n"
    return text


STATUS_LABEL = {"applied": "정돈 결과", "partial": "일부만 반영", "failed": "정돈을 끝내지 못함"}


def build_memory_injection(refs: dict, room: int) -> str:
    """The "참고할 기억" section within ``room`` UTF-8 bytes: ids and headlines only, never a record body.

    The header, the coverage/reason lines and the result path always survive; list lines are dropped
    from the end first and counted as "외 N건".
    """
    head = f"[참고할 기억] 묶음 {refs.get('batch', '')} — {STATUS_LABEL.get(refs.get('status'), '')}".rstrip(" —")
    tail = [str(refs[k]) for k in ("coverage",) if refs.get(k)]
    if refs.get("detail"):
        tail.append(f"사유: {refs['detail']}")
    items = [f"- {r.get('id', '')} [{r.get('kind', '')}] {r.get('headline', '')}" for r in refs.get("refs") or []
             if isinstance(r, dict)]
    hidden = int(refs.get("more") or 0)
    path = f"전체 결과: {refs['result_path']}" if refs.get("result_path") else ""
    fixed = [head] + tail + ([path] if path else [])
    room = max(0, room)
    used = sum(len(x.encode("utf-8")) + 1 for x in fixed)
    shown: list = []
    for line in items:
        size = len(line.encode("utf-8")) + 1
        if used + size + (24 if len(shown) + 1 < len(items) or hidden else 0) > room:
            break
        shown.append(line)
        used += size
    omitted = len(items) - len(shown) + hidden
    lines = [head] + shown + ([f"외 {omitted}건"] if omitted else []) + tail + ([path] if path else [])
    return _cut_bytes("\n".join(lines), room)[0] if room else ""


def update_memory_refs(seat: Seat, *, batch: str, status: str, refs: list, more: int = 0, coverage: str = "",
                       detail: str = "", result_path: str = "", source_generation: Optional[int] = None,
                       now: Optional[float] = None) -> Optional[dict]:
    """Set the latest card's layer B ("참고할 기억") for a finished tidy batch; takes the seat lock.

    Only ``memory_refs`` changes: the card's body, author, time and generation stay, so layer A is
    never handed out again because of this.  ``revision`` rises by one per batch result for the seat
    and is what the hooks hand out once.  The same batch with the same result changes nothing, and a
    batch that finishes after a newer card was written still lands here (marked with the generation
    it was queued under).  No card, no layer: ``None``.
    """
    now = now_epoch() if now is None else now
    with seat_lock(seat.key):
        card = read_latest_card(seat)
        if not card:
            return None
        old = card.get("memory_refs") if isinstance(card.get("memory_refs"), dict) else {}
        new = {"batch": batch, "status": status, "refs": refs, "more": int(more), "coverage": coverage,
               "detail": detail, "result_path": result_path}
        if old and all(old.get(k) == v for k, v in new.items()):
            return old
        new["revision"] = int(old.get("revision", 0) or 0) + 1
        new["source_generation"] = int(card.get("generation", 0) or 0) if source_generation is None \
            else int(source_generation)
        new["updated"] = iso_utc(now)
        card["memory_refs"] = new
        atomic_write_json(card_json_path(seat), card)
        atomic_write(card_text_path(seat), render_card_text(card).encode("utf-8"))
        return new


# ---------------------------------------------------------------------------
# Consumption bookkeeping and notices
# ---------------------------------------------------------------------------

def _consumed_path(seat: Seat) -> Path:
    return state_root() / "consumed" / f"{card_key(seat)}.json"


def _write_consumed(seat: Seat, **fields) -> None:
    """Merge ``fields`` into the seat's consumption file (layer A and layer B receipts are independent)."""
    consumed = read_json(_consumed_path(seat))
    data = dict(consumed) if isinstance(consumed, dict) else {}
    data.update(fields)
    data["schema"] = SCHEMA
    atomic_write_json(_consumed_path(seat), data)


def _receipt(harness: str, sid: str, epoch: int) -> str:
    return f"{harness}:{sid}:{epoch}"


def _reread_path(harness: str, sid: str) -> Path:
    return state_root() / "reread" / f"{harness}-{_digest(sid, size=16)}.json"


def _notices_path(seat: Seat) -> Path:
    return state_root() / "notices" / f"{seat.key}.json"


def _prompt_seq_path(seat: Seat) -> Path:
    return state_root() / "prompt-seq" / f"{seat.key}.json"


def read_prompt_seq(seat: Seat) -> int:
    """How many real prompts were submitted at this seat (0 before the first one)."""
    data = read_json(_prompt_seq_path(seat))
    try:
        return max(0, int(data.get("seq", 0))) if isinstance(data, dict) else 0
    except (TypeError, ValueError):
        return 0


def bump_prompt_seq(seat: Seat, harness: str, sid: str, now: float) -> int:
    """Count one physical prompt boundary before its source is classified (caller holds the seat lock).

    A file of its own, written for every prompt: the ledger throttles prompt lines and
    folds old ones. The source tag is filled immediately by the shared claim classifier.
    """
    seq = read_prompt_seq(seat) + 1
    atomic_write_json(_prompt_seq_path(seat), {"schema": SCHEMA, "seq": seq, "at": now,
                                              "harness": harness, "sid": sid, "source": "pending"})
    return seq


def _current_request_origin(prompt_text: str, harness: str, sid: str) -> Optional[dict]:
    """Use the common transfer reader on this prompt only; never search nearby receipts."""
    if not prompt_text:
        return None
    try:
        import importlib.util
        path = Path(__file__).resolve().with_name("peer-message.py")
        spec = importlib.util.spec_from_file_location("session_tidy_peer_message", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        origin = module.prompt_origin(prompt_text, harness, sid)
        return origin if isinstance(origin, dict) else None
    except Exception:
        return None


def record_prompt_origin(seat: Seat, harness: str, sid: str, sequence: int, origin: dict) -> bool:
    """Attach validated non-content provenance to the current prompt sequence."""
    required = {"kind", "harness", "session_id", "request_digest", "transfer_ref",
                "transfer_body_sha256", "name"}
    if (not isinstance(origin, dict) or not required.issubset(origin)
            or origin.get("kind") not in {"peer", "runtime-completion"}
            or origin.get("harness") != harness or origin.get("session_id") != sid
            or not re.fullmatch(r"[0-9a-f]{64}", str(origin.get("request_digest") or ""))
            or not isinstance(origin.get("transfer_ref"), str) or not origin["transfer_ref"]
            or not isinstance(origin.get("name"), str)):
        return False
    path = _prompt_seq_path(seat)
    current = read_json(path)
    if (not isinstance(current, dict) or current.get("seq") != sequence
            or current.get("harness") != harness or current.get("sid") != sid):
        return False
    current["request_origin"] = dict(origin)
    atomic_write_json(path, current)
    return True


def write_notice(seat: Seat, text: str, *, author_harness: str = "", author_sid: str = "",
                 now: Optional[float] = None) -> dict:
    """Leave one result line for the seat's next start/prompt (takes the seat lock)."""
    now = now_epoch() if now is None else now
    line = " ".join((text or "").split())
    line = _cut_bytes("".join(ch for ch in line if ch.isprintable()), NOTICE_MAX_BYTES)[0]
    item = {"id": secrets.token_hex(6), "text": line, "created": now,
            "author": {"harness": author_harness, "sid": author_sid}}
    with seat_lock(seat.key):
        data = read_json(_notices_path(seat))
        items = data.get("items", []) if isinstance(data, dict) else []
        items.append(item)
        atomic_write_json(_notices_path(seat), {"schema": SCHEMA, "items": items[-20:]})
    return item


def pending_notices(seat: Seat, harness: str, sid: str, *, now: Optional[float] = None,
                    summary: Optional[dict] = None) -> list[dict]:
    """Notices this session should show: its own, or a departed author's."""
    now = now_epoch() if now is None else now
    data = read_json(_notices_path(seat))
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        return []
    sessions = summary if summary is not None else session_summary(seat)
    out = []
    for item in items:
        author = item.get("author") or {}
        a_key = (author.get("harness") or "", author.get("sid") or "")
        if not a_key[1] or a_key == (harness, sid):
            out.append(item)
            continue
        theirs = sessions.get(a_key)
        replaced = _appeared_after(sessions, harness, sid, a_key)
        if theirs is None:
            if replaced:
                out.append(item)      # author unknown to the ledger: nobody else will show it
            continue
        stale = now - theirs["last_seen"] > AUTHOR_STALE_SEC
        if replaced and (seat.kind in ("pane", "native") or stale):
            out.append(item)
    return out


def _appeared_after(sessions: dict, harness: str, sid: str, author_key: tuple[str, str]) -> bool:
    """Whether this session began after the author's last recorded activity."""
    author = sessions.get(author_key)
    if author is None:
        return True
    me = sessions.get((harness, sid))
    return bool(me and me["first_seen"] > author["last_seen"])


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "방금"
    if seconds < 3600:
        return f"{seconds // 60}분 전"
    if seconds < 172800:
        return f"{seconds // 3600}시간 전"
    return f"{seconds // 86400}일 전"


def build_card_injection(card: dict, card_path: Path, now: float, cap: int,
                         previous_transcript: Optional[dict] = None) -> str:
    """Card text within ``cap`` UTF-8 bytes; guidance is part of the fixed header."""
    author = card.get("author") or {}
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(card.get("authored_at_epoch") or now)))
    header = (f"[세션 카드] 같은 자리에서 이어받는 작업 카드 — 작성 {stamp} "
              f"({_ago(now - float(card.get('authored_at_epoch') or now))}, {author.get('harness', '')} "
              f"{str(author.get('sid', ''))[:8]})\n{CARD_FIRST_REPLY_GUIDANCE}")
    footer = f"카드 전문: {card_path}"
    previous_line = ""
    if isinstance(previous_transcript, dict):
        prior_harness = str(previous_transcript.get("harness") or "")
        prior_sid = str(previous_transcript.get("sid") or "")
        location = previous_transcript.get("transcript") or {}
        status = str(location.get("status") or "missing") if isinstance(location, dict) else "missing"
        path = str(location.get("path") or "") if isinstance(location, dict) else ""
        if status == "available" and path:
            if len(path.encode("utf-8")) > 320:
                path = "경로 생략"
            detail = path
        else:
            detail = "읽기 불가" if status == "unreadable" else "없음"
        previous_line = f"이전 대화: {prior_harness} {prior_sid[:8]} · 원문 {detail}"
    cut_note = "…(이하 생략)"
    body = card.get("body", "")
    fixed = (len(header.encode("utf-8")) + len(footer.encode("utf-8")) + 2
             + (len(previous_line.encode("utf-8")) + 1 if previous_line else 0))
    if fixed > cap and previous_line:
        previous_line = ""
        fixed = len(header.encode("utf-8")) + len(footer.encode("utf-8")) + 2
    room = cap - fixed
    if room <= 0:
        return _cut_bytes(f"{header}\n{footer}" + (f"\n{previous_line}" if previous_line else ""), cap)[0]
    if len(body.encode("utf-8")) <= room:
        return f"{header}\n{body}\n{footer}" + (f"\n{previous_line}" if previous_line else "")
    room -= len(cut_note.encode("utf-8")) + 1
    trimmed = _cut_bytes(body, max(0, room))[0].rstrip()
    return f"{header}\n{trimmed}\n{cut_note}\n{footer}" + (f"\n{previous_line}" if previous_line else "")


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

def build_human_history_notice(seat: Seat, harness: str, sid: str, *, env=None) -> str:
    """Build a person-only predecessor notice without adding it to model context."""
    env = os.environ if env is None else env
    lineage = seat_lineage(seat, harness=harness, sid=sid)
    previous = _confirmed_predecessor(lineage, harness, sid)
    if not previous:
        return ""
    transcript_info = previous.get("transcript") or {}
    state = transcript_info.get("status", "missing")
    availability = {"available": "원문 있음", "missing": "원문 없음", "expired": "원문 없음",
                    "unreadable": "원문 읽기 불가"}.get(state, "원문 읽기 불가")
    pane = str(env.get("HERDR_ACTIVE_PANE_ID") or env.get("HERDR_PANE_ID") or "")
    socket = str(env.get("HERDR_SOCKET_PATH") or "")
    key_available = False
    if pane and socket:
        try:
            install_dir = str(Path(__file__).resolve().parents[1] / "tools" / "install")
            if install_dir not in sys.path:
                sys.path.append(install_dir)
            from herdr_integration import history_key_available, history_key_label
            key_available = bool(history_key_available(env=env))
            key_label = history_key_label(env=env) if key_available else ""
        except Exception:
            pass
    action = key_label if key_available else "hearting run session_history"
    return (f"이전 대화 열기: {action} · {previous.get('harness', harness)} "
            f"{str(previous.get('sid', ''))[:8]} · {availability}")


def run_hook(harness: str, event: str, sid: str, *, source: str = "", transcript: str = "",
             cwd: str = "", reread: bool = False, env=None, now: Optional[float] = None,
             emit: Optional[Callable[[str], None]] = None, phase: Optional[str] = None,
             compact_summary: str = "", compact_context: Optional[dict] = None,
             prompt_text: str = "", emit_human: Optional[Callable[[str], None]] = None,
             human_only: bool = False) -> str:
    """Record the session, and print the card / notices it is due exactly once.

    Returns the text that was emitted ("" for none). The receipt is written only
    after ``emit`` returned, so a hook whose output never went out keeps the card
    for the next start or prompt.
    """
    env = os.environ if env is None else env
    if is_worker(env) or harness not in HARNESSES or not sid or event not in ("start", "prompt", "compact", "runtime-status"):
        return ""
    if event == "runtime-status":
        if harness == "opencode":
            observed = False
            with contextlib.suppress(BaseException):
                import session_tidy_clear
                observed = session_tidy_clear.observe_runtime_status(
                    sid, str((compact_context or {}).get("status") or ""),
                    observed_at=(compact_context or {}).get("observed_at"), cwd=cwd)
            return "booking=pending" if observed else ""
        return ""
    if event == "compact" and phase == "before":
        text = COMPACT_CARD_INSTRUCTIONS if harness in ("claude", "opencode") else ""
        if text and emit:
            emit(text)
        return text
    if event == "compact" and phase == "after":
        booked = _run_compact_after(harness, sid, compact_summary, cwd=cwd, transcript=transcript,
                                    compact_context=compact_context or {}, env=env, now=now)
        return booked
    now = now_epoch() if now is None else now
    seat = resolve_seat(harness, cwd or None, env, sid, source)
    if human_only:
        if event == "start" and emit_human:
            with contextlib.suppress(BaseException):
                notice = build_human_history_notice(seat, harness, sid, env=env)
                if notice:
                    emit_human(notice)
        return ""
    if event == "start":
        with contextlib.suppress(BaseException):
            import session_tidy_clear
            req = session_tidy_clear.read_reservation(seat.key)
            if req and req.get("harness") == harness and req.get("sid") == sid:
                session_tidy_clear.reconcile_pending(seat)
            else:
                reconcile_compact_frontier(harness, sid, cwd, env=env, now=now)
    if event in ("start", "prompt"):
        with contextlib.suppress(BaseException):
            import session_tidy_runner
            session_tidy_runner.reconcile_queued(seat, harness, sid)
            if event == "start":
                session_tidy_runner.reconcile_reservation_queue(seat)
    with seat_lock(seat.key):
        if reread:
            saved = read_json(_reread_path(harness, sid))
            summary = session_summary(seat).get((harness, sid))
            text = str(saved.get("text") or "") if isinstance(saved, dict) and summary \
                and saved.get("epoch") == summary["epoch"] else ""
            if text and emit:
                emit(text)
            return text
        if event == "prompt":
            sequence = bump_prompt_seq(seat, harness, sid, now)
            request_origin = _current_request_origin(prompt_text, harness, sid)
            if request_origin:
                record_prompt_origin(seat, harness, sid, sequence, request_origin)
            with contextlib.suppress(BaseException):
                import session_tidy_clear as clear
                if not clear.observe_prompt_locked(seat, harness, sid, prompt_text, now, sequence):
                    clear.note_real_prompt_locked(seat, harness, sid, sequence, now)
        sessions = session_summary(seat)
        prior = sessions.get((harness, sid))
        # SessionStart(source=compact) and a separate compact event describe the same
        # compaction; count it once.
        compacting = (event == "compact" or source == "compact") and not (
            prior and now - prior["last_compact_at"] < COMPACT_DEDUPE_SEC)
        launch = None
        if event == "start" and source in {"startup", "resume", "select", "clear", "compact", "fork"}:
            try:
                tools = str(Path(__file__).resolve().parents[1] / "tools")
                if tools not in sys.path:
                    sys.path.insert(0, tools)
                from fleet.process_identity import current_launch_context, native_resume_wire_argv
                launch = current_launch_context(harness, sid)
                wire = native_resume_wire_argv((launch or {}).get("argv"), harness, sid)
                if launch:
                    launch = {**launch, "argv": wire}
            except Exception:
                launch = None
        row = record_event(seat, harness, sid, event, source=source, transcript=transcript,
                           cwd=cwd, now=now, bump_epoch=compacting,
                           resume_argv=(launch or {}).get("argv"),
                           launch_cwd=(launch or {}).get("cwd") or "")
        sessions[(harness, sid)] = row
        if event == "compact":
            return ""
        if event == "start" and seat.kind in ("pane", "native"):
            with contextlib.suppress(BaseException):    # a hook never fails for the clear note
                import session_tidy_clear
                session_tidy_clear.note_start_locked(seat, harness, sid, now)
        if ((event == "start" and source == "clear")
                or (harness == "opencode" and event == "prompt")):
            with contextlib.suppress(BaseException):
                _record_tidy_clear_succession_locked(seat, harness, sid, now)
        # The one place a same-seat handover (A -> B) is recorded: B's confirmed start/first prompt.
        handed = None
        handover = None
        if seat.kind in ("pane", "native") and event in ("start", "prompt"):
            with contextlib.suppress(BaseException):    # a hook never fails for the handover
                import dispatch_seat_handover as handover
                handed = handover.record_locked(seat, harness, sid, event, source, now)
        epoch = int(row.get("epoch", 0) or 0)
        parts: list[str] = []
        notices = pending_notices(seat, harness, sid, now=now, summary=sessions)
        notice_block = ""
        if notices:
            notice_block = "\n".join(f"[정리 결과] {n['text']}" for n in notices)
            notice_block = _cut_bytes(notice_block, NOTICE_MAX_BYTES)[0]
            parts.append(notice_block)
        card_seat = seat
        own_card = read_latest_card(seat) if seat.kind == "project" else None
        own_author = (own_card or {}).get("author") or {}
        own_card_due = bool(own_card
                            and (own_author.get("harness"), own_author.get("sid")) == (harness, sid)
                            and own_card.get("origin") != "runtime-compact"
                            and epoch > int(own_author.get("epoch", 0) or 0))
        if own_card_due:
            card = own_card
        elif seat.kind == "project":
            card, card_seat = read_latest_project_card(seat)
        else:
            card = read_latest_card(seat)
        card_due = False
        memory_due = False
        memory = None
        resume_text = ""
        if card:
            author = card.get("author") or {}
            same_session = (author.get("harness"), author.get("sid")) == (harness, sid)
            if seat.kind == "project" and own_card_due:
                eligible = True
            elif seat.kind == "project" and not same_session:
                eligible = _appeared_after(sessions, harness, sid,
                                           (author.get("harness") or "", author.get("sid") or ""))
            elif seat.kind == "project":
                eligible = (card.get("origin") != "runtime-compact"
                            and epoch > int(author.get("epoch", 0) or 0))
            else:
                eligible = (not same_session) or (
                    card.get("origin") != "runtime-compact" and epoch > int(author.get("epoch", 0) or 0))
            consumed = read_json(_consumed_path(card_seat))
            # A card generation is consumed once within its author slot.
            taken = isinstance(consumed, dict) and consumed.get("generation") == card.get("generation") \
                and bool(consumed.get("receipts"))
            card_due = eligible and not taken
            # Layer B ("참고할 기억") has its own revision and its own receipt: a session that already
            # took card generation N still gets a newer B once, and the writer of the card (who
            # could be cleared next) does not use it up before the new session can see it.
            memory = card.get("memory_refs") if isinstance(card.get("memory_refs"), dict) else None
            taken_rev = int(consumed.get("memory_revision", 0) or 0) if isinstance(consumed, dict) else 0
            memory_due = bool(memory) and eligible and int(memory.get("revision", 0) or 0) > taken_rev
            used = len(notice_block.encode("utf-8")) + (1 if notice_block else 0)
            # The verified route and the existing resume command, one line, with the card B receives.
            if handover is not None and (card_due or handed):
                resume_text = handover.resume_line(seat, harness, sid)
                used += len(resume_text.encode("utf-8")) + (1 if resume_text else 0)
            room = INJECTION_MAX_BYTES - used - 1  # reserve the CLI emitter's trailing newline
            memory_text = ""
            if memory_due:
                wanted = build_memory_injection(memory, MEMORY_REFS_BYTES + 600)
                memory_text = build_memory_injection(
                    memory, min(len(wanted.encode("utf-8")), room - CARD_MIN_WHEN_BUNDLED - 1) if card_due else room)
                room -= len(memory_text.encode("utf-8")) + 1
            if resume_text:
                parts.append(resume_text)
            if card_due:
                previous = None
                if seat.kind in ("pane", "native") and card_seat.kind in ("pane", "native"):
                    previous = _confirmed_predecessor(
                        seat_lineage(seat, harness=harness, sid=sid), harness, sid)
                parts.append(build_card_injection(
                    card, card_text_path(card_seat), now, room, previous_transcript=previous))
            if memory_text:
                parts.append(memory_text)
        if not card and handed and handover is not None:
            resume_text = handover.resume_line(seat, harness, sid)
            if resume_text:
                parts.append(resume_text)
        text = "\n".join(parts)
        if event == "start" and emit_human:
            with contextlib.suppress(BaseException):
                notice = build_human_history_notice(seat, harness, sid, env=env)
                if notice:
                    emit_human(notice)
        if not text:
            return ""
        (emit or (lambda t: None))(text)
        if card_due:
            consumed = read_json(_consumed_path(card_seat))
            receipts = list(consumed.get("receipts", [])) if isinstance(consumed, dict) \
                and consumed.get("generation") == card.get("generation") else []
            receipts.append(_receipt(harness, sid, epoch))
            _write_consumed(card_seat, generation=card["generation"], receipts=receipts[-200:])
        if memory_due:
            consumed = read_json(_consumed_path(card_seat))
            same = isinstance(consumed, dict) and consumed.get("memory_revision") == memory["revision"]
            receipts = list(consumed.get("memory_receipts", [])) if same else []
            receipts.append(_receipt(harness, sid, epoch))
            _write_consumed(card_seat, memory_revision=memory["revision"], memory_receipts=receipts[-200:])
        if notices:
            shown = {n["id"] for n in notices}
            data = read_json(_notices_path(seat))
            rest = [i for i in (data.get("items", []) if isinstance(data, dict) else []) if i.get("id") not in shown]
            atomic_write_json(_notices_path(seat), {"schema": SCHEMA, "items": rest})
        # OpenCode re-emits this cache every turn: a later "참고할 기억" is added to what the session
        # already received instead of replacing it.
        earlier = read_json(_reread_path(harness, sid))
        kept = str(earlier.get("text") or "") if isinstance(earlier, dict) and earlier.get("epoch") == epoch else ""
        cached = _cut_bytes(kept + "\n" + text, 2 * INJECTION_MAX_BYTES)[0] if kept else text
        atomic_write_json(_reread_path(harness, sid), {"schema": SCHEMA, "epoch": epoch, "text": cached, "at": now})
        return text


def _compact_booking_result(booked: str) -> str:
    """Keep compact-stage outcomes bounded and observable across the hook bridge."""
    fields = str(booked or "").split()
    outcome = fields[0] if fields else "clear=pending"
    reason = next((field.split("=", 1)[1] for field in fields[1:] if field.startswith("reason=")), "")
    if outcome == "clear=scheduled":
        return "booking=scheduled" + (f" reason={reason}" if reason else "")
    if outcome == "clear=pending":
        return "booking=pending" + (f" reason={reason or 'unfinished'}")
    if outcome == "clear=completed":
        return "booking=completed" + (f" reason={reason or 'terminal'}")
    if outcome == "clear=off":
        return "booking=off"
    if outcome == "clear=manual":
        return "booking=manual" + (f" reason={reason or 'outside-herdr'}")
    if outcome == "clear=skipped":
        return "booking=skipped" + (f" reason={reason or 'ineligible'}")
    return "booking=pending reason=unrecognized-outcome"


def _run_compact_after(harness: str, sid: str, summary: str, *, cwd: str, transcript: str,
                       compact_context: Optional[dict], env, now: Optional[float]) -> str:
    """Record one compact event and preserve its card → queue → clear progress."""
    now = now_epoch() if now is None else now
    started = time.monotonic()
    seat = resolve_seat(harness, cwd or None, env, sid, "compact")
    prior_hint = (session_summary(seat).get((harness, sid)) or {}).get("transcript", "")
    transcript = transcript or prior_hint
    context = dict(compact_context or {})
    context["trigger"] = context.get("trigger") if context.get("trigger") in ("auto", "manual") else "unknown"
    context["transcript"] = transcript or str(context.get("transcript") or "")
    if not context["transcript"] and harness in ("codex", "opencode"):
        with contextlib.suppress(BaseException):
            import tidy_transcripts
            found = tidy_transcripts.locate_transcript(harness, sid)
            context["transcript"] = str(found or "")
    if harness == "opencode" and not context["transcript"]:
        with contextlib.suppress(BaseException):
            import tidy_transcripts
            context["transcript"] = str(tidy_transcripts.opencode_db_path())
    context["source"] = context["transcript"]
    if harness in ("claude", "codex") and context["transcript"] and "boundary_offset" not in context:
        with contextlib.suppress(OSError):
            path = Path(context["transcript"])
            info = path.stat()
            if path.is_file() and not path.is_symlink() and stat.S_ISREG(info.st_mode):
                context["boundary_offset"] = int(info.st_size)
                context["transcript_identity"] = f"{info.st_dev}:{info.st_ino}"
    codex_auto = harness == "codex" and context["trigger"] == "auto"
    if codex_auto:
        context["card_request_required"] = True
        if not context.get("transcript") or not isinstance(context.get("boundary_offset"), int)                 or not context.get("transcript_identity"):
            context["card_request_required"] = False
            context["turn_evidence_reason"] = "transcript-unavailable"
        if not any(context.get(key) for key in
                   ("turn_id", "summary_id", "compaction_part_id", "event_id")):
            context["card_request_required"] = False
            context["turn_evidence_reason"] = "turn-identity-unknown"
    context["prompt_seq"] = read_prompt_seq(seat)
    previous = read_latest_card(seat)
    context["card_generation"] = int((previous or {}).get("generation", 0) or 0)
    identity_fields = [harness, sid, context.get("trigger", "unknown"), context.get("summary_id", ""),
                       context.get("parent_id", ""), context.get("tail_start_id", ""),
                       context.get("compaction_part_id", ""), context.get("event_id", ""),
                       context.get("turn_id", ""), context.get("transcript", ""),
                       hashlib.sha256(_normalize_body(summary).encode("utf-8")).hexdigest()]
    context["identity"] = hashlib.sha256("\0".join(map(str, identity_fields)).encode("utf-8")).hexdigest()
    body = _normalize_body(summary)
    prior = None
    persisted = False
    card_duplicate = False
    ledger_event = None
    record_error = ""
    preserve_idle = False
    compact_request_in_progress = False
    expected_booking_nonce: Optional[str] = None
    inherit_continue_off = False
    try:
        with seat_lock(seat.key, wait=COMPACT_LOCK_WAIT_SEC):
            prior = session_summary(seat).get((harness, sid))
            previous = read_latest_card(seat)
            matching_events = [row for row in _read_ledger_lines(seat)
                               if row.get("event") == "compact" and row.get("harness") == harness
                               and row.get("sid") == sid and row.get("compact_identity") == context["identity"]]
            ledger_event = max(matching_events, key=lambda row: float(row.get("ts", 0) or 0), default=None)
            if ledger_event and isinstance(ledger_event.get("compact"), dict):
                saved = ledger_event["compact"]
                for key in ("prompt_seq", "card_generation", "boundary_offset", "transcript_identity",
                            "transcript", "turn_id", "summary_id", "parent_id", "tail_start_id",
                            "compaction_part_id", "event_id", "card_request_required"):
                    if key in saved:
                        context[key] = saved[key]
            compact_meta = (previous or {}).get("compact")
            card_duplicate = bool(compact_meta and compact_meta.get("identity") == context["identity"])
            newer_card = bool(ledger_event and previous
                              and float(previous.get("authored_at_epoch", 0) or 0)
                              >= float(ledger_event.get("ts", 0) or 0)
                              and not card_duplicate)
            import session_tidy_clear as clear
            held = clear.read_reservation(seat.key)
            same_session = bool(held and (held.get("harness"), held.get("sid")) == (harness, sid))
            if held and held.get("status") == "reserved":
                expected_booking_nonce = str(held.get("nonce") or "")
                prior_generation = int((previous or {}).get("generation", 0) or 0)
                old_claim = held.get("clear_claim") if isinstance(held.get("clear_claim"), dict) else {}
                inherit_continue_off = bool(
                    same_session and float(held.get("deadline", 0) or 0) >= now
                    and int(held.get("prompt_seq", -1)) == read_prompt_seq(seat)
                    and int(held.get("card_generation", -1)) == prior_generation
                    and held.get("continue_off") and old_claim.get("state") != "sending")
            preserve_idle = bool(same_session and held.get("origin") == "idle"
                                 and held.get("status") in ("reserved", "cleared")
                                 and held.get("followup_kind") == "status-only"
                                 and (held.get("status") == "reserved"
                                      or not clear.idle_clear_armed(seat, harness, sid)))
            compact_request_in_progress = bool(same_session and held.get("origin") == "compact"
                                               and held.get("status") == "reserved"
                                               and (held.get("compact") or {}).get("card_request_required"))
            if not ledger_event:
                compacting = not (prior and now - prior["last_compact_at"] < COMPACT_DEDUPE_SEC)
                record_event(seat, harness, sid, "compact", source="compact",
                             transcript=context["transcript"] or (prior or {}).get("transcript", ""),
                             cwd=cwd, now=now, bump_epoch=compacting,
                             compact_identity=context["identity"], compact_context=context)
            if not preserve_idle and not compact_request_in_progress and not codex_auto \
                    and body and not card_duplicate and not newer_card and seat.kind in COMPACT_CARD_SEAT_KINDS:
                try:
                    card = write_card(seat, harness, sid, body, cwd=cwd, now=now,
                                      prompt_seq=read_prompt_seq(seat), origin="runtime-compact",
                                      compact_context=context)
                    saved_card = read_latest_card(seat)
                    persisted = bool(card and saved_card
                                     and saved_card.get("generation") == card.get("generation")
                                     and (saved_card.get("compact") or {}).get("identity") == context["identity"]
                                     and card_text_path(seat).is_file())
                except BaseException as exc:  # noqa: BLE001 - keep memory queueable, report no clear
                    record_error = f"card-write-{type(exc).__name__}"
            elif card_duplicate:
                persisted = bool(previous and (previous.get("compact") or {}).get("identity") == context["identity"]
                                 and card_text_path(seat).is_file())
            with contextlib.suppress(BaseException):
                reread_path = _reread_path(harness, sid)
                _reject_symlink(reread_path)
                with contextlib.suppress(FileNotFoundError):
                    reread_path.unlink()
    except BaseException as exc:  # noqa: BLE001 - retain memory work but do not claim clear acceptance
        try:
            import session_tidy_runner as runner
            item = runner.enqueue_item(seat, harness, sid, cwd, context["transcript"],
                                       operation_id=context["identity"], start=False)
            item = runner._start_queued(item)
            detail = f" runner-start={str(item.get('runner_start_error'))[:40]}" \
                if item.get("runner_start_error") else ""
            return f"booking=pending reason=compact-state-{type(exc).__name__}" + detail
        except BaseException as queue_exc:  # noqa: BLE001
            return (f"booking=pending reason=compact-state-{type(exc).__name__} "
                    f"queue-write={type(queue_exc).__name__}")

    compact_queue = None
    if codex_auto:
        try:
            import session_tidy_runner as runner
            queue_transcript = str(context.get("transcript") or (prior or {}).get("transcript", ""))
            compact_queue = runner.enqueue_item(seat, harness, sid, cwd, queue_transcript,
                                                operation_id=context["identity"], start=False)
            context["queue_id"] = compact_queue["id"]
            context["operation_id"] = context["identity"]
            compact_queue = runner._start_queued(compact_queue)
        except BaseException as exc:  # noqa: BLE001 - the ledger event lets an ordinary hook retry
            return f"booking=pending reason=queue-write-{type(exc).__name__}"

    if preserve_idle:
        return "booking=skipped reason=idle-origin-preserved"
    if compact_request_in_progress and not (ledger_event and
            ledger_event.get("compact_identity") == context["identity"]):
        return "booking=pending reason=card-request-in-progress"

    import session_tidy_runner as runner
    import session_tidy_clear as clear
    operation_id = context["identity"]
    transcript_path = str(context.get("transcript") or (prior or {}).get("transcript", ""))
    if codex_auto:
        if not context.get("card_request_required") or seat.kind != "pane" or not seat.pane:
            launch_error = (compact_queue or {}).get("runner_start_error")
            return f"booking=queue-only reason={context.get('turn_evidence_reason') or 'exact-pane-unavailable'}" + (
                f" runner-start={str(launch_error)[:48]}" if launch_error else "")
        try:
            wait = max(0.0, min(COMPACT_LOCK_WAIT_SEC, started + COMPACT_HOOK_BUDGET_SEC - time.monotonic()))
            booked = clear.schedule_compact_card_request(seat, harness, sid, cwd, context,
                                                          now=now, lock_wait=wait)
            return _compact_booking_result(booked)
        except BaseException as exc:  # noqa: BLE001
            return f"booking=pending reason=reservation-write-{type(exc).__name__}"

    idle_clear_armed = clear.idle_clear_armed(seat, harness, sid)
    if context.get("trigger") != "auto":
        reason = "trigger-manual" if context.get("trigger") == "manual" else "trigger-unknown"
    elif not body:
        reason = "empty-summary"
    elif not persisted:
        reason = record_error or "card-not-persisted"
    elif harness not in ("claude", "opencode") or seat.kind not in COMPACT_CARD_SEAT_KINDS:
        reason = "compact-seat-ineligible"
    elif not idle_clear_armed:
        reason = "idle-not-rearmed"
    else:
        reason = ""
    if reason:
        try:
            item = runner.enqueue_item(seat, harness, sid, cwd, transcript_path,
                                       operation_id=operation_id, start=False)
            started_item = runner._start_queued(item)
            launch_error = started_item.get("runner_start_error")
            return f"booking=queue-only reason={reason}" + (
                f" runner-start={str(launch_error)[:48]}" if launch_error else "")
        except BaseException as exc:  # noqa: BLE001
            return f"booking=pending reason=queue-write-{type(exc).__name__}"
    try:
        item, booked = _enqueue_tidy(seat, harness, sid, cwd, transcript_path,
                                     compact_context=context,
                                     bounded_until=started + COMPACT_HOOK_BUDGET_SEC,
                                     expected_booking_nonce=expected_booking_nonce,
                                     compact_started_at=now,
                                     inherit_continue_off=inherit_continue_off,
                                     operation_id=operation_id, runner=runner)
        launch_error = item.get("runner_start_error") if isinstance(item, dict) else ""
        outcome = _compact_booking_result(booked)
        return outcome + (f" runner-start={str(launch_error)[:40]}"
                          if launch_error else "")
    except BaseException as exc:  # noqa: BLE001 - queue/reservation intent may already be durable
        return f"booking=pending reason=enqueue-{type(exc).__name__}"


def reconcile_compact_frontier(harness: str, sid: str, cwd: str = "", *, env=None,
                               now: Optional[float] = None,
                               lock_wait: Optional[float] = None) -> str:
    """Resume one automatic compact event from its seat ledger/card and stable queue identity."""
    env = os.environ if env is None else env
    if is_worker(env) or harness not in HARNESSES or not sid:
        return ""
    seat = resolve_seat(harness, cwd or None, env, sid, "compact")
    events = [row for row in _read_ledger_lines(seat)
              if row.get("event") == "compact" and row.get("harness") == harness
              and row.get("sid") == sid and row.get("compact_identity")]
    event = max(events, key=lambda row: float(row.get("ts", 0) or 0), default=None)
    compact = (event or {}).get("compact") if isinstance((event or {}).get("compact"), dict) else {}
    identity = str((event or {}).get("compact_identity") or compact.get("identity") or "")
    if not event or not identity or compact.get("trigger") != "auto":
        return ""
    try:
        import session_tidy_runner as runner
        transcript = str(compact.get("transcript") or event.get("transcript") or "")
        # Queue receipt and runner restart are independent of card request and clear state.
        # The stable operation ID makes this safe after cancellation, timeout or partial booking.
        queued = runner.enqueue_item(seat, harness, sid, cwd or str(event.get("cwd") or ""), transcript,
                                     operation_id=identity, queue_id=runner.operation_queue_id(identity),
                                     start=False)
        context_queue_id = str(queued.get("id") or "")

        def start_queue():
            return runner._start_queued(queued)

        import session_tidy_clear as clear
        held = clear.read_reservation(seat.key)
        if held and (held.get("compact") or {}).get("identity") == identity:
            if held.get("status") == "reserved":
                import session_tidy_runner as runner
                runner.reconcile_reservation_queue(seat)
                return "booking=scheduled" if clear.reconcile_pending(seat) else "booking=pending reason=helper-reconcile"
            if read_prompt_seq(seat) == int(compact.get("prompt_seq", -1)):
                start_queue()
                return f"booking=completed reason={held.get('status') or 'terminal'}"
            start_queue()
            return ""
        if held and held.get("status") == "reserved":
            start_queue()
            return "booking=pending reason=booking-pending"
        if int(compact.get("prompt_seq", -1)) != read_prompt_seq(seat):
            start_queue()
            return "booking=skipped reason=new-input"
        current_card = read_latest_card(seat)
        generation = int((current_card or {}).get("generation", 0) or 0)
        card_request_required = compact.get("card_request_required") is True
        if card_request_required and generation != int(compact.get("card_generation", generation)):
            start_queue()
            return "booking=skipped reason=card-changed"
        context = dict(compact)
        context["identity"] = identity
        context["trigger"] = "auto"
        context["transcript"] = transcript
        context["card_request_required"] = card_request_required
        if context["card_request_required"]:
            context["queue_id"] = context_queue_id
            start_queue()
            booked = clear.schedule_compact_card_request(
                seat, harness, sid, cwd or str(event.get("cwd") or ""), context, now=now,
                lock_wait=lock_wait)
            return _compact_booking_result(booked)
        if (not current_card or (current_card.get("compact") or {}).get("identity") != identity
                or (current_card.get("author") or {}).get("harness") != harness
                or (current_card.get("author") or {}).get("sid") != sid):
            return "booking=skipped reason=card-changed"
        import session_tidy_runner as runner
        _item, booked = _enqueue_tidy(seat, harness, sid, cwd or str(event.get("cwd") or ""),
                                      context["transcript"], compact_context=context,
                                      bounded_until=(time.monotonic() + lock_wait if lock_wait is not None else None),
                                      compact_started_at=float(event.get("ts", 0) or 0),
                                      operation_id=identity, runner=runner)
        return _compact_booking_result(booked)
    except BaseException as exc:  # noqa: BLE001 - retain a bounded recovery result
        return f"booking=pending reason=reconcile-{type(exc).__name__}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):  # a hook must never exit non-zero because of its arguments
        raise ValueError(message)


def _emit_stdout(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _guess_seat_from_ledgers(cwd: str) -> tuple[Optional[Seat], Optional[dict]]:
    best: tuple[Optional[Seat], Optional[dict]] = (None, None)
    for name in HARNESSES:
        seat = seat_for_project(name, project_key_for(cwd))
        row = latest_session(seat, name)
        if row and (best[1] is None or row["last_seen"] > best[1]["last_seen"]):
            best = (seat, row)
    return best


def resolve_caller(harness, sid, cwd: str) -> Optional[tuple[Seat, str, str]]:
    """(seat, harness, session id) of the calling session; ids come from the arguments,
    harness variables and, for pane/native seats, the seat ledger. Project ledger inference
    selects only the shared seat and harness; an absent caller id gets a fresh ``unknown-`` id."""
    explicit_sid = bool(sid)
    if not sid:
        harness, sid = session_from_env(harness)
    detected = harness or session_from_env(None)[0]
    seat = resolve_seat(detected, cwd, sid=sid)
    if seat.native:
        # The inherited Claude session variable can still name the previous
        # conversation after /clear. This exact process declares the current id.
        from session_tidy_native import live
        record = live(seat.native)
        if explicit_sid and (not record or record["sessionId"] != sid):
            return None
        if record and not explicit_sid:
            sid = record["sessionId"]
    if seat.kind == "project":
        # Ledger inference still chooses the project seat and harness when neither
        # was supplied, but a neighboring session is never evidence of this caller's id.
        if not detected:
            guessed_seat, ledger_row = _guess_seat_from_ledgers(cwd)
            if guessed_seat is not None and ledger_row:
                seat = guessed_seat
                harness = ledger_row.get("harness") or ""
        harness = harness or detected or "unknown"
        sid = sid or f"unknown-{secrets.token_hex(4)}"
        seat = dataclasses.replace(seat, harness=harness, sid=sid)
    elif not sid or not harness:
        ledger_row = latest_session(seat, harness)
        if ledger_row:
            harness = harness or ledger_row["harness"]
            sid = sid or ledger_row["sid"]
    if seat is None:
        return None
    return seat, harness or detected or "unknown", sid or f"unknown-{secrets.token_hex(4)}"


def _write_caller_card(args, cwd: str) -> tuple[Optional[tuple[Seat, str, str]], str]:
    """Write the calling session's card; (caller, printed line), caller None when nothing was written."""
    if args.text is not None:
        body = args.text
    elif args.file:
        body = Path(args.file).read_text(encoding="utf-8", errors="replace")
    else:
        body = sys.stdin.read()
    if not sanitize_body(body):
        return None, "card=none reason=empty-body"
    caller = resolve_caller(args.harness, args.session_id, cwd)
    if caller is None:
        return None, "card=none reason=no-seat"
    seat, harness, sid = caller
    with seat_lock(seat.key):
        previous = read_latest_card(seat)
        generation = int(previous.get("generation", 0) or 0) + 1 if previous else 1
        record_event(seat, harness, sid, "card", cwd=cwd, card_generation=generation)
        write_card(seat, harness, sid, body, cwd=cwd, prompt_seq=read_prompt_seq(seat))
    return caller, f"card={card_text_path(seat)} seat={seat.key}"


def cmd_card(args) -> int:
    if is_worker():
        print("card=none reason=worker")
        return 0
    _caller, line = _write_caller_card(args, args.cwd or os.getcwd())
    print(line)
    return 0


def _enqueue_tidy(seat: Seat, harness: str, sid: str, cwd: str, transcript: str = "", *,
                  opt_out: bool = False, no_continue: bool = False,
                  compact_context: Optional[dict] = None, bounded_until: Optional[float] = None,
                  expected_booking_nonce: Optional[str] = None,
                  compact_started_at: Optional[float] = None,
                  inherit_continue_off: bool = False,
                  idle_context: Optional[dict] = None,
                  operation_id: Optional[str] = None,
                  runner=None):
    """Durably queue one operation and book its helper; the helper snapshots outside the hook."""
    import session_tidy_clear as clear
    if runner is None:
        import session_tidy_runner as runner

    def lock_wait():
        if bounded_until is None:
            return None
        return max(0.0, min(COMPACT_LOCK_WAIT_SEC, bounded_until - time.monotonic()))

    operation_id = operation_id or (idle_context or {}).get("identity") or ((compact_context or {}).get("identity"))
    item = runner.enqueue_item(seat, harness, sid, cwd, transcript,
                               operation_id=operation_id, start=False)
    try:
        booked = clear.schedule_for_enqueue(seat, harness, sid, cwd, opt_out=opt_out,
                                            no_continue=no_continue, compact_context=compact_context,
                                            lock_wait=lock_wait(),
                                            expected_booking_nonce=expected_booking_nonce,
                                            compact_started_at=compact_started_at,
                                            inherit_continue_off=inherit_continue_off,
                                            operation_id=operation_id, queue_id=item.get("id"),
                                            idle_context=idle_context)
    except BaseException as exc:  # noqa: BLE001 - retain the accepted queue and its unfinished booking
        booked = f"clear=pending reason=reservation-write-{type(exc).__name__}"
    item = runner._start_queued(item)
    return item, booked


def _enqueue_caller(caller: tuple[Seat, str, str], cwd: str, *, no_clear: bool,
                    no_continue: bool) -> tuple[str, Optional[dict]]:
    seat, harness, sid = caller
    transcript = (session_summary(seat).get((harness, sid)) or {}).get("transcript", "")
    import session_tidy_clear as clear
    idle_context = clear.idle_context_for_enqueue(seat, harness, sid)
    item, booked = _enqueue_tidy(seat, harness, sid, cwd, transcript,
                                 opt_out=no_clear, no_continue=no_continue,
                                 idle_context=idle_context,
                                 operation_id=(idle_context or {}).get("identity"))
    return f"enqueue={item['id']} seat={seat.key} status=queued {booked}", idle_context


def cmd_enqueue(args) -> int:
    if is_worker():
        print("enqueue=none reason=worker")
        return 0
    cwd = args.cwd or os.getcwd()
    caller = resolve_caller(args.harness, args.session_id, cwd)
    if caller is None:
        print("enqueue=none reason=no-seat")
        return 0
    line, _idle = _enqueue_caller(caller, cwd, no_clear=bool(args.no_clear),
                                  no_continue=bool(args.no_continue))
    print(line)
    return 0


def tidy_report_line(booked: str, *, handoff: bool = False, origin: str = "", harness: str = "") -> str:
    """The one line the session shows the user after a tidy, from the enqueue ``clear=`` words."""
    fields = str(booked or "").split()
    outcome = next((field for field in fields if field.startswith("clear=")), "")
    if outcome == "clear=scheduled":
        # The clear waits for this turn to end with an empty input box; typing first keeps the window.
        if handoff or origin == "idle" or "continue=off" in fields:
            return "입력창이 비어 있으면 곧 이 창을 비웁니다. 그 전에 입력하시면 비우지 않습니다."
        return ("입력창이 비어 있으면 곧 이 창을 비우고, 새 세션이 카드를 받아 이어서 진행합니다. "
                "그 전에 입력하시면 비우지 않습니다.")
    command = "/new" if harness == "opencode" else "/clear"
    if outcome == "clear=off":
        return f"이 창은 그대로 둡니다. 이제 {command} 하거나 닫아도 됩니다."
    hint = next((field.split("=", 1)[1] for field in fields if field.startswith("hint=")), "")
    if outcome == "clear=manual":
        return f"이제 {hint or command} 하거나 닫아도 됩니다."
    if outcome == "clear=skipped":
        return f"이제 {command} 하거나 닫아도 됩니다."
    return "카드와 기억 정리 접수를 마쳤습니다. 창 비우기 결과는 다음 입력 때 한 줄로 알려 드립니다."


def cmd_tidy(args) -> int:
    """Card, memory-tidy queue (with its clear booking) and optional handoff in one call."""
    if is_worker():
        print("card=none reason=worker")
        return 0
    cwd = args.cwd or os.getcwd()
    caller, line = _write_caller_card(args, cwd)
    print(line)
    if caller is None:
        # No card means no queue and no clear booking, exactly as a failed card step did.
        print("enqueue=none reason=no-card")
        return 1
    handoff = bool(args.handoff)
    line, idle_context = _enqueue_caller(caller, cwd, no_clear=bool(args.no_clear),
                                         no_continue=bool(args.no_continue))
    print(line)
    code = 0
    if handoff:
        import session_tidy_runner as runner
        handoff_line, code = runner.handoff(caller[0], args.handoff)
        print(handoff_line)
    origin = str((idle_context or {}).get("origin") or "")
    print("say=" + tidy_report_line(line, handoff=handoff, origin=origin, harness=caller[1]))
    return code


def reply_card_markers(nonce: str) -> tuple[str, str]:
    """First and last line of a card written as the reply to one automatic tidy request."""
    return f"[[session-tidy card {nonce}]]", f"[[session-tidy card end {nonce}]]"


def reply_card_body(reply: str, nonce: str) -> Optional[str]:
    """The card inside a complete marked reply, or None (unmarked, cut off or another request's)."""
    if not nonce or not isinstance(reply, str):
        return None
    begin, end = reply_card_markers(nonce)
    lines = reply.strip().splitlines()
    if len(lines) < 3 or lines[0].strip() != begin or lines[-1].strip() != end:
        return None
    body = "\n".join(lines[1:-1]).strip()
    return body if sanitize_body(body) else None


def accept_reply_card(harness: str, sid: str, cwd: str, reply: str) -> str:
    """At turn end, an automatic idle/compact tidy request answered with a complete marked card
    becomes that card and the same enqueue ``tidy`` runs; anything else changes nothing."""
    if is_worker():
        return "reply-card=none reason=worker"
    if not reply or "[[session-tidy card " not in reply:
        return "reply-card=none"
    caller = resolve_caller(harness, sid, cwd)
    if caller is None:
        return "reply-card=none reason=no-seat"
    seat, harness, sid = caller
    import session_tidy_clear as clear
    context = clear.idle_context_for_enqueue(seat, harness, sid)
    if not context or context.get("origin") not in ("idle", "compact"):
        return "reply-card=none reason=no-request"
    body = reply_card_body(reply, str(context.get("nonce") or ""))
    if body is None:
        return "reply-card=none reason=unmarked"
    if context.get("eligible") is not True:
        return "reply-card=skipped reason=request-not-eligible"
    request = (clear.read_reservation(seat.key) or {}).get("idle_request")
    if isinstance(request, dict) and request.get("state") == "accepted":
        return "reply-card=skipped reason=already-accepted"       # the session ran ``tidy`` itself
    with seat_lock(seat.key):
        previous = read_latest_card(seat)
        generation = int(previous.get("generation", 0) or 0) + 1 if previous else 1
        record_event(seat, harness, sid, "card", cwd=cwd, card_generation=generation)
        write_card(seat, harness, sid, body, cwd=cwd, prompt_seq=read_prompt_seq(seat))
    line, _idle = _enqueue_caller((seat, harness, sid), cwd, no_clear=False, no_continue=False)
    return "reply-card=accepted " + line


def cmd_handoff(args) -> int:
    if is_worker():
        print("prompted=false reason=worker")
        return 1
    caller = resolve_caller(args.harness, args.session_id, args.cwd or os.getcwd())
    if caller is None:
        print("prompted=false reason=no-seat")
        return 1
    import session_tidy_runner as runner
    if args.target == "accept":
        if not args.transfer_ref:
            raise StateError("handoff-transfer-ref-required")
        try:
            line, code = runner.accept_handoff(args.transfer_ref, caller)
        except ValueError as exc:
            raise StateError(str(exc)) from exc
    else:
        line, code = runner.handoff(caller[0], args.target)
    print(line)
    return code


def cmd_hook(args) -> int:
    try:
        emitted = []
        human = []
        output_format = getattr(args, "format", "text")
        sid = args.session_id or session_from_env(args.harness)[1] or ""
        summary = ""
        prompt_text = ""
        compact_context = {}
        if args.phase == "after" or args.event in ("runtime-status", "prompt"):
            try:
                payload = json.load(sys.stdin)
            except (EOFError, ValueError):
                payload = {}
            if isinstance(payload, dict):
                if isinstance(payload.get("summary"), str):
                    summary = payload["summary"]
                if isinstance(payload.get("prompt"), str):
                    prompt_text = payload["prompt"]
                for key in ("trigger", "summary_id", "parent_id", "tail_start_id", "compaction_part_id",
                            "turn_id", "event_id", "status", "observed_at"):
                    value = payload.get(key)
                    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                        compact_context[key] = value
                transcript = payload.get("transcript") or payload.get("transcript_path")
                if isinstance(transcript, str) and transcript:
                    compact_context["transcript"] = transcript
        result = run_hook(args.harness, args.event, sid, source=args.source or "", transcript=args.transcript or "",
                          cwd=args.cwd or os.getcwd(), reread=args.reread,
                          emit=(emitted.append if output_format == "json" else _emit_stdout),
                          emit_human=human.append,
                          human_only=getattr(args, "human_only", False),
                          phase=args.phase, compact_summary=summary, compact_context=compact_context,
                          prompt_text=prompt_text)
        if output_format == "json":
            print(json.dumps({"additionalContext": "\n".join(emitted),
                              "humanNotice": "\n".join(human)}, ensure_ascii=False))
        if (args.event == "compact" and args.phase == "after" and result.startswith("booking=")) \
                or (args.event == "runtime-status" and result == "booking=pending"):
            _emit_stdout(result)
    except BaseException as exc:  # noqa: BLE001 - keep compact failure visible without prompt text
        if args.event == "compact" and args.phase == "after" and not is_worker():
            _emit_stdout(f"booking=pending reason=hook-{type(exc).__name__}")
    return 0


def _queue_counts(seat: Seat) -> dict:
    counts: dict = {}
    folder = state_root() / "queue"
    try:
        names = list(folder.iterdir())
    except OSError:
        return counts
    for path in names:
        item = read_json(path) if path.suffix == ".json" else None
        if isinstance(item, dict) and (item.get("seat") or {}).get("key") == seat.key:
            counts[item.get("status", "?")] = counts.get(item.get("status", "?"), 0) + 1
    return counts


def cmd_status(args) -> int:
    harness, sid = session_from_env(None)
    seat = resolve_seat(harness, args.cwd or os.getcwd(), sid=sid)
    card = read_latest_card(seat)
    notices = read_json(_notices_path(seat))
    info = {
        "state_root": str(state_root()),
        "seat": seat.as_dict(),
        "worker": is_worker(),
        "card": {"generation": card["generation"], "authored_at": card["authored_at"],
                 "path": str(card_text_path(seat))} if card else None,
        "sessions": len(session_summary(seat)),
        "notices": len(notices.get("items", [])) if isinstance(notices, dict) else 0,
        "queue": _queue_counts(seat),
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, sort_keys=True))
    else:
        print(f"state_root={info['state_root']}")
        print(f"seat={seat.key} kind={seat.kind}")
        print("card=" + (f"gen{card['generation']} {card['authored_at']}" if card else "none"))
        print(f"sessions={info['sessions']} notices={info['notices']} worker={int(info['worker'])}")
        print("queue=" + (" ".join(f"{k}={v}" for k, v in sorted(info["queue"].items())) or "empty"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="session_tidy.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    card = sub.add_parser("card", help="write the calling session's card")
    card.add_argument("--harness", choices=HARNESSES)
    card.add_argument("--session-id")
    card.add_argument("--cwd")
    card.add_argument("--file")
    card.add_argument("--text")
    card.set_defaults(func=cmd_card)
    hook = sub.add_parser("hook", help="record the session; print the card/notice it is due")
    hook.add_argument("--harness", required=True)
    hook.add_argument("--event", required=True, choices=("start", "prompt", "compact", "runtime-status"))
    hook.add_argument("--session-id")
    hook.add_argument("--source")
    hook.add_argument("--transcript")
    hook.add_argument("--cwd")
    hook.add_argument("--reread", action="store_true")
    hook.add_argument("--format", choices=("text", "json"), default="text")
    hook.add_argument("--human-only", action="store_true")
    hook.add_argument("--phase", choices=("before", "after"))
    hook.set_defaults(func=cmd_hook)
    enqueue = sub.add_parser("enqueue", help="queue the memory tidy and return at once")
    enqueue.add_argument("--harness", choices=HARNESSES)
    enqueue.add_argument("--session-id")
    enqueue.add_argument("--cwd")
    enqueue.add_argument("--no-clear", action="store_true",
                         help="keep this window: no auto-clear (cancels a pending one)")
    enqueue.add_argument("--no-continue", action="store_true",
                         help="clear the window but type nothing after it (the user asked to stop after tidying)")
    enqueue.set_defaults(func=cmd_enqueue)
    tidy = sub.add_parser("tidy", help="write the card, queue the memory tidy and book the clear in one call")
    tidy.add_argument("--harness", choices=HARNESSES)
    tidy.add_argument("--session-id")
    tidy.add_argument("--cwd")
    tidy.add_argument("--file")
    tidy.add_argument("--text")
    tidy.add_argument("--no-clear", action="store_true", help="as enqueue --no-clear (정리만)")
    tidy.add_argument("--no-continue", action="store_true", help="as enqueue --no-continue (정리하고 끝)")
    tidy.add_argument("--handoff", metavar="TARGET", help="then deliver the card to this peer (인계)")
    tidy.set_defaults(func=cmd_tidy)
    handoff = sub.add_parser("handoff", help="deliver this seat's card to a peer session")
    handoff.add_argument("target")
    handoff.add_argument("transfer_ref", nargs="?")
    handoff.add_argument("--harness", choices=HARNESSES)
    handoff.add_argument("--session-id")
    handoff.add_argument("--cwd")
    handoff.set_defaults(func=cmd_handoff)
    status = sub.add_parser("status", help="show state location and what is waiting")
    status.add_argument("--json", action="store_true")
    status.add_argument("--cwd")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args, _unknown = parser.parse_known_args(argv)
    except ValueError:
        if argv and argv[0] == "hook":
            return 0
        parser.print_usage(sys.stderr)
        return 2
    except SystemExit as exc:
        return 0 if (argv and argv[0] == "hook") else int(exc.code or 0)
    try:
        return int(args.func(args) or 0)
    except StateError as exc:
        if args.command == "hook":
            return 0
        print(f"session-tidy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
