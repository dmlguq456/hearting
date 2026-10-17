#!/usr/bin/env python3
"""At-least-once completion delivery through Codex's native per-thread queue.

The app-server queue API is used directly so Hearting can supply a stable
``clientUserMessageId`` and inspect only its own pending item. One completion
observation pass may reuse a bounded queue-only connection. The interactive
TUI remains the only subscriber and approval owner; no events are subscribed
to and no user-owned queue item is edited, deleted, or reordered.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import stat
import struct
import time
from typing import Any


MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_QUEUE_PAGES = 16
MAX_PAGE_SIZE = 100


class QueueDeliveryError(RuntimeError):
    """A native queue operation failed; ``ambiguous`` marks a sent request."""

    def __init__(self, reason: str, *, ambiguous: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.ambiguous = ambiguous


def resolve_endpoint(path: Path) -> Path:
    """Accept Codex 0.157's owned short-path daemon socket symlink."""
    if not path.is_absolute():
        raise QueueDeliveryError("queue-endpoint-path-unsafe")
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            target = Path(os.readlink(path))
            if info.st_uid != os.geteuid() or not target.is_absolute() or target.is_symlink():
                raise QueueDeliveryError("queue-endpoint-path-unsafe")
            parent = target.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
                raise QueueDeliveryError("queue-endpoint-path-unsafe")
            path = target
        return path
    except OSError as exc:
        raise QueueDeliveryError("queue-endpoint-unavailable") from exc


