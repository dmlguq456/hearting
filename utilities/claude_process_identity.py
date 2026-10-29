"""Lightweight exact PID/start Claude session claims.

This module deliberately has no Fleet model, pane collector, transcript, or herdr
dependencies so bounded process observers can share the native identity evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import time


def tap_claim_index(home: str | os.PathLike, *, strict: bool = False) -> dict[tuple[str, str], set[str]]:
    """Index statusline claims by their recorded PID and process start ticks."""
    directory = Path(home) / ".statusline"
    if strict:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("statusline tap directory is not an owner directory")
    try:
        names = os.listdir(directory)
    except OSError:
        if strict:
            raise
        return {}
    result: dict[tuple[str, str], set[str]] = {}
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        path = directory / name
        try:
            if strict:
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_size > 65536):
                    raise OSError("statusline tap is not a bounded owner file")
            with path.open(encoding="utf-8") as handle:
                row = json.load(handle)
        except (OSError, ValueError):
            if strict:
                raise OSError("statusline tap is unreadable")
            continue
        if not isinstance(row, dict):
            if strict:
                raise OSError("statusline tap has an invalid shape")
            continue
        if row.get("pid") is None:
            continue
        pid, start = str(row["pid"]), str(row.get("proc_start") or "")
        if not start:
            continue
        sid = row.get("session_id") or name[:-5]
        if isinstance(sid, str) and sid:
            result.setdefault((pid, start), set()).add(sid)
    return result


def _proc_start(stat_text: str) -> str:
    close = stat_text.rfind(")")
    fields = stat_text[close + 2:].split()
    return fields[19] if close >= 0 and len(fields) > 19 else ""


_EMBEDDED_HELPERS = {"rg", "ugrep", "bfs"}
_INITIAL_PID_NAMESPACE = "pid:[4026531836]"


def _namespace_pid(proc: Path, pid: str) -> str | None:
    """Return the innermost pid of a nested-namespace process, or None if not exact."""
    try:
        lines = (proc / "status").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("NSpid:"):
            continue
        values = line.split()[1:]
        if (len(values) < 2 or not all(item.isdecimal() for item in values)
                or values[0] != pid):
            return None
        return values[-1]
    return None


def _native_claude_executable(executable: str) -> bool:
    return (os.path.basename(executable).lower() in {"claude", "claude-code"}
            or "/claude/versions/" in executable.lower())


def _claude_shaped_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    argv0 = os.path.basename(argv[0]).lower()
    return (argv0 in {"claude", "claude-code", "claude bg-spare", "claude bg-pty-host"}
            or "/claude/versions/" in argv[0].lower()
            or any("@anthropic-ai/claude-code" in item or item.endswith("/claude")
                   for item in argv)
            or (argv0 in {"node", "nodejs", "bun"} and len(argv) > 1
                and ("claude-code" in argv[1] or "@anthropic-ai/claude-code" in argv[1])))


def _runtime_identity(proc: Path):
    """Read process ownership and argv; disappeared processes are not candidates."""
    try:
        process_uid = proc.stat().st_uid
        stat_text = (proc / "stat").read_text(encoding="ascii")
        start = _proc_start(stat_text)
    except FileNotFoundError:
        return None
    try:
        argv = [item.decode("utf-8", "replace")
                for item in (proc / "cmdline").read_bytes().split(b"\0") if item]
    except FileNotFoundError:
        return None
    try:
        executable = os.readlink(proc / "exe")
    except FileNotFoundError:
        return None
    except OSError:
        executable = None
    return process_uid, start, argv, executable


def _argument_sessions(argv: list[str]) -> set[str]:
    values = set()
    value_flags = {"--resume", "--session", "--session-id", "--resume-session", "-r"}
    for index, token in enumerate(argv):
        if token in value_flags and index + 1 < len(argv):
            value = argv[index + 1]
            if value and not value.startswith("-"):
                values.add(value)
        elif any(token.startswith(flag + "=") for flag in value_flags if flag.startswith("--")):
            value = token.split("=", 1)[1]
            if value:
                values.add(value)
    return values


_CLAUDE_COMMANDS = {"agents", "auth", "doctor", "install", "mcp", "plugin",
                    "setup-token", "update", "upgrade"}
_CLAUDE_VALUE_OPTIONS = set("--model -m --effort --config -c --enable --disable --remote "
                            "--remote-auth-token-env --local-provider --profile -p "
                            "--sandbox -s --add-dir --ask-for-approval -a --session-id "
                            "--resume -r --agent --permission-mode --allowedTools "
                            "--disallowedTools --mcp-config --settings "
                            "--append-system-prompt --system-prompt --cwd".split())
_CLAUDE_FLAG_OPTIONS = set("--oss --strict-config --dangerously-bypass-approvals-and-sandbox "
                           "--yolo --dangerously-bypass-hook-trust --worktree --no-alt-screen "
                           "--no-daemon --search --help -h --version -V --dangerously-skip-permissions "
                           "--allow-dangerously-skip-permissions --verbose --debug --continue "
                           "--no-session-persistence --print-logs".split())


def invocation_kind(argv: list[str]) -> str:
    """Classify known Claude commands without importing Fleet's process model."""
    if not argv:
        return "unknown"
    executable = os.path.basename(argv[0]).lower()
    tokens = list(argv)
    if executable in {"node", "nodejs", "bun"} and len(tokens) > 1:
        script = tokens[1]
        if "@anthropic-ai/claude-code" not in script and "claude-code" not in script:
            return "unknown"
        tokens = ["claude", *tokens[2:]]
    elif executable not in {"claude", "claude-code"} and "/claude/versions/" not in argv[0].lower():
        # Native launcher paths are accepted only when argv names that launcher.
        if not executable.endswith("claude"):
            return "unknown"
        tokens[0] = "claude"
    index = 1
    while index < len(tokens):
        word = tokens[index]
        if word == "--":
            return "session"
        if word in {"-p", "--print"}:
            return "session"
        if word in _CLAUDE_VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                return "unknown"
            index += 2
            continue
        if word in _CLAUDE_FLAG_OPTIONS or (word.startswith("--") and "=" in word):
            if word in {"--help", "-h", "--version", "-V"}:
                return "command"
            index += 1
            continue
        if word.startswith("-"):
            return "unknown"
        return "command" if word in _CLAUDE_COMMANDS else "session"
    return "session"


