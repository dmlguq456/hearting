"""Peer steward: watch identities, claims, arms, close evidence, and join.

Split out of `peer-steward.py`, which re-exports every name defined here.
"""
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from dispatch_contract import process_start_ticks
from parent_next_directive import steward_fields
import peer_obligations
import shared_readiness

from peer_steward_base import (
    _AGENT_STATES, _CLAIM_TIMEOUT_MS, _HERDR_GET_TIMEOUT_SECONDS, _interpret_payload,
    _proc_start_ticks, _typed_line, peer_message,
)


# ---------------------------------------------------------------------------
# SD-122 (10) detached steward watch: watch / join / status / rearm / ack
# ---------------------------------------------------------------------------

_WATCH_SCHEMA = 1
_WATCH_STATES = _AGENT_STATES + (
    "timeout", "agent-not-found", "herdr-unavailable", "pane-exited",
    "session-ended", "session-replaced", "steward-exited", "superseded",
)
_JOIN_EXIT = {"timeout": 3, "agent-not-found": 2, "herdr-unavailable": 4,
              "pane-exited": 2, "session-ended": 0, "session-replaced": 0,
              "steward-exited": 0, "superseded": 0}
_WATCH_TRANSITION_LOCK_BUDGET_SEC = 0.2
_WATCH_RECEIPT_CONTENDED = "contended"
_WATCH_RECEIPT_PENDING = "pending"
_WATCH_RECEIPT_EXPIRED = "watch-deadline"


class _WatchTransitionLockBusy(RuntimeError):
    """The shared receipt lock budget expired before both locks were held."""


class _WatchDeadlineExpired(RuntimeError):
    """The watch's original monotonic deadline expired at the receipt edge."""


def _wake_watch_observer(arm):
    if not isinstance(arm, dict):
        return False
    try:
        protocol = int(arm.get("observer_protocol", 0) or 0)
    except (TypeError, ValueError):
        return False
    if protocol < 2:
        return False
    watcher = (arm or {}).get("watcher") or {}
    pid, start = watcher.get("pid"), watcher.get("pid_start")
    if not _pid_identity_ok(pid, start):
        return False
    try:
        os.kill(int(pid), signal.SIGUSR1)
        return True
    except (OSError, ValueError):
        return False


def _wake_current_watch_observer(watch_root, watch_id, identity, *, transition=None,
                                 readiness_root=None):
    """Wake only the observer still named by this watch's current arm."""
    if not isinstance(identity, dict):
        return False
    paths = _watch_paths(watch_id, watch_root)
    if paths.receipt.exists():
        return False
    arm = _read_json(paths.arm)
    if (not isinstance(arm, dict) or arm.get("watch_id") != watch_id
            or not _watch_readiness_identity_matches_arm(
                identity, arm, identity.get("native_state"))):
        return False
    watcher = arm.get("watcher") or {}
    if not _pid_identity_ok(watcher.get("pid"), watcher.get("pid_start")):
        return False
    if transition is not None:
        if readiness_root is None:
            return False
        observation = _read_json(paths.observation) or {}
        current = observation.get("readiness_transition")
        consumer = transition.get("consumer") if isinstance(transition, dict) else None
        token = transition.get("token") if isinstance(transition, dict) else None
        if (not isinstance(current, dict) or not isinstance(consumer, dict)
                or transition.get("identity") != identity
                or current.get("token") != token
                or current.get("identity") != identity
                or current.get("consumer") != consumer
                or consumer != watcher
                or not shared_readiness.consumer_is_current({"consumer": consumer})
                or shared_readiness.watch_transition_result(
                    readiness_root, watch_id, identity, token) is None):
            return False
    return _wake_watch_observer(arm)


def _watch_root():
    """`peer-watches/` beside `peer-messages/`, under the one resolved state root.

    Routed through `peer_message.peer_state_root()` on purpose: resolving the
    state root a second time here would let the ledger root and the watch root
    diverge whenever the resolver's inputs differ, and a watch whose receipt
    lives beside a different ledger is unfindable.
    """
    return peer_message.peer_state_root() / "peer-watches"


@dataclass(frozen=True)
class _WatchPaths:
    arm: Path
    lock: Path
    log: Path
    receipt: Path
    ack: Path
    observation: Path


def _watch_paths(watch_id, root=None):
    root = root or _watch_root()
    return _WatchPaths(
        arm=root / f"{watch_id}.json",
        lock=root / f"{watch_id}.lock",
        log=root / f"{watch_id}.log",
        receipt=root / f"{watch_id}.receipt.json",
        ack=root / f"{watch_id}.ack.json",
        observation=root / f"{watch_id}.observation.json",
    )


def _prepare_watch_notice(watch_id, arm, receipt):
    """Queue one stable exact-ref notice under the original steward identity."""
    steward = (arm or {}).get("steward") or {}
    steward_seat = (arm or {}).get("steward_seat") or {}
    harness, sid = steward.get("harness"), steward.get("session_id")
    if (harness not in {"claude", "codex", "opencode"}
            or not peer_message.usable_session_id(sid)
            or steward_seat.get("kind") not in {"pane", "native"}
            or not steward_seat.get("key")):
        return None
    agent = (receipt or {}).get("agent") or (arm or {}).get("agent") or {}
    target = agent.get("name") or agent.get("session_id") or (arm or {}).get("target") or "unknown"
    body = (f"[peer-steward] watch_id={watch_id} state={(receipt or {}).get('state', 'unknown')} "
            f"target={target}; exact watch receipt is available")
    sender = {"harness": harness, "session_id": sid}
    if steward.get("project"):
        sender["project"] = steward["project"]
    recipient = {"harness": harness, "session_id": sid}
    try:
        _text, transfer_ref = peer_message.prepare_peer_message(
            body, sender, recipient, defer=True, refs=[f"peer-watch:{watch_id}"],
            receipt=f"peer-steward-watch:{watch_id}")
        current = _read_json(_watch_paths(watch_id).arm) or dict(arm or {})
        if current.get("notice_transfer_ref") != transfer_ref:
            _write_json_atomic(_watch_paths(watch_id).arm,
                               {**current, "notice_transfer_ref": transfer_ref})
        if (arm or {}).get("wake") == "queue":
            _queue_watch_notice(transfer_ref, arm)
        return transfer_ref
    except Exception:  # noqa: BLE001 - status/reconnect remains the ordinary fallback
        return None


