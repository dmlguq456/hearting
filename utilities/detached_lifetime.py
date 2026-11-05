#!/usr/bin/env python3
"""End a detached helper together with the test that started it.

Hearting starts its helpers detached (``start_new_session=True``, ``setsid``) because in
production each one must outlive the hook or command that started it. Started from a
test, the same design lets the helper outlive the test and go on acting on the machine:
three ``__obligation-runner`` processes from a direct test run (2026-10-31) ran for days,
and a ``compute-hosts watch-run`` started by a direct ``compute_hosts.test.py`` run
(2026-11-02) was still alive 34 hours later with the caller's live session id in its
environment. Reapers in the test runner or in one fixture miss every test run directly
(``python3 x.test.py``, ``python3 -m unittest``) and every test killed before its
cleanup ran.

So each detaching spawn site passes the child it just started to :func:`bind`. ``bind``
looks for the nearest *Hearting test process* among the caller and its ancestors: an
interpreter running a test file (``*.test.py``, ``test_*.py``, ``*_test.py``,
``*.test.sh``, ``*.test.js`` ...) that lies in a Hearting tree, or a test runner
(``-m unittest``, ``pytest``, ``node --test``) working in one; of a test and its forks
(which run the same command) the outermost is the test. A test-shaped program outside
any Hearting tree (a user's ``run_test.py``) is not one. Outside a test there is none and
``bind`` returns after reading a few ``/proc`` entries, so production lifetimes do not
change. Under a test it records the child under that test process and makes sure one
small reaper waits for the test process to end. However the test ends (normally, by an
exception, or SIGKILL), the reaper then kills each recorded child that is still the same
process, together with its descendants, and removes the record. A helper started by a
recorded helper is recorded under the same test, even after its own parent is gone.

The record is a directory private to the user and to this boot: ``/run/user/<uid>/
hearting-detached-lifetime/<boot id>`` (under ``/tmp/hearting-detached-lifetime-<uid>``
without a runtime directory).
``<pid>-<start>.child`` is a symlink naming the test it belongs to, ``<pid>-<start>.lock``
is held by that test's reaper, and the reaper runs from a copy of this file there
(``reaper-<digest>.py``) so it never pins the checkout or release the test ran from.
A reaper that is itself killed leaves its records behind; the next reaper finishes them.
"""
from __future__ import annotations

import fcntl
import os
import re
import select
import signal
import stat
import sys
import time

_TEST_FILE = re.compile(r"(?:^test_.*\.py|_test\.py|\.test\.(?:py|sh|bash|js|mjs|cjs|ts))$")
_PYTHON = re.compile(r"^python[0-9.]*$")
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh"})
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
_NODES = frozenset({"node", "nodejs"})
_NODE_EVAL = frozenset({"-e", "--eval", "-p", "--print"})
_NODE_VALUE_OPTIONS = frozenset({"-r", "--require", "--import", "--loader",
                                 "--experimental-loader", "--conditions", "-C"})
_TEST_MODULES = frozenset({"unittest", "pytest"})
_RUNNERS = frozenset({"pytest", "py.test"})
_RUNNER = "<runner>"  # a test runner (no test file named): its working directory decides
_IDENTITY = re.compile(r"^([0-9]+)-([0-9]+)$")
_BOOT = re.compile(r"^[0-9a-f-]{1,64}$")
_MARKER = os.path.join("utilities", "detached_lifetime.py")
_MAX_DEPTH = 64
_SETTLE_S = 5.0
_COPY_AGE_S = 7 * 86400


# --- which process is the test --------------------------------------------------------

def _is_test_file(path: str) -> bool:
    return bool(_TEST_FILE.search(os.path.basename(path)))


