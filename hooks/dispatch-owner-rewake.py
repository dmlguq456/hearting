#!/usr/bin/env python3
"""Wake an interactive Claude parent once when its exact headless owner finishes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    _runtime_ancestry_proc_stat as _proc_stat,
    process_namespace_identity,
    resolve_agent_home as _resolve_agent_home,
    runtime_ancestry_binding,
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
import dispatch_pending_delivery as pending_delivery  # noqa: E402
from dispatch_session_sweep import (  # noqa: E402
    HUMAN_GATE_PREFIX,
    _bounded_receipt_text,
    is_human_gate_record,
)
# SD-111 P3 §4.4: carrier 1 must never create a pending-delivery record --
# it only claims one trigger 1/2 already produced. The materializer function
# from dispatch_completion_join is deliberately absent from this import
# block; DispatchOwnerRewakeMaterializeAbsenceTest statically asserts that.


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
CLAIM_LEASE_SECONDS = 30.0


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


# ---------------------------------------------------------------------------
# Attempt identity (2026-09-09). This hook never reads the Bash command text.
# Six consecutive reviews of the previous cycle (codex R3/R5/R6/R7, Fable,
# OpenCode) each found a new hole in the shell parsing that decided which
# owner a command had started -- `echo … --start`, repeated flags, `arg;`
# without whitespace, `2>&1`, `$( )`, `time`/`nohup`/`bash -c` prefixes.
# Parsing shell is re-implementing shell. What the launch actually did is
# on disk: the wrapper appends a claimed-and-started depth-1 owner row bound
# to this session (`parent_sid`) before `dispatch-owner --start` returns, and
# the start receipt names the same `attempt_id`. The hook identifies the
# owner from those two, and an *arm ledger* -- one file per attempt under
# the registry's state root -- makes arming exactly-once: a second hook
# process (a later Bash call, a parallel tool call, a foreign command that
# merely mentions the utility) finds the claim held and arms nothing.
# ---------------------------------------------------------------------------

ARM_DIRECTORY = "rewake-arms"
ARM_LIMIT = 8  # same finite discipline as dispatch_pending_delivery.RECLAIM_LIMIT
ARM_SCHEMA = 1
ARM_STATES = frozenset({"waiting", "gate-wake-sent", "lapsed", "ended"})


@dataclass(frozen=True)
class ArmClaim:
    """The arm-ledger record this hook process holds for one attempt."""

    path: Path
    attempt_id: str
    session_id: str
    arms: int
    holder: tuple[str, str, str]


@dataclass(frozen=True)
class ArmRefusal:
    """Why `claim_arm` did not take the claim. `watched` reasons mean the
    attempt's wake is already someone's job (a live holder, or a gate wake
    spent on a record that is still open); every other reason means nothing
    is watching -- the caller says so (review R1 M2)."""

    reason: str

    WATCHED = frozenset({"held-live", "gate-open", "ended"})

    @property
    def watched(self) -> bool:
        return self.reason in self.WATCHED


ARM_RETENTION_ENDED_SECONDS = 7 * 86_400
ARM_RETENTION_ANY_SECONDS = 30 * 86_400
ARM_PRUNE_SCAN_LIMIT = 256
ARM_PRUNE_CURSOR = ".prune-cursor"


def _bash_call(payload: object) -> tuple[dict[str, Any], str] | None:
    """`(payload, session_id)` for a PostToolUse Bash call of a real session."""

    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Bash":
        return None
    if not isinstance(payload.get("tool_input"), dict):
        return None
    session = _payload_session(payload)
    if session is None:
        return None
    return payload, session


def parse_launch(payload: object) -> Launch | None:
    """The receipt fast path: a successful depth-1 owner start whose stdout
    names the attempt, the registry, and this session as the parent."""

    gate = _bash_call(payload)
    if gate is None:
        return None
    payload, payload_session = gate
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
    if parent_session != payload_session or attempt_id is None or ATTEMPT.fullmatch(attempt_id) is None:
        return None
    if _single(fields, "job_registry") is None:
        return None
    jobs = _resolved_jobs(payload)  # the receipt may name the trusted registry, never replace it
    if jobs is None:
        return None
    return Launch(attempt_id=attempt_id, jobs=jobs, session_id=payload_session, armed="stdout")


def receipt_row_age(launch: Launch) -> float | None:
    """The registry's own proof for a receipt-named attempt: its age when the
    row exists, is bound to this session, and carries the owner-start stamps
    (`open`, or `done` for an owner that finished before the hook ran); None
    otherwise. Review R1 B1: without this, any Bash output shaped like a
    start receipt could arm a foreign, depth-2, unstarted, or invented
    attempt -- the command-surface check that used to stand in front of the
    receipt is gone, so the row has to."""

    for attempt_id, age in _session_owner_rows(
        launch.jobs, launch.session_id, statuses=RECEIPT_ROW_STATUSES
    ):
        if attempt_id == launch.attempt_id:
            return age
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

    The hook's own environment may carry no `AGENT_DISPATCH_JOBS` and a filtered
    stdout names no `job_registry`; both left a real, session-bound owner row
    unarmed (observed 2026-08-26, two owner attempts in a row). The canonical root
    is deterministic from the agent home, so it is the last resort before giving
    up — the row match itself stays exact."""
    try:
        from dispatch_contract import resolve_dispatch_state_root  # noqa: WPS433

        return str(resolve_dispatch_state_root(_resolve_agent_home(), None) / "jobs.log")
    except Exception:  # noqa: BLE001 — absence beats misattribution
        return None


