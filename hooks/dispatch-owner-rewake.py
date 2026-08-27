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
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    resolve_agent_home as _resolve_agent_home,
)
from dispatch_completion_join import (  # noqa: E402
    CurrentDeliveryState,
    JoinContractError,
    current_attempt_row,
    current_children,
    current_delivery_state,
    delivery_classification,
    delivery_required_action,
)


ATTEMPT = re.compile(r"att-[A-Za-z0-9._-]{1,240}\Z")
DEFAULT_INTERVAL_SECONDS = 5  # one readiness probe costs ~0.1s; 20s dominated the wake tail (2026-08-27)
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
SUCCESS_NOTIFICATION = "\x1b]9;Hearting dispatch completed\x07"


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


def _start_surface(command: str) -> str | None:
    """Recognize only the two typed depth-1 owner start command surfaces."""

    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    separators = {"|", "||", "&&", ";"}
    for index, token in enumerate(parts):
        name = Path(token).name
        if name not in {
            "dispatch-owner", "dispatch-owner.py", "dispatch-node", "dispatch-node.py"
        }:
            continue
        if index:
            launcher = Path(parts[index - 1]).name
            if re.fullmatch(r"python(?:3(?:\.\d+)?)?", launcher) is None:
                continue
        end = index + 1
        while end < len(parts) and parts[end] not in separators:
            end += 1
        arguments = parts[index + 1 : end]
        if name in {"dispatch-owner", "dispatch-owner.py"}:
            if "--start" in arguments:
                return "dispatch-owner"
            continue
        if "--action=start" in arguments:
            return "dispatch-node"
        for offset, value in enumerate(arguments[:-1]):
            if value == "--action" and arguments[offset + 1] == "start":
                return "dispatch-node"
        continue
    return None


def _owner_start_command(payload: object) -> tuple[dict[str, Any], str] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or _start_surface(command) is None:
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


