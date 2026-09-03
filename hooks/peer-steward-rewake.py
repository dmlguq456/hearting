#!/usr/bin/env python3
"""Wake a Claude steward session once when its exact detached watch completes.

SD-122 §13.37.2-(10), Claude carrier. `PostToolUse(Bash)` with
`asyncRewake: true`: the hook runs in the background and wakes Claude only on
exit code 2, showing stderr (or stdout when stderr is empty) as a system
reminder. Exit 0 output is not delivered until the next user interaction, which
from a parked session is indistinguishable from a lost wake -- so every terminal
receipt exits 2, exactly as `dispatch-owner-rewake.py` does.

It never injects a synthetic user turn (core/HOOKS.md forbids it), and it never
opens the watch state itself: every read and mutation goes through
`utilities/peer-steward.py status|join|rearm|ack`. That boundary is what keeps
Codex/OpenCode parity a carrier problem rather than a schema fork.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any

_HOOKS_DIR = Path(__file__).resolve().parent
_PEER_STEWARD = _HOOKS_DIR.parent / "utilities" / "peer-steward.py"

BUDGET_SECONDS = 21_600          # matches the settings.json timeout
BUDGET_MARGIN_SECONDS = 60       # 21600 - 60 s = 21_540_000 ms
SUBPROCESS_TIMEOUT_SECONDS = 15   # status/ack/rearm only; never the join
NOTICE = "화면·디스크를 읽고 판단(idle ≠ done)"


def _bounded_number(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _fields(output: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in output.splitlines():
        for token in line.strip().split():
            key, separator, value = token.partition("=")
            if not separator or not re.fullmatch(r"[a-z][a-z0-9_.-]*", key):
                continue
            result.setdefault(key, []).append(value)
    return result


def _single(fields: dict[str, list[str]], key: str) -> str | None:
    values = fields.get(key, [])
    return values[0] if len(values) == 1 else None


def _is_watch_command(command: str) -> bool:
    """Recognize only `… peer-steward.py watch …`. Any other Bash call exits 0."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    for index, token in enumerate(parts):
        if Path(token).name not in {"peer-steward", "peer-steward.py"}:
            continue
        rest = [p for p in parts[index + 1:] if not p.startswith("-")]
        return bool(rest) and rest[0] == "watch"
    return False