def _trusted_jobs() -> Path | None:
    """The one registry this session trusts: the inherited `AGENT_DISPATCH_JOBS`
    (immutable for the session, OPERATIONS §5.10) when the variable is set --
    an unusable value (symlink, missing, not a regular file) trusts nothing,
    never the canonical registry in its place (review R3 B1) -- and the
    installed harness's canonical registry only when the variable is absent.
    Nothing a Bash call prints can replace it."""

    inherited = os.environ.get("AGENT_DISPATCH_JOBS")
    if inherited is not None:
        return _validated_jobs(inherited)
    return _validated_jobs(_canonical_jobs())


def _resolved_jobs(payload: dict[str, Any]) -> Path | None:
    """The registry this call is bound to: the trusted registry, and only it.

    A receipt's `job_registry` is accepted when it names that same file
    (review R2 B1: a receipt that pointed at any writable regular file made
    that file's self-described rows the identity proof, so receipt and row
    were one attacker-controlled input); a receipt naming another file binds
    nothing. A `--jobs` literal in the command is deliberately not read."""

    trusted = _trusted_jobs()
    raw = _single(_fields(_stdout(payload.get("tool_response"))), "job_registry")
    if raw is not None:
        named = _validated_jobs(raw)
        if named is None or trusted is None or named.resolve(strict=False) != trusted.resolve(strict=False):
            return None
    return trusted


ARM_ROW_STATUSES = frozenset({"open"})
RECEIPT_ROW_STATUSES = frozenset({"open", "done"})


