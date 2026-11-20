#!/usr/bin/env python3
"""Fail-closed cleanup for merged, pushed, inactive linked worktrees."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import stat
import unicodedata
import importlib.util
import signal
import uuid
import threading
import resource


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test
except ImportError:  # a partial tree without it (e.g. a fixture release): no record
    def end_with_test(child):
        return child
from dispatch_contract import (
    DispatchContractError,
    dispatch_state_root,
    parse_registry_metadata,
    resolve_agent_home as _resolve_agent_home,
    resolve_dispatch_state_root,
    validate_attempt_metadata,
    git_optional_locks_env,
)

OPEN_STATES = {"open", "running"}
MAX_AUDIT_BYTES = 1024 * 1024
KEEP_AUDIT_BYTES = 512 * 1024
OBSERVATION_TIMEOUT = 60


# Plain mutable classes, not @dataclass: this module is hyphenated (loaded
# elsewhere by file-spec without guaranteed sys.modules registration), and a
# module-level @dataclass under `from __future__ import annotations` breaks
# CPython's string-annotation resolution in that load mode (see D0 in
# claude-session-supervisor.py / dispatch_completion_join.test.py). Both
# classes here are mutated field-by-field after construction, so a NamedTuple
# is not a drop-in replacement.
class WorktreeEntry:
    def __init__(self, path: Path, head: str = "", branch: str = "", locked: bool = False):
        self.path = path
        self.head = head
        self.branch = branch
        self.locked = locked


class Verdict:
    def __init__(
        self,
        path: Path,
        primary: Path,
        integration_ref: str,
        eligible: bool = False,
        reasons: list[str] | None = None,
        head: str = "",
        branch: str = "",
        upstream: str = "none",
        integration_commit: str = "",
        stale_rows: int = 0,
        active_pids: list[int] | None = None,
        process_scan: str = "unknown",
        repository_mode: str = "remote",
    ):
        self.path = path
        self.primary = primary
        self.integration_ref = integration_ref
        self.eligible = eligible
        self.reasons = reasons if reasons is not None else []
        self.head = head
        self.branch = branch
        self.upstream = upstream
        self.integration_commit = integration_commit
        self.stale_rows = stale_rows
        self.active_pids = active_pids if active_pids is not None else []
        self.process_scan = process_scan
        self.repository_mode = repository_mode
        self.unreadable_pids = 0
        self.ignored_files = 0
        self.ignored_bytes = 0
        self.ignored_largest = []
        self.ignored_truncated = False
        self.local_integration_commit = ""
        self.integration_refs = {}
        self.integration_symbols = {}


_OBSERVATION_LIFECYCLE = None


class UnsettledObservation(RuntimeError):
    """The pass must retain diagnostics and perform no further mutations."""


def observation_lifecycle():
    global _OBSERVATION_LIFECYCLE
    if _OBSERVATION_LIFECYCLE is None:
        spec = importlib.util.spec_from_file_location(
            "cleanup_observation_lifecycle", ROOT / "utilities/verification-background-lease.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _OBSERVATION_LIFECYCLE = module
    return _OBSERVATION_LIFECYCLE


def observe_command(argv):
    """Private child boundary: the existing subreaper owns only this probe.

    Its descendant helper deliberately includes all immediate children. Running
    it in the importing caller would also claim unrelated caller children.
    """
    lifecycle = observation_lifecycle()
    if not lifecycle.become_subreaper():
        raise RuntimeError("owned observation lifecycle unavailable")
    scope = uuid.uuid4().hex
    status = 1
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    observed = {}
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                env={**os.environ, lifecycle.SCOPE_ENV: scope},
                                timeout=OBSERVATION_TIMEOUT, check=False)
        status = result.returncode
    except subprocess.TimeoutExpired:
        print("observation timed out: " + " ".join(argv), file=sys.stderr)
        status = 124
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else 143
    finally:
        # In this single-threaded private subreaper, every surviving descendant
        # of the waited command is now an immediate child (also after setsid or
        # closing output fds). The common no-child case needs no host-wide scan.
        try:
            children = (Path("/proc/self/task") / str(os.getpid()) / "children").read_text().split()
        except OSError:
            children = None
        if children or children is None:
            lifecycle.end_scope(scope, observed=observed)
            lifecycle._remember_scope(lifecycle.process_snapshot(), scope, observed)
            live, unknown = lifecycle._owned_process_state(observed)
            if live or unknown or children is None:
                print(f"observation descendants unresolved: live={sorted(live)} unknown={unknown}", file=sys.stderr)
                status = 125
            lifecycle.reap_zombies()
    return status


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # Fork the small observer after loading the shared lifecycle once. This
    # gives it a private child boundary without interpreter/registry startup
    # on every Git read. No observer survives its command's settled lifetime.
    observation_lifecycle()
    private = Path(tempfile.mkdtemp(prefix="cleanup-observation-"))
    process = None
    pid = None
    handlers = {}
    settled = False
    child_env = {**(os.environ if env is None else env), **git_optional_locks_env()}
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    try:
        with (private / "stdout").open("w+b") as output, (private / "stderr").open("w+b") as errors:
            if hasattr(os, "fork") and threading.active_count() == 1:
                pid = os.fork()
                if pid == 0:
                    try:
                        os.setsid()
                        with open(os.devnull, "rb") as input_null:
                            os.dup2(input_null.fileno(), 0)
                        os.dup2(output.fileno(), 1)
                        os.dup2(errors.fileno(), 2)
                        # Do not retain caller locks or registry descriptors.
                        limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
                        os.closerange(3, int(limit if limit != resource.RLIM_INFINITY else 1048576))
                        sys.stdout = os.fdopen(os.dup(1), "w", buffering=1)
                        sys.stderr = os.fdopen(os.dup(2), "w", buffering=1)
                        if cwd is not None:
                            os.chdir(cwd)
                        os.environ.clear()
                        os.environ.update(child_env)
                        status = observe_command(argv)
                    except BaseException as exc:
                        print(f"observation interrupted: {exc}", file=sys.stderr)
                        status = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 125
                    os._exit(status if status >= 0 else 128 - status)
            else:
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "_observe", str(OBSERVATION_TIMEOUT),
                     json.dumps(argv)], cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=errors,
                    start_new_session=True, env=child_env)
                pid = process.pid
            end_with_test(pid)
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGHUP):
                    handlers[signum] = signal.signal(signum, interrupted)
            waited = False
            try:
                if process is None:
                    _pid, status = os.waitpid(pid, 0)
                    code = os.waitstatus_to_exitcode(status)
                else:
                    code = process.wait()
                waited = True
                settled = code != 125 and code >= 0
            finally:
                if not waited:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        if process is None:
                            _pid, status = os.waitpid(pid, 0)
                            settled = os.waitstatus_to_exitcode(status) != 125
                        else:
                            settled = process.wait() != 125
                    except ChildProcessError:
                        pass
            output.seek(0)
            errors.seek(0)
            result = subprocess.CompletedProcess(argv, code,
                output.read().decode("utf-8", errors="replace"), errors.read().decode("utf-8", errors="replace"))
    finally:
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
        if settled:
            shutil.rmtree(private)
        else:
            print(f"observation diagnostics retained: {private}", file=sys.stderr)
    if not settled:
        raise UnsettledObservation(f"observation descendants unresolved; diagnostics={private}")
    if result.returncode in {69, 70, 124, 125, 126, 137, 143}:
        raise RuntimeError("observation unavailable: " + result.stderr.strip())
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise RuntimeError(f"{' '.join(argv)}: {detail}")
    return result


def normalize(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def lexical(path: str | Path) -> Path:
    """Compare proc/registry paths without following NAS directory symlinks."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def known_exists(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