def _queue_watch_notice(transfer_ref, arm):
    """Hand a `wake=queue` receipt notice to the normal message courier.

    The same duty `peer-steward prompt` schedules for a peer report
    (`_schedule_message_obligation`): the obligation runner takes the exact
    pending row to the Codex steward's native thread queue, which starts a turn
    on an idle thread. Without it the notice waits for the next prompt.
    """
    from peer_steward_native import _message_obligation_id
    row = peer_message._read_pending(transfer_ref)
    if not row:
        return None
    recipient = peer_message.pending_delivery_recipient(row) or row.get("to") or {}
    identity = {"server": (arm or {}).get("steward_server") or (arm or {}).get("server") or "default",
                "pane": "", "harness": recipient.get("harness") or "",
                "session_id": recipient.get("session_id") or ""}
    intent = {"ref": row.get("ref"), "body_digest": row.get("body_sha256"),
              "target": recipient.get("session_id") or "", "from": row.get("from") or {},
              "to": recipient, "refs": list(row.get("refs") or []), "delay_notice_for": ""}
    try:
        duty = peer_obligations.ObligationStore().create(
            _message_obligation_id(row["ref"]), "message", identity, intent)
        peer_obligations.ensure_normal_runner()
        return duty
    except (OSError, ValueError, peer_obligations.ObligationError):
        return None


def _terminal_watch_request(paths, expected, server):
    observation = _read_json(paths.observation)
    if (not isinstance(observation, dict) or observation.get("state") != "terminal_pending"
            or (observation.get("server") or "default") != server):
        return None
    requested = observation.get("agent") or {}
    if any(requested.get(key) != expected.get(key)
           for key in ("harness", "session_id", "pane")):
        return None
    return str(observation.get("reason") or "completed-retire")


class _WatchCloseEvidence:
    """Memoized native lifetime and lineage evidence for one close pass."""

    def __init__(self, recipient_census=None):
        self._chain = {}
        self._liveness = {}

    def chain_end(self, harness, sid):
        key = (harness, sid)
        if key not in self._chain:
            try:
                import dispatch_seat_handover as handover
                graph = handover.lineage_graph()
                self._chain[key] = handover._chain_end(graph, harness, sid)
            except Exception:
                self._chain[key] = (sid, "unresolved")
        return self._chain[key]

    def liveness(self, harness, sid, agent=None):
        key = (harness, sid)
        if key not in self._liveness:
            try:
                from peer_steward_native import native_lifetime
                agent = agent if isinstance(agent, dict) else {}
                self._liveness[key] = native_lifetime(
                    harness, sid, pid=agent.get("pid"),
                    pid_start=agent.get("pid_start"),
                    pid_namespace=agent.get("pid_namespace"))
            except Exception:
                self._liveness[key] = "unknown"
        return self._liveness[key]


def _watch_close_reason(watch_id, arm, evidence):
    """Close only on exact native end or a confirmed successor; unknown stays armed."""
    expected = dict(arm.get("agent") or {})
    target_harness, target_sid = expected.get("harness"), expected.get("session_id")
    if (isinstance(target_harness, str) and isinstance(target_sid, str)
            and target_harness not in {"", "-"} and target_sid not in {"", "-"}):
        end, disposition = evidence.chain_end(target_harness, target_sid)
        if disposition == "successor" and end != target_sid:
            return "session-replaced"
        if evidence.liveness(target_harness, target_sid, expected) == "ended":
            return "session-ended"

    steward = arm.get("steward") or {}
    seat = arm.get("steward_seat") or {}
    holder = (steward.get("harness"), steward.get("session_id"))
    if seat.get("key"):
        try:
            import dispatch_seat_handover as handover
            relation = handover.resolve_watch(watch_id, seat_key=seat["key"])
            current = (relation or {}).get("holder") or {}
            holder = (current.get("harness"), current.get("sid")) if relation else (None, None)
        except Exception:
            holder = (None, None)
    if (all(isinstance(value, str) and value for value in holder)
            and evidence.liveness(*holder) == "ended"):
        return "steward-exited"
    return None


