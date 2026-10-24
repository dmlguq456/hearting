"""Conservative normalization for Herdr 0.9.x pane observations.

Herdr remains optional. A failed command is unknown; only a successful,
well-shaped pane census can confirm absence. Pane events invalidate the
endpoint view and wake a fresh snapshot; their unsequenced deltas are not state.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Callable


HERDR_API_PROTOCOL = 22
_API_LINE_LIMIT = 16 * 1024 * 1024
_SNAPSHOT_OVERLAP_ATTEMPTS = 2
_EVENT_DRAIN_FRAME_LIMIT = 256
# Protocol 22 permits these endpoint-wide pane subscriptions. The
# pane.agent_status_changed variant also requires an exact pane_id and must
# only be added after a real pane has appeared in a snapshot.
_PANE_SUBSCRIPTIONS = (
    {"type": "pane.created"}, {"type": "pane.updated"},
    {"type": "pane.closed"}, {"type": "pane.exited"},
    {"type": "pane.agent_detected"},
)


def endpoint_name(session: str | None = None) -> str:
    value = session if session is not None else os.environ.get("AGENT_HERDR_SESSION")
    return value.strip() if isinstance(value, str) and value.strip() else "default"


@dataclass(frozen=True)
class PaneCensus:
    endpoint: str
    panes: tuple[dict, ...]
    observed_at: float
    complete: bool = True


def pane_census(*, session: str | None = None,
                runner: Callable = subprocess.run,
                which: Callable = shutil.which) -> PaneCensus | None:
    """Read one complete ``herdr pane list`` result; return None on uncertainty."""
    if which("herdr") is None:
        return None
    server = endpoint_name(session)
    argv = ["herdr"]
    if server != "default":
        argv.extend(("--session", server))
    argv.extend(("pane", "list"))
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=2.0)
        if getattr(proc, "returncode", 1) != 0:
            return None
        payload = json.loads(proc.stdout or "")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = result.get("panes") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return None
    panes = []
    seen = set()
    for row in rows:
        pane_id = row.get("pane_id") if isinstance(row, dict) else None
        if not isinstance(pane_id, str) or not pane_id or pane_id in seen:
            return None
        seen.add(pane_id)
        panes.append(dict(row))
    return PaneCensus(server, tuple(panes), time.monotonic())


def confirms_absence(census: PaneCensus | None, pane_id: str,
                     *, endpoint: str | None = None) -> bool:
    """A missing pane is terminal only in a complete census of its endpoint."""
    if (census is None or census.complete is not True
            or not isinstance(pane_id, str) or not pane_id
            or (endpoint is not None and census.endpoint != endpoint_name(endpoint))):
        return False
    return all(row.get("pane_id") != pane_id for row in census.panes)


@dataclass
class ObservationSnapshot:
    """A small event reducer; reconnect starts a new, untrusted generation."""

    endpoint: str
    generation: int = 0
    complete: bool = False
    panes: dict[str, dict] = field(default_factory=dict)
    terminal: dict[str, str] = field(default_factory=dict)
    revision: int = 0
    pane_revisions: dict[str, int] = field(default_factory=dict)

    def reconnect(self) -> None:
        self.generation += 1
        self.complete = False
        self.panes.clear()
        self.terminal.clear()
        self.pane_revisions.clear()

    def replace_snapshot(self, endpoint: str, panes) -> bool:
        if endpoint_name(endpoint) != self.endpoint or not isinstance(panes, list):
            self.reconnect()
            return False
        replacement = {}
        for pane in panes:
            pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
            if not isinstance(pane_id, str) or not pane_id or pane_id in replacement:
                self.reconnect()
                return False
            replacement[pane_id] = dict(pane)
        self.generation += 1
        self.panes = replacement
        self.terminal = {}
        self.pane_revisions = {pane_id: 0 for pane_id in replacement}
        self.revision += 1
        self.complete = True
        return True

    def apply_event(self, endpoint: str, envelope: dict) -> bool:
        """Invalidate this view when a valid pane event arrives.

        Protocol 22 exposes no sequence shared with ``session.snapshot``. The
        event is therefore only evidence that a new census is needed; its pane
        delta must never be applied to the current snapshot.
        """
        if endpoint_name(endpoint) != self.endpoint:
            return False
        if not isinstance(envelope, dict):
            self.reconnect()
            return False
        if "error" in envelope:
            self.reconnect()
            return False
        data = envelope.get("data")
        event = envelope.get("event")
        if not isinstance(data, dict):
            self.reconnect()
            return False
        event = event or data.get("type")
        pane = data.get("pane")
        pane_id = data.get("pane_id")
        if pane_id is None and isinstance(pane, dict):
            pane_id = pane.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            self.reconnect()
            return False
        if event in {"pane_created", "pane.created", "pane_updated", "pane.updated"}:
            if not isinstance(pane, dict) or pane.get("pane_id") != pane_id:
                self.reconnect()
                return False
        elif event in {"pane_agent_status_changed", "pane.agent_status_changed"}:
            status = data.get("agent_status")
            if status not in {
                    None, "idle", "working", "blocked", "done", "unknown"}:
                self.reconnect()
                return False
        elif event not in {
                "pane_closed", "pane.closed", "pane_exited", "pane.exited",
                "pane_agent_detected", "pane.agent_detected"}:
            self.reconnect()
            return False
        self.reconnect()
        return True

    def confirms_absence(self, pane_id: str) -> bool:
        return self.complete and pane_id not in self.panes


@dataclass(frozen=True)
class ObservationView:
    endpoint: str
    socket_path: str
    generation: int
    revision: int
    panes: tuple[dict, ...]
    pane_revisions: dict[str, int]
    terminal: dict[str, str]
    agents: tuple[dict, ...]
    observed_at: float
    complete: bool = True


def socket_path_for_endpoint(session: str | None = None, environ=None) -> str | None:
    """Return only an explicitly inherited endpoint socket; never guess paths."""
    env = os.environ if environ is None else environ
    value = env.get("HERDR_SOCKET_PATH")
    if not isinstance(value, str) or not value.strip():
        return None
    requested = endpoint_name(session)
    # AGENT_HERDR_SESSION selects a herdr CLI target.  The inherited socket's
    # owner is the actual HERDR_SESSION, with an unset value belonging to default.
    socket_owner = endpoint_name(env.get("HERDR_SESSION") or "default")
    if socket_owner != requested:
        return None
    return value.strip()


def _api_id():
    return "hearting-observation:" + uuid.uuid4().hex


def _receive_line(sock, buffer: bytearray, deadline: float) -> bytes:
    while True:
        marker = buffer.find(b"\n")
        if marker >= 0:
            line = bytes(buffer[:marker]).rstrip(b"\r")
            del buffer[:marker + 1]
            if not line or len(line) > _API_LINE_LIMIT:
                raise ValueError("herdr-api-empty-or-oversized-line")
            return line
        if len(buffer) > _API_LINE_LIMIT:
            raise ValueError("herdr-api-line-too-large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("herdr-api-timeout")
        readable, _, _ = select.select([sock], [], [], remaining)
        if not readable:
            raise TimeoutError("herdr-api-timeout")
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("herdr-api-disconnected")
        buffer.extend(chunk)


def _decode_line(line: bytes):
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("herdr-api-malformed-json") from exc
    if not isinstance(value, dict):
        raise ValueError("herdr-api-malformed-envelope")
    return value


def _request_once(socket_path: str, method: str, params: dict, *, timeout: float,
                  connector=None):
    request_id = _api_id()
    request = {"id": request_id, "method": method, "params": params}
    client = (connector or socket.socket)(socket.AF_UNIX, socket.SOCK_STREAM)
    buffer = bytearray()
    try:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
        response = _decode_line(_receive_line(client, buffer, time.monotonic() + timeout))
        if response.get("id") != request_id:
            raise ValueError("herdr-api-response-id-mismatch")
        return response
    finally:
        client.close()


class ObservationTransport:
    """One endpoint-local subscribe-before-snapshot connection.

    The server has no public event sequence or replay guarantee. Events only
    invalidate a candidate/current view. A snapshot is published only when no
    event overlaps its acquisition; bounded overlap or transport uncertainty
    leaves the endpoint unknown.
    """

    def __init__(self, endpoint: str | None = None, socket_path: str | None = None,
                 *, timeout: float = 0.35, connector=None):
        self.endpoint = endpoint_name(endpoint)
        self.socket_path = socket_path or socket_path_for_endpoint(endpoint)
        self.timeout = max(0.05, min(float(timeout), 2.0))
        self._connector = connector or socket.socket
        self._socket = None
        self._buffer = bytearray()
        self._state = ObservationSnapshot(self.endpoint)
        self._status_pane_ids: set[str] = set()
        self._agents: tuple[dict, ...] = ()
        self._observed_at = 0.0
        self._lock = threading.RLock()

    def close(self):
        with self._lock:
            self._close_socket()
            self._state.reconnect()

    def fileno(self):
        """Return the live subscription descriptor for an external event selector."""
        with self._lock:
            return self._socket.fileno() if self._socket is not None else None

    def _close_socket(self):
        client, self._socket = self._socket, None
        self._buffer.clear()
        if client is not None:
            try:
                client.close()
            except OSError:
                pass

    def _subscribe(self):
        if not self.socket_path:
            return False
        client = self._connector(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(self.timeout)
            client.connect(self.socket_path)
            request_id = _api_id()
            subscriptions = list(_PANE_SUBSCRIPTIONS)
            subscriptions.extend(
                {"type": "pane.agent_status_changed", "pane_id": pane_id}
                for pane_id in sorted(self._status_pane_ids))
            request = {"id": request_id, "method": "events.subscribe",
                       "params": {"subscriptions": subscriptions}}
            client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
            response = _decode_line(_receive_line(
                client, self._buffer, time.monotonic() + self.timeout))
            if response.get("id") != request_id or "error" in response:
                raise ValueError("herdr-subscribe-rejected")
            self._socket = client
            return True
        except (OSError, ValueError, TimeoutError, ConnectionError):
            client.close()
            self._buffer.clear()
            return False

    def _read_snapshot(self):
        if not self.socket_path:
            return None
        try:
            response = _request_once(self.socket_path, "session.snapshot", {},
                                     timeout=self.timeout, connector=self._connector)
        except (OSError, ValueError, TimeoutError, ConnectionError):
            return None
        if "error" in response:
            return None
        result = response.get("result")
        snapshot = result.get("snapshot") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get("type") != "session_snapshot"
                or not isinstance(snapshot, dict)
                or snapshot.get("protocol") != HERDR_API_PROTOCOL
                or not isinstance(snapshot.get("panes"), list)
                or not isinstance(snapshot.get("agents"), list)):
            return None
        agents = tuple(dict(agent) for agent in snapshot["agents"]
                        if isinstance(agent, dict))
        return snapshot["panes"], agents

    def _drain_events(self):
        if self._socket is None:
            return False, False
        saw_event = False
        frames = 0
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                if frames >= _EVENT_DRAIN_FRAME_LIMIT or time.monotonic() >= deadline:
                    self._state.reconnect()
                    return False, saw_event
                marker = self._buffer.find(b"\n")
                if marker < 0:
                    if self._buffer:
                        # A partial frame may complete after the candidate
                        # snapshot. Keep that candidate out of the published
                        # view until the frame is resolved.
                        saw_event = True
                    readable, _, _ = select.select([self._socket], [], [], 0)
                    if not readable:
                        return True, saw_event
                    chunk = self._socket.recv(65536)
                    if not chunk:
                        raise ConnectionError("herdr-api-disconnected")
                    self._buffer.extend(chunk)
                    if len(self._buffer) > _API_LINE_LIMIT:
                        raise ValueError("herdr-api-line-too-large")
                    continue
                line = bytes(self._buffer[:marker]).rstrip(b"\r")
                del self._buffer[:marker + 1]
                envelope = _decode_line(line)
                if "error" in envelope:
                    # Includes `error.code == events_lost`; no event enum or
                    # sequence number is inferred from that server error.
                    self._state.reconnect()
                    return False, saw_event
                if not self._state.apply_event(self.endpoint, envelope):
                    self._state.reconnect()
                    return False, saw_event
                saw_event = True
                frames += 1
        except (OSError, ValueError, TimeoutError, ConnectionError):
            self._state.reconnect()
            return False, saw_event

    def _consistent_snapshot(self):
        """Publish a snapshot only after a quiet, bounded acquisition window."""
        if self._socket is None:
            self._state.reconnect()
            return False
        for _attempt in range(_SNAPSHOT_OVERLAP_ATTEMPTS):
            healthy, _prior_events = self._drain_events()
            if not healthy:
                self._close_socket()
                self._state.reconnect()
                return False
            candidate = self._read_snapshot()
            if candidate is None:
                self._close_socket()
                self._state.reconnect()
                return False
            panes, agents = candidate
            status_pane_ids = {
                pane["pane_id"] for pane in panes
                if (isinstance(pane, dict) and pane.get("agent")
                    and isinstance(pane.get("pane_id"), str)
                    and pane["pane_id"])
            }
            if status_pane_ids != self._status_pane_ids:
                # Protocol 22 requires an exact pane_id for status changes.
                # Bind only IDs present on this snapshot, then discard it and
                # take a new snapshot under the complete subscription set.
                self._status_pane_ids = status_pane_ids
                self._close_socket()
                self._state.reconnect()
                if not self._subscribe():
                    self._close_socket()
                    self._state.reconnect()
                    return False
                continue
            healthy, overlapped = self._drain_events()
            if not healthy:
                self._close_socket()
                self._state.reconnect()
                return False
            if overlapped:
                self._state.reconnect()
                continue
            if not self._state.replace_snapshot(self.endpoint, panes):
                self._close_socket()
                return False
            self._agents = agents
            self._observed_at = time.monotonic()
            return True
        self._close_socket()
        self._state.reconnect()
        return False

    def _synchronize(self):
        # One initial attempt plus one recovery attempt. A broken endpoint never
        # traps Fleet's basic process/resource scan in reconnect backoff.
        for _attempt in range(2):
            self._close_socket()
            self._state.reconnect()
            if not self._subscribe():
                continue
            if self._consistent_snapshot():
                return True
            else:
                self._close_socket()
        self._close_socket()
        self._state.reconnect()
        return False

    def observe(self) -> ObservationView | None:
        with self._lock:
            if self._socket is None:
                if not self._synchronize():
                    return None
            elif not self._state.complete:
                if not self._consistent_snapshot():
                    return None
            else:
                healthy, saw_event = self._drain_events()
                if not healthy:
                    if not self._synchronize():
                        return None
                elif saw_event and not self._consistent_snapshot():
                    return None
            if not self._state.complete:
                return None
            return self._view()

    def resnapshot(self) -> ObservationView | None:
        """Take a fresh snapshot on the live subscription with overlap checks."""
        with self._lock:
            if self._socket is None:
                return self.observe()
            if not self._consistent_snapshot():
                return None
            return self._view()

    def _view(self) -> ObservationView:
        return ObservationView(
            endpoint=self.endpoint, socket_path=self.socket_path or "",
            generation=self._state.generation,
            revision=self._state.revision,
            panes=tuple(dict(row) for row in self._state.panes.values()),
            pane_revisions=dict(self._state.pane_revisions),
            terminal=dict(self._state.terminal),
            agents=tuple(dict(row) for row in self._agents),
            observed_at=self._observed_at,
        )

    def wait_for_event(self, pane_id: str, timeout: float,
                       event_types=None) -> dict | None:
        """Wait once for an event on this exact endpoint and pane lifetime."""
        allowed = set(event_types or (
            "pane.closed", "pane.exited", "pane.updated",
            "pane.agent_detected", "pane.agent_status_changed",
            "pane_closed", "pane_exited", "pane_updated",
            "pane_agent_detected", "pane_agent_status_changed",
        ))
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            if self._socket is None or not self._state.complete:
                return None
            while True:
                try:
                    envelope = _decode_line(_receive_line(self._socket, self._buffer, deadline))
                except TimeoutError:
                    return None
                except (OSError, ValueError, ConnectionError):
                    self._close_socket()
                    self._state.reconnect()
                    return None
                data = envelope.get("data")
                event = envelope.get("event") or (
                    data.get("type") if isinstance(data, dict) else None)
                current_pane = data.get("pane_id") if isinstance(data, dict) else None
                pane = data.get("pane") if isinstance(data, dict) else None
                if current_pane is None and isinstance(pane, dict):
                    current_pane = pane.get("pane_id")
                matches = current_pane == pane_id and event in allowed
                if "error" in envelope:
                    self._close_socket()
                    self._state.reconnect()
                    return None
                if not self._state.apply_event(self.endpoint, envelope):
                    self._close_socket()
                    self._state.reconnect()
                    return None
                if matches:
                    return envelope


def _native_status_path(endpoint: str, pane_id: str, harness: str, session_id: str) -> Path:
    from dispatch_contract import stable_state_root
    key = "\0".join((endpoint_name(endpoint), pane_id, harness, session_id)).encode()
    return stable_state_root(os.environ) / "herdr-observation" / "native-turn" / (sha256(key).hexdigest() + ".json")


def _process_start_ticks(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rindex(")") + 1:].split()
        if fields[0] in {"Z", "X"} or not fields[19].isdigit():
            return None
        return fields[19]
    except (OSError, ValueError, IndexError):
        return None


def record_native_status(payload: dict, *, now: float | None = None) -> bool:
    """Persist one exact native OpenCode turn event for bounded retire reads."""
    if not isinstance(payload, dict):
        return False
    endpoint = endpoint_name(payload.get("endpoint"))
    pane = payload.get("pane")
    harness = payload.get("harness")
    session_id = payload.get("session_id")
    pid = payload.get("pid")
    pid_start = payload.get("pid_start")
    status = payload.get("status")
    if (harness != "opencode" or not all(isinstance(value, str) and value and value != "-"
                                         for value in (pane, session_id))
            or type(pid) is not int or pid <= 1
            or not isinstance(pid_start, str) or not pid_start.isdigit()
            or status not in {"idle", "working", "blocked", "unknown"}
            or _process_start_ticks(pid) != pid_start):
        return False
    try:
        namespace = os.readlink(f"/proc/{pid}/ns/pid")
        if namespace != os.readlink("/proc/self/ns/pid"):
            return False
    except OSError:
        return False
    observed_at = payload.get("observed_at", time.time() if now is None else now)
    if type(observed_at) not in (float, int) or not (0 < observed_at < float("inf")):
        return False
    row = {"schema_version": 1, "endpoint": endpoint, "pane": pane,
           "harness": harness, "session_id": session_id, "pid": pid,
           "pid_start": pid_start, "pid_namespace": namespace,
           "status": status, "observed_at": float(observed_at)}
    path = _native_status_path(endpoint, pane, harness, session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".native-turn-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(row, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return True
    except OSError:
        return False
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def read_native_status(endpoint: str, pane: str, harness: str, session_id: str,
                       pid: int, pid_start: str, *, max_age: float = 15.0,
                       now: float | None = None) -> dict | None:
    """Return a fresh native event only for its exact pane, SID and PID birth."""
    try:
        path = _native_status_path(endpoint, pane, harness, session_id)
        row = json.loads(path.read_text(encoding="utf-8"))
        current = time.time() if now is None else now
        if (not isinstance(row, dict) or row.get("schema_version") != 1
                or any(row.get(key) != value for key, value in (
                    ("endpoint", endpoint_name(endpoint)), ("pane", pane),
                    ("harness", harness), ("session_id", session_id),
                    ("pid", pid), ("pid_start", pid_start)))
                or row.get("status") not in {"idle", "working", "blocked", "unknown"}
                or type(row.get("observed_at")) not in (int, float)
                or current - row["observed_at"] < 0
                or current - row["observed_at"] > max_age
                or _process_start_ticks(pid) != pid_start
                or row.get("pid_namespace") != os.readlink(f"/proc/{pid}/ns/pid")
                or row.get("pid_namespace") != os.readlink("/proc/self/ns/pid")):
            return None
        return row
    except (OSError, ValueError, TypeError):
        return None


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["record-native-turn"]:
        return 2
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 1
    return 0 if record_native_status(payload) else 1


if __name__ == "__main__":
    raise SystemExit(main())
