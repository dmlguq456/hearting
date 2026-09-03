#!/usr/bin/env python3
"""SD-122 steward surfaces over herdr: (9) wait/start, (10) watch/join/status/rearm/ack.

Checked wrapper around `herdr agent wait|get|start` — no self-written sleep or
poll loop, event-driven only. Ledger writes go through
`utilities/peer-message.py`'s own `cmd_record`, so the ledger root is always
resolved via `dispatch_contract.resolve_dispatch_state_root` exactly as the
writer of every other peer_message_v1 record resolves it — never a
hardcoded stable literal.

(10) adds three separable lifetimes. `watch` spawns a detached watcher whose
lifetime is not the caller's tool-task lifetime (backgrounding `wait` lost it
to Claude Code interrupt/lifecycle kills 3/3, hearting-21 2026-09-02~03); the
watcher fixes the completion fact in an immutable disk receipt that outlives
it; and `ack` makes an at-least-once wake idempotent to display. `wait` keeps
its bounded-foreground semantics unchanged.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_UTILITIES_DIR = Path(__file__).resolve().parent

_PM_SPEC = importlib.util.spec_from_file_location(
    "peer_message", str(_UTILITIES_DIR / "peer-message.py")
)
peer_message = importlib.util.module_from_spec(_PM_SPEC)
_PM_SPEC.loader.exec_module(peer_message)

sys.path.insert(0, str(_UTILITIES_DIR))
from dispatch_contract import process_start_ticks  # noqa: E402

_DEFAULTS_SPEC = importlib.util.spec_from_file_location(
    "dispatch_defaults", str(_UTILITIES_DIR / "dispatch-defaults.py")
)
DEFAULTS = importlib.util.module_from_spec(_DEFAULTS_SPEC)
_DEFAULTS_SPEC.loader.exec_module(DEFAULTS)

_PERMISSION_FLAGS = {
    "claude": ["--permission-mode", "bypassPermissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "opencode": ["--auto"],
}


def _current_session_identity():
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return os.environ["CLAUDE_CODE_SESSION_ID"], "claude"
    if os.environ.get("CODEX_THREAD_ID"):
        return os.environ["CODEX_THREAD_ID"], "codex"
    if os.environ.get("AGENT_SESSION_ID"):
        return os.environ["AGENT_SESSION_ID"], "unknown"
    return "", "unknown"


def _project_of(cwd):
    if not cwd:
        return ""
    return os.path.basename(str(cwd).rstrip("/"))


def _fallback():
    return "claude-native-notify-idle" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "poll-fallback"


def _default_permission_mode():
    try:
        cfg = DEFAULTS.load_and_validate(
            DEFAULTS.default_config_path(), DEFAULTS.default_topology_path()
        )
        return DEFAULTS.query_steward_child_permission_mode(cfg)
    except Exception:
        return DEFAULTS.DEFAULT_STEWARD_CHILD_PERMISSION_MODE


def _record(*, to_harness, to_name, kind, ref=None, summary_text=None,
            receipt=None, status="sent", from_identity=None):
    """Write one peer_message_v1 row.

    `from_identity` exists for the detached watcher: `_current_session_identity`
    reads the *environment*, and a watcher re-armed from a hook does not
    necessarily inherit the steward's environment. The watcher therefore carries
    the steward identity on argv and passes it here explicitly. `wait`/`start`
    keep the environment-derived default.
    """
    if from_identity is not None:
        from_sid, from_harness, from_project = from_identity
    else:
        from_sid, from_harness = _current_session_identity()
        from_project = _project_of(os.getcwd())
    body_file = None
    tmp_path = None
    if summary_text is not None:
        fd, tmp_path = tempfile.mkstemp(prefix="peer-steward-summary-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(summary_text)
        body_file = tmp_path
    ns = peer_message.argparse.Namespace(
        from_harness=from_harness,
        from_session_id=from_sid,
        from_project=from_project,
        to_harness=to_harness,
        to_session_id=None,
        to_name=to_name,
        kind=kind,
        surface="herdr",
        status=status,
        receipt=receipt,
        ref=list(ref or []),
        body_file=body_file,
        body_stdin=False,
    )
    try:
        peer_message.cmd_record(ns)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _herdr_missing():
    return shutil.which("herdr") is None


def _unavailable(reason):
    print(f"herdr-unavailable reason={reason} fallback={_fallback()}")
    return 4


def _run_herdr_wait(target, until, timeout_ms):
    cmd = ["herdr", "agent", "wait", target]
    for state in until or []:
        cmd += ["--until", state]
    if timeout_ms is not None:
        cmd += ["--timeout", str(timeout_ms)]
    global _LAST_HERDR_EXIT
    _LAST_HERDR_EXIT = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    _LAST_HERDR_EXIT = proc.returncode
    # Measured 2026-09-02 (herdr 0.8.0): a successful wait prints its
    # `agent_info` JSON on stdout and exits 0, but `{"error":{"code":...}}`
    # goes to STDERR with exit 1.  Reading stdout alone therefore turns every
    # real `timeout` and `agent_not_found` into `herdr-unavailable`, which is
    # the wrong exit code (4 instead of 3/2) and the wrong fallback advice.
    for stream in (proc.stdout, proc.stderr):
        try:
            payload = json.loads(stream)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


_AGENT_STATES = ("idle", "done", "blocked", "working", "unknown")
_LAST_HERDR_EXIT = None          # real `herdr agent wait` return code of the last call (m3)
_HERDR_GET_TIMEOUT_SECONDS = 15  # `watch` pre-check must never hang the caller (M3)
_CLAIM_TIMEOUT_MS = 15_000       # dedupe claim acquisition bound (M3)


def _typed_line(state, harness, session_id, name, pane):
    """The (9) five-field typed line. Extracted from `cmd_wait` verbatim so
    `wait`, `watch`'s watcher, `join` and `status` cannot drift apart; the
    pre-existing A51-1 assertions are the byte-identity guard."""
    return f"state={state} agent={harness} session_id={session_id} name={name} pane={pane}"


def _interpret_payload(payload, target):
    """Map a herdr payload onto ((9) state, agent fields, exit code).

    Returns `(state, agent, exit_code, unavailable_reason)`. `unavailable_reason`
    is non-None only when the caller should render `herdr-unavailable`.
    """
    blank = {"harness": "-", "session_id": "-", "name": target, "pane": "-"}
    if not isinstance(payload, dict):
        return "herdr-unavailable", blank, 4, "herdr-protocol-error"

    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if code == "timeout":
            return "timeout", blank, 3, None
        if code == "agent_not_found":
            return "agent-not-found", blank, 2, None
        return "herdr-unavailable", blank, 4, f"herdr-error-code-{code or 'unknown'}"

    result = payload.get("result")
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        return "herdr-unavailable", blank, 4, "herdr-malformed-result"

    state = agent.get("agent_status") or "unknown"
    if state not in _AGENT_STATES:
        state = "unknown"
    return state, {
        "harness": agent.get("agent") or "-",
        "session_id": (agent.get("agent_session") or {}).get("value") or "-",
        "name": agent.get("name") or "-",
        "pane": agent.get("pane_id") or "-",
    }, 0, None


def cmd_wait(args):
    target = args.target
    # S6: one record at wait start, append-only, never updated on return.
    _record(to_harness="unknown", to_name=target, kind="watch", ref=args.ref)

    if _herdr_missing():
        return _unavailable("herdr-not-found")

    payload = _run_herdr_wait(target, args.until, args.timeout)
    state, agent, code, reason = _interpret_payload(payload, target)
    if reason is not None:
        return _unavailable(reason)
    print(_typed_line(state, agent["harness"], agent["session_id"], agent["name"], agent["pane"]))
    return code


def cmd_start(args):
    if _herdr_missing():
        return _unavailable("herdr-not-found")

    mode = args.permission_mode or _default_permission_mode()
    agent_args = list(getattr(args, "agent_args", None) or [])
    prefix = list(_PERMISSION_FLAGS.get(args.kind, [])) if mode == "bypass" else []
    full_agent_args = prefix + agent_args

    # herdr `agent start <NAME> --kind --pane` — the display name is a required
    # positional (herdr 0.8+ prints `unknown option: <kind>` and starts nothing when
    # it is missing; measured 2026-09-03, F-100 comms test).
    cmd = ["herdr", "agent", "start", args.name, "--kind", args.kind, "--pane", args.pane]
    if full_agent_args:
        cmd += ["--"] + full_agent_args

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=args.cwd or None)
    except (OSError, subprocess.SubprocessError):
        return _unavailable("herdr-invocation-failed")

    started = proc.returncode == 0
    _record(
        to_harness=args.kind, to_name=args.name, kind="steer",
        summary_text=f"[start] {args.name} kind={args.kind} mode={mode}",
    )
    print(
        f"started={str(started).lower()} agent={args.kind} name={args.name} "
        f"pane={args.pane} permission_mode={mode}"
    )
    return 0


# ---------------------------------------------------------------------------
# SD-122 (10) detached steward watch: watch / join / status / rearm / ack
# ---------------------------------------------------------------------------

_WATCH_SCHEMA = 1
_WATCH_STATES = _AGENT_STATES + ("timeout", "agent-not-found", "herdr-unavailable")
_JOIN_EXIT = {"timeout": 3, "agent-not-found": 2, "herdr-unavailable": 4}


def _watch_root():
    """`peer-watches/` beside `peer-messages/`, under the one resolved state root.

    Routed through `peer_message._ledger_root()` on purpose: resolving the
    dispatch state root a second time here would let the ledger root and the
    watch root diverge whenever the resolver's inputs differ, and a watch whose
    receipt lives beside a different ledger is unfindable.
    """
    return peer_message._ledger_root() / "peer-watches"


@dataclass(frozen=True)
class _WatchPaths:
    arm: Path
    lock: Path
    log: Path
    receipt: Path
    ack: Path


def _watch_paths(watch_id, root=None):
    root = root or _watch_root()
    return _WatchPaths(
        arm=root / f"{watch_id}.json",
        lock=root / f"{watch_id}.lock",
        log=root / f"{watch_id}.log",
        receipt=root / f"{watch_id}.receipt.json",
        ack=root / f"{watch_id}.ack.json",
    )


def _new_watch_id(steward_sid, target, armed_ts, nonce):
    raw = f"{steward_sid}|{target}|{armed_ts}|{nonce}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _dedupe_key(steward_sid, target, until):
    # `until=[]` (herdr's default set) and `until=["idle","done","blocked"]` are
    # the same *behaviour* but stay distinct keys on purpose: (10) dedupes on the
    # "until 집합" as given, and silently normalizing them would suppress a
    # legitimate second watch.
    raw = f"{steward_sid}|{target}|{'|'.join(sorted(until or []))}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def _is_zombie(pid):
    """A reaped-but-not-yet-collected watcher is dead, not pending.

    A zombie still has `/proc/<pid>/stat` with matching start ticks, so PID
    identity alone would call it alive. It holds no file locks, so `_alive`
    already rejects it; `cmd_join` cannot use the lock condition (it holds the
    lock itself) and needs this explicitly.
    """
    try:
        raw = (Path("/proc") / str(int(pid)) / "stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    tail = raw[raw.rfind(")") + 2:].split()
    return bool(tail) and tail[0] == "Z"


def _pid_identity_ok(pid, pid_start):
    """§5.12 PID identity: the pid exists *and* its start ticks still match."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    observed = process_start_ticks(pid)
    if observed is None or str(observed) != str(pid_start):
        return False
    return not _is_zombie(pid)


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
    return (
        f"watch_id={arm['watch_id']} state=armed target={arm['target']} "
        f"until={_until_field(arm.get('until'))} wake={arm.get('wake', 'none')} "
        f"pid={watcher.get('pid', '-')} pid_start={watcher.get('pid_start', '-')} "
        f"receipt={paths.receipt}"
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


def _run_herdr_get(target):
    try:
        proc = subprocess.run(
            ["herdr", "agent", "get", target], capture_output=True, text=True,
            timeout=_herdr_get_timeout(),   # a wedged socket is `herdr-unavailable`, not a hang (M3)
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for stream in (proc.stdout, proc.stderr):
        try:
            payload = json.loads(stream)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def cmd_watch(args):
    root = _watch_root()
    root.mkdir(parents=True, exist_ok=True)
    identity = getattr(args, "steward_identity", None)
    if identity:
        # `rearm` (possibly running inside a hook whose environment carries no
        # session id) must keep the ORIGINAL steward identity: the dedupe key,
        # the receipt's `steward.session_id` and the sweep's `--undelivered`
        # filter all hang off it (review M1).
        steward_sid, steward_harness, steward_project = identity
    else:
        steward_sid, steward_harness = _current_session_identity()
        steward_project = _project_of(os.getcwd())
    target = args.target
    until = list(args.until or [])
    wake = args.wake
    if wake == "auto":
        wake = "hook" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "none"

    # Dedupe is its own serialization point, entered BEFORE the herdr pre-checks
    # and held until the winning watch_id is published. A bare `O_EXCL` create
    # serializes only the first arm: two callers arriving while a claimed watcher
    # is dead would both decide to reclaim and both spawn -- the exact
    # double-spawn the claim exists to prevent. One blocking lock over
    # decide + spawn + publish removes that whole class, and it lives no longer
    # than this foreground call.
    #
    # `until=[]` (herdr's default set) and `until=["idle","done","blocked"]` are
    # the same *behaviour* but stay distinct keys on purpose: (10) dedupes on the
    # "until 집합" as given, and silently normalizing them would suppress a
    # legitimate second watch.
    claim = root / f"{_dedupe_key(steward_sid, target, until)}.arm"
    try:
        claim_fd = _open_lock(claim)
    except OSError as exc:
        return _unavailable(f"watch-claim-failed-{exc.errno}")
    try:
        if not _acquire_bounded(claim_fd, _CLAIM_TIMEOUT_MS):
            return _unavailable("watch-claim-contended")

        existing_id = ""
        try:
            existing_id = claim.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if existing_id:
            existing_paths = _watch_paths(existing_id, root)
            existing_arm = _read_json(existing_paths.arm)
            if not existing_paths.receipt.exists() and _watcher_present(existing_arm):
                print(_already_armed_line(existing_id, existing_arm, existing_paths))
                return 0
            # Dead with no receipt, or already finished: the key is reclaimable.

        # herdr pre-checks. Nothing beyond the claim exists yet, so an early
        # return leaves no watch state behind.
        if _herdr_missing():
            return _unavailable("herdr-not-found")
        state, agent, _code, reason = _interpret_payload(_run_herdr_get(target), target)
        if state == "agent-not-found":
            print(_typed_line("agent-not-found", "-", "-", target, "-"))
            return 2
        if reason is not None:
            return _unavailable(reason)

        armed_ts = _utc_now()
        watch_id = _new_watch_id(steward_sid, target, armed_ts, os.urandom(8).hex())
        paths = _watch_paths(watch_id, root)
        log_fd = os.open(str(paths.log), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)

        # Take the watch lock here, before the spawn, and hand the open file
        # description to the watcher. A flock belongs to the description, not the
        # fd, so the child keeps holding it after this process closes its copy,
        # and the lock is held continuously from before `watch` prints its armed
        # line. Letting the watcher take its own lock leaves a spawn-latency
        # window in which a `join` wins the lock first, sees no receipt, and
        # reports a healthy watch as timed out or dead.
        lock_fd = _open_lock(paths.lock)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock_fd)
            os.close(log_fd)
            return _unavailable("watch-lock-contended")
        os.set_inheritable(lock_fd, True)

        argv = [
            sys.executable, str(Path(__file__).resolve()), "__watch-run",
            "--watch-id", watch_id, "--target", target,
            "--steward-harness", steward_harness, "--steward-session-id", steward_sid,
            "--steward-project", steward_project, "--armed-ts", armed_ts,
            "--rearm-count", str(args.rearm_count or 0),
            "--lock-fd", str(lock_fd),
        ]
        for state_name in until:
            argv += ["--until", state_name]
        if args.timeout is not None:
            argv += ["--timeout", str(args.timeout)]
        for ref in args.ref or []:
            argv += ["--ref", ref]
        if getattr(args, "rearmed_from", None):
            argv += ["--rearmed-from", args.rearmed_from]

        try:
            # Re-exec, never fork: a fork inherits the caller's process group,
            # file descriptors and interpreter state, which is precisely the
            # coupling this contract removes.
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True, pass_fds=(lock_fd,), cwd=str(root),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            os.close(log_fd)
            os.close(lock_fd)
            return _unavailable(f"watcher-spawn-failed-{getattr(exc, 'errno', 'unknown')}")
        os.close(log_fd)
        # Closing this copy does not release the flock: the child still holds a
        # descriptor for the same open file description.
        os.close(lock_fd)

        # Spawn first, then the immutable arm record already carrying the real
        # watcher identity. The reverse order would need a mutable record or a
        # second write. It is safe because the watcher takes target, until,
        # timeout and steward identity from argv and never reads the arm record.
        arm = {
            "schema_version": _WATCH_SCHEMA,
            "watch_id": watch_id,
            "target": target,
            "until": until,
            "timeout": args.timeout,
            "refs": list(args.ref or []),
            "steward": {
                "harness": steward_harness,
                "session_id": steward_sid,
                "project": steward_project,
            },
            "armed_ts": armed_ts,
            "wake": wake,
            "watcher": {"pid": proc.pid, "pid_start": process_start_ticks(proc.pid) or ""},
            "rearmed_from": getattr(args, "rearmed_from", None) or None,
            "rearm_count": int(args.rearm_count or 0),
        }
        _write_json_atomic(paths.arm, arm)

        # Publish the winning watch_id into the claim while still holding it.
        try:
            os.ftruncate(claim_fd, 0)
            os.lseek(claim_fd, 0, os.SEEK_SET)
            os.write(claim_fd, watch_id.encode("ascii"))
            os.fsync(claim_fd)
        except OSError:
            pass

        # `receipt=<watch_id>` is what distinguishes this `kind=watch` row from
        # (9) `wait`'s, which carries no receipt. Written while the claim is
        # still held so a caller killed after the spawn cannot leave a live
        # watcher with no arm row (review n4).
        _record(
            to_harness=agent["harness"] if agent["harness"] != "-" else "unknown",
            to_name=target, kind="watch", ref=args.ref, receipt=watch_id,
            from_identity=(steward_sid, steward_harness, steward_project),
        )
    finally:
        os.close(claim_fd)

    print(_armed_line(arm, paths))
    return 0


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
        f"wake={wake} pid={pid} pid_start={pid_start} receipt={paths.receipt}"
    )


