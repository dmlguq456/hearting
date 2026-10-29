"""Pure, shared projection of opaque runtime session IDs into display handles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional


_PREFIXES = {"claude": "CL", "codex": "CX", "opencode": "OC"}


def session_handle(harness: object, session_id: object) -> str:
    prefix = _PREFIXES.get(str(harness or "").lower())
    if not prefix or not isinstance(session_id, str) or not session_id:
        return ""
    return f"{prefix}/{session_id[:8]}"


def sanitize_title(title: object) -> str:
    if not isinstance(title, str):
        return ""
    single_line = "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch
                          for ch in title)
    return " ".join(single_line.split())


def current_session_subject(runtime_name: object, successful_title: object) -> str:
    """Choose the current user name, last successful title, or an empty subject.

    This narrow display policy deliberately has no native-title, registry, slug,
    or cwd fallback. Callers resolve any user-owned name and successful title
    through their existing source-specific paths before passing them here.
    """
    for candidate in (runtime_name, successful_title):
        cleaned = sanitize_title(candidate)
        if cleaned:
            return cleaned
    return ""


def _cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(ch) else
               (2 if unicodedata.east_asian_width(ch) in "WFA" else 1)
               for ch in text)


def clip_cells(text: object, budget: object) -> str:
    value = str(text or "")
    try:
        limit = max(0, int(budget))
    except (TypeError, ValueError):
        limit = 0
    if _cell_width(value) <= limit:
        return value
    if limit <= 0:
        return ""
    if limit == 1:
        return "…"
    out = []
    used = 0
    for ch in value:
        width = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in "WFA" else 1)
        if used + width > limit - 1:
            break
        out.append(ch)
        used += width
    return "".join(out) + "…"


def session_display_name(harness: object, session_id: object, title: object,
                         budget: object = None, fallback: object = "") -> str:
    handle = session_handle(harness, session_id)
    if not handle:
        return sanitize_title(fallback)
    name = handle
    clean_title = sanitize_title(title)
    if clean_title:
        candidate = f"{handle} · {clean_title}"
        if budget is None or _cell_width(candidate) <= int(budget):
            return candidate
        separator = " · "
        remaining = int(budget) - _cell_width(handle) - _cell_width(separator)
        # A lone ellipsis is not a meaningful title; drop the whole optional
        # segment rather than leaving a separator-only display behind.
        if remaining > 1:
            name = f"{handle}{separator}{clip_cells(clean_title, remaining)}"
    if budget is None:
        return name
    return clip_cells(name, max(0, int(budget)))


def display_name(harness: object, session_id: object, *, runtime_name: object = None,
                  registry_name: object = None, title: object = None,
                  slug: object = None, cwd: object = None) -> str:
    """F-99a — the one canonical human-readable session name.

    Pure: no I/O. Precedence ① ``runtime_name`` (a runtime-exposed user-set name,
    already folded with the hearting session-name registry by
    ``resolve_display_inputs`` before this is called) → ② the existing F-95 chain
    (``title`` → ``registry_name`` → ``slug`` → ``basename(cwd)``). Never returns a
    ``CL/``/``CX/``/``OC/`` sid8 handle — ``session_handle()``/``session_display_name()``
    stay only for back-compat callers.
    """
    for candidate in (runtime_name, title, registry_name, slug):
        cleaned = sanitize_title(candidate) if isinstance(candidate, str) else ""
        if cleaned:
            return cleaned
    base = str(cwd or "").rstrip("/").rsplit("/", 1)[-1] if cwd else ""
    return sanitize_title(base) or "?"


_DERIVED_TAG_RE = re.compile(r"^.+-(?P<tag>[0-9a-f]{2})$")


def derived_tag(name: object) -> Optional[str]:
    """F-100a — the 2-hex suffix of a harness-derived session name.

    Claude Code mints ``<cwd basename>-<xx>`` (``hearting-46``, ``cairn-47``,
    ``claude-cf``); the suffix is random, not a function of the session id, so it can
    only be read off the name. Pure. Returns ``None`` for any other shape. The CALLER
    must already know the name is derived (``nameSource == "derived"``) — a user-set
    ``release-1a`` has the same shape and must never be mistaken for a tag.
    """
    if not isinstance(name, str):
        return None
    m = _DERIVED_TAG_RE.match(name.strip())
    return m.group("tag") if m else None


def minted_tag(session_id: object) -> Optional[str]:
    """F-100b (user 2026-09-03) — the 2-hex tag for a harness that mints no derived name.

    Claude carries its tag inside the derived session name, so ``derived_tag()`` only has
    to read it off. Codex and OpenCode expose no such name, so Fleet mints the tag itself
    from the canonical session id. This pure hash is only an allocator candidate. Display consumers
    use ``resolve_tag`` to honor collision-free live assignments.

    NOT the id's own leading hex: a Codex thread id is a UUIDv7 and every one of them
    starts ``01``. Pure. Returns ``None`` for a missing or non-string id. Collisions are
    possible at 256 values and are the same property Claude's random suffix already has.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    return hashlib.sha256(session_id.strip().encode("utf-8")).hexdigest()[:2]