def _native_session_observation(home: Path, pid: str, start: str) -> tuple[str | None, bool | None]:
    """Return a claim and validity: True observed, False invalid, None unreadable.

    Selection and invalidation need different evidence. The raw census reader
    below still reports both invalid and unreadable native records as unusable.
    """
    path = home / "sessions" / (pid + ".json")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, True
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return None, False
    if info.st_size > 65536:
        return None, None
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
            return None, None
        raw = os.read(fd, 65537)
        after = os.fstat(fd)
        current = path.lstat()
        if (len(raw) != info.st_size or len(raw) > 65536 or (after.st_dev, after.st_ino, after.st_size) !=
                (info.st_dev, info.st_ino, info.st_size)
                or (current.st_dev, current.st_ino, current.st_size) !=
                (info.st_dev, info.st_ino, info.st_size)):
            return None, None
    except OSError:
        return None, None
    finally:
        if fd is not None:
            os.close(fd)
    try:
        row = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None, False
    if not isinstance(row, dict):
        return None, False
    recorded_pid = row.get("pid")
    recorded_start = row.get("procStart", row.get("proc_start"))
    sid = row.get("sessionId")
    if (str(recorded_pid) != pid or str(recorded_start or "") != start
            or not isinstance(sid, str) or not sid):
        return None, False
    return sid, True


def _native_session_claim(home: Path, pid: str, start: str):
    sid, valid = _native_session_observation(home, pid, start)
    return sid, valid is True


def _conversation_tokens(argv: list[str]) -> list[str] | None:
    """Only a native launcher or the supported Node/Bun CLI script position."""
    if not argv:
        return None
    name = os.path.basename(argv[0]).lower()
    if name in {"node", "nodejs", "bun"}:
        if (len(argv) < 2 or Path(argv[1]).parent.name != "claude-code"
                or Path(argv[1]).name not in {"cli.js", "cli.mjs", "cli.cjs"}):
            return None
        return ["claude", *argv[2:]]
    if name in {"claude bg-spare", "claude bg-pty-host"}:
        return ["claude", name.split(" ", 1)[1], *argv[1:]]
    if name in {"claude", "claude-code"} or _native_claude_executable(argv[0]):
        return ["claude", *argv[1:]]
    return None