def _read_registry_lines(jobs: Path) -> list[str] | None:
    """Read the trusted registry without following a symlink placed at its
    path after validation (top review N1): the descriptor is opened with
    O_NOFOLLOW and must be a regular file, so a swapped-in link is refused
    at read time rather than followed."""

    try:
        fd = os.open(jobs, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
            fd = -1
            return handle.read().splitlines()
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _session_owner_rows(
    jobs: Path, session: str, *, statuses: frozenset[str] = ARM_ROW_STATUSES
) -> list[tuple[str, float]]:
    """Every claimed-and-started depth-1 owner row bound to `session` whose
    latest status is in `statuses`, as ``(attempt_id, age_seconds)``, oldest
    first. Empty on any refusal. This is the one identity check both arming
    paths share (review R1 B1): a receipt on stdout only *names* a candidate;
    the row proves it -- exists, `parent_sid` is this session, every
    `REGISTRY_OWNER_START` key matches. A receipt may name a row that already
    ran to `done` (a short owner finishing before the hook ran). The registry
    path takes a *new* claim only on an open row; a row that ran to `done`
    while this session already held its claim is still re-armable, because
    the wake it owes was never delivered (top review M1)."""

    lines = _read_registry_lines(jobs)
    if lines is None:
        return []
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
    now = time.time()
    rows: list[tuple[str, float]] = []
    for attempt_id, (stamp, status, metadata) in latest.items():
        if status not in statuses or metadata.get("parent_sid") != session:
            continue
        if any(metadata.get(key) != value for key, value in REGISTRY_OWNER_START.items()):
            continue
        age = _row_age(stamp, now)
        if age is None or age < -MAXIMUM_CLOCK_SKEW_SECONDS:
            continue
        rows.append((attempt_id, age))
    rows.sort(key=lambda row: -row[1])
    return rows


def _arm_window() -> int:
    return _bounded_number(
        "AGENT_CLAUDE_REWAKE_ARM_WINDOW_SECONDS", DEFAULT_ARM_WINDOW_SECONDS, 30, 86_400
    )


def registry_launch(payload: object) -> tuple[Launch, ArmClaim] | ArmRefusal | None:
    """Arm from the wrapper-written registry: the first open depth-1 owner row
    bound to this session whose arm claim this hook process can take.

    A wave of same-session owner starts is legitimate: each Bash call's hook
    takes one unclaimed row (oldest first), so five starts in five calls arm
    five waiters, and a start whose stdout was filtered away is armed by its
    own call or the next one. A row older than the arm window is a first-time
    refusal (the next-prompt sweep owns stale completions); a row whose claim
    lapsed -- its holder died with the session, or its gate wake was spent and
    the gate has since closed -- is re-armed regardless of age."""

    gate = _bash_call(payload)
    if gate is None:
        return None
    payload, session = gate
    jobs = _resolved_jobs(payload)
    if jobs is None:
        return None
    window = _arm_window()
    named = _single(_fields(_stdout(payload.get("tool_response"))), "attempt_id")
    refusal: ArmRefusal | None = None
    open_rows = {attempt_id for attempt_id, _age in _session_owner_rows(jobs, session)}
    # A row that already ran to `done` is never a *new* claim (its completion
    # is the sweep's), but one whose claim this session already holds -- a
    # spent gate wake, a dead holder, a lapse -- is still owed its wake and
    # may have finished between the release and this call (top review M1).
    for attempt_id, age in _session_owner_rows(jobs, session, statuses=RECEIPT_ROW_STATUSES):
        fresh = attempt_id in open_rows and age <= window
        if attempt_id not in open_rows and not arm_path(jobs, attempt_id).exists():
            continue
        claim = claim_arm(jobs, attempt_id, session, fresh=fresh)
        if isinstance(claim, ArmRefusal):
            # Keep the receipt-named attempt's reason above any other's
            # (review R2 M2): the notice must say why *that* start did not arm.
            if refusal is None or attempt_id == named:
                refusal = claim
            continue
        armed = "registry" if claim.arms == 1 else "registry-rearm"
        return Launch(attempt_id=attempt_id, jobs=jobs, session_id=session, armed=armed), claim
    return refusal


def _process_identity(pid: int) -> tuple[str, str, str] | None:
    """`(pid, start_ticks, pid_ns)` of one live process, or None."""

    found = _proc_stat(pid)
    namespace = process_namespace_identity(pid)
    if found is None or not namespace:
        return None
    return (str(pid), str(found["start"]), namespace)


def _holder_alive(holder: object) -> bool:
    """Whether the recorded holder triple still names a live process.

    Dead means *provably* gone: the pid no longer exists, or it exists with a
    different start time or pid namespace (pid reuse). A record that cannot
    be parsed, or a live pid whose identity cannot be read right now
    (a transient /proc failure -- review R1 M1), counts as alive: a duplicate
    waiter is the failure mode arming exists to prevent, a missed re-arm is
    what the next-prompt sweep exists to cover."""

    if not isinstance(holder, list) or len(holder) != 3 or not all(isinstance(v, str) and v for v in holder):
        return True
    try:
        pid = int(holder[0])
    except ValueError:
        return True
    if holder[2] != process_namespace_identity(os.getpid()):
        # The pid is a coordinate in the holder's own PID namespace; from
        # another one the same number names someone else and its absence
        # proves nothing (top review M3) -- unobservable counts as alive.
        return True
    if not Path(f"/proc/{pid}").exists():
        return False
    observed = _process_identity(pid)
    if observed is None:
        return True
    return list(observed) == holder


def arm_directory(jobs: Path) -> Path:
    return jobs.resolve(strict=False).parent / ARM_DIRECTORY


def arm_path(jobs: Path, attempt_id: str) -> Path:
    return arm_directory(jobs) / f"{attempt_id}.json"


def _read_arm(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"state": "unreadable"}
    return value if isinstance(value, dict) else {"state": "unreadable"}


def _write_arm(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _gate_record_open(jobs: Path, session: str, delivery_id: object) -> bool:
    """Whether the gate record a spent wake announced is still open.

    Only a record that was read and carries a closed state counts as closed.
    An unnamed, unreadable, or *missing* record counts as open (review R1 B2:
    `pending_delivery.read` returns None for a missing file, and treating
    that as closed re-armed a waiter with no closing evidence)."""

    if not isinstance(delivery_id, str) or not delivery_id:
        return True
    root = jobs.resolve(strict=False).parent
    try:
        record = pending_delivery.read(root, session, delivery_id)
    except pending_delivery.PendingDeliveryError:
        return True
    if not isinstance(record, dict):
        return True
    return record.get("state") in pending_delivery.OPEN_STATES


def _reclaim_refusal(existing: dict[str, Any], jobs: Path, session: str) -> str | None:
    """None when the existing record may be re-taken, else the typed reason."""

    if existing.get("state") == "unreadable":
        return "unreadable"
    if existing.get("schema") != ARM_SCHEMA:
        return "unreadable"
    if existing.get("session_id") != session:
        return "foreign-session"
    arms = existing.get("arms")
    if not isinstance(arms, int):
        return "unreadable"
    if arms >= ARM_LIMIT:
        return "exhausted"
    state = existing.get("state")
    if state == "ended":
        return "ended"
    if state not in ARM_STATES:
        return "unreadable"
    if _holder_alive(existing.get("holder")):
        return "held-live"
    if state == "gate-wake-sent" and _gate_record_open(jobs, session, existing.get("gate_delivery_id")):
        return "gate-open"
    return None  # waiting with a dead holder, lapsed, or a gate that has closed


def _reclaimable(existing: dict[str, Any], jobs: Path, session: str) -> bool:
    return _reclaim_refusal(existing, jobs, session) is None


def _prune_arm_directory(directory: Path, now: float) -> None:
    """Bounded retention for the ledger (review R1 M3), run under the ledger
    lock by a process that just took a claim. An `ended` record older than
    seven days is deleted; a record older than thirty days is deleted only
    when it is `lapsed`, unreadable, or held by a provably dead process -- a
    `waiting`/`gate-wake-sent` record whose holder is alive or unobservable
    is never touched, however old (review R2 M1: the lock serialises file
    access, it does not prove a holder dead). At most `ARM_PRUNE_SCAN_LIMIT`
    entries are examined per claim; the lock file itself is never removed."""

    try:
        names = sorted(entry.name for entry in directory.glob("att-*.json"))
    except OSError:
        return
    if not names:
        return
    # A rotating cursor (review R3 M2): a run scans the window that follows
    # the last scanned name and records where it stopped, so a prefix of
    # preserved live-holder records cannot starve the expired records behind
    # it. The cursor file is best-effort; a missing or stale one restarts at
    # the beginning, never skips deletion of anything.
    cursor = directory / ARM_PRUNE_CURSOR
    try:
        last = cursor.read_text(encoding="utf-8").strip()
    except OSError:
        last = ""
    start = next((index for index, name in enumerate(names) if name > last), 0)
    window = (names[start:] + names[:start])[:ARM_PRUNE_SCAN_LIMIT]
    try:
        cursor.write_text(window[-1], encoding="utf-8")
    except OSError:
        pass
    for entry in (directory / name for name in window):
        try:
            age = now - entry.stat().st_mtime
            if age < ARM_RETENTION_ENDED_SECONDS:
                continue
            record = _read_arm(entry) or {}
            state = record.get("state")
            if state == "ended":
                entry.unlink()
            elif age >= ARM_RETENTION_ANY_SECONDS and (
                state in {"lapsed", "unreadable"} or not _holder_alive(record.get("holder"))
            ):
                entry.unlink()
        except OSError:
            continue


def claim_arm(jobs: Path, attempt_id: str, session: str, *, fresh: bool) -> ArmClaim | ArmRefusal:
    """Take the arm claim for `attempt_id` under the ledger lock.

    A missing record is created only for a `fresh` row (started inside the arm
    window); an existing record is re-taken only when `_reclaim_refusal` finds
    no reason. Returns an `ArmRefusal` naming the reason otherwise -- another
    hook process holds the wait, the wake was spent and its gate is still
    open, the attempt ended, the budget is spent, or the ledger could not be
    read or written -- and never raises."""

    identity = _process_identity(os.getpid())
    if identity is None:
        return ArmRefusal("identity-unavailable")
    path = arm_path(jobs, attempt_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = os.open(path.parent / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return ArmRefusal("io-error")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            existing = _read_arm(path)
            if existing is None:
                if not fresh:
                    return ArmRefusal("not-fresh")
                arms = 1
            else:
                refusal = _reclaim_refusal(existing, jobs, session)
                if refusal is not None:
                    return ArmRefusal(refusal)
                arms = int(existing["arms"]) + 1
            record = {
                "schema": ARM_SCHEMA,
                "attempt_id": attempt_id,
                "session_id": session,
                "holder": list(identity),
                "state": "waiting",
                "arms": arms,
                "gate_delivery_id": None,
                "armed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            _write_arm(path, record)
            _prune_arm_directory(path.parent, time.time())
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        return ArmRefusal("io-error")
    finally:
        os.close(lock)
    return ArmClaim(path=path, attempt_id=attempt_id, session_id=session, arms=arms, holder=identity)


def settle_arm(claim: ArmClaim, state: str, *, gate_delivery_id: str | None = None) -> bool:
    """Record how this hook process's wait ended, if it still holds the claim.

    `ended` is permanent (a terminal receipt was emitted); `gate-wake-sent`
    names the gate record whose closing lets a later Bash call re-arm;
    `lapsed` (timeout, bridge error) lets the next call re-arm at once."""

    if state not in ARM_STATES:
        return False
    try:
        lock = os.open(claim.path.parent / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            existing = _read_arm(claim.path)
            if existing is None or existing.get("holder") != list(claim.holder):
                return False
            existing["state"] = state
            existing["gate_delivery_id"] = gate_delivery_id
            existing["settled_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            _write_arm(claim.path, existing)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        return False
    finally:
        os.close(lock)
    return True


def agent_home() -> Path:
    return _resolve_agent_home()


def _bounded_number(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def wait_for_attempt(
    launch: Launch, readiness: Path, *, gate_probe: Any = None, deadline: float | None = None
) -> tuple[str, str]:
    """Poll the exact attempt to terminal quiescence.

    `gate_probe` (SD-129) is consulted once per interval while the attempt is
    still open: when it reports an open human gate addressed to this attempt,
    the wait ends with `("gate", "human-gate-open")` so the caller can wake the
    session NOW instead of at the owner's terminal. Before this cycle the gate
    rode only the terminal wake, so an owner that stayed alive at its gate --
    the correct behaviour -- reached the person only through the next-prompt
    sweep (measured 2026-09-04, rt-da62cded: 3.5 minutes, and only because the
    user happened to type).
    """
    interval = _bounded_number(
        "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS, 1, 300
    )
    maximum = _bounded_number(
        "AGENT_CLAUDE_REWAKE_MAX_SECONDS", DEFAULT_MAX_SECONDS, interval, 86_400
    )
    # The deadline is the caller's when it re-enters after a gate probe (review
    # round 1, B3): recomputing it here on every re-entry made the bound
    # unreachable.
    if deadline is None:
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
        if gate_probe is not None and gate_probe():
            return "gate", "human-gate-open"
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
        elif required_action.startswith(HUMAN_GATE_PREFIX):
            instruction = (
                "A human gate is open; it is answered, not harvested. Read the artifact named "
                "in the gate record, then record the answer with "
                "workflow-supervisor.py release --route <route file> --gate "
                f"{shlex.quote(required_action[len(HUMAN_GATE_PREFIX):] or '-')} "
                "--decision proceed|revise|stop."
            )
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


TERMINAL_STATES = frozenset({"success", "attention"})


def emit_receipt(state: str, message: str, *, block: bool | None = None) -> int:
    """Render the receipt and choose the exit code that actually wakes Claude.

    Claude Code delivers an `asyncRewake` hook's exit-0 output only "on the
    next conversation turn" -- an idle session stays asleep until the user
    types -- and wakes the session immediately only on exit code 2, showing
    stderr (or stdout when stderr is empty) as a system reminder
    (code.claude.com/docs/en/hooks, "Run hooks in the background"). Until
    2026-08-29 a completed owner exited 0 here, so every successful
    completion waited for the next user prompt and looked like a lost wake
    (five observed "gap 4" incidents). Every terminal receipt -- success or
    attention -- now exits 2 with the receipt on stderr; the structured
    stdout payload is kept for success and non-blocking attention so a
    transcript reader sees the same notice as before. Non-terminal bridge
    states (timeout, bridge-error) keep exit 0: there is nothing to wake for.
    """

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
        if state in TERMINAL_STATES:
            print(message, file=sys.stderr)
            return 2
        return 0
    print(message, file=sys.stderr)
    return 2


def _attention_has_open_child(message: str) -> bool:
    match = re.search(r"\bowned_children=([0-9]+)\b", message)
    return bool(match and int(match.group(1)) > 0)


def _incarnation_binding_matches(metadata: dict[str, str]) -> bool:
    """SD-111 P2 round 2 C-3: the recorded launch-time triple must match this
    hook process's own walk exactly on all three fields. Absence, a partial
    recording, or any mismatch is a fail-closed no (§3.2.1's fork)."""

    recorded = (
        metadata.get("parent_runtime_pid", ""),
        metadata.get("parent_runtime_pid_start", ""),
        metadata.get("parent_runtime_ns", ""),
    )
    if not all(recorded):
        return False
    observed = runtime_ancestry_binding(os.getpid())
    if observed is None:
        return False
    return recorded == observed


@dataclass(frozen=True)
class ClaimWin:
    claim_owner: str
    recipient_key: str
    delivery_id: str
    root: Path


def _delivery_owing_row(launch: Launch) -> dict[str, str] | None:
    """Return the row's metadata only when it is a genuine delivery-owing
    terminal completion (`done` + `delivery_intent` stamped, §4.3.1) -- the
    claim gate governs exactly this population. A still-open/running row
    (e.g. `wait_for_attempt` timed out or hit a bridge error) has taken no
    terminal edge yet and carries no intent; gating *that* notice behind
    claim would silence a live diagnostic forever, which is the opposite of
    what SD-111 exists to prevent, so it is deliberately left ungated."""

    try:
        row = current_attempt_row(launch.jobs, launch.attempt_id)
    except (JoinContractError, OSError):
        return None
    if row is None or row.status != "done" or row.metadata.get("delivery_intent") != "1":
        return None
    return row.metadata


def _carrier_one_claim(launch: Launch, metadata: dict[str, str]) -> ClaimWin | None:
    """P3 claim gate. Never creates a record (§4.4 -- carrier 1 only claims
    what trigger 1/2 already materialized); returns the winning claim on
    success, or ``None`` on any refusal -- claim lost, record already
    acked/claimed, record not yet materialized, or the incarnation binding
    does not match. Every ``None`` path is a silent, zero-notice exit-0 per
    SD-97 (no re-delivery)."""

    if not _incarnation_binding_matches(metadata):
        return None
    delivery_id = metadata.get("delivery_id", "")
    recipient_key = metadata.get("parent_sid", "")
    if not delivery_id or not recipient_key:
        return None
    root = launch.jobs.resolve(strict=False).parent
    claim_owner = f"claude-async-rewake:{os.getpid()}:{time.monotonic_ns()}"
    try:
        pending_delivery.claim(
            root,
            recipient_key,
            delivery_id,
            claim_owner=claim_owner,
            lease_seconds=CLAIM_LEASE_SECONDS,
            require_generation_proof=False,
        )
    except pending_delivery.PendingDeliveryError:
        return None
    return ClaimWin(claim_owner, recipient_key, delivery_id, root)


_RECIPIENT_KEY_CACHE: dict[tuple[str, str], str] = {}


def _recipient_key(launch: Launch) -> str:
    """The owner row's `parent_sid`, read from the registry once per hook
    process: the row's recipient never changes, and re-reading `jobs.log` every
    interval for the owner's whole life was the per-interval cost review round
    1 (minor 9) measured."""

    key = (str(launch.jobs), launch.attempt_id)
    cached = _RECIPIENT_KEY_CACHE.get(key)
    if cached:
        return cached
    try:
        row = current_attempt_row(launch.jobs, launch.attempt_id)
    except (JoinContractError, OSError):
        return ""
    recipient_key = row.metadata.get("parent_sid", "") if row is not None else ""
    if recipient_key:
        _RECIPIENT_KEY_CACHE[key] = recipient_key
    return recipient_key


def _recipient_gate_records(launch: Launch) -> list[tuple[Path, str, str, dict]]:
    """Every open gate record addressed to this launch's depth-0 session:
    `(root, recipient_key, delivery_id, record)`; empty on any refusal."""

    recipient_key = _recipient_key(launch)
    if not recipient_key:
        return []
    root = launch.jobs.resolve(strict=False).parent
    try:
        directory = pending_delivery.record_directory(root, recipient_key)
        entries = sorted(p for p in directory.glob("delivery-*.json") if p.is_file())
    except (pending_delivery.PendingDeliveryError, OSError):
        return []
    found: list[tuple[Path, str, str, dict]] = []
    for entry in entries:
        delivery_id = entry.stem
        try:
            record = pending_delivery.read(root, recipient_key, delivery_id)
        except pending_delivery.PendingDeliveryError:
            continue
        if record is None or record.get("state") not in pending_delivery.OPEN_STATES:
            continue
        if not is_human_gate_record(record) and record.get("receipt", {}).get("kind") != "supervision":
            continue
        found.append((root, recipient_key, delivery_id, record))
    return found


_PROBE_DIRECTORY_MTIME: dict[str, float] = {}
_PROBE_NEXT_DEADLINE_NS: dict[str, int] = {}


def _open_gate_pending(launch: Launch) -> bool:
    """SD-129 probe for `wait_for_attempt`: is a gate record for THIS attempt
    waiting for a carrier? `pending`, or a lease another carrier let expire,
    and still claimable (`attempts` below `RECLAIM_LIMIT`; a record whose
    reclaim budget is spent is left to the release-time retirement, so the
    hook never spins on something it can never claim -- review round 1, B3).
    A live claim by the sweep is left alone -- it acks within its own turn.

    The recipient directory is only scanned when its mtime moved since the
    last probe -- a record being created, claimed or acked rewrites a file in
    it -- or when the earliest live lease seen at the last scan has now expired
    (a lease expiring is a clock event with no write; review round 2, N1).
    """

    recipient_key = _recipient_key(launch)
    if not recipient_key:
        return False
    root = launch.jobs.resolve(strict=False).parent
    try:
        directory = pending_delivery.record_directory(root, recipient_key)
        mtime = directory.stat().st_mtime
    except (pending_delivery.PendingDeliveryError, OSError):
        return False
    key = str(directory)
    now = time.monotonic_ns()
    next_deadline = _PROBE_NEXT_DEADLINE_NS.get(key)
    if _PROBE_DIRECTORY_MTIME.get(key) == mtime and (next_deadline is None or now < next_deadline):
        return False
    _PROBE_DIRECTORY_MTIME[key] = mtime
    _PROBE_NEXT_DEADLINE_NS.pop(key, None)
    for _root, _key, _delivery_id, record in _recipient_gate_records(launch):
        if launch.attempt_id not in (record.get("attempt_ids") or []):
            continue
        if (record.get("attempts") or 0) >= pending_delivery.RECLAIM_LIMIT:
            continue
        state = record.get("state")
        if state == "pending":
            return True
        if state in {"claimed", "sent-ambiguous"}:
            deadline = int(record.get("claim_deadline_ns") or 0)
            if deadline < now:
                return True
            _PROBE_NEXT_DEADLINE_NS[key] = min(_PROBE_NEXT_DEADLINE_NS.get(key, deadline), deadline)
    return False


def _gate_notices(
    launch: Launch, *, attempt_only: bool = False, settle: str = "ack",
    announced: list[str] | None = None,
) -> list[str]:
    """SD-123 (8)(b) carrier 1: fold every open gate record for this recipient
    into the wake this hook is about to emit.

    Two call sites since SD-129. The terminal wake still folds in every open
    gate for the recipient (contract (c): a gate outlives its owner). The
    in-wait wake (`wait_for_attempt` returned `gate`) passes `attempt_only` so
    it announces only the gate this owner raised -- a parallel owner's gate is
    that owner's hook's wake, and spending this process's single wake on it
    would lose this attempt's completion notice.

    `settle` is how the announced record is left, and both call sites pass
    `sent-ambiguous` (review round 1, M6): whether an exit-2 wake reaches the
    session is unmeasured (SD-OPEN-29/32), and an acked record would have
    spent the sweep fallback the contract still requires. The terminal caller
    (`_emit_with_gates`) acks separately once its receipt has actually gone
    out, so a gate folded into a delivered terminal receipt is not
    re-announced (A59-3) while one whose receipt never went out still is. The
    cost is bounded at-least-once: if the person has not released by the next
    prompt the sweep shows the same gate once more.
    """

    notices: list[str] = []
    for root, recipient_key, delivery_id, record in _recipient_gate_records(launch):
        if attempt_only and launch.attempt_id not in (record.get("attempt_ids") or []):
            continue
        claim_owner = f"claude-async-rewake-gate:{os.getpid()}:{time.monotonic_ns()}"
        try:
            if record.get("state") in {"claimed", "sent-ambiguous"}:
                # `now_ns` is keyword-only and required. Omitting it raised
                # TypeError, which the `except PendingDeliveryError` below does
                # NOT catch — so the whole rewake hook died and the parent lost
                # its completion receipt entirely, not just the gate. Reachable
                # whenever a sweep claimed the record first, an ack failed, two
                # parallel-group rewakes raced, or after `mark_sent_ambiguous`.
                # The sibling call in `dispatch_session_sweep.sweep_deliver`
                # always passed it; this one never did, and no test covered it.
                # `monotonic_ns`, not `time_ns`: `reclaim` compares `now_ns`
                # against `claim_deadline_ns`, which `claim` stamps from
                # `time.monotonic_ns()`. An epoch-clock value is astronomically
                # larger, so every deadline would look passed and a live lease
                # would be reclaimed out from under its holder. The sweep sibling
                # passes `time.monotonic_ns()` for the same reason.
                pending_delivery.reclaim(
                    root, recipient_key, delivery_id, now_ns=time.monotonic_ns()
                )
            pending_delivery.claim(
                root, recipient_key, delivery_id, claim_owner=claim_owner,
                lease_seconds=CLAIM_LEASE_SECONDS, require_generation_proof=False,
            )
        except pending_delivery.PendingDeliveryError:
            continue
        if record.get("receipt", {}).get("kind") == "supervision":
            try:
                from dispatch_supervision import notice_is_current
                if not notice_is_current(record):
                    pending_delivery.reject_claimed(root, recipient_key, delivery_id,
                        claim_owner=claim_owner, reason="supervision-resolved")
                    continue
            except (OSError, ValueError, pending_delivery.PendingDeliveryError):
                continue
        notices.append(_bounded_receipt_text(record))
        if announced is not None:
            announced.append(delivery_id)
        try:
            if settle == "sent-ambiguous":
                pending_delivery.mark_sent_ambiguous(
                    root, recipient_key, delivery_id, claim_owner=claim_owner
                )
            else:
                pending_delivery.ack(
                    root, recipient_key, delivery_id, acked_by=f"async-rewake:{launch.session_id}"
                )
        except pending_delivery.PendingDeliveryError:
            pass
    return notices


def gate_wake_message(launch: Launch, notices: list[str]) -> str:
    """The in-wait gate wake: bounded, typed, and explicit that the owner is
    still alive and waiting -- so the session answers the gate instead of
    harvesting the attempt."""

    if any("Hearting supervision needs attention." in notice for notice in notices):
        return " ".join(notices)
    return (
        "Hearting human gate awaiting your decision (SD-123/129). Runtime gate receipt "
        f"schema=2 state=attention attempt_id={launch.attempt_id} armed={launch.armed} "
        "owner=alive-waiting required_action=human-gate. "
        "The owner is waiting on `workflow-supervisor.py await-release`; it is answered, not "
        "harvested. Read the artifact named below (an interview file or frame summary), put "
        "the [방향 확인] card -- and every interview question, one topic at a time, in plain "
        "words -- to the user through AskUserQuestion, then record the answer with "
        "`workflow-supervisor.py release --route <route file> --gate <name> --decision "
        "proceed|revise|stop [--answers <answers.json>]`. That release command re-arms this "
        "hook for the owner's completion. Do not start or re-arm Background Bash, Monitor, "
        "liveness, or dispatch-wait, and do not emit a periodic progress recap. "
        + " ".join(notices)
    )


def no_arm_notice(payload: object, *, reason: str | None = None) -> int:
    """One typed notice when a start receipt proves `started=1` for this
    session but no hook process holds the attempt's arm claim.

    Arming stays fail-closed -- absence beats misattribution -- but the *loss*
    must be loud (fix candidate ③ of the 2026-08-24 quick-gap record): tell
    the launching session immediately that completion will not wake it. A
    start whose own stdout already reports failure keeps telling that story
    itself, and a filtered stdout that names no attempt cannot be judged
    here -- its row is armed by this or the next Bash call from the registry."""

    gate = _bash_call(payload)
    if gate is None:
        return 0
    inner, session = gate
    fields = _fields(_stdout(inner.get("tool_response")))
    started = "start" in fields.get("status", []) and "1" in fields.get("started", [])
    attempt_id = _single(fields, "attempt_id")
    if not started or attempt_id is None or _single(fields, "parent_session_id") != session:
        return 0
    if reason is None:
        jobs = _resolved_jobs(inner)
        if jobs is None:
            reason = "no-registry"
        else:
            existing = _read_arm(arm_path(jobs, attempt_id))
            if existing is None:
                reason = "unclaimed"
            else:
                refusal = _reclaim_refusal(existing, jobs, session)
                reason = refusal or "reclaimable"
    if reason in ArmRefusal.WATCHED:
        return 0
    message = (
        f"[dispatch-owner-rewake] schema=2 state=not-armed attempt_id={attempt_id} "
        f"reason={reason} — this owner start reported started=1 but the asyncRewake bridge "
        "did NOT arm, so its completion will not wake this session and no bridge is "
        "watching it. Watch this exact attempt via the explicit poll-fallback "
        "(dispatch-wait --attempt-id <id>); do not wait for a wake that cannot arrive."
    )
    print(json.dumps({"systemMessage": message}, ensure_ascii=False, separators=(",", ":")))
    print(message, file=sys.stderr)
    return 2


def main() -> int:
    try:
        payload: Any = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        return 0
    launch = parse_launch(payload)
    if launch is not None:
        # The receipt names a candidate; the registry row proves it (R1 B1).
        age = receipt_row_age(launch)
        if age is None:
            return no_arm_notice(payload, reason="row-identity-mismatch")
        claim = claim_arm(launch.jobs, launch.attempt_id, launch.session_id, fresh=age <= _arm_window())
        if isinstance(claim, ArmRefusal):
            # A watched attempt (live holder, spent gate wake, ended) is
            # someone else's wake; anything else is a loss and says so.
            return 0 if claim.watched else no_arm_notice(payload, reason=claim.reason)
    else:
        resolved = registry_launch(payload)
        if isinstance(resolved, ArmRefusal):
            return 0 if resolved.watched else no_arm_notice(payload, reason=resolved.reason)
        if resolved is None:
            return no_arm_notice(payload)
        launch, claim = resolved
    root = agent_home()
    readiness = root / "utilities" / "dispatch-attempt-ready.py"
    if not readiness.is_file():
        # A helper missing from *this* call's agent home (release rotation, a
        # wrong AGENT_HOME) is a lapse, not an end: the next Bash call may
        # see a whole home again (review R1 B3).
        settle_arm(claim, "lapsed")
        state, message = classified_receipt(
            launch, "bridge-error", "readiness-helper-missing", root
        )
    else:
        interval = _bounded_number(
            "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS, 1, 300
        )
        maximum = _bounded_number(
            "AGENT_CLAUDE_REWAKE_MAX_SECONDS", DEFAULT_MAX_SECONDS, interval, 86_400
        )
        deadline = time.monotonic() + maximum
        while True:
            wait_state, wait_reason = wait_for_attempt(
                launch, readiness, gate_probe=lambda: _open_gate_pending(launch),
                deadline=deadline,
            )
            if wait_state != "gate":
                break
            # SD-129: the owner is alive at its gate. Wake the person now with
            # this process's one wake and record which gate record it spent
            # it on: once that record closes (the release retires it, or the
            # next-prompt sweep acks it) the next Bash call in this session
            # re-arms the wait (`registry_launch` -> `_reclaimable`). A probe
            # that another carrier beat to the record (nothing left to
            # announce) sleeps one interval and resumes waiting -- never a
            # tight loop (review round 1, B3).
            announced: list[str] = []
            notices = _gate_notices(
                launch, attempt_only=True, settle="sent-ambiguous", announced=announced
            )
            if notices:
                code = emit_receipt("attention", gate_wake_message(launch, notices), block=False)
                settle_arm(claim, "gate-wake-sent", gate_delivery_id=announced[0])
                return code
            time.sleep(interval)
        if wait_state not in {"ready", "attention"}:
            settle_arm(claim, "lapsed")
        state, message = classified_receipt(launch, wait_state, wait_reason, root)
    # `ended` is sealed only once the receipt has actually gone out (review
    # R2 B2), or once another carrier owns the completion: a crash between
    # classification and emission leaves the claim `waiting` under a dead
    # holder, so the next Bash call re-arms and the wake is delivered at
    # least once instead of never.
    #
    # Open gates ride the wake this process actually emits (SD-123 (8)(b))
    # and force the attention state. They are folded in *after* the
    # decision to emit (top review B1: a hook that lost the completion claim
    # used to claim-and-ack every gate for the recipient on its way to a
    # silent exit, so the gate was never announced by anyone), left
    # `sent-ambiguous` while the receipt goes out, and acked only after it
    # did (A59-3: a gate folded into a delivered terminal receipt is not
    # re-announced; one that was not delivered still is).
    block = _attention_has_open_child(message)
    if block:
        # A live owned child is still open -- no delivery-owing terminal
        # transition has happened yet (§4.3.1 stamps intent only at
        # open|running -> done), so there is nothing to claim. This keeps
        # Claude from stopping prematurely; SD-111's claim gate does not
        # apply to it.
        return _emit_with_gates(launch, claim, state, message, block=True)
    owing = _delivery_owing_row(launch)
    if owing is None:
        # Not (yet) a delivery-owing terminal completion -- still open/
        # running (timeout, bridge error) or a non-SD-111 row. Emit exactly
        # as before the claim gate existed; only a genuine delivery-owing
        # terminal notice is claim-gated.
        return _emit_with_gates(launch, claim, state, message, block=False)
    win = _carrier_one_claim(launch, owing)
    if win is None:
        # Another carrier holds (or already acked) the durable record: the
        # completion is delivered by it, so this attempt is finished here --
        # and the recipient's gates are that carrier's (or the sweep's) too.
        return _ended(claim, 0, terminal=True)
    exit_code = _emit_with_gates(launch, claim, state, message, block=False)
    try:
        pending_delivery.mark_sent_ambiguous(
            win.root, win.recipient_key, win.delivery_id, claim_owner=win.claim_owner,
        )
    except pending_delivery.PendingDeliveryError:
        pass
    return exit_code


def _emit_with_gates(launch: Launch, claim: ArmClaim, state: str, message: str, *, block: bool) -> int:
    """Fold the recipient's open gates into the receipt about to go out, emit
    it, and only then ack them; seal the claim after the emit."""

    # The owner's own outcome, read before any gate is folded in (top review
    # R2-M1): a gate belongs to whoever raised it and only changes what this
    # receipt *displays*. Letting it also decide this claim sealed a timed-out
    # or helper-less wait as `ended`, so the owner -- still open -- could never
    # be re-armed and its completion never woke the session again.
    terminal = state in TERMINAL_STATES
    announced: list[str] = []
    gates = _gate_notices(launch, settle="sent-ambiguous", announced=announced)
    if gates:
        state = "attention"
        message = (
            message
            + " A human gate is open and awaiting your decision; it is answered, not harvested. "
            + " ".join(gates)
        )
    exit_code = emit_receipt(state, message, block=block)
    root = launch.jobs.resolve(strict=False).parent
    recipient_key = _recipient_key(launch)
    for delivery_id in announced:
        try:
            pending_delivery.ack(root, recipient_key, delivery_id, acked_by=f"async-rewake:{launch.session_id}")
        except pending_delivery.PendingDeliveryError:
            pass
    return _ended(claim, exit_code, terminal=terminal)


def _ended(claim: ArmClaim, exit_code: int, *, terminal: bool) -> int:
    """Seal the claim after the receipt is out: `ended` for a terminal wake,
    `lapsed` for a non-terminal bridge state a later call may retry."""

    settle_arm(claim, "ended" if terminal else "lapsed")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
