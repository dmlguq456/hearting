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
    process_path = os.fspath(proc)
    try:
        process_uid = proc.stat().st_uid
        with open(process_path + "/stat", encoding="ascii") as source:
            stat_text = source.read()
        start = _proc_start(stat_text)
    except FileNotFoundError:
        return None
    try:
        with open(process_path + "/cmdline", "rb") as source:
            argv = [item.decode("utf-8", "replace") for item in source.read().split(b"\0") if item]
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
    """Session ids a Claude argv names for its own conversation.

    ``--fork-session`` resumes a copy under a new id and leaves the original
    unchanged on disk; ``--session-id`` names the conversation's id. With both,
    the one ``--session-id`` value is this process's session and the resumed
    source is not held. Without ``--fork-session``, every named id stays a claim.
    """
    values = set()
    named = set()
    value_flags = {"--resume", "--session", "--session-id", "--resume-session", "-r"}
    for index, token in enumerate(argv):
        if token in value_flags and index + 1 < len(argv):
            value = argv[index + 1]
            if value and not value.startswith("-"):
                (named if token == "--session-id" else values).add(value)
        elif any(token.startswith(flag + "=") for flag in value_flags if flag.startswith("--")):
            flag, value = token.split("=", 1)
            if value:
                (named if flag == "--session-id" else values).add(value)
    if "--fork-session" in argv and len(named) == 1:
        return named
    return values | named


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


class ClaimedSessions(set):
    """Census holders that remember which process made each claim.

    ``process_claims`` holds one ``(native_sid | None, claimed_sids)`` pair per
    counted process, so a caller can tell a session that only one live process's
    stale claim names from one that process currently runs. Set equality and
    membership are unchanged; a plain ``set`` simply carries no process claims.
    """

    process_claims: tuple = ()


_SESSION_SHELL_SCRIPTS = {"entry.py": "entry", "host.cjs": "host"}


def _session_shell_role(argv: list[str]) -> str | None:
    """``entry``/``host`` for Hearting's own Claude session-shell wrapper argv.

    ``entry.py`` runs as ``python utilities/session-shell/entry.py claude
    <native> ...`` and forks the native Claude with that exact ``<native> ...``
    argv; its display ``host.cjs`` runs as ``node .../host.cjs claude <native>
    ...``. The shape alone excludes nothing: see ``claude_session_census``.
    """
    if len(argv) < 4 or argv[2] != "claude":
        return None
    script = Path(argv[1])
    role = _SESSION_SHELL_SCRIPTS.get(script.name)
    if role is None or script.parent.name != "session-shell" or script.parent.parent.name != "utilities":
        return None
    interpreter = os.path.basename(argv[0]).lower()
    if role == "entry" and interpreter.startswith("python"):
        return role
    if role == "host" and interpreter in {"node", "nodejs"}:
        return role
    return None


def _background_role(argv: list[str]) -> str | None:
    """``supervisor``/``pty-host``/``spare`` for Claude Code's own background argv.

    ``claude daemon run`` is the background-session supervisor; it starts each
    session under ``claude --bg-pty-host <pty.sock> <cols> <rows> -- <exe>
    <args>``, and keeps a waiting ``--bg-spare <claim.sock>`` process that takes
    the next session over without changing its argv. The shape alone excludes
    nothing: see ``claude_session_census``.
    """
    if argv[1:3] == ["daemon", "run"]:
        return "supervisor"
    if argv[1:2] == ["--bg-pty-host"] and "--" in argv[2:]:
        return "pty-host"
    if len(argv) == 3 and argv[1] == "--bg-spare":
        return "spare"
    return None


def _bounded_json(path: Path, limit: int):
    """One same-user regular file's JSON value, or None when absent or unusable."""
    fd = None
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            return None
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


_SUPERVISOR_JOB_LIMIT = 4096


def _supervisor_lock_names(home: Path, pid: str, start: str) -> bool:
    lock = _bounded_json(home / "daemon.lock", 65536)
    return (isinstance(lock, dict) and str(lock.get("pid")) == pid
            and str(lock.get("procStart") or "") == start)