def conversation_invocation(argv: list[str]) -> str:
    """Fleet conversation mode; census holder semantics deliberately stay separate."""
    tokens = _conversation_tokens(argv)
    if tokens is None:
        return "unknown"
    services = _CLAUDE_COMMANDS | {"daemon", "remote-control", "bg-spare", "bg-pty-host"}
    index = 1
    while index < len(tokens):
        word = tokens[index]
        if word in {"--", "-p", "--print"}:
            return "session"
        if word.split("=", 1)[0] in {"--bg-pty-host", "--bg-spare"}:
            return "command"
        if word in _CLAUDE_VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                return "unknown"
            index += 2
            continue
        if word in _CLAUDE_FLAG_OPTIONS or word == "--fork-session":
            if word in {"--help", "-h", "--version", "-V"}:
                return "command"
            index += 1
            continue
        if word.startswith("--") and "=" in word:
            option = word.split("=", 1)[0]
            if option not in _CLAUDE_VALUE_OPTIONS:
                return "unknown"
            index += 1
            continue
        if word.startswith("-"):
            return "unknown"
        return "command" if word in services else "session"
    return "session"


def observe_process(pid, *, proc_root: str | os.PathLike = "/proc"):
    """A bounded same-user PID/start/executable/NUL-argv observation, or unknown.

    The second read rejects exits, PID reuse and exec during observation. This
    focused predicate is shared by the producer and both Fleet consumers; it
    never enumerates processes or decides whether a recipient is dead.
    """
    if isinstance(pid, bool) or not str(pid).isdecimal() or int(pid) <= 1:
        return None
    proc = Path(proc_root) / str(pid)

    def read():
        uid = proc.stat().st_uid
        with (proc / "stat").open(encoding="ascii") as handle:
            text = handle.read(8193)
        with (proc / "cmdline").open("rb") as handle:
            raw = handle.read(32769)
        if len(text) > 8192 or not raw or len(raw) > 32768 or not raw.endswith(b"\0"):
            return None
        fields = text[text.rfind(")") + 2:].split()
        start = _proc_start(text)
        argv = [os.fsdecode(item) for item in raw[:-1].split(b"\0")]
        if (uid != os.geteuid() or not start.isdecimal() or len(fields) < 20
                or not fields[1].isdecimal() or fields[0] in {"Z", "X", "x"}
                or not argv[0]):
            return None
        return uid, start, int(fields[1]), argv, os.readlink(proc / "exe")

    try:
        before = read()
        if before is None or read() != before:
            return None
    except (OSError, ValueError, UnicodeError):
        return None
    _, start, parent, argv, executable = before
    tokens = _conversation_tokens(argv)
    launcher = os.path.basename(executable).lower()
    native_executable = _native_claude_executable(executable)
    native = (native_executable and tokens is not None
              and os.path.basename(argv[0]).lower() not in {"node", "nodejs", "bun"})
    scripted = (launcher in {"node", "nodejs", "bun"} and tokens is not None
                and os.path.basename(argv[0]).lower() in {"node", "nodejs", "bun"})
    mode = conversation_invocation(argv) if native or scripted else "unknown"
    if native or scripted:
        kind = {"command": "service", "session": "runtime"}.get(mode, "unknown")
    elif native_executable and os.path.basename(argv[0]).lower() not in _EMBEDDED_HELPERS:
        kind = "unknown"
    else:
        kind = "helper"
    return {"pid": int(pid), "proc_start": start, "ppid": parent,
            "argv": argv, "executable": executable, "kind": kind, "invocation": mode}


class _InvalidTapClaimError(OSError):
    """Positive file ownership/type rejection, rather than a failed read."""


