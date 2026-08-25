#!/usr/bin/env python3
"""Hold a claimed worker behind a parent-death-safe registry publication gate."""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys

from dispatch_contract import mark_attempt_launch_started


PR_SET_PDEATHSIG = 1
ROOT = Path(__file__).resolve().parents[1]
_ROUTE_SPEC = importlib.util.spec_from_file_location(
    "launch_fence_capability_route", ROOT / "utilities" / "capability-route.py"
)
ROUTE = importlib.util.module_from_spec(_ROUTE_SPEC)
assert _ROUTE_SPEC.loader is not None
_ROUTE_SPEC.loader.exec_module(ROUTE)
FAILURE_RECORD_MAX_BYTES = 4096


def set_parent_death_signal(signum: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signum, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--gate-fd", required=True, type=int)
    parser.add_argument("--failure-fd", type=int)
    parser.add_argument("--jobs")
    parser.add_argument("--attempt-id")
    parser.add_argument("--route-file")
    parser.add_argument("--launch-phase", choices=("dry-run", "register", "start"))
    parser.add_argument(
        "--post-release-parent-death-signal",
        choices=("none", "term", "kill"),
        default="none",
        help="retain parent-death coupling after publication when lifecycle owns it",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.failure_fd is not None:
        # A successful payload exec is represented by EOF with no failure
        # record.  Do not leak this private wrapper channel into the payload.
        os.set_inheritable(args.failure_fd, False)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("launch fence command is required")
    if bool(args.jobs) != bool(args.attempt_id):
        raise ValueError("--jobs and --attempt-id must be provided together")
    if bool(args.route_file) != bool(args.launch_phase):
        raise ValueError("--route-file and --launch-phase must be provided together")

    set_parent_death_signal(signal.SIGKILL)
    if os.getppid() != args.parent_pid:
        return 70
    try:
        released = os.read(args.gate_fd, 1)
    finally:
        os.close(args.gate_fd)
    if released != b"1":
        return 70
    # Publication is now durable. Detached workers clear parent coupling;
    # foreground-scoped launchers retain an explicit post-release signal.
    post_release_signal = {
        "none": 0,
        "term": signal.SIGTERM,
        "kill": signal.SIGKILL,
    }[args.post_release_parent_death_signal]
    set_parent_death_signal(post_release_signal)
    if post_release_signal and os.getppid() != args.parent_pid:
        return 70
    if args.route_file:
        route_path = Path(args.route_file).resolve(strict=True)
        route = ROUTE.verify_route(json.loads(route_path.read_text(encoding="utf-8")))
        compatible, mismatches = ROUTE.revalidate_launch_compatibility(route)
        if mismatches.get("tuple") == "absent-legacy":
            raise ValueError("launch-compatibility-tuple-required")
        if not compatible:
            raise ValueError(
                "launch-runtime-root-mismatch "
                + json.dumps({"phase": args.launch_phase, "mismatches": mismatches}, sort_keys=True)
            )
    # Detached fences must clear the short-lived launcher's PDEATHSIG before
    # committing launch_started; if the launcher disappears after this point,
    # the detached fence remains the exact governed process and can still exec.
    # Foreground fences deliberately retain coupling, so parent loss is a
    # terminal launch failure rather than permission to replay the assignment.
    if args.jobs:
        mark_attempt_launch_started(Path(args.jobs), args.attempt_id, os.getpid())
    os.execvpe(command[0], command, os.environ)
    return 70  # pragma: no cover - exec never returns


def _failure_fd(argv: list[str] | None) -> int | None:
    values = sys.argv[1:] if argv is None else argv
    try:
        index = values.index("--failure-fd")
        return int(values[index + 1])
    except (ValueError, IndexError):
        return None


def _write_failure(fd: int | None, exc: OSError | ValueError) -> None:
    if fd is None:
        return
    detail = str(exc)
    reason = detail.split(" ", 1)[0] or "launch-fence-error"
    record = json.dumps(
        {
            "schema_version": 1,
            "reason": reason[:160],
            "detail": detail[:3072],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    try:
        os.write(fd, record[:FAILURE_RECORD_MAX_BYTES])
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def cli(argv: list[str] | None = None) -> int:
    try:
        return main(argv)
    except (OSError, ValueError) as exc:
        _write_failure(_failure_fd(argv), exc)
        print(f"launch-fence: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(cli())