def cmd_watch_run(args):
    """The detached watcher. Reached only by re-exec from `cmd_watch`."""
    root = _watch_root()
    paths = _watch_paths(args.watch_id, root)
    if args.lock_fd is not None and args.lock_fd >= 0:
        lock_fd = args.lock_fd            # already flocked by the caller
    else:
        lock_fd = _open_lock(paths.lock)  # direct invocation fallback
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

    payload = _run_herdr_wait(args.target, args.until, args.timeout)  # exactly once
    state, agent, _code, reason = _interpret_payload(payload, args.target)
    if reason is not None:
        # A herdr invocation that cannot be interpreted still terminates the
        # watch: an un-receipted watcher waiting forever is the failure mode this
        # whole contract exists to remove.
        state = "herdr-unavailable"

    pid = os.getpid()
    receipt = {
        "schema_version": _WATCH_SCHEMA,
        "watch_id": args.watch_id,
        "target": args.target,
        "steward": {
            "harness": args.steward_harness,
            "session_id": args.steward_session_id,
            "project": args.steward_project,
        },
        "armed_ts": args.armed_ts,
        "done_ts": _utc_now(),
        "state": state,
        "agent": agent,
        "herdr_exit": _LAST_HERDR_EXIT if _LAST_HERDR_EXIT is not None else -1,
        # Self-describing: taken from this process, not from the arm record, so
        # a caller killed between spawn and the arm write still yields a usable
        # receipt.
        "watcher": {"pid": pid, "pid_start": process_start_ticks(pid) or ""},
        "rearmed_from": args.rearmed_from or None,
        "refs": list(args.ref or []),
    }
    _write_json_atomic(paths.receipt, receipt)

    _record(
        to_harness=agent["harness"] if agent["harness"] != "-" else "unknown",
        to_name=args.target, kind="notice", ref=args.ref, receipt=args.watch_id,
        status="received",
        summary_text=f"[notice] watch {args.watch_id} state={state} target={args.target}",
        from_identity=(args.steward_session_id, args.steward_harness, args.steward_project),
    )
    # No LOCK_UN, ever: lock release IS process exit, and the receipt rename
    # strictly precedes it. A watcher that unlocked before writing would make
    # every concurrent `join` report a dead watcher.
    os._exit(0)


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
    if not root.is_dir():
        return []
    entries = []
    def _mtime(path):
        try:
            return path.stat().st_mtime
        except OSError:      # vanished between glob and stat (review n3)
            return 0.0

    for arm_path in sorted(root.glob("*.json"), key=_mtime, reverse=True):
        name = arm_path.name
        if name.endswith(".receipt.json") or name.endswith(".ack.json") or name.startswith("."):
            continue
        watch_id = name[: -len(".json")]
        arm = _read_json(arm_path)
        entries.append((watch_id, arm))
    seen = {wid for wid, _ in entries}
    # Tolerate receipt-without-arm: a caller killed between spawn and the arm
    # write leaves a self-describing receipt and no record.
    for receipt_path in sorted(root.glob("*.receipt.json")):
        watch_id = receipt_path.name[: -len(".receipt.json")]
        if watch_id not in seen:
            entries.append((watch_id, None))
    return entries