def _supervisor_sessions(home: Path, pid: str, start: str) -> set[str] | None:
    """Every session id this exact supervisor may carry, or None when not exact.

    The supervisor runs no conversation itself; each background session is its
    own process. Between processes it does carry one: a respawn stops the old
    worker and hands the session to a spare about 0.1 s later, while the job's
    state reads ``stopped`` and no process or roster entry names it. Every
    session it starts or carries has ``jobs/<id>/state.json`` naming it before
    dispatch and through respawn, so all of those ids count as held while
    ``daemon.lock`` names this pid and start. An unreadable, symlinked or
    still-empty job record makes the result None.
    """
    if not _supervisor_lock_names(home, pid, start):
        return None
    jobs = home / "jobs"
    try:
        entries = list(os.scandir(jobs))
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    if len(entries) > _SUPERVISOR_JOB_LIMIT or not _owner_directory(jobs):
        return None
    sessions = set()
    for entry in entries:
        try:
            if entry.is_symlink():
                return None
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            return None
        row = _bounded_json(Path(entry.path) / "state.json", 1 << 20)
        sid = row.get("sessionId") if isinstance(row, dict) else None
        if not isinstance(sid, str) or not sid:
            return None
        sessions.add(sid)
    return sessions


def _spare_waiting(proc: Path, home: Path, pid: str, start: str, claim: str) -> bool:
    """A spare that has not taken a session: it still listens in its spare directory.

    Taking a session, a spare first removes its claim socket, then moves to the
    session's directory and only then writes ``sessions/<pid>.json``. A session
    still in flight to it (about a millisecond) is already held through the
    supervisor's job records.
    """
    own = _process_parent(proc)
    try:
        socket_info = os.lstat(claim)
        cwd = os.readlink(proc / "cwd")
    except OSError:
        return False
    try:
        (home / "sessions" / (pid + ".json")).lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    else:
        return False
    return (own is not None and own[1] == start and stat.S_ISSOCK(socket_info.st_mode)
            and socket_info.st_uid == os.getuid() and cwd == os.path.dirname(claim))


def _process_parent(proc: Path) -> tuple[str, str] | None:
    """``(ppid, start ticks)`` from one stat read, or None when unreadable."""
    try:
        with open(os.fspath(proc) + "/stat", encoding="ascii") as source:
            text = source.read()
    except (OSError, UnicodeError):
        return None
    close = text.rfind(")")
    fields = text[close + 2:].split()
    if close < 0 or len(fields) <= 19:
        return None
    return fields[1], fields[19]


