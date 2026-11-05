#!/usr/bin/env python3
"""Launch exact-batch joiners for the caller's native Codex queue."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Mapping

from codex_managed_dispatch import ManagedDispatchError, ManagedSidecar
try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test
except ImportError:  # a partial tree without it (e.g. a fixture release): no record
    def end_with_test(child):
        return child


ROOT = Path(__file__).resolve().parents[1]
SESSION_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def resolve_queue_socket(environ: Mapping[str, str] | None = None) -> Path:
    """Find the App Server that owns this TUI, including legacy managed panes."""
    env = os.environ if environ is None else environ
    raw_control = env.get("AGENT_CODEX_MANAGED_CONTROL_SOCKET")
    if raw_control:
        control = Path(raw_control)
        if not control.is_absolute() or control.is_symlink() or control.name != "managed-control.sock":
            raise ManagedDispatchError("native-queue-endpoint-invalid")
        return control.parent / "app-server.sock"
    raw_home = env.get("CODEX_HOME") or "~/.codex"
    home = Path(raw_home).expanduser()
    if not home.is_absolute() or home.is_symlink():
        raise ManagedDispatchError("native-queue-home-invalid")
    return home / "app-server-control" / "app-server-control.sock"


def _timeout(environ: Mapping[str, str]) -> int:
    raw = environ.get("AGENT_CODEX_QUEUE_COMPLETION_TIMEOUT", "86400")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ManagedDispatchError("native-queue-completion-timeout-invalid") from exc
    if not 60 <= value <= 604800:
        raise ManagedDispatchError("native-queue-completion-timeout-invalid")
    return value


def _batch_id(thread_id: str, attempt_ids: set[str], jobs: Path) -> str:
    material = "codex-native-queue-batch-v1\0" + "\0".join(
        [thread_id, str(jobs.resolve()), *sorted(attempt_ids)]
    )
    return "batch-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def launch_codex_queue_completion_sidecar(
    *,
    jobs: Path,
    parent_session_id: str,
    attempt_ids: set[str],
    environ: Mapping[str, str] | None = None,
) -> ManagedSidecar:
    env = dict(os.environ if environ is None else environ)
    if (
        not jobs.is_absolute() or jobs.is_symlink() or not jobs.is_file()
        or not SESSION_ID.fullmatch(parent_session_id)
        or not attempt_ids or len(attempt_ids) > 4
    ):
        raise ManagedDispatchError("native-queue-sidecar-identity-invalid")
    queue_socket = resolve_queue_socket(env)
    batch_id = _batch_id(parent_session_id, attempt_ids, jobs)
    log_dir = jobs.resolve(strict=False).parent / "queue-sidecars"
    try:
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
        info = log_dir.stat()
        if log_dir.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ManagedDispatchError("native-queue-log-directory-unsafe")
        log_file = log_dir / f"{batch_id}.jsonl"
        output = log_file.open("ab", buffering=0)
        os.chmod(log_file, 0o600)
    except OSError as exc:
        raise ManagedDispatchError("native-queue-log-unavailable") from exc
    command = [
        sys.executable,
        str(ROOT / "utilities" / "codex-managed-completion.py"),
        "--queue-socket", str(queue_socket),
        "--jobs", str(jobs),
        "--parent-session-id", parent_session_id,
        "--thread-id", parent_session_id,
        "--sealed-batch-id", batch_id,
        "--launch-ready-timeout", "60",
        "--timeout", str(_timeout(env)),
    ]
    for attempt_id in sorted(attempt_ids):
        command += ["--attempt-id", attempt_id]
    env["AGENT_CODEX_QUEUE_SIDECAR"] = "1"
    try:
        process = subprocess.Popen(
            command, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
            stdout=output, stderr=output, start_new_session=True,
        )
    except OSError as exc:
        output.close()
        raise ManagedDispatchError("native-queue-sidecar-launch-failed") from exc
    end_with_test(process)
    output.close()
    return ManagedSidecar(process.pid, batch_id, log_file)