def _status_entry(watch_id, arm, root):
    paths = _watch_paths(watch_id, root)
    receipt = _read_json(paths.receipt)
    if arm is None and receipt is not None:
        arm = {
            "watch_id": watch_id, "target": receipt.get("target", "-"), "until": [],
            "steward": receipt.get("steward") or {}, "wake": "-",
            "watcher": receipt.get("watcher") or {}, "armed_ts": receipt.get("armed_ts", "-"),
            "refs": receipt.get("refs") or [],
        }
    arm = arm or {"watch_id": watch_id}
    if paths.ack.exists():
        state = "acked"
    elif receipt is not None:
        state = "receipt"
    elif _alive(arm, root):
        state = "alive"
    else:
        state = "armed"
    watcher = arm.get("watcher") or {}
    return {
        "watch_id": watch_id,
        "state": state,
        "target": arm.get("target", "-"),
        "until": arm.get("until") or [],
        "wake": arm.get("wake", "-"),
        "armed_ts": arm.get("armed_ts", "-"),
        "steward": arm.get("steward") or {},
        "watcher": {"pid": watcher.get("pid", "-"), "pid_start": watcher.get("pid_start", "-")},
        "receipt": str(paths.receipt),
        "ack": str(paths.ack),
        "receipt_state": (receipt or {}).get("state"),
        "agent": (receipt or {}).get("agent") or {},
    }


