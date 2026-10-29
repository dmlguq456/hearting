"""Universal backbone — observe live runtime processes and their session/work roles.

A runtime name identifies a candidate process. The shared role judgment consumes
terminal, native PID/start session and registered work ownership before producing
session rows. Unbound services are not conversations. Failed observations keep an
unknown row; later enrichment fills fields without deciding process existence.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

from ..model import Session, etime_to_min, EXEC_WAIT_COMMS

_UTILITIES = str(Path(__file__).resolve().parents[3] / "utilities")
if _UTILITIES not in sys.path:
    sys.path.insert(0, _UTILITIES)
import claude_process_identity  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")
_DELETED = " (deleted)"
_PS_ETIME_RE = re.compile(r"(?:\d+-)?(?:\d+:)?\d+:\d{2}\Z")


class _CensusRows(list):
    """List-compatible observation rows with explicit census evidence."""

    def __init__(self, rows=(), *, complete=False, reasons=()):
        super().__init__(rows)
        self.census_complete = complete is True
        self.census_reasons = tuple(dict.fromkeys(reasons))

    def mark_incomplete(self, reason):
        self.census_complete = False
        self.census_reasons = tuple(dict.fromkeys((*self.census_reasons, reason)))

# Managed (client-server) Codex keeps one state dir per session and carries its socket
# paths in argv: `--listen unix://<dir>/app-server.sock` on the session process that owns
# the rollout fd, and `--remote unix://<dir>/managed-tui.sock` on the visible TUI client.
# <dir> is the only deterministic join key between the two halves (see collectors/codex.py
# prepare_tick). Parsed here because a Session row keeps no argv.
_MANAGED_DIR_RE = re.compile(r"(?:unix://)?(/[^\s]*/managed-sessions/[^/\s]+)/[^/\s]+\.sock(?:\s|$)")


def _managed_dir(args):
    """Managed-codex session state dir from argv, else None (exact match only, no guessing)."""
    match = _MANAGED_DIR_RE.search(args or "")
    return match.group(1) if match else None


# Native CLI command inventories translate argv to modes. The role judgment
# consumes modes/ownership and has no daemon/proxy-name exclusion branches.
_COMMANDS = {
    "codex": set("exec app-server exec-server login logout mcp completion features debug apply resume fork cloud agents remote-control update doctor sandbox queue archive delete migrate-rollouts unarchive help review plugin".split()),
    "opencode": set("agent attach auth bench completion debug export github import models mcp run serve session stats tui upgrade web".split()),
}
_SESSION_COMMANDS = {"codex": {"exec", "review", "resume", "fork"},
                     "opencode": {"run", "attach", "tui"}}
_VALUE_OPTIONS = set("--model -m --effort --config -c --enable --disable --remote --remote-auth-token-env --local-provider --profile -p --sandbox -s --add-dir --ask-for-approval -a --cd -C --image -i --session-id --resume -r --agent --permission-mode --allowedTools --disallowedTools --mcp-config --settings --append-system-prompt --system-prompt --cwd --port --hostname --log-level".split())
_FLAG_OPTIONS = set("--oss --strict-config --approve-for-me --dangerously-bypass-approvals-and-sandbox --yolo --dangerously-bypass-hook-trust --worktree --no-alt-screen --no-daemon --search --help -h --version -V --dangerously-skip-permissions --allow-dangerously-skip-permissions --verbose --debug --continue --no-session-persistence --print-logs".split())


def invocation_mode(harness, argv):
    """Native conversation/worker invocation, another CLI command, or unreadable."""
    if harness == "claude":
        return claude_process_identity.conversation_invocation(argv)
    if not argv or os.path.basename(argv[0]) != harness:
        return "unknown"
    i = 1
    while i < len(argv):
        word = argv[i]
        if word == "--":
            return "session"
        if word in _VALUE_OPTIONS:
            if i + 1 >= len(argv):
                return "unknown"
            i += 2
            continue
        if word in _FLAG_OPTIONS or (word.startswith("--") and "=" in word):
            i += 1
            continue
        if word.startswith("-"):
            return "unknown"  # unknown arity must not turn an option value into a command
        if word in _COMMANDS.get(harness, ()):
            return "session" if word in _SESSION_COMMANDS.get(harness, ()) else "command"
        return "session"  # positional prompt
    return "session"


def _shared_codex_daemon(args):
    """The user-wide App Server service, which is not a session process.

    Per-session managed app servers have a concrete managed-sessions socket and
    remain visible to the existing exact server/client transfer.
    """
    words = (args or "").split()
    if len(words) < 2 or words[1] != "app-server":
        return False
    tail = words[2:]
    return bool(tail and (tail[0] == "daemon" or "--managed-daemon" in tail))


def is_shared_codex_daemon(pid):
    """Read-only live argv check for callers that start from a PID, not ``scan``."""
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as handle:
            args = handle.read(16384).decode("utf-8", "replace").replace("\0", " ")
    except (OSError, TypeError, ValueError):
        return False
    return _shared_codex_daemon(args)


def _user_ps_args(columns):
    """GNU ps argv scoped to Fleet's current effective UID, or None.

    The UID is resolved for every query rather than cached at import time so tests,
    privilege transitions, and long-lived Fleet processes all use the caller's current
    boundary.  Platforms without ``geteuid`` fail closed; Windows session discovery uses
    ``_scan_disk`` and never depends on this helper.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return None
    try:
        uid = str(geteuid())
    except (OSError, TypeError, ValueError):
        return None
    return ["ps", "-u", uid, "-o", columns]


