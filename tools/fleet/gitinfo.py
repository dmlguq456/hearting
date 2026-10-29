"""Non-blocking git branch and ahead/behind telemetry."""
import os
import re
import subprocess
import threading
import time
import fcntl
import hashlib
import json
import math
import stat
import sys
import tempfile

_TTL = 30.0
_CACHE = {}
_INFLIGHT = set()
_LOCK = threading.RLock()


def resolve_gitdir(cwd):
    if not cwd:
        return None, None
    directory = os.path.abspath(cwd)
    for _ in range(30):
        pointer = os.path.join(directory, ".git")
        if os.path.isdir(pointer):
            real = os.path.realpath(pointer)
            return real, real
        if os.path.isfile(pointer):
            try:
                with open(pointer, encoding="utf-8") as fh:
                    text = fh.read().strip()
                target = text.split("gitdir:", 1)[1].strip()
                if not os.path.isabs(target):
                    target = os.path.join(directory, target)
                linked = os.path.realpath(target)
                main = linked.split(os.sep + "worktrees" + os.sep, 1)[0]
                return linked, main
            except (OSError, IndexError):
                return None, None
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return None, None


def branch(cwd):
    gitdir, _main = resolve_gitdir(cwd)
    if not gitdir:
        return None
    try:
        with open(os.path.join(gitdir, "HEAD"), encoding="utf-8") as fh:
            head = fh.readline().strip()
    except OSError:
        return None
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):] or None
    if re.fullmatch(r"[0-9a-fA-F]{40}", head):
        return head[:7]
    return None


def worktree_count(cwd):
    _linked, main = resolve_gitdir(cwd)
    if not main:
        return 0
    try:
        return sum(1 for name in os.listdir(os.path.join(main, "worktrees"))
                   if not name.startswith("."))
    except OSError:
        return 0


def _branch_section_matches(header, br):
    quoted = re.match(r'^([^\s"]+)\s+"(.*)"$', header)
    if quoted:
        return quoted.group(1).lower() == "branch" and quoted.group(2) == br
    if "." in header:
        prefix, _, rest = header.partition(".")
        return prefix.lower() == "branch" and rest == br
    return False


def _configured(cwd, br):
    if not br:
        return False
    linked, main = resolve_gitdir(cwd)
    gitdir = main or linked
    if not gitdir:
        return False
    try:
        with open(os.path.join(gitdir, "config"), encoding="utf-8") as fh:
            lines = fh.readlines()
    except (OSError, UnicodeDecodeError):
        return False
    found = {"remote": False, "merge": False}
    in_section = False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            header = line[1:line.rindex("]")] if "]" in line else line[1:]
            in_section = _branch_section_matches(header.strip(), br)
            continue
        if not in_section or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key in found and value.strip():
            found[key] = True
    return found["remote"] or found["merge"]