def cmd_status(args):
    root = _watch_root()
    entries = _watch_entries(root)
    if args.watch:
        entries = [(w, a) for w, a in entries if w == args.watch]
    rows = [_status_entry(w, a, root) for w, a in entries]
    if args.undelivered:
        sid, _harness = _current_session_identity()
        rows = [
            row for row in rows
            if row["state"] == "receipt" and (row["steward"] or {}).get("session_id") == sid
        ]
    if args.json:
        print(json.dumps({"watch_root": str(root), "watches": rows}, ensure_ascii=False))
        return 0
    for row in rows:
        agent = row["agent"] or {}
        print(
            _typed_line(
                row["receipt_state"] or row["state"], agent.get("harness", "-"),
                agent.get("session_id", "-"), agent.get("name", row["target"]),
                agent.get("pane", "-"),
            )
            + f" watch_id={row['watch_id']} receipt={row['receipt']}"
        )
    return 0


def cmd_rearm(args):
    root = _watch_root()
    watch_id = args.watch_id
    paths = _watch_paths(watch_id, root)
    arm = _read_json(paths.arm)

    if paths.receipt.exists():
        print(f"watch_id={watch_id} state=already-done receipt={paths.receipt}")
        return 0
    if _watcher_present(arm):
        print(_already_armed_line(watch_id, arm, paths).replace("state=already-armed", "state=alive"))
        return 0
    if arm is None:
        print(f"watch_id={watch_id} state=unknown receipt={paths.receipt}")
        return 0

    # No claim bookkeeping here: `cmd_watch` holds the dedupe lock, sees that the
    # claimed watch is dead with no receipt, and reclaims the key itself.
    steward = arm.get("steward") or {}
    ns = argparse.Namespace(
        target=arm["target"], until=list(arm.get("until") or []), timeout=arm.get("timeout"),
        ref=list(arm.get("refs") or []), wake=arm.get("wake", "none"),
        rearmed_from=watch_id, rearm_count=int(arm.get("rearm_count", 0)) + 1,
        steward_identity=(
            steward.get("session_id", ""), steward.get("harness", "unknown"),
            steward.get("project", ""),
        ),
    )
    import io
    buffer = io.StringIO()
    stdout, sys.stdout = sys.stdout, buffer
    try:
        code = cmd_watch(ns)
    finally:
        sys.stdout = stdout
    line = buffer.getvalue().strip()
    if code != 0 or "state=armed" not in line:
        print(line)
        return code
    print(line.replace("state=armed", f"state=rearmed rearmed_from={watch_id}"))
    return 0


