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
import contextvars
import copy
import dataclasses
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from typing import Callable, Iterator, Optional

import session_card_format as card_format

SCHEMA = 1
HARNESSES = ("claude", "codex", "opencode")
CARD_BODY_MAX_BYTES = 8 * 1024
CARD_FIELDS = tuple(f["label"] for f in card_format.fields())
CARD_FIELD_IDS = tuple(f["id"] for f in card_format.fields())
CARD_FIELDS_BY_ID = dict(zip(CARD_FIELD_IDS, CARD_FIELDS))
CARD_FIXED_FIELD_IDS = ("original_goal", "user_request_quote", "requirements_prd_location")
CARD_FIXED_FIELDS = tuple(CARD_FIELDS_BY_ID[field_id] for field_id in CARD_FIXED_FIELD_IDS)
COMPACT_SUMMARY_MAX_BYTES = 256 * 1024
COMPACT_RECENT_MAX_BYTES = 8 * 1024
COMPACT_METADATA_MAX_BYTES = 1024
COMPACT_CARD_BODY_MAX_BYTES = (COMPACT_SUMMARY_MAX_BYTES + COMPACT_RECENT_MAX_BYTES
                               + COMPACT_METADATA_MAX_BYTES + 3072)
INJECTION_MAX_BYTES = 2400
MEMORY_REFS_MAX = 8                 # "참고할 기억" (card layer B): records listed
MEMORY_REFS_BYTES = 1200            # ... and the bytes their lines may take
# The Korean form of tools/i18n/messages.json "tidy.card.first_reply" (the injected text reads the
# catalog); kept here because the bundled-card floor below is sized from it, the longer form.
CARD_FIRST_REPLY_GUIDANCE = (
    "[첫 답] 이 카드로 이어받은 첫 답 맨 앞에, 도구 호출 전에, 사용자의 언어로 "
    "'하던 일 · 남은 일 · 다음 할 일' 세 머리말을 써서 5줄 안팎 요약을 한 번 보여 준 뒤 "
    "이어서 하세요. 사용자가 이어서하기 대신 다른 지시를 먼저 주면 요약을 생략하고 그 지시만 따르세요."
    " 요약에는 대상의 원천 설명과 구별 조건을 먼저 쓰고 원래 번호를 덧붙이세요. "
    "첫 실작업 전에 카드의 " + "·".join(CARD_FIXED_FIELDS) + "와 그 칸이 가리키는 PRD 절을 다시 읽으세요. "
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
    "Write this summary as a session handoff card in the user's language, with these fields "
    "in this order: " + " → ".join(CARD_FIELDS) + ". Keep the details needed to continue the work "
    "inside those fields, within about 90 lines. Carry forward the previous card's "
    + " / ".join(CARD_FIXED_FIELDS)
    + " verbatim; do not re-summarize them. Preserve dated original user quotes and append "
    "new material without re-summarizing. Remove only items the user explicitly replaced. "
    "Before the first real work, reread the full card and the PRD section its fixed fields point to. "
    "Carry the source description and distinguishing comparison condition before the "
    "original identifier; preserve document/revision and checkpoint references when they "
    "affect meaning. Keep failed, interrupted and unfinished states with their next action, "
    "and leave unexplained identifiers unresolved. In the existing related or other fields, "
    "keep a session's confirmed Fleet number together with its observed pane and full exact "
    "native SID; leave any unknown number, pane or SID unknown."
    " Some recent turns may remain verbatim after compaction, depending on the runtime. "
    "Do not infer that an omitted request or reply was undelivered. When known, say which "
    "last turn this summary covers; note when the preserved transcript needs checking. "
    "Do not assume a later turn is visible or available."
)


class StateError(Exception):
    """The state folder is unsafe (a symlink) or unusable."""


# ---------------------------------------------------------------------------
# State folder primitives
# ---------------------------------------------------------------------------

def state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    root = Path(base) / "hearting" / "session-tidy"
    # Test isolation only: `tools/memory/tests/tidy_isolation.py` sets this for
    # the lifetime of an IsolatedEnv so a bare in-process state access outside
    # that root fails immediately instead of touching the developer's state.
    # Production never sets it, so behavior without the variable is unchanged.
    confinement = os.environ.get("HEARTING_STATE_CONFINEMENT")
    if confinement:
        try:
            inside = os.path.commonpath(
                (os.path.realpath(root), os.path.realpath(confinement))
            ) == os.path.realpath(confinement)
        except (OSError, ValueError):
            inside = False
        if not inside:
            raise StateError(f"outside the test confinement {confinement}: {root}")
    return root


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


def bump_dispatch_edge_generation(jobs=None) -> None:
    """Record that a confirmed lineage edge changed, for derived inbox discovery.

    Permission is never derived from this counter: every record re-reads its
    recipient authority. A missed bump costs one periodic runner reconciliation,
    and the canonical ledger row is already durable when this runs, so a failure
    to record the derived observation never fails the writer.
    """
    try:
        import dispatch_recipient_inbox as inbox
        inbox.bump_edge_generation(inbox.dispatch_inbox_root(jobs=jobs))
    except Exception:  # noqa: BLE001 - derived observation only
        return


def iso_utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _digest(*parts: str, size: int = 20) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:size]


def _endpoint_scope(env=None) -> str:
    """A pane id is local to its Herdr endpoint; keep named sockets in separate seats.

    The default path (no ``env``) asks herdr for that endpoint's socket identity, so a reader
    outside herdr keys a pane as the pane's own hooks do; an explicit env (``{}``, a dict or the
    ``os.environ`` object) is computed as-is, so writer and lock keys never change."""
    if env is None:
        return _default_endpoint()[0]
    return _env_endpoint_scope(env)


def _default_endpoint() -> tuple[str, str]:
    """``(endpoint, socket herdr reported)`` of this process; the socket is "" unless discovered.

    Only a process outside any herdr pane asks: a pane process keys from its env exactly as
    that pane's own hooks do (herdr exports the socket together with the pane id)."""
    if not (str(os.environ.get("HERDR_SOCKET_PATH") or "").strip()
            or str(os.environ.get("HERDR_PANE_ID") or "").strip()):
        socket_path = _herdr_endpoint_socket(str(os.environ.get("AGENT_HERDR_SESSION") or "").strip())
        if socket_path:
            return _digest("herdr-endpoint", "socket", os.path.realpath(socket_path)), socket_path
    return _env_endpoint_scope(os.environ), ""


def _env_endpoint_scope(env) -> str:
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

    Read by `session_identity`; several inherited harness ids without a named
    caller are ambiguous and do not supply a session identity.
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
    continuation_sid: str = ""  # exact successor prompt frontier for a completed physical booking
    _logical_identity: tuple[str, str] = dataclasses.field(default=("", ""), compare=False, repr=False)

    def as_dict(self) -> dict:
        result = {"kind": self.kind, "key": self.key, "pane": self.pane,
                  "harness": self.harness, "project_key": self.project_key}
        if self.native is not None:
            result["native"] = self.native
        if self.sid:
            result["sid"] = self.sid
        if self.endpoint:
            result["endpoint"] = self.endpoint
        if self.continuation_sid:
            result["continuation_sid"] = self.continuation_sid
        return result


def _native_session_key(harness: str, sid: str) -> str:
    if harness not in HARNESSES or not isinstance(sid, str) or not sid or sid == "-" \
            or sid.startswith("unknown-"):
        return ""
    return _digest("native-session", harness, sid)


def seat_state_key(seat: Seat) -> str:
    """The logical state key for an exact native identity, else its physical seat key."""
    harness, sid = session_identity(seat)
    return _native_session_key(harness, sid) or seat.key


def session_identity(seat: Seat) -> tuple[str, str]:
    """Return explicit logical session context without changing the physical seat fields."""
    logical = getattr(seat, "_logical_identity", ("", ""))
    if isinstance(logical, tuple) and len(logical) == 2 and logical[1]:
        return str(logical[0] or ""), str(logical[1])
    return str(getattr(seat, "harness", "") or ""), str(getattr(seat, "sid", "") or "")


def physical_seat(seat: Seat) -> Seat:
    """Drop logical session context while keeping the original physical binding."""
    return dataclasses.replace(seat, sid="", _logical_identity=("", ""))


def session_seat(seat: Seat, harness: str, sid: str) -> Seat:
    """Carry a logical identity separately from the unchanged physical seat binding."""
    if _native_session_key(harness, sid):
        return dataclasses.replace(seat, sid="", _logical_identity=(harness, sid))
    if seat.kind == "project" and harness in HARNESSES and isinstance(sid, str) and sid.startswith("unknown-"):
        return dataclasses.replace(seat, sid="", _logical_identity=(harness, sid))
    if getattr(seat, "_logical_identity", ("", "")) != ("", ""):
        return physical_seat(seat)
    if not sid:
        return seat
    return seat


@contextlib.contextmanager
def session_state_lock(seat: Seat, *, wait: Optional[float] = None) -> Iterator[None]:
    """Lock logical session state and its physical seat in a stable order."""
    with _session_state_locks((seat,), wait=wait):
        yield


@contextlib.contextmanager
def _session_state_locks(seats: Iterable[Seat], *, wait: Optional[float] = None) -> Iterator[None]:
    """Lock related logical sessions and their shared physical seats in one order."""
    keys = set()
    for seat in seats:
        keys.update(key for key in (seat_state_key(seat), seat.key) if key)
        harness, sid = session_identity(seat)
        native_key = _native_session_key(harness, sid)
        if (native_key and not (state_root() / "cards" / f"{native_key}.json").exists()
                and _native_address_status(seat) != "conflict"):
            keys.update(_legacy_native_source_keys(seat))
    keys = sorted(keys)
    with contextlib.ExitStack() as stack:
        for key in keys:
            stack.enter_context(seat_lock(key, wait=wait))
        with seat_ledger_read_scope():
            active = _NATIVE_ADDRESS_READ_SCOPE.get()
            token = _NATIVE_ADDRESS_READ_SCOPE.set(active if active is not None else {})
            try:
                yield
            finally:
                _NATIVE_ADDRESS_READ_SCOPE.reset(token)


def seat_from_dict(data: dict) -> Seat:
    kind, harness = str(data.get("kind") or ""), str(data.get("harness") or "")
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    logical_harness = str(session.get("harness") or harness)
    logical_sid = str(session.get("sid") or data.get("sid") or "")
    sid = str(data.get("sid") or "")
    seat = Seat(kind, str(data.get("key") or ""), str(data.get("pane") or ""), harness,
                str(data.get("project_key") or ""), data.get("native"), sid,
                str(data.get("endpoint") or ""), "", str(data.get("continuation_sid") or ""))
    if (_native_session_key(logical_harness, logical_sid)
            or (kind == "project" and logical_sid.startswith("unknown-"))):
        return session_seat(dataclasses.replace(seat, sid=""), logical_harness, logical_sid)
    return seat


def terminal_tmux_pane(env) -> str:
    """``$TMUX_PANE`` of a process started inside tmux (with ``$TMUX``); a hint, never proof."""
    from terminal_surface import caller_tmux_pane
    return caller_tmux_pane(env)[0]


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


HERDR_ENDPOINT_RETRY_SEC = 60.0     # an unanswered endpoint question is asked again after this
_HERDR_ENDPOINT_SOCKETS: dict[str, tuple[str, float]] = {}   # server -> (socket or "", monotonic)
_HERDR_ENDPOINT_LOCK = threading.Lock()


def _herdr_endpoint_socket(server: str) -> str:
    """The socket herdr itself reports for its running ``server`` ("" is the default one), else "".

    Only herdr's answer counts: ``running`` is true, ``socket`` is a non-empty string and
    ``session`` names the asked server (null, "" or "default" for the default one); a stopped
    session still names a socket. A found socket is kept for the process lifetime: herdr derives
    it from its config directory, so a server restart keeps the path (a moved socket needs the
    reading process restarted). A miss is asked again only after HERDR_ENDPOINT_RETRY_SEC."""
    server = str(server or "").strip()
    with _HERDR_ENDPOINT_LOCK:
        cached = _HERDR_ENDPOINT_SOCKETS.get(server)
        if cached is not None and (cached[0] or time.monotonic() - cached[1] < HERDR_ENDPOINT_RETRY_SEC):
            return cached[0]
        found = _ask_herdr_endpoint_socket(server)
        _HERDR_ENDPOINT_SOCKETS[server] = (found, time.monotonic())
        return found


def _ask_herdr_endpoint_socket(server: str) -> str:
    exe = _herdr_executable()
    if not exe:
        return ""
    command = [exe] + (["--session", server] if server else []) + ["status", "server", "--json"]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=HERDR_LOOKUP_TIMEOUT_SEC)
        status = json.loads(done.stdout or "")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError, TypeError):
        return ""
    if not isinstance(status, dict) or status.get("running") is not True:
        return ""
    socket_path = status.get("socket")
    if not isinstance(socket_path, str) or not socket_path.strip():
        return ""
    named = status.get("session")
    default = (None, "", "default")
    if not (named in default if server in ("", "default") else named == server):
        return ""
    return socket_path.strip()


def _legacy_pane_seat(seat: Seat) -> Optional[Seat]:
    """The endpoint-free key of an endpoint-keyed pane seat (read-only fallback), else None."""
    if seat.kind != "pane" or not seat.endpoint or not seat.pane:
        return None
    return Seat("pane", _digest("pane", seat.pane), seat.pane, seat.harness)