class CleanupPass:
    """Stable observations for one check/apply call, never removal authority."""

    def __init__(self, repo: Path, jobs: Path):
        self.entries = worktree_entries(repo)
        if not self.entries:
            raise RuntimeError("git reported no registered worktrees")
        self.primary = self.entries[0].path
        self.common = common_git_dir(repo)
        self.rows = registry_rows(jobs)
        self.processes = process_snapshot()
        self.remote_defaults = {}
        self.remote_commits = {}
        self.remote_urls = {}
        self.graph_results = {}

    def graph(self, *args):
        # Callers supply exact object IDs, never mutable ref names. Rechecking
        # the target and integration refs remains mandatory before removal.
        if args not in self.graph_results:
            self.graph_results[args] = run(["git", "-C", str(self.primary), *args], check=False)
        return self.graph_results[args]

    def default_branch(self, remote):
        if remote not in self.remote_defaults:
            try:
                self.remote_defaults[remote] = remote_default_branch(self.primary, remote)
            except RuntimeError as exc:
                self.remote_defaults[remote] = exc
        value = self.remote_defaults[remote]
        if isinstance(value, Exception):
            raise value
        return value

    def fetched(self, remote, branch):
        key = (remote, branch)
        if key not in self.remote_commits:
            # Failed evidence stays failed for this pass, too.
            try:
                self.remote_commits[key] = fetch_remote_commit(self.primary, remote, branch)
                self.remote_urls[remote] = run(
                    ["git", "-C", str(self.primary), "remote", "get-url", "--all", remote]).stdout
            except RuntimeError as exc:
                self.remote_commits[key] = exc
        value = self.remote_commits[key]
        if isinstance(value, Exception):
            raise value
        return value

    def before_remove(self, target, jobs, integration_ref, repository_mode, previous):
        # Keep the pass inventory but replace this target's mutable entry with
        # direct current HEAD/branch/registration/lock evidence. No full census
        # is needed to recheck one linked checkout.
        identity = target.stat()
        if (identity.st_dev, identity.st_ino) != previous.path_identity:
            raise RuntimeError("worktree directory changed")
        for remote, expected in self.remote_urls.items():
            if run(["git", "-C", str(self.primary), "remote", "get-url", "--all", remote]).stdout != expected:
                raise RuntimeError("integration remote changed")
        gitdir = git_path(target, "--git-dir")
        if common_git_dir(target) != self.common:
            raise RuntimeError("worktree common directory changed")
        backlink = Path((gitdir / "gitdir").read_text().strip())
        if lexical(backlink) != lexical(target / ".git"):
            raise RuntimeError("worktree registration changed")
        registration = self.registration_identity(target, gitdir)
        head = run(["git", "-C", str(target), "rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
        branch_result = run(["git", "-C", str(target), "symbolic-ref", "-q", "HEAD"], check=False)
        if branch_result.returncode not in (0, 1):
            raise RuntimeError("cannot verify current worktree branch")
        branch = branch_result.stdout.strip()
        fresh = WorktreeEntry(target, head, branch, os.path.lexists(gitdir / "locked"))
        entries = [fresh if entry.path == target else entry for entry in self.entries]
        rows = registry_rows(jobs)
        checked = evaluate(target, jobs, integration_ref, repository_mode,
                           context=self, entries=entries, rows=rows, fresh_processes=True)
        old_rows = tuple(fields for fields, _meta in self.rows if lexical(fields[3]) == target)
        new_rows = tuple(fields for fields, _meta in rows if lexical(fields[3]) == target)
        if old_rows != new_rows:
            checked.reasons.append("registry-changed")
        for field in ("head", "branch", "integration_ref", "integration_commit", "upstream",
                      "local_integration_commit"):
            if getattr(previous, field) != getattr(checked, field):
                checked.reasons.append("worktree-or-integration-changed")
                break
        # evaluate includes slow graph, registry and proc observations. Its
        # input entry is not current authority after those observations.
        for ref, expected in checked.integration_refs.items():
            if expected:
                result = run(["git", "-C", str(self.primary), "rev-parse", "--verify", ref + "^{commit}"],
                             check=False)
                same = result.returncode == 0 and result.stdout.strip() == expected
            else:
                result = run(["git", "-C", str(self.primary), "show-ref", "--verify", "--quiet", ref],
                             check=False)
                same = result.returncode == 1  # Positive absence, not an unreadable ref.
            if not same:
                checked.reasons.append("worktree-or-integration-changed")
        for ref, expected in checked.integration_symbols.items():
            result = run(["git", "-C", str(self.primary), "rev-parse", "--symbolic-full-name", ref],
                         check=False)
            if result.returncode or result.stdout.strip() != expected:
                checked.reasons.append("worktree-or-integration-changed")
        for remote, expected in self.remote_urls.items():
            if run(["git", "-C", str(self.primary), "remote", "get-url", "--all", remote]).stdout != expected:
                checked.reasons.append("worktree-or-integration-changed")
        head = run(["git", "-C", str(target), "rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
        branch_result = run(["git", "-C", str(target), "symbolic-ref", "-q", "HEAD"], check=False)
        if branch_result.returncode not in (0, 1):
            raise RuntimeError("cannot verify current worktree branch")
        branch = branch_result.stdout.strip().removeprefix("refs/heads/") or "detached"
        if (head, branch) != (checked.head, checked.branch):
            checked.reasons.append("worktree-or-integration-changed")
        checked.head, checked.branch = head, branch
        if self.registration_identity(target, gitdir) != registration:
            checked.reasons.append("worktree-registration-changed")
        latest_rows = registry_rows(jobs)
        if tuple(fields for fields, _meta in latest_rows if lexical(fields[3]) == target) != new_rows:
            checked.reasons.append("registry-changed")
        if any(known_exists(path) for path in (
                gitdir / "locked", gitdir / "index.lock", gitdir / "HEAD.lock",
                self.common / "packed-refs.lock", self.common / ("refs/heads/" + branch + ".lock"))):
            checked.reasons.append("locked")
        checked.eligible = not checked.reasons
        return checked

    @staticmethod
    def registration_identity(target, gitdir):
        """Exact linked-checkout registration, without another worktree census."""
        paths = (target, gitdir, target / ".git", gitdir / "gitdir", gitdir / "commondir")
        identity = []
        for path in paths:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError("worktree registration symlink")
            identity.append((info.st_dev, info.st_ino, info.st_mode,
                             path.read_bytes() if stat.S_ISREG(info.st_mode) else None))
        return tuple(identity)


def resolve_agent_home() -> Path:
    return _resolve_agent_home()


def worktree_entries(repo: Path) -> list[WorktreeEntry]:
    output = run(["git", "-C", str(repo), "worktree", "list", "--porcelain"]).stdout
    entries: list[WorktreeEntry] = []
    current: WorktreeEntry | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            if current is not None:
                entries.append(current)
            current = WorktreeEntry(normalize(line[len("worktree ") :]))
        elif current is not None and line.startswith("HEAD "):
            current.head = line[len("HEAD ") :]
        elif current is not None and line.startswith("branch "):
            current.branch = line[len("branch ") :]
        elif current is not None and line.startswith("locked"):
            current.locked = True
    if current is not None:
        entries.append(current)
    return entries


def common_git_dir(repo: Path) -> Path:
    return git_path(repo, "--git-common-dir")


def git_path(repo: Path, field: str) -> Path:
    value = run(["git", "-C", str(repo), "rev-parse", field]).stdout.strip()
    path = Path(value)
    if not path.is_absolute():
        path = repo / path
    return normalize(path)


def default_integration_ref(primary: Path) -> str:
    symbolic = run(
        ["git", "-C", str(primary), "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        check=False,
    )
    if symbolic.returncode == 0 and symbolic.stdout.strip().startswith("origin/"):
        candidate = symbolic.stdout.strip().split("/", 1)[1]
        if run(
            ["git", "-C", str(primary), "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
            check=False,
        ).returncode == 0:
            return candidate
    for candidate in ("main", "master", "develop", "trunk"):
        if run(
            ["git", "-C", str(primary), "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
            check=False,
        ).returncode == 0:
            return candidate
    raise RuntimeError("no local integration branch; pass --integration-ref")


def remote_ref_identity(primary: Path, full_ref: str) -> tuple[str, str] | None:
    prefix = "refs/remotes/"
    if not full_ref.startswith(prefix):
        return None
    remote_names = run(["git", "-C", str(primary), "remote"], check=False)
    if remote_names.returncode != 0:
        return None
    tail = full_ref[len(prefix) :]
    matches = [
        name for name in remote_names.stdout.splitlines()
        if name and tail.startswith(f"{name}/")
    ]
    if not matches:
        return None
    remote = max(matches, key=len)
    branch = tail[len(remote) + 1 :]
    if not branch:
        return None
    return remote, branch


def remote_default_branch(primary: Path, remote: str = "origin") -> str:
    result = run(
        ["git", "-C", str(primary), "ls-remote", "--symref", remote, "HEAD"],
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "remote query failed").strip()
        raise RuntimeError(f"cannot query {remote} default branch: {detail}")
    for line in result.stdout.splitlines():
        fields = line.split("\t", 1)
        if (
            len(fields) == 2
            and fields[1] == "HEAD"
            and fields[0].startswith("ref: refs/heads/")
        ):
            branch = fields[0][len("ref: refs/heads/") :]
            if branch:
                return branch
    raise RuntimeError(f"{remote} does not advertise a default branch")


def fetch_remote_commit(primary: Path, remote: str, branch: str) -> str:
    tracking_ref = f"refs/remotes/{remote}/{branch}"
    refspec = f"+refs/heads/{branch}:{tracking_ref}"
    fetched = run(
        ["git", "-C", str(primary), "fetch", "--no-tags", remote, refspec],
        check=False,
    )
    if fetched.returncode != 0:
        detail = (fetched.stderr or fetched.stdout or "fetch failed").strip()
        raise RuntimeError(f"cannot fetch {remote}/{branch}: {detail}")
    resolved = run(
        ["git", "-C", str(primary), "rev-parse", "--verify", f"{tracking_ref}^{{commit}}"],
        check=False,
    )
    if resolved.returncode != 0 or not resolved.stdout.strip():
        detail = (resolved.stderr or resolved.stdout or "fetched commit is unavailable").strip()
        raise RuntimeError(f"cannot resolve fetched {remote}/{branch}: {detail}")
    return resolved.stdout.strip()


def git_operation(worktree: Path) -> str:
    gitdir = git_path(worktree, "--git-dir")
    if known_exists(gitdir / "MERGE_HEAD"):
        return "merge"
    if known_exists(gitdir / "rebase-merge") or known_exists(gitdir / "rebase-apply"):
        return "rebase"
    if known_exists(gitdir / "CHERRY_PICK_HEAD"):
        return "cherry-pick"
    return "none"


def registry_rows(jobs: Path):
    """Reuse the common generation-checked reader; do not invent another log reader."""
    from dispatch_registry_cache import registry_view
    try:
        if jobs.is_symlink():
            raise RuntimeError("registry symlink is not cleanup evidence")
        view = registry_view(jobs)
    except FileNotFoundError:
        return ()
    if view.completeness_reason:
        raise RuntimeError("registry observation incomplete: " + view.completeness_reason)
    return tuple((record.fields, record.completion_metadata()) for record in view.records)


def parse_registry(jobs: Path, target: Path, *, rows=None) -> tuple[int, list[int]]:
    stale = 0
    active: list[int] = []
    for fields, meta in registry_rows(jobs) if rows is None else rows:
        if fields[1] not in OPEN_STATES:
            continue
        if lexical(fields[3]) != target:
            continue
        pid = meta.get("pid", "")
        if pid.isdigit() and pid_is_live(int(pid), meta.get("pid_start")):
            active.append(int(pid))
        else:
            stale += 1
    return stale, sorted(set(active))


def registry_duty(jobs, target, rows):
    """A dead PID is not release of a route, paused result or retained answer.
    Two kinds of leftover no longer hold a worktree forever: an owner input
    request still unfinished once its route is closed (no replacement owner can
    take it; the text stays in the state folder), and the zero-byte tombstone
    the 2026-09/10 pruner left where a finished delivery's record had been."""
    from dispatch_attempt_policy import readable_result, terminal_conflict_pending
    from worker_home_retention import _route_evidence, _delivery_state
    import dispatch_owner_input as owner_input
    for fields, meta in rows:
        if lexical(fields[3]) != target:
            continue
        if fields[1] not in OPEN_STATES | {"done", "killed", "cancelled"}:
            return True
        if terminal_conflict_pending(meta):
            return True
        route_state, _age, _cause = _route_evidence(jobs, meta)
        if route_state in {"open", "pending", "unknown"}:
            return True
        if readable_result(meta) and route_state != "closed":
            return True
        if _delivery_state(dispatch_state_root(jobs), meta,
                           legacy_empty_tombstone=True)[0] in {"open", "pending", "unknown"}:
            return True
        if meta.get("attempt_id"):
            path = owner_input._path(jobs, meta["attempt_id"])
            try:
                if path.is_symlink():
                    return True
                value = json.loads(path.read_text())
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                return True
            if (not isinstance(value, dict) or value.get("schema_version") != 1
                    or value.get("attempt_id") != meta["attempt_id"]):
                return True
            requests = value.get("requests", [])
            if not isinstance(requests, list) or any(not isinstance(item, dict) for item in requests):
                return True
            if route_state != "closed" and any(
                    item.get("state") != "turn-completed" for item in requests):
                return True
    return False


def pid_is_live(pid: int, pid_start: str | None = None) -> bool:
    """(pid, start-time) identity when the registry recorded `pid_start`: a live
    pid whose /proc starttime differs is a host PID reuse, not this attempt.
    Bare-pid probing survives only for legacy rows that never captured it."""
    if pid_start is not None:
        current = read_proc_start(pid)
        return current == pid_start or (current is None and Path(f"/proc/{pid}").exists())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_proc_start(pid: int) -> str | None:
    """/proc/<pid>/stat field 22 (starttime) as str, else None. comm (field 2) is
    parenthesized and may contain spaces/parens, so split after the last ')';
    A zombie leader is terminal only when no other threads remain."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    try:
        rest = data[data.rindex(")") + 1:].split()
        threads = int(rest[17])
        if threads < 0:
            raise ValueError("negative num_threads")
        if rest[0] in {"Z", "X"} and threads <= 1:
            return None
        return rest[19]
    except (ValueError, IndexError):
        return None


def proc_is_gone(entry):
    """Only positive disappearance/zombie evidence clears a raced cwd read."""
    try:
        raw = (entry / "stat").read_text()
        fields = raw[raw.rindex(")") + 1:].split()
        threads = int(fields[17])
        if threads < 0:
            raise ValueError("negative num_threads")
        return fields[0] in {"Z", "X"} and threads <= 1
    except FileNotFoundError:
        return True
    except (OSError, ValueError, IndexError):
        return False


def same_credentials(entry) -> bool:
    """True when /proc/<pid>/status shows our uid and gid as real, effective and
    saved. A read failure or odd status is False: the caller then follows the
    ordinary gone-or-unknown rule."""
    try:
        text = (entry / "status").read_text()
    except OSError:
        return False
    found = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("Uid", "Gid"):
            found[key] = rest.split()[:3]
    expected = {"Uid": str(os.geteuid()), "Gid": str(os.getegid())}
    return all(found.get(key) == [value] * 3 for key, value in expected.items())


def process_snapshot():
    """(kind, rows, unreadable): `unreadable` counts same-user processes whose
    cwd the kernel will not show us although their uid and gid (real, effective,
    saved) equal ours: non-dumpable login-session processes and the user service
    manager. Only that refusal is counted instead of being "unknown"; a process
    with another gid (sg/newgrp) and every other failure still is."""
    proc = Path("/proc")
    if proc.is_dir():
        rows = []
        unknown = False
        unreadable = 0
        for entry in tuple(proc.iterdir()):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                if entry.stat().st_uid != os.geteuid():
                    continue
                before = read_proc_start(pid)
                if before is None:
                    # A dead leader can still have live threads. Missing cwd
                    # alone never proves that the thread group has ended.
                    if proc_is_gone(entry):
                        continue
                    unknown = True
                    continue
                try:
                    raw_cwd = os.readlink(entry / "cwd")
                except PermissionError:
                    # Only a process that holds exactly our uid and gid and is
                    # still refused is non-dumpable. A different gid (sg/newgrp)
                    # is refused the same way but may sit in the worktree.
                    if same_credentials(entry):
                        unreadable += 1
                    elif not proc_is_gone(entry):
                        unknown = True
                    continue
                after = read_proc_start(pid)
                if after is None and proc_is_gone(entry):
                    continue
                if before != after:
                    unknown = True
                    continue
                # A deleted cwd still has a kernel-reported lexical path. It
                # keeps that target, rather than blocking unrelated targets.
                if raw_cwd.endswith(" (deleted)"):
                    raw_cwd = raw_cwd[:-len(" (deleted)")]
                rows.append((pid, before, lexical(raw_cwd)))
            except FileNotFoundError:
                if not proc_is_gone(entry):
                    unknown = True
                continue
            except (OSError, ValueError, IndexError):
                unknown = True
        return ("procfs-unknown" if unknown else "procfs"), tuple(rows), unreadable
    return "unavailable", (), 0


def process_cwds(target: Path, *, snapshot=None) -> tuple[str, list[int], int]:
    kind, rows, unreadable = process_snapshot() if snapshot is None else snapshot
    if kind != "unavailable":
        return kind, sorted(pid for pid, _start, cwd in rows if cwd == target or target in cwd.parents), unreadable

    lsof = shutil.which("lsof")
    if lsof:
        matches = []
        result = run([lsof, "-t", "+D", str(target)], check=False)
        if result.returncode not in (0, 1):
            return "lsof-error", [], 0
        for value in result.stdout.splitlines():
            if value.isdigit() and int(value) != os.getpid():
                matches.append(int(value))
        return "lsof", sorted(set(matches)), 0
    return "unavailable", [], 0


# Rebuildable caches only. A short fixed list on purpose: names such as
# build/, dist/, node_modules/ or .venv/ can hold real output, so they are not
# here, and nothing selects or extends the list. A cache name alone is not
# enough: __pycache__/ must hold only *.pyc files, and the tool caches must
# carry the CACHEDIR.TAG that pytest, mypy and ruff all write.
PYCACHE_DIR = "__pycache__"
TAGGED_CACHE_DIRS = frozenset({".pytest_cache", ".mypy_cache", ".ruff_cache"})
MEASURE_FILE_LIMIT = 100_000
LARGEST_SHOWN = 5


def parse_status(raw: str) -> tuple[bool, list[str]]:
    """(dirty, ignored paths) from `status --porcelain=v1 -z --ignored=matching`."""
    dirty = False
    ignored: list[str] = []
    tokens = raw.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        code, path = token[:2], token[3:]
        if code == "!!":
            ignored.append(path)
            continue
        dirty = True
        if "R" in code or "C" in code:
            index += 1  # a rename/copy is followed by its origin path
    return dirty, ignored


def _has_cache_tag(directory: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(directory / "CACHEDIR.TAG").st_mode)
    except OSError:
        return False


def _only_pyc_files(directory: Path) -> bool:
    """One level only: every entry is a plain *.pyc file."""
    try:
        with os.scandir(directory) as entries:
            return all(item.name.endswith(".pyc") and item.is_file(follow_symlinks=False)
                       for item in entries)
    except OSError:
        return False


def hidden_tracked_files(target: Path) -> bool:
    """True when a tracked file that still exists in the worktree is marked
    skip-worktree or assume-unchanged (`ls-files -v`: `S`, or a lowercase tag).
    A marked file that is absent (sparse checkout) holds no edits."""
    listing = run(["git", "-C", str(target), "ls-files", "-v", "-z"],
                  env={**os.environ, **git_optional_locks_env()}).stdout
    for item in listing.split("\0"):
        if item[:1] and (item[0] == "S" or item[0].islower()) and os.path.lexists(target / item[2:]):
            return True
    return False


def is_rebuildable_cache(path: str, target: Path) -> bool:
    """`path` is one `!!` entry of `status --ignored=matching`, relative to the
    worktree `target`. It is a cache when it, or a directory above it, is a
    tool cache folder holding CACHEDIR.TAG (a tool's own `.gitignore` of `*`
    makes git list the folder's contents instead of the folder), when it is a
    `__pycache__/` of only *.pyc files, or when it is a single *.pyc file."""
    parts = path.rstrip("/").split("/")
    is_dir = path.endswith("/")
    for depth in range(len(parts) if is_dir else len(parts) - 1):
        if parts[depth] in TAGGED_CACHE_DIRS and _has_cache_tag(target.joinpath(*parts[:depth + 1])):
            return True
    if is_dir:
        return parts[-1] == PYCACHE_DIR and _only_pyc_files(target.joinpath(*parts))
    return path.endswith(".pyc")


def measure_ignored(target: Path, paths: list[str]) -> tuple[int, int, list[tuple[int, str]], bool]:
    """(files, bytes, largest entries, truncated) for blocking ignored entries.
    Only called once they already block, never on the clean path. Symlinks are
    counted as themselves and never followed."""
    files = total = 0
    truncated = False
    sizes: list[tuple[int, str]] = []
    for rel in paths:
        entry_bytes = 0
        full = target / rel
        try:
            if rel.endswith("/"):
                for base, dirs, names in os.walk(full, followlinks=False):
                    for name in names:
                        if files >= MEASURE_FILE_LIMIT:
                            truncated = True
                            break
                        files += 1
                        try:
                            entry_bytes += os.lstat(os.path.join(base, name)).st_size
                        except OSError:
                            pass
                    if truncated:
                        break
            else:
                files += 1
                entry_bytes = os.lstat(full).st_size
        except OSError:
            pass
        total += entry_bytes
        sizes.append((entry_bytes, rel))
        if truncated:
            break
    sizes.sort(key=lambda item: (-item[0], item[1]))
    return files, total, sizes[:LARGEST_SHOWN], truncated


def owner_is_known(target: Path, common: Path) -> bool:
    """The worktree is ours: owned by this user, or owned like this repository's
    shared git directory which this user can write. Network mounts that show
    every file under one foreign uid pass through the second form only; a
    checkout owned by anyone else stays unknown."""
    info = target.stat()
    if info.st_uid == os.geteuid():
        return True
    if os.geteuid() == 0:
        return False  # root can write anywhere: that proves nothing about whose checkout it is
    try:
        return info.st_uid == common.stat().st_uid and os.access(common, os.W_OK, effective_ids=True)
    except OSError:
        return False


def evaluate(
    target: Path,
    jobs: Path,
    integration_ref: str | None,
    repository_mode: str = "remote",
    *, context: CleanupPass | None = None, entries=None, rows=None, fresh_processes=False,
) -> Verdict:
    entries = entries if entries is not None else (context.entries if context else worktree_entries(target))
    if not entries:
        raise RuntimeError("git reported no registered worktrees")
    primary = entries[0].path
    if repository_mode == "local-only":
        chosen_ref = integration_ref or default_integration_ref(primary)
    else:
        chosen_ref = integration_ref or "origin/HEAD"
    verdict = Verdict(
        path=target,
        primary=primary,
        integration_ref=chosen_ref,
        repository_mode=repository_mode,
    )

    by_path = {entry.path: entry for entry in entries}
    entry = by_path.get(target)
    if entry is None:
        verdict.reasons.append("not-registered-worktree")
        return verdict
    verdict.head = entry.head
    branch_prefix = "refs/heads/"
    verdict.branch = (
        entry.branch[len(branch_prefix) :]
        if entry.branch.startswith(branch_prefix)
        else entry.branch
    ) or "detached"

    if target == primary:
        verdict.reasons.append("primary-worktree")
    if lexical(target) != normalize(target) or target.is_symlink():
        verdict.reasons.append("worktree-path-ambiguous")
    worktree_gitdir = git_path(target, "--git-dir")
    if entry.locked or any(known_exists(worktree_gitdir / name)
                           for name in ("locked", "index.lock", "HEAD.lock")):
        verdict.reasons.append("locked")
    common = context.common if context else common_git_dir(target)
    if any(known_exists(path) for path in (common / "packed-refs.lock",
                                         common / (entry.branch + ".lock"))):
        verdict.reasons.append("locked")
    if not target.is_dir():
        verdict.reasons.append("worktree-path-missing")
        return verdict
    if not owner_is_known(target, common):
        verdict.reasons.append("worktree-owner-unknown")
    identity = target.stat()
    verdict.path_identity = (identity.st_dev, identity.st_ino)

    rows = rows if rows is not None else (context.rows if context else registry_rows(jobs))
    verdict.stale_rows, registered_pids = parse_registry(jobs, target, rows=rows)
    scan_kind, cwd_pids, verdict.unreadable_pids = (
        process_cwds(target) if context is None else process_cwds(target, snapshot=context.processes))
    verdict.process_scan = scan_kind
    verdict.active_pids = sorted(set(registered_pids + cwd_pids))
    if verdict.active_pids:
        verdict.reasons.append("active-process")
    if scan_kind in {"unavailable", "lsof-error", "procfs-unknown"}:
        verdict.reasons.append("process-scan-unavailable")
    if registry_duty(jobs, target, rows):
        verdict.reasons.append("retained-work")
    if verdict.reasons:
        return verdict

    # `git worktree remove` deletes ignored files with the folder, so they are
    # part of "clean": anything ignored that is not a rebuildable cache blocks.
    # `matching` reports a wholly ignored folder as one entry, so the check stays
    # cheap however many files it holds.
    status = run(
        ["git", "-C", str(target), "status", "--porcelain=v1", "-z", "--untracked-files=all",
         "--ignored=matching"],
        env={**os.environ, **git_optional_locks_env()},
    ).stdout
    dirty, ignored = parse_status(status)
    # `status` never lists a tracked file marked skip-worktree/assume-unchanged,
    # yet `git worktree remove` deletes it with its edits: that is dirt too.
    dirty = dirty or hidden_tracked_files(target)
    if dirty:
        verdict.reasons.append("dirty")
    blocking = [path for path in ignored if not is_rebuildable_cache(path, target)]
    if blocking:
        verdict.reasons.append("ignored-files")
        (verdict.ignored_files, verdict.ignored_bytes,
         verdict.ignored_largest, verdict.ignored_truncated) = measure_ignored(target, blocking)

    operation = git_operation(target)
    if operation != "none":
        verdict.reasons.append(f"git-operation-{operation}")

    def graph(*args):
        return context.graph(*args) if context else run(["git", "-C", str(primary), *args], check=False)

    if repository_mode == "local-only":
        ref_check = run(
            ["git", "-C", str(primary), "rev-parse", "--verify", f"{chosen_ref}^{{commit}}"],
            check=False,
        )
        verdict.upstream = "local-only"
        if ref_check.returncode != 0:
            verdict.reasons.append("integration-ref-missing")
        else:
            verdict.integration_commit = ref_check.stdout.strip()
            verdict.integration_refs[chosen_ref] = verdict.integration_commit
            merged = graph("merge-base", "--is-ancestor", entry.head, verdict.integration_commit)
            if merged.returncode == 1:
                verdict.reasons.append("unmerged")
            elif merged.returncode != 0:
                raise RuntimeError("cannot verify worktree HEAD against local integration ref")
    else:
        remote: str | None = None
        branch: str | None = None
        local_ref: str | None = None
        require_local_inclusion = False
        display_ref = chosen_ref

        if integration_ref is None:
            remote = "origin"
            branch = context.default_branch(remote) if context else remote_default_branch(primary, remote)
            local_ref = f"refs/heads/{branch}"
            display_ref = f"{remote}/{branch}"
        else:
            symbolic_ref = run(
                ["git", "-C", str(primary), "rev-parse", "--symbolic-full-name", integration_ref],
                check=False,
            )
            full_ref = symbolic_ref.stdout.strip()
            if symbolic_ref.returncode != 0 or not full_ref:
                verdict.reasons.append("integration-ref-missing")
            else:
                verdict.integration_symbols[integration_ref] = full_ref
                identity = remote_ref_identity(primary, full_ref)
                if identity is not None:
                    remote, branch = identity
                    local_ref = f"refs/heads/{branch}"
                elif full_ref.startswith("refs/heads/"):
                    local_ref = full_ref
                    require_local_inclusion = True
                    local_branch = full_ref[len("refs/heads/") :]
                    upstream_result = run(
                        [
                            "git", "-C", str(primary), "rev-parse", "--symbolic-full-name",
                            f"{local_branch}@{{upstream}}",
                        ],
                        check=False,
                    )
                    upstream_ref = upstream_result.stdout.strip()
                    if upstream_result.returncode == 0:
                        verdict.integration_symbols[f"{local_branch}@{{upstream}}"] = upstream_ref
                    identity = remote_ref_identity(primary, upstream_ref)
                    if upstream_result.returncode != 0 or identity is None:
                        verdict.reasons.append("no-upstream-configured")
                    else:
                        remote, branch = identity
                else:
                    verdict.reasons.append("no-upstream-configured")

        if remote is not None and branch is not None:
            remote_commit = context.fetched(remote, branch) if context else fetch_remote_commit(primary, remote, branch)
            if context:
                current_tracking = run(["git", "-C", str(primary), "rev-parse", "--verify",
                                        f"refs/remotes/{remote}/{branch}^{{commit}}"], check=False)
                if current_tracking.returncode or current_tracking.stdout.strip() != remote_commit:
                    verdict.reasons.append("integration-ref-changed")
            verdict.integration_ref = display_ref
            verdict.integration_commit = remote_commit
            verdict.integration_refs[f"refs/remotes/{remote}/{branch}"] = remote_commit
            verdict.upstream = f"{remote}/{branch}"

            merged = graph("merge-base", "--is-ancestor", entry.head, remote_commit)
            if merged.returncode == 1:
                verdict.reasons.append("unmerged")
            elif merged.returncode != 0:
                raise RuntimeError("cannot verify worktree HEAD against fetched remote commit")

            local_result = run(
                ["git", "-C", str(primary), "rev-parse", "--verify", f"{local_ref}^{{commit}}"],
                check=False,
            ) if local_ref else None
            if local_result is not None and local_result.returncode == 0:
                local_commit = local_result.stdout.strip()
                verdict.local_integration_commit = local_commit
                verdict.integration_refs[local_ref] = local_commit
                if not local_commit:
                    raise RuntimeError("cannot resolve local integration commit")
                if require_local_inclusion:
                    local_merged = graph("merge-base", "--is-ancestor", entry.head, local_commit)
                    if local_merged.returncode == 1:
                        if "unmerged" not in verdict.reasons:
                            verdict.reasons.append("unmerged")
                    elif local_merged.returncode != 0:
                        raise RuntimeError("cannot verify worktree HEAD against local integration ref")
                counts_result = graph("rev-list", "--left-right", "--count", f"{remote_commit}...{local_commit}")
                if counts_result.returncode != 0:
                    raise RuntimeError("cannot compare local integration commits with fetched remote")
                counts = counts_result.stdout.split()
                if len(counts) != 2:
                    raise RuntimeError("Git returned invalid integration ahead/behind counts")
                try:
                    behind, local_ahead = (int(value) for value in counts)
                except ValueError as error:
                    raise RuntimeError("Git returned invalid integration ahead/behind counts") from error
                if behind < 0 or local_ahead < 0:
                    raise RuntimeError("Git returned invalid integration ahead/behind counts")
                if local_ahead > 0:
                    verdict.reasons.append("integration-not-pushed")
            elif local_ref is not None:
                local_ref_exists = run(
                    ["git", "-C", str(primary), "show-ref", "--verify", "--quiet", local_ref],
                    check=False,
                )
                if require_local_inclusion or local_ref_exists.returncode == 0:
                    raise RuntimeError("cannot resolve local integration ref")
                if local_ref_exists.returncode != 1:
                    raise RuntimeError("cannot verify local integration ref existence")
                verdict.integration_refs[local_ref] = ""

    if fresh_processes:
        # Do the volatile registry/process observation after Git checks so a
        # process born (or chdir) during those checks is still a keep veto.
        latest_rows = registry_rows(jobs)
        relevant = lambda source: tuple(fields for fields, _meta in source if lexical(fields[3]) == target)
        if relevant(latest_rows) != relevant(rows) or registry_duty(jobs, target, latest_rows):
            verdict.reasons.append("registry-changed-or-retained")
        scan_kind, cwd_pids, verdict.unreadable_pids = process_cwds(target)
        _stale, pids = parse_registry(jobs, target, rows=latest_rows)
        verdict.process_scan = scan_kind
        verdict.active_pids = sorted(set(pids + cwd_pids))
        if verdict.active_pids:
            verdict.reasons.append("active-process")
        if scan_kind in {"unavailable", "lsof-error", "procfs-unknown"}:
            verdict.reasons.append("process-scan-unavailable")
        if any(known_exists(worktree_gitdir / name) for name in ("locked", "index.lock", "HEAD.lock")):
            verdict.reasons.append("locked")
        if git_operation(target) != "none":
            verdict.reasons.append("git-operation-changed")
    verdict.eligible = not verdict.reasons
    return verdict


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def reconcile_registry(jobs: Path, target: Path) -> int:
    if not jobs.is_file():
        return 0
    lock = Path(f"{jobs}.lock")
    changed = 0
    with file_lock(lock):
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        rewritten: list[str] = []
        for line in lines:
            fields = line.rstrip("\n").split("\t")
            current_attempt = False
            if len(fields) == 6:
                try:
                    validate_attempt_metadata(parse_registry_metadata(fields[5]))
                    current_attempt = True
                except DispatchContractError:
                    current_attempt = False
            if (
                len(fields) == 6
                and fields[1] in OPEN_STATES
                and current_attempt
                and lexical(fields[3]) == target
            ):
                fields[1] = "done"
                if not re.search(r"(?:^|,)note=cleanup-merged(?:,|$)", fields[5]):
                    fields[5] += ",note=cleanup-merged"
                line = "\t".join(fields) + "\n"
                changed += 1
            rewritten.append(line)
        if changed:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(jobs.parent), delete=False
            ) as handle:
                handle.writelines(rewritten)
                temp_name = handle.name
            Path(temp_name).replace(jobs)
    return changed


def append_audit(audit: Path, record: dict[str, object]) -> None:
    lock = Path(f"{audit}.lock")
    with file_lock(lock):
        audit.parent.mkdir(parents=True, exist_ok=True)
        previous = b""
        if audit.is_file():
            previous = audit.read_bytes()
            if len(previous) > MAX_AUDIT_BYTES:
                previous = previous[-KEEP_AUDIT_BYTES:]
                newline = previous.find(b"\n")
                previous = previous[newline + 1 :] if newline >= 0 else b""
        payload = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
        with tempfile.NamedTemporaryFile("wb", dir=str(audit.parent), delete=False) as handle:
            handle.write(previous)
            handle.write(payload)
            temp_name = handle.name
        Path(temp_name).replace(audit)


def printable(text: str) -> str:
    """Control characters in a file name must not split or rewrite an output line."""
    return "".join(
        ch if unicodedata.category(ch) not in ("Cc", "Zl", "Zp")
        else {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, f"\\x{ord(ch):02x}" if ord(ch) < 256 else f"\\u{ord(ch):04x}")
        for ch in text)


def emit(verdict: Verdict, status: str, reconciled: int = 0) -> None:
    print(f"status={status}")
    print(f"worktree={verdict.path}")
    print(f"primary_worktree={verdict.primary}")
    print(f"integration_ref={verdict.integration_ref}")
    print(f"integration_upstream={verdict.upstream}")
    print(f"integration_commit={verdict.integration_commit or '-'}")
    print(f"repository_mode={verdict.repository_mode}")
    print(f"head={verdict.head or '-'}")
    print(f"branch={verdict.branch or '-'}")
    print(f"process_scan={verdict.process_scan}")
    print(f"unreadable_pids={verdict.unreadable_pids}")
    print(
        "active_pids="
        + (",".join(str(pid) for pid in verdict.active_pids) if verdict.active_pids else "-")
    )
    print(f"stale_registry_rows={verdict.stale_rows}")
    print(f"registry_reconciled={reconciled}")
    print("branch_deleted=0")
    harvest = "ignored-files" in verdict.reasons
    print(f"artifact_harvest_required={int(harvest)}")
    if harvest:
        more = "+" if verdict.ignored_truncated else ""
        print(f"ignored_files={verdict.ignored_files}{more}")
        print(f"ignored_bytes={verdict.ignored_bytes}{more}")
        for size, rel in verdict.ignored_largest:
            print(f"ignored_path={size} {printable(rel)}")
    print("reasons=" + (",".join(verdict.reasons) if verdict.reasons else "-"))


def registered_job_worktrees(jobs: Path, repo: Path, *, context: CleanupPass | None = None) -> list[Path]:
    context = context or CleanupPass(repo, jobs)
    found: list[Path] = []
    registered = {entry.path for entry in context.entries}
    for fields, metadata in context.rows:
        try:
            validate_attempt_metadata(metadata)
        except DispatchContractError:
            continue
        if not metadata.get("attempt_id"):
            continue
        candidate = lexical(fields[3])
        if candidate in found or not candidate.is_dir():
            continue
        if candidate in registered:
            found.append(candidate)
    return found


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    action = p.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="evaluate only (default)")
    action.add_argument("--apply", action="store_true", help="remove eligible worktrees")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--worktree")
    target.add_argument(
        "--all-eligible",
        action="store_true",
        help="evaluate worktrees referenced by this repo's jobs registry",
    )
    p.add_argument("--repo", default=os.getcwd(), help="repo used with --all-eligible")
    p.add_argument("--integration-ref")
    p.add_argument(
        "--repository-mode",
        choices=("remote", "local-only"),
        default="remote",
        help="explicitly skip only the push-sync gate for a local-only repository",
    )
    p.add_argument("--jobs", help="absolute path of the jobs registry (jobs.log)")
    p.add_argument("--audit")
    return p


def main(argv: list[str]) -> int:
    arguments = parser()
    args = arguments.parse_args(argv[1:])
    from dispatch_terminal_commit import require_current_cleanup
    require_current_cleanup('worktree-cleanup')
    apply = bool(args.apply)
    agent_home = resolve_agent_home()
    try:
        jobs = normalize(
            resolve_dispatch_state_root(agent_home, explicit_jobs=args.jobs) / "jobs.log"
        )
    except DispatchContractError as exc:
        arguments.error(f"{'--jobs' if args.jobs is not None else 'jobs registry'}: "
                        f"{exc.reason} ({exc.detail})")
    audit = normalize(args.audit or dispatch_state_root(jobs) / "worktree-cleanup.jsonl")

    repo = normalize(args.repo) if args.all_eligible else lexical(args.worktree)
    try:
        context = CleanupPass(repo, jobs)
    except (OSError, RuntimeError, ValueError) as exc:
        print("status=blocked\nreasons=evidence-error")
        print(f"detail={exc}")
        return 3
    if args.all_eligible:
        targets = registered_job_worktrees(jobs, repo, context=context)
        if not targets:
            print("status=no-candidates")
            print("candidate_source=job-registry")
            return 0
    else:
        targets = [lexical(args.worktree)]

    blocked = 0
    removed_primaries = set()
    unsettled = False
    try:
        for index, target in enumerate(targets):
            if index:
                print()
            try:
                verdict = evaluate(
                    target, jobs, args.integration_ref, args.repository_mode, context=context
                )
            except (OSError, RuntimeError, ValueError) as e:
                if isinstance(e, UnsettledObservation):
                    raise
                print("status=blocked")
                print(f"worktree={target}")
                print("reasons=evidence-error")
                print(f"detail={e}")
                blocked += 1
                continue

            if not verdict.eligible:
                emit(verdict, "blocked")
                blocked += 1
                continue
            if not apply:
                emit(verdict, "eligible")
                continue

            try:
                verdict = context.before_remove(target, jobs, args.integration_ref,
                                                args.repository_mode, verdict)
                if not verdict.eligible:
                    emit(verdict, "blocked")
                    blocked += 1
                    continue
                result = run(
                    ["git", "-C", str(verdict.primary), "worktree", "remove", str(target)],
                    check=False,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                if isinstance(exc, UnsettledObservation):
                    raise
                verdict.reasons.append("fresh-evidence-or-remove-error")
                emit(verdict, "blocked")
                print(f"detail={exc}")
                blocked += 1
                continue
            if result.returncode != 0:
                verdict.reasons.append("remove-failed")
                emit(verdict, "blocked")
                if result.stderr.strip():
                    print(f"detail={result.stderr.strip()}")
                blocked += 1
                continue
            removed_primaries.add(verdict.primary)
            # Successful removal is audited even if registry reconciliation fails.
            reconciled = 0
            reconciliation_error = None
            try:
                reconciled = reconcile_registry(jobs, target)
            except (OSError, RuntimeError, ValueError) as exc:
                reconciliation_error = str(exc)
                blocked += 1
            record = {
                "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "action": "removed",
                "worktree": str(target),
                "primary_worktree": str(verdict.primary),
                "integration_ref": verdict.integration_ref,
                "integration_upstream": verdict.upstream,
                "integration_commit": verdict.integration_commit,
                "repository_mode": verdict.repository_mode,
                "head": verdict.head,
                "branch": verdict.branch,
                "branch_deleted": False,
                "registry_reconciled": reconciled,
                "artifact_harvest_required": False,
            }
            if reconciliation_error:
                record["registry_reconciliation_error"] = reconciliation_error
            append_audit(audit, record)
            emit(verdict, "removed", reconciled)
            print(f"audit_log={audit}")

    except UnsettledObservation as exc:
        unsettled = True
        blocked += 1
        print("status=blocked\nreasons=observation-descendants-unresolved")
        print(f"detail={exc}")
    finally:
        # One prune after successful removals, including a partial/interrupted
        # pass. Branch refs are deliberately retained for rollback.
        for primary in removed_primaries:
            if unsettled:
                print(f"prune_deferred={primary}: observation descendants unresolved", file=sys.stderr)
                continue
            try:
                run(["git", "-C", str(primary), "worktree", "prune"])
            except (OSError, RuntimeError) as exc:
                print(f"prune_deferred={exc}", file=sys.stderr)
                blocked += 1

    return 3 if blocked else 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_observe":
        OBSERVATION_TIMEOUT = float(sys.argv[2])
        raise SystemExit(observe_command(json.loads(sys.argv[3])))
    raise SystemExit(main(sys.argv))
