#!/usr/bin/env python3
"""Opt-in single-ingress gateway for an interactive Codex App Server thread.

One remote TUI owns the only upstream App Server connection. Completion
producers use a bounded Unix control socket and never subscribe to App Server
notifications or approval requests. Manual turn/start and completion delivery
are ordered under one lock. Completion first attempts turn/steer against the
exact active turn; an explicit not-steerable rejection is serialized into one
turn/start after that manual turn becomes idle.

This is local at-most-once delivery, not a claim of native idle-claim or
server-side idempotency. If a send is interrupted before its accept response is
observed, the durable ledger leaves it sent-ambiguous and never resends it.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.fleet import interaction as fleet_interaction
from dispatch_completion_join import (  # noqa: E402
    DELIVERY_TIMING_POINTS,
    JoinContractError,
    STAGE_ADVANCE_RECEIPT_KEY,
    STAGE_ADVANCE_SCHEMA_VERSION,
    advance_delivery_timing,
    delivery_timing_fields,
    receipt_with_stage_advance,
    typed_stage_advance_block,
    validate_delivery_timing,
)


MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_CONTROL_BYTES = 16 * 1024
# SD-92 bounds the typed receipt itself at 2048 bytes.  The older 1536-byte
# implementation reserved the remaining 512 bytes of MAX_CONTEXT_BYTES for
# fixed prose, but an exact registry path repeated across up to four actionable
# commands makes those budgets independent.  Keep both bounded explicitly.
MAX_RECEIPT_BYTES = 2048
MAX_CONTEXT_BYTES = 8 * 1024
MAX_JOB_REGISTRY_BYTES = 1024
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_DELIVERIES = 4096
MAX_THREAD_LINEAGE = 16
INTERNAL_ID_PREFIX = "hearting-managed:"
ID_PATTERN = re.compile(r"^[A-Za-z0-9._:@/+\-=]{1,256}$")
FLEET_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,256}$")
ALLOWED_RECEIPT_KEYS = {
    "schema_version", "state", "parent_attempt_id", "job_registry", "children",
    "delivery_timing", "delivery_classification",
}
ALLOWED_CHILD_KEYS = {
    "attempt_id", "status", "readiness", "reason", "required_action", "harness",
    "delivery_classification",
}
ALLOWED_REASONS = {
    "registry-closed", "terminal-observed", "row-advanced",
    "terminal-failure-or-unclosed",
}
REQUIRED_ACTIONS = {
    "complete-open", "inspect-done-failure", "advance-completed",
}
AGENT_HOME = Path(__file__).resolve().parents[1]


class GatewayError(RuntimeError):
    """The gateway cannot preserve its delivery contract."""


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def request_key(value: Any) -> tuple[str, Any] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)):
        return (type(value).__name__, value)
    return None


def turn_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
    params = message.get("params")
    if isinstance(params, dict) and isinstance(params.get("turn"), dict):
        return params["turn"]
    result = message.get("result")
    if isinstance(result, dict) and isinstance(result.get("turn"), dict):
        return result["turn"]
    return None


def turn_id_from_message(message: dict[str, Any]) -> str:
    params = message.get("params")
    if isinstance(params, dict):
        if isinstance(params.get("turnId"), str):
            return params["turnId"]
        turn = params.get("turn")
        if isinstance(turn, dict) and isinstance(turn.get("id"), str):
            return turn["id"]
    result = message.get("result")
    if isinstance(result, dict):
        if isinstance(result.get("turnId"), str):
            return result["turnId"]
        turn = result.get("turn")
        if isinstance(turn, dict) and isinstance(turn.get("id"), str):
            return turn["id"]
    return ""


def thread_id_from_message(message: dict[str, Any]) -> str:
    params = message.get("params")
    if isinstance(params, dict) and isinstance(params.get("threadId"), str):
        return params["threadId"]
    result = message.get("result")
    if isinstance(result, dict):
        thread = result.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("id"), str):
            return thread["id"]
    return ""


class WebSocket:
    """Minimal RFC 6455 text transport for App Server Unix sockets."""

    def __init__(self, connection: socket.socket, *, mask_outbound: bool) -> None:
        self.connection = connection
        self.mask_outbound = mask_outbound
        self._buffer = bytearray()
        self._write_lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _read_headers(connection: socket.socket) -> tuple[bytes, bytes]:
        value = bytearray()
        while b"\r\n\r\n" not in value:
            chunk = connection.recv(4096)
            if not chunk:
                raise EOFError
            value.extend(chunk)
            if len(value) > 65536:
                raise GatewayError("websocket-upgrade-oversized")
        return bytes(value).split(b"\r\n\r\n", 1)

    @classmethod
    def accept(cls, connection: socket.socket) -> "WebSocket":
        headers, remainder = cls._read_headers(connection)
        lines = headers.decode("latin-1").split("\r\n")
        parsed: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                parsed[name.strip().lower()] = value.strip()
        key = parsed.get("sec-websocket-key", "")
        if (
            not lines
            or not lines[0].startswith("GET ")
            or parsed.get("upgrade", "").lower() != "websocket"
            or not key
        ):
            raise GatewayError("websocket-upgrade-invalid")
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        connection.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )
        websocket = cls(connection, mask_outbound=False)
        websocket._buffer.extend(remainder)
        return websocket

    @classmethod
    def connect_unix(cls, path: Path) -> "WebSocket":
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(10.0)
        try:
            connection.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            connection.sendall(
                (
                    "GET /rpc HTTP/1.1\r\n"
                    "Host: localhost\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode("ascii")
            )
            headers, remainder = cls._read_headers(connection)
            lines = headers.decode("latin-1").split("\r\n")
            if not lines or " 101 " not in f" {lines[0]} ":
                raise GatewayError("upstream-websocket-upgrade-rejected")
            parsed: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    parsed[name.strip().lower()] = value.strip()
            expected = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(
                        "ascii"
                    )
                ).digest()
            ).decode("ascii")
            if parsed.get("sec-websocket-accept") != expected:
                raise GatewayError("upstream-websocket-accept-mismatch")
            connection.settimeout(None)
            websocket = cls(connection, mask_outbound=True)
            websocket._buffer.extend(remainder)
            return websocket
        except Exception:
            connection.close()
            raise

    def _recv_exact(self, size: int) -> bytes:
        value = bytearray()
        if self._buffer:
            take = min(size, len(self._buffer))
            value.extend(self._buffer[:take])
            del self._buffer[:take]
        while len(value) < size:
            chunk = self.connection.recv(size - len(value))
            if not chunk:
                raise EOFError
            value.extend(chunk)
        return bytes(value)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._recv_exact(2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if length > MAX_FRAME_BYTES:
            raise GatewayError("websocket-frame-oversized")
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(
                byte ^ mask[index % 4] for index, byte in enumerate(payload)
            )
        return final, opcode, payload

    def _write_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise EOFError
        first = 0x80 | opcode
        length = len(payload)
        mask_bit = 0x80 if self.mask_outbound else 0
        if length < 126:
            header = bytes((first, mask_bit | length))
        elif length <= 0xFFFF:
            header = bytes((first, mask_bit | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, mask_bit | 127)) + struct.pack("!Q", length)
        if self.mask_outbound:
            mask = os.urandom(4)
            payload = bytes(
                byte ^ mask[index % 4] for index, byte in enumerate(payload)
            )
            frame = header + mask + payload
        else:
            frame = header + payload
        with self._write_lock:
            self.connection.sendall(frame)

    def read_json(self) -> dict[str, Any]:
        fragments = bytearray()
        fragment_opcode = 0
        while True:
            final, opcode, payload = self._read_frame()
            if opcode == 0x8:
                raise EOFError
            if opcode == 0x9:
                self._write_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = bytearray(payload)
                fragment_opcode = opcode
            elif opcode == 0x0 and fragment_opcode:
                fragments.extend(payload)
            else:
                raise GatewayError("websocket-unsupported-opcode")
            if not final:
                continue
            if fragment_opcode != 0x1:
                raise GatewayError("websocket-nontext-message")
            try:
                value = json.loads(bytes(fragments).decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise GatewayError("websocket-json-invalid") from exc
            if not isinstance(value, dict):
                raise GatewayError("websocket-json-shape-invalid")
            return value

    def write_json(self, value: dict[str, Any]) -> None:
        encoded = canonical(value).encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES:
            raise GatewayError("websocket-json-oversized")
        self._write_frame(0x1, encoded)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


class DeliveryLedger:
    """Durable gateway-owned delivery state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.value = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            if self.path.is_symlink() or self.path.stat().st_size > MAX_LEDGER_BYTES:
                raise GatewayError("ledger-file-unsafe")
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "deliveries": {}}
        except (OSError, ValueError, TypeError) as exc:
            raise GatewayError("ledger-unreadable") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or not isinstance(value.get("deliveries"), dict)
            or len(value["deliveries"]) > MAX_DELIVERIES
        ):
            raise GatewayError("ledger-schema-invalid")
        for delivery_id, row in value["deliveries"].items():
            if (
                not isinstance(delivery_id, str)
                or not isinstance(row, dict)
                or row.get("state")
                not in {"prepared", "sent", "accepted", "rejected"}
            ):
                raise GatewayError("ledger-row-invalid")
        return value

    def get(self, delivery_id: str) -> dict[str, Any] | None:
        row = self.value["deliveries"].get(delivery_id)
        return dict(row) if isinstance(row, dict) else None

    def transition(
        self,
        delivery_id: str,
        state: str,
        *,
        thread_id: str,
        parent_attempt_id: str,
        sealed_batch_id: str,
        receipt_digest: str,
        action: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        previous = self.value["deliveries"].get(delivery_id)
        prior = previous.get("state") if isinstance(previous, dict) else None
        allowed = {
            None: {"prepared"},
            "prepared": {"prepared", "sent", "rejected"},
            "sent": {"sent", "accepted", "rejected"},
            "accepted": {"accepted"},
            "rejected": {"rejected"},
        }
        if state not in allowed.get(prior, set()):
            raise GatewayError("ledger-transition-conflict")
        row = {
            "state": state,
            "thread_id": thread_id,
            "parent_attempt_id": parent_attempt_id,
            "sealed_batch_id": sealed_batch_id,
            "receipt_digest": receipt_digest,
            "action": action or (previous or {}).get("action"),
            "reason": reason,
            "updated_at_ns": time.time_ns(),
        }
        if previous:
            for key in (
                "thread_id", "parent_attempt_id",
                "sealed_batch_id", "receipt_digest",
            ):
                if previous.get(key) != row[key]:
                    raise GatewayError("ledger-identity-conflict")
        self.value["deliveries"][delivery_id] = row
        self._write()
        return dict(row)

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = (canonical(self.value) + "\n").encode("utf-8")
        if len(encoded) > MAX_LEDGER_BYTES:
            raise GatewayError("ledger-oversized")
        descriptor, raw = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(raw)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


@dataclass
class PendingInternal:
    kind: str
    thread_id: str
    request_id: str = ""
    delivery_id: str = ""
    identity: dict[str, str] = field(default_factory=dict)
    receipt: dict[str, Any] | None = None
    tui_request_id: Any = None
    deferred_after_steer: bool = False
    event: threading.Event = field(default_factory=threading.Event)
    outcome: dict[str, Any] | None = None


@dataclass
class QueuedManual:
    request_id: Any
    params: dict[str, Any]


@dataclass
class ThreadState:
    active_turn_id: str = ""
    active_turn: dict[str, Any] | None = None
    steer_ready: bool = False
    pending_start_id: tuple[str, Any] | None = None
    pending_start_owner: str = ""
    queued: list[PendingInternal | QueuedManual] = field(default_factory=list)
    idle_completions: list[PendingInternal] = field(default_factory=list)


class ManagedGateway:
    def __init__(
        self,
        *,
        listen_path: Path,
        upstream_path: Path,
        control_path: Path,
        ledger_path: Path,
        trace_path: Path | None = None,
        fault: str = "none",
        accept_stage_advance: bool = False,
    ) -> None:
        self.listen_path = listen_path
        self.upstream_path = upstream_path
        self.control_path = control_path
        self.ledger = DeliveryLedger(ledger_path)
        self.trace_path = trace_path
        self.fault = fault
        # SD-110 13.32.1-(3)B: whether this gateway has negotiated the v3
        # receipt schema. Defaults False -- an un-negotiated gateway takes the
        # literal, unmodified v2 validation path (A-18 byte-identity).
        self._accept_stage_advance = accept_stage_advance
        self._fault_used = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._front_listener: socket.socket | None = None
        self._control_listener: socket.socket | None = None
        self._tui: WebSocket | None = None
        self._upstream: WebSocket | None = None
        self._epoch = 0
        self._current_thread_id = ""
        self._thread_predecessors: dict[str, str] = {}
        self._threads: dict[str, ThreadState] = {}
        self._tui_requests: dict[tuple[str, Any], tuple[str, dict[str, Any]]] = {}
        self._appserver_requests: dict[
            tuple[str, tuple[str, Any]], float
        ] = {}
        self._internal: dict[tuple[str, Any], PendingInternal] = {}
        self._delivery_pending: dict[str, PendingInternal] = {}
        self._next_internal_id = 1

    def trace(self, event: str, **fields: Any) -> None:
        if self.trace_path is None:
            return
        allowed = {
            "epoch", "method", "thread_id", "turn_id", "delivery_id",
            "action", "status", "reason", "delivery_timing_schema_version",
            *DELIVERY_TIMING_POINTS,
        }
        row = {
            "event": event,
            "time": round(time.monotonic(), 6),
            **{key: value for key, value in fields.items() if key in allowed},
        }
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(canonical(row) + "\n")

    def _new_internal_id(self) -> str:
        value = f"{INTERNAL_ID_PREFIX}{self._next_internal_id}"
        self._next_internal_id += 1
        return value

    @staticmethod
    def _bind_listener(path: Path) -> socket.socket:
        if path.exists() or path.is_symlink():
            raise GatewayError(f"socket-path-exists:{path}")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(16)
        return listener

    def serve_forever(self) -> None:
        self._front_listener = self._bind_listener(self.listen_path)
        self._control_listener = self._bind_listener(self.control_path)
        control_thread = threading.Thread(
            target=self._control_loop,
            name="managed-gateway-control",
            daemon=True,
        )
        control_thread.start()
        self.trace("gateway-ready")
        try:
            while not self._stop.is_set():
                try:
                    connection, _ = self._front_listener.accept()
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                self._serve_tui(connection)
        finally:
            self.close()
            control_thread.join(timeout=2)

    def _serve_tui(self, connection: socket.socket) -> None:
        tui: WebSocket | None = None
        upstream: WebSocket | None = None
        epoch = 0
        reader: threading.Thread | None = None
        try:
            tui = WebSocket.accept(connection)
            upstream = WebSocket.connect_unix(self.upstream_path)
            with self._lock:
                if self._tui is not None or self._upstream is not None:
                    raise GatewayError("tui-already-connected")
                self._epoch += 1
                epoch = self._epoch
                self._tui = tui
                self._upstream = upstream
                self._current_thread_id = ""
                self._thread_predecessors.clear()
                self._threads.clear()
                self._tui_requests.clear()
                for thread_id in {
                    identity[0] for identity in self._appserver_requests
                }:
                    self._clear_thread_requests_locked(thread_id)
                self._appserver_requests.clear()
                self._internal.clear()
                self._delivery_pending.clear()
            self.trace("tui-connected", epoch=epoch)
            reader = threading.Thread(
                target=self._upstream_loop,
                args=(upstream, epoch),
                name=f"managed-gateway-upstream-{epoch}",
                daemon=True,
            )
            reader.start()
            while not self._stop.is_set():
                self._handle_tui_message(tui.read_json(), epoch)
        except (EOFError, OSError, GatewayError) as exc:
            self.trace(
                "tui-disconnected", epoch=epoch, reason=type(exc).__name__
            )
        finally:
            self._disconnect_epoch(epoch, tui, upstream)
            if reader is not None:
                reader.join(timeout=2)

    def _upstream_loop(self, upstream: WebSocket, epoch: int) -> None:
        try:
            while not self._stop.is_set():
                message = upstream.read_json()
                with self._lock:
                    if epoch != self._epoch or upstream is not self._upstream:
                        return
                    self._handle_upstream_locked(message)
        except (EOFError, OSError, GatewayError) as exc:
            self.trace(
                "upstream-disconnected", epoch=epoch, reason=type(exc).__name__
            )
        finally:
            self._disconnect_epoch(epoch, None, upstream)

    def _disconnect_epoch(
        self,
        epoch: int,
        tui: WebSocket | None,
        upstream: WebSocket | None,
    ) -> None:
        with self._lock:
            if epoch and epoch == self._epoch:
                for pending in list(self._delivery_pending.values()):
                    row = self.ledger.get(pending.delivery_id)
                    status = (
                        "sent-ambiguous"
                        if row and row.get("state") == "sent"
                        else "retryable"
                    )
                    pending.outcome = {
                        "schema_version": 1,
                        "status": status,
                        "delivery_id": pending.delivery_id,
                        "reason": "transport-disconnected",
                    }
                    pending.event.set()
                self._delivery_pending.clear()
                self._internal.clear()
                self._tui_requests.clear()
                for thread_id in {
                    identity[0] for identity in self._appserver_requests
                }:
                    self._clear_thread_requests_locked(thread_id)
                self._appserver_requests.clear()
                self._threads.clear()
                self._current_thread_id = ""
                self._thread_predecessors.clear()
                current_tui = self._tui
                current_upstream = self._upstream
                self._tui = None
                self._upstream = None
            else:
                current_tui = None
                current_upstream = None
        seen: set[int] = set()
        for websocket in (tui, upstream, current_tui, current_upstream):
            if websocket is not None and id(websocket) not in seen:
                seen.add(id(websocket))
                websocket.close()

    def _handle_tui_message(self, message: dict[str, Any], epoch: int) -> None:
        with self._lock:
            if epoch != self._epoch or self._upstream is None:
                raise GatewayError("tui-epoch-stale")
            method = message.get("method")
            response_key = (
                request_key(message.get("id")) if "id" in message else None
            )
            if (
                response_key is not None
                and "method" not in message
                and ("result" in message or "error" in message)
            ):
                self._resolve_unique_appserver_response_locked(response_key)
            if method == "turn/start" and "id" in message:
                params = message.get("params")
                if not isinstance(params, dict):
                    self._send_tui_error_locked(
                        message.get("id"), -32602, "turn/start params invalid"
                    )
                    return
                self._handle_manual_start_locked(message.get("id"), params)
                return
            key = request_key(message.get("id")) if "id" in message else None
            if key is not None and isinstance(method, str):
                params = message.get("params")
                self._tui_requests[key] = (
                    method,
                    dict(params) if isinstance(params, dict) else {},
                )
            self._upstream.write_json(message)
            self.trace("tui-forward", method=str(method or ""))

    def _handle_manual_start_locked(
        self, request_id: Any, params: dict[str, Any]
    ) -> None:
        key = request_key(request_id)
        thread_id = params.get("threadId")
        if (
            key is None
            or not isinstance(thread_id, str)
            or not ID_PATTERN.fullmatch(thread_id)
        ):
            self._send_tui_error_locked(
                request_id, -32602, "turn/start identity invalid"
            )
            return
        self._current_thread_id = thread_id
        state = self._threads.setdefault(thread_id, ThreadState())
        if state.active_turn_id and state.steer_ready:
            self._send_manual_steer_locked(request_id, params, state)
            return
        if state.active_turn_id or state.pending_start_id is not None:
            state.queued.append(
                QueuedManual(request_id=request_id, params=dict(params))
            )
            self.trace("manual-queued", thread_id=thread_id)
            return
        self._tui_requests[key] = ("turn/start", dict(params))
        state.steer_ready = False
        state.pending_start_id = key
        state.pending_start_owner = "manual"
        self._upstream_required_locked().write_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "turn/start",
                "params": params,
            }
        )
        self.trace("manual-start", thread_id=thread_id, action="start")

    def _send_manual_steer_locked(
        self,
        request_id: Any,
        params: dict[str, Any],
        state: ThreadState,
    ) -> None:
        thread_id = str(params["threadId"])
        internal_id = self._new_internal_id()
        pending = PendingInternal(
            kind="manual-steer",
            thread_id=thread_id,
            request_id=internal_id,
            tui_request_id=request_id,
        )
        key = request_key(internal_id)
        assert key is not None
        self._internal[key] = pending
        steer_params = {
            "threadId": thread_id,
            "expectedTurnId": state.active_turn_id,
            "input": params.get("input", []),
        }
        for name in (
            "additionalContext",
            "clientUserMessageId",
            "responsesapiClientMetadata",
        ):
            if name in params:
                steer_params[name] = params[name]
        self._upstream_required_locked().write_json(
            {
                "jsonrpc": "2.0",
                "id": internal_id,
                "method": "turn/steer",
                "params": steer_params,
            }
        )
        self.trace(
            "manual-steer",
            thread_id=thread_id,
            turn_id=state.active_turn_id,
            action="steer",
        )

    def _handle_upstream_locked(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        key = request_key(message.get("id")) if "id" in message else None
        if method == "item/tool/requestUserInput":
            thread_id = thread_id_from_message(message)
            if (
                thread_id
                and FLEET_SESSION_PATTERN.fullmatch(thread_id)
                and key is not None
            ):
                identity = (thread_id, key)
                self._appserver_requests.setdefault(identity, time.time())
                self._publish_wait_locked(thread_id)
        elif method == "serverRequest/resolved":
            params = message.get("params")
            if isinstance(params, dict):
                thread_id = params.get("threadId")
                resolved = request_key(params.get("requestId"))
                if (
                    isinstance(thread_id, str)
                    and FLEET_SESSION_PATTERN.fullmatch(thread_id)
                    and resolved is not None
                ):
                    self._resolve_appserver_request_locked(
                        thread_id, resolved
                    )
        if key is not None and key in self._internal:
            self._handle_internal_response_locked(
                self._internal.pop(key), message
            )
            return
        if key is not None and ("result" in message or "error" in message):
            tracked = self._tui_requests.pop(key, None)
            if tracked is not None:
                self._track_tui_response_locked(
                    key, tracked[0], tracked[1], message
                )
            self._send_tui_locked(message)
            return
        if method == "turn/started":
            thread_id = thread_id_from_message(message)
            turn_id = turn_id_from_message(message)
            if thread_id and turn_id:
                state = self._threads.setdefault(thread_id, ThreadState())
                state.active_turn_id = turn_id
                state.active_turn = turn_from_message(message)
                state.steer_ready = True
                # App Server notifications cover every thread sharing the
                # upstream connection, including native subagents.  They are
                # useful per-thread state, but are not ingress-lineage proof.
                # Only a witnessed thread start/resume/fork response may move
                # an established parent binding; the first notification stays
                # a bootstrap fallback for older servers.
                if not self._current_thread_id:
                    self._current_thread_id = thread_id
                self.trace(
                    "turn-started", thread_id=thread_id, turn_id=turn_id
                )
                if state.pending_start_id is None and state.queued:
                    self._drain_queued_locked(state)
        elif method == "turn/completed":
            thread_id = thread_id_from_message(message)
            turn_id = turn_id_from_message(message)
            if thread_id:
                self._clear_thread_requests_locked(thread_id)
            state = self._threads.get(thread_id)
            if state is not None and (
                not turn_id or state.active_turn_id == turn_id
            ):
                state.active_turn_id = ""
                state.active_turn = None
                state.steer_ready = False
                self._start_next_idle_completion_locked(state)
            self.trace(
                "turn-completed", thread_id=thread_id, turn_id=turn_id
            )
        self._send_tui_locked(message)

    def _publish_wait_locked(self, thread_id: str) -> None:
        try:
            pending = [
                waiting
                for (current, _), waiting in self._appserver_requests.items()
                if current == thread_id
            ]
            if pending:
                existing = fleet_interaction.read_wait(thread_id, "codex")
                if (
                    existing is None
                    or existing.get("source") == "codex-appserver"
                ):
                    fleet_interaction.set_wait(
                        thread_id,
                        "codex",
                        "decision",
                        "codex-appserver",
                        now=min(pending),
                    )
        except Exception:
            pass

    def _clear_thread_requests_locked(self, thread_id: str) -> None:
        keys = [
            identity
            for identity in self._appserver_requests
            if identity[0] == thread_id
        ]
        for key in keys:
            self._appserver_requests.pop(key, None)
        try:
            existing = fleet_interaction.read_wait(thread_id, "codex")
            if existing and existing.get("source") == "codex-appserver":
                fleet_interaction.clear_wait(thread_id, "codex")
        except Exception:
            pass

    def _resolve_unique_appserver_response_locked(
        self, key: tuple[str, Any]
    ) -> None:
        matches = [
            identity
            for identity in self._appserver_requests
            if identity[1] == key
        ]
        if len(matches) == 1:
            self._resolve_appserver_request_locked(*matches[0])

    def _resolve_appserver_request_locked(
        self, thread_id: str, key: tuple[str, Any]
    ) -> None:
        identity = (thread_id, key)
        if self._appserver_requests.pop(identity, None) is None:
            return
        remaining = [
            waiting
            for (current, _), waiting in self._appserver_requests.items()
            if current == thread_id
        ]
        try:
            existing = fleet_interaction.read_wait(thread_id, "codex")
            if existing and existing.get("source") == "codex-appserver":
                if remaining:
                    fleet_interaction.set_wait(
                        thread_id,
                        "codex",
                        "decision",
                        "codex-appserver",
                        now=min(remaining),
                    )
                else:
                    fleet_interaction.clear_wait(thread_id, "codex")
        except Exception:
            pass

    def _track_tui_response_locked(
        self,
        request: tuple[str, Any],
        method: str,
        params: dict[str, Any],
        message: dict[str, Any],
    ) -> None:
        if method in {"thread/start", "thread/resume", "thread/fork"} and "error" not in message:
            thread_id = (
                thread_id_from_message(message)
                or str(params.get("threadId", ""))
            )
            if thread_id:
                predecessor = params.get("threadId")
                if (
                    method == "thread/fork"
                    and isinstance(predecessor, str)
                    and ID_PATTERN.fullmatch(predecessor)
                    and predecessor != thread_id
                ):
                    self._thread_predecessors[thread_id] = predecessor
                self._current_thread_id = thread_id
                self._threads.setdefault(thread_id, ThreadState())
        if method != "turn/start":
            return
        thread_id = str(params.get("threadId", ""))
        state = self._threads.setdefault(thread_id, ThreadState())
        if state.pending_start_id == request:
            state.pending_start_id = None
            state.pending_start_owner = ""
        if "error" in message:
            self._fail_queued_locked(state, "manual-start-rejected")
            return
        turn = turn_from_message(message)
        turn_id = turn_id_from_message(message)
        if not turn_id:
            self._fail_queued_locked(state, "manual-start-response-invalid")
            return
        state.active_turn_id = turn_id
        state.active_turn = turn or self._minimal_turn(turn_id)
        if state.steer_ready:
            self._drain_queued_locked(state)

    def _handle_internal_response_locked(
        self, pending: PendingInternal, message: dict[str, Any]
    ) -> None:
        state = self._threads.setdefault(pending.thread_id, ThreadState())
        if pending.kind == "completion-start":
            state.pending_start_id = None
            state.pending_start_owner = ""
            if "error" in message:
                self._reject_delivery_locked(pending, "turn-start-rejected")
                self._fail_queued_locked(state, "completion-start-rejected")
                return
            turn_id = turn_id_from_message(message)
            if not turn_id:
                self._reject_delivery_locked(
                    pending, "turn-start-response-invalid"
                )
                self._fail_queued_locked(
                    state, "completion-start-response-invalid"
                )
                return
            state.active_turn_id = turn_id
            state.active_turn = (
                turn_from_message(message) or self._minimal_turn(turn_id)
            )
            self._accept_delivery_locked(
                pending,
                (
                    "start-after-steer-rejected"
                    if pending.deferred_after_steer
                    else "start"
                ),
            )
            if state.steer_ready:
                self._drain_queued_locked(state)
            return
        if pending.kind == "completion-steer":
            if "error" in message:
                # The explicit RPC error proves that App Server did not accept
                # the receipt. Keep this exact delivery in memory and start it
                # once the manual turn is idle. Its durable state stays `sent`,
                # so a crash in this interval is fail-closed sent-ambiguous.
                pending.kind = "completion-wait"
                pending.request_id = ""
                pending.deferred_after_steer = True
                state.idle_completions.append(pending)
                self.trace(
                    "completion-deferred",
                    thread_id=pending.thread_id,
                    turn_id=state.active_turn_id,
                    delivery_id=pending.delivery_id,
                    action="start",
                    reason="turn-steer-rejected",
                )
                self._start_next_idle_completion_locked(state)
            else:
                response_turn = turn_id_from_message(message)
                if response_turn and response_turn != state.active_turn_id:
                    self._reject_delivery_locked(
                        pending, "turn-steer-id-mismatch"
                    )
                else:
                    self._accept_delivery_locked(pending, "steer")
            return
        if pending.kind == "manual-steer":
            if "error" in message:
                self._send_tui_locked(
                    {
                        "jsonrpc": "2.0",
                        "id": pending.tui_request_id,
                        "error": message["error"],
                    }
                )
            else:
                self._send_tui_locked(
                    {
                        "jsonrpc": "2.0",
                        "id": pending.tui_request_id,
                        "result": {
                            "turn": state.active_turn
                            or self._minimal_turn(state.active_turn_id)
                        },
                    }
                )

    @staticmethod
    def _minimal_turn(turn_id: str) -> dict[str, Any]:
        return {"id": turn_id, "items": [], "status": "inProgress"}

    def _drain_queued_locked(self, state: ThreadState) -> None:
        if not state.active_turn_id:
            self._fail_queued_locked(state, "active-turn-missing")
            return
        queued = list(state.queued)
        state.queued.clear()
        for item in queued:
            if isinstance(item, QueuedManual):
                self._send_manual_steer_locked(
                    item.request_id, item.params, state
                )
            else:
                self._send_completion_locked(item, state, "steer")

    def _start_next_idle_completion_locked(
        self, state: ThreadState
    ) -> None:
        if (
            state.active_turn_id
            or state.pending_start_id is not None
            or not state.idle_completions
        ):
            return
        pending = state.idle_completions.pop(0)
        self._send_completion_locked(pending, state, "start")

    def _fail_queued_locked(
        self, state: ThreadState, reason: str
    ) -> None:
        queued = list(state.queued)
        state.queued.clear()
        for item in queued:
            if isinstance(item, QueuedManual):
                self._send_tui_error_locked(item.request_id, -32001, reason)
            else:
                self._reject_delivery_locked(item, reason)

    def _upstream_required_locked(self) -> WebSocket:
        if self._upstream is None:
            raise GatewayError("upstream-not-connected")
        return self._upstream

    def _send_tui_locked(self, message: dict[str, Any]) -> None:
        if self._tui is None:
            raise GatewayError("tui-not-connected")
        self._tui.write_json(message)

    def _send_tui_error_locked(
        self, request_id: Any, code: int, reason: str
    ) -> None:
        self._send_tui_locked(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": reason},
            }
        )

    def _control_loop(self) -> None:
        assert self._control_listener is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._control_listener.accept()
            except OSError:
                if self._stop.is_set():
                    return
                raise
            threading.Thread(
                target=self._handle_control,
                args=(connection,),
                daemon=True,
            ).start()

    @staticmethod
    def _read_control_line(connection: socket.socket) -> bytes:
        value = bytearray()
        while b"\n" not in value:
            chunk = connection.recv(4096)
            if not chunk:
                break
            value.extend(chunk)
            if len(value) > MAX_CONTROL_BYTES:
                raise GatewayError("control-request-oversized")
        if not value:
            raise GatewayError("control-request-empty")
        line, separator, remainder = bytes(value).partition(b"\n")
        if not separator or remainder.strip():
            raise GatewayError("control-request-framing-invalid")
        return line

    def _handle_control(self, connection: socket.socket) -> None:
        connection.settimeout(130.0)
        try:
            line = self._read_control_line(connection)
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise GatewayError("control-request-json-invalid") from exc
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise GatewayError("control-request-schema-invalid")
            if value.get("op") == "status":
                response = self.status()
            elif value.get("op") == "deliver":
                response = self.deliver(value)
            else:
                raise GatewayError("control-operation-invalid")
        except (GatewayError, OSError) as exc:
            response = {
                "schema_version": 1,
                "status": "rejected",
                "reason": str(exc),
            }
        try:
            connection.sendall(
                (canonical(response) + "\n").encode("utf-8")
            )
        except OSError:
            pass
        finally:
            connection.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._threads.get(self._current_thread_id)
            ancestors: list[str] = []
            observed = {self._current_thread_id}
            current = self._current_thread_id
            while len(ancestors) < MAX_THREAD_LINEAGE:
                predecessor = self._thread_predecessors.get(current, "")
                if not predecessor or predecessor in observed:
                    break
                ancestors.append(predecessor)
                observed.add(predecessor)
                current = predecessor
            return {
                "schema_version": 1,
                "status": (
                    "ready" if self._tui and self._upstream
                    else "disconnected"
                ),
                "epoch": self._epoch,
                "thread_id": self._current_thread_id,
                "thread_ancestors": ancestors,
                "active_turn_id": state.active_turn_id if state else "",
                "approval_owner": "tui",
                "upstream_clients": 1 if self._upstream else 0,
            }

    @staticmethod
    def _validate_identifier(value: Any, reason: str) -> str:
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            raise GatewayError(reason)
        return value

    def _validate_delivery(
        self, request: dict[str, Any]
    ) -> tuple[str, str, str, dict[str, Any], str, str]:
        thread_id = request.get("thread_id")
        if thread_id in {None, ""}:
            with self._lock:
                thread_id = self._current_thread_id
        thread_id = self._validate_identifier(
            thread_id, "thread-id-invalid"
        )
        parent_attempt_id = self._validate_identifier(
            request.get("parent_attempt_id"), "parent-attempt-id-invalid"
        )
        sealed_batch_id = self._validate_identifier(
            request.get("sealed_batch_id"), "sealed-batch-id-invalid"
        )
        receipt = request.get("receipt")
        allowed_receipt_keys = ALLOWED_RECEIPT_KEYS
        allowed_schema_versions = {2}
        if self._accept_stage_advance:
            allowed_receipt_keys = ALLOWED_RECEIPT_KEYS | {STAGE_ADVANCE_RECEIPT_KEY}
            allowed_schema_versions = {2, STAGE_ADVANCE_SCHEMA_VERSION}
        if (
            not isinstance(receipt, dict)
            or set(receipt) - allowed_receipt_keys
        ):
            raise GatewayError("receipt-shape-invalid")
        if (
            receipt.get("schema_version") not in allowed_schema_versions
            or receipt.get("state") != "ready"
            or receipt.get("parent_attempt_id") != parent_attempt_id
        ):
            raise GatewayError("receipt-identity-invalid")
        stage_advance = None
        if (
            self._accept_stage_advance
            and receipt.get("schema_version") == STAGE_ADVANCE_SCHEMA_VERSION
        ):
            try:
                stage_advance = typed_stage_advance_block(
                    receipt.get(STAGE_ADVANCE_RECEIPT_KEY)
                )
            except JoinContractError as exc:
                raise GatewayError("receipt-stage-advance-invalid") from exc
        elif STAGE_ADVANCE_RECEIPT_KEY in receipt:
            raise GatewayError("receipt-shape-invalid")
        raw_jobs = receipt.get("job_registry")
        if (
            not isinstance(raw_jobs, str)
            or not raw_jobs
            or len(raw_jobs.encode("utf-8")) > MAX_JOB_REGISTRY_BYTES
        ):
            raise GatewayError("receipt-job-registry-invalid")
        jobs = Path(raw_jobs)
        if (
            not jobs.is_absolute()
            or jobs.is_symlink()
            or not jobs.is_file()
            or str(jobs) != raw_jobs
        ):
            raise GatewayError("receipt-job-registry-unsafe")
        raw_children = receipt.get("children")
        if (
            not isinstance(raw_children, list)
            or not 1 <= len(raw_children) <= 4
        ):
            raise GatewayError("receipt-children-invalid")
        children: list[dict[str, str]] = []
        observed: set[str] = set()
        for raw in raw_children:
            if (
                not isinstance(raw, dict)
                or set(raw) - ALLOWED_CHILD_KEYS
            ):
                raise GatewayError("receipt-child-shape-invalid")
            attempt_id = self._validate_identifier(
                raw.get("attempt_id"), "receipt-child-attempt-invalid"
            )
            harness = raw.get("harness", "unknown")
            status = raw.get("status")
            required_action = raw.get("required_action")
            delivery_state = raw.get("delivery_classification")
            if (
                attempt_id in observed
                or status not in {"open", "running", "done"}
                or raw.get("readiness") != "ready"
                or raw.get("reason") not in ALLOWED_REASONS
                or required_action not in REQUIRED_ACTIONS
                or (
                    required_action == "complete-open"
                    and status not in {"open", "running"}
                )
                or (
                    required_action in {
                        "inspect-done-failure", "advance-completed"
                    }
                    and status != "done"
                )
                or harness not in {"codex", "claude", "unknown"}
                or delivery_state not in {"success", "attention"}
                or (
                    delivery_state == "success"
                    and required_action != "advance-completed"
                )
                or (
                    delivery_state == "attention"
                    and required_action == "advance-completed"
                )
                or (
                    delivery_state == "attention"
                    and status in {"open", "running"}
                    and required_action != "complete-open"
                )
                or (
                    delivery_state == "attention"
                    and status == "done"
                    and required_action != "inspect-done-failure"
                )
            ):
                raise GatewayError("receipt-child-contract-invalid")
            observed.add(attempt_id)
            children.append(
                {
                    "attempt_id": attempt_id,
                    "status": str(status),
                    "readiness": "ready",
                    "reason": str(raw["reason"]),
                    "required_action": str(required_action),
                    "harness": str(harness),
                    "delivery_classification": str(delivery_state),
                }
            )
        aggregate = (
            "success"
            if children and {child["delivery_classification"] for child in children} == {"success"}
            else "attention"
        )
        if receipt.get("delivery_classification") != aggregate:
            raise GatewayError("receipt-delivery-classification-invalid")
        try:
            delivery_timing = validate_delivery_timing(
                receipt.get("delivery_timing")
            )
        except JoinContractError as exc:
            raise GatewayError("receipt-delivery-timing-invalid") from exc
        if any(
            delivery_timing[point] is not None
            for point in DELIVERY_TIMING_POINTS[2:]
        ):
            raise GatewayError("receipt-delivery-timing-phase-invalid")
        normalized = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": parent_attempt_id,
            "job_registry": raw_jobs,
            "children": children,
            "delivery_classification": aggregate,
            "delivery_timing": delivery_timing,
        }
        if stage_advance is not None:
            normalized = receipt_with_stage_advance(
                normalized, stage_advance_record=stage_advance,
            )
        receipt_bytes = canonical(normalized).encode("utf-8")
        if len(receipt_bytes) > MAX_RECEIPT_BYTES:
            raise GatewayError("receipt-oversized")
        receipt_digest = (
            "sha256:" + hashlib.sha256(receipt_bytes).hexdigest()
        )
        identity = {
            "thread_id": thread_id,
            "parent_attempt_id": parent_attempt_id,
            "sealed_batch_id": sealed_batch_id,
            "receipt_digest": receipt_digest,
        }
        delivery_id = "dlv-" + hashlib.sha256(
            ("managed-delivery-v1\0" + canonical(identity)).encode("utf-8")
        ).hexdigest()
        supplied = request.get("delivery_id")
        if supplied is not None and supplied != delivery_id:
            raise GatewayError("delivery-id-mismatch")
        return (
            thread_id,
            parent_attempt_id,
            sealed_batch_id,
            normalized,
            receipt_digest,
            delivery_id,
        )

    @staticmethod
    def _context_params(
        thread_id: str,
        receipt: dict[str, Any],
        delivery_id: str,
    ) -> dict[str, Any]:
        commands: list[str] = []
        harvest = shlex.quote(
            str(AGENT_HOME / "adapters" / "codex" / "bin" / "preflight.sh")
        )
        jobs = shlex.quote(str(receipt["job_registry"]))
        for child in receipt["children"]:
            attempt = shlex.quote(str(child["attempt_id"]))
            if child["required_action"] == "complete-open":
                commands.append(
                    f"{harvest} harvest --jobs {jobs} --attempt-id {attempt} "
                    "--status open --mark-done"
                )
            elif child["required_action"] == "inspect-done-failure":
                commands.append(
                    f"{harvest} harvest --jobs {jobs} --attempt-id {attempt} "
                    "--status done --failure-detail"
                )
        command_text = "\n".join(commands) or "(no harvest command; advance the route)"
        context = (
            "AGENT_HARNESS_COMPLETION_V1\n"
            + canonical(receipt)
            + "\nExact typed receipt. Run only these commands:\n"
            + command_text
            + "\nThen continue the route; no raw logs or waits."
        )
        if len(context.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise GatewayError("completion-context-oversized")
        return {
            "threadId": thread_id,
            "input": [],
            "clientUserMessageId": delivery_id,
            "additionalContext": {
                "hearting-completion": {
                    "kind": "application",
                    "value": context,
                }
            },
        }

    def deliver(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            (
                thread_id,
                parent_attempt_id,
                sealed_batch_id,
                receipt,
                receipt_digest,
                delivery_id,
            ) = self._validate_delivery(request)
        except GatewayError as exc:
            return {
                "schema_version": 1,
                "status": "rejected",
                "reason": str(exc),
            }
        identity = {
            "thread_id": thread_id,
            "parent_attempt_id": parent_attempt_id,
            "sealed_batch_id": sealed_batch_id,
            "receipt_digest": receipt_digest,
        }
        with self._lock:
            existing = self.ledger.get(delivery_id)
            if existing and existing.get("state") == "accepted":
                return {
                    "schema_version": 1,
                    "status": "accepted",
                    "delivery_id": delivery_id,
                    "action": existing.get("action"),
                    "replay": True,
                }
            if existing and existing.get("state") == "sent":
                live = self._delivery_pending.get(delivery_id)
                if live is None:
                    return {
                        "schema_version": 1,
                        "status": "sent-ambiguous",
                        "delivery_id": delivery_id,
                        "reason": "accept-not-observed-no-resend",
                    }
                pending = live
            elif existing and existing.get("state") == "rejected":
                return {
                    "schema_version": 1,
                    "status": "rejected",
                    "delivery_id": delivery_id,
                    "reason": (
                        existing.get("reason") or "prior-rejection"
                    ),
                }
            else:
                if self._tui is None or self._upstream is None:
                    return {
                        "schema_version": 1,
                        "status": "retryable",
                        "delivery_id": delivery_id,
                        "reason": "tui-not-connected",
                    }
                if self._current_thread_id != thread_id:
                    return {
                        "schema_version": 1,
                        "status": "rejected",
                        "delivery_id": delivery_id,
                        "reason": "thread-not-owned-by-current-tui",
                    }
                if existing is None:
                    self.ledger.transition(
                        delivery_id, "prepared", **identity
                    )
                pending = PendingInternal(
                    kind="completion-wait",
                    thread_id=thread_id,
                    delivery_id=delivery_id,
                    identity=identity,
                    receipt=receipt,
                )
                self._delivery_pending[delivery_id] = pending
                if self.fault == "before-send" and not self._fault_used:
                    self._fault_used = True
                    os._exit(91)
                state = self._threads.setdefault(
                    thread_id, ThreadState()
                )
                if state.active_turn_id and state.steer_ready:
                    self._send_completion_locked(
                        pending, state, "steer"
                    )
                elif (
                    state.active_turn_id
                    or state.pending_start_id is not None
                ):
                    state.queued.append(pending)
                    self.trace(
                        "completion-queued",
                        thread_id=thread_id,
                        delivery_id=delivery_id,
                    )
                else:
                    self._send_completion_locked(
                        pending, state, "start"
                    )
        if not pending.event.wait(120.0):
            return {
                "schema_version": 1,
                "status": "sent-ambiguous",
                "delivery_id": delivery_id,
                "reason": "accept-response-timeout-no-resend",
            }
        return pending.outcome or {
            "schema_version": 1,
            "status": "sent-ambiguous",
            "delivery_id": delivery_id,
            "reason": "delivery-outcome-missing",
        }

    def _send_completion_locked(
        self,
        pending: PendingInternal,
        state: ThreadState,
        action: str,
    ) -> None:
        if pending.receipt is None:
            self._reject_delivery_locked(
                pending, "queued-receipt-missing"
            )
            return
        request_id = self._new_internal_id()
        pending.request_id = request_id
        pending.kind = f"completion-{action}"
        key = request_key(request_id)
        assert key is not None
        self._internal[key] = pending
        params = self._context_params(
            pending.thread_id, pending.receipt, pending.delivery_id
        )
        if action == "steer":
            params["expectedTurnId"] = state.active_turn_id
            method = "turn/steer"
        else:
            method = "turn/start"
            state.steer_ready = False
            state.pending_start_id = key
            state.pending_start_owner = "completion"
        self.ledger.transition(
            pending.delivery_id,
            "sent",
            action=action,
            **pending.identity,
        )
        self._upstream_required_locked().write_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        self.trace(
            "completion-send",
            thread_id=pending.thread_id,
            turn_id=state.active_turn_id,
            delivery_id=pending.delivery_id,
            action=action,
            **advance_delivery_timing(
                pending.receipt["delivery_timing"], "same_thread_resume_ns"
            ),
        )
        if self.fault == "after-send" and not self._fault_used:
            self._fault_used = True
            os._exit(92)

    def _accept_delivery_locked(
        self, pending: PendingInternal, action: str
    ) -> None:
        try:
            self.ledger.transition(
                pending.delivery_id,
                "accepted",
                action=action,
                **pending.identity,
            )
            outcome = {
                "schema_version": 1,
                "status": "accepted",
                "delivery_id": pending.delivery_id,
                "action": action,
                "replay": False,
            }
        except GatewayError:
            outcome = {
                "schema_version": 1,
                "status": "sent-ambiguous",
                "delivery_id": pending.delivery_id,
                "reason": "accepted-ledger-commit-failed",
            }
        self._delivery_pending.pop(pending.delivery_id, None)
        pending.outcome = outcome
        pending.event.set()
        self.trace(
            "completion-result",
            thread_id=pending.thread_id,
            delivery_id=pending.delivery_id,
            action=action,
            status=outcome["status"],
        )

    def _reject_delivery_locked(
        self, pending: PendingInternal, reason: str
    ) -> None:
        try:
            self.ledger.transition(
                pending.delivery_id,
                "rejected",
                reason=reason,
                **pending.identity,
            )
        except GatewayError:
            reason = "rejection-ledger-commit-failed"
        self._delivery_pending.pop(pending.delivery_id, None)
        pending.outcome = {
            "schema_version": 1,
            "status": "rejected",
            "delivery_id": pending.delivery_id,
            "reason": reason,
        }
        pending.event.set()
        self.trace(
            "completion-result",
            thread_id=pending.thread_id,
            delivery_id=pending.delivery_id,
            status="rejected",
            reason=reason,
        )

    def close(self) -> None:
        already_stopped = self._stop.is_set()
        self._stop.set()
        if not already_stopped:
            for listener in (
                self._front_listener, self._control_listener
            ):
                if listener is not None:
                    try:
                        listener.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    listener.close()
            with self._lock:
                tui, upstream = self._tui, self._upstream
                self._tui = None
                self._upstream = None
            for websocket in (tui, upstream):
                if websocket is not None:
                    websocket.close()
        for path in (self.listen_path, self.control_path):
            try:
                if stat.S_ISSOCK(path.lstat().st_mode):
                    path.unlink()
            except FileNotFoundError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", required=True, type=Path)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--trace", type=Path)
    parser.add_argument(
        "--fault",
        choices=("none", "before-send", "after-send"),
        default="none",
        help="one-shot proof fault; never set in a managed session",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gateway = ManagedGateway(
        listen_path=args.listen,
        upstream_path=args.upstream,
        control_path=args.control,
        ledger_path=args.ledger,
        trace_path=args.trace,
        fault=args.fault,
    )

    def stop(_signum: int, _frame: Any) -> None:
        gateway.close()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        gateway.serve_forever()
    except GatewayError as exc:
        print(
            canonical({"status": "error", "reason": str(exc)}),
            flush=True,
        )
        return 65
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