def _canonical_jobs() -> str | None:
    """The installed harness's own registry — the path every wrapper writes by default.

    A launch command that spells `--jobs "$J"` hands this hook an unexpanded shell
    variable, and the hook's own environment carries no `AGENT_DISPATCH_JOBS`; both
    left a real, session-bound owner row unarmed (observed 2026-08-26, two owner
    attempts in a row). The canonical root is deterministic from the agent home, so
    it is the last resort before giving up — the row match itself stays exact."""
    try:
        from dispatch_contract import resolve_dispatch_state_root  # noqa: WPS433

        return str(resolve_dispatch_state_root(_resolve_agent_home(), None) / "jobs.log")
    except Exception:  # noqa: BLE001 — absence beats misattribution
        return None


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
    session = _payload_session(payload)
    if session is None:
        return None
    fields = _fields(_stdout(payload.get("tool_response")))
    jobs = (
        _validated_jobs(_single(fields, "job_registry"))
        or _validated_jobs(_command_jobs(command))
        or _validated_jobs(os.environ.get("AGENT_DISPATCH_JOBS"))
        or _validated_jobs(_canonical_jobs())
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


def _completion_evidence_current(state: CurrentDeliveryState) -> bool:
    """Consume the marker identity already verified inside the jobs lock."""

    marker = state.marker
    return bool(
        isinstance(marker, dict)
        and re.fullmatch(r"[0-9a-f]{64}", state.marker_digest)
        and marker.get("route_id")
        and marker.get("route_hash")
        and marker.get("node_id")
        and marker.get("attempt_id")
    )


def classified_receipt(
    launch: Launch, state: str, reason: str, root: Path
) -> tuple[str, str]:
    row = None
    delivery = None
    transaction_error = ""
    try:
        if state in {"ready", "attention"}:
            try:
                delivery = current_delivery_state(
                    launch.jobs,
                    launch.attempt_id,
                    parent_attempt_id=launch.attempt_id,
                )
            except (DispatchContractError, JoinContractError, OSError) as exc:
                transaction_error = (
                    exc.reason
                    if isinstance(exc, DispatchContractError)
                    else str(exc) or type(exc).__name__
                )
        # Rendering the sealed launch-home path is deliberately separate from
        # classification. The transaction above is the only current-row
        # decision authority.
        row = current_attempt_row(launch.jobs, launch.attempt_id)
    except (JoinContractError, OSError):
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
    status = delivery.status if delivery is not None else ""
    row_revision = delivery.row_revision if delivery is not None else "unavailable"
    marker_current = bool(delivery and _completion_evidence_current(delivery))
    if delivery is not None:
        owned_children = delivery.owned_children
    elif transaction_error:
        try:
            owned_children = sum(
                child.status in {"open", "running"}
                and child.metadata.get("registered_worker") == "1"
                and child.metadata.get("execution_surface") == "registered-headless"
                for child in current_children(launch.jobs, launch.attempt_id)
            )
        except (JoinContractError, OSError):
            owned_children = 0
    else:
        owned_children = 0
    quiescent = bool(delivery and delivery.quiescent)
    advanced = bool(delivery and delivery.advanced)
    row_digest = delivery.row_digest if delivery is not None else "unavailable"
    if (
        state in {"ready", "attention"}
        and delivery is not None
        and delivery.status in {"open", "running", "done"}
    ):
        required_action = delivery_required_action(delivery)
        snapshot_state = state
        state = delivery_classification(delivery)
        if state == "success":
            reason = (
                "row-advanced"
                if delivery.advanced or snapshot_state == "attention"
                else "terminal-complete"
            )
        else:
            reason = "terminal-failure-or-unclosed"
        if state == "success":
            instruction = "No harvest command is required; the registered owner completed."
        elif required_action == "complete-open":
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
        elif required_action == "advance-completed":
            instruction = "No harvest command is required; advance or finish the route."
        else:
            instruction = (
                "Inspect the exact current row and completion marker with: "
                f"{shlex.quote(str(harvest))} harvest --jobs {jobs_argument} "
                f"--attempt-id {shlex.quote(launch.attempt_id)} --status done "
                "--failure-detail."
            )
    elif transaction_error:
        state = "attention"
        reason = f"delivery-transaction-failed-{transaction_error}"
        required_action = "complete-open" if owned_children else "inspect-bridge"
        instruction = (
            "A real owned child remains open; inspect only the sealed registry."
            if owned_children
            else "The delivery transaction needs attention; no open child was observed, so do not block this Stop."
        )
    else:
        required_action = "inspect-bridge"
        instruction = "Inspect the typed bridge state; do not harvest or re-arm it."
    title = (
        "Hearting dispatch completed"
        if state == "success"
        else "Hearting dispatch requires attention"
    )
    message = (
        f"{title}. Runtime owner completion receipt "
        f"schema=2 state={state} attempt_id={launch.attempt_id} armed={launch.armed} "
        f"status={status or '-'} row_revision={row_revision} "
        f"row_digest={row_digest} marker_current={int(marker_current)} "
        f"quiescent={int(quiescent)} owned_children={owned_children} "
        f"advanced={int(advanced)} "
        f"reason={reason} required_action={required_action}. "
        "Do not start or re-arm Background Bash, Monitor, liveness, or dispatch-wait. "
        f"{instruction} Do not emit a periodic progress recap."
    )
    return state, message


def receipt(launch: Launch, state: str, reason: str, root: Path) -> str:
    """Compatibility text view used by tests and non-hook callers."""

    return classified_receipt(launch, state, reason, root)[1]


def emit_receipt(state: str, message: str, *, block: bool | None = None) -> int:
    """Render success/nonblocking attention without a Stop block decision."""

    if block is None:
        block = state != "success"
    if state == "success" or not block:
        payload = {"systemMessage": message}
        if state == "success":
            payload["terminalSequence"] = SUCCESS_NOTIFICATION
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 0
    print(message, file=sys.stderr)
    return 2


def _attention_has_open_child(message: str) -> bool:
    match = re.search(r"\bowned_children=([0-9]+)\b", message)
    return bool(match and int(match.group(1)) > 0)


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
        state, message = classified_receipt(
            launch, "bridge-error", "readiness-helper-missing", root
        )
        return emit_receipt(state, message, block=_attention_has_open_child(message))
    state, reason = wait_for_attempt(launch, readiness)
    state, message = classified_receipt(launch, state, reason, root)
    return emit_receipt(state, message, block=_attention_has_open_child(message))


if __name__ == "__main__":
    raise SystemExit(main())