_SOCKET_ADDRESS_RE = re.compile(r"^(?:uds|unix):/.*/(?P<pid>\d+)\.sock$")


def _claude_sessions_dir(home: object = None) -> Optional[str]:
    """The Claude Code session-registry directory, or ``None`` when none exists.

    The env candidates are tried in order and the first one that actually HAS a
    `sessions/` directory wins. `AGENT_HOME` is in the list for historical reasons and is
    the harness root, not a Claude config dir — under a managed release it pointed at a
    tree with no `sessions/` at all, and taking it on faith made every lookup here return
    nothing while Fleet ran (measured 2026-09-10; `resolve_tag` only survived it because
    it has a second source).
    """
    candidates = [str(home)] if home else [
        os.environ.get("CLAUDE_CONFIG_DIR"),
        os.environ.get("CLAUDE_HOME"),
        os.environ.get("AGENT_HOME"),
        os.path.expanduser("~/.claude"),
    ]
    for base in candidates:
        if not base:
            continue
        path = os.path.join(base, "sessions")
        if os.path.isdir(path):
            return path
    return None


def session_id_for_address(address: object, *, config_dir: object = None) -> Optional[str]:
    """``uds:/run/user/1002/cc-socks/284639.sock`` → that session's id, else ``None``.

    Claude Code addresses a peer by its socket, and the socket is named after the peer
    process's PID — the same PID the runtime writes ``<config>/sessions/<pid>.json`` for.
    So the address IS an exact identity; the ledger simply could not read it and filed it
    under ``name`` instead, where a socket path is (correctly) refused as a display name
    and the board fell back to a bare ``claude``: 222 rows measured 2026-09-10.

    One definition, two callers — the ledger writer resolves it once at record time so the
    stored row is durable, and the board resolves it again for rows written before that.
    A resumed session can leave a stale ``sessionId`` in its PID file, so this is best
    evidence rather than proof; the PID inside the file must at least agree with the one
    in the address.
    """
    if not isinstance(address, str):
        return None
    match = _SOCKET_ADDRESS_RE.match(address.strip())
    if not match:
        return None
    pid = match.group("pid")
    sessions = _claude_sessions_dir(config_dir)
    if not sessions:
        return None
    try:
        with open(os.path.join(sessions, pid + ".json"), encoding="utf-8") as fh:
            record = json.load(fh)
    except Exception:
        return None
    if not isinstance(record, dict) or str(record.get("pid") or pid) != pid:
        return None
    session_id = record.get("sessionId")
    return session_id if isinstance(session_id, str) and session_id.strip() else None