def cmd_ack(args):
    """The single O_EXCL ack implementation.

    A second carrier is expected and is not an error: wake is at-least-once,
    display is idempotent. If `FileExistsError` ever escaped here, a carrier
    that lost the race would exit 0 where it must exit 2.
    """
    paths = _watch_paths(args.watch_id)
    session_id, _harness = _current_session_identity()
    try:
        fd = os.open(str(paths.ack), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        print(f"watch_id={args.watch_id} ack=already")
        return 0
    except OSError as exc:
        print(f"watch_id={args.watch_id} ack=failed reason=errno-{exc.errno}", file=sys.stderr)
        return 0
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {"carrier": args.carrier, "session_id": session_id, "ts": _utc_now()},
            fh, ensure_ascii=False,
        )
    print(f"watch_id={args.watch_id} ack=created carrier={args.carrier}")
    return 0



def build_parser():
    parser = argparse.ArgumentParser(prog="peer-steward")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_wait = sub.add_parser("wait")
    p_wait.add_argument("target")
    p_wait.add_argument("--until", action="append", default=[])
    p_wait.add_argument("--timeout", type=int, default=None)
    p_wait.add_argument("--ref", action="append", default=[])
    p_wait.set_defaults(func=cmd_wait)

    p_start = sub.add_parser("start")
    p_start.add_argument("name")
    p_start.add_argument("--kind", required=True, choices=("claude", "codex", "opencode"))
    p_start.add_argument("--pane", required=True)
    p_start.add_argument("--cwd", default=None)
    p_start.add_argument("--permission-mode", choices=("bypass", "inherit"), default=None)
    p_start.set_defaults(func=cmd_start)

    # --- SD-122 (10) detached steward watch ---
    p_watch = sub.add_parser("watch")
    p_watch.add_argument("target")
    p_watch.add_argument("--until", action="append", default=[])
    p_watch.add_argument("--timeout", type=int, default=None)
    p_watch.add_argument("--ref", action="append", default=[])
    p_watch.add_argument("--wake", choices=("auto", "hook", "none"), default="auto")
    p_watch.set_defaults(func=cmd_watch, rearmed_from=None, rearm_count=0)

    # Hidden: the detached watcher's own entry point, reached only by re-exec.
    p_run = sub.add_parser("__watch-run")
    p_run.add_argument("--watch-id", required=True)
    p_run.add_argument("--target", required=True)
    p_run.add_argument("--until", action="append", default=[])
    p_run.add_argument("--timeout", type=int, default=None)
    p_run.add_argument("--ref", action="append", default=[])
    p_run.add_argument("--steward-harness", default="unknown")
    p_run.add_argument("--steward-session-id", default="")
    p_run.add_argument("--steward-project", default="")
    p_run.add_argument("--armed-ts", default="")
    p_run.add_argument("--rearmed-from", default=None)
    p_run.add_argument("--rearm-count", type=int, default=0)
    p_run.add_argument("--lock-fd", type=int, default=None)
    p_run.set_defaults(func=cmd_watch_run)

    p_join = sub.add_parser("join")
    p_join.add_argument("watch_id")
    p_join.add_argument("--timeout", type=int, default=None)
    p_join.set_defaults(func=cmd_join)

    p_status = sub.add_parser("status")
    p_status.add_argument("--watch", default=None)
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument("--undelivered", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_rearm = sub.add_parser("rearm")
    p_rearm.add_argument("watch_id")
    p_rearm.set_defaults(func=cmd_rearm)

    p_ack = sub.add_parser("ack")
    p_ack.add_argument("watch_id")
    p_ack.add_argument("--carrier", required=True)
    p_ack.set_defaults(func=cmd_ack)

    return parser


def _split_agent_args(argv):
    """Split raw argv on a literal `--`: everything after it is agent-args,
    passed through untouched. argparse's own REMAINDER nargs greedily
    swallows *every* remaining token — including options meant for
    peer-steward itself, like `--kind`/`--pane` — from the first positional
    onward, so the split must happen before argparse ever sees the args.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1:]
    return argv, []


def main(argv=None):
    parsed_argv, agent_args = _split_agent_args(argv)
    args = build_parser().parse_args(parsed_argv)
    args.agent_args = agent_args
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
