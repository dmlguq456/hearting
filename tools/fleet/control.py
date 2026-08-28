"""F-27 — limited, user-initiated session control (PRD v8 §4.8, prd.md:250-255).

The ONLY module in fleet that is allowed to signal a process or write a registry, and it
does neither unless a human pressed a key. Everything here is fail-closed.

Why this is a module and not part of render.py: the safety contract has to be testable
WITHOUT curses. A guard nobody can unit-test is not a guard.

Three hard rules, in order of importance:
  1. **Never signal the wrong process.** A pid is not an identity — (pid, start-time) is.
     The start-time is re-read immediately before the signal and compared against what was
     collected; any mismatch (or any doubt) refuses. A recycled pid is the whole reason.
  2. **Never signal without an explicit human decision.** No function here is reachable from
     the collector, --json, --once, or any scheduler. There is no automatic escalation:
     SIGKILL after SIGTERM requires a SECOND, separate confirmation.
  3. **Never signal fleet itself, or the session driving it.**

Everything a signal does is appended to a bounded action log so it can be audited later.
"""
import errno
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

from .collectors.procscan import read_environ, read_proc_start  # noqa: F401  (re-export)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    reconcile_attempt_terminal,
)
from dispatch_completion_join import materialize_after_terminal_close  # noqa: E402

# SIGTERM → wait → (ASK AGAIN) → SIGKILL. This is the wait, not an auto-escalation timer:
# nothing escalates when it expires; the UI re-prompts and the user decides again.
KILL_GRACE_SEC = 5

ACTION_LOG_MAX_BYTES = 1 << 20        # 1 MiB, then rotate to .1 (2 files total, bounded)

# Session states a single confirmation may terminate (prd.md:251). `unused` is here because
# F-26's whole point is that such a row is a cleanup candidate (prd.md:248).
SINGLE_CONFIRM_STATES = ("unused", "stale", "dead")
# Demonstrably alive → a second, DIFFERENT confirmation. prd.md:251 scopes this to SESSIONS.
DOUBLE_CONFIRM_STATES = ("working",)


def actions_root():
    """Action-log directory. FLEET_ACTION_STATE_DIR exists so tests are hermetic; the
    default follows the XDG state convention already used by titles.py:30."""
    override = os.environ.get("FLEET_ACTION_STATE_DIR")
    if override:
        return override
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "agent-fleet", "actions")


def _log_path():
    return os.path.join(actions_root(), "actions.jsonl")


def _rotate_if_needed(path):
    """Keep the log bounded at 2 files. Called under the same lock as the append."""
    try:
        if os.path.getsize(path) < ACTION_LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(path, path + ".1")       # atomic; overwrites any previous .1
    except OSError:
        pass