class _WebSocket:
    """Small synchronous WebSocket client for Codex's local UDS control API."""

    def __init__(self, path: Path, *, timeout: float):
        path = resolve_endpoint(path)
        try:
            info = path.lstat()
        except OSError as exc:
            raise QueueDeliveryError("queue-endpoint-unavailable") from exc
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise QueueDeliveryError("queue-endpoint-permissions-unsafe")
        self.path = path
        self.timeout = timeout
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        try:
            self.socket.connect(str(path))
            self._upgrade()
        except (OSError, QueueDeliveryError) as exc:
            self.socket.close()
            if isinstance(exc, QueueDeliveryError):
                raise
            raise QueueDeliveryError("queue-endpoint-unavailable") from exc
        self.next_id = 1
        self.request_sent = False

    def close(self) -> None:
        self.socket.close()

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self.buffer:
            if len(self.buffer) > limit:
                raise QueueDeliveryError("queue-websocket-header-oversized")
            chunk = self.socket.recv(4096)
            if not chunk:
                raise QueueDeliveryError("queue-websocket-disconnected")
            self.buffer.extend(chunk)
        end = self.buffer.index(marker) + len(marker)
        value = bytes(self.buffer[:end])
        del self.buffer[:end]
        return value

    def _upgrade(self) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET /rpc HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        # The buffer is initialized lazily because the handshake precedes the
        # rest of the instance state.
        self.buffer = bytearray()
        response = self._read_until(b"\r\n\r\n", 16 * 1024).decode("latin-1")
        lines = response.split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            raise QueueDeliveryError("queue-websocket-upgrade-refused")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise QueueDeliveryError("queue-websocket-handshake-invalid")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        size = len(payload)
        if size > MAX_FRAME_BYTES:
            raise QueueDeliveryError("queue-websocket-message-oversized")
        first = bytes((0x80 | opcode,))
        if size < 126:
            header = first + bytes((0x80 | size,))
        elif size <= 0xFFFF:
            header = first + bytes((0x80 | 126,)) + struct.pack("!H", size)
        else:
            header = first + bytes((0x80 | 127,)) + struct.pack("!Q", size)
        masked = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def _recv_exact(self, size: int, *, deadline: float | None = None) -> bytes:
        while len(self.buffer) < size:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QueueDeliveryError("queue-websocket-timeout",
                                             ambiguous=getattr(self, "request_sent", False))
                self.socket.settimeout(remaining)
            chunk = self.socket.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise QueueDeliveryError("queue-websocket-disconnected",
                                         ambiguous=getattr(self, "request_sent", False))
            self.buffer.extend(chunk)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def _recv_message(self, *, deadline: float | None = None) -> tuple[int, bytes]:
        parts = bytearray()
        initial_opcode = None
        while True:
            first, second = self._recv_exact(2, deadline=deadline)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._recv_exact(2, deadline=deadline))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._recv_exact(8, deadline=deadline))[0]
            if size > MAX_FRAME_BYTES:
                raise QueueDeliveryError("queue-websocket-message-oversized")
            mask = self._recv_exact(4, deadline=deadline) if masked else b""
            data = self._recv_exact(size, deadline=deadline)
            if masked:
                data = bytes(byte ^ mask[index & 3] for index, byte in enumerate(data))
            if opcode == 0x8:
                raise QueueDeliveryError("queue-websocket-closed",
                                         ambiguous=getattr(self, "request_sent", False))
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                initial_opcode = opcode
            elif opcode != 0x0 or initial_opcode is None:
                raise QueueDeliveryError("queue-websocket-frame-invalid",
                                         ambiguous=getattr(self, "request_sent", False))
            parts.extend(data)
            if len(parts) > MAX_FRAME_BYTES:
                raise QueueDeliveryError("queue-websocket-message-oversized")
            if final:
                return initial_opcode or opcode, bytes(parts)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_sent = False
        deadline = time.monotonic() + self.timeout
        request_id = self.next_id
        self.next_id += 1
        self.request_sent = True  # A partial send also has an ambiguous outcome.
        self._send_frame(0x1, json.dumps({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }, separators=(",", ":")).encode("utf-8"))
        self.request_sent = True
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QueueDeliveryError("queue-websocket-timeout", ambiguous=self.request_sent)
            try:
                opcode, raw = self._recv_message(deadline=deadline)
            except QueueDeliveryError as exc:
                if (not exc.ambiguous and method in {"thread/queue/add", "thread/queue/start"}
                        and exc.reason in {"queue-websocket-message-oversized",
                                           "queue-websocket-frame-invalid"}):
                    raise QueueDeliveryError(exc.reason, ambiguous=True) from exc
                raise
            except (OSError, TimeoutError) as exc:
                raise QueueDeliveryError("queue-websocket-timeout", ambiguous=self.request_sent) from exc
            if opcode != 0x1:
                continue
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, ValueError) as exc:
                raise QueueDeliveryError("queue-websocket-json-invalid", ambiguous=self.request_sent) from exc
            if not isinstance(value, dict):
                continue
            # A server request is never an RPC response, even when the numeric
            # id collides. This queue-only client does not answer server requests.
            if isinstance(value.get("method"), str):
                continue
            if value.get("id") != request_id:
                # Notifications and unrelated responses do not affect delivery.
                continue
            if "error" in value:
                error = value["error"]
                code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message") if isinstance(error, dict) else None
                safe = str(message or "request-failed").replace("\n", " ")[:160]
                raise QueueDeliveryError(f"queue-rpc-refused:{method}:{code}:{safe}", ambiguous=False)
            result = value.get("result")
            if not isinstance(result, dict):
                raise QueueDeliveryError(f"queue-rpc-result-invalid:{method}", ambiguous=self.request_sent)
            return result

    def initialize(self) -> None:
        result = self.request("initialize", {
            "clientInfo": {"name": "hearting-native-queue", "title": "Hearting Queue", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        if not isinstance(result, dict):
            raise QueueDeliveryError("queue-initialize-result-invalid")
        self._send_frame(0x1, json.dumps({"jsonrpc": "2.0", "method": "initialized"},
                                         separators=(",", ":")).encode("utf-8"))


def _connect(path: Path, timeout: float) -> _WebSocket:
    client = _WebSocket(path, timeout=timeout)
    try:
        client.initialize()
        return client
    except Exception:
        client.close()
        raise


class DeliverySession:
    """Reuse one queue-only WebSocket for a bounded completion observation pass."""

    def __init__(self, path: Path, *, timeout: float = 5.0):
        self.path: Path | None = None
        self.timeout = timeout
        self.client: _WebSocket | None = None

    def __enter__(self) -> "DeliverySession":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        client = self.client
        self.client = None
        if client is not None:
            try:
                client.close()
            except OSError:
                pass

    def request(self, path: Path, method: str, params: dict[str, Any], *,
                timeout: float | None = None) -> dict[str, Any]:
        timeout = self.timeout if timeout is None else timeout
        try:
            endpoint = resolve_endpoint(path)
        except QueueDeliveryError:
            self.close()
            raise
        if self.path is None:
            self.path = endpoint
        elif endpoint != self.path:
            self.close()
            # Rebind only on the next RPC, after the caller has observed this
            # endpoint change and restarted its exact-state check.
            self.path = endpoint
            raise QueueDeliveryError("queue-session-endpoint-changed")
        if self.client is None:
            self.client = _connect(endpoint, timeout)
        client = self.client
        client.timeout = timeout
        client.socket.settimeout(timeout)
        try:
            return client.request(method, params)
        except QueueDeliveryError as exc:
            if not exc.reason.startswith("queue-rpc-refused:"):
                self.close()
            raise
        except (OSError, TimeoutError) as exc:
            ambiguous = client.request_sent
            self.close()
            raise QueueDeliveryError(f"queue-rpc-transport-failed:{method}",
                                     ambiguous=ambiguous) from exc


def _session_kwargs(session):
    return {"session": session} if session is not None else {}


def _rpc(path: Path, method: str, params: dict[str, Any], *, timeout: float,
         session: DeliverySession | None = None) -> dict[str, Any]:
    if session is not None:
        return session.request(path, method, params, timeout=timeout)
    client = _connect(path, timeout)
    try:
        return client.request(method, params)
    except QueueDeliveryError as exc:
        if exc.ambiguous:
            raise
        raise
    except (OSError, TimeoutError) as exc:
        raise QueueDeliveryError(f"queue-rpc-transport-failed:{method}",
                                 ambiguous=client.request_sent) from exc
    finally:
        client.close()


def list_queue(path: Path, thread_id: str, *, timeout: float = 5.0,
               session: DeliverySession | None = None) -> list[dict[str, Any]]:
    if not thread_id:
        raise QueueDeliveryError("queue-thread-id-missing")
    cursor = None
    values: list[dict[str, Any]] = []
    for _ in range(MAX_QUEUE_PAGES):
        params: dict[str, Any] = {"threadId": thread_id, "limit": MAX_PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        response = _rpc(path, "thread/queue/list", params, timeout=timeout,
                        **_session_kwargs(session))
        page = response.get("data")
        if not isinstance(page, list):
            raise QueueDeliveryError("queue-list-response-invalid")
        values.extend(item for item in page if isinstance(item, dict))
        cursor = response.get("nextCursor")
        if not cursor:
            return values
    raise QueueDeliveryError("queue-list-page-limit-exceeded")


def _latest_turn(path: Path, thread_id: str, *, timeout: float,
                 session: DeliverySession | None = None) -> dict[str, Any] | None:
    result = _rpc(path, "thread/turns/list", {
        "threadId": thread_id, "limit": 1, "sortDirection": "desc",
    }, timeout=timeout, **_session_kwargs(session))
    turns = result.get("data")
    if not isinstance(turns, list) or not turns:
        return None
    return turns[0] if isinstance(turns[0], dict) else None


def _latest_turn_status(path: Path, thread_id: str, *, timeout: float,
                        session: DeliverySession | None = None) -> str | None:
    turn = _latest_turn(path, thread_id, timeout=timeout, **_session_kwargs(session))
    status = turn.get("status") if turn else None
    return status if isinstance(status, str) else None


def find_turn_by_client_message_id(
    path: Path, thread_id: str, client_message_id: str, *, timeout: float = 5.0,
    strict_history: bool = False, session: DeliverySession | None = None,
) -> dict[str, Any] | None:
    """Find the turn that consumed an exact queued client message id."""
    if strict_history:
        observation = _observe_peer_message(path, thread_id, client_message_id,
                                            timeout=timeout)
        if observation.status == "conflict":
            raise QueueDeliveryError("queue-peer-history-conflict")
        return observation.turn if observation.status == "matched" else None
    cursor = None
    for _ in range(MAX_QUEUE_PAGES):
        params: dict[str, Any] = {
            "threadId": thread_id, "limit": MAX_PAGE_SIZE,
            "sortDirection": "desc", "itemsView": "full",
        }
        if cursor:
            params["cursor"] = cursor
        response = _rpc(path, "thread/turns/list", params, timeout=timeout,
                        **_session_kwargs(session))
        turns = response.get("data")
        if not isinstance(turns, list):
            raise QueueDeliveryError("queue-turns-response-invalid")
        for turn in turns:
            items = turn.get("items") if isinstance(turn, dict) else None
            if isinstance(items, list) and any(
                isinstance(item, dict) and item.get("clientId") == client_message_id
                for item in items
            ):
                return turn
        cursor = response.get("nextCursor")
        if not cursor:
            return None
    # Completion delivery keeps its existing bounded at-least-once behavior.
    return None


class _PeerObservation:
    __slots__ = ("status", "turn", "reason")

    def __init__(self, status: str, turn: dict[str, Any] | None = None,
                 reason: str | None = None):
        self.status = status
        self.turn = turn
        self.reason = reason


def _native_file(path: Path, *, directory: bool = False) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return (kind_ok and info.st_uid == os.geteuid() and not info.st_mode & 0o022)


def _native_rollout_file(path: Path, *, trusted_home: Path) -> bool:
    """Validate a regular owned rollout under the already trusted native home."""
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o002):
            return False
        path.resolve(strict=True).relative_to(trusted_home.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _observe_peer_message(path, thread_id, client_message_id, *, timeout,
                          expected_message=None):
    """Observe an exact userMessage in the native projection without writing it."""
    deadline = time.monotonic() + max(0.0, timeout)
    state_db = history_db = None

    def unknown(reason):
        return _PeerObservation("unknown", reason=reason)

    def open_readonly(db_path):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True,
                                     timeout=min(0.05, remaining))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline), 200)
        connection.execute("BEGIN")
        return connection

    try:
        endpoint = Path(path)
        if (not endpoint.is_absolute() or endpoint.name != "app-server-control.sock"
                or endpoint.parent.name != "app-server-control"):
            return unknown("native-home-unbound")
        home = endpoint.parent.parent
        control_dir = endpoint.parent
        if not _native_file(home, directory=True) or not _native_file(control_dir, directory=True):
            return unknown("native-home-unsafe")

        state_path = home / "state_5.sqlite"
        history_path = home / "thread_history_1.sqlite"
        if not _native_file(state_path) or not _native_file(history_path):
            return unknown("native-history-unavailable")

        state_db = open_readonly(state_path)
        native = state_db.execute(
            "SELECT rollout_path, history_mode FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if (native is None or native["history_mode"] != "paginated"
                or not isinstance(native["rollout_path"], str)):
            return unknown("native-thread-unbound")

        rollout = Path(native["rollout_path"])
        if (not rollout.is_absolute()
                or not _native_rollout_file(rollout, trusted_home=home)):
            return unknown("native-rollout-unbound")
        try:
            rollout_size = rollout.stat().st_size
        except (OSError, ValueError):
            return unknown("native-rollout-unbound")

        history_db = open_readonly(history_path)
        projection = history_db.execute(
            "SELECT next_rollout_byte_offset, next_rollout_ordinal "
            "FROM thread_history_projection_state WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if projection is None:
            return unknown("native-projection-missing")
        projected_offset = projection["next_rollout_byte_offset"]
        projected_ordinal = projection["next_rollout_ordinal"]
        if (type(projected_offset) is not int or type(projected_ordinal) is not int
                or projected_offset < 0 or projected_ordinal < 0):
            return unknown("native-projection-invalid")

        item_state = history_db.execute(
            "SELECT COUNT(*) AS total, COUNT(rollout_ordinal) AS ordered, "
            "MAX(rollout_ordinal) AS maximum FROM thread_items WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if (item_state["total"] != item_state["ordered"]
                or (item_state["maximum"] is not None
                    and item_state["maximum"] >= projected_ordinal)):
            return unknown("native-projection-incomplete")
        projection_complete = projected_offset == rollout_size

        rows = history_db.execute(
            "SELECT ti.turn_id, ti.item_id, ti.item_type, ti.item_json, "
            "tt.turn_id AS confirmed_turn_id "
            "FROM thread_items AS ti "
            "LEFT JOIN thread_turns AS tt "
            "ON tt.thread_id = ti.thread_id AND tt.turn_id = ti.turn_id "
            "WHERE ti.thread_id = ? AND ti.item_type = 'userMessage' "
            "AND json_extract(ti.item_json, '$.clientId') = ? "
            "ORDER BY ti.rollout_ordinal LIMIT 2",
            (thread_id, client_message_id),
        ).fetchall()
        if len(rows) > 1:
            return _PeerObservation("conflict", reason="duplicate-client-id")
        if rows:
            row = rows[0]
            try:
                item = json.loads(row["item_json"])
            except (TypeError, json.JSONDecodeError):
                return unknown("candidate-json-invalid")
            turn_id = row["turn_id"]
            if (not isinstance(item, dict) or not isinstance(turn_id, str) or not turn_id
                    or row["confirmed_turn_id"] != turn_id
                    or item.get("clientId") != client_message_id
                    or row["item_type"] != "userMessage"
                    or item.get("type") != "userMessage"):
                return _PeerObservation("conflict", reason="candidate-binding-conflict")
            if (expected_message is not None
                    and not _peer_text_input(item.get("content"), expected_message)):
                return _PeerObservation("conflict", reason="candidate-content-conflict")
            if not projection_complete:
                return unknown("native-projection-incomplete")
            return _PeerObservation("matched", {"id": turn_id, "items": [item]})
        if not projection_complete:
            return unknown("native-projection-incomplete")
        return _PeerObservation("not_observed")
    except (OSError, sqlite3.Error, TimeoutError, TypeError, ValueError):
        return unknown("native-history-unavailable")
    finally:
        if history_db is not None:
            history_db.close()
        if state_db is not None:
            state_db.close()


def _start_interrupted_owned_item(
    path: Path, *, thread_id: str, client_message_id: str, item_id: str,
    timeout: float, session: DeliverySession | None = None,
) -> bool:
    # Never start a thread merely because the queue contains user input. This
    # code reaches queue/start only for the exact Hearting clientUserMessageId.
    queued = list_queue(path, thread_id, timeout=timeout, **_session_kwargs(session))
    pending, current_id = _pending_item(queued, client_message_id)
    if not pending or current_id != item_id:
        return False
    if _latest_turn_status(path, thread_id, timeout=timeout,
                           **_session_kwargs(session)) != "interrupted":
        return False
    params = {"threadId": thread_id, "queuedSubmissionId": item_id}
    result = _rpc(path, "thread/queue/start", params, timeout=timeout,
                  **_session_kwargs(session))
    turn = result.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
        raise QueueDeliveryError("queue-start-response-invalid", ambiguous=True)
    return True


def _pending_item(items, client_message_id):
    matches = [item for item in items
               if item.get("clientUserMessageId") == client_message_id]
    if not matches:
        return False, None
    # Matching malformed or duplicate entries still suppress retransmission.
    # Only one unambiguous item can authorize an interrupted restart.
    item_id = matches[0].get("id") if len(matches) == 1 else None
    return True, item_id if isinstance(item_id, str) and item_id else None


def _known_consumed(path, thread_id, client_message_id, *, timeout, strict_history=False,
                    session: DeliverySession | None = None, expected_message=None):
    if strict_history:
        observation = _observe_peer_message(
            path, thread_id, client_message_id, timeout=timeout,
            expected_message=expected_message,
        )
        if observation.status == "conflict":
            raise QueueDeliveryError("queue-peer-history-conflict")
        return observation.turn if observation.status == "matched" else None
    try:
        return find_turn_by_client_message_id(path, thread_id, client_message_id, timeout=timeout,
                                              **_session_kwargs(session))
    except QueueDeliveryError as exc:
        # History can be unavailable or oversized on a long coding turn.
        # That is an ambiguous result under the accepted at-least-once policy,
        # never evidence of consumption. Exact pending lookup still follows.
        return None


def _peer_text_input(content, message):
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return False
    item = content[0]
    return (item.get("type") == "text" and item.get("text") == message
            and set(item) <= {"type", "text", "text_elements"}
            and item.get("text_elements", []) == [])


def _exact_peer_content(turn, client_message_id, message):
    items = turn.get("items") if isinstance(turn, dict) else None
    matches = [item for item in items if isinstance(item, dict)
               and item.get("clientId") == client_message_id] if isinstance(items, list) else []
    return (len(matches) == 1 and matches[0].get("type") == "userMessage"
            and _peer_text_input(matches[0].get("content"), message))


def send_at_least_once(
    path: Path, *, thread_id: str, client_message_id: str, message: str,
    timeout: float = 5.0, allow_restart: bool = True,
    retry_ambiguous: bool = True, allow_add: bool = True,
    require_message_match: bool = False, before_add=None,
    session: DeliverySession | None = None,
) -> dict[str, Any]:
    """Inspect exact history and pending input before every at-least-once send."""
    if not thread_id or not client_message_id.startswith("delivery-"):
        raise QueueDeliveryError("queue-delivery-identity-invalid")
    if not isinstance(message, str) or not message.strip() or len(message.encode("utf-8")) > 16 * 1024:
        raise QueueDeliveryError("queue-message-invalid")
    for retry in range(2):
        consumed = _known_consumed(path, thread_id, client_message_id, timeout=timeout,
                                   strict_history=require_message_match,
                                   expected_message=message if require_message_match else None,
                                   **_session_kwargs(session))
        if consumed is not None:
            if require_message_match and not _exact_peer_content(consumed, client_message_id, message):
                raise QueueDeliveryError("queue-peer-history-mismatch", ambiguous=True)
            return {"status": "consumed", "queued_submission_id": None,
                    "already_pending": False, "started_after_interrupt": False}
        queued = list_queue(path, thread_id, timeout=timeout, **_session_kwargs(session))
        pending, item_id = _pending_item(queued, client_message_id)
        if pending:
            if require_message_match:
                matches = [item for item in queued if isinstance(item, dict)
                           and item.get("clientUserMessageId") == client_message_id]
                if len(matches) != 1 or not _peer_text_input(matches[0].get("input"), message):
                    raise QueueDeliveryError("queue-peer-pending-mismatch", ambiguous=True)
            started = allow_restart and bool(item_id) and _start_interrupted_owned_item(
                path, thread_id=thread_id, client_message_id=client_message_id,
                item_id=item_id, timeout=timeout, **_session_kwargs(session))
            return {"status": "queued", "queued_submission_id": item_id,
                    "already_pending": True, "started_after_interrupt": started}
        # Close the common consume-between-history-and-list race. Another
        # writer may still win after this read; accepted duplicates are benign.
        consumed = _known_consumed(path, thread_id, client_message_id, timeout=timeout,
                                   strict_history=require_message_match,
                                   expected_message=message if require_message_match else None,
                                   **_session_kwargs(session))
        if consumed is not None:
            if require_message_match and not _exact_peer_content(consumed, client_message_id, message):
                raise QueueDeliveryError("queue-peer-history-mismatch", ambiguous=True)
            return {"status": "consumed", "queued_submission_id": None,
                    "already_pending": False, "started_after_interrupt": False}
        if not allow_add:
            raise QueueDeliveryError("queue-add-previously-unconfirmed", ambiguous=True)
        interrupted = allow_restart and _latest_turn_status(
            path, thread_id, timeout=timeout, **_session_kwargs(session)) == "interrupted"
        try:
            if before_add is not None:
                before_add()
            result = _rpc(path, "thread/queue/add", {
                "threadId": thread_id, "clientUserMessageId": client_message_id,
                "input": [{"type": "text", "text": message}],
            }, timeout=timeout, **_session_kwargs(session))
        except QueueDeliveryError as exc:
            if not exc.ambiguous or retry or not retry_ambiguous:
                raise
            continue
        submission = result.get("queuedSubmission")
        item_id = submission.get("id") if isinstance(submission, dict) else None
        if not isinstance(item_id, str) or not item_id:
            item_id = None
        # Successful add is acceptance even when the server omits item identity.
        started = False
        if interrupted and item_id:
            started = _start_interrupted_owned_item(
                path, thread_id=thread_id, client_message_id=client_message_id,
                item_id=item_id, timeout=timeout, **_session_kwargs(session))
        return {"status": "queued", "queued_submission_id": item_id,
                "already_pending": False, "started_after_interrupt": started}
    raise QueueDeliveryError("queue-add-unconfirmed", ambiguous=True)


def poll_owned_item(
    path: Path, *, thread_id: str, client_message_id: str, timeout: float = 5.0,
    session: DeliverySession | None = None,
) -> dict[str, Any]:
    """Restart exactly one Hearting item; human-only/ambiguous queues stay put."""
    pending, item_id = _pending_item(
        list_queue(path, thread_id, timeout=timeout, **_session_kwargs(session)), client_message_id)
    if not pending:
        return {"pending": False, "started": False}
    started = bool(item_id) and _start_interrupted_owned_item(
        path, thread_id=thread_id, client_message_id=client_message_id,
        item_id=item_id, timeout=timeout, **_session_kwargs(session))
    return {"pending": True, "started": started, "queued_submission_id": item_id}