def _worker(cwd, br):
    value = None
    try:
        if _configured(cwd, br):
            result = subprocess.run(
                ["git", "-C", cwd, "rev-list", "--left-right", "--count",
                 "HEAD...@{upstream}"], capture_output=True, text=True, timeout=2,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
            fields = result.stdout.split()
            if result.returncode == 0 and len(fields) == 2 and all(x.isdigit() for x in fields):
                ahead, behind = (int(fields[0]), int(fields[1]))
                if ahead or behind:
                    value = (ahead, behind)
    except Exception:
        value = None
    with _LOCK:
        _CACHE[cwd] = (time.time(), value)
        _INFLIGHT.discard(cwd)


def cached_ahead_behind(cwd):
    """Read-only cache lookup — never schedules the background worker.

    F-51d: snapshot paths (--json/--once/--demo) must not spawn a new git subprocess
    thread; they may only surface whatever a prior live-TUI tick already resolved.
    """
    if not cwd:
        return None
    with _LOCK:
        cached = _CACHE.get(cwd)
        if cached and time.time() - cached[0] <= _TTL:
            return cached[1]
    return None


def ahead_behind(cwd):
    if not cwd:
        return None
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(cwd)
        if cached and now - cached[0] <= _TTL:
            return cached[1]
        if cwd not in _INFLIGHT:
            _INFLIGHT.add(cwd)
            threading.Thread(target=_worker, args=(cwd, branch(cwd)),
                             name="fleet-git-%s" % abs(hash(cwd)), daemon=True).start()
    return None


def enrich_entities(entities, schedule_ahead=True):
    """Attach Git display metadata outside the render loop.

    ``branch()`` and ``worktree_count()`` perform small filesystem reads which
    become visibly expensive on NAS-backed repositories when repeated at the
    10fps animation cadence. The live snapshot worker calls this helper once
    per unique cwd; renderers consume only the attached values.
    """
    rows = list(entities or ())
    metadata = {}
    for cwd in dict.fromkeys(getattr(row, "cwd", None) for row in rows):
        if not cwd:
            continue
        br = branch(cwd)
        count = worktree_count(cwd)
        if schedule_ahead:
            ahead_behind(cwd)
        metadata[cwd] = (br, count, cached_ahead_behind(cwd))
    for row in rows:
        cwd = getattr(row, "cwd", None)
        br, count, divergence = metadata.get(cwd, (None, 0, None))
        if not getattr(row, "branch", None):
            row.branch = br
        row.worktree_count = count
        row.branch_ahead = divergence[0] if divergence else None
        row.branch_behind = divergence[1] if divergence else None
    return rows


# Shell displays share a small local projection. Fleet's existing API above is
# intentionally independent: --json/--once never request this subprocess worker.
_DISPLAY_VERSION = 1
_DISPLAY_LIMIT = 65536
_DISPLAY_FIELDS = ("repo", "root", "gitdir", "common", "branch", "detached",
                   "operation", "dirty", "dirty_tracked", "untracked", "dirty_total",
                   "worktree_count", "extra_worktrees", "upstream", "ahead", "behind",
                   "default_branch", "branch_done", "merged")


def _display_request(cwd, project_dir=None):
    # Lexical only. Resolving/stat-ing a NAS cwd to construct the cache key would
    # put the slow observation back on the native callback's critical path.
    return {"cwd": os.path.abspath(cwd), "project_dir": os.path.abspath(project_dir or cwd),
            **{key: os.environ.get(key, "") for key in (
        "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "AGENT_ARTIFACT_ROOT")}}


def _display_paths(request):
    base = os.environ.get("HARNESS_STATE_ROOT") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "hearting")
    root = os.path.join(base, "git-display")
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    return root, os.path.join(root, key + ".json"), os.path.join(root, key + ".lock")


def _private_display_root(root, create=False):
    if create:
        os.makedirs(root, mode=0o700, exist_ok=True)
    meta = os.lstat(root)
    if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid()
            or stat.S_IMODE(meta.st_mode) & 0o077):
        raise OSError("Git display directory is not private owner state")


def _display_open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or meta.st_nlink != 1 or stat.S_IMODE(meta.st_mode) & 0o077):
            raise OSError("Git display file is not private owner state")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_display(request):
    root, path, _ = _display_paths(request)
    try:
        _private_display_root(root)
        with os.fdopen(_display_open(path, os.O_RDONLY), encoding="utf-8") as handle:
            raw = handle.read(_DISPLAY_LIMIT + 1)
            if len(raw) > _DISPLAY_LIMIT:
                return None
            value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("version") != _DISPLAY_VERSION
                or value.get("request") != request):
            return None
        for key in ("observed_at", "completed_at"):
            stamp = value.get(key)
            if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                    or not math.isfinite(stamp) or stamp <= 0 or stamp > time.time() + 1):
                return None
        if value["completed_at"] < value["observed_at"]:
            return None
        fields = value.get("git")
        if not isinstance(fields, dict) or set(fields) != set(_DISPLAY_FIELDS):
            return None
        numbers = {"repo", "detached", "dirty", "dirty_tracked", "untracked", "dirty_total",
                   "worktree_count", "extra_worktrees", "ahead", "behind", "branch_done", "merged"}
        for key, item in fields.items():
            if item is None:
                continue
            if key in numbers:
                if type(item) is not int or item < 0:
                    return None
            elif not isinstance(item, str) or len(item) > 4096:
                return None
        if not isinstance(value.get("jobs"), str) or len(value["jobs"]) > 8192:
            return None
        for key in ("artifact_root", "artifact_root_kind", "artifact_root_scope"):
            if value.get(key) is not None and not isinstance(value[key], str):
                return None
        if value.get("artifact_root_exists") not in (None, 0, 1):
            return None
        return value
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def _publish_display(request, value):
    root, path, _ = _display_paths(request)
    _private_display_root(root, create=True)
    data = json.dumps(value, ensure_ascii=True).encode()
    if len(data) > _DISPLAY_LIMIT:
        raise ValueError("Git display projection too large")
    fd, scratch = tempfile.mkstemp(prefix=".display-", dir=root)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(scratch, path)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)