def _pane_seat_candidates(pane: str) -> list[Seat]:
    """``pane``'s normalized seat, then its legacy endpoint-free seat when the two differ."""
    seat = _pane_seat_of(pane)
    legacy = _legacy_pane_seat(seat)
    return [seat] + ([legacy] if legacy is not None else [])


def _agent_session(agent: dict) -> str:
    session = agent.get("agent_session")
    return str(session.get("value") or "") if isinstance(session, dict) else ""


def _real(path) -> str:
    with contextlib.suppress(OSError, TypeError, ValueError):
        return os.path.realpath(str(path))
    return ""


def _pane_seat_of(pane: str, *, endpoint: Optional[str] = None,
                  socket_path: Optional[str] = None) -> Seat:
    discovered = ""
    if endpoint is None:
        endpoint, discovered = _default_endpoint()
    if socket_path is None:
        socket_path = str(os.environ.get("HERDR_SOCKET_PATH") or "") or discovered
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
    booking, seat = None, None
    try:
        import session_tidy_clear
        for seat in _pane_seat_candidates(pane):
            # The legacy key is read only when the normalized key holds no booking.
            booking = session_tidy_clear.read_reservation(seat.key)
            if booking:
                break
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
                     if any(("codex", sid) in session_summary(seat)
                            for seat in _pane_seat_candidates(str(a["pane_id"])))]
    if not panes:
        panes = _cleared_into(sid)
    if not panes and agents and source == "clear" and cwd:
        panes = _successor_pane(sid, agents, cwd)
    return panes[0] if len(set(panes)) == 1 else ""


def resolve_seat(harness: Optional[str], cwd=None, env=None, sid: Optional[str] = None,
                 source: str = "", *, project: bool = True, hook: bool = False) -> Seat:
    """The pane, native or project seat; ``project=False`` skips the project key (an empty one).
    ``hook``: the caller is the runtime's own hook for ``sid`` (a Codex daemon hook carries no
    thread id of its own; see ``pane_ownership.codex_caller_binding``)."""
    env = os.environ if env is None else env
    live_caller = env is os.environ
    # Outside Herdr, the caller's tmux pane is the hint; the same live proof decides.
    pane = ((env.get("HERDR_PANE_ID") or terminal_tmux_pane(env)).strip()
            if live_caller or harness != "codex" else "")
    server = env.get("AGENT_HERDR_SESSION") if live_caller else None
    socket_value = env.get("HERDR_SOCKET_PATH", "") if live_caller else ""
    endpoint_unproven = bool(harness == "codex" and server and not socket_value)
    steps: dict = {}
    if live_caller and harness == "codex" and sid:
        from pane_ownership import _caller_runtime, codex_caller_binding
        caller, service = _caller_runtime(os.getpid(), "codex")
        thread = env.get("CODEX_THREAD_ID") or ""
        steps = {"caller": "codex" if caller is not None else "none", "service": bool(service),
                 "thread_env": "absent" if not thread else ("same" if thread == sid else "other"),
                 "pane_hint": bool(pane), "binding": "not-tried"}
        if caller is not None and not service and not caller[3]:
            # This foreground runtime has no controlling terminal. Its native
            # session state still applies, but no physical pane can own it.
            # A shared service is different: its caller can have an exact TUI.
            pane = ""
            endpoint_unproven = True
        if caller is not None and service:
            binding = codex_caller_binding(sid, environ=env, from_hook=hook)
            steps["binding"] = "found" if binding else "none"
            if binding:
                return session_seat(pane_seat_from_binding(binding), harness or "codex", sid)
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
        hint = pane
        pane = verified_pane(pane, harness, sid, executable=_herdr_executable(),
                             server=server, socket_path=socket_path)
        tmux_hint = terminal_tmux_pane(env)
        if not pane and tmux_hint and tmux_hint != hint:
            # A tmux server started under herdr hands its panes a herdr pane id that is not
            # theirs; the pane's own tmux hint gets the same live proof.
            hint, pane = tmux_hint, verified_pane(tmux_hint, harness, sid, executable=_herdr_executable(),
                                                  server=server, socket_path=socket_path)
        if steps:
            steps.update(pane_hint=steps["pane_hint"] or bool(hint), verified_pane=pane or "")
    if pane:
        from terminal_surface import is_tmux_pane
        endpoint = "" if is_tmux_pane(pane) else _endpoint_scope(env if live_caller else {})
        key = _digest("pane", endpoint, pane) if endpoint else _digest("pane", pane)
        return session_seat(Seat("pane", key, pane, harness or "", "", endpoint=endpoint),
                            harness or "", sid or "")
    from session_tidy_native import caller_target
    native = caller_target(harness, env)
    if native:
        return session_seat(Seat("native", _digest("native", harness, native["home"], native["job"], native["start"]),
                                 harness=harness or "", native=native), harness or "", sid or "")
    if not project:
        return Seat("project", "", harness=harness or "", sid=sid or "")
    if hook and steps.get("caller") == "codex":
        _note_seat_fallback("codex", str(sid), steps)
    found = seat_for_project(harness or "unknown", project_key_for(cwd))
    return session_seat(found, harness or "", sid or "")


def _note_seat_fallback(harness: str, sid: str, steps: dict) -> None:
    """Keep one record per session whose own hook fell back to the project seat: the step
    values that decided it (caller runtime, service, thread id in the environment, binding,
    verified pane), rewritten only when they change."""
    with contextlib.suppress(BaseException):
        key = _native_session_key(harness, sid)
        if not key:
            return
        path = state_root() / "seat-fallback" / f"{key}.json"
        record = {"schema": 1, "harness": harness, "sid": sid, **steps}
        held = read_json(path)
        if isinstance(held, dict) and {k: v for k, v in held.items() if k != "at"} == record:
            return
        ensure_dir(path.parent)
        atomic_write_json(path, {**record, "at": now_epoch()})


# ---------------------------------------------------------------------------
# Seat ledger
# ---------------------------------------------------------------------------

_LEDGER_READ_SCOPE = contextvars.ContextVar("session_tidy_ledger_read_scope", default=None)
_LEDGER_READ_ONLY = contextvars.ContextVar("session_tidy_ledger_read_only", default=False)
_LEDGER_PARSE_SCOPE = contextvars.ContextVar("session_tidy_ledger_parse_scope", default=None)
_NATIVE_ADDRESS_READ_SCOPE = contextvars.ContextVar("session_tidy_native_address_scope", default=None)


@contextlib.contextmanager
def seat_ledger_read_scope(parse_cache=None):
    """Share parsed ledger rows within one read pass, with nested scope reuse."""
    active = _LEDGER_READ_SCOPE.get()
    token = _LEDGER_READ_SCOPE.set(active if active is not None else {})
    parse_token = (_LEDGER_PARSE_SCOPE.set(parse_cache) if parse_cache is not None else None)
    try:
        yield
    finally:
        _LEDGER_READ_SCOPE.reset(token)
        if parse_token is not None:
            _LEDGER_PARSE_SCOPE.reset(parse_token)


@contextlib.contextmanager
def seat_ledger_readonly_scope():
    """Avoid defensive row copies for trusted readers that only inspect ledger data."""
    token = _LEDGER_READ_ONLY.set(True)
    try:
        yield
    finally:
        _LEDGER_READ_ONLY.reset(token)


def _invalidate_ledger_read(seat: Seat) -> None:
    scope = _LEDGER_READ_SCOPE.get()
    if scope is not None:
        for path in (_native_ledger_path(seat), _physical_ledger_path(seat)):
            key = os.fspath(path)
            scope.pop(key, None)
            scope.pop(key + "#lineage", None)

def _physical_ledger_path(seat: Seat) -> Path:
    return state_root() / "sessions" / f"{seat.key}.jsonl"


def _ledger_path(seat: Seat) -> Path:
    """Keep the shared legacy ledger boundary addressed to its physical seat."""
    return _physical_ledger_path(seat)


def _native_ledger_path(seat: Seat) -> Path:
    """Return the exact logical ledger used by this module's native session state."""
    return state_root() / "sessions" / f"{seat_state_key(seat)}.jsonl"


class _LedgerLines(list):
    """Legacy row list with observation health for authority readers."""
    usable = True


class LedgerParseCache:
    """Bounded disposable parses for passive scans; observed bytes stay authoritative."""

    def __init__(self):
        from collections import OrderedDict
        self.entries = OrderedDict()
        self.generations = {}
        self.bytes = 0
        self.lock = threading.RLock()

    def read(self, path, reader, parser, *, read_only=False):
        key = os.fspath(path)
        def generation():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                return None
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns, reader, parser)
        try:
            before = generation()
        except OSError:
            before = None
        with self.lock:
            held = self.entries.get(key)
            raw = (held[1] if held is not None and before is not None
                   and self.generations.get(key) == before else None)
        if raw is None:
            raw = reader(path)
        rows = self.parse(path, raw, parser, read_only=read_only)
        try:
            after = generation()
        except OSError:
            after = None
        with self.lock:
            held = self.entries.get(key)
            if (held is not None and held[0] is parser and held[1] == raw
                    and before is not None and before == after):
                self.generations[key] = before
            else:
                self.generations.pop(key, None)
        return rows

    def parse(self, path, raw, parser, *, read_only=False):
        key = os.fspath(path)
        with self.lock:
            previous = self.entries.get(key)
        if previous is not None and previous[0] is parser and previous[1] == raw:
            rows = previous[2]
        elif (previous is not None and previous[0] is parser
              and isinstance(raw, bytes) and isinstance(previous[1], bytes)
              and (not previous[1] or previous[1].endswith(b"\n"))
              and raw.startswith(previous[1])):
            suffix = parser(raw[len(previous[1]):])
            rows = _LedgerLines([*previous[2], *suffix])
            rows.usable = previous[2].usable and suffix.usable
        else:
            rows = parser(raw)
        size = len(raw or b"")
        with self.lock:
            old = self.entries.pop(key, None)
            if old is None or old[0] is not parser or old[1] != raw:
                self.generations.pop(key, None)
            if old is not None:
                self.bytes -= len(old[1] or b"")
            if size <= 2 * 1024 * 1024:
                self.entries[key] = (parser, raw, rows)
                self.bytes += size
            while len(self.entries) > 2048 or self.bytes > 32 * 1024 * 1024:
                _key, retired = self.entries.popitem(last=False)
                self.generations.pop(_key, None)
                self.bytes -= len(retired[1] or b"")
        if not read_only:
            return copy.deepcopy(rows)
        # Physical relation joins may extend the list, never its row records.
        value = _LedgerLines(rows)
        value.usable = rows.usable
        return value


def _handover_origin(row) -> Optional[tuple[str, str]]:
    """Normalize the existing string/structured origin without inventing an edge."""
    harness = row.get("harness")
    if not isinstance(harness, str) or harness not in HARNESSES:
        return None
    origin = row.get("from")
    if isinstance(origin, dict):
        harness, origin = origin.get("harness"), origin.get("sid")
    if (not isinstance(harness, str) or harness not in HARNESSES
            or not isinstance(origin, str) or not origin):
        return None
    return harness, origin


def _parse_ledger_lines(raw: Optional[bytes], *, strict: bool = False) -> list[dict]:
    """Preserve observation health while parsing one ledger incarnation."""
    entries = _LedgerLines()
    entries.usable = raw is not None
    if raw is not None:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            entries.usable = False
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            entries.usable = False
            continue
        if not isinstance(item, dict):
            entries.usable = False
            continue
        if strict and not item.get("sid"):
            entries.usable = False
        event, source = item.get("event"), item.get("source")
        if event in {"handover", "succession"}:
            try:
                if not math.isfinite(float(item.get("ts", 0) or 0)):
                    entries.usable = False
            except (ValueError, TypeError):
                entries.usable = False
            bindings = item.get("bindings", [])
            if (not isinstance(bindings, list) or not all(
                    isinstance(binding, dict)
                    and all(isinstance(binding.get(key), str) and binding[key]
                            for key in ("jobs", "route", "hash"))
                    and ("attempt" not in binding or isinstance(binding["attempt"], str) and binding["attempt"])
                    and isinstance(binding.get("node"), str)
                    and ("parent" not in binding or isinstance(binding["parent"], str) and binding["parent"])
                    for binding in bindings)):
                entries.usable = False
        # Schema-1 handovers predate source annotations. Their exact logical
        # bindings remain authority, but tidy_lineage_edge deliberately gives
        # these rows no unbound succession edge.
        if event == "handover" and "source" not in item:
            bindings = item.get("bindings")
            origin = _handover_origin(item)
            if (type(item.get("schema", 1)) is not int or item.get("schema", 1) != 1
                    or not all(isinstance(value, str) and value
                               for value in (item.get("harness"), item.get("sid"))) or origin is None
                    or item.get("harness") not in HARNESSES
                    or not isinstance(bindings, list) or not bindings
                    or not all(isinstance(binding, dict)
                               and all(isinstance(binding.get(key), str) and binding[key]
                                       for key in ("jobs", "route", "hash"))
                               and isinstance(binding.get("node"), str) for binding in bindings)):
                entries.usable = False
            if item.get("sid"):
                entries.append(item)
            continue
        if event in ("handover", "succession") and not isinstance(source, str):
            entries.usable = False
        relation = (event == "succession" and (source is None or source == "tidy-clear")
                    or event == "handover" and (source is None or isinstance(source, str)
                        and source in {"clear", "reservation", "first-message", "retire"}))
        if relation:
            origin = _handover_origin(item)
            if (not source or not all(isinstance(value, str) and value
                                      for value in (item.get("harness"), item.get("sid")))
                    or origin is None or source != "retire" and origin[0] != item.get("harness")
                    or not isinstance(item.get("bindings", []), list)):
                entries.usable = False
        if isinstance(item, dict) and item.get("sid"):
            entries.append(item)
    return entries


