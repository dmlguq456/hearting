"""A project's status document (``현황.md`` / ``STATUS.md``) for Fleet's per-project line.

Read-only and best-effort. The location and shape are the convention in
``core/ARTIFACT_META.md`` ("The project status document"): ``현황.md`` (else ``STATUS.md``)
directly in a cycle's ``artifacts/``, which the producer makes that cycle's primary, so
Cairn's status row reads the same file. Names match NFC-normalized and case-folded, as
the producer and Cairn match them. The most recently changed one is the project's current
status. A file at the cycle root is never sealed and is not read; the legacy flat folder
``research/<dir>/`` is read too.

Cost: the artifact root comes from file reads only (no git process), and one scan per
root runs at most every ``INTERVAL`` seconds, over campaigns changed in the last
``RECENT_DAYS`` days, with one stat per cycle and a time budget. Everything else is
cached. Nothing here writes, gates, or asks anything.
"""
import os
import re
import time
import unicodedata

STATUS_NAMES = ("현황.md", "status.md")    # preference order, NFC and case-folded
RECENT_DAYS = 14
INTERVAL = 60.0           # seconds between scans of one artifact root
SCAN_BUDGET = 3.0         # seconds one scan may spend before it stops (the rest waits a pass)
READ_LIMIT = 65536        # bytes read from a status document
_IMMUTABLE = ("/.harness/bundles/", "/hearting/releases/")

_ROOTS = {}               # cwd -> (resolved_at, root or None)
_SCANS = {}               # root -> (scanned_at, record or None)
_CYCLES = {}              # campaign dir -> (dir mtime, cycle dirs)
_LISTED = {}              # folder -> (folder mtime, its status document path or None)
_DOCS = {}                # path -> (mtime, size, parsed)


def clear_cache():
    for cache in (_ROOTS, _SCANS, _CYCLES, _LISTED, _DOCS):
        cache.clear()


def _isdir(path):
    try:
        return os.path.isdir(path)
    except OSError:
        return False


def _reports(base):
    for name in (".agent_reports", ".claude_reports"):
        if _isdir(os.path.join(base, name)):
            return os.path.join(base, name)
    return None


def _git_common(top):
    """The common git directory of the checkout at ``top`` (a linked worktree's ``.git`` is a file)."""
    git = os.path.join(top, ".git")
    if os.path.isdir(git):
        return git
    try:
        with open(git, encoding="utf-8") as handle:
            line = handle.readline(4096).strip()
    except (OSError, UnicodeError):
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = os.path.normpath(os.path.join(top, line[len("gitdir:"):].strip()))
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as handle:
            common = handle.readline(4096).strip()
    except (OSError, UnicodeError):
        return gitdir
    return os.path.normpath(os.path.join(gitdir, common)) if common else gitdir


def _is_git(folder):
    """Whether ``folder/.git`` is one git itself accepts: a directory with ``HEAD``, or a ``gitdir:`` file."""
    git = os.path.join(folder, ".git")
    if os.path.isdir(git):
        return os.path.isfile(os.path.join(git, "HEAD"))
    try:
        with open(git, encoding="utf-8") as handle:
            return handle.read(7) == "gitdir:"
    except (OSError, UnicodeError):
        return False


