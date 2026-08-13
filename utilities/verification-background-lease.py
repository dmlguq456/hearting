#!/usr/bin/env python3
"""Run one bounded verification command with an exact attempt-owned lease."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from dispatch_contract import (
    dispatch_state_root,
    parse_registry_metadata,
    process_launch_identity,
)


def exact_attempt(jobs: Path, attempt_id: str) -> tuple[str, dict[str, str]] | None:
    found = None
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == attempt_id:
            found = (fields[1], metadata)
    return found


def atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def lease_context(command: list[str], timeout: float) -> tuple[Path, dict[str, object]] | None:
    attempt = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    raw_jobs = os.environ.get("AGENT_DISPATCH_JOBS", "")
    if not attempt or not raw_jobs:
        return None
    jobs = Path(raw_jobs).expanduser().resolve(strict=False)
    row = exact_attempt(jobs, attempt)
    if row is None or row[0] not in {"open", "running"}:
        return None
    metadata = row[1]
    route_id = metadata.get("route_id", "")
    route_node = metadata.get("route_node", "")
    if not route_id or not route_node:
        return None
    digest = hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()
    path = dispatch_state_root(jobs) / "verification-leases" / f"{attempt}.json"
    return path, {
        "schema_version": 1,
        "attempt_id": attempt,
        "route_id": route_id,
        "route_node": route_node,
        "command_digest": digest,
        "started_at": time.time(),
        "deadline": time.time() + timeout,
    }


def terminate_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if args.timeout <= 0 or not command:
        parser.error("a positive --timeout and command are required")
    process = subprocess.Popen(command, start_new_session=True)
    lease = lease_context(command, args.timeout)
    lease_path: Path | None = None
    if lease is not None:
        lease_path, value = lease
        identity = process_launch_identity(process.pid)
        if identity.get("pid_start") and identity.get("pgid") == str(process.pid):
            value.update(
                {
                    "pid": process.pid,
                    "pid_start": identity["pid_start"],
                    "pgid": process.pid,
                }
            )
            atomic_json(lease_path, value)
        else:
            lease_path = None
    try:
        return process.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        terminate_group(process)
        return 124
    finally:
        if lease_path is not None:
            try:
                lease_path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
