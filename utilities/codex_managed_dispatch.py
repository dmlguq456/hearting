#!/usr/bin/env python3
"""Checked adapter glue for an opt-in managed Codex parent.

This module is intentionally child-runtime neutral.  Codex and Claude dispatch
wrappers call it only for a direct registered child whose *parent* is Codex.
The completion sidecar talks to the gateway's private control socket; it never
opens an App Server connection and therefore cannot subscribe to or answer
approval requests.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
MANAGED_PARENT_DELIVERY = "codex-managed-gateway"
MAX_CONTROL_RESPONSE_BYTES = 16 * 1024
MAX_BATCH_ATTEMPTS = 4


READINESS_REASON_CLASSES = (
    "expected-thread-not-witnessed",
    "lineage-mismatch",
    "tui-disconnected",
    "approval-owner-mismatch",
    "upstream-client-count-invalid",
)


class ManagedDispatchError(RuntimeError):
    """The managed parent boundary could not be proved or launched."""

    def __init__(self, message: str, *, reason_class: str = "") -> None:
        super().__init__(message)
        self.reason_class = reason_class


@dataclass(frozen=True)
class ManagedGatewayBinding:
    control_socket: Path
    thread_id: str
    epoch: int
    inherited_thread_id: str = ""
    binding_source: str = ""

    @property
    def thread_advanced(self) -> bool:
        return bool(
            self.inherited_thread_id
            and self.inherited_thread_id != self.thread_id
        )


@dataclass(frozen=True)
class ManagedSidecar:
    pid: int
    sealed_batch_id: str
    log_file: Path


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _secure_control_socket(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise ManagedDispatchError("managed-control-path-unsafe")
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
        parent = path.parent.resolve(strict=True)
        parent_info = parent.stat()
    except OSError as exc:
        raise ManagedDispatchError("managed-control-unavailable") from exc
    if resolved != path or not stat.S_ISSOCK(info.st_mode):
        raise ManagedDispatchError("managed-control-path-unsafe")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ManagedDispatchError("managed-control-permissions-unsafe")
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or parent_info.st_mode & 0o077
    ):
        raise ManagedDispatchError("managed-state-directory-unsafe")
    return path


def _control_request(
    path: Path,
    request: dict[str, Any],
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(path))
        connection.sendall((_canonical(request) + "\n").encode("utf-8"))
        response = bytearray()
        while b"\n" not in response:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_CONTROL_RESPONSE_BYTES:
                raise ManagedDispatchError("managed-status-response-oversized")
    except OSError as exc:
        raise ManagedDispatchError("managed-control-unavailable") from exc
    finally:
        connection.close()
    line, separator, remainder = bytes(response).partition(b"\n")
    if not separator or remainder.strip():
        raise ManagedDispatchError("managed-status-framing-invalid")
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManagedDispatchError("managed-status-json-invalid") from exc
    if not isinstance(value, dict):
        raise ManagedDispatchError("managed-status-shape-invalid")
    return value


def probe_managed_codex_parent(
    *,
    parent_harness: str,
    parent_session_id: str | None,
    environ: Mapping[str, str] | None = None,
) -> ManagedGatewayBinding:
    """Return a live single-ingress binding or fail with a typed reason."""

    env = os.environ if environ is None else environ
    if env.get("AGENT_CODEX_MANAGED_GATEWAY") != "1":
        raise ManagedDispatchError("managed-entry-not-enabled")
    if env.get("AGENT_CODEX_MANAGED_PARENT_RUNTIME") != "codex":
        raise ManagedDispatchError("managed-parent-runtime-mismatch")
    if parent_harness != "codex":
        raise ManagedDispatchError("managed-parent-harness-mismatch")
    current_thread = env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")
    if not current_thread or parent_session_id != current_thread:
        raise ManagedDispatchError("managed-parent-thread-mismatch")
    raw_control = env.get("AGENT_CODEX_MANAGED_CONTROL_SOCKET", "")
    if not raw_control:
        raise ManagedDispatchError("managed-control-missing")
    control = _secure_control_socket(Path(raw_control))
    response = _control_request(
        control, {"schema_version": 1, "op": "status"}
    )
    epoch = response.get("epoch")
    gateway_thread = response.get("thread_id")
    ancestors = response.get("thread_ancestors", [])
    siblings = response.get("sibling_thread_ids", [])
    witnessed_thread = response.get("witnessed_thread_id", "")
    binding_source = response.get("binding_source", "")
    lineage_valid = (
        isinstance(ancestors, list)
        and len(ancestors) <= 16
        and all(isinstance(value, str) for value in ancestors)
    )
    siblings_valid = (
        isinstance(siblings, list)
        and len(siblings) <= 16
        and all(isinstance(value, str) for value in siblings)
    )
    if (
        response.get("schema_version") != 1
        or not isinstance(gateway_thread, str)
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch < 1
    ):
        raise ManagedDispatchError("managed-gateway-not-ready")
    inherited_is_current_or_ancestor = (
        gateway_thread == current_thread
        or (lineage_valid and current_thread in ancestors)
    )
    known_threads: set[str] = {gateway_thread}
    if lineage_valid:
        known_threads.update(ancestors)
    if siblings_valid:
        known_threads.update(siblings)
    if isinstance(witnessed_thread, str) and witnessed_thread:
        known_threads.add(witnessed_thread)
    # Evaluate the five typed readiness reason classes in a fixed,
    # documented order so exactly one class is ever reported.
    if current_thread not in known_threads:
        raise ManagedDispatchError(
            "managed-gateway-not-ready",
            reason_class="expected-thread-not-witnessed",
        )
    if not inherited_is_current_or_ancestor:
        raise ManagedDispatchError(
            "managed-gateway-not-ready", reason_class="lineage-mismatch"
        )
    if response.get("status") != "ready":
        raise ManagedDispatchError(
            "managed-gateway-not-ready", reason_class="tui-disconnected"
        )
    if response.get("approval_owner") != "tui":
        raise ManagedDispatchError(
            "managed-gateway-not-ready",
            reason_class="approval-owner-mismatch",
        )
    if response.get("upstream_clients") != 1:
        raise ManagedDispatchError(
            "managed-gateway-not-ready",
            reason_class="upstream-client-count-invalid",
        )
    return ManagedGatewayBinding(
        control,
        gateway_thread,
        epoch,
        inherited_thread_id=current_thread,
        binding_source=binding_source if isinstance(binding_source, str) else "",
    )


def sealed_batch_id(thread_id: str, attempt_ids: set[str]) -> str:
    if not attempt_ids or len(attempt_ids) > MAX_BATCH_ATTEMPTS:
        raise ManagedDispatchError("managed-attempt-set-invalid")
    material = "managed-root-batch-v1\0" + thread_id + "\0" + "\0".join(
        sorted(attempt_ids)
    )
    return "batch-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def registered_parent_delivery(jobs: Path, attempt_id: str) -> str:
    """Read the immutable parent delivery stamped on one exact attempt."""

    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ManagedDispatchError("managed-registry-unreadable") from exc
    observed = ""
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = dict(
            part.split("=", 1)
            for part in fields[5].split(",")
            if "=" in part
        )
        if metadata.get("attempt_id") == attempt_id:
            observed = metadata.get("parent_completion_delivery", "")
    if not observed:
        raise ManagedDispatchError("managed-parent-delivery-missing")
    return observed


def _completion_timeout(environ: Mapping[str, str]) -> int:
    raw = environ.get("AGENT_CODEX_MANAGED_COMPLETION_TIMEOUT", "86400")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ManagedDispatchError("managed-completion-timeout-invalid") from exc
    if not 60 <= value <= 604800:
        raise ManagedDispatchError("managed-completion-timeout-invalid")
    return value


def launch_managed_completion_sidecar(
    *,
    binding: ManagedGatewayBinding,
    jobs: Path,
    parent_session_id: str,
    attempt_ids: set[str],
    environ: Mapping[str, str] | None = None,
) -> ManagedSidecar:
    """Launch one detached exact-batch joiner and return only typed metadata."""

    env = dict(os.environ if environ is None else environ)
    if not jobs.is_absolute() or parent_session_id != binding.thread_id:
        raise ManagedDispatchError("managed-sidecar-identity-invalid")
    batch_id = sealed_batch_id(binding.thread_id, attempt_ids)
    state_dir = binding.control_socket.parent
    log_dir = state_dir / "managed-sidecars"
    try:
        log_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(log_dir, 0o700)
        if log_dir.resolve(strict=True) != log_dir or log_dir.stat().st_uid != os.geteuid():
            raise ManagedDispatchError("managed-sidecar-log-directory-unsafe")
        log_file = log_dir / f"{batch_id}.jsonl"
        output = log_file.open("ab", buffering=0)
        os.chmod(log_file, 0o600)
    except OSError as exc:
        raise ManagedDispatchError("managed-sidecar-log-unavailable") from exc
    command = [
        sys.executable,
        str(ROOT / "utilities" / "codex-managed-completion.py"),
        "--control-socket",
        str(binding.control_socket),
        "--jobs",
        str(jobs),
        "--parent-session-id",
        parent_session_id,
        "--thread-id",
        binding.thread_id,
        "--sealed-batch-id",
        batch_id,
        "--launch-ready-timeout",
        "60",
        "--timeout",
        str(_completion_timeout(env)),
    ]
    for attempt_id in sorted(attempt_ids):
        command += ["--attempt-id", attempt_id]
    env["AGENT_CODEX_MANAGED_SIDECAR"] = "1"
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
    except OSError as exc:
        output.close()
        raise ManagedDispatchError("managed-sidecar-launch-failed") from exc
    output.close()
    return ManagedSidecar(process.pid, batch_id, log_file)