def session_id_for_derived_name(name: object, *, home: object = None) -> Optional[str]:
    """A Claude derived session name (``bc-resnet-15``) → that session's id, else ``None``.

    The inverse of the name the badge is read off. Older ledger rows carry a peer's name
    but no id, and a name and a badge for the same peer are two shapes for one relation —
    the inconsistency the user reported (2026-09-09). Only a record that still says
    ``nameSource == "derived"`` counts, for the same reason `derived_tag` insists on it: a
    user-set ``release-1a`` has the identical shape and is not a badge.

    Ambiguity yields ``None``: a name reused by two records names neither.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    wanted = name.strip()
    if not derived_tag(wanted):
        return None
    sessions = _claude_sessions_dir(home)
    if not sessions:
        return None
    found = None
    try:
        for entry in os.listdir(sessions):
            if not entry.endswith(".json"):
                continue
            try:
                with open(os.path.join(sessions, entry), encoding="utf-8") as fh:
                    record = json.load(fh)
            except Exception:
                continue
            if not isinstance(record, dict) or record.get("name") != wanted:
                continue
            if record.get("nameSource") != "derived":
                continue
            session_id = record.get("sessionId")
            if not isinstance(session_id, str) or not session_id.strip():
                continue
            if found is not None and found != session_id:
                return None
            found = session_id
    except Exception:
        return None
    return found


_UNSET_RECIPIENT_CONTEXT = object()


def resolve_tag(harness: object, session_id: object, *, home: object = None) -> Optional[str]:
    """Resolve the shared display tag using the caller's historical context.

    Existing callers retain their environment-based Claude seat fallback. A
    recipient lookup must use :func:`resolve_recipient_tag` so the sender's
    pane and cwd cannot affect which resumed seat history is read.
    """
    return _resolve_tag(harness, session_id, home=home)


def resolve_recipient_tag(harness: object, session_id: object, *, pane: object,
                          cwd: object, home: object = None) -> Optional[str]:
    """Resolve a tag for one recipient pane without consulting sender context."""
    return _resolve_tag(harness, session_id, home=home,
                        recipient_pane=pane, recipient_cwd=cwd)


def _resolve_tag(harness: object, session_id: object, *, home: object = None,
                 recipient_pane: object = _UNSET_RECIPIENT_CONTEXT,
                 recipient_cwd: object = _UNSET_RECIPIENT_CONTEXT) -> Optional[str]:
    """The one 2-hex badge rule, shared by every surface that draws `[xx]`.

    Fleet's collectors, `peer-message.py`'s trailer alias, and the Herdr pane title all
    have to answer the same question — "which badge does this session wear?" — and the
    rule differs per harness (Claude reads its derived name, the other two read their
    confirmed shared assignment). Keeping copies is how the badge and the
    trailer drift apart, so this is the single definition; callers add their own
    brackets. I/O-bearing and fail-soft: any unreadable record yields the next source,
    and an unresolvable session yields ``None`` rather than a guess.
    """
    harness_key = str(harness or "").lower()
    sid = str(session_id or "").strip()
    if not harness_key or not sid:
        return None
    if harness_key in ("codex", "opencode"):
        try:
            from .session_tags import assigned_tag
            return assigned_tag(harness_key, sid)
        except ImportError:
            return None
    if harness_key != "claude":
        return None
    # Claude: the derived `<basename>-<xx>` name is the only carrier, and only while the
    # record still says `derived` (a user-set `release-1a` has the same shape, F-100a).
    sessions = _claude_sessions_dir(home)
    records = []
    try:
        for entry in os.listdir(sessions or ""):
            if not entry.endswith(".json"):
                continue
            try:
                with open(os.path.join(sessions, entry), encoding="utf-8") as fh:
                    record = json.load(fh)
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            records.append(record)
            if record.get("sessionId") != sid:
                continue
            if record.get("nameSource") != "derived":
                break
            tag = derived_tag(record.get("name"))
            if tag:
                return tag
            break
    except Exception:
        pass
    # Fleet's own snapshot of that tag survives a later rename (F-100a).
    try:
        from fleet.titles import read_tag
        remembered = read_tag(sid, harness="claude")
        if remembered:
            return remembered
        # Keep the derived pane badge across a native resume. The alias lookup
        # is the same display-only seat history Fleet uses, never a cwd guess.
        from fleet.collectors.herdr import pane_session_aliases
        if recipient_pane is _UNSET_RECIPIENT_CONTEXT:
            alias_pane, alias_cwd = os.environ.get("HERDR_PANE_ID"), os.getcwd()
        else:
            alias_pane = recipient_pane
            alias_cwd = None if recipient_cwd is _UNSET_RECIPIENT_CONTEXT else recipient_cwd
        aliases = pane_session_aliases("claude", sid, alias_pane, alias_cwd)
        tags = {derived_tag(record.get("name")) for record in records
                if record.get("sessionId") in aliases and record.get("nameSource") == "derived"}
        tags.update(read_tag(alias, harness="claude") for alias in aliases)
        tags.discard(None)
        return tags.pop() if len(tags) == 1 else None
    except Exception:
        return None


def _agent_home_for_state_root() -> str:
    """Same resolution chain as ``tools/fleet/route.py``'s ``_completion_home`` —
    ``AGENT_HOME``/``CLAUDE_HOME`` first, else the validated resolver found by
    walking up from this file to a checkout's ``utilities/``."""
    h = os.environ.get("AGENT_HOME") or os.environ.get("CLAUDE_HOME")
    if h:
        return h
    here = Path(__file__).resolve()
    for candidate in here.parents:
        utilities_dir = candidate / "utilities"
        if (utilities_dir / "dispatch_contract.py").is_file():
            if str(utilities_dir) not in sys.path:
                sys.path.insert(0, str(utilities_dir))
            from dispatch_contract import resolve_agent_home

            return str(resolve_agent_home())
    return os.path.expanduser("~/.claude")