def _display_claim(request):
    root, _, lock = _display_paths(request)
    _private_display_root(root, create=True)
    fd = _display_open(lock, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def cached_display(cwd, request=True, project_dir=None):
    """Read last observation and request one finite refresh; never touch cwd.

    A stale result retains its original timestamp. Missing/failed fields are
    None, never synthesized clean/zero. This projection grants no edit authority.
    """
    binding = _display_request(cwd, project_dir)
    value = _read_display(binding)
    if request and (value is None or time.time() - value["observed_at"] > _TTL):
        try:
            fd = _display_claim(binding)
        except OSError:
            return value
        try:
            # Recheck under the lifetime lock: racing requests can arrive after
            # the first worker finished. There is no TTL-based lock takeover.
            latest = _read_display(binding)
            if latest is not None and time.time() - latest["observed_at"] <= _TTL:
                return latest
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "--display-refresh",
                              json.dumps(binding), "--lock-fd", str(fd)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, cwd="/", close_fds=True,
                             pass_fds=(fd,), start_new_session=True)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            os.close(fd)
    return value


def _display_command(argv, fd):
    # No timeout kills or lease expiry. A stalled child keeps the inherited
    # flock, including when the renderer/refresh parent exits. Disk capture
    # avoids native stdout pipes and unbounded communicate() memory growth.
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                                 stderr=subprocess.DEVNULL, close_fds=True,
                                 pass_fds=(fd,), cwd="/",
                                 env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "COLUMNS": "100000"})
        code = child.wait()
        output.seek(0)
        raw = output.read(_DISPLAY_LIMIT + 1)
        if len(raw) > _DISPLAY_LIMIT:
            return None, ""
        return code, raw.decode("utf-8", errors="replace")