def log_action(**kw):
    """Append one action row. Never raises: failing to log must not change what the caller
    does about a signal, and must never crash the TUI.

    Row: ts, action(sigterm|sigkill|refused|close_row), pid, sid, state, approval,
         result(ok|refused|error), reason.
    """
    row = {"ts": kw.get("ts") or time.time(),
           "action": kw.get("action"), "pid": kw.get("pid"), "sid": kw.get("sid"),
           "state": kw.get("state"), "approval": kw.get("approval"),
           "result": kw.get("result"), "reason": kw.get("reason")}
    try:
        root = actions_root()
        os.makedirs(root, exist_ok=True)
        path = _log_path()
        lock_path = os.path.join(root, ".lock")
        with open(lock_path, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                _rotate_if_needed(path)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    return row


def _ancestors(pid, max_depth=16):
    """Ancestor pids of `pid`, best-effort. Killing any of them kills fleet with it."""
    out = []
    cur = pid
    for _ in range(max_depth):
        try:
            with open("/proc/%d/stat" % cur) as f:
                data = f.read()
            cur = int(data[data.rindex(")") + 1:].split()[1])   # field 4 = ppid
        except (OSError, ValueError, IndexError):
            break
        if cur <= 1 or cur in out:
            break
        out.append(cur)
    return out


def _current_session_pid():
    """pid of the harness session driving fleet right now, via CLAUDE_CODE_SESSION_ID →
    the registry file that claims that sessionId. None when it cannot be resolved."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    try:
        names = os.listdir(os.path.join(home, "sessions"))
    except OSError:
        return None
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(home, "sessions", name)) as f:
                d = json.load(f)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("sessionId") == sid:
            p = d.get("pid")
            return p if isinstance(p, int) else None
    return None


def is_excluded(pid):
    """True = this pid may never be a kill target, and is removed from selection BEFORE any
    prompt can be shown. Covers fleet itself, its whole ancestry (the terminal/tmux/harness
    that owns it), the session currently driving fleet, and init.

    Unresolvable input is excluded too — fail closed.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 1:
        return True                       # init / invalid
    own = os.getpid()
    if pid in (own, os.getppid()):
        return True
    if pid in _ancestors(own):
        return True
    cur = _current_session_pid()
    if cur is not None and pid == cur:
        return True
    return False


def verify_reason(pid, proc_start):
    """None when the target is verified, else the precise reason it is refused.

    The reasons are kept distinct because this is what an audit reads (prd.md:253):
      · no_proc_start       — no start-time was ever COLLECTED for this row
      · target_gone         — /proc says the pid is no longer there (or is unreadable)
      · start_time_mismatch — a start-time WAS collected and it DIFFERS → the pid was recycled
    Collapsing the first into the third would make the log claim fleet detected PID reuse when
    it merely never had the evidence — a false alarm and a lie in the audit trail.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "invalid_pid"
    if not proc_start:
        return "no_proc_start"
    actual = read_proc_start(pid)
    if actual is None:
        return "target_gone"
    if str(actual) != str(proc_start):
        return "start_time_mismatch"
    return None


def verify_target(pid, proc_start):
    """True only when `pid` is STILL the exact process that was collected.

    Re-reads /proc/<pid>/stat field 22 at signal time and compares it with the start-time
    captured during collection. Anything else — the process is gone, the start-time differs
    (pid recycled), /proc is unreadable, or no start-time was ever captured — is False.

    Fail-closed on purpose: the cost of refusing a legitimate kill is one keystroke; the
    cost of a false accept is killing an unrelated process.
    """
    return verify_reason(pid, proc_start) is None


def requires_double_confirm(state, registry_status=None):
    """A demonstrably-live target needs a second, DIFFERENT confirmation (prd.md:253)."""
    return state in DOUBLE_CONFIRM_STATES or registry_status == "busy"


def single_confirm_allowed(state, registry_status=None, is_worker=False, kind="session"):
    """prd.md:251's allowed-target whitelist, stated once, here.

    "기본 허용 대상 = unused/stale/dead/idle worker 세션과 registry 잡의 exact pid;
     working/busy 세션은 경고 + 이중 확인."

    A WHITELIST on purpose: an unrecognized or newly-added state must fall through to
    "needs more confirmation", never sail past on one keystroke. Default deny.
    """
    if kind == "job":
        # Checked BEFORE the live-target gate on purpose: prd.md:251 scopes the warning +
        # double confirmation to SESSIONS ("working/busy 세션"), and lists "registry 잡의
        # exact pid" as basic-allowed with no state qualifier. A dispatch job is a disposable
        # worker the user launched; a session is a human's working context. The spec draws the
        # line there deliberately (user-confirmed 2026-07-15), so a `working` job stays a
        # single-confirm target.
        return True
    if requires_double_confirm(state, registry_status):
        return False
    if state in SINGLE_CONFIRM_STATES:
        return True
    return state == "idle" and bool(is_worker)     # a leftover headless worker


def kill_target(pid, proc_start, sid, state, approval, registry_status=None,
                is_worker=False, kind="session"):
    """Signal one verified target. Returns 'ok' | 'refused' | 'error'.

    approval: 'single'    → SIGTERM, allowed ONLY for SINGLE_CONFIRM_STATES
              'double'    → SIGTERM for a live target, after two confirmations
              'escalated' → SIGKILL, only after the grace window AND a fresh confirmation

    The caller owns the human interaction; this function's job is to refuse anything that is
    not provably safe, log the outcome, and otherwise send exactly one signal. It does NOT
    trust the caller: every gate here is re-checked independently of the UI, which is the
    reason this module exists apart from render.py.
    """
    base = {"pid": pid, "sid": sid, "state": state, "approval": approval}

    if approval not in ("single", "double", "escalated"):
        log_action(action="refused", result="refused", reason="no_approval", **base)
        return "refused"
    if is_excluded(pid):
        log_action(action="refused", result="refused", reason="excluded_target", **base)
        return "refused"
    if approval == "single" and not single_confirm_allowed(state, registry_status,
                                                           is_worker, kind):
        log_action(action="refused", result="refused",
                   reason="live_target_needs_double_confirm", **base)
        return "refused"
    reason = verify_reason(pid, proc_start)
    if reason is not None:
        # The most important refusal in this module: the target is not provably the process
        # that was collected (PID reuse being the case that matters).
        log_action(action="refused", result="refused", reason=reason, **base)
        return "refused"

    sig = signal.SIGKILL if approval == "escalated" else signal.SIGTERM
    action = "sigkill" if sig == signal.SIGKILL else "sigterm"
    try:
        os.kill(int(pid), sig)
    except ProcessLookupError:
        log_action(action=action, result="error", reason="already_gone", **base)
        return "error"
    except OSError as e:
        log_action(action=action, result="error",
                   reason=errno.errorcode.get(e.errno, str(e)), **base)
        return "error"
    log_action(action=action, result="ok", reason=None, **base)
    return "ok"


# ---------------------------------------------------------------------------
# registry close — the ONE documented exception to fleet's no-write invariant
# (prd.md:255). Applies to a killed dispatch JOB row only; a session kill never
# touches the registry.
# ---------------------------------------------------------------------------

def close_registry_row(jobs, attempt_id):
    """Close exactly the user-terminated attempt as a typed cancellation."""

    if not attempt_id or not os.path.isfile(jobs):
        return False
    try:
        result = reconcile_attempt_terminal(
            Path(jobs),
            attempt_id,
            "fleet-kill",
            evidence={
                "failure_class": "cancelled",
                "classifier_source": "fleet-cancel-v1",
                "reconcile_reason": "user-terminated",
            },
        )
    except (DispatchContractError, OSError):
        return False
    # SD-111 P2 trigger 1. Not a new fleet transition authority: this
    # function already performs the terminal `open|running -> done` write
    # above (reconcile_attempt_terminal), and only this module (F-27, a
    # human-confirmed control action, never the read-only collector) may do
    # that. Materializing merely turns intent this call site already stamped
    # into its durable record -- it commits no state fleet did not already
    # commit one line above.
    if result == "closed":
        materialize_after_terminal_close(Path(jobs), attempt_id)
    return result == "closed"