def _read_ledger_lines(seat: Seat, *, include_physical_relations: bool = True,
                       strict: bool = False, raw: Optional[bytes] = None) -> list[dict]:
    """Parse one observed incarnation, preserving health for every reader."""
    if raw is not None:
        cache = _LEDGER_PARSE_SCOPE.get() if not strict else None
        entries = (cache.parse(_native_ledger_path(seat), raw, _parse_ledger_lines,
                               read_only=_LEDGER_READ_ONLY.get()) if cache is not None
                   else _parse_ledger_lines(raw, strict=True) if strict else _parse_ledger_lines(raw))
        if strict and not entries.usable:
            raise ValueError("seat-ledger-schema-invalid")
        return entries
    path, physical = _native_ledger_path(seat), _physical_ledger_path(seat)
    scope = None if strict else _LEDGER_READ_SCOPE.get()
    read_only = _LEDGER_READ_ONLY.get()
    cache_key = os.fspath(path) + ("#lineage" if include_physical_relations and path != physical else "")
    if scope is not None and cache_key in scope:
        cached = scope[cache_key]
        return cached if read_only else copy.deepcopy(cached)

    def parse(source):
        cache = _LEDGER_PARSE_SCOPE.get() if not strict else None
        if cache is not None:
            return cache.read(source, read_bytes, _parse_ledger_lines, read_only=read_only)
        if strict:
            _reject_symlink(source)
            try:
                observed = source.read_bytes()
            except FileNotFoundError:
                observed = b""
        else:
            observed = read_bytes(source)
        rows = (_parse_ledger_lines(observed, strict=True)
                if strict else _parse_ledger_lines(observed))
        if strict and not rows.usable:
            raise ValueError("seat-ledger-schema-invalid")
        return rows

    entries = parse(path)
    if include_physical_relations and path != physical:
        physical_rows = parse(physical)
        if physical.exists():
            entries.usable = entries.usable and physical_rows.usable
        seen = {json.dumps(row, ensure_ascii=False, sort_keys=True) for row in entries}
        for row in physical_rows:
            if row.get("event") not in {"handover", "succession", "steward-release"}:
                continue
            identity = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if identity not in seen:
                entries.append(row)
                seen.add(identity)
    if scope is not None:
        scope[cache_key] = entries if read_only else copy.deepcopy(entries)
    return entries

def _append_ledger_entry(seat: Seat, entry: dict) -> None:
    """Append logical state and retain the same event in its physical seat ledger."""
    harness, sid = str(entry.get("harness") or ""), str(entry.get("sid") or "")
    if _native_session_key(harness, sid):
        seat = session_seat(seat, harness, sid)
    paths = [_native_ledger_path(seat)]
    physical = _physical_ledger_path(seat)
    if physical != paths[0]:
        paths.append(physical)
    encoded = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    transition = entry.get("event") in {"handover", "succession"}
    for path in paths:
        ensure_dir(path.parent)
        _reject_symlink(path)
        if transition:
            raw = read_bytes(path) or b""
            found = False
            for line in raw.decode("utf-8", "replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(row, dict)
                        and row.get("event") == entry.get("event")
                        and row.get("snapshot_id") == entry.get("snapshot_id")
                        and row.get("harness") == entry.get("harness")
                        and row.get("sid") == entry.get("sid")
                        and row.get("from") == entry.get("from")
                        and row.get("source") == entry.get("source")
                        and row.get("bindings", []) == entry.get("bindings", [])):
                    found = True
                    break
            if found:
                continue
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)
    _invalidate_ledger_read(seat)
    _fold_ledger(seat)
    if physical != _native_ledger_path(seat):
        _fold_ledger(physical_seat(seat))


def record_tidy_claim(seat: Seat, *, harness: str, sid: str, claim_id: str, nonce: str,
                      token: str, digest: str, origin: str, operation_id: str,
                      sent_seq: int, state: str, now: Optional[float] = None,
                      arrival_seq: Optional[int] = None, target_sid: str = "",
                      acceptance: Optional[dict] = None) -> None:
    """Persist one claim transition in the existing seat ledger (caller holds its lock)."""
    if not claim_id or not nonce or not token or not digest or harness not in HARNESSES or not sid:
        return
    seat = session_seat(seat, harness, sid)
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


def runtime_prompt_text(prompt: str) -> str:
    """Unwrap only the runtime's whole-message paste envelope; preserve any human framing."""
    match = re.fullmatch(r'\s*<pasted_content id="([^"<>]+)">\n(.*)\n</pasted_content id="\1">\s*',
                         prompt, re.S)
    return match.group(2) if match else prompt


def matching_tidy_claim(seat: Seat, harness: str, sid: str, prompt: str, sequence: int) -> Optional[dict]:
    """Find an exact, still-classifiable request claim for this session and prompt boundary."""
    if not prompt or harness not in HARNESSES or not sid:
        return None
    seat = session_seat(seat, harness, sid)
    digest = hashlib.sha256(runtime_prompt_text(prompt).encode("utf-8")).hexdigest()
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
    seat = session_seat(seat, harness, sid)
    rows = [row for row in _read_ledger_lines(seat)
            if row.get("event") == "real-prompt" and row.get("harness") == harness and row.get("sid") == sid]
    return max(rows, key=lambda row: int(row.get("sequence", 0) or 0), default=None)


def record_real_prompt(seat: Seat, harness: str, sid: str, sequence: int, now: float) -> None:
    """Persist user activity after prompt-source classification (caller holds the seat lock)."""
    seat = session_seat(seat, harness, sid)
    _append_ledger_entry(seat, {"ts": now, "harness": harness, "sid": sid,
                                "event": "real-prompt", "sequence": max(0, int(sequence))})


def mark_prompt_source(seat: Seat, harness: str, sid: str, sequence: int, source: str,
                       claim_id: str = "") -> None:
    """Tag the current physical prompt boundary after its exact source has been classified."""
    seat = session_seat(seat, harness, sid)
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
    if source in NON_HUMAN_SOURCES and type(current.get("prior_human_seq")) is int:
        current["last_human_seq"] = current["prior_human_seq"]
    atomic_write_json(path, current)


def _record_tidy_clear_succession_locked(seat: Seat, harness: str, sid: str, now: float) -> bool:
    """Record the confirmed session edge from a matching tidy-clear reservation only.

    Returns whether this call appended the edge."""
    seat = session_seat(seat, harness, sid)
    if seat.kind not in ("pane", "native"):
        return False
    try:
        import session_tidy_clear
        booking = session_tidy_clear.read_reservation(seat.key)
    except Exception:
        return False
    if not isinstance(booking, dict) or booking.get("harness") != harness:
        return False
    old = booking.get("sid")
    observed = booking.get("observed")
    observed_sid = observed.get("sid") if isinstance(observed, dict) else None
    if (not isinstance(old, str) or not old or old == sid
            or not (observed_sid == sid or booking.get("new_session") == sid)):
        return False
    if isinstance(observed, dict) and observed.get("harness") not in (None, harness):
        return False
    import dispatch_seat_handover as handover
    # All edge writers share this lock, after the caller's session/seat locks.
    # Read fresh bytes inside it, never a pass context captured before waiting.
    with seat_lock(handover.LINEAGE_WRITE_KEY):
        try:
            graph = handover.lineage_graph()
        except (OSError, ValueError, TypeError):
            # The matching booking already observed this first clear. Preserve
            # its historical publication fallback; this grants no new booking.
            graph = None
        if graph is not None:
            _end, state = handover._chain_end(graph, harness, old)
            target, target_state = handover._chain_end(graph, harness, sid)
            if state != "self" or target_state == "unresolved" or target == old:
                return False
        if any(row.get("event") == "succession" and row.get("harness") == harness
               and row.get("sid") == sid and row.get("from") == old
               for row in _read_ledger_lines(seat)):
            return False
        _append_ledger_entry(seat, {"ts": now, "harness": harness, "sid": sid,
                                    "event": "succession", "from": old, "source": "tidy-clear"})
        bump_dispatch_edge_generation()
        return True


def _confirmed_clear_predecessor(seat: Seat, harness: str, sid: str) -> Optional[dict]:
    """The one predecessor this ``/clear`` or first message provably replaced, else None.

    The seat ledger must name exactly one predecessor, a matching tidy-clear
    reservation (when one exists) must agree with it, and the same-harness session
    lineage must end at ``sid``. Read-only: callable while the hook holds its locks."""
    predecessor = confirmed_predecessor_identity(seat, harness, sid)
    if not predecessor:
        return None
    logical = session_seat(seat, harness, sid)
    try:
        import session_tidy_clear
        booking = session_tidy_clear.read_reservation(logical.key)
    except Exception:
        booking = None
    if (isinstance(booking, dict) and booking.get("harness") == harness
            and booking.get("sid") not in (None, predecessor["sid"])):
        return None
    import dispatch_seat_handover as handover
    if handover.chain_end(harness, predecessor["sid"]) != (sid, "successor"):
        return None
    return predecessor


def prompt_state(seat: Seat) -> Optional[dict]:
    data = read_json(_prompt_seq_path(seat))
    return data if isinstance(data, dict) else None


def session_summary(seat: Seat) -> dict[tuple[str, str], dict]:
    """Fold the ledger: ``(harness, sid) -> first_seen, last_seen, epoch, transcript, cwd, ...``."""
    return _summarize_sessions(_read_ledger_lines(seat))


def _summarize_sessions(rows) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for item in rows:
        if item.get("event") in {"handover", "succession", "steward-release", "tidy-claim",
                                  "real-prompt", "native-address"}:
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
        if item.get("event") == "start":
            row["last_start_at"] = max(row.get("last_start_at", 0), ts)
    return out


def latest_session(seat: Seat, harness: Optional[str] = None) -> Optional[dict]:
    rows = [r for r in session_summary(seat).values() if not harness or r["harness"] == harness]
    return max(rows, key=lambda r: r["last_seen"]) if rows else None


def _project_seat_lineage(rows: list[dict], cards: dict, locations: dict,
                          *, harness=None, sid=None, graph=None) -> list[dict]:
    """Pure projection of already-read seat rows, cards and transcript locations."""
    grouped = {}
    starts = {}
    edges = {}
    outgoing = {}
    if graph is not None:
        for edge_harness, source, target in graph.edges:
            edges.setdefault((edge_harness, target), set()).add(source)
            outgoing.setdefault((edge_harness, source), set()).add(target)
    for row in rows:
        name, session = str(row.get("harness") or ""), str(row.get("sid") or "")
        if not name or not session or row.get("event") == "native-address":
            continue
        key = (name, session)
        edge = None if graph is not None else tidy_lineage_edge(row)
        if edge is not None:
            edge_harness, source, target = edge
            if edge_harness == name:
                edges.setdefault((name, target), set()).add(source)
                outgoing.setdefault((name, source), set()).add(target)
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
        # A fork's child has its own valid authority, but its incoming
        # relation still crosses an ambiguous source and cannot be displayed
        # as a confirmed (or merely chronological) predecessor.
        unresolved = graph is not None and (key in graph.unresolved or any(
            (item["harness"], origin) in graph.unresolved for origin in predecessors))
        if unresolved:
            item["relation"] = "unknown"
        elif (len(predecessors) == 1
                and len(outgoing.get((item["harness"], next(iter(predecessors))), ())) == 1):
            item["relation"] = "confirmed"
            item["predecessor"] = {"harness": item["harness"], "sid": next(iter(predecessors))}
        elif len(predecessors) > 1:
            item["relation"] = "unknown"
        elif index + 1 < len(output) and output[index + 1]["harness"] == item["harness"]:
            item["relation"] = "chronological"
    return output


def tidy_lineage_edge(row: dict) -> Optional[tuple[str, str, str]]:
    """Normalize the only ledger rows that establish a same-harness lineage edge."""
    if not isinstance(row, Mapping):
        return None
    event, source_kind = row.get("event"), row.get("source")
    if event == "succession":
        if source_kind != "tidy-clear":
            return None
    elif event == "handover":
        if not isinstance(source_kind, str) or source_kind not in {"clear", "reservation", "first-message"}:
            return None
    else:
        return None
    harness, target = row.get("harness"), row.get("sid")
    source = _handover_origin(row)
    if source is None or source[0] != harness:
        return None
    origin = source[1]
    if not all(isinstance(value, str) and value for value in (harness, origin, target)):
        return None
    return harness, origin, target


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