def _observe_display_git(cwd, fd):
    fields = dict.fromkeys(_DISPLAY_FIELDS)
    def git(*args):
        return _display_command(["git", "-C", cwd, *args], fd)
    code, text = git("rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir")
    if code != 0:
        # Only a successful, accessible ancestor walk proves non-Git. An I/O
        # error is not absence; avoid converting inaccessible repos into no-git.
        directory = cwd
        ceilings = {os.path.abspath(item) for item in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep) if item}
        while True:
            try:
                os.stat(directory)
                os.lstat(os.path.join(directory, ".git"))
                return fields
            except FileNotFoundError:
                # A missing cwd itself is inaccessible, not an empty directory.
                try:
                    os.stat(directory)
                except OSError:
                    return fields
            except OSError:
                return fields
            parent = os.path.dirname(directory)
            if parent == directory or parent in ceilings:
                fields["repo"] = 0
                return fields
            directory = parent
    lines = text.splitlines()
    if len(lines) != 3 or not all(lines):
        return fields
    root, linked, common = lines
    common = os.path.abspath(os.path.join(cwd, common))
    fields.update(repo=1, root=root, gitdir=linked, common=common)
    try:
        with open(os.path.join(linked, "HEAD"), encoding="utf-8") as handle:
            head = handle.readline().strip()
        if head.startswith("ref: refs/heads/"):
            fields.update(branch=head[len("ref: refs/heads/"):], detached=0)
        elif re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head):
            fields.update(branch=head[:7], detached=1)
    except OSError:
        pass
    try:
        operation = "none"
        for name, label in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"),
                            ("rebase-apply", "rebase"), ("CHERRY_PICK_HEAD", "cherry-pick")):
            try:
                os.stat(os.path.join(linked, name))
                operation = label
            except FileNotFoundError:
                pass
        fields["operation"] = operation
    except OSError:
        pass
    code, text = git("status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if code == 0:
        tracked = untracked = 0
        entries = iter(text.split("\0"))
        for entry in entries:
            if not entry:
                continue
            if entry.startswith("?? "):
                untracked += 1
            else:
                tracked += 1
                if "R" in entry[:2] or "C" in entry[:2]:
                    next(entries, None)  # rename source is not another dirty path
        fields.update(dirty_tracked=tracked, untracked=untracked,
                      dirty_total=tracked + untracked, dirty=int(bool(tracked + untracked)))
    code, text = git("worktree", "list", "--porcelain")
    if code == 0:
        count = sum(line.startswith("worktree ") for line in text.splitlines())
        if count:
            fields.update(worktree_count=count, extra_worktrees=count - 1)
    code, text = git("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if code == 0 and text.strip().startswith("origin/"):
        fields["default_branch"] = text.strip()[len("origin/"):]
    elif code == 1:
        fields["default_branch"] = "main"  # existing display fallback
    br = fields["branch"]
    if fields["detached"] == 1:
        fields.update(upstream="none", branch_done=0, merged=0)
    elif br:
        code, text = git("for-each-ref", "--format=%(refname) %(upstream:short)", "refs/heads/" + br)
        exact = [line[len("refs/heads/" + br + " "):] for line in text.splitlines()
                 if line.startswith("refs/heads/" + br + " ")]
        if code == 0 and len(exact) == 1:
            upstream = exact[0].strip()
            fields["upstream"] = upstream or "none"
            if not upstream:
                fields["branch_done"] = 0
            else:
                code, counts = git("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
                nums = counts.split()
                if code == 0 and len(nums) == 2 and all(x.isdigit() for x in nums):
                    fields["ahead"], fields["behind"] = map(int, nums)
                    if fields["operation"] is not None:
                        fields["branch_done"] = int(fields["ahead"] == 0 and
                            fields["operation"] == "none" and br not in
                            {"main", "master", "develop", "trunk", fields["default_branch"]})
        default = fields["default_branch"]
        if br == default:
            fields["merged"] = 0
        elif default:
            code, count = git("rev-list", "--count", "origin/" + default + "..HEAD")
            if code == 0 and count.strip().isdigit():
                fields["merged"] = int(count.strip() == "0")
    return fields


def _legacy_display_jobs(cwd, fd):
    """One finite, same-UID fallback census, off the render path.

    An argv/legacy artifact hint never claims completion. Modern registered
    pipeline state remains owned by its existing projection, not this fallback.
    """
    code, text = _display_command(["ps", "-u", str(os.getuid()), "-o", "pid=,etime=,args="], fd)
    if code != 0:
        return ""
    seen = {}
    for line in text.splitlines():
        columns = line.strip().split(None, 2)
        if len(columns) != 3:
            continue
        pid, elapsed, args = columns
        match = re.findall(r"/autopilot-([a-z-]+)", args)
        if not match or "claude" not in args:
            loop = re.search(r"loops/(oncall|note|study|drill|runtime-watch)(?:\b|/)", args)
            if loop:
                seen.setdefault((loop[1], "loop"), loop[1] + " ⏳" + elapsed)
            continue
        if os.path.basename(args.split()[0]) in {"sh", "bash", "zsh", "dash"}:
            continue
        try:
            jcwd = os.readlink("/proc/" + pid + "/cwd")
        except OSError:
            continue
        if not (cwd == jcwd or jcwd.startswith(cwd.rstrip("/") + "/")
                or cwd.startswith(jcwd.rstrip("/") + "/")
                or jcwd.startswith(cwd + "-wt/") or cwd.startswith(jcwd + "-wt/")
                or (os.path.dirname(jcwd) == os.path.dirname(cwd) and os.path.dirname(cwd).endswith("-wt"))):
            continue
        key = match[-1]
        slug = os.path.basename(jcwd)
        # Exact legacy folder only; no fuzzy token search, summary=>done or
        # second Git status. Limit fallback to this job's directly named plans.
        stage = key
        current = _display_job_stage(pid)
        if current is not None:
            stage = current  # an invalid exact binding stays unknown
        for plans in (() if current is not None else _legacy_display_plan_roots(jcwd)):
            try:
                with os.scandir(plans) as entries:
                    candidates = []
                    for index, entry in enumerate(entries):
                        if index >= 64:
                            candidates = []
                            break
                        if entry.name.endswith("_" + slug):
                            candidates.append(entry.path)
                if len(candidates) == 1:
                    for bucket, hint in (("test_logs", "test~"), ("dev_logs", "exec~"), ("plan", "plan~")):
                        with os.scandir(candidates[0]) as entries:
                            if any(entry.name == bucket for entry in entries):
                                stage = hint
                                break
            except OSError:
                continue
        seen.setdefault((key, jcwd), key + ("▸" + stage if key != stage else "") + " ⏳" + elapsed + " " + slug)
        if len(seen) >= 4:
            break
    values = list(seen.values())
    return " / ".join(values[:3] + (["+1"] if len(values) > 3 else []))


def _display_job_stage(pid):
    """Consume the existing exact route projection, without a jobs reader."""
    try:
        with open("/proc/" + pid + "/environ", "rb") as handle:
            raw = handle.read(_DISPLAY_LIMIT + 1)
        if len(raw) > _DISPLAY_LIMIT:
            return "unknown"
        env = dict(part.decode(errors="replace").split("=", 1) for part in raw.split(b"\0") if b"=" in part)
        entity = {key: env.get(name) for key, name in (
            ("route_file", "AGENT_ROUTE_FILE"), ("route_id", "AGENT_ROUTE_ID"),
            ("route_hash", "AGENT_ROUTE_HASH"), ("route_node", "AGENT_ROUTE_NODE"))}
        if not any(entity.values()):
            return None
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from fleet.projection import resolve_work_projection
        value = resolve_work_projection(entity)
        return value.stage_label if value.source == "route-exact" and not value.ambiguity else "unknown"
    except (OSError, ValueError, ImportError):
        return "unknown"


def _legacy_display_plan_roots(cwd):
    # Finite compatibility read of the two existing campaign layouts plus the
    # legacy top-level bucket. No glob/fuzzy match or terminal-state inference.
    budget = 64
    def children(path):
        nonlocal budget
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if budget <= 0:
                        return
                    budget -= 1
                    if not entry.name.startswith("."):
                        yield entry.path
        except OSError:
            return
    for artifact in (".agent_reports", ".claude_reports"):
        root = os.path.join(cwd, artifact)
        yield os.path.join(root, "plans")
        for campaign in children(os.path.join(root, "campaigns")):
            for cycle in children(campaign):
                if os.path.basename(cycle) == "cycles":
                    for old_cycle in children(cycle):
                        yield os.path.join(old_cycle, "artifacts", "plans")
                else:
                    yield os.path.join(cycle, "artifacts", "plans")


def refresh_display(request, fd):
    started = time.time()
    value = {"version": _DISPLAY_VERSION, "request": request, "observed_at": started,
             "git": dict.fromkeys(_DISPLAY_FIELDS), "jobs": ""}
    try:
        disabled = False
        for name in ("settings.json", "settings.local.json"):
            try:
                with open(os.path.join(request["project_dir"], ".claude", name), encoding="utf-8") as handle:
                    if json.load(handle).get("autoCompactEnabled") is False:
                        disabled = True
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError):
                if disabled is not True:
                    disabled = None
        value["project_auto_compact_disabled"] = disabled
        value["git"] = _observe_display_git(request["cwd"], fd)
        value["jobs"] = _legacy_display_jobs(request["cwd"], fd)
        # Artifact discovery can also traverse Git/NAS paths. Keep the existing
        # canonical resolver, but only invoke it in this observation worker.
        utility = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "utilities", "artifact-root.sh")
        code, text = _display_command([utility, value["git"]["root"] or request["cwd"]], fd)
        if code == 0 and text.strip():
            artifact = text.strip()
            value.update(artifact_root=artifact, artifact_root_kind=os.path.basename(artifact),
                         artifact_root_exists=int(os.path.isdir(artifact)),
                         artifact_root_scope="explicit" if request["AGENT_ARTIFACT_ROOT"] else
                         "primary-worktree" if value["git"]["root"] and
                         os.path.dirname(artifact) != value["git"]["root"] else "project-local")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    value["completed_at"] = time.time()
    _publish_display(request, value)
    return value


def _owned_display_refresh(request, fd):
    # Reuse the verification runner's native subreaper support. Inherited
    # descriptors alone cannot cover a helper that closes them before forking.
    # This is one finite wait for owned children, with no poller or signals.
    import importlib.util
    utilities = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "utilities")
    sys.path.insert(0, utilities)
    try:
        spec = importlib.util.spec_from_file_location("display_child_lifetime",
            os.path.join(utilities, "verification-background-lease.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        supported = module.become_subreaper()
    except (OSError, ImportError, AttributeError):
        supported = False
    if not supported:
        now = time.time()
        value = {"version": _DISPLAY_VERSION, "request": request,
                 "observed_at": now, "completed_at": now,
                 "git": dict.fromkeys(_DISPLAY_FIELDS), "jobs": "",
                 "reason": "owned-descendant-wait-unavailable"}
        _publish_display(request, value)
        return value
    try:
        return refresh_display(request, fd)
    finally:
        while True:
            try:
                os.waitpid(-1, 0)
            except InterruptedError:
                continue
            except ChildProcessError:
                break


def _display_main():
    import argparse
    import shlex
    parser = argparse.ArgumentParser()
    parser.add_argument("--display-refresh")
    parser.add_argument("--lock-fd", type=int)
    parser.add_argument("--display", choices=("shell", "json", "refresh"))
    parser.add_argument("--project-dir")
    parser.add_argument("cwd", nargs="?", default=os.environ.get("PWD", "/"))
    args = parser.parse_args()
    if args.display_refresh:
        request = json.loads(args.display_refresh)
        # fd is inherited from the requester, never reopened by pathname.
        with os.fdopen(args.lock_fd, "rb"):
            _owned_display_refresh(request, args.lock_fd)
        return
    if args.display == "refresh":
        request = _display_request(args.cwd, args.project_dir)
        fd = _display_claim(request)
        try:
            value = _owned_display_refresh(request, fd)
        finally:
            os.close(fd)
    else:
        value = cached_display(args.cwd, project_dir=args.project_dir)
    if args.display in {"json", "refresh"}:
        print(json.dumps(value))
        return
    fields = value["git"] if value else dict.fromkeys(_DISPLAY_FIELDS)
    fields = {**fields, "observed_at": value["observed_at"] if value else None,
              "completed_at": value["completed_at"] if value else None,
              "observation": ("fresh" if time.time() - value["observed_at"] <= _TTL else "stale") if value else "unknown",
              "jobs": value["jobs"] if value else "",
              "project_auto_compact_disabled": int(value["project_auto_compact_disabled"])
                  if value and value.get("project_auto_compact_disabled") is not None else None,
              "observed_label": time.strftime("%H:%M:%S", time.localtime(value["observed_at"])) if value else "?"}
    for key in ("artifact_root", "artifact_root_kind", "artifact_root_exists", "artifact_root_scope"):
        fields[key] = value.get(key) if value else None
    for key, item in fields.items():
        # Never place native path/control bytes into a terminal or shell source.
        text = "unknown" if item is None else str(item)
        text = "".join(c for c in text if c >= " " and c != "\x7f")
        print("git_" + key + "=" + shlex.quote(text))


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    _display_main()