def _read_cwd(pid):
    """(resolved cwd, orphan?) — orphan = /proc/<pid>/cwd symlink ended in ' (deleted)'."""
    try:
        target = os.readlink("/proc/%d/cwd" % pid)
    except OSError:
        return "", False
    if target.endswith(_DELETED):
        return target[: -len(_DELETED)], True
    return target, False


def read_environ(pid):
    """/proc/<pid>/environ → dict[str,str] (or {} on failure). pid: int or str. Read-only, same-user only."""
    try:
        with open("/proc/%s/environ" % pid, "rb") as handle:
            raw = handle.read()
    except OSError:
        return {}
    env = {}
    for kv in raw.split(b"\0"):
        if b"=" in kv:
            k, v = kv.split(b"=", 1)
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def codex_effective_cwd(argv, observed_cwd, launch_cwd=None):
    """Resolve Codex root cwd options from argv tokens, keeping ambiguity observed.

    Only root options before a subcommand or positional prompt are considered. The
    NUL-delimited argv is authoritative; a prompt, shell payload, or subcommand flag
    must never become a process identity.
    """
    if not isinstance(argv, (list, tuple)):
        return observed_cwd
    tokens = list(argv)
    if not tokens or os.path.basename(str(tokens[0])) != "codex":
        return observed_cwd
    target = None
    # Keep this list in step with the installed root-level `codex --help`, plus
    # the hidden `--yolo` alias that help does not list.
    # Values must be consumed as argv tokens: a model/config/image value can
    # itself resemble an option and must never be reinterpreted as one.
    value_options = {
        "--config", "-c", "--enable", "--disable", "--remote",
        "--remote-auth-token-env", "--local-provider", "--model", "-m",
        "--profile", "-p", "--sandbox", "-s", "--add-dir",
        "--ask-for-approval", "-a", "--cd", "-C", "--image", "-i",
    }
    flag_options = {
        "--oss", "--strict-config", "--approve-for-me",
        "--dangerously-bypass-approvals-and-sandbox", "--yolo",
        "--dangerously-bypass-hook-trust", "--worktree", "--no-alt-screen",
        "--no-daemon", "--search", "--help", "-h", "--version", "-V",
    }
    attached_short = ("-c", "-m", "-p", "-s", "-a", "-C", "-i")
    subcommands = {"exec", "app-server", "login", "logout", "mcp", "completion",
                   "features", "debug", "apply", "resume", "fork", "cloud", "agents",
                   "remote-control", "update", "doctor", "sandbox", "queue", "archive",
                   "delete", "migrate-rollouts", "unarchive", "help", "review", "exec-server",
                   "plugin"}
    i = 1
    while i < len(tokens):
        token = str(tokens[i])
        if token == "--":
            break
        if token in subcommands or not token.startswith("-"):
            break
        if token in {"--cd", "-C"}:
            if i + 1 < len(tokens) and str(tokens[i + 1]) != "--":
                target = str(tokens[i + 1])
            break
        if token.startswith("--cd="):
            target = token.partition("=")[2]
            break
        if token in flag_options:
            i += 1
            continue
        if token in value_options:
            if (i + 1 >= len(tokens) or str(tokens[i + 1]) == "--"
                    or str(tokens[i + 1]).startswith("-")):
                break
            if token in {"--image", "-i"}:
                # --image accepts one or more files. The parser's next option
                # begins a new argument; bare tokens remain image values.
                i += 1
                while i < len(tokens) and not str(tokens[i]).startswith("-"):
                    i += 1
                continue
            i += 2
            continue
        if token.startswith("--") and "=" in token:
            option = token.partition("=")[0]
            if option in value_options:
                if option in {"--cd", "-C"}:
                    target = token.partition("=")[2]
                    break
                i += 1
                continue
        attached = next((short for short in attached_short
                         if token.startswith(short) and len(token) > len(short)), None)
        if attached:
            if attached == "-C":
                target = token[len(attached):]
                break
            i += 1
            continue
        # An unknown option's arity is unknown. Stop rather than mistaking a
        # following option-looking value for Codex's workspace root.
        break
    if not target or "\x00" in target:
        return observed_cwd
    path = Path(target).expanduser()
    if not path.is_absolute():
        if not launch_cwd or not Path(launch_cwd).is_absolute():
            return observed_cwd
        # `launch_cwd` is supplied from initial /proc/<pid>/environ PWD, not
        # reconstructed from current cwd. Accept it only when proc cwd agrees
        # with the launch base (not moved yet) or the resolved argv target
        # (already moved by Codex); otherwise PWD and proc evidence conflict.
        try:
            launch_base = Path(launch_cwd).resolve(strict=False)
            effective = (launch_base / path).resolve(strict=False)
            observed = Path(observed_cwd).resolve(strict=False) if observed_cwd else None
            if observed not in {launch_base, effective}:
                return observed_cwd
            return str(effective)
        except (OSError, RuntimeError, ValueError):
            return observed_cwd
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return observed_cwd


