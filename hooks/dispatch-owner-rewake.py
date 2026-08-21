#!/usr/bin/env python3
"""Wake an interactive Claude parent once when its exact headless owner finishes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))
from dispatch_contract import resolve_agent_home as _resolve_agent_home  # noqa: E402
from dispatch_completion_join import (  # noqa: E402
    JoinContractError,
    child_row_revision,
    current_attempt_row,
    required_action_for_attempt,
)


ATTEMPT = re.compile(r"att-[A-Za-z0-9._-]{1,240}\Z")
DEFAULT_INTERVAL_SECONDS = 20
DEFAULT_MAX_SECONDS = 21_600
DEFAULT_ARM_WINDOW_SECONDS = 600
MAXIMUM_CLOCK_SKEW_SECONDS = 60
REGISTRY_OWNER_START = {
    "worker_type": "owner",
    "dispatch_depth": "1",
    "parent_completion_delivery": "claude-parent-runtime",
    "launch_claimed": "1",
    "launch_started": "1",
}


@dataclass(frozen=True)
class Launch:
    attempt_id: str
    jobs: Path
    session_id: str
    armed: str = "stdout"


def _stdout(response: object) -> str:
    if not isinstance(response, dict):
        return ""
    value = response.get("stdout")
    return value if isinstance(value, str) else ""


def _fields(output: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_.-]*", key):
            continue
        result.setdefault(key, []).append(value)
    return result


def _single(fields: dict[str, list[str]], key: str) -> str | None:
    values = fields.get(key, [])
    return values[0] if len(values) == 1 else None


def _payload_session(payload: dict[str, Any]) -> str | None:
    value = payload.get("session_id")
    return value if isinstance(value, str) and value else None


def _validated_jobs(raw: str | None) -> Path | None:
    """Apply the one registry-path security boundary to every arming route."""

    if not raw:
        return None
    jobs = Path(raw)
    if not jobs.is_absolute() or jobs.is_symlink() or not jobs.is_file():
        return None
    return jobs


def _owner_start_command(payload: object) -> tuple[dict[str, Any], str] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or "dispatch-owner" not in command:
        return None
    return payload, command


def parse_launch(payload: object) -> Launch | None:
    """Accept only a successful exact depth-1 owner start from this session."""

    gate = _owner_start_command(payload)
    if gate is None:
        return None
    payload, command = gate
    fields = _fields(_stdout(payload.get("tool_response")))
    required_memberships = {
        "check": "ok",
        "status": "start",
        "dispatch_depth": "1",
        "worker_type": "owner",
        "parent_completion_delivery": "claude-parent-runtime",
        "registered": "1",
        "started": "1",
    }
    if any(expected not in fields.get(key, []) for key, expected in required_memberships.items()):
        return None
    attempt_id = _single(fields, "attempt_id")
    parent_session = _single(fields, "parent_session_id")
    payload_session = _payload_session(payload)
    if (
        payload_session is None
        or parent_session != payload_session
        or attempt_id is None
        or ATTEMPT.fullmatch(attempt_id) is None
    ):
        return None
    jobs = _validated_jobs(_single(fields, "job_registry"))
    if jobs is None:
        return None
    return Launch(attempt_id=attempt_id, jobs=jobs, session_id=payload_session, armed="stdout")


def _command_jobs(command: str) -> str | None:
    """Read the inherited registry path the launch command itself declared."""

    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for index, part in enumerate(parts):
        if part == "--jobs" and index + 1 < len(parts):
            return parts[index + 1]
        if part.startswith("--jobs="):
            return part[len("--jobs=") :]
    return None


def _registry_metadata(pipe: str) -> dict[str, str]:
    """Tolerantly read the six-column registry pipe in comma or space dual form."""

    def pairs(parts: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in parts:
            key, separator, value = part.strip().partition("=")
            if separator and key:
                result[key] = value
        return result

    comma = pairs(pipe.split(","))
    return comma if "attempt_id" in comma else pairs(pipe.replace(",", " ").split())


def _row_age(stamp: str, now: float) -> float | None:
    try:
        moment = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return now - moment.timestamp()


def registry_launch(payload: object) -> Launch | None:
    """Arm from the wrapper-written registry when stdout was filtered away.

    A piped `dispatch-owner --start | tail` hands this hook a truncated stdout,
    so the receipt fast path silently fails to arm.  The lock-written registry
    row carries the same exactness — one open depth-1 owner attempt bound to
    this Claude session, started inside the immediately preceding tool window —
    and is harder to forge than tool output.  Zero or several candidates stay a
    silent no-op: absence beats misattribution.
    """

    gate = _owner_start_command(payload)
    if gate is None:
        return None
    payload, command = gate
    if "--start" not in command:
        return None
    session = _payload_session(payload)
    if session is None:
        return None
    fields = _fields(_stdout(payload.get("tool_response")))
    jobs = (
        _validated_jobs(_single(fields, "job_registry"))
        or _validated_jobs(_command_jobs(command))
        or _validated_jobs(os.environ.get("AGENT_DISPATCH_JOBS"))
        or _validated_jobs(str(agent_home() / ".dispatch" / "jobs.log"))
    )
    if jobs is None:
        return None
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    latest: dict[str, tuple[str, str, dict[str, str]]] = {}
    for line in lines:
        columns = line.split("\t")
        if len(columns) != 6:
            continue
        metadata = _registry_metadata(columns[5])
        attempt_id = metadata.get("attempt_id", "")
        if ATTEMPT.fullmatch(attempt_id) is None:
            continue
        latest[attempt_id] = (columns[0], columns[1], metadata)
    window = _bounded_number(
        "AGENT_CLAUDE_REWAKE_ARM_WINDOW_SECONDS", DEFAULT_ARM_WINDOW_SECONDS, 30, 86_400
    )
    now = time.time()
    candidates = []
    for attempt_id, (stamp, status, metadata) in latest.items():
        if status != "open" or metadata.get("parent_sid") != session:
            continue
        if any(metadata.get(key) != value for key, value in REGISTRY_OWNER_START.items()):
            continue
        age = _row_age(stamp, now)
        if age is None or age > window or age < -MAXIMUM_CLOCK_SKEW_SECONDS:
            continue
        candidates.append(attempt_id)
    if len(candidates) != 1:
        return None
    return Launch(attempt_id=candidates[0], jobs=jobs, session_id=session, armed="registry")


def agent_home() -> Path:
    return _resolve_agent_home()


def _bounded_number(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def wait_for_attempt(launch: Launch, readiness: Path) -> tuple[str, str]:
    interval = _bounded_number(
        "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS, 1, 300
    )
    maximum = _bounded_number(
        "AGENT_CLAUDE_REWAKE_MAX_SECONDS", DEFAULT_MAX_SECONDS, interval, 86_400
    )
    deadline = time.monotonic() + maximum
    command = [
        sys.executable,
        str(readiness),
        "--jobs",
        str(launch.jobs),
        "--attempt-id",
        launch.attempt_id,
    ]
    while True:
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "bridge-error", type(exc).__name__
        if result.returncode == 0:
            return "ready", "terminal-quiescent"
        if result.returncode == 3:
            return "attention", "terminal-failure-or-unclosed"
        if result.returncode != 2:
            return "bridge-error", f"readiness-exit-{result.returncode}"
        if time.monotonic() >= deadline:
            return "timeout", f"owner-not-quiescent-after-{maximum}s"
        time.sleep(interval)


def receipt(launch: Launch, state: str, reason: str, root: Path) -> str:
    row = None
    try:
        row = current_attempt_row(launch.jobs, launch.attempt_id)
    except JoinContractError:
        row = None
    # The row's sealed launch_home (SD-49) is the checkout the attempt actually
    # ran under; `root` (agent_home(), preferring env AGENT_HOME) may point at a
    # mutable primary checkout instead when this hook inherits that env var --
    # a harvest command built from `root` can then be rejected by a parent
    # guard that expects the sealed path. Prefer the sealed value and fall back
    # to `root` only when it is missing or no longer names a real checkout.
    sealed = row.metadata.get("launch_home") if row is not None else None
    home = Path(sealed) if sealed else root
    if not (home / "adapters" / "codex" / "bin" / "preflight.sh").is_file():
        home = root
    harvest = home / "adapters" / "codex" / "bin" / "preflight.sh"
    jobs_argument = shlex.quote(str(launch.jobs))
    status = row.status if row is not None else ""
    row_revision = child_row_revision(row) if row is not None else "unavailable"
    if state in {"ready", "attention"} and row is not None:
        required_action = required_action_for_attempt(row.status, row.metadata)
        expected_state = (
            "ready" if required_action == "advance-completed" else "attention"
        )
        if expected_state != state:
            reason = "row-advanced"
        if required_action == "complete-open":
            instruction = (
                "Use only the exact checked harvest command: "
                f"{shlex.quote(str(harvest))} harvest --jobs {jobs_argument} "
                f"--attempt-id {shlex.quote(launch.attempt_id)} --status open --mark-done."
            )
        elif required_action == "inspect-done-failure":
            instruction = (
                "Use only the exact checked harvest command: "
                f"{shlex.quote(str(harvest))} harvest --jobs {jobs_argument} "
                f"--attempt-id {shlex.quote(launch.attempt_id)} --status done --failure-detail."
            )
        else:
            instruction = "No harvest command is required; advance or finish the route."
    else:
        required_action = "inspect-bridge"
        instruction = "Inspect the typed bridge state; do not harvest or re-arm it."
    return (
        "Runtime owner completion receipt "
        f"schema=2 state={state} attempt_id={launch.attempt_id} armed={launch.armed} "
        f"status={status or '-'} row_revision={row_revision} "
        f"reason={reason} required_action={required_action}. "
        "Do not start or re-arm Background Bash, Monitor, liveness, or dispatch-wait. "
        f"{instruction} Do not emit a periodic progress recap."
    )


def main() -> int:
    try:
        payload: Any = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        return 0
    launch = parse_launch(payload) or registry_launch(payload)
    if launch is None:
        return 0
    root = agent_home()
    readiness = root / "utilities" / "dispatch-attempt-ready.py"
    if not readiness.is_file():
        print(receipt(launch, "bridge-error", "readiness-helper-missing", root), file=sys.stderr)
        return 2
    state, reason = wait_for_attempt(launch, readiness)
    print(receipt(launch, state, reason, root), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