def _python_target(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if arg.startswith("--"):
            i += 2 if arg == "--check-hash-based-pycs" else 1
            continue
        if arg.startswith("-") and arg != "-":
            cluster = arg[1:]
            for j, flag in enumerate(cluster):
                if flag == "c":
                    return None
                if flag == "m":
                    module = cluster[j + 1:] or (args[i + 1] if i + 1 < len(args) else "")
                    return _RUNNER if module in _TEST_MODULES else None
                if flag in "XWQ":
                    if not cluster[j + 1:]:
                        i += 1
                    break
            i += 1
            continue
        break
    if i >= len(args):
        return None
    script = args[i]
    if os.path.basename(script) in _RUNNERS:  # `pytest` through its shebang
        return _RUNNER
    return script if _is_test_file(script) else None


def _shell_target(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if arg in _SHELL_VALUE_OPTIONS:
            i += 2
            continue
        if arg[:1] in ("-", "+") and arg not in ("-", "+"):
            if not arg.startswith("--") and "c" in arg[1:]:
                return None
            i += 1
            continue
        break
    return args[i] if i < len(args) and _is_test_file(args[i]) else None


def _node_target(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _NODE_EVAL:
            return None
        if arg == "--test":  # a node option only before the script; after it, the script's
            return _RUNNER
        if arg in _NODE_VALUE_OPTIONS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg if _is_test_file(arg) else None
    return None


def _test_target(argv: list[str]) -> str | None:
    """The test file ``argv`` runs, ``_RUNNER`` for a test runner, else ``None``."""
    if not argv:
        return None
    name = os.path.basename(argv[0])
    args = list(argv[1:])
    if name in _RUNNERS:
        return _RUNNER
    if _PYTHON.match(name):
        return _python_target(args)
    if name in _SHELLS:
        return _shell_target(args)
    if name in _NODES:
        return _node_target(args)
    return None


def _in_hearting_tree(path: str) -> bool:
    """Whether ``path`` lies in a tree that carries this module (checkout, worktree, release)."""
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    for _ in range(_MAX_DEPTH):
        if os.path.isfile(os.path.join(directory, _MARKER)):
            return True
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent
    return False


def is_test_command(argv: list[str], cwd: str | None) -> bool:
    """Whether ``argv``, run in ``cwd``, is a Hearting test.

    The shape is a test file (``*.test.py``, ``test_*.py``, ``*_test.py``,
    ``*.test.sh``, ``*.test.js`` ...) run by python, a shell or node, or a test runner
    (``-m unittest``, ``-m pytest``, ``pytest``, ``node --test``). Only one located in a
    Hearting tree counts: the test file, or for a runner its working directory, has
    ``utilities/detached_lifetime.py`` above it. A user's ``run_test.py`` elsewhere is
    not a Hearting test, so work Hearting starts under it keeps its lifetime.
    """
    target = _test_target(argv)
    if target is None:
        return False
    if target == _RUNNER:
        location = cwd
    elif os.path.isabs(target):
        location = target
    else:
        location = os.path.join(cwd, target) if cwd else None
    return bool(location) and _in_hearting_tree(os.path.realpath(location))


def _is_test_process(pid: int, argv: list[str]) -> bool:
    if _test_target(argv) is None:  # the common case: no filesystem look at all
        return False
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = None
    return is_test_command(argv, cwd)


# --- /proc ------------------------------------------------------------------------------

def _stat(pid: int):
    """``(state, ppid, pgid, start ticks)`` of a process, or ``None``."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        return fields[0].decode(), int(fields[1]), int(fields[2]), fields[19].decode()
    except (OSError, IndexError, ValueError, UnicodeDecodeError):
        return None


def _argv(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [os.fsdecode(part) for part in parts]


def _live(identity: tuple[int, str]) -> bool:
    info = _stat(identity[0])
    return info is not None and info[3] == identity[1] and info[0] != "Z"


# --- the record -------------------------------------------------------------------------

def _private_dir(path: str, create: bool) -> bool:
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError:
            return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077)


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            value = fh.read().strip()
    except OSError:
        return "unknown-boot"
    return value if _BOOT.match(value) else "unknown-boot"


def _record_dir(create: bool) -> str | None:
    """This boot's private record directory; ``None`` if it is missing or not private.

    Records live under the boot id: a pid and start time from an earlier boot (the /tmp
    fallback survives a reboot) never name a process of this one.
    """
    uid = os.getuid()
    runtime = f"/run/user/{uid}"
    try:
        info = os.lstat(runtime)
        private_runtime = stat.S_ISDIR(info.st_mode) and info.st_uid == uid
    except OSError:
        private_runtime = False
    base = (os.path.join(runtime, "hearting-detached-lifetime") if private_runtime
            else f"/tmp/hearting-detached-lifetime-{uid}")
    if not _private_dir(base, create):
        return None
    path = os.path.join(base, _boot_id())
    return path if _private_dir(path, create) else None


def _name(identity: tuple[int, str]) -> str:
    return "%d-%s" % identity


def _parse(name: str) -> tuple[int, str] | None:
    match = _IDENTITY.match(name)
    return (int(match.group(1)), match.group(2)) if match else None


def test_anchor(pid: int | None = None) -> tuple[int, str] | None:
    """``(pid, start ticks)`` of the test that ``pid`` (default: this process) runs under.

    The nearest ancestor (or the process itself) running a test command, or the test an
    ancestor was recorded under by :func:`bind`. ``None`` outside a test.
    """
    pid = os.getpid() if pid is None else pid
    record = _record_dir(create=False)
    for _ in range(_MAX_DEPTH):
        if pid <= 1:
            return None
        info = _stat(pid)
        if info is None:
            return None
        argv = _argv(pid)
        if _is_test_process(pid, argv):
            # A fork of the test (multiprocessing "fork", os.fork) runs the very same
            # command; the test is the outermost of them, not the worker that ends first.
            while info[1] > 1 and _argv(info[1]) == argv:
                parent = _stat(info[1])
                if parent is None:
                    break
                pid, info = info[1], parent
            return pid, info[3]
        if record is not None:
            try:
                owner = _parse(os.readlink(os.path.join(record, "%d-%s.child" % (pid, info[3]))))
            except OSError:
                owner = None
            if owner is not None:
                return owner
        pid = info[1]
    return None


def bind(child):
    """End a detached ``child`` (a Popen-like object or a pid) with the test that started it.

    Outside a test, or for anything but a child of this process, nothing happens. Never
    raises; returns ``child`` so a spawn expression can be wrapped in place.
    """
    pid = getattr(child, "pid", child)
    if type(pid) is int and pid > 1:
        try:
            _bind(pid)
        except Exception:  # noqa: BLE001 -- a lifetime record never fails a spawn
            pass
    return child


def _bind(pid: int) -> None:
    me = os.getpid()
    info = _stat(pid)
    # A child that already exited is still our unreaped child (a zombie): recording
    # it costs the same as a live one, so this path does the same work either way.
    if info is None or info[1] != me:
        return
    anchor = test_anchor(me)
    if anchor is None:
        return
    record = _record_dir(create=True)
    if record is None:
        return
    try:
        os.symlink(_name(anchor), os.path.join(record, "%d-%s.child" % (pid, info[3])))
    except FileExistsError:
        pass
    _ensure_reaper(record, anchor)


def _ensure_reaper(record: str, anchor: tuple[int, str]) -> None:
    lock = os.open(os.path.join(record, _name(anchor) + ".lock"),
                   os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # this test's reaper is waiting
        program = _reaper_copy(record)
        # The lock moves to the reaper as its fd 3 and stays held until it exits. An
        # intermediate starts the reaper and exits, so the reaper is nobody's child:
        # a caller that waits on or counts its own children never sees it.
        _spawn([_python(), "-I", "-S", program, "--detach", record, _name(anchor)], lock)
    finally:
        os.close(lock)


def _python() -> str:
    return sys.executable if os.path.isabs(sys.executable or "") else "/usr/bin/python3"


def _reaper_copy(record: str) -> str:
    import hashlib

    with open(os.path.abspath(__file__), "rb") as fh:
        source = fh.read()
    path = os.path.join(record, "reaper-%s.py" % hashlib.sha256(source).hexdigest()[:16])
    try:
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode) and info.st_size == len(source):
            return path
    except OSError:
        pass
    temporary = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
                 0o600)
    try:
        view = memoryview(source)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
    os.replace(temporary, path)
    return path


def _spawn(argv: list[str], lock: int) -> None:
    """Run ``argv`` in its own session with only /dev/null and ``lock`` (as fd 3); wait.

    No pipe or descriptor of the caller reaches it, so a caller whose output is being
    read (``subprocess.run(capture_output=True)``) is never kept open by it.
    """
    actions = []
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        try:
            if fd > 2 and os.get_inheritable(fd):
                actions.append((os.POSIX_SPAWN_CLOSE, fd))
        except OSError:
            continue
    actions += [(os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0)]
    # dup2 onto 3 clears close-on-exec; a source above 3 keeps it a real dup2.
    source = fcntl.fcntl(lock, fcntl.F_DUPFD_CLOEXEC, 10)
    actions.append((os.POSIX_SPAWN_DUP2, source, 3))
    try:
        pid = os.posix_spawn(argv[0], argv, {"PATH": os.defpath}, file_actions=actions,
                             setsid=True)
    finally:
        os.close(source)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


# --- the reaper -------------------------------------------------------------------------

def _table() -> dict[int, tuple[str, int, int, str]]:
    rows = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            info = _stat(int(name))
            if info is not None:
                rows[int(name)] = info
    return rows


def _records(record: str) -> dict[tuple[int, str], list[tuple[int, str]]]:
    """``{test: [child, ...]}`` for every recorded child."""
    found: dict[tuple[int, str], list[tuple[int, str]]] = {}
    for entry in os.listdir(record):
        if not entry.endswith(".child"):
            continue
        child = _parse(entry[:-len(".child")])
        try:
            owner = _parse(os.readlink(os.path.join(record, entry)))
        except OSError:
            continue
        if child is not None and owner is not None:
            found.setdefault(owner, []).append(child)
    return found


def _tree(roots: dict[int, str], table: dict[int, tuple[str, int, int, str]]) -> dict[int, str]:
    """``roots`` and every live descendant, as ``{pid: start ticks}``."""
    found = dict(roots)
    by_parent: dict[int, list[int]] = {}
    for pid, info in table.items():
        by_parent.setdefault(info[1], []).append(pid)
    pending = list(found)
    while pending:
        for pid in by_parent.get(pending.pop(), ()):
            if pid not in found and table[pid][0] != "Z":
                found[pid] = table[pid][3]
                pending.append(pid)
    found.pop(os.getpid(), None)
    found.pop(1, None)
    return found


def _signal(targets: dict[int, str], signum: int) -> None:
    for pid, ticks in targets.items():
        info = _stat(pid)
        if info is None or info[3] != ticks:
            continue
        try:
            # A detached child leads its own group: take the whole group with it.
            if info[2] == pid:
                os.killpg(pid, signum)
            else:
                os.kill(pid, signum)
        except OSError:
            pass


def _kill_tree(children: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """SIGKILL each child that is still the same process, with its descendants.

    The tree is stopped first and re-read until it stops growing, so a helper cannot
    start a child between the read and the kill that would then escape as an orphan.
    """
    table = _table()
    roots = {pid: ticks for pid, ticks in children
             if pid in table and table[pid][3] == ticks and table[pid][0] != "Z"}
    if not roots:
        return []
    targets = _tree(roots, table)
    for _ in range(5):
        _signal(targets, signal.SIGSTOP)
        grown = _tree(targets, _table())
        if grown.keys() <= targets.keys():
            break
        targets = grown
    _signal(targets, signal.SIGKILL)
    return list(targets.items())


def reap(record: str, anchor: tuple[int, str]) -> None:
    """Kill everything recorded under ``anchor`` (an ended test) and drop its records."""
    deadline = time.monotonic() + 30.0
    while True:
        children = _records(record).get(anchor, [])
        signalled = _kill_tree(children)
        settle = min(deadline, time.monotonic() + _SETTLE_S)
        while time.monotonic() < settle and any(_live(identity) for identity in signalled):
            time.sleep(0.02)
        for child in children:
            if not _live(child):
                try:
                    os.unlink(os.path.join(record, _name(child) + ".child"))
                except OSError:
                    pass
        if time.monotonic() >= deadline or (not signalled and not _records(record).get(anchor)):
            break
    try:
        os.unlink(os.path.join(record, _name(anchor) + ".lock"))
    except OSError:
        pass


def _finish_abandoned(record: str, own: tuple[int, str]) -> None:
    """Reap the records of ended tests whose reaper is gone (killed, or never started).

    Records of an earlier boot name no process of this one: they are only removed.
    """
    import shutil

    base, current = os.path.split(record)
    for entry in os.listdir(base):
        if entry == current or not (_BOOT.match(entry) or entry == "unknown-boot"):
            continue
        path = os.path.join(base, entry)
        try:
            earlier_boot = stat.S_ISDIR(os.lstat(path).st_mode)
        except OSError:
            continue
        if earlier_boot:
            shutil.rmtree(path, ignore_errors=True)
    now = time.time()
    tests = set(_records(record))
    for entry in os.listdir(record):
        if entry.endswith(".lock"):
            identity = _parse(entry[:-len(".lock")])
            if identity is not None:
                tests.add(identity)
        elif entry.startswith("reaper-") and entry != os.path.basename(__file__):
            try:
                if now - os.lstat(os.path.join(record, entry)).st_mtime > _COPY_AGE_S:
                    os.unlink(os.path.join(record, entry))
            except OSError:
                pass
    for test in sorted(tests - {own}):
        if _live(test):
            continue
        lock = os.open(os.path.join(record, _name(test) + ".lock"),
                       os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            reap(record, test)
        finally:
            os.close(lock)


def _wait_for_end(anchor: tuple[int, str]) -> None:
    try:
        pidfd = os.pidfd_open(anchor[0])
    except (AttributeError, OSError):
        pidfd = None
    try:
        if not _live(anchor):
            return
        if pidfd is not None:
            poller = select.poll()
            poller.register(pidfd, select.POLLIN)
            while not poller.poll():
                pass
            return
        while _live(anchor):
            time.sleep(0.5)
    finally:
        if pidfd is not None:
            os.close(pidfd)


def _main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("--detach", "--reap"):
        print("usage: detached_lifetime.py --detach|--reap RECORD TEST", file=sys.stderr)
        return 2
    mode, record, name = argv
    anchor = _parse(name)
    if anchor is None:
        return 2
    os.chdir("/")
    if mode == "--detach":
        # fd 3 (the lock) is inheritable here; the reaper keeps it.
        os.posix_spawn(_python(), [_python(), "-I", "-S", os.path.abspath(__file__), "--reap",
                                   record, name], {"PATH": os.defpath}, setsid=True)
        return 0
    try:
        _finish_abandoned(record, anchor)
    except OSError:
        pass
    _wait_for_end(anchor)
    reap(record, anchor)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