def _read_argv(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as handle:
            return [part.decode("utf-8", "replace") for part in handle.read().split(b"\0") if part]
    except (OSError, TypeError, ValueError):
        return []


def read_proc_start(pid):
    """Live /proc/<pid>/stat field 22 (starttime) as a str, else None.

    This is the PID-reuse guard: pid alone is not an identity, (pid, start-time) is. The
    claude registry stores the same value as `procStart`, so the two compare directly.
    Field 22 is counted from field 1, and comm (field 2) is parenthesized and may itself
    contain spaces/parens — so split AFTER the last ')' and index from there.
    Zombie processes are already terminal and therefore return None, matching the
    authoritative governor's process identity rule. Windows / unreadable /proc →
    None (tolerate: callers treat None as "no evidence").
    """
    try:
        with open("/proc/%s/stat" % pid) as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    try:
        rest = data[data.rindex(")") + 1:].split()
        if rest[0] == "Z":
            return None
        return rest[19]           # field 22 = index 19 after (pid, comm) are consumed
    except (ValueError, IndexError):
        return None


def is_terminal_state(pid):
    """True when /proc/<pid>/stat says the process is a zombie (`Z`) or dead (`X`).

    An unreadable stat is no evidence either way: it returns False so a live process is never
    hidden on missing evidence. The one small state test the session loop and `read_proc_start`
    share the rule of.
    """
    try:
        with open("/proc/%s/stat" % pid) as f:
            data = f.read()
        return data[data.rindex(")") + 1:].split()[0] in ("Z", "X")
    except (OSError, ValueError, IndexError):
        return False


_PROVENANCE_COMMS = (
    ("herdr", "herdr"),
    ("tmux", "terminal"), ("sshd", "terminal"), ("login", "terminal"),
    ("bash", "terminal"), ("zsh", "terminal"),
)


def _ppid_of(pid):
    try:
        with open("/proc/%s/stat" % pid) as f:
            data = f.read()
        return int(data[data.rindex(")") + 1:].split()[1])   # field 4 = ppid
    except (OSError, ValueError, IndexError):
        return None


def _comm_of(pid):
    try:
        with open("/proc/%s/comm" % pid) as f:
            return f.read().strip()
    except OSError:
        return None


# The kernel-fixed inode of the initial PID namespace. A caller in any other one (a
# container, a sandbox) sees only part of the process table, so an absence it observes
# proves nothing.
_INITIAL_PID_NAMESPACE = "pid:[4026531836]"


def initial_pid_namespace():
    """True when this process runs in the initial PID namespace; unreadable is False."""
    try:
        return os.readlink("/proc/self/ns/pid") == _INITIAL_PID_NAMESPACE
    except OSError:
        return False


def boot_time():
    """Kernel boot time (epoch seconds) from /proc/stat, or None."""
    try:
        with open("/proc/stat", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def provenance(pid, max_depth=6):
    """Best-effort launcher lineage: 'herdr' | 'terminal' | 'vscode' | 'worker' | None.

    Walks up to `max_depth` ancestors matching /proc/<ppid>/comm. Misattribution is worse
    than absence (PRD F-26): anything unrecognized returns None and renders no tag at all.
    """
    env = read_environ(pid)
    if env.get("AGENT_SESSION_ROLE", "").lower() == "worker":
        return "worker"
    # F-100b: herdr starts the harness THROUGH the user's shell (measured 2026-09-03:
    # claude -> zsh -> herdr), so stopping at the first match tagged every herdr pane
    # "terminal". The walk now runs the whole chain: herdr anywhere above wins; otherwise
    # the nearest terminal/vscode match is what it always was.
    cur, seen = _ppid_of(pid), set()
    nearest = None
    for _ in range(max_depth):
        if not cur or cur in seen or cur <= 1:
            break
        seen.add(cur)
        comm = (_comm_of(cur) or "").lower()
        if "herdr" in comm:
            return "herdr"
        if nearest is None:
            for needle, tag in _PROVENANCE_COMMS:
                if needle in comm:
                    nearest = tag
                    break
            if nearest is None and comm in ("code", "node"):
                # vscode-server only — a bare node parent is not evidence of an editor.
                args = " ".join(read_environ(cur).get("_", "").split())
                if "vscode-server" in args or "vscode" in args:
                    nearest = "vscode"
        cur = _ppid_of(cur)
    return nearest


def _ps_lines():
    # COLUMNS pinned huge: Claude Code injects terminal width into the statusline env and
    # ps truncates args= to COLUMNS, which would break argv matching downstream
    # (observed in statusline.sh:108). Harmless for procscan's comm match but kept for the shared
    # dispatch scan that reuses the same ps invocation contract.
    env = dict(os.environ, COLUMNS="100000")
    argv = _user_ps_args("pid=,comm=,etime=,args=")
    if argv is None:
        return _CensusRows(complete=False, reasons=("effective-uid-unavailable",))
    try:
        result = subprocess.run(
            argv,
            capture_output=True, text=True, timeout=5, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        output = getattr(exc, "stdout", None)
        if output is None:
            output = getattr(exc, "output", None)
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        lines = output.splitlines() if isinstance(output, str) else ()
        return _CensusRows(lines, complete=False, reasons=("ps-timeout",))
    except Exception as exc:
        return _CensusRows(complete=False,
                           reasons=("ps-error-" + type(exc).__name__,))

    output = getattr(result, "stdout", None)
    if isinstance(output, str):
        lines = output.splitlines()
        reasons = []
    elif isinstance(output, bytes):
        lines = output.decode("utf-8", "replace").splitlines()
        reasons = ["ps-output-not-text"]
    else:
        lines = []
        reasons = ["ps-output-unavailable"]

    returncode = getattr(result, "returncode", None)
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        reasons.append("ps-returncode-unavailable")
    elif returncode != 0:
        reasons.append("ps-nonzero-exit")
    return _CensusRows(lines, complete=not reasons, reasons=reasons)


# --- F-47 exec visibility (v30) --------------------------------------------------------
# A session that left `python train.py` running and is waiting on it is NOT idle, and the
# only owned evidence for that is its own process subtree (prd.md:263 — no guessing, no
# daemonized/reparented processes). Age is the primary defense against harness-internal
# noise: every statusline / title-refresh / mem-worker helper finishes in seconds, so a
# ≥60s child is already almost certainly real work. `SESSION_WORK_SEC` (model.py:477) is
# the same 60s constant the classifier uses — one threshold, one place.
EXEC_MIN_AGE_SEC = 60
# Second defense, and the general form of "exclude harness-internal helpers": a child that
# started WITH its session was launched by the runtime, not by a tool call. Observed
# 2026-07-30 — an idle Codex row carried `node /tmp/steward-mcp-bridge.cjs` aged 237s inside a
# 238s session, i.e. a stdio MCP server that lives for the whole session and would have
# badged every row forever. A real exec cannot begin until a user turn has been prompted and
# answered, so a same-cohort child is plumbing by construction. Preferred over a name list
# (F-26: name lists rot and each new helper needs a code change).
EXEC_BOOT_GRACE_SEC = 10
# `zsh -c '<snapshot> && <real command>'` is how Claude's Bash tool runs everything, so the
# direct child is almost never the interesting name — descend to the real work (prd.md:263).
# It is still the TOOL CALL, though: its age is how long the session has been on this one
# call, which is the number `exec_child` reports as `etime_s` (F-47, 2026-09-16).
_EXEC_WRAPPER_COMMS = ("sh", "bash", "zsh", "dash", "ksh", "fish")
# Harness-internal helpers that could in principle outlive 60s. Deliberately short: the
# canonical identification is the env marker below (same keys as `mem_worker` in scan()),
# these are only the comm-visible shapes that carry no env of their own.
_EXEC_HELPER_COMMS = ("statusline", "ps", "tmux", "ss")
_EXEC_MAX_DEPTH = 12  # sandbox → bwrap → sandbox → shell → lease → workload
_EXEC_SANDBOX_COMMS = ("codex-linux-sandbox", "codex-linux-san", "bwrap")


def _exec_identity(pid):
    """One local PID/start/parent observation; unreadable is not absence proof."""
    try:
        with open("/proc/%s/stat" % pid) as handle:
            raw = handle.read()
        fields = raw[raw.rindex(")") + 1:].split()
        if fields[0] in ("Z", "X"):
            return None
        return (int(fields[1]), fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _exec_is_wrapper(pid, comm):
    if comm in _EXEC_WRAPPER_COMMS or comm in _EXEC_SANDBOX_COMMS:
        return True
    if comm in ("python", "python3"):
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as handle:
                args = handle.read(16384).decode(errors="replace").split("\0")
            return any(os.path.basename(arg) == "verification-background-lease.py"
                       for arg in args[1:3])
        except OSError:
            pass
    return False


def _exec_is_helper(pid, comm):
    """True for a child that is harness plumbing, not the session's work.

    A nested harness process is excluded because it already owns a row of its own (a
    session or an F-29 sub-agent) — surfacing it again as its parent's `⚙` detail would
    double-count the same work.
    """
    base = (comm or "").strip()
    # Execution containers carry tool descendants; unlike model/MCP companions,
    # they are transparent and never themselves returned as work evidence.
    if base in _EXEC_SANDBOX_COMMS:
        env = read_environ(pid)
        return env.get("MEM_DISTILL") == "1" or env.get("FLEET_TITLE_REFRESH") == "1"
    # A harness process, or one of a harness's own hyphenated companion binaries (observed
    # 2026-07-30: every idle Codex row carried a long-lived `codex-code-mode` child, which is
    # runtime plumbing, not the user's work). Nested harnesses already own their own rows.
    if base in HARNESSES or any(base.startswith(h + "-") for h in HARNESSES):
        return True
    if base == "git" and _opencode_snapshot_maintenance(pid):
        return True
    low = base.lower()
    if any(needle in low for needle in _EXEC_HELPER_COMMS):
        return True
    env = read_environ(pid)
    return env.get("MEM_DISTILL") == "1" or env.get("FLEET_TITLE_REFRESH") == "1"


def _opencode_snapshot_maintenance(pid):
    """Git in the runtime's private snapshot repository is not user work."""
    parent = _ppid_of(pid)
    if not parent or _comm_of(parent) != "opencode":
        return False
    env = read_environ(parent)
    data = env.get("XDG_DATA_HOME") or os.path.join(env.get("HOME") or "", ".local", "share")
    if not os.path.isabs(data):
        return False
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            return False
        args = raw.decode(errors="replace").split("\0")
        paths = []
        for i, arg in enumerate(args):
            if arg == "--git-dir" and i + 1 < len(args):
                paths.append(args[i + 1])
            elif arg.startswith("--git-dir="):
                paths.append(arg.split("=", 1)[1])
        root = os.path.realpath(os.path.join(data, "opencode", "snapshot"))
        return len(paths) == 1 and os.path.commonpath(
            [root, os.path.realpath(paths[0])]) == root and os.path.realpath(paths[0]) != root
    except (OSError, ValueError):
        return False


def proc_tree():
    """{pid: (ppid, etime_sec, comm)} from one cheap `ps`.

    A separate invocation on purpose: `_ps_lines`' four-column format is a shared contract
    (collectors/dispatch.py, and every test that mocks it), so the parent/child columns get
    their own call — the same reason `_pid_ttys` is separate. comm is placed LAST because it
    can contain spaces; the numeric columns stay unambiguously splittable.
    """
    argv = _user_ps_args("pid=,ppid=,etimes=,comm=")
    if argv is None:
        return {}
    try:
        out = subprocess.run(argv,
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    tree = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            tree[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3].strip())
        except ValueError:
            continue
    return tree


def children_index(tree):
    """{ppid: [pid, ...]} — the reverse edge of `proc_tree`, built once per tick."""
    kids = {}
    for pid, entry in tree.items():
        kids.setdefault(entry[0], []).append(pid)
    for pids in kids.values():
        pids.sort()
    return kids


def _oldest_work_child(pid, tree, kids, min_age=0, max_age=None, attempt_id=None,
                       bind_attempt=False):
    """(cpid, etime, comm) of the longest-running non-helper child, or None.

    Longest-running rather than newest: the thing the session is *waiting on* is the thing
    that has been alive longest, and it makes the pick deterministic across ticks (pid
    breaks exact-age ties). `max_age` applies the boot-cohort cut (see EXEC_BOOT_GRACE_SEC).
    """
    best = None
    for cpid in kids.get(pid, ()):
        entry = tree.get(cpid)
        if entry is None:
            continue
        _ppid, etime, comm = entry
        if etime < min_age or (max_age is not None and etime > max_age):
            continue
        if _exec_is_helper(cpid, comm):
            continue
        if bind_attempt and (read_environ(cpid).get("AGENT_DISPATCH_ATTEMPT_ID") or None) != attempt_id:
            continue
        if best is None or (etime, -cpid) > (best[1], -best[0]):
            best = (cpid, etime, comm)
    return best


def exec_child(pid, tree=None, kids=None, min_age=EXEC_MIN_AGE_SEC,
               max_depth=_EXEC_MAX_DEPTH, expected_start=None, attempt_id=None):
    """What `pid`'s long-lived tool call is doing → evidence dict, else None.

    Shape: {'pid','comm','kind','etime_s','leaf_etime_s','child_pid'} + ownership fields.

    The ≥`min_age` gate applies to the session's DIRECT child, and for a Claude session that
    child IS the tool call (`zsh -c '<snapshot> && <command>'`), so **`etime_s` is the direct
    child's age**: how long the session has been on this one call. `pid`/`comm` name the
    process reached by descending — what the call is doing at this instant — and
    `leaf_etime_s` keeps that process's own age. Measured 2026-09-16: a `sleep 20` poll loop
    resets its leaf every 20s, so leaf elapsed can never show a 9-minute wait; prd.md:263's
    "경과 = 그 프로세스 etime" is corrected to the call's elapsed (the comm half stands).

    `kind` is `work` when a real workload runs beneath the call, `wait` when nothing does.
    Wrappers AND wait/guard primitives are transparent while something runs under them —
    `flock … timeout … node …` is node's work, not a wait (measured 2026-09-16: 121/121
    samples read as `flock` while python3 computed three levels below it) — and each becomes
    the wait leaf when nothing does: a bare `flock` really is blocked on its lock, and a bare
    `zsh` is a Bash call between commands. Reporting that bare wrapper instead of nothing is
    what keeps the badge alive every tick rather than blinking out in a loop's gaps.

    Children older than `session age - EXEC_BOOT_GRACE_SEC` are dropped as boot-cohort
    plumbing. An unknown session age (pid absent from `tree`) disables that cut rather than
    hiding the evidence — absence of a fact is never used as a fact.
    """
    root_identity = _exec_identity(pid) if expected_start is not None else None
    if expected_start is not None and (
            root_identity is None or root_identity[1] != str(expected_start)):
        return None
    if tree is None:
        tree = proc_tree()
    if kids is None:
        kids = children_index(tree)
    entry = tree.get(pid)
    max_age = None
    if entry is not None and entry[1] > EXEC_BOOT_GRACE_SEC:
        max_age = entry[1] - EXEC_BOOT_GRACE_SEC
    picked = _oldest_work_child(pid, tree, kids, min_age=min_age, max_age=max_age,
                                attempt_id=attempt_id, bind_attempt=expected_start is not None)
    if picked is None:
        return None
    call_pid, call_etime, _call_comm = picked
    cur_pid, cur_etime, cur_comm = picked
    kind = "work"
    path = [pid]
    identities = {pid: root_identity} if root_identity is not None else {}
    for _ in range(max(0, max_depth)):
        if cur_pid in path:
            return None
        if expected_start is not None:
            identity = _exec_identity(cur_pid)
            if identity is None or identity[0] != path[-1]:
                return None
            identities[cur_pid] = identity
        path.append(cur_pid)
        if not (_exec_is_wrapper(cur_pid, cur_comm) or cur_comm in EXEC_WAIT_COMMS):
            break
        nxt = _oldest_work_child(cur_pid, tree, kids, attempt_id=attempt_id,
                                 bind_attempt=expected_start is not None)
        if nxt is None:
            if cur_comm in _EXEC_WRAPPER_COMMS or cur_comm in EXEC_WAIT_COMMS:
                kind = "wait"
                break
            # A sandbox or lease wrapper with nothing under it is plumbing mid-setup, not a
            # tool call: naming it would badge every row for the runtime's own scaffolding.
            return None
        cur_pid, cur_etime, cur_comm = nxt
    else:
        return None
    result = {"pid": cur_pid, "comm": cur_comm, "etime_s": call_etime,
              "leaf_etime_s": cur_etime, "kind": kind, "child_pid": call_pid}
    if expected_start is not None:
        # Recheck every edge after selection, so PID reuse or reparenting during
        # the snapshot cannot turn a different process into owned execution.
        if any(_exec_identity(p) != identities[p] for p in path):
            return None
        result.update(ownership_verified=True, root_pid=pid,
                      root_start=str(expected_start), proc_start=identities[cur_pid][1],
                      attempt_id=attempt_id,
                      ancestry=[{"pid": p, "ppid": identities[p][0],
                                 "start": identities[p][1]} for p in path])
    return result


def exec_children(pid, tree=None, kids=None, min_age=0,
                  max_depth=_EXEC_MAX_DEPTH, expected_start=None, attempt_id=None,
                  limit=64):
    """Enumerate bounded, independently identity-checked work branches.

    This additive observer shares F-47's helper filtering and transparent
    wrapper descent. Unlike ``exec_child`` it does not choose one longest
    sibling. ``expected_start`` pins the root generation; every parent edge is
    rechecked before a result is returned. Attempt tags are an additional
    filter only when an exact attempt was supplied.
    """
    if tree is None:
        tree = proc_tree()
    if kids is None:
        kids = children_index(tree)
    root_identity = _exec_identity(pid) if expected_start is not None else None
    if expected_start is not None and (
            root_identity is None or root_identity[1] != str(expected_start)):
        return []
    root_entry = tree.get(pid)
    max_age = None
    if root_entry is not None and root_entry[1] > EXEC_BOOT_GRACE_SEC:
        max_age = root_entry[1] - EXEC_BOOT_GRACE_SEC

    def eligible(child, age_floor=0, cohort_max=None):
        entry = tree.get(child)
        if entry is None:
            return None
        _ppid, age, comm = entry
        if age < age_floor or (cohort_max is not None and age > cohort_max):
            return None
        if _exec_is_helper(child, comm):
            return None
        if attempt_id is not None and (
                (read_environ(child).get("AGENT_DISPATCH_ATTEMPT_ID") or None) != attempt_id):
            return None
        return child, age, comm

    results, seen = [], set()
    direct = [item for child in kids.get(pid, ())
              if (item := eligible(child, min_age, max_age)) is not None]
    direct.sort(key=lambda item: (item[1], -item[0]), reverse=True)
    for call_pid, call_age, call_comm in direct:
        stack = [(call_pid, call_age, call_comm, [pid, call_pid])]
        while stack and len(results) < max(1, min(int(limit), 128)):
            cur_pid, cur_age, cur_comm, path = stack.pop()
            if len(path) > max_depth + 2 or len(set(path)) != len(path):
                continue
            transparent = _exec_is_wrapper(cur_pid, cur_comm) or cur_comm in EXEC_WAIT_COMMS
            descendants = []
            if transparent:
                for child in kids.get(cur_pid, ()):
                    item = eligible(child)
                    if item is not None:
                        descendants.append(item)
                descendants.sort(key=lambda item: (item[1], -item[0]), reverse=True)
            if descendants:
                for child, age, comm in reversed(descendants):
                    stack.append((child, age, comm, path + [child]))
                continue
            if transparent and cur_comm not in _EXEC_WRAPPER_COMMS and cur_comm not in EXEC_WAIT_COMMS:
                continue
            if cur_pid in seen:
                continue
            kind = "wait" if transparent else "work"
            identities = {}
            if expected_start is not None:
                valid = True
                for node in path:
                    identity = _exec_identity(node)
                    if identity is None or (node != pid and identity[0] != path[path.index(node) - 1]):
                        valid = False
                        break
                    identities[node] = identity
                if not valid:
                    continue
                if any(_exec_identity(node) != identities[node] for node in path):
                    continue
            evidence = {"pid": cur_pid, "comm": cur_comm,
                        "etime_s": call_age, "leaf_etime_s": cur_age,
                        "kind": kind, "child_pid": call_pid}
            if expected_start is not None:
                evidence.update(ownership_verified=True, root_pid=pid,
                               root_start=str(expected_start),
                               proc_start=identities[cur_pid][1],
                               attempt_id=attempt_id,
                               ancestry=[{"pid": node, "ppid": identities[node][0],
                                          "start": identities[node][1]} for node in path])
            results.append(evidence)
            seen.add(cur_pid)
    return results


def _pid_ttys():
    """{pid: 'pts/N'} controlling tty per process (separate cheap ps so the main _ps_lines
    contract, shared with the dispatch scan, stays untouched). '?' (no tty) is kept as-is."""
    argv = _user_ps_args("pid=,tty=")
    if argv is None:
        return {}
    try:
        out = subprocess.run(argv,
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return {}
    m = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 2:
            try:
                m[int(p[0])] = p[1]
            except ValueError:
                pass
    return m


def _detached_ttys():
    """Set of ttys ('pts/N') whose tmux session has NO client attached. session_attached is
    per-session, so a session viewed in any window counts as attached. Empty if tmux absent."""
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_tty}\t#{session_attached}"],
            capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return set()
    det = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].strip() == "0":
            det.add(parts[0].replace("/dev/", "", 1))
    return det


def _orca_dead_socks(socks):
    """Subset of Orca relay socket paths that currently have NO established client.

    Sessions spawned through the Orca remote relay (`relay.js --detached`) carry
    ORCA_RELAY_SOCKET_PATH in their environ. The relay keeps panes alive after the
    remote client disconnects, so a closed remote tab leaves a live harness process
    on a pty no terminal owns (observed 2026-07-15: a 33h-old claude and codex pair
    rendered as active idle sessions). A relay socket with zero connected peers in
    `ss -x` means no client is attached to ANY of its panes → those sessions are
    detached (reconnectable — same vocabulary as a detached tmux session). Per-tab
    liveness is invisible from outside the relay, so a connected client marks all
    of its relay's panes attached. Fail open: if `ss` is missing or errors, nothing
    is marked."""
    socks = {s for s in socks if s}
    if not socks:
        return set()
    try:
        out = subprocess.run(["ss", "-x"], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return set()
    return {s for s in socks if s not in out}


def _is_detached(tty, app_server, det_ttys):
    """A session no one is attached to — NOT tmux-specific (user 2026-07-02: sessions can just be
    run directly). Two general signals (a third, Orca relay panes with no connected
    client, is applied post-loop in scan() via _orca_dead_socks):
      · no controlling terminal (tty '?'/'-') → run in the background / detached from any terminal.
        (app-server companions ALSO have no tty but are services, not sessions → excluded.)
      · a tmux pane whose session has 0 attached clients → detached tmux session.
    A plain foreground terminal session keeps its pts and isn't in the tmux-detached set → attached."""
    if app_server:
        return False
    if not tty or tty in ("?", "-"):
        return True
    return tty in det_ttys


def _pid_alive(pid):
    """True if pid is a live process. Windows has no /proc: probe via OpenProcess."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _scan_disk(harness_filter=None):
    """Windows / no-procfs backbone: derive sessions from on-disk state instead of
    `ps -eo comm` + /proc (neither exists under MSYS). Claude writes one
    ~/.claude/sessions/<pid>.json per live session (sessionId, pid, cwd, status);
    presence + a live pid = an existing session. Per-harness collectors enrich as usual.
    Codex/opencode still surface via their own collectors; only claude has this native
    session-state file, so it is the one reconstructed here."""
    import glob
    import json
    from . import claude as _claude   # lazy: avoid procscan<->claude import cycle
    sessions = []
    if harness_filter and "claude" not in harness_filter:
        return sessions
    try:
        home = _claude._home()
    except Exception:
        return sessions
    for path in sorted(glob.glob(os.path.join(home, "sessions", "*.json"))):
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception:
            d = {}
        pid = d.get("pid")
        if not isinstance(pid, int):
            try:
                pid = int(os.path.splitext(os.path.basename(path))[0])
            except ValueError:
                continue
        if not _pid_alive(pid):
            continue
        cwd = (d.get("cwd") or "").replace("\\", "/")
        # NT has no /proc/<pid>/environ, so mem_worker safely remains false.
        sessions.append(Session(
            harness="claude",
            pid=pid,
            cwd=cwd,
            orphan=False,
            app_server=False,
            is_child=False,
            detached=False,
            elapsed_min=0,
            slug=os.path.basename(cwd.rstrip("/")) if cwd else None,
        ))
    return sessions


def scan(harness_filter=None):
    """Return [Session] for every live harness leaf process.

    harness_filter: optional iterable of harness names to keep (e.g. {'claude','codex'}).
    """
    if os.name == "nt":
        # Windows: MSYS `ps` has no -eo/comm and there is no /proc — reconstruct
        # session existence from on-disk state (see _scan_disk).
        return _CensusRows(_scan_disk(harness_filter), complete=False,
                           reasons=("disk-state-is-not-process-census",))
    raw_lines = _ps_lines()
    sessions = _CensusRows(
        complete=getattr(raw_lines, "census_complete", False) is True,
        reasons=getattr(raw_lines, "census_reasons", ("process-census-unverified",)),
    )
    try:
        selected_harnesses = tuple(harness_filter) if harness_filter else ()
    except TypeError:
        selected_harnesses = ()
        sessions.mark_incomplete("harness-filter-unreadable")
    if selected_harnesses and not set(HARNESSES).issubset(
            {name for name in selected_harnesses if isinstance(name, str)}):
        sessions.mark_incomplete("harness-filter-partial")
    orca_panes = []                      # (Session, ORCA_RELAY_SOCKET_PATH) rows
    pid_tty = _pid_ttys()
    det_ttys = _detached_ttys()
    tree = proc_tree()                   # F-47: one ps for the whole parent/child map
    kids = children_index(tree)
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 3)          # pid, comm, etime, args
        if len(parts) < 3:
            sessions.mark_incomplete("process-row-malformed")
            continue
        pid_s, comm, etime = parts[0], parts[1], parts[2]
        args = parts[3] if len(parts) > 3 else ""
        try:
            pid = int(pid_s)
        except ValueError:
            sessions.mark_incomplete("process-row-pid-invalid")
            continue
        if pid <= 0:
            sessions.mark_incomplete("process-row-pid-invalid")
            continue
        if not _PS_ETIME_RE.fullmatch(etime):
            sessions.mark_incomplete("process-row-malformed")
        harness = comm
        claude_observation = None
        if comm == "claude" or comm not in HARNESSES:
            # Flat ps text is only a discovery hint. A version-shaped comm or
            # a prompt mentioning Claude never proves a native process.
            candidate = (comm == "claude" or comm in {"node", "nodejs", "bun"}
                         or "/claude/versions/" in args
                         or os.path.basename(args.split(" ", 1)[0]) in {"claude", "claude-code"})
            if not candidate or selected_harnesses and "claude" not in selected_harnesses:
                continue
            claude_observation = claude_process_identity.observe_process(pid)
            if claude_observation and claude_observation["kind"] in {"service", "helper"}:
                continue
            if comm != "claude" and (not claude_observation or claude_observation["kind"] != "runtime"):
                continue
            harness = "claude"
        # Filter the proven harness, not a native runtime's version-shaped comm.
        if selected_harnesses and harness not in selected_harnesses:
            continue
        # A defunct process (the shared Codex daemon never reaps the app-server children it
        # replaced) has no cwd, environ or session: not a row.
        if is_terminal_state(pid):
            continue
        cwd, orphan = _read_cwd(pid)
        if harness == "codex":
            proc_argv = _read_argv(pid)
            initial_env = read_environ(pid)
            initial_launch_pwd = initial_env.get("PWD")
            cwd = codex_effective_cwd(proc_argv, cwd, initial_launch_pwd)
        # app-server companion marker: codex-only, literal "app-server" token in args.
        # Interactive `codex`/`codex exec` never carries this token, so the gate cannot
        # false-positive on interactive sessions. COLUMNS is pinned to 100000 for the ps
        # call (see _ps_lines), so args are never truncated and the token stays visible
        # even for long command lines.
        app_server = harness == "codex" and "app-server" in args
        # Cross-runtime headless child marker (SD-24). Unreadable environ fails open:
        # a process is never hidden merely from argv/PPID/cwd resemblance.
        env = read_environ(pid)                       # Read once and reuse.
        is_child = env.get("AGENT_SESSION_ROLE", "").lower() == "worker" or bool(env.get("AGENT_DISPATCH_DEPTH")) or env.get("AGENT_DISPATCH_CHILD") == "1" or (
            harness == "claude" and env.get("CLAUDE_CODE_CHILD_SESSION") == "1"
        )
        # Tag memory workers and title refreshers to prevent inherited cwd/env misattribution.
        mem_worker = env.get("MEM_DISTILL") == "1" or env.get("FLEET_TITLE_REFRESH") == "1"
        detached = _is_detached(pid_tty.get(pid), app_server, det_ttys)
        proc_start = read_proc_start(pid)
        if claude_observation and (proc_start != claude_observation["proc_start"]
                                  or claude_process_identity.observe_process(pid) != claude_observation):
            continue
        managed_dir = _managed_dir(args) if harness == "codex" else None
        sess = Session(
            harness=harness,
            pid=pid,
            cwd=cwd,
            orphan=orphan,
            app_server=app_server,
            managed_dir=managed_dir,
            is_child=is_child,
            detached=detached,
            elapsed_min=etime_to_min(etime),
            slug=os.path.basename(cwd.rstrip("/")) if cwd else None,
            mem_worker=mem_worker,
            proc_start=proc_start,      # tier-2 identity half — see read_proc_start
            route_file=env.get("AGENT_ROUTE_FILE") or None,
            route_id=env.get("AGENT_ROUTE_ID") or None,
            route_node=env.get("AGENT_ROUTE_NODE") or None,
            attempt_id=env.get("AGENT_DISPATCH_ATTEMPT_ID") or None,
            assigned_contract=env.get("AGENT_DISPATCH_ASSIGNED_CONTRACT") or None,
            unit=env.get("AGENT_DISPATCH_UNIT") or None,
            worker_type=env.get("AGENT_DISPATCH_WORKER_TYPE") or None,
            owner=env.get("AGENT_DISPATCH_OWNER") or None,
            model_role=env.get("AGENT_DISPATCH_MODEL_ROLE") or None,
            exec_child=(exec_child(pid, tree, kids, expected_start=proc_start,
                                   attempt_id=env.get("AGENT_DISPATCH_ATTEMPT_ID") or None)
                        if proc_start is not None else None),
        )
        # A registered owner has a route binding, but no stage node. Preserve
        # the wrapper's complete/partial tuple for the existing fail-closed
        # owner resolver, including when its runtime session id is unobserved.
        # These observation-only attributes do not extend Session's JSON shape.
        for field in ("owner_route_file", "owner_route_id", "owner_route_hash"):
            value = env.get("AGENT_" + field.upper())
            if value:
                setattr(sess, field, value)
        sess._dispatch_depth = env.get("AGENT_DISPATCH_DEPTH")
        # Shared process snapshot for targeted output-writer correlation. These
        # private references are excluded from public dataclass serialization.
        sess._proc_tree = tree
        sess._proc_kids = kids
        sess._tty = pid_tty.get(pid)
        sess._invocation_mode = (claude_observation["invocation"] if claude_observation
                                 else invocation_mode(harness, _read_argv(pid)))
        sessions.append(sess)
        orca_sock = env.get("ORCA_RELAY_SOCKET_PATH")
        if orca_sock:
            orca_panes.append((sess, orca_sock))
    # Third detached signal: Orca relay panes whose relay has no connected client
    # (one ss probe per scan tick, mirroring the single tmux probe above).
    dead_socks = _orca_dead_socks({s for _, s in orca_panes})
    for sess, sock in orca_panes:
        if sock in dead_socks:
            sess.detached = True
    return sessions