def _new_watch_id(steward_sid, target, armed_ts, nonce):
    raw = f"{steward_sid}|{target}|{armed_ts}|{nonce}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _dedupe_key(steward_sid, target, until, server=None):
    # `until=[]` (herdr's default set) and `until=["idle","done","blocked"]` are
    # the same *behaviour* but stay distinct keys on purpose: (10) dedupes on the
    # "until 집합" as given, and silently normalizing them would suppress a
    # legitimate second watch.
    raw = f"{server or 'default'}|{steward_sid}|{target}|{'|'.join(sorted(until or []))}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _logical_watch_claim_key(steward_sid, agent, until, server=None):
    """Key a watch by its resolved target while retaining the caller's raw claim.

    Pane identity is stable before a new native session exists. Once Herdr knows
    the session, include it so a reused pane cannot inherit the prior session's
    watch. The separate pane lock serializes that transition.
    """
    session_id = agent.get("session_id")
    bound = isinstance(session_id, str) and session_id not in {"", "-"}
    identity = [agent.get("harness"), agent.get("pane"),
                "session" if bound else "pane", session_id if bound else ""]
    raw = json.dumps([
        server or "default", steward_sid, identity, sorted(until or []),
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
    return "logical-" + hashlib.sha256(raw).hexdigest()[:16]


def _watch_alias_lock_key(steward_sid, agent, until, server=None):
    """Serialize registrations that resolve to one pane, including SID binding."""
    raw = json.dumps([
        server or "default", steward_sid, agent.get("harness"), agent.get("pane"),
        sorted(until or []),
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
    return "alias-" + hashlib.sha256(raw).hexdigest()[:16]


def _write_watch_claim(lock_fd, watch_id):
    try:
        os.ftruncate(lock_fd, 0)
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.write(lock_fd, watch_id.encode("ascii"))
        os.fsync(lock_fd)
    except OSError:
        pass


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return value or None
    except OSError:
        return None


def _watch_deadline(timeout_ms, *, wall=None, monotonic=None, boot_id=None):
    """Capture one absolute budget; observer restarts never receive a fresh timeout."""
    if timeout_ms is None:
        return None
    requested = int(timeout_ms)
    duration = max(0, requested) / 1000.0
    now_wall = time.time() if wall is None else float(wall)
    now_mono = time.monotonic() if monotonic is None else float(monotonic)
    return {
        "requested_ms": requested,
        "wall_deadline": now_wall + duration,
        "monotonic_deadline": now_mono + duration,
        "boot_id": _boot_id() if boot_id is None else boot_id,
    }


def _watch_timeout_remaining_ms(arm, *, wall=None, monotonic=None, boot_id=None):
    """Return (remaining-ms, state); legacy incomplete deadlines stay unknown."""
    timeout = (arm or {}).get("timeout")
    if timeout is None:
        return None, "unbounded"
    try:
        requested = int(timeout)
    except (TypeError, ValueError):
        return None, "unknown"
    if requested <= 0:
        return 0, "expired"
    now_wall = time.time() if wall is None else float(wall)
    now_mono = time.monotonic() if monotonic is None else float(monotonic)
    current_boot = _boot_id() if boot_id is None else boot_id
    deadline = (arm or {}).get("deadline")
    if not isinstance(deadline, dict):
        armed_ts = (arm or {}).get("armed_ts")
        try:
            armed_wall = datetime.datetime.strptime(
                armed_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            return None, "unknown"
        deadline = {"wall_deadline": armed_wall + requested / 1000.0,
                    "requested_ms": requested, "boot_id": None}
    try:
        original = min(requested, max(0, int(deadline.get("requested_ms", requested))))
        if (deadline.get("boot_id") and current_boot
                and deadline.get("boot_id") == current_boot
                and isinstance(deadline.get("monotonic_deadline"), (int, float))):
            remaining = (float(deadline["monotonic_deadline"]) - now_mono) * 1000
        else:
            remaining = (float(deadline["wall_deadline"]) - now_wall) * 1000
        value = min(original, max(0, math.ceil(remaining)))
    except (TypeError, ValueError, OverflowError):
        return None, "unknown"
    return value, "expired" if value <= 0 else "active"


def _watch_monotonic_deadline(arm, *, wall=None, monotonic=None, boot_id=None):
    """Resolve the arm's one absolute deadline into this process's monotonic clock."""
    timeout = (arm or {}).get("timeout")
    if timeout is None:
        return None
    try:
        requested = int(timeout)
    except (TypeError, ValueError):
        return None
    if requested <= 0:
        return time.monotonic() if monotonic is None else float(monotonic)
    now_wall = time.time() if wall is None else float(wall)
    now_mono = time.monotonic() if monotonic is None else float(monotonic)
    current_boot = _boot_id() if boot_id is None else boot_id
    deadline = (arm or {}).get("deadline")
    if not isinstance(deadline, dict):
        armed_ts = (arm or {}).get("armed_ts")
        try:
            armed_wall = datetime.datetime.strptime(
                armed_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            return None
        deadline = {"wall_deadline": armed_wall + requested / 1000.0,
                    "requested_ms": requested}
    try:
        original = min(requested, max(0, int(deadline.get("requested_ms", requested))))
        cap = now_mono + original / 1000.0
        if (deadline.get("boot_id") and current_boot
                and deadline.get("boot_id") == current_boot
                and isinstance(deadline.get("monotonic_deadline"), (int, float))):
            return min(cap, float(deadline["monotonic_deadline"]))
        return min(cap, now_mono + float(deadline["wall_deadline"]) - now_wall)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _steward_seat(harness, sid):
    """Return a pane/native seat only when the shared seat resolver proves one."""
    if not harness or harness == "unknown" or not sid:
        return None
    try:
        import dispatch_seat_handover
        seat = dispatch_seat_handover.pane_seat(harness=harness, sid=sid)
        return seat.as_dict() if seat is not None and seat.kind in {"pane", "native"} else None
    except Exception:
        return None


def _seat_claim_path(root, server, seat):
    raw = f"{server or 'default'}|{seat.get('key', '')}".encode("utf-8", "replace")
    return Path(root) / (hashlib.sha256(raw).hexdigest()[:16] + ".seat")


def _write_json_atomic(path, obj):
    """tmp in the same directory -> fsync file -> rename -> fsync directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    dirfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def _open_lock(path):
    """Open the lock file without truncating it.

    Never `open(path, "w")`: a truncating open against a lock another process is
    holding is silent corruption of shared state.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_identity_ok(pid, pid_start):
    """§5.12 PID identity: the pid exists *and* its start ticks still match.

    Birth and exit come from one `/proc/<pid>/stat` read. A zombie (Z) or a
    process its parent is reaping (X) still shows the same start ticks, but it
    has exited and holds no lock. A second read could find the row already
    gone and call that exited watcher alive.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    observed = _proc_start_ticks(pid)
    return observed is not None and observed == str(pid_start)


def _lock_held(lock_path):
    if not lock_path.exists():
        return False
    try:
        fd = _open_lock(lock_path)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)   # one probe, one descriptor (review m1)


def _watcher_present(arm):
    """Is a watcher process for this watch still running?

    PID identity only, deliberately. `_alive` additionally requires the lock,
    which is correct for *reporting* (`status` must distinguish "spawned" from
    "actually watching") but wrong for any *decision to replace* the watch: for
    the few hundred milliseconds between spawn and flock a perfectly healthy
    watcher holds no lock, and treating that as death double-spawns on `watch`
    and replaces a live watch on `rearm`. This is the same spawn-latency trap
    that `cmd_join` avoids when it decides `watcher-dead`.
    """
    if not isinstance(arm, dict):
        return False
    watcher = arm.get("watcher") or {}
    return _pid_identity_ok(watcher.get("pid"), watcher.get("pid_start"))


def _alive(arm, root=None):
    """Both conditions, never either: a pid that matches but holds no lock is a
    watcher that died before flocking (or was never a watcher at all).

    This is the A56-4 reporting predicate. Use `_watcher_present` to decide
    whether a watch may be replaced.
    """
    if not _watcher_present(arm):
        return False
    return _lock_held(_watch_paths(arm["watch_id"], root).lock)


def _until_field(until):
    return "|".join(until) if until else "-"


def _armed_line(arm, paths):
    watcher = arm.get("watcher") or {}
    # The armed line states the caller's next action for the same reason a
    # launch receipt does (`utilities/parent_next_directive.py`): the watcher
    # carries this watch to one wake, so the caller yields instead of polling.
    return (
        f"watch_id={arm['watch_id']} state=armed target={arm['target']} "
        f"until={_until_field(arm.get('until'))} wake={arm.get('wake', 'none')} "
        f"pid={watcher.get('pid', '-')} pid_start={watcher.get('pid_start', '-')} "
        f"receipt={paths.receipt}\n"
        + steward_fields(
            arm.get("wake"), arm.get("watch_id"),
            agent_home=Path(__file__).resolve().parents[1],
            timeout_ms=arm.get("timeout"),
        )
    )


def _receipt_line(receipt, watch_id, paths):
    agent = receipt.get("agent") or {}
    return _typed_line(
        receipt.get("state", "unknown"), agent.get("harness", "-"),
        agent.get("session_id", "-"), agent.get("name", "-"), agent.get("pane", "-"),
    ) + f" watch_id={watch_id} receipt={paths.receipt}"


def _exit_for_state(state):
    return _JOIN_EXIT.get(state, 0)


def _herdr_get_timeout():
    try:
        value = float(os.environ.get("AGENT_PEER_STEWARD_HERDR_GET_TIMEOUT", _HERDR_GET_TIMEOUT_SECONDS))
    except ValueError:
        value = float(_HERDR_GET_TIMEOUT_SECONDS)
    return min(600.0, max(0.5, value))


def _watch_arm_scope_matches(arm, server, steward_sid, until, agent):
    if not isinstance(arm, dict) or (arm.get("server") or "default") != server:
        return False
    steward = arm.get("steward") or {}
    if steward.get("session_id") != steward_sid:
        if not _confirmed_watch_successor(arm.get("watch_id"), arm, steward_sid):
            return False
    if tuple(sorted(arm.get("until") or [])) != tuple(sorted(until or [])):
        return False
    expected = arm.get("agent") or {}
    return all(expected.get(key) not in {None, "", "-"}
               and expected.get(key) == agent.get(key)
               for key in ("harness", "pane"))


def _confirmed_watch_successor(watch_id, arm, requested_sid=None, *, include_acked=False):
    """A watch follows only the exact successor stored by the common seat resolver."""
    steward = (arm or {}).get("steward") or {}
    seat_key = ((arm or {}).get("steward_seat") or {}).get("key")
    if not watch_id or not seat_key:
        return None
    try:
        import dispatch_seat_handover as handover
        relation = handover.resolve_watch(str(watch_id), seat_key=seat_key,
                                          include_acked=include_acked)
    except Exception:
        return None
    if not isinstance(relation, dict):
        return None
    if any(relation.get(field) != (arm or {}).get(field) for field in
           ("server", "target", "agent", "until", "deadline", "origin")):
        # Arms predating the optional persisted `origin` compare against their
        # original steward tuple below; no name or pane alias fills the gap.
        if any(relation.get(field) != (arm or {}).get(field) for field in
               ("server", "target", "agent", "until", "deadline")):
            return None
    holder = relation.get("holder") or {}
    if holder.get("harness") != steward.get("harness"):
        return None
    if requested_sid is not None and holder.get("sid") != requested_sid:
        return None
    return relation


def _watch_arm_matches(arm, server, steward_sid, until, agent):
    if not _watch_arm_scope_matches(arm, server, steward_sid, until, agent):
        return False
    expected_sid = (arm.get("agent") or {}).get("session_id")
    observed_sid = agent.get("session_id")
    return expected_sid == observed_sid


def _watch_claim_path_for_arm(paths, arm, server):
    claim_key = arm.get("claim_key") if isinstance(arm, dict) else None
    if isinstance(claim_key, str) and re.fullmatch(r"logical-[0-9a-f]{16}", claim_key):
        return paths.arm.parent / f"{claim_key}.arm"
    raw_target = arm.get("target") if isinstance(arm, dict) else None
    if not isinstance(raw_target, str) or not raw_target:
        return None
    steward_sid = (arm.get("steward") or {}).get("session_id")
    if not isinstance(steward_sid, str) or not steward_sid:
        return None
    return paths.arm.parent / (
        f"{_dedupe_key(steward_sid, raw_target, list(arm.get('until') or []), server)}.arm"
    )


def _persist_watch_session_under_alias_lock(paths, server, session_id):
    """Bind an arm while its pane alias lock is held, then its claim lock.

    Lock order for watch identity is steward-seat (registration only), pane
    alias, then the arm's logical or legacy raw claim. Registration admission
    uses the same order. The claim lock protects durable legacy/new claim
    publication; the pane lock serializes the SID transition itself.
    """
    if not isinstance(session_id, str) or session_id in {"", "-"}:
        return False, _read_json(paths.arm)
    arm = _read_json(paths.arm)
    agent = arm.get("agent") if isinstance(arm, dict) else None
    if (not isinstance(agent, dict) or (arm.get("server") or "default") != server
            or any(not isinstance(agent.get(key), str) or agent.get(key) in {"", "-"}
                   for key in ("harness", "pane"))):
        return False, arm
    claim = _watch_claim_path_for_arm(paths, arm, server)
    if claim is None:
        return False, arm
    try:
        claim_fd = _open_lock(claim)
    except OSError:
        return False, arm
    try:
        if not _acquire_bounded(claim_fd, _CLAIM_TIMEOUT_MS):
            return False, _read_json(paths.arm)
        current = _read_json(paths.arm)
        current_agent = current.get("agent") if isinstance(current, dict) else None
        if (not isinstance(current_agent, dict)
                or (current.get("server") or "default") != server
                or (current.get("steward") or {}).get("session_id") !=
                    (arm.get("steward") or {}).get("session_id")
                or tuple(sorted(current.get("until") or [])) !=
                    tuple(sorted(arm.get("until") or []))
                or any(current_agent.get(key) != agent.get(key) for key in ("harness", "pane"))):
            return False, current
        current_sid = current_agent.get("session_id")
        if current_sid in {None, "", "-"}:
            updated = {**current, "agent": {**current_agent, "session_id": session_id}}
            _write_json_atomic(paths.arm, updated)
            return True, updated
        if current_sid == session_id:
            return True, current
        # A native binding is immutable. A conflicting later result stays
        # unknown to its observer and never replaces the established SID.
        return False, current
    finally:
        os.close(claim_fd)


def _watch_native_binding_evidence(paths, arm):
    """Return durable binding evidence without inventing a second identity source."""
    observation = _read_json(paths.observation) or {}
    wait_record = observation.get("native_wait") if isinstance(observation, dict) else None
    payload = None
    try:
        raw = paths.log.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    candidates = [raw, *reversed(raw.splitlines())] if raw else []
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
            payload = parsed
            break

    if payload is not None:
        state, result_agent, _code, reason = _interpret_payload(
            payload, arm.get("target", "-"))
        if state in {"timeout", "agent-not-found", "herdr-unavailable"}:
            return "terminal", None, None
        result = {"agent_state": state, "agent": result_agent,
                  "reason": reason or state}
        identity, sid, _identity_reason = _watch_native_result_identity(arm, result)
        if identity == "conflict":
            return "conflict", sid, result
        if reason is not None or state == "unknown":
            return "unknown", None, None
        if identity == "unbound" and sid is None:
            return "sidless", None, result
        if identity in {"match", "unbound"}:
            return "bound", sid, result
        if identity == "sidless":
            return "sidless", sid, result
        return "unknown", None, None

    if isinstance(wait_record, dict) and wait_record.get("state") == "result":
        result_agent = wait_record.get("agent")
        if isinstance(result_agent, dict):
            result = {
                "agent_state": wait_record.get("agent_state", "unknown"),
                "agent": result_agent,
                "reason": wait_record.get("reason", "native-wait-result"),
            }
            identity, sid, _identity_reason = _watch_native_result_identity(arm, result)
            if identity == "unbound" and sid is None:
                return "sidless", None, result
            if identity in {"match", "unbound"}:
                return "bound", sid, result
            if identity == "sidless":
                return "sidless", sid, result
            if identity == "conflict":
                return "conflict", sid, result
            return "unknown", None, None
    if isinstance(wait_record, dict) and wait_record.get("state") == "unknown":
        return "unknown", None, None
    if observation.get("state") == "unknown" or raw.strip():
        return "unknown", None, None
    return "none", None, None


def _watch_native_result_identity(arm, result):
    """Classify a parsed wait result against the exact durable arm identity."""
    if not isinstance(arm, dict) or not isinstance(result, dict):
        return "unknown", None, "target-identity-unverified"
    if (result.get("terminal_reason")
            or result.get("agent_state") in {"timeout", "agent-not-found", "herdr-unavailable"}):
        return "terminal", None, None
    expected = arm.get("agent")
    observed = result.get("agent")
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return "unknown", None, "target-identity-unverified"
    if any(not isinstance(expected.get(key), str) or expected.get(key) in {"", "-"}
           or observed.get(key) != expected.get(key)
           for key in ("harness", "pane")):
        return "unknown", None, "target-identity-unverified"
    sid = observed.get("session_id")
    if not isinstance(sid, str):
        return "unknown", None, "target-identity-unverified"
    expected_sid = expected.get("session_id")
    if expected_sid in {None, "", "-"}:
        if sid in {"", "-"}:
            return "unbound", None, None
        return "unbound", sid, None
    if sid in {"", "-"}:
        return "sidless", expected_sid, None
    if expected_sid != sid:
        return "conflict", sid, "target-identity-conflict"
    return "match", sid, None


def _preserve_watch_native_result_unknown(paths, arm, result, reason):
    """Keep a result unresolved and retain the exact prior wait process identity."""
    observation = _read_json(paths.observation) or {}
    previous = observation.get("native_wait")
    unknown_wait = dict(previous) if isinstance(previous, dict) else {}
    if isinstance(result, dict):
        if isinstance(result.get("agent"), dict):
            unknown_wait["agent"] = result["agent"]
        if result.get("agent_state") is not None:
            unknown_wait["agent_state"] = result["agent_state"]
        if result.get("reason") is not None:
            unknown_wait["result_reason"] = result["reason"]
        unknown_wait["unresolved_result"] = dict(result)
    unknown_wait.update({"state": "unknown", "reason": reason})
    observation.update({
        "schema_version": _WATCH_SCHEMA,
        "watch_id": paths.arm.stem,
        "server": arm.get("server") or "default",
        "state": "unknown",
        "reason": reason,
        "native_wait": unknown_wait,
        "observed_ts": _utc_now(),
    })
    _write_json_atomic(paths.observation, observation)
    return unknown_wait, observation


def _record_watch_native_result(paths, arm, result):
    """Publish only a result attributed to this arm's exact native identity."""
    if not isinstance(result, dict):
        return False
    identity, _sid, identity_reason = _watch_native_result_identity(arm, result)
    if identity not in {"match", "unbound", "sidless"}:
        _preserve_watch_native_result_unknown(
            paths, arm, result, identity_reason or "target-identity-unverified")
        return False
    observation = _read_json(paths.observation) or {}
    previous = observation.get("native_wait")
    native_wait = dict(previous) if isinstance(previous, dict) else {}
    native_wait.update(result)
    native_wait["state"] = "result"
    observation.update({
        "schema_version": _WATCH_SCHEMA,
        "watch_id": paths.arm.stem,
        "server": arm.get("server") or "default",
        "native_wait": native_wait,
        "observed_ts": _utc_now(),
    })
    _write_json_atomic(paths.observation, observation)
    return True


def _write_watch_observation(paths, observation, *, control_updates=()):
    """Keep a transition request beside its exact durable native result."""
    current = _read_json(paths.observation) or {}
    current_native = current.get("native_wait") or {}
    next_native = observation.get("native_wait") or {}
    if (current_native.get("state") == "result"
            and current_native.get("agent_state") in {"idle", "done"}
            and next_native.get("state") == "result"
            and next_native.get("agent_state") == current_native.get("agent_state")
            and next_native.get("agent") == current_native.get("agent")):
        control_fields = {"readiness_transition", "readiness_contention"}
        updates = set(control_updates)
        if not updates.issubset(control_fields):
            raise ValueError("unknown watch observation control update")
        observation = dict(observation)
        for field in control_fields - updates:
            if field in current:
                observation[field] = current[field]
            else:
                observation.pop(field, None)
    _write_json_atomic(paths.observation, observation)
    return observation


def _persist_watch_session(args, paths, server, session_id):
    """Bind an unbound arm under the same pane admission lock as registration."""
    arm = _read_json(paths.arm)
    agent = arm.get("agent") if isinstance(arm, dict) else None
    if (not isinstance(agent, dict) or (arm.get("server") or "default") != server
            or (arm.get("steward") or {}).get("session_id") != args.steward_session_id
            or tuple(sorted(arm.get("until") or [])) != tuple(sorted(args.until or []))
            or agent.get("harness") != getattr(args, "expected_harness", None)
            or agent.get("pane") != getattr(args, "expected_pane", None)):
        return False
    alias = paths.arm.parent / (
        f"{_watch_alias_lock_key(args.steward_session_id, agent, list(arm.get('until') or []), server)}.lock"
    )
    try:
        alias_fd = _open_lock(alias)
    except OSError:
        return False
    try:
        if not _acquire_bounded(alias_fd, _CLAIM_TIMEOUT_MS):
            return False
        current = _read_json(paths.arm)
        current_agent = current.get("agent") if isinstance(current, dict) else None
        if (not isinstance(current_agent, dict)
                or (current.get("steward") or {}).get("session_id") != args.steward_session_id
                or any(current_agent.get(key) != agent.get(key) for key in ("harness", "pane"))):
            return False
        ok, _updated = _persist_watch_session_under_alias_lock(paths, server, session_id)
        return ok
    finally:
        os.close(alias_fd)


def _already_armed_line(watch_id, arm, paths):
    """`state=already-armed` must survive a missing arm record: the winner can
    die between spawn and the arm write, and crashing here would take down an
    otherwise healthy `watch` call."""
    if isinstance(arm, dict):
        watcher = arm.get("watcher") or {}
        target = arm.get("target", "-")
        until = _until_field(arm.get("until"))
        wake = arm.get("wake", "-")
        pid = watcher.get("pid", "-")
        pid_start = watcher.get("pid_start", "-")
    else:
        target = until = wake = pid = pid_start = "-"
    return (
        f"watch_id={watch_id} state=already-armed target={target} until={until} "
        f"wake={wake} pid={pid} pid_start={pid_start} receipt={paths.receipt}\n"
        # The hook arms only from a `watch` command printing `state=armed`.
        # This line is `already-armed` (or `alive`, from `rearm`), so it arms
        # nothing -- and it cannot prove the *earlier* arm succeeded either.
        # That earlier arm failing is exactly why a caller re-runs `watch`, so
        # answering `end-turn` here would hand back the most dangerous reply at
        # the moment the caller is trying to recover. A redundant wake is
        # absorbed by at-least-once ack; a missed one is lost work.
        + steward_fields(
            wake if wake != "-" else None, watch_id,
            agent_home=Path(__file__).resolve().parents[1],
            arms_hook=False,
            timeout_ms=arm.get("timeout") if isinstance(arm, dict) else None,
        )
    )


def _record_own_watch_identity(watch_id, paths, server):
    """Name this observer in its arm when its spawner never did.

    A spawner (`watch`, or `_spawn_watch_observer` on reattach) records the
    observer identity after the spawn, under the arm's claim lock. One killed
    in between leaves an arm that names no observer or a previous one that has
    exited, so nothing could wake or replace the observer holding the watch
    lock. Under the same claim lock either that write already landed, or the
    spawner is gone and this lock holder names itself. A live watcher other
    than this process is never overwritten.
    """
    pid = os.getpid()
    start = _proc_start_ticks(pid)
    if not start:
        # An empty start would make this live observer read as exited.
        return _read_json(paths.arm)
    own = {"pid": pid, "pid_start": start}

    def needs_naming(arm):
        if not isinstance(arm, dict) or arm.get("watch_id") != watch_id:
            return False
        recorded = arm.get("watcher") or {}
        if {"pid": recorded.get("pid"), "pid_start": recorded.get("pid_start")} == own:
            return False
        return not _watcher_present(arm)

    arm = _read_json(paths.arm)
    if not needs_naming(arm):
        return arm
    claim = _watch_claim_path_for_arm(paths, arm, server)
    if claim is None:
        return arm
    try:
        claim_fd = _open_lock(claim)
    except OSError:
        return arm
    try:
        if not _acquire_bounded(claim_fd, _CLAIM_TIMEOUT_MS):
            return _read_json(paths.arm) or arm
        current = _read_json(paths.arm)
        if not needs_naming(current) or paths.receipt.exists():
            return current if isinstance(current, dict) else arm
        updated = {**current, "watcher": own}
        # A failed write raises: this observer then ends and releases the lock,
        # so the next spawn can start and name a replacement. An unnamed
        # observer that kept the lock could be neither woken nor replaced.
        _write_json_atomic(paths.arm, updated)
        return updated
    finally:
        os.close(claim_fd)


def _stop_watch_wait(wait_proc, wait_pid, wait_start):
    """Stop only this watch's exact native wait child; never signal its target."""
    try:
        pid = int(wait_pid)
    except (TypeError, ValueError):
        return
    if not isinstance(wait_start, str) or not wait_start:
        return
    if str(process_start_ticks(pid) or "") != wait_start:
        return
    try:
        if wait_proc is not None and wait_proc.pid == pid:
            if wait_proc.poll() is not None:
                return
            wait_proc.terminate()
            try:
                wait_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if str(process_start_ticks(pid) or "") == wait_start:
                    wait_proc.kill()
                    wait_proc.wait(timeout=2)
            return
        os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return
    except Exception:
        return

class _JoinTimeout(Exception):
    pass


def _acquire_bounded(lock_fd, timeout_ms):
    """Blocking `flock`, bounded by a kernel timer. Zero polling.

    The SIGALRM handler must *raise*: measured on this runtime, a raising
    handler interrupts a blocking `fcntl.flock` (1.00s against a held lock),
    while a handler that only sets a flag would let the lock call resume and
    `--timeout` would never fire.
    """
    if timeout_ms is None:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return True

    def _alarm(_signum, _frame):
        raise _JoinTimeout()

    previous = signal.signal(signal.SIGALRM, _alarm)
    try:
        signal.setitimer(signal.ITIMER_REAL, max(0.001, timeout_ms / 1000.0))
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return True
        except (_JoinTimeout, InterruptedError):
            return False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def cmd_join(args):
    root = _watch_root()
    watch_id = args.watch_id
    paths = _watch_paths(watch_id, root)

    receipt = _read_json(paths.receipt)
    if receipt is not None:
        print(_receipt_line(receipt, watch_id, paths))
        return _exit_for_state(receipt.get("state", "unknown"))

    try:
        lock_fd = _open_lock(paths.lock)
    except OSError:
        print(f"state=watcher-dead watch_id={watch_id} pid=-")
        return 5
    try:
        # At most two passes, both event-driven. The second exists only for an
        # unbounded join that somehow won the lock ahead of the watcher: it must
        # not invent a timeout it was never asked to bound.
        for attempt in range(2):
            if not _acquire_bounded(lock_fd, args.timeout):
                print(f"state=join-timeout watch_id={watch_id}")
                return 6
            # Re-read AFTER acquiring: the watcher renames the receipt and only
            # then exits, so the receipt is guaranteed visible once its lock is
            # free.
            receipt = _read_json(paths.receipt)
            if receipt is not None:
                print(_receipt_line(receipt, watch_id, paths))
                return _exit_for_state(receipt.get("state", "unknown"))

            arm = _read_json(paths.arm)
            watcher = (arm or {}).get("watcher") or {}
            if arm is None or not _pid_identity_ok(watcher.get("pid"), watcher.get("pid_start")):
                print(f"state=watcher-dead watch_id={watch_id} pid={watcher.get('pid', '-')}")
                return 5

            # Lock acquired, no receipt, but the watcher's PID identity still
            # holds. Deciding `watcher-dead` from lock acquisition alone would
            # misreport a healthy watch as dead. `cmd_watch` now holds the lock
            # from before it returns, so this is unreachable for a `watch`-armed
            # id; a bounded join reports its timeout, an unbounded one releases
            # and waits once more for the real exit event.
            if args.timeout is not None or attempt == 1:
                print(f"state=join-timeout watch_id={watch_id}")
                return 6
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        print(f"state=join-timeout watch_id={watch_id}")
        return 6
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def _watch_entries(root):
    entries = []
    def _mtime(path):
        try:
            return path.stat().st_mtime
        except OSError:      # vanished between glob and stat (review n3)
            return 0.0

    # Path.glob suppresses filesystem errors raised while scanning, making an
    # unreadable existing root indistinguishable from a readable empty one.
    # Enumerate directly so callers can keep retryable work pending on failure.
    try:
        with os.scandir(root) as directory:
            json_paths = [root / entry.name for entry in directory
                          if entry.name.endswith(".json")]
    except FileNotFoundError:
        return []
    for arm_path in sorted(json_paths, key=_mtime, reverse=True):
        name = arm_path.name
        if (name.endswith(".receipt.json") or name.endswith(".ack.json")
                or name.endswith(".observation.json") or name.startswith(".")):
            continue
        watch_id = name[: -len(".json")]
        arm = _read_json(arm_path)
        entries.append((watch_id, arm))
    seen = {wid for wid, _ in entries}
    # Tolerate receipt-without-arm: a caller killed between spawn and the arm
    # write leaves a self-describing receipt and no record.
    for receipt_path in sorted(path for path in json_paths
                               if path.name.endswith(".receipt.json")):
        watch_id = receipt_path.name[: -len(".receipt.json")]
        if watch_id not in seen:
            entries.append((watch_id, None))
    return entries


def _watch_observer_duty_argv(watch_id, arm, *, rearm_generation, lock_fd=None,
                              service_current=None, timeout_before_refs=False,
                              include_rearmed_from=True):
    """Build the accepted watch contract, with service controls kept separate."""
    agent = arm.get("agent") or {}
    steward = arm.get("steward") or {}
    argv = [
        "__watch-run", "--watch-id", str(watch_id),
        "--target", str(arm.get("target", "")),
        "--server", str(arm.get("server") or "default"),
        "--expected-harness", str(agent.get("harness", "-")),
        "--expected-session-id", str(agent.get("session_id", "-")),
        "--expected-pane", str(agent.get("pane", "-")),
        "--steward-harness", str(steward.get("harness", "unknown")),
        "--steward-session-id", str(steward.get("session_id", "")),
        "--steward-project", str(steward.get("project", "")),
        "--armed-ts", str(arm.get("armed_ts", "")),
        "--rearm-count", str(int(rearm_generation)),
    ]
    if lock_fd is not None:
        argv.extend(("--lock-fd", str(lock_fd)))
    until = list(arm.get("until") or [])
    refs = list(arm.get("refs") or [])
    timeout = arm.get("timeout")
    for state in until:
        argv.extend(("--until", str(state)))
    if timeout_before_refs and timeout is not None:
        argv.extend(("--timeout", str(timeout)))
    for ref in refs:
        argv.extend(("--ref", str(ref)))
    if not timeout_before_refs and timeout is not None:
        argv.extend(("--timeout", str(timeout)))
    rearmed_from = arm.get("rearmed_from")
    if include_rearmed_from and rearmed_from:
        argv.extend(("--rearmed-from", str(rearmed_from)))
    if service_current:
        argv.extend(("--service-current", str(service_current)))
    return argv


def _watch_observer_takeover_argvs(watch_id, arm):
    """Return exact argv layouts supported by the recorded watch generation."""
    generation = arm.get("observer_generation") if isinstance(arm, dict) else None
    if (not isinstance(generation, int) or isinstance(generation, bool)
            or generation < 1):
        return None
    try:
        if generation == 1:
            accepted_generation = arm.get("rearm_count")
            if (not isinstance(accepted_generation, int)
                    or isinstance(accepted_generation, bool) or accepted_generation < 0):
                return None
            return [_watch_observer_duty_argv(
                watch_id, arm, rearm_generation=accepted_generation,
                timeout_before_refs=True)]

        # A restart records the next observer_generation just before it
        # spawns. The live process therefore carries the preceding value.
        accepted_generation = generation - 1
        current = _watch_observer_duty_argv(
            watch_id, arm, rearm_generation=accepted_generation)
        variants = [current]
        # v3.35.28/.33 restart builders omitted this lineage-only option even
        # when the arm retained it. Keep that exact historical argv readable.
        legacy = _watch_observer_duty_argv(
            watch_id, arm, rearm_generation=accepted_generation,
            include_rearmed_from=False)
        if legacy != current:
            variants.append(legacy)
        return variants
    except (TypeError, ValueError, OverflowError):
        return None


_WATCH_SERVER_ENDPOINT_PREFIX = "server:"
_WATCH_NATIVE_ENDPOINT_PREFIX = "native:"


def _watch_selected_readiness_endpoint(arm, server=None):
    """Return this watch's selected endpoint, independent of the runner's env."""
    selected_server = server or (arm.get("server") if isinstance(arm, dict) else None) or "default"
    if isinstance(arm, dict) and "readiness_endpoint" in arm:
        endpoint = arm.get("readiness_endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return None
        if endpoint.startswith(_WATCH_SERVER_ENDPOINT_PREFIX):
            expected = _WATCH_SERVER_ENDPOINT_PREFIX + str(selected_server)
            return endpoint if endpoint == expected else None
        if endpoint.startswith(_WATCH_NATIVE_ENDPOINT_PREFIX):
            expected = _WATCH_NATIVE_ENDPOINT_PREFIX + str(selected_server)
            return endpoint if endpoint == expected else None
        return endpoint
    return _WATCH_NATIVE_ENDPOINT_PREFIX + str(selected_server)


def _watch_readiness_identity_matches_arm(identity, arm, native_state):
    """Validate a watch-carried endpoint identity against its immutable target."""
    if not isinstance(identity, dict) or not isinstance(arm, dict):
        return False
    agent = arm.get("agent") or {}
    server = arm.get("server") or "default"
    session_id = agent.get("session_id") or ""
    if session_id == "-":
        session_id = ""
    endpoint = identity.get("endpoint")
    if (not isinstance(endpoint, str) or not endpoint
            or identity.get("server") != server
            or identity.get("pane") != agent.get("pane")
            or identity.get("harness") != agent.get("harness")
            or identity.get("session_id") != session_id
            or identity.get("native_state") != native_state
            or identity.get("purpose") != "completion"):
        return False
    if endpoint.startswith(_WATCH_SERVER_ENDPOINT_PREFIX):
        if endpoint != _WATCH_SERVER_ENDPOINT_PREFIX + str(server):
            return False
    if ("readiness_endpoint" in arm
            and endpoint != _watch_selected_readiness_endpoint(arm, server)):
        return False
    pane_identity = arm.get("pane_identity") or arm.get("foreground_identity") or {}
    if int(arm.get("observer_protocol", 0) or 0) >= 3:
        shell_pid = agent.get("pid")
        shell_start = str(agent.get("pid_start") or "")
    else:
        shell_pid = pane_identity.get("shell_pid")
        shell_start = str(pane_identity.get("shell_start") or "")
    if shell_pid is not None:
        try:
            if (int(identity.get("shell_pid")) != int(shell_pid)
                    or str(identity.get("shell_start") or "") != shell_start):
                return False
        except (TypeError, ValueError):
            return False
    elif identity.get("shell_pid") is not None or identity.get("shell_start") not in {None, ""}:
        return False
    return True


def _steward_check_module():
    """The periodic steward-check schedule (lazy: a missing module only disables it)."""
    import importlib
    return importlib.import_module("steward_check")