def _focused_tap_claim(path: Path, pid, start: str):
    """Read one bounded owner claim, never stdin-supplied PID evidence."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise _InvalidTapClaimError("statusline fallback is not an owner regular file")
        if before.st_size > 65536:
            raise OSError("statusline fallback file unusable")
        raw = os.read(fd, 65537)
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_size,
                              info.st_mtime_ns, info.st_ctime_ns)
        if (len(raw) != before.st_size or len(raw) > 65536 or stamp(os.fstat(fd)) != stamp(before)
                or stamp(path.lstat()) != stamp(before)):
            raise OSError("statusline fallback file changed")
        row = json.loads(raw)
        if not isinstance(row, dict):
            raise ValueError("invalid statusline fallback")
        sid = row.get("session_id") or path.name[:-5]
        if (str(row.get("pid")) == str(pid) and str(row.get("proc_start")) == start
                and isinstance(sid, str) and sid):
            return sid
        return None
    finally:
        os.close(fd)


def _focused_tap_claims(home: Path, pid, start: str):
    """Bound registry-loss work without changing the census's raw tap reader."""
    directory = home / ".statusline"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return set()  # first event; unreadable evidence is not an empty inventory
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("statusline fallback directory unusable")
    claims = set()
    with os.scandir(directory) as entries:
        for count, entry in enumerate(entries):
            if count >= 128:
                raise OSError("statusline fallback inventory incomplete")
            if entry.name.startswith(".") or not entry.name.endswith(".json"):
                continue
            claim = _focused_tap_claim(Path(entry.path), pid, start)
            if claim:
                claims.add(claim)
    return claims


def statusline_identity(parent_pid, sid: str, home: str | os.PathLike, *,
                        proc_root: str | os.PathLike = "/proc", max_depth: int = 5):
    """Bind only the nearest verified runtime; a mismatch never borrows an ancestor."""
    return statusline_bindings(parent_pid, sid, home, proc_root=proc_root, max_depth=max_depth)[0]


def statusline_bindings(parent_pid, sid: str, home: str | os.PathLike, *,
                        proc_root: str | os.PathLike = "/proc", max_depth: int = 5):
    """Return (current-SID proof, tap claim, tap replacement policy).

    Failed selection may still preserve this callback's prior exact claim on
    the freshly verified nearest runtime. An unobserved result authorizes no
    new PID evidence and leaves existing claims untouched; it does not reject
    them. Positive mismatches still permit replacement without PID fields.
    """
    if not isinstance(sid, str) or not sid or Path(sid).name != sid:
        return None, None, "unobserved"
    pid = parent_pid
    for _ in range(max_depth):
        observed = observe_process(pid, proc_root=proc_root)
        if observed is None:
            # A foreign owner is positive rejection. Other failed process
            # reads cannot establish that an existing claim became invalid.
            try:
                foreign = (Path(proc_root) / str(pid)).stat().st_uid != os.geteuid()
            except OSError:
                foreign = False
            return None, None, "replace" if foreign else "unobserved"
        if observed["kind"] == "service":
            return None, None, "replace"
        if observed["kind"] == "helper":
            pid = observed["ppid"]
            continue
        start = observed["proc_start"]
        try:
            current, valid = _native_session_observation(Path(home), str(pid), start)
        except OSError:
            return None, None, "unobserved"
        if valid is None:
            return None, None, "unobserved"
        if not valid:
            return None, None, "replace"
        if observed["kind"] == "unknown":
            # Unknown argv stops ancestry lookup and supplies no proof. Known
            # PID/start or current-native mismatches may still reject a prior.
            if current not in {None, sid}:
                return None, None, "replace"
            try:
                prior = _focused_tap_claim(Path(home) / ".statusline" / (sid + ".json"), pid, start)
            except _InvalidTapClaimError:
                return None, None, "replace"
            except (OSError, ValueError):
                return None, None, "unobserved"
            if observe_process(pid, proc_root=proc_root) != observed:
                return None, None, "unobserved"
            return None, None, "replace" if prior != sid else "unobserved"
        selected = current == sid
        retained = False
        if current is None:
            try:
                claims = _focused_tap_claims(Path(home), pid, start)
            except (OSError, ValueError):
                claims = None  # incomplete/unreadable, not first-event silence
            if claims == set():
                # Launch selectors may bind a first event only after checking
                # prior exact taps. Resume values are never current proof.
                tokens = _conversation_tokens(observed["argv"])
                index = 1
                while index < len(tokens):
                    word = tokens[index]
                    if word in {"--", "-p", "--print"}:
                        break
                    if word in _CLAUDE_VALUE_OPTIONS:
                        if index + 1 >= len(tokens):
                            break
                        if word == "--session-id":
                            claims.add(tokens[index + 1])
                        index += 2
                    elif word.startswith("--session-id="):
                        claims.add(word.split("=", 1)[1])
                        index += 1
                    elif word in _CLAUDE_FLAG_OPTIONS or word == "--fork-session":
                        index += 1
                    else:
                        break
            selected = claims == {sid}
            if not selected:
                try:
                    retained = _focused_tap_claim(Path(home) / ".statusline" / (sid + ".json"), pid, start) == sid
                except _InvalidTapClaimError:
                    retained = False
                except (OSError, ValueError):
                    return None, None, "unobserved"
        if observe_process(pid, proc_root=proc_root) != observed:
            return None, None, "unobserved"
        return (observed if selected else None, observed if selected or retained else None,
                "replace")
    return None, None, "unobserved"