def _lineage_history_index(ledgers):
    """Summarize a readable ledger generation once, before narrowing to a seat."""
    rows, sessions, session_ledgers, summaries = [], {}, {}, {}
    for ledger_key, ledger_rows in ledgers.items():
        rows.extend(ledger_rows)
        summaries[ledger_key] = _summarize_sessions(ledger_rows)
        for key, summary in summaries[ledger_key].items():
            current = sessions.get(key)
            if current is None or float(summary.get("last_seen", 0) or 0) >= float(
                    current.get("last_seen", 0) or 0):
                sessions[key], session_ledgers[key] = summary, ledger_key
    return rows, sessions, session_ledgers, summaries


def seat_lineage(seat: Seat, *, harness=None, sid=None, read_context=None,
                 predecessor_only=False) -> list[dict]:
    """Project this seat's history and confirmed relatives from the shared graph."""
    physical = physical_seat(seat)
    logical_harness, logical_sid = session_identity(seat)
    sid = sid or logical_sid or None
    if sid:
        harness = harness or logical_harness or None
    wanted = (str(harness or ""), str(sid or ""))
    graph = None
    if predecessor_only and all(wanted):
        # A startup notice needs full history only when a confirmed incoming
        # edge exists. Use the existing compact relation reader for that
        # question; detailed history still uses every activity row below.
        from peer_read_context import PeerReadContext
        import dispatch_seat_handover
        try:
            compact = dispatch_seat_handover._pass_lineage_graph(
                read_context or PeerReadContext(compact_lineage=True))
        except (OSError, ValueError, TypeError):
            return []  # unavailable authority cannot confirm a predecessor
        origins = {origin for name, origin, target in compact.edges
                   if (name, target) == wanted}
        if len(origins) != 1 or wanted in compact.unresolved:
            return []
        origin = next(iter(origins))
        if ((wanted[0], origin) in compact.unresolved
                or sum(name == wanted[0] and source == origin
                       for name, source, _target in compact.edges) != 1):
            return []
    if read_context is not None and getattr(read_context, "compact_lineage", False):
        # Compact rows carry relation rows only; this projection summarizes every
        # row for history. Observe normally rather than silently narrowing it.
        from peer_read_context import PeerReadContext
        read_context = PeerReadContext()
    try:
        import dispatch_seat_handover
        graph = dispatch_seat_handover._pass_lineage_graph(read_context)
    except Exception:  # noqa: BLE001 - keep the requested seat's local history
        pass
    ledgers = dict(graph.ledger_rows) if graph is not None else {}
    index = (read_context.lineage_summary(graph, lambda: _lineage_history_index(ledgers))
             if graph is not None and read_context is not None else _lineage_history_index(ledgers))
    rows, sessions, session_ledgers, summaries = index
    # Missing/legacy local ledgers supplement this query without altering the shared index.
    ledgers, summaries = dict(ledgers), dict(summaries)
    own_rows = ledgers.get(physical.key)
    if own_rows is None:
        own_rows = _read_ledger_lines(physical)
        ledgers[physical.key] = own_rows
        summaries[physical.key] = _summarize_sessions(own_rows)
    if all(wanted) and wanted not in summaries[physical.key]:
        legacy = _legacy_pane_seat(physical)
        if legacy is not None:
            legacy_rows = ledgers.get(legacy.key)
            if legacy_rows is None:
                legacy_rows = _read_ledger_lines(legacy)
                ledgers[legacy.key] = legacy_rows
                summaries[legacy.key] = _summarize_sessions(legacy_rows)
            if wanted in summaries[legacy.key]:
                physical, own_rows = legacy, legacy_rows
    if graph is None or set(ledgers) != set(graph.ledger_rows):
        # Only the uncommon local fallback needs an augmented index.
        rows, sessions, session_ledgers, _ = _lineage_history_index(ledgers)
    own_sessions = summaries[physical.key]
    session_seats = {key: (physical if ledger_key == physical.key else Seat("pane", ledger_key))
                     for key, ledger_key in session_ledgers.items()}
    related = {key for key in own_sessions if not harness or key[0] == harness}
    current_sid = sid
    if not current_sid:
        current = max((summary for key, summary in own_sessions.items()
                       if not harness or key[0] == harness),
                      key=lambda summary: float(summary.get("last_seen", 0) or 0), default=None)
        current_sid = current.get("sid") if current else None
    if current_sid:
        related.update(key for key in sessions if key[1] == current_sid
                       and (not harness or key[0] == harness))
    if graph is not None:
        adjacent = {}
        for name, origin, target in graph.edges:
            left, right = (name, origin), (name, target)
            if left in graph.unresolved or right in graph.unresolved:
                continue
            adjacent.setdefault(left, set()).add(right)
            adjacent.setdefault(right, set()).add(left)
        remaining = list(related)
        while remaining:
            for key in adjacent.get(remaining.pop(), ()):
                if key not in related:
                    related.add(key)
                    remaining.append(key)
    # Filter before any card or transcript lookup. The graph is global; a
    # history/focus view must not open unrelated seats' native files.
    sessions = {key: summary for key, summary in sessions.items() if key in related}
    rows = [row for row in rows if (row.get("harness"), row.get("sid")) in related]
    cards, locations = {}, {}
    for key, summary in sessions.items():
        name, session = key
        if harness and name != harness:
            continue
        author_seat = session_seat(session_seats.get(key, physical), name, session)
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
    projected = _project_seat_lineage(rows, cards, locations, harness=harness,
                                     sid=current_sid, graph=graph)
    if graph is None:
        # Readable local history cannot establish uniqueness in an unavailable
        # shared graph. Keep the rows without inventing a second authority.
        for item in projected:
            item["predecessor"], item["relation"] = None, "unknown"
    return projected


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


def confirmed_predecessor_identity(seat: Seat, harness: str, sid: str) -> Optional[dict]:
    """Resolve only the exact predecessor edge in this native session's existing ledger."""
    if not _native_session_key(harness, sid):
        return None
    seat = session_seat(seat, harness, sid)
    predecessors = set()
    for row in _read_ledger_lines(seat):
        if (row.get("event") not in {"handover", "succession"}
                or (row.get("harness"), row.get("sid")) != (harness, sid)):
            continue
        old = row.get("from")
        if isinstance(old, dict):
            if old.get("harness") != harness:
                continue
            old = old.get("sid")
        if isinstance(old, str) and old and old != sid:
            predecessors.add(old)
    return {"harness": harness, "sid": next(iter(predecessors))} if len(predecessors) == 1 else None


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
    seat = session_seat(seat, harness, sid)
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
    native_path = _native_ledger_path(seat)
    physical_path = _physical_ledger_path(seat)
    if native_path == physical_path:
        # Physical-seat folding reads one existing ledger and preserves the
        # unary reader contract used by its observers.
        lines = _read_ledger_lines(seat)
    else:
        # A native ledger is distinct from its physical relation projection.
        lines = _read_ledger_lines(seat, include_physical_relations=False)
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
    native_addresses: dict[tuple[str, str], dict] = {}
    activity = [l for l in lines if l.get("event") not in {
        "handover", "succession", "steward-release", "tidy-claim", "real-prompt", "native-address"}]
    for item in lines:
        if item.get("event") == "tidy-claim" and item.get("claim_id"):
            claims[str(item["claim_id"])] = item
        elif item.get("event") == "real-prompt":
            key = (str(item.get("harness") or ""), str(item.get("sid") or ""))
            prior = real_frontiers.get(key)
            if prior is None or int(item.get("sequence", 0) or 0) >= int(prior.get("sequence", 0) or 0):
                real_frontiers[key] = item
        elif item.get("event") == "native-address":
            native_addresses[(str(item.get("harness") or ""), str(item.get("sid") or ""))] = item
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
                    for r in list(folded.values()) + relations + provenance
                    + list(native_addresses.values()) + recent)
    # Retained edges are bounded above; a fold that drops one changes reachability.
    dropped_edges = any(index not in keep_relation_indices for index, _row in handovers)
    try:
        atomic_write(native_path, body.encode("utf-8"))
    finally:
        _invalidate_ledger_read(seat)
    if dropped_edges:
        bump_dispatch_edge_generation()


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def card_key(seat: Seat) -> str:
    """Return the exact native card key, or the existing physical/project key without one."""
    harness, sid = session_identity(seat)
    native_key = _native_session_key(harness, sid)
    if native_key:
        return native_key
    if seat.kind != "project" or not sid:
        return seat.key
    keyed = _digest("project-card", seat.key, sid)
    if (state_root() / "cards" / f"{keyed}.json").exists():
        return keyed
    # Keep the old shared file and its history/receipts with its original author.
    legacy = read_json(state_root() / "cards" / f"{seat.key}.json")
    author = legacy.get("author") if isinstance(legacy, dict) else None
    if isinstance(legacy, dict) and legacy.get("schema") == SCHEMA and isinstance(legacy.get("body"), str) \
            and isinstance(author, dict) \
            and (author.get("harness"), author.get("sid")) == (harness, sid):
        return seat.key
    return keyed


def card_json_path(seat: Seat) -> Path:
    return state_root() / "cards" / f"{card_key(seat)}.json"


def card_text_path(seat: Seat) -> Path:
    return state_root() / "cards" / f"{card_key(seat)}.md"


def _native_address_status(seat: Seat) -> str:
    harness, sid = session_identity(seat)
    key = _native_session_key(harness, sid)
    if not key:
        return ""
    cache = _NATIVE_ADDRESS_READ_SCOPE.get()
    cache_key = (os.fspath(state_root()), key, harness, sid)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    rows = _read_ledger_lines(seat)
    marker = next((row for row in reversed(rows)
                   if row.get("event") == "native-address"
                   and (row.get("harness"), row.get("sid")) == (harness, sid)), None)
    status = str((marker or {}).get("legacy_status") or "")
    if cache is not None:
        cache[cache_key] = status
    return status


def _remember_native_address_status(seat: Seat, status: str) -> None:
    cache = _NATIVE_ADDRESS_READ_SCOPE.get()
    if cache is not None:
        harness, sid = session_identity(seat)
        cache[(os.fspath(state_root()), card_key(seat), harness, sid)] = status


def _legacy_native_source_keys(seat: Seat) -> list[str]:
    """Return only legacy card addresses already named by this caller or its ledger."""
    harness, sid = session_identity(seat)
    keys = set()
    if re.fullmatch(r"[0-9a-f]{20,64}", seat.key):
        # The caller's exact physical/project key is read-only provenance for migration.
        keys.add(seat.key)
    if harness in HARNESSES and seat.project_key:
        # Older project cards used the shared project-author slot.
        keys.add(_digest("project", harness, seat.project_key))
    if harness and sid:
        for row in _read_ledger_lines(seat):
            if (row.get("event") != "native-address"
                    or (row.get("harness"), row.get("sid")) != (harness, sid)):
                continue
            source_key = row.get("legacy_source")
            if isinstance(source_key, str) and re.fullmatch(r"[0-9a-f]{20,64}", source_key):
                keys.add(source_key)
    return sorted(keys)


def _legacy_native_cards(seat: Seat, source_keys=None) -> list[tuple[Path, bytes, dict]]:
    """Read exact known legacy card addresses; never enumerate the card store."""
    harness, sid = session_identity(seat)
    root = state_root() / "cards"
    if source_keys is None:
        source_keys = _legacy_native_source_keys(seat)
    logical = card_key(seat)
    out = []
    for source_key in sorted(set(source_keys)):
        if (not isinstance(source_key, str)
                or not re.fullmatch(r"[0-9a-f]{20,64}", source_key)
                or source_key == logical):
            continue
        path = root / f"{source_key}.json"
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
        except OSError:
            continue
        raw = read_bytes(path)
        if raw is None or len(raw) > 512 * 1024:
            continue
        try:
            card = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            continue
        author = card.get("author") if isinstance(card, dict) else None
        card_seat = card.get("seat") if isinstance(card, dict) else None
        if (isinstance(card, dict) and card.get("schema") == SCHEMA
                and isinstance(card.get("body"), str) and isinstance(author, dict)
                and (author.get("harness"), author.get("sid")) == (harness, sid)
                and isinstance(card_seat, dict) and card_seat.get("key") == source_key):
            out.append((path, raw, card))
    return out