def claude_session_census(*, proc_root: str | os.PathLike = "/proc",
                          budget_seconds: float = 1.0):
    """Return ``(complete, live_sids, reasons)`` from a bounded host census.

    Unreadable process identity, unbound Claude processes, namespace mismatch,
    malformed native claims, and process churn make the result incomplete.
    A process in a nested PID namespace binds by its own innermost pid; otherwise
    namespace mismatch keeps the census incomplete.

    Hearting's session-shell wrapper (``entry.py`` and its ``host.cjs``) repeats
    its native Claude child's argv. It adds no unresolved reason only while that
    direct child, in the same PID namespace with the identical argv, is confirmed
    by its own native session record and the wrapper holds no claim of its own.
    Otherwise it is judged like any other Claude-shaped process.

    Claude Code's own background processes follow the same rule. The supervisor
    (``claude daemon run``) whose ``daemon.lock`` names its pid and start holds
    every session its ``jobs/`` records name, and nothing else. A ``--bg-spare``
    under a ``--bg-pty-host`` of that supervisor holds nothing while its claim
    socket and spare directory show it waiting, rechecked when the census closes.
    A ``--bg-pty-host`` adds no unresolved reason only while its direct child
    runs its exact command and is confirmed or such a waiting spare. Anything
    less exact stays incomplete; these rules only add holders, never remove one.
    """
    root = Path(proc_root)
    started = time.monotonic()
    reasons = set()
    holders = ClaimedSessions()
    process_claims = []
    try:
        host_namespace = os.readlink(root / "self" / "ns" / "pid")
        if host_namespace != _INITIAL_PID_NAMESPACE:
            return False, holders, {"observer-not-in-initial-pid-namespace"}
        pids = sorted(item.name for item in root.iterdir() if item.name.isdecimal())
    except OSError:
        return False, holders, {"proc-inventory-unavailable"}
    candidates = {}
    tap_indexes = {}
    remote_homes = {}
    mounts_path = root / "mounts"
    if not mounts_path.exists():
        mounts_path = Path("/proc/mounts")

    def observe(pid, proc, start, argv):
        """One candidate's evidence, applied by ``commit``; never mutates the census."""
        seen = {"reasons": set(), "holders": set(), "claim": None, "candidate": None,
                "namespace": None, "native_sid": None, "own_claims": set()}
        found = seen["reasons"]
        try:
            namespace = os.readlink(proc / "ns" / "pid")
            environ = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            found.add("process-context-unreadable")
            return seen
        seen["namespace"] = namespace
        claim_pid = pid
        if namespace != host_namespace:
            claim_pid = _namespace_pid(proc, pid)
            if claim_pid is None:
                found.add("pid-namespace-mismatch")
                return seen
        env = {}
        for item in environ:
            if b"=" in item:
                key, value = item.split(b"=", 1)
                if key in {b"CLAUDE_CONFIG_DIR", b"HOME"}:
                    env[key.decode()] = value.decode("utf-8", "replace")
        home_value = env.get("CLAUDE_CONFIG_DIR") or str(Path(env.get("HOME", "")).expanduser() / ".claude")
        if not home_value or not Path(home_value).is_absolute():
            found.add("config-home-unresolved")
            return seen
        home = Path(home_value)
        if home_value not in remote_homes:
            # One mount table per census: every process naming this home has
            # the same answer, so read and match the table once per home.
            remote_homes[home_value] = _remote_mount(home, mounts_path)
        if (not _owner_directory(home) or not _owner_directory(home / "sessions")
                or remote_homes[home_value]):
            found.add("remote-config-home")
            return seen
        seen["candidate"] = (start, argv, home)
        argv_sids = _argument_sessions(argv)
        seen["holders"].update(argv_sids)
        if len(argv_sids) > 1:
            found.add("conflicting-session-arguments")
        native_sid, valid = _native_session_claim(home, claim_pid, start)
        if not valid:
            found.add("native-session-claim-unusable")
            if argv_sids:
                seen["claim"] = (None, frozenset(argv_sids))
            return seen
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
            found.add("statusline-tap-index-incomplete")
            if argv_sids:
                seen["claim"] = (None, frozenset(argv_sids))
            return seen
        taps = tap_sids_by_pid(home, claim_pid, start, index=tap_indexes[home_key])
        claims = {native_sid} if native_sid else set()
        claims.update(taps)
        seen["holders"].update(claims)
        seen["own_claims"] = claims
        seen["native_sid"] = native_sid or None
        if argv_sids or claims:
            seen["claim"] = (native_sid or None, frozenset(argv_sids | claims))
        if not claims:
            if not argv_sids and invocation_kind(argv) == "command":
                return seen
            found.add("session-identity-unresolved")
        # Native, exact tap, and argv aliases may all describe one process's
        # session history. Every exact claim is a holder; multiplicity is safe.
        return seen

    def commit(pid, seen, *, repeats_child=False):
        if seen["candidate"] is not None:
            candidates[pid] = seen["candidate"]
        holders.update(seen["holders"])
        if repeats_child:
            # The confirmed child already names these argv ids itself.
            reasons.update(seen["reasons"] - {"session-identity-unresolved"})
            return
        reasons.update(seen["reasons"])
        if seen["claim"] is not None:
            process_claims.append(seen["claim"])

    def claims_nothing_itself(seen):
        return (seen["candidate"] is not None and not seen["own_claims"]
                and seen["reasons"] <= {"session-identity-unresolved",
                                        "conflicting-session-arguments"})

    confirmed = {}
    wrappers = []
    deferred = {}
    rechecks = []
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
        seen = observe(pid, proc, start, argv)
        role = None if native_exe else _session_shell_role(argv)
        if role is not None:
            # Judged after every child is seen: a child may sort before or after.
            wrappers.append((role, pid, proc, start, argv, seen))
            continue
        background = _background_role(argv) if native_exe else None
        if background is not None and not seen["native_sid"]:
            # Judged after every process is seen, like the session-shell wrapper.
            deferred[pid] = (background, proc, start, argv, seen)
            continue
        commit(pid, seen)
        if native_exe and seen["native_sid"]:
            confirmed[pid] = (seen["namespace"], start, argv)

    def quiet(seen):
        """No claim, argv id, or reason other than the missing session identity."""
        return (claims_nothing_itself(seen) and not seen["holders"]
                and seen["reasons"] <= {"session-identity-unresolved"}
                and seen["namespace"] == host_namespace)

    supervisors = {}
    for pid, (background, proc, start, argv, seen) in deferred.items():
        if background != "supervisor":
            continue
        home = seen["candidate"][2] if seen["candidate"] is not None else None
        carried = _supervisor_sessions(home, pid, start) if quiet(seen) else None
        if carried is not None:
            supervisors[pid] = (start, home)
            seen["holders"].update(carried)
            seen["claim"] = (None, frozenset(carried)) if carried else None
            seen["reasons"].discard("session-identity-unresolved")
            rechecks.append(lambda home=home, pid=pid, start=start:
                            _supervisor_lock_names(home, pid, start))
        commit(pid, seen)

    def pty_tail(argv):
        tail = argv[argv.index("--", 2) + 1:]
        return tail if tail and _native_claude_executable(tail[0]) else None

    waiting = {}
    for pid, (background, proc, start, argv, seen) in deferred.items():
        if background != "spare":
            continue
        claim = argv[2]
        parent = _process_parent(proc)
        host = deferred.get(parent[0]) if parent is not None and parent[1] == start else None
        verified = False
        if (quiet(seen) and os.path.isabs(claim) and claim.endswith(".claim.sock")
                and os.path.basename(os.path.dirname(claim)) == "spare"
                and host is not None and host[0] == "pty-host"
                and host[4]["namespace"] == seen["namespace"]
                and (pty_tail(host[3]) or [None])[1:] == ["--bg-spare", claim]):
            grandparent = _process_parent(host[1])
            supervisor = supervisors.get(grandparent[0]) if grandparent is not None else None
            home = seen["candidate"][2]
            verified = (supervisor is not None and grandparent[1] == host[2]
                        and supervisor[1] == home
                        and _spare_waiting(proc, home, pid, start, claim))
            if verified:
                waiting[pid] = (seen["namespace"], start, argv)
                rechecks.append(lambda proc=proc, home=home, pid=pid, start=start, claim=claim:
                                _spare_waiting(proc, home, pid, start, claim))
        commit(pid, seen, repeats_child=verified)

    def explained_pty_host(pid, proc, start, argv, seen):
        """A direct child running this host's exact command, confirmed or waiting."""
        own = _process_parent(proc)
        tail = pty_tail(argv)
        if (not claims_nothing_itself(seen) or seen["namespace"] != host_namespace
                or own is None or own[1] != start or tail is None):
            return False
        for child, (namespace, child_start, child_argv) in (*confirmed.items(), *waiting.items()):
            if (namespace == seen["namespace"] and child_argv[1:] == tail[1:]
                    and (child_argv[0] == tail[0]
                         or (child_argv[0] == "claude bg-spare" and tail[1:2] == ["--bg-spare"]))
                    and _process_parent(root / child) == (pid, child_start)):
                return True
        return False

    for pid, (background, proc, start, argv, seen) in deferred.items():
        if background == "pty-host":
            commit(pid, seen, repeats_child=explained_pty_host(pid, proc, start, argv, seen))

    def confirmed_shell_child(pid, start, argv, seen):
        """A direct native child with this wrapper's exact argv tail and namespace."""
        own = _process_parent(root / pid)
        if not claims_nothing_itself(seen) or own is None or own[1] != start:
            return False
        for child, (namespace, child_start, child_argv) in confirmed.items():
            if (child_argv == argv[3:] and namespace == seen["namespace"]
                    and _process_parent(root / child) == (pid, child_start)):
                return True
        return False

    entries = {}
    for role, pid, proc, start, argv, seen in wrappers:
        if role == "entry":
            verified = confirmed_shell_child(pid, start, argv, seen)
            if verified:
                entries[pid] = (argv, seen["namespace"])
            commit(pid, seen, repeats_child=verified)
    for role, pid, proc, start, argv, seen in wrappers:
        if role == "host":
            parent = _process_parent(proc)
            entry = entries.get(parent[0]) if parent is not None and parent[1] == start else None
            verified = (entry is not None and claims_nothing_itself(seen)
                        and entry[0][2:] == argv[2:] and entry[1] == seen["namespace"])
            commit(pid, seen, repeats_child=verified)

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
            elif not all(check() for check in rechecks):
                # A supervisor lost its lock or a spare took a session meanwhile.
                reasons.add("process-inventory-changed")
        except OSError:
            reasons.add("closing-inventory-unavailable")
    if time.monotonic() - started > budget_seconds:
        reasons.add("census-budget-exhausted")
    holders.process_claims = tuple(process_claims)
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
