"""Non-blocking git branch and ahead/behind telemetry."""
import os
import re
import subprocess
import threading
import time

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
                 "HEAD...@{upstream}"], capture_output=True, text=True, timeout=2)
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