def prepare_native_session_state(seat: Seat) -> bool:
    """Link one unambiguous author-bound legacy card into its deterministic native slot."""
    harness, sid = session_identity(seat)
    logical = _native_session_key(harness, sid)
    if not logical:
        return True
    target = state_root() / "cards" / f"{logical}.json"
    current = read_json(target)
    if current is not None:
        author = current.get("author") if isinstance(current, dict) else None
        valid = bool(isinstance(author, dict)
                     and (author.get("harness"), author.get("sid")) == (harness, sid))
        if valid:
            _remember_native_address_status(seat, "ready")
        else:
            _append_ledger_entry(seat, {"ts": now_epoch(), "harness": harness, "sid": sid,
                                        "event": "native-address", "legacy_status": "conflict"})
            _remember_native_address_status(seat, "conflict")
        return valid
    status = _native_address_status(seat)
    if status and status != "ready":
        return False

    source_keys = _legacy_native_source_keys(seat)
    candidates = _legacy_native_cards(seat, source_keys)
    if not candidates:
        if not status:
            _append_ledger_entry(seat, {"ts": now_epoch(), "harness": harness, "sid": sid,
                                        "event": "native-address", "legacy_status": "ready"})
            _remember_native_address_status(seat, "ready")
        return True
    first_path, first_raw, first_card = candidates[0]
    source_key = str(((first_card.get("seat") or {}).get("key") or ""))
    consumed_path = state_root() / "consumed" / f"{first_path.stem}.json"
    consumed_raw = read_bytes(consumed_path)
    conflict = not re.fullmatch(r"[0-9a-f]{20,64}", source_key)
    for path, raw, card in candidates[1:]:
        other_consumed = read_bytes(state_root() / "consumed" / f"{path.stem}.json")
        if (raw != first_raw or other_consumed != consumed_raw
                or str(((card.get("seat") or {}).get("key") or "")) != source_key):
            conflict = True
            break
    if conflict:
        _append_ledger_entry(seat, {"ts": now_epoch(), "harness": harness, "sid": sid,
                                    "event": "native-address", "legacy_status": "conflict"})
        _remember_native_address_status(seat, "conflict")
        return False

    try:
        text_raw = read_bytes(first_path.with_suffix(".md"))
        atomic_write(target, first_raw)
        atomic_write(state_root() / "cards" / f"{logical}.md",
                     text_raw if text_raw is not None else render_card_text(first_card).encode("utf-8"))

        history_src = state_root() / "card-history" / first_path.stem
        history_dst = state_root() / "card-history" / logical
        if history_src.exists() or history_src.is_symlink():
            if history_src.is_symlink() or not history_src.is_dir():
                raise StateError("legacy card history is not a private directory")
            ensure_dir(history_dst)
            for path in sorted(history_src.glob("*.json")):
                raw = read_bytes(path)
                if raw is None:
                    raise StateError("legacy card history is unreadable")
                try:
                    old = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError) as exc:
                    raise StateError("legacy card history is unreadable") from exc
                author = old.get("author") if isinstance(old, dict) else None
                if (isinstance(author, dict)
                        and (author.get("harness"), author.get("sid")) == (harness, sid)):
                    atomic_write(history_dst / path.name, raw)

        if consumed_raw is not None:
            atomic_write(state_root() / "consumed" / f"{logical}.json", consumed_raw)

        # Import only this author's old session rows; the physical ledger remains
        # intact and continues to serve pane-bound readers.
        source_ledger = state_root() / "sessions" / f"{source_key}.jsonl"
        raw_ledger = read_bytes(source_ledger) or b""
        rows = []
        for line in raw_ledger.decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            exact = (row.get("harness"), row.get("sid")) == (harness, sid)
            targeted = (row.get("harness") == harness
                        and row.get("event") == "tidy-claim"
                        and row.get("target_sid") == sid)
            if exact or targeted:
                rows.append(row)
        if rows:
            ledger_target = _native_ledger_path(seat)
            atomic_write(ledger_target, b"".join(
                (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                for row in rows))

        # Notices and prompt frontiers are seat-wide in the old layout. Carry
        # only this author's results and its own most recent prompt boundary.
        old_notices = read_json(state_root() / "notices" / f"{source_key}.json")
        own_notices = [item for item in (old_notices or {}).get("items", [])
                       if ((item.get("author") or {}).get("harness"),
                           (item.get("author") or {}).get("sid")) == (harness, sid)] \
            if isinstance(old_notices, dict) else []
        if own_notices:
            atomic_write_json(_notices_path(seat), {"schema": SCHEMA, "items": own_notices[-20:]})
        old_prompt = read_json(state_root() / "prompt-seq" / f"{source_key}.json")
        if isinstance(old_prompt, dict) and old_prompt.get("sid") == sid:
            atomic_write_json(_prompt_seq_path(seat), old_prompt)
        _append_ledger_entry(seat, {"ts": now_epoch(), "harness": harness, "sid": sid,
                                    "event": "native-address", "legacy_status": "ready",
                                    "legacy_source": source_key})
        _remember_native_address_status(seat, "ready")
        return True
    except BaseException:
        _append_ledger_entry(seat, {"ts": now_epoch(), "harness": harness, "sid": sid,
                                    "event": "native-address", "legacy_status": "conflict"})
        _remember_native_address_status(seat, "conflict")
        return False


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
        chosen = session_seat(seat, harness, sid)
        card = read_latest_card(chosen)
        author = (card or {}).get("author") or {}
        if not card or (author.get("harness"), author.get("sid")) != (harness, sid):
            return None, seat
        return card, chosen
    # One fixed legacy path is the only fallback; never enumerate author card files.
    card = read_latest_card(physical_seat(seat))
    if not card:
        return None, seat
    author = card.get("author") or {}
    harness, sid = author.get("harness") or seat.harness, author.get("sid") or ""
    return card, session_seat(seat, harness, sid)


def _normalize_body(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())
    return text.strip()


def sanitize_body(text: str) -> str:
    return card_format.bounded_body(_normalize_body(text), CARD_BODY_MAX_BYTES)


def _cut_bytes(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", "ignore"), True


def read_latest_card(seat: Seat) -> Optional[dict]:
    if _native_address_status(seat) == "conflict":
        return None
    card = read_json(card_json_path(seat))
    author = card.get("author") if isinstance(card, dict) else None
    harness, sid = session_identity(seat)
    exact = _native_session_key(harness, sid)
    if (isinstance(card, dict) and card.get("schema") == SCHEMA
            and isinstance(card.get("body"), str)
            and (not exact or isinstance(author, dict)
                 and (author.get("harness"), author.get("sid")) == (harness, sid))):
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
    seat = session_seat(seat, harness, sid)
    if _native_session_key(harness, sid):
        prepare_native_session_state(seat)
        if _native_address_status(session_seat(seat, harness, sid)) == "conflict":
            raise ValueError("ambiguous legacy card for native session")
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
        body = _normalize_body(body)
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
    if compact is None:
        history = state_root() / "card-history" / card_key(seat)
        full_path = history / f"{generation:08d}.json"
        bounded = card_format.bounded_body(body, CARD_BODY_MAX_BYTES, str(full_path))
        if bounded != body:
            # Save the complete incoming generation first. A failed history
            # write must leave the previous latest card intact.
            ensure_dir(history)
            atomic_write_json(full_path, card)
            card["source_history"] = str(full_path)
            card["body"] = bounded
    _write_card_pair(seat, card, rollback_on_error=compact is not None)
    if previous:
        try:
            history = ensure_dir(state_root() / "card-history" / card_key(seat))
            previous_path = history / f"{int(previous.get('generation', 0)):08d}.json"
            # An overflowing generation is already here in full; never replace
            # it with the bounded latest copy when the next card arrives.
            if not previous_path.exists():
                atomic_write_json(previous_path, previous)
            for old in sorted(history.glob("*.json"))[:-CARD_HISTORY_KEEP]:
                with contextlib.suppress(OSError):
                    old.unlink()
        except (OSError, StateError):
            if compact is None:
                raise
    seat_harness, seat_sid = session_identity(seat)
    if seat.kind == "project" and not _native_session_key(seat_harness, seat_sid) and seat_sid:
        atomic_write_json(_project_latest_path(seat), {"harness": seat_harness or harness, "sid": seat_sid})
    return card


def _compact_body(compact: dict) -> str:
    summary_text = str(compact.get("summary") or "")
    recent = str(compact.get("recent") or "")
    preserved = str(compact.get("recent_preserved") or "")
    post_recent = str(compact.get("recent_post", recent) or "")
    source = _cut_bytes(_normalize_body(str(compact.get("source") or compact.get("transcript") or "")),
                        COMPACT_METADATA_MAX_BYTES)[0]
    t = _ui_translator()
    partial_note = "\n" + t("tidy.compact.partial") if compact.get("recent_partial") else ""
    footer = ("\n\n" + t("tidy.compact.after") + partial_note + "\n" + post_recent) if post_recent \
        else ("\n\n" + partial_note.strip()) if partial_note else ""
    preserved_footer = ("\n\n" + t("tidy.compact.preserved") + "\n" + preserved) if preserved else ""
    reply = compact.get("last_reply") if isinstance(compact.get("last_reply"), dict) else {}
    reply_line = ""
    if reply.get("first_line"):
        timestamp = str(reply.get("timestamp") or "")
        try:
            instant = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=datetime.timezone.utc)
            utc = instant.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            kst = instant.astimezone(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST")
            when = f"{utc} / {kst}"
        except (ValueError, OverflowError):
            when = timestamp or t("tidy.compact.no_time")
        reply_line = t("tidy.compact.last_reply", when=when, line=str(reply["first_line"])[:1024]) + "\n\n"
    caution = "\n\n" + t("tidy.compact.caution")
    path = "\n" + (t("tidy.compact.source", source=source) if source else t("tidy.compact.no_source"))
    reply_priority = (t("tidy.compact.reply_first") + "\n\n") if reply_line else ""
    result = reply_line + reply_priority + summary_text + caution + path + preserved_footer + footer
    if len(result.encode("utf-8")) > COMPACT_CARD_BODY_MAX_BYTES:
        raise ValueError("compact card exceeds bounded size")
    return result


def update_compact_card_locked(seat: Seat, generation: int, identity: str, *, recent: str,
                               source: str = "", recent_partial: bool = False,
                               recent_preserved: str = "", last_reply: Optional[dict] = None) -> bool:
    """Idempotently add a bounded transcript tail without changing card identity."""
    card = read_latest_card(seat)
    compact = (card or {}).get("compact")
    if (not card or int(card.get("generation", 0)) != int(generation)
            or not isinstance(compact, dict) or compact.get("identity") != identity):
        return False
    compact["recent"], recent_cut = _cut_bytes(_normalize_body(recent), COMPACT_RECENT_MAX_BYTES)
    compact["recent_post"] = compact["recent"]
    # Preserve the original total tail budget, prioritizing the newest dialogue.
    remaining = COMPACT_RECENT_MAX_BYTES - len(compact["recent"].encode("utf-8"))
    compact["recent_preserved"], preserved_cut = _cut_bytes(_normalize_body(recent_preserved), remaining)
    if isinstance(last_reply, dict) and last_reply.get("first_line"):
        compact["last_reply"] = {
            "uuid": str(last_reply.get("uuid") or "")[:96],
            "byte_offset": max(0, int(last_reply.get("byte_offset", 0) or 0)),
            "timestamp": str(last_reply.get("timestamp") or "")[:64],
            "first_line": _cut_bytes(_normalize_body(str(last_reply["first_line"])), 1024)[0],
        }
    compact["recent_partial"] = bool(recent_partial or recent_cut or preserved_cut)
    if source:
        compact["source"] = _cut_bytes(_normalize_body(source), COMPACT_METADATA_MAX_BYTES)[0]
    card["body"] = _compact_body(compact)
    _write_card_pair(seat, card, rollback_on_error=True)
    return True


def mark_card_handed_off(seat: Seat) -> None:
    """The latest card went to a peer session: this seat's window is not continued from it
    (takes the seat lock; a newer card starts unmarked)."""
    with seat_lock(seat_state_key(seat)):
        card = read_latest_card(seat)
        if card and not card.get("handed_off"):
            card["handed_off"] = True
            atomic_write_json(card_json_path(seat), card)


def _card_author_label(card: dict) -> str:
    """Use the stored author's exact identity, independently of the current seat."""
    author = card.get("author") if isinstance(card.get("author"), dict) else {}
    seat = card.get("seat") if isinstance(card.get("seat"), dict) else {}
    harness, sid = author.get("harness") or "-", author.get("sid") or "-"
    tag = None
    if sid != "-":
        try:
            tools = str(Path(__file__).resolve().parents[1] / "tools")
            if tools not in sys.path:
                sys.path.insert(0, tools)
            from fleet.session_handle import resolve_tag
            tag = resolve_tag(harness, sid)
        except Exception:
            pass
    badge = f" [{tag}]" if isinstance(tag, str) and re.fullmatch(r"[0-9a-f]{2}", tag) else ""
    return f"{harness}{badge} pane={seat.get('pane') or '-'} sid={sid}"


def render_card_text(card: dict) -> str:
    """The readable card file: header, the card body (layer A), and the "참고할 기억" section (layer B)."""
    header = (f"# session card {card.get('generation')} — {card.get('authored_at')} "
              f"({_card_author_label(card)})\n\n")
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
    with seat_lock(seat_state_key(seat)):
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


def apply_deferred(entries) -> int:
    """Apply receipt writes a caller deferred until after its own final output.

    ``run_hook(..., deferred=[...])`` records plain dictionaries here instead of
    writing them, so a bridge that assembles more context (the Codex hook, the
    OpenCode plugin) can commit only after its own output returned. Every entry
    carries the exact state path resolved at emit time, so no seat is rebuilt and
    no new state file exists. A failed render simply leaves the entries
    unapplied: the card/candidates stay due for the next prompt.
    """
    applied = 0
    for entry in list(entries or []):
        if not isinstance(entry, dict):
            continue
        path = Path(str(entry.get("path") or ""))
        if not path.name:
            continue
        try:
            kind = entry.get("kind")
            if kind == "startup-context":
                data = read_json(path)
                data = dict(data) if isinstance(data, dict) else {}
                data.update(schema=SCHEMA, startup_context=entry.get("receipt"))
                atomic_write_json(path, data)
            elif kind in ("card-consumed", "memory-consumed"):
                data = read_json(path)
                data = dict(data) if isinstance(data, dict) else {}
                if kind == "card-consumed":
                    same = data.get("generation") == entry.get("generation")
                    receipts = list(data.get("receipts", [])) if same else []
                    receipts.append(str(entry.get("receipt") or ""))
                    data.update({"generation": entry.get("generation"),
                                 "receipts": receipts[-200:]})
                else:
                    same = data.get("memory_revision") == entry.get("revision")
                    receipts = list(data.get("memory_receipts", [])) if same else []
                    receipts.append(str(entry.get("receipt") or ""))
                    data.update({"memory_revision": entry.get("revision"),
                                 "memory_receipts": receipts[-200:]})
                data["schema"] = SCHEMA
                atomic_write_json(path, data)
            elif kind == "notices":
                data = read_json(path)
                if entry.get("require_existing") and not isinstance(data, dict):
                    continue
                if entry.get("require_items") and not isinstance(
                        data.get("items") if isinstance(data, dict) else None, list):
                    continue
                data = data if isinstance(data, dict) else {}
                remove = {str(value) for value in entry.get("remove_ids") or [] if value}
                items = data.get("items") if isinstance(data.get("items"), list) else []
                delivered = data.get("delivered") if isinstance(data.get("delivered"), list) else []
                delivered = [value for value in delivered if isinstance(value, str)]
                delivered.extend(str(value) for value in entry.get("delivered") or [] if value)
                received = data.get("lineage_received") \
                    if isinstance(data.get("lineage_received"), list) else []
                received = [value for value in received if isinstance(value, str)]
                received.extend(str(value) for value in entry.get("lineage_received") or [] if value)
                atomic_write_json(path, {
                    "schema": SCHEMA,
                    "items": [item for item in items
                              if not isinstance(item, dict) or item.get("id") not in remove],
                    "delivered": list(dict.fromkeys(delivered))[-200:],
                    "lineage_received": list(dict.fromkeys(received))[-200:],
                })
            else:
                continue
            applied += 1
        except (OSError, TypeError, ValueError):
            continue
    return applied


def _receipt(harness: str, sid: str, epoch: int) -> str:
    return f"{harness}:{sid}:{epoch}"


def _reread_path(harness: str, sid: str) -> Path:
    return state_root() / "reread" / f"{harness}-{_digest(sid, size=16)}.json"


def _notices_path(seat: Seat) -> Path:
    return state_root() / "notices" / f"{seat_state_key(seat)}.json"


def _prompt_seq_path(seat: Seat) -> Path:
    harness, _sid = session_identity(seat)
    target_key = _native_session_key(harness, seat.continuation_sid) \
        if seat.continuation_sid else ""
    return state_root() / "prompt-seq" / f"{target_key or seat_state_key(seat)}.json"


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
    seat = session_seat(seat, harness, sid)
    previous = read_json(_prompt_seq_path(seat))
    seq = read_prompt_seq(seat) + 1
    prior_human = last_human_prompt_seq(seat) if isinstance(previous, dict) else 0
    atomic_write_json(_prompt_seq_path(seat), {"schema": SCHEMA, "seq": seq, "at": now,
                                              "harness": harness, "sid": sid, "source": "pending",
                                              "last_human_seq": seq, "prior_human_seq": prior_human})
    return seq


def last_human_prompt_seq(seat: Seat) -> int:
    """The newest prompt boundary that was not a runtime task notification (older files: the newest)."""
    current = read_json(_prompt_seq_path(seat))
    if not isinstance(current, dict):
        return 0
    value = current.get("last_human_seq", current.get("seq", 0))
    return value if type(value) is int and value >= 0 else int(current.get("seq", 0) or 0)


# Turns the runtime itself starts: a finished background task (Claude ``<task-notification>``,
# Codex ``<subagent_notification>``), a message from another session of the same user
# (Claude ``<agent-message>`` with its fixed framing) and runtime ``<system-reminder>`` text.
_RUNTIME_BLOCK = re.compile(
    r"<(task-notification|subagent_notification|agent-message|system-reminder)\b[^>]*>.*?</\1>", re.S)
_PEER_FRAMING = (
    re.compile(r"^\s*Another Claude session sent a message[^\n:]*:"),
    # The fixed one-line paragraph, up to its fixed last sentence and no further.
    re.compile(r'That "other Claude session" is an agent working inside this same session\b'
               r"[^\n]*?that's permission laundering\."),
)
# Prompt sources that are not the person: they answer no wait, rearm no idle tidy and keep
# ``last_human_seq`` where it was.
NON_HUMAN_SOURCES = frozenset({"notification", "peer", "runtime", "claim"})


def runtime_turn_source(prompt_text: str) -> str:
    """``notification``, ``peer`` or ``runtime`` when the prompt holds nothing but runtime blocks
    and their fixed framing; "" as soon as the person wrote anything of their own in it."""
    if not isinstance(prompt_text, str):
        return ""
    prompt_text = runtime_prompt_text(prompt_text)
    kinds = [match.group(1) for match in _RUNTIME_BLOCK.finditer(prompt_text)]
    if not kinds:
        return ""
    rest = _RUNTIME_BLOCK.sub("", prompt_text)
    for framing in _PEER_FRAMING:
        rest = framing.sub("", rest)
    if rest.strip():
        return ""
    if any(kind in ("task-notification", "subagent_notification") for kind in kinds):
        return "notification"
    return "peer" if "agent-message" in kinds else "runtime"


def is_task_notification(prompt_text: str) -> bool:
    """A turn the runtime started for a finished background task (Claude ``<task-notification>``)."""
    return runtime_turn_source(prompt_text) == "notification"


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
        origin = module.prompt_origin(runtime_prompt_text(prompt_text), harness, sid)
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
    seat = session_seat(seat, harness, sid)
    path = _prompt_seq_path(seat)
    current = read_json(path)
    if (not isinstance(current, dict) or current.get("seq") != sequence
            or current.get("harness") != harness or current.get("sid") != sid):
        return False
    current["request_origin"] = dict(origin)
    atomic_write_json(path, current)
    return True


def write_notice(seat: Seat, text: str, *, author_harness: str = "", author_sid: str = "",
                 now: Optional[float] = None, notice_id: str = "") -> dict:
    """Leave one result line for the seat's next start/prompt (takes the seat lock)."""
    if author_harness and author_sid:
        seat = session_seat(seat, author_harness, author_sid)
    now = now_epoch() if now is None else now
    line = " ".join((text or "").split())
    line = _cut_bytes("".join(ch for ch in line if ch.isprintable()), NOTICE_MAX_BYTES)[0]
    item = {"id": (notice_id[:96] if notice_id else secrets.token_hex(6)), "text": line, "created": now,
            "author": {"harness": author_harness, "sid": author_sid}}
    with seat_lock(seat_state_key(seat)):
        data = read_json(_notices_path(seat))
        items = data.get("items", []) if isinstance(data, dict) else []
        delivered = data.get("delivered", []) if isinstance(data, dict) else []
        lineage_received = data.get("lineage_received", []) if isinstance(data, dict) else []
        if item["id"] in delivered:
            return item
        for index, existing in enumerate(items):
            if existing.get("id") == item["id"]:
                items[index] = {**existing, "text": line}
                atomic_write_json(_notices_path(seat), {"schema": SCHEMA, "items": items[-20:],
                                                        "delivered": delivered[-200:],
                                                        "lineage_received": lineage_received[-200:]})
                return items[index]
        items.append(item)
        atomic_write_json(_notices_path(seat), {"schema": SCHEMA, "items": items[-20:],
                                                "delivered": delivered[-200:],
                                                "lineage_received": lineage_received[-200:]})
    return item


def emit_idle_notice(seat: Seat, harness: str, sid: str, emit_human, deferred=None) -> None:
    """The completed idle result goes to native human output, never to a new model turn."""
    if emit_human is None:
        return
    seat = session_seat(seat, harness, sid)
    with session_state_lock(seat):
        data = read_json(_notices_path(seat)) or {}
        items = [n for n in data.get("items", []) if str(n.get("id", "")).startswith("idle:")
                 and (n.get("author") or {}) == {"harness": harness, "sid": sid}]
        if not items:
            return
        emit_human("\n".join(n["text"] for n in items))
        ids = [n["id"] for n in items]
        receipt = {"kind": "notices", "path": str(_notices_path(seat)),
                   "remove_ids": ids, "delivered": ids, "lineage_received": []}
    if deferred is None:
        apply_deferred([receipt])
    else:
        deferred.append(receipt)


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


def _ago(seconds: float, t=None) -> str:
    t = t or _ui_translator()
    seconds = max(0, int(seconds))
    if seconds < 90:
        return t("tidy.ago.now")
    count, unit = ((seconds // 60, "minute") if seconds < 3600 else
                   (seconds // 3600, "hour") if seconds < 172800 else (seconds // 86400, "day"))
    return t(f"tidy.ago.{unit}" if count == 1 else f"tidy.ago.{unit}s", count=count)


def previous_transcript_line(harness: str, sid: str, location, t=None) -> str:
    """The one "이전 대화" line shared by card injection and a supervisor handoff.

    The 320-byte rule is the existing rendering: a longer path becomes
    ``경로 생략`` instead of an unbounded absolute path. ``location`` is the
    transcript dict produced by ``_lineage_transcript``.
    """
    t = t or _ui_translator()
    if not isinstance(location, dict):
        location = {}
    status = str(location.get("status") or "missing")
    path = str(location.get("path") or "")
    if status == "available" and path:
        if len(path.encode("utf-8")) > 320:
            path = t("tidy.card.path_omitted")
        detail = path
    else:
        detail = t("tidy.card.transcript_unreadable" if status == "unreadable" else "tidy.card.transcript_none")
    return t("tidy.card.previous", harness=harness, sid=sid[:8], detail=detail)


def build_card_injection(card: dict, card_path: Path, now: float, cap: int,
                         previous_transcript: Optional[dict] = None) -> str:
    """Card text within ``cap`` UTF-8 bytes; guidance is part of the fixed header."""
    t = _ui_translator()
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(card.get("authored_at_epoch") or now)))
    prefix = t("tidy.card.prefix", stamp=stamp, ago=_ago(now - float(card.get("authored_at_epoch") or now), t))
    identity = f" · {_card_author_label(card)}"
    header = f"{prefix}{identity}\n{t('tidy.card.first_reply', fields='·'.join(CARD_FIXED_FIELDS))}"
    footer = t("tidy.card.full_card", path=card_path)
    previous_line = ""
    if isinstance(previous_transcript, dict):
        previous_line = previous_transcript_line(
            str(previous_transcript.get("harness") or ""),
            str(previous_transcript.get("sid") or ""),
            previous_transcript.get("transcript"), t)
    cut_note = t("tidy.card.cut")
    body = card.get("body", "")
    fixed = (len(header.encode("utf-8")) + len(footer.encode("utf-8")) + 2
             + (len(previous_line.encode("utf-8")) + 1 if previous_line else 0))
    if fixed > cap and previous_line:
        previous_line = ""
        fixed = len(header.encode("utf-8")) + len(footer.encode("utf-8")) + 2
    if fixed > cap:
        header = prefix + identity
        fixed = len(header.encode("utf-8")) + len(footer.encode("utf-8")) + 2
    if fixed > cap:
        # A tiny cap drops identity as a unit, never an apparently exact SID prefix.
        header = prefix
        fixed = len(header.encode("utf-8")) + len(footer.encode("utf-8")) + 2
    room = cap - fixed
    if room <= 0:
        return _cut_bytes(f"{header}\n{footer}" + (f"\n{previous_line}" if previous_line else ""), cap)[0]
    if len(body.encode("utf-8")) <= room:
        return f"{header}\n{body}\n{footer}" + (f"\n{previous_line}" if previous_line else "")
    if room > len(cut_note.encode("utf-8")) + 1:
        room -= len(cut_note.encode("utf-8")) + 1
        trimmed = card_format.complete_lines(body, room) + "\n" + cut_note
    else:
        trimmed = card_format.complete_lines(body, room)
    return f"{header}\n{trimmed}\n{footer}" + (f"\n{previous_line}" if previous_line else "")


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

# The Claude SessionStart card hook has 5 s; it shows the person-only history
# line only while it is within this much of its start (a normal start takes
# about 1 s, HOOK-LATENCY measured up to 3.7 s).
HUMAN_NOTICE_BUDGET_SECONDS = 2.0


def build_human_history_notice(seat: Seat, harness: str, sid: str, *, env=None,
                              read_context=None) -> str:
    """Build a person-only predecessor notice without adding it to model context."""
    env = os.environ if env is None else env
    lineage = seat_lineage(seat, harness=harness, sid=sid, predecessor_only=True,
                           read_context=read_context)
    previous = _confirmed_predecessor(lineage, harness, sid)
    if not previous:
        return ""
    t = _ui_translator()
    transcript_info = previous.get("transcript") or {}
    state = transcript_info.get("status", "missing")
    availability = t({"available": "tidy.history.available", "missing": "tidy.history.missing",
                      "expired": "tidy.history.missing"}.get(state, "tidy.history.unreadable"))
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
    return t("tidy.history.open", action=action, harness=previous.get("harness", harness),
             sid=str(previous.get("sid", ""))[:8], availability=availability)


def run_hook(*args, **kwargs) -> str:
    """Record the session and print its card / notices (see ``_run_hook``).

    A confirmed ``/clear`` also lets go of what its predecessor still held. That runs
    here, after every hook lock is released: the release takes its own seat lock, and
    taking it under the hook's would deadlock."""
    closed: list = []
    try:
        return _run_hook(*args, _closed=closed, **kwargs)
    finally:
        if closed:
            with contextlib.suppress(BaseException):
                import dispatch_seat_handover as handover
                handover._peer_message().release_closed_session(
                    closed[0]["harness"], closed[0]["sid"], reason="cleared")


def _run_hook(harness: str, event: str, sid: str, *, source: str = "", transcript: str = "",
              cwd: str = "", reread: bool = False, env=None, now: Optional[float] = None,
              emit: Optional[Callable[[str], None]] = None, phase: Optional[str] = None,
              compact_summary: str = "", compact_context: Optional[dict] = None,
              prompt_text: str = "", emit_human: Optional[Callable[[str], None]] = None,
              human_only: bool = False,
              render_context: bool = True, startup_context: Optional[dict] = None,
              deferred: Optional[list] = None, _closed: Optional[list] = None,
              human_deadline: Optional[float] = None, read_context=None) -> str:
    """Record the session, and print the card / notices it is due exactly once.

    Returns the text that was emitted ("" for none). The receipt is written only
    after ``emit`` returned, so a hook whose output never went out keeps the card
    for the next start or prompt. A caller that only *collects* the text (a JSON
    bridge that renders more context afterwards) passes ``deferred=[]`` instead;
    the writes are then appended there for :func:`apply_deferred` after that
    caller's own final output.
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
        text = ""  # Native compaction owns its summary; no second tidy workflow.
        if text and emit:
            emit(text)
        return text
    if event == "compact" and phase == "after":
        booked = _run_compact_after(harness, sid, compact_summary, cwd=cwd, transcript=transcript,
                                    compact_context=compact_context or {}, env=env, now=now)
        emit_idle_notice(Seat("project", "", harness=harness, sid=sid), harness, sid, emit_human, deferred)
        return booked
    now = now_epoch() if now is None else now
    seat = session_seat(resolve_seat(harness, cwd or None, env, sid, source, hook=True), harness, sid)
    emit_idle_notice(seat, harness, sid, emit_human, deferred)
    if human_only:
        if event == "start" and emit_human:
            with contextlib.suppress(BaseException):
                notice = build_human_history_notice(seat, harness, sid, env=env,
                                                    read_context=read_context)
                if notice:
                    emit_human(notice)
        return ""
    if _native_session_key(harness, sid):
        prior_identity = None
        if event == "start" and source == "clear":
            with contextlib.suppress(BaseException):
                import session_tidy_clear as clear
                booking = clear.read_reservation(seat.key)
                if (booking and booking.get("harness") == harness
                        and booking.get("new_session") == sid and booking.get("sid")
                        and booking.get("sid") != sid):
                    prior_identity = {"harness": harness, "sid": str(booking["sid"])}
        if prior_identity is None:
            prior_identity = confirmed_predecessor_identity(seat, harness, sid)
        prior_seat = None
        if prior_identity:
            prior_seat = dataclasses.replace(
                session_seat(seat, prior_identity["harness"], prior_identity["sid"]),
                continuation_sid="")
            with session_state_lock(prior_seat):
                prepare_native_session_state(prior_seat)
        with session_state_lock(seat):
            prepare_native_session_state(seat)
    if event == "start":
        with contextlib.suppress(BaseException):
            import session_tidy_clear
            req = session_tidy_clear.read_reservation(seat.key)
            if req and req.get("harness") == harness and req.get("sid") == sid:
                session_tidy_clear.reconcile_pending(seat)
    if event in ("start", "prompt"):
        with contextlib.suppress(BaseException):
            import session_tidy_runner
            session_tidy_runner.reconcile_queued(seat, harness, sid)
            if event == "start":
                session_tidy_runner.reconcile_reservation_queue(seat)
    answer_wait_lines = []
    if render_context and event in ("start", "prompt"):
        with contextlib.suppress(BaseException):
            utilities = str(Path(__file__).resolve().parent)
            if utilities not in sys.path:
                sys.path.insert(0, utilities)
            import human_wait_state
            projected = human_wait_state.cached_view(now=now)
            projected = projected or {"items": [], "by_session": {}}
            # The caller's own S4 is exact and cheap; replace its possibly older
            # producer copy so a just-closed prompt cannot be resurrected.
            own = human_wait_state._read(human_wait_state._path(seat.key))
            projected["items"] = [item for item in projected["items"]
                                   if not (item.get("source") == "S4"
                                           and (item.get("owner") or {}).get("seat_key") == seat.key)]
            if own:
                projected["items"].append(
                    {"id": own["id"], "source": "S4", "question": own.get("question"),
                     "since": own.get("since"), "stale": False,
                     "owner": {"harness": harness, "session_id": sid, "seat_key": seat.key}})
            ids = set(projected.get("by_session", {}).get((harness, sid), ()))
            for item in projected.get("items", ()):
                owner = item.get("owner") or {}
                if item.get("stale"):
                    continue
                if item.get("id") not in ids and owner.get("seat_key") != seat.key:
                    continue
                if (event == "prompt" and item.get("source") == "S4"
                        and owner.get("seat_key") == seat.key):
                    continue
                question = item.get("question") or "사용자 답 대기"
                answer_wait_lines.append("[답변 필요] " + str(question)[:160])
                if len(answer_wait_lines) == 3:
                    break
    state_seats = [seat]
    if _native_session_key(harness, sid) and prior_seat:
        state_seats.append(prior_seat)
    elif _native_session_key(harness, sid) and event in ("start", "prompt"):
        # OpenCode can confirm a snapshot's successor on its first message in
        # this hook. Lock that named predecessor too; the snapshot is only a
        # lock hint here, while the handover reader still decides authority.
        with contextlib.suppress(BaseException):
            import dispatch_seat_handover as handover
            snapshot = handover.read_snapshot(seat.key)
            origin = (snapshot or {}).get("from") or {}
            old_sid = str(origin.get("sid") or "")
            if origin.get("harness") == harness and old_sid and old_sid != sid:
                state_seats.append(session_seat(seat, harness, old_sid))
    with _session_state_locks(state_seats):
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
            runtime_source = runtime_turn_source(prompt_text)
            request_origin = None if runtime_source else _current_request_origin(prompt_text, harness, sid)
            self_request = matching_tidy_claim(seat, harness, sid, prompt_text, sequence)
            notification = bool(runtime_source or request_origin or self_request)
            if runtime_source:
                # A finished background task or another session reporting in is new content,
                # not the person: it neither answers a wait nor rearms or cancels automatic tidy.
                mark_prompt_source(seat, harness, sid, sequence, runtime_source)
            elif request_origin:
                # A verified Hearting transfer (peer or runtime completion, any harness) is the
                # same: another session speaking, not the person.
                record_prompt_origin(seat, harness, sid, sequence, request_origin)
                mark_prompt_source(seat, harness, sid, sequence, "peer")
            elif self_request:
                mark_prompt_source(seat, harness, sid, sequence, "claim", self_request["claim_id"])
            else:
                # A physical user prompt closes this exact seat's marked wait.
                # Transfer/automatic prompts are not answers from the user.
                with contextlib.suppress(BaseException):
                    import human_wait_state
                    human_wait_state.close_signal(seat.key, sequence=sequence)
            with contextlib.suppress(BaseException):
                import session_tidy_clear as clear
                claimed = clear.observe_prompt_locked(seat, harness, sid, prompt_text, now, sequence)
                if not claimed and not notification:
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
        appended = False
        if ((event == "start" and source == "clear")
                or (harness == "opencode" and event == "prompt")):
            with contextlib.suppress(BaseException):
                appended = _record_tidy_clear_succession_locked(seat, harness, sid, now)
        # The one place a same-seat handover (A -> B) is recorded: B's confirmed start/first prompt.
        handed = None
        handover = None
        if seat.kind in ("pane", "native") and event in ("start", "prompt"):
            with contextlib.suppress(BaseException):    # a hook never fails for the handover
                import dispatch_seat_handover as handover
                handed = handover.record_locked(seat, harness, sid, event, source, now)
                if handed:
                    # Keep the shared writer's confirmed edge in this exact native ledger
                    # as well as its physical seat ledger for current seat-scoped readers.
                    _append_ledger_entry(physical_seat(seat), handed)
        if (_closed is not None and seat.kind in ("pane", "native")
                and ((event == "start" and source == "clear")
                     or (harness == "opencode" and event == "prompt" and (handed or appended)))):
            with contextlib.suppress(BaseException):    # a hook never fails for a release
                predecessor = _confirmed_clear_predecessor(seat, harness, sid)
                if predecessor:
                    _closed.append(predecessor)
        if not render_context:
            return ""
        epoch = int(row.get("epoch", 0) or 0)
        if startup_context is not None and event == "prompt":
            # Reuse the existing consumption file and deferred commit boundary:
            # a failed outer render leaves this first-prompt probe due.
            start_at = (prior or {}).get("last_start_at", 0)
            receipt = f"{_receipt(harness, sid, epoch)}:start:{start_at}"
            consumed = read_json(_consumed_path(seat))
            consumed = consumed if isinstance(consumed, dict) else {}
            startup_context["due"] = consumed.get("startup_context") != receipt
            if startup_context["due"]:
                entry = {"kind": "startup-context", "path": str(_consumed_path(seat)), "receipt": receipt}
                if deferred is not None:
                    deferred.append(entry)
                else:
                    apply_deferred([entry])
        parts: list[str] = []
        lineage = []
        predecessor = None
        predecessor_seat = None
        if _native_session_key(harness, sid):
            predecessor = confirmed_predecessor_identity(seat, harness, sid)
            if predecessor:
                predecessor_seat = session_seat(seat, predecessor["harness"], predecessor["sid"])
        notice_groups = [(seat, pending_notices(seat, harness, sid, now=now, summary=sessions))]
        if predecessor_seat:
            current_notice_state = read_json(_notices_path(seat))
            received_ids = set((current_notice_state or {}).get("lineage_received", [])) \
                if isinstance(current_notice_state, dict) else set()
            prior_notices = [item for item in pending_notices(
                predecessor_seat, harness, sid, now=now, summary=sessions)
                if item.get("id") not in received_ids]
            notice_groups.append((predecessor_seat, prior_notices))
        notice_groups = [(origin, [n for n in group if not str(n.get("id", "")).startswith("idle:")])
                         for origin, group in notice_groups]
        notices = []
        notice_ids = set()
        for _source_seat, group in notice_groups:
            for item in group:
                if item.get("id") not in notice_ids:
                    notices.append(item)
                    notice_ids.add(item.get("id"))
        notice_block = ""
        if notices:
            t = _ui_translator()
            notice_block = "\n".join(t("tidy.notice.result", text=n["text"]) for n in notices)
            notice_block = _cut_bytes(notice_block, NOTICE_MAX_BYTES)[0]
            parts.append(notice_block)
        card_seat = seat
        project_fallback = seat.kind == "project" and not _native_session_key(harness, sid)
        own_card = read_latest_card(seat) if project_fallback else None
        own_author = (own_card or {}).get("author") or {}
        own_card_due = bool(own_card
                            and (own_author.get("harness"), own_author.get("sid")) == (harness, sid)
                            and own_card.get("origin") != "runtime-compact"
                            and epoch > int(own_author.get("epoch", 0) or 0))
        if own_card_due:
            card = own_card
        elif project_fallback:
            card, card_seat = read_latest_project_card(seat)
        else:
            card = read_latest_card(seat)
        predecessor_card = False
        if not card and predecessor_seat:
            inherited = read_latest_card(predecessor_seat)
            author = (inherited or {}).get("author") or {}
            if inherited and (author.get("harness"), author.get("sid")) == (
                    predecessor["harness"], predecessor["sid"]):
                card, card_seat, predecessor_card = inherited, predecessor_seat, True
        card_due = False
        memory_due = False
        memory = None
        resume_text = ""
        if card:
            author = card.get("author") or {}
            same_session = (author.get("harness"), author.get("sid")) == (harness, sid)
            if project_fallback and own_card_due:
                eligible = True
            elif project_fallback and not same_session:
                eligible = _appeared_after(sessions, harness, sid,
                                           (author.get("harness") or "", author.get("sid") or ""))
            elif project_fallback:
                eligible = (card.get("origin") != "runtime-compact"
                            and epoch > int(author.get("epoch", 0) or 0))
            elif (predecessor_card and predecessor
                  and (author.get("harness"), author.get("sid"))
                  == (predecessor.get("harness"), predecessor.get("sid"))):
                eligible = True
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
                if predecessor:
                    with contextlib.suppress(BaseException):
                        lineage = seat_lineage(seat, harness=harness, sid=sid)
                        previous = _confirmed_predecessor(lineage, harness, sid)
                parts.append(build_card_injection(
                    card, card_text_path(card_seat), now, room, previous_transcript=previous))
            if memory_text:
                parts.append(memory_text)
        if not card and handed and handover is not None:
            resume_text = handover.resume_line(seat, harness, sid)
            if resume_text:
                parts.append(resume_text)
        parts.extend(answer_wait_lines)
        text = "\n".join(parts)
        # The person-only "previous conversation" line needs the whole shared
        # lineage (every seat ledger, about half of a 1 s start). Past
        # ``human_deadline`` (monotonic) a loaded start leaves it out so the
        # card itself still reaches the session inside the hook's limit.
        if (event == "start" or (event == "prompt" and (startup_context or {}).get("due"))) and emit_human and (
                human_deadline is None or time.monotonic() < human_deadline):
            with contextlib.suppress(BaseException):
                notice = build_human_history_notice(seat, harness, sid, env=env,
                                                    read_context=read_context)
                if notice:
                    emit_human(notice)
        if not text:
            return ""
        (emit or (lambda t: None))(text)
        # The receipts are prepared here and applied by the caller when it
        # renders this text into its own output. A standalone hook (the Claude
        # card bridge prints in-process, a text CLI prints inside ``emit``) has
        # no later boundary, so it applies them immediately.
        receipts = [] if deferred is None else deferred
        if card_due:
            receipts.append({"kind": "card-consumed",
                             "path": str(_consumed_path(card_seat)),
                             "generation": card["generation"],
                             "receipt": _receipt(harness, sid, epoch)})
        if memory_due:
            receipts.append({"kind": "memory-consumed",
                             "path": str(_consumed_path(card_seat)),
                             "revision": memory["revision"],
                             "receipt": _receipt(harness, sid, epoch)})
        if notices:
            own_ids = {item.get("id") for item in notice_groups[0][1]}
            inherited_ids = {item.get("id") for _origin, group in notice_groups[1:]
                             for item in group}
            receipts.append({"kind": "notices", "path": str(_notices_path(seat)),
                             "remove_ids": sorted(value for value in own_ids if value),
                             "delivered": sorted(value for value in own_ids if value),
                             "lineage_received": sorted(
                                 value for value in inherited_ids if value)})
            # The successor's receipt also settles the exact predecessor item, so the
            # same result cannot reappear if that native session later prompts again.
            for origin, group in notice_groups[1:]:
                origin_ids = {item["id"] for item in group
                              if isinstance(item.get("id"), str) and item["id"]}
                if not origin_ids:
                    continue
                receipts.append({"kind": "notices", "path": str(_notices_path(origin)),
                                 "require_existing": True, "require_items": True,
                                 "remove_ids": sorted(origin_ids),
                                 "delivered": sorted(origin_ids),
                                 "lineage_received": []})
        if deferred is None:
            apply_deferred(receipts)
        # OpenCode re-emits this cache every turn: a later "참고할 기억" is added to what the session
        # already received instead of replacing it.
        earlier = read_json(_reread_path(harness, sid))
        kept = str(earlier.get("text") or "") if isinstance(earlier, dict) and earlier.get("epoch") == epoch else ""
        cached = _cut_bytes(kept + "\n" + text, 2 * INJECTION_MAX_BYTES)[0] if kept else text
        atomic_write_json(_reread_path(harness, sid), {"schema": SCHEMA, "epoch": epoch, "text": cached, "at": now})
        return text


def _run_compact_after(harness: str, sid: str, summary: str, *, cwd: str, transcript: str,
                       compact_context: Optional[dict], env, now: Optional[float]) -> str:
    """Observe native compaction. Never turn it into another card/clear/model request."""
    now = now_epoch() if now is None else now
    seat = session_seat(resolve_seat(harness, cwd or None, env, sid, "compact", hook=True), harness, sid)
    try:
        with session_state_lock(seat, wait=COMPACT_LOCK_WAIT_SEC):
            prior = session_summary(seat).get((harness, sid))
            record_event(seat, harness, sid, "compact", source="compact", transcript=transcript,
                         cwd=cwd, now=now,
                         bump_epoch=not (prior and now - prior["last_compact_at"] < COMPACT_DEDUPE_SEC))
    except (OSError, TimeoutError, StateError):
        return ""  # Native history remains the evidence; never hold up compaction.
    import session_tidy_clear as clear
    clear.note_compaction(seat, harness, sid, now=now)
    return ""


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


def resolve_caller(harness, sid, cwd: str, *, project: bool = True) -> Optional[tuple[Seat, str, str]]:
    """(seat, harness, session id) of the calling session; ids come from the arguments,
    harness variables and, for pane/native seats, the seat ledger. Project ledger inference
    selects only the shared seat and harness when project resolution is enabled; an absent
    caller id gets a fresh ``unknown-`` id."""
    explicit_sid = bool(sid)
    if not sid:
        harness, sid = session_from_env(harness)
    detected = harness or session_from_env(None)[0]
    seat = (resolve_seat(detected, cwd, sid=sid) if project else
            resolve_seat(detected, cwd, sid=sid, project=False))
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
        if project and not detected:
            guessed_seat, ledger_row = _guess_seat_from_ledgers(cwd)
            if guessed_seat is not None and ledger_row:
                seat = guessed_seat
                harness = ledger_row.get("harness") or ""
        harness = harness or detected or "unknown"
        sid = sid or f"unknown-{secrets.token_hex(4)}"
        seat = session_seat(seat, harness, sid)
    elif not sid or not harness:
        ledger_row = latest_session(seat, harness)
        if ledger_row:
            harness = harness or ledger_row["harness"]
            sid = sid or ledger_row["sid"]
    if seat is None:
        return None
    return seat, harness or detected or "unknown", sid or f"unknown-{secrets.token_hex(4)}"


def write_session_card(seat: Seat, harness: str, sid: str, body: str, *, cwd: str = "",
                       prompt_seq: Optional[int] = None) -> dict:
    """Seal one card generation through the existing ledger event and card writer.

    Callers hold no lock here: the helper takes the same shared seat lock, records
    the ``card`` event, then writes the card. ``prompt_seq`` defaults to the
    current frontier exactly as the caller-card path does.
    """
    with session_state_lock(seat):
        prepare_native_session_state(seat)
        previous = read_latest_card(seat)
        generation = int(previous.get("generation", 0) or 0) + 1 if previous else 1
        record_event(seat, harness, sid, "card", cwd=cwd, card_generation=generation)
        return write_card(seat, harness, sid, body, cwd=cwd,
                          prompt_seq=read_prompt_seq(seat) if prompt_seq is None else prompt_seq)


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
    write_session_card(seat, harness, sid, body, cwd=cwd)
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
                               operation_id=operation_id, origin=(idle_context or {}).get("origin", ""), start=False)
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


def _ui_translator():
    """The shared UI-text catalog (English by default, Korean when declared)."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "tools" / "i18n" / "ui_text.py"
    spec = importlib.util.spec_from_file_location("ui_text", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.translator()


def tidy_report_line(booked: str, *, handoff: bool = False, origin: str = "", harness: str = "") -> str:
    """The one line the session shows the user after a tidy, from the enqueue ``clear=`` words."""
    t = _ui_translator()
    fields = str(booked or "").split()
    outcome = next((field for field in fields if field.startswith("clear=")), "")
    if outcome == "clear=scheduled":
        # The clear waits for this turn to end with an empty input box; typing first keeps the window.
        if handoff or origin == "idle" or "continue=off" in fields:
            return t("tidy.say.clear_soon")
        return t("tidy.say.clear_and_continue")
    command = "/new" if harness == "opencode" else "/clear"
    if outcome == "clear=off":
        return t("tidy.say.window_kept", command=command)
    hint = next((field.split("=", 1)[1] for field in fields if field.startswith("hint=")), "")
    if outcome == "clear=manual":
        return t("tidy.say.you_can_now", command=hint or command)
    if outcome == "clear=skipped":
        return t("tidy.say.you_can_now", command=command)
    return t("tidy.say.accepted")


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
        handoff_line, code = runner.handoff(caller[0], args.handoff, caller=caller)
        print(handoff_line)
    origin = str((idle_context or {}).get("origin") or "")
    print("say=" + tidy_report_line(line, handoff=handoff, origin=origin, harness=caller[1]))
    if caller[1] == "claude" and "clear=scheduled" in line.split():
        compact = False
        with contextlib.suppress(BaseException):
            import session_tidy_clear as clear
            compact = clear.background_agents_running("claude", caller[2], caller[0]) \
                and clear.mark_compact(caller[0].key, "claude", caller[2])
        if compact:
            print("say=" + _ui_translator()("tidy.say.compact_for_agents"))
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


def accept_reply_card(harness: str, sid: str, cwd: str, reply: str, *,
                      seat: Optional[Seat] = None) -> str:
    """At turn end, an automatic idle/compact tidy request answered with a complete marked card
    becomes that card and the same enqueue ``tidy`` runs; anything else changes nothing.
    ``seat`` is the exact seat the turn-end hook proved for this session (its observer runs
    detached and cannot prove one itself)."""
    if is_worker():
        return "reply-card=none reason=worker"
    if not reply or "[[session-tidy card " not in reply:
        return "reply-card=none"
    caller = ((session_seat(seat, harness, sid), harness, sid) if seat is not None and sid
              else resolve_caller(harness, sid, cwd))
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
        current = prompt_state(seat) or {}
        if current.get("source") != "claim" or current.get("claim_id") != context.get("claim_id"):
            return "reply-card=skipped reason=request-not-eligible"
        # A late but exactly identified response still belongs in the external card.
        # Retaining it never restores a cancelled compaction's permission.
    request = (clear.read_reservation(seat.key) or {}).get("idle_request")
    if isinstance(request, dict) and request.get("state") == "accepted":
        return "reply-card=skipped reason=already-accepted"       # the session ran ``tidy`` itself
    with session_state_lock(seat):
        prepare_native_session_state(seat)
        previous = read_latest_card(seat)
        if not (previous and previous.get("origin") == "idle"
                and previous.get("prompt_seq") == read_prompt_seq(seat)):
            generation = int(previous.get("generation", 0) or 0) + 1 if previous else 1
            record_event(seat, harness, sid, "card", cwd=cwd, card_generation=generation)
            write_card(seat, harness, sid, body, cwd=cwd, prompt_seq=read_prompt_seq(seat), origin="idle")
    line, _idle = _enqueue_caller((seat, harness, sid), cwd, no_clear=False, no_continue=False)
    return "reply-card=accepted " + line


def cmd_handoff(args) -> int:
    if is_worker():
        print("prompted=false reason=worker")
        return 1
    source = getattr(args, "source", None)
    if source is not None:
        if args.harness or args.session_id:
            raise StateError("handoff-source-with-caller")
        if args.transfer_ref:
            raise StateError("handoff-source-with-transfer-ref")
        if args.kind is not None and args.target is not None:
            raise StateError("handoff-kind-with-target")
        if args.target == "accept":
            raise StateError("handoff-source-with-accept")
        if args.kind is None and args.target is None:
            raise StateError("handoff-target-required")
        import session_tidy_runner as runner
        line, code = runner.supervised_handoff(
            source, kind=args.kind, target=args.target, name=args.name)
        print(line)
        return code
    if args.kind is not None:
        raise StateError("handoff-kind-requires-source")
    if args.target is None:
        raise StateError("handoff-target-required")
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
        line, code = runner.handoff(caller[0], args.target, caller=caller)
    print(line)
    return code


def cmd_hook(args) -> int:
    try:
        emitted = []
        human = []
        output_format = getattr(args, "format", "text")
        # A JSON caller renders the text into its own final output, so the
        # helper's own print is not the delivery boundary: hand the receipt
        # writes back and let that caller apply them after its output returned.
        deferred = [] if output_format == "json" else None
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
        startup_context = {}
        result = run_hook(args.harness, args.event, sid, source=args.source or "", transcript=args.transcript or "",
                          cwd=args.cwd or os.getcwd(), reread=args.reread,
                          emit=(emitted.append if output_format == "json" else _emit_stdout),
                          emit_human=human.append,
                          human_only=getattr(args, "human_only", False),
                          startup_context=startup_context,
                          phase=args.phase, compact_summary=summary, compact_context=compact_context,
                          prompt_text=prompt_text, deferred=deferred)
        if output_format == "json":
            payload = {"additionalContext": "\n".join(emitted),
                       "humanNotice": "\n".join(human)}
            if startup_context:
                payload["initialPrompt"] = bool(startup_context.get("due"))
            if deferred is not None:
                payload["deferredConsumed"] = deferred
                deferred = None
            print(json.dumps(payload, ensure_ascii=False))
        if deferred:
            apply_deferred(deferred)
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


def cmd_apply_deferred(_args) -> int:
    """Apply the receipt entries a JSON hook caller carried in its own output.

    The caller pipes the ``deferredConsumed`` value back only after it emitted,
    so a failed or lost render commits nothing. A malformed payload applies
    nothing; the call is then a no-op rather than an error.
    """
    try:
        value = json.load(sys.stdin)
    except (EOFError, ValueError, OSError):
        value = []
    entries = value.get("deferredConsumed") if isinstance(value, dict) else value
    applied = apply_deferred(entries if isinstance(entries, list) else [])
    print(json.dumps({"applied": applied}))
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
    apply_deferred_parser = sub.add_parser(
        "_apply-deferred", help="apply receipt entries a JSON hook caller deferred")
    apply_deferred_parser.set_defaults(func=cmd_apply_deferred)
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
    handoff.add_argument("target", nargs="?")
    handoff.add_argument("transfer_ref", nargs="?")
    handoff.add_argument("--from", dest="source", metavar="SESSION",
                         help="observed live session to transfer from (supervisor form)")
    handoff.add_argument("--kind", choices=HARNESSES,
                         help="start a successor of this harness beside --from")
    handoff.add_argument("--name", help="successor name for --kind (default <kind>-<sid8>)")
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