def _git_top(folder):
    """The nearest folder at or above ``folder`` that is a Git checkout's top, or None."""
    while True:
        if _is_git(folder):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def artifact_root(cwd):
    """The artifact root ``utilities/artifact-root.sh`` names for ``cwd``, when it exists.

    The same rule from file reads only: inside a Git checkout the primary checkout's
    ``.agent_reports`` (then ``.claude_reports``), so a linked worktree reads its main
    checkout's root; elsewhere the folder's own root, or an ancestor's root when that
    ancestor carries an ``.agent-workspace`` marker. An installed release tree is never one.
    """
    try:
        start = os.path.realpath(str(cwd))
    except (OSError, ValueError):
        return None
    if not _isdir(start):
        return None
    top = _git_top(start)
    root = None
    if top is not None:
        common = _git_common(top)
        root = _reports(os.path.dirname(common) if common and os.path.basename(common) == ".git" else top)
    else:
        folder = start
        while root is None:
            found = _reports(folder)
            if found and (folder == start or os.path.isfile(os.path.join(folder, ".agent-workspace"))):
                root = found
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent
    if root and any(part in root + "/" for part in _IMMUTABLE):
        return None
    return root


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _subdirs(path):
    try:
        with os.scandir(path) as entries:
            return [entry.path for entry in entries
                    if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _status_file(folder):
    """The status document directly in ``folder``: ``현황.md`` before ``STATUS.md``, any case or
    Unicode form. One listing per change of the folder (a new or renamed file changes its mtime)."""
    stamp = _mtime(folder)
    if stamp is None:
        return None
    known = _LISTED.get(folder)
    if known is None or known[0] != stamp:
        names = {}
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    key = unicodedata.normalize("NFC", entry.name).casefold()
                    if key in STATUS_NAMES and entry.is_file():
                        names.setdefault(key, entry.path)
        except OSError:
            names = {}
        known = (stamp, next((names[key] for key in STATUS_NAMES if key in names), None))
        _LISTED[folder] = known
    return known[1]


def find_documents(root, now=None, budget=SCAN_BUDGET):
    """Status documents under ``root`` the convention names, plus the legacy research copies.

    Campaigns changed in the last ``RECENT_DAYS`` days are read newest first, one stat per
    cycle's ``artifacts/`` (listed again only when it changed); past ``budget`` seconds the
    rest waits for the next scan.
    """
    now = time.time() if now is None else now
    since = now - RECENT_DAYS * 86400
    started = time.monotonic()
    found = []
    for folder in _subdirs(os.path.join(root, "research")):
        found.append(_status_file(folder))
    campaigns = []
    for campaign in _subdirs(os.path.join(root, "campaigns")):
        mtime = _mtime(campaign)
        if mtime is not None and mtime >= since:
            campaigns.append((mtime, campaign))
    for mtime, campaign in sorted(campaigns, reverse=True):
        cached = _CYCLES.get(campaign)
        if cached is None or cached[0] != mtime:
            cached = (mtime, _subdirs(campaign))
            _CYCLES[campaign] = cached
        for cycle in cached[1]:
            if time.monotonic() - started > budget:
                return [path for path in found if path]
            found.append(_status_file(os.path.join(cycle, "artifacts")))
    return [path for path in found if path]


# -- reading one document ---------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")
_ONE_LINE = re.compile(r"^\*\*\s*(?:한\s*줄|one[\s-]*line|summary|요약)\s*[:：]?\s*\*\*\s*[:：]?\s*(.*)$",
                       re.IGNORECASE)
_UPDATED = re.compile(r"(?:갱신|updated)\s*[:：]?\s*[0-9]", re.IGNORECASE)
# Heading words: the convention names "결정 필요" / "Decisions needed" (what waits on the
# person: a decision or an action) and "다음" / "Next"; a few near spellings are read too.
# A record of decisions already made is not counted.
_DECISION_WORDS = ("결정 필요", "결정할", "결정 대기", "사용자에게 필요", "decisions needed", "decision needed",
                   "needs decision", "to decide", "open decision", "pending decision", "needs you")
_RECORD_WORDS = ("기록", "내린", "log", "made", "history")
_NEXT_WORDS = ("다음", "next", "할 일", "to do", "todo")


def _plain(text):
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\s+", " ", text.replace("**", "").replace("__", "").replace("`", "")).strip()


def _section_kind(title):
    lowered = title.casefold()
    if any(word in lowered for word in _RECORD_WORDS):
        return None
    if any(word in lowered for word in _DECISION_WORDS):
        return "decisions"
    if lowered.startswith(_NEXT_WORDS):
        return "next"
    return None


def parse(text):
    """``{title, line, decisions, next}`` from a status document; loose by design.

    Read: the H1 title, the ``**한 줄**:``/``**One line**:`` paragraph above the first
    section (else the first plain paragraph there that is not the ``갱신``/``Updated``
    stamp, else the title), and the top-level bullets under a "decisions needed" heading
    and a "next" heading.
    Anything else is ignored; a missing part stays empty.
    """
    title = line = first_paragraph = None
    sections = {"decisions": [], "next": []}
    current, level, in_body = None, 0, False
    paragraph = []

    def close_paragraph():
        nonlocal first_paragraph
        joined = _plain(" ".join(paragraph))
        paragraph.clear()
        if joined and first_paragraph is None and not in_body and not _UPDATED.match(joined):
            first_paragraph = joined

    for raw in str(text).splitlines():
        stripped = raw.strip()
        heading = _HEADING.match(stripped)
        if heading:
            close_paragraph()
            depth, words = len(heading.group(1)), _plain(heading.group(2))
            if title is None and depth == 1 and not in_body:
                title = words
                continue
            in_body = True
            kind = _section_kind(words)
            if kind is not None:
                current, level = kind, depth
            elif current is not None and depth <= level:
                current = None
            continue
        if current is not None:
            bullet = _BULLET.match(raw) if raw[:1] not in (" ", "\t") else None
            if bullet:
                sections[current].append(_plain(bullet.group(1)))
            continue
        if in_body:
            continue
        one = _ONE_LINE.match(stripped)
        if one:
            close_paragraph()
            line = line or _plain(one.group(1)) or None
        elif not stripped or _BULLET.match(stripped) or stripped.startswith(("|", ">", "```")):
            close_paragraph()
        else:
            paragraph.append(stripped)
    close_paragraph()
    return {"title": title, "line": line or first_paragraph or title,
            "decisions": sections["decisions"], "next": sections["next"]}


def read_document(path):
    """The parsed document with its path and mtime; None when it cannot be read."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    cached = _DOCS.get(path)
    if cached is not None and cached[:2] == (info.st_mtime, info.st_size):
        parsed = cached[2]
    else:
        try:
            with open(path, "rb") as handle:
                text = handle.read(READ_LIMIT).decode("utf-8", "replace")
        except OSError:
            return None
        parsed = parse(text)
        _DOCS[path] = (info.st_mtime, info.st_size, parsed)
    return dict(parsed, path=path, name=os.path.basename(path), mtime=info.st_mtime)


def newest(root, now=None, budget=SCAN_BUDGET):
    """The most recently changed status document under ``root``, parsed; None when there is none."""
    best = None
    for path in find_documents(root, now, budget):
        record = read_document(path)
        if record is not None and (best is None or record["mtime"] > best["mtime"]):
            best = record
    if best is not None:
        best["relative"] = os.path.relpath(best["path"], root)
    return best


def collect(cwds, now=None, budget=SCAN_BUDGET):
    """``{cwd: status document record or None}`` for the given session folders.

    Each artifact root is scanned at most once per ``INTERVAL``; between scans the last
    answer stands. One call spends at most about ``budget`` seconds on scans: a root it
    does not reach keeps its last answer and is scanned on a later call. Never raises.
    """
    now = time.time() if now is None else now
    deadline = time.monotonic() + budget
    result = {}
    for cwd in dict.fromkeys(str(cwd) for cwd in cwds or () if cwd):
        try:
            resolved = _ROOTS.get(cwd)
            if resolved is None or now - resolved[0] >= INTERVAL:
                resolved = (now, artifact_root(cwd))
                _ROOTS[cwd] = resolved
            root = resolved[1]
            scanned = _SCANS.get(root) if root else None
            left = deadline - time.monotonic()
            if root and (scanned is None or now - scanned[0] >= INTERVAL) and left > 0:
                scanned = (now, newest(root, now, left))
                _SCANS[root] = scanned
            result[cwd] = scanned[1] if scanned else None
        except Exception:
            result[cwd] = None
    return result