def _read_claude_pid_record(pid: object) -> Optional[dict]:
    """``~/.claude/sessions/<pid>.json`` → dict, or ``None``. Tolerant by contract
    (mirrors ``collectors/claude.py::read_registry``) — this module is loaded
    standalone by file path from ``statusline.sh`` with every failure swallowed."""
    home = os.environ.get("AGENT_HOME") or os.environ.get("CLAUDE_HOME") or os.path.expanduser("~/.claude")
    try:
        with open(os.path.join(home, "sessions", "%d.json" % int(pid))) as f:
            d = json.load(f)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _session_name_registry_path(harness: str, session_id: str) -> Optional[Path]:
    agent_home = _agent_home_for_state_root()
    here = Path(__file__).resolve()
    for candidate in here.parents:
        utilities_dir = candidate / "utilities"
        if (utilities_dir / "dispatch_contract.py").is_file():
            if str(utilities_dir) not in sys.path:
                sys.path.insert(0, str(utilities_dir))
            from dispatch_contract import dispatch_state_roots

            roots = dispatch_state_roots(
                Path(agent_home), jobs=os.environ.get("AGENT_DISPATCH_JOBS"))
            return Path(roots[0]) / "session-names" / harness / ("%s.json" % session_id)
    return None


def _read_session_name_registry(harness: object, session_id: object) -> Optional[str]:
    """② hearting-owned ``<dispatch state root>/session-names/<harness>/<session_id>.json``
    → ``{name, set_at}["name"]``. Missing/malformed/unreadable → ``None`` (fail-soft)."""
    harness_key = str(harness or "").lower()
    sid = str(session_id or "")
    if not harness_key or not sid:
        return None
    try:
        path = _session_name_registry_path(harness_key, sid)
    except Exception:
        return None
    if path is None:
        return None
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return None
    name = d.get("name") if isinstance(d, dict) else None
    if not isinstance(name, str) or not name:
        return None
    # F-99a ② — uniqueness is not enforced at write time, so a name already claimed
    # by a DIFFERENT session id collides; append the sid8 suffix so the two stay
    # visually distinct rather than silently rendering identical labels.
    try:
        for sibling in path.parent.glob("*.json"):
            if sibling.stem == sid:
                continue
            try:
                other = json.loads(sibling.read_text())
            except Exception:
                continue
            if isinstance(other, dict) and other.get("name") == name:
                return "%s [%s]" % (name, sid[:8])
    except Exception:
        pass
    return name


def resolve_display_inputs(harness: object, session_id: object, *, pid: object = None) -> dict:
    """I/O-bearing companion to ``display_name()`` — resolves the ①②-tier
    ``runtime_name`` (and the native ``registry_name``) so ``display_name()`` itself
    stays a pure function of already-resolved inputs, and so the rule lives in one
    place shared by ``statusline.sh``, Fleet's Claude collector, and the Herdr
    formatter (F-99e). Every failure here is swallowed — a module-top state-root
    import would otherwise raise ``ModuleNotFoundError`` when this module is loaded
    standalone by file path and silently vanish the whole name segment (T-4)."""
    inputs = {"runtime_name": None, "registry_name": None}
    harness_key = str(harness or "").lower()
    if harness_key == "claude" and pid is not None:
        try:
            sj = _read_claude_pid_record(pid)
        except Exception:
            sj = None
        if isinstance(sj, dict):
            name = sj.get("name")
            if isinstance(name, str) and name:
                inputs["registry_name"] = name
                if sj.get("nameSource") != "derived":
                    inputs["runtime_name"] = name
    if not inputs["runtime_name"]:
        try:
            registered = _read_session_name_registry(harness_key, session_id)
        except Exception:
            registered = None
        if registered:
            inputs["runtime_name"] = registered
    return inputs