def _run(*argv: str, timeout: float | None = None) -> subprocess.CompletedProcess[str] | None:
    """Run one utility call. `status`/`ack`/`rearm` are short and get the 15 s
    cap; the `join` call is the one that legitimately blocks for hours and must
    pass its own bound (review B1, 2026-09-03: a 15 s cap on `join` returned
    None after 15 s for every real watch and the hook exited 0 -- no wake)."""
    cap = _bounded_number(
        "AGENT_PEER_STEWARD_REWAKE_SUBPROCESS_SECONDS", SUBPROCESS_TIMEOUT_SECONDS, 1, 600
    ) if timeout is None else timeout
    try:
        return subprocess.run(
            [sys.executable, str(_PEER_STEWARD), *argv],
            capture_output=True, text=True, timeout=cap,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def parse_arm(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Return `(watch_id, target)` for the watch this hook owns, else None.

    Fail-closed on every condition: a mismatch is not an error to report, it is
    simply not this hook's watch.
    """
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not _is_watch_command(command):
        return None
    response = payload.get("tool_response")
    stdout = response.get("stdout") if isinstance(response, dict) else None
    if not isinstance(stdout, str):
        return None

    fields = _fields(stdout)
    if _single(fields, "state") != "armed":
        return None
    if _single(fields, "wake") != "hook":
        return None
    watch_id = _single(fields, "watch_id")
    if not watch_id or not re.fullmatch(r"[0-9a-f]{16}", watch_id):
        return None

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None

    status = _run("status", "--watch", watch_id, "--json")
    if status is None or status.returncode != 0:
        return None
    try:
        state = json.loads(status.stdout)
    except (ValueError, TypeError):
        return None
    watches = state.get("watches") or []
    if len(watches) != 1:
        return None
    entry = watches[0]
    if (entry.get("steward") or {}).get("session_id") != session_id:
        return None

    # The receipt must live under the canonical state root the utility itself
    # reports -- never under a path taken from the command line.
    watch_root = state.get("watch_root")
    receipt = entry.get("receipt")
    if not watch_root or not receipt:
        return None
    receipt_path = Path(receipt)
    if not receipt_path.is_absolute() or receipt_path.parent != Path(watch_root):
        return None
    return watch_id, str(entry.get("target") or "-")


def notice(watch_id: str, target: str, line: str, *, rearmed: int) -> str:
    fields = _fields(line)

    def one(key: str, default: str = "-") -> str:
        return _single(fields, key) or default

    message = (
        f"[peer-steward-rewake] watch_id={watch_id} state={one('state')} "
        f"target={target} agent={one('agent')} session_id={one('session_id')} "
        f"name={one('name')} pane={one('pane')} receipt={one('receipt')}"
    )
    if rearmed:
        message += f" rearmed={rearmed}"
    return f"{message} — {NOTICE}"


def emit(message: str) -> int:
    """Every terminal receipt exits 2: that is the only code that wakes Claude.

    Flushed before the caller records any ack: the ack is what silences the
    prompt sweep, so it must never precede the wake output (review M2)."""
    print(json.dumps({"systemMessage": message}, ensure_ascii=False, separators=(",", ":")))
    print(message, file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    return 2


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    armed = parse_arm(payload)
    if armed is None:
        return 0
    watch_id, target = armed

    # One deadline, computed once. A rearm must never restart it: a restarted
    # clock pushes the hook past the runtime's own kill and the session gets no
    # wake at all, which is the exact failure this cycle exists to remove.
    budget = _bounded_number(
        "AGENT_PEER_STEWARD_REWAKE_MAX_SECONDS", BUDGET_SECONDS, 60, 86_400
    )
    deadline = time.monotonic() + budget - BUDGET_MARGIN_SECONDS

    def remaining_ms() -> str:
        return str(int(max(1.0, deadline - time.monotonic()) * 1000))

    # At most two joins: the original watch and, if its watcher died, exactly one
    # rearm. A bounded `for` rather than `while True` -- the bound is part of the
    # contract, and it keeps the "no poll loop in product code" assertion honest.
    rearmed = 0
    for attempt in range(2):
        # The join blocks for the whole remaining budget; its own `--timeout`
        # is the bound, and the subprocess cap only backstops a wedged utility.
        join_cap = max(1.0, deadline - time.monotonic()) + BUDGET_MARGIN_SECONDS
        joined = _run("join", watch_id, "--timeout", remaining_ms(), timeout=join_cap)
        if joined is None:
            return 0
        code = joined.returncode

        if code in (0, 2, 3, 4):                      # a receipt exists
            # Wake first, ack second: an ack with no wake behind it would
            # permanently disarm the prompt-sweep fallback (review M2).
            result = emit(notice(watch_id, target, joined.stdout, rearmed=rearmed))
            _run("ack", watch_id, "--carrier", "claude-async-rewake")
            return result

        if code == 6:                                 # hook budget exhausted
            return emit(
                f"[peer-steward-rewake] watch_id={watch_id} state=hook-budget-expired "
                f"target={target} — watcher may still be alive; join or rearm from the session"
            )

        if code != 5:                                 # unclassified: stay silent
            return 0

        if attempt == 0:                              # watcher died: rearm once
            again = _run("rearm", watch_id)
            fields = _fields(again.stdout) if again else {}
            new_id = _single(fields, "watch_id")
            if again and again.returncode == 0 and _single(fields, "state") == "rearmed" and new_id:
                watch_id, rearmed = new_id, 1
                continue
        # First death that could not be rearmed, or the second death: attention.
        return emit(notice(watch_id, target, joined.stdout, rearmed=rearmed))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