def _remote_mount(home: Path, mounts_path: Path) -> bool:
    try:
        home = home.resolve(strict=True)
    except OSError:
        return True
    try:
        rows = mounts_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    best = (0, "")
    for row in rows:
        fields = row.split()
        if len(fields) < 3:
            continue
        mountpoint = fields[1].replace("\\040", " ")
        try:
            relative = home.relative_to(Path(mountpoint))
        except ValueError:
            continue
        if len(mountpoint) > best[0]:
            best = (len(mountpoint), fields[2])
    return (not best[1] or best[1] in
            {"nfs", "nfs4", "cifs", "smb3", "9p", "sshfs", "ceph", "glusterfs"})


def _owner_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()


def is_local_path(path: str | os.PathLike, *, mounts_path: str | os.PathLike = "/proc/mounts") -> bool:
    """Require a known local filesystem for native session or watch evidence."""
    mount_file = Path(mounts_path)
    return not _remote_mount(Path(path), mount_file)


def claude_session_census(*, proc_root: str | os.PathLike = "/proc",
                          budget_seconds: float = 1.0):
    """Return ``(complete, live_sids, reasons)`` from a bounded host census.

    Unreadable process identity, unbound Claude processes, namespace mismatch,
    malformed native claims, and process churn make the result incomplete.
    A process in a nested PID namespace binds by its own innermost pid; otherwise
    namespace mismatch keeps the census incomplete.
    """
    root = Path(proc_root)
    started = time.monotonic()
    reasons = set()
    holders = set()
    try:
        host_namespace = os.readlink(root / "self" / "ns" / "pid")
        if host_namespace != _INITIAL_PID_NAMESPACE:
            return False, holders, {"observer-not-in-initial-pid-namespace"}
        pids = sorted(item.name for item in root.iterdir() if item.name.isdecimal())
    except OSError:
        return False, holders, {"proc-inventory-unavailable"}
    candidates = {}
    tap_indexes = {}
    mounts_path = root / "mounts"
    if not mounts_path.exists():
        mounts_path = Path("/proc/mounts")
    for pid in pids:
        if time.monotonic() - started > budget_seconds:
            reasons.add("census-budget-exhausted")
            break
        proc = root / pid
        try:
            identity = _runtime_identity(proc)
        except OSError:
            if proc.exists():
                reasons.add("process-identity-unreadable")
            continue
        if identity is None:
            continue
        process_uid, start, argv, executable = identity
        native_exe = bool(executable and _native_claude_executable(executable))
        argv0 = os.path.basename(argv[0]).lower() if argv else ""
        if process_uid != os.getuid():
            if _claude_shaped_argv(argv):
                reasons.add("foreign-claude-shaped-process")
            continue
        if native_exe and argv0 in _EMBEDDED_HELPERS:
            continue
        if native_exe and not _claude_shaped_argv(argv):
            reasons.add("native-process-argv-unresolved")
            continue
        if not native_exe and not _claude_shaped_argv(argv):
            # A same-uid process with an unreadable executable is resolved by
            # its argv. Ordinary non-Claude helpers are outside this census.
            continue
        try:
            namespace = os.readlink(proc / "ns" / "pid")
            environ = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            reasons.add("process-context-unreadable")
            continue
        claim_pid = pid
        if namespace != host_namespace:
            claim_pid = _namespace_pid(proc, pid)
            if claim_pid is None:
                reasons.add("pid-namespace-mismatch")
                continue
        env = {}
        for item in environ:
            if b"=" in item:
                key, value = item.split(b"=", 1)
                if key in {b"CLAUDE_CONFIG_DIR", b"HOME"}:
                    env[key.decode()] = value.decode("utf-8", "replace")
        home_value = env.get("CLAUDE_CONFIG_DIR") or str(Path(env.get("HOME", "")).expanduser() / ".claude")
        if not home_value or not Path(home_value).is_absolute():
            reasons.add("config-home-unresolved")
            continue
        home = Path(home_value)
        if (not _owner_directory(home) or not _owner_directory(home / "sessions")
                or _remote_mount(home, mounts_path)):
            reasons.add("remote-config-home")
            continue
        candidates[pid] = (start, argv, home)
        argv_sids = _argument_sessions(argv)
        holders.update(argv_sids)
        if len(argv_sids) > 1:
            reasons.add("conflicting-session-arguments")
        native_sid, valid = _native_session_claim(Path(home_value), claim_pid, start)
        if not valid:
            reasons.add("native-session-claim-unusable")
            continue
        home_key = str(home)
        if home_key not in tap_indexes:
            try:
                statusline_dir = home / ".statusline"
                try:
                    statusline_info = statusline_dir.lstat()
                except FileNotFoundError:
                    tap_indexes[home_key] = {}
                else:
                    if not stat.S_ISDIR(statusline_info.st_mode):
                        raise OSError("statusline tap path is not a directory")
                    tap_indexes[home_key] = tap_claim_index(home, strict=True)
            except OSError:
                tap_indexes[home_key] = None
        if tap_indexes[home_key] is None:
            reasons.add("statusline-tap-index-incomplete")
            continue
        taps = tap_sids_by_pid(home, claim_pid, start, index=tap_indexes[home_key])
        claims = {native_sid} if native_sid else set()
        claims.update(taps)
        holders.update(claims)
        if not claims:
            if not argv_sids and invocation_kind(argv) == "command":
                continue
            reasons.add("session-identity-unresolved")
            continue
        # Native, exact tap, and argv aliases may all describe one process's
        # session history. Every exact claim is a holder; multiplicity is safe.

    if not reasons:
        try:
            final_pids = sorted(item.name for item in root.iterdir() if item.name.isdecimal())
            final_candidates = {}
            for pid in final_pids:
                identity = _runtime_identity(root / pid)
                if identity is not None:
                    process_uid, start, argv, executable = identity
                    native_exe = bool(executable and _native_claude_executable(executable))
                    if (process_uid == os.getuid()
                            and not (native_exe and os.path.basename(argv[0]).lower() in _EMBEDDED_HELPERS)
                            and (native_exe or _claude_shaped_argv(argv))):
                        final_candidates[pid] = start
            if final_candidates != {pid: item[0] for pid, item in candidates.items()}:
                reasons.add("process-inventory-changed")
        except OSError:
            reasons.add("closing-inventory-unavailable")
    if time.monotonic() - started > budget_seconds:
        reasons.add("census-budget-exhausted")
    return not reasons, holders, reasons


def tap_sids_by_pid(home: str | os.PathLike, pid, proc_start: str,
                    *, index: dict[tuple[str, str], set[str]] | None = None) -> set[str]:
    """Return only statusline claims bound to this exact live process identity."""
    if pid is None or not proc_start:
        return set()
    claims = tap_claim_index(home) if index is None else index
    return set(claims.get((str(pid), str(proc_start)), ()))


def tap_sid_by_pid(home: str | os.PathLike, pid, proc_start: str) -> str | None:
    claims = tap_sids_by_pid(home, pid, proc_start)
    return next(iter(claims)) if len(claims) == 1 else None
