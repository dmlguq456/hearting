"""Resolve the Hearting version and installer method shown by Fleet.

The resolver is deliberately read-only and fail-soft. It accepts installer state only
when that state names the exact Hearting root containing this Fleet, so a stale managed
release or another checkout can never label the current process. Snapshot modes use only
local state. The live worker may additionally refresh a HEAD-exact remote release tag;
that lookup never mutates local refs or installer state.
"""

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path


RUNTIMES = ("claude", "codex", "opencode")
ACTIVATION_MODES = {"linked", "packaged"}
CHANNELS = {"stable", "pinned"}
ACTIVATION_SCHEMA = 2
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MAX_STATE_BYTES = 256 * 1024
_REMOTE_TTL = 300.0
_REMOTE_MISS_TTL = 15.0
_DIRTY_TTL = 5.0
_SEMVER_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_OID_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_REMOTE_CACHE = {}
_DIRTY_CACHE = {}
_REMOTE_LOCK = threading.RLock()


def _root_from_module():
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "harness-manifest.json").is_file() and (parent / "core/CORE.md").is_file():
            return parent
    return here.parents[2]


def _absolute_env_path(env, name, default):
    raw = env.get(name)
    path = Path(raw).expanduser() if raw else Path(default).expanduser()
    return path if path.is_absolute() else None


def _load_json(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_STATE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        return None
    return value if isinstance(value, dict) else None


def _resolved(path):
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _same_root(value, root):
    candidate = _resolved(value) if isinstance(value, str) and value else None
    return candidate is not None and candidate == root


def _valid_version(value):
    clean = value.strip() if isinstance(value, str) else ""
    return clean if _VERSION_RE.fullmatch(clean) else None


def _release_version(root):
    marker = root / "RELEASE_VERSION"
    try:
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 256:
            return None
        return _valid_version(marker.read_text(encoding="utf-8").strip())
    except (OSError, RuntimeError):
        return None


def _git_version(root, runner):
    try:
        result = runner(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=str(root), capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    return _valid_version((getattr(result, "stdout", "") or "").strip())


def _git_dirs(root):
    pointer = root / ".git"
    if pointer.is_dir():
        resolved = pointer.resolve(strict=False)
        return resolved, resolved
    if not pointer.is_file() or pointer.is_symlink():
        return None, None
    try:
        text = pointer.read_text(encoding="utf-8").strip()
        raw = text.split("gitdir:", 1)[1].strip()
        linked = Path(raw)
        if not linked.is_absolute():
            linked = pointer.parent / linked
        linked = linked.resolve(strict=False)
    except (OSError, IndexError, RuntimeError):
        return None, None
    marker = str(Path("worktrees"))
    parts = str(linked).split(os.sep + marker + os.sep, 1)
    main = Path(parts[0]) if len(parts) == 2 else linked
    return linked, main


def _read_oid(path):
    try:
        value = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError, UnicodeError):
        return None
    return value.lower() if _OID_RE.fullmatch(value) else None


def _head_oid(root):
    """Resolve HEAD by reading Git metadata only; no subprocess or ref mutation."""
    linked, main = _git_dirs(root)
    if linked is None:
        return None
    try:
        head = (linked / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if _OID_RE.fullmatch(head):
        return head.lower()
    if not head.startswith("ref: "):
        return None
    ref = head[5:].strip()
    for base in (linked, main):
        oid = _read_oid(base / ref)
        if oid:
            return oid
    try:
        for raw in (main / "packed-refs").read_text(encoding="utf-8").splitlines():
            if raw.startswith(("#", "^")):
                continue
            oid, name = raw.split(" ", 1)
            if name.strip() == ref and _OID_RE.fullmatch(oid):
                return oid.lower()
    except (OSError, ValueError, UnicodeError):
        pass
    return None


def _head_version(root):
    head = _head_oid(root)
    return head[:8] if head else None


def _parse_remote_tags(text, head):
    direct = {}
    peeled = {}
    for raw in str(text or "").splitlines():
        fields = raw.split()
        if len(fields) != 2 or not _OID_RE.fullmatch(fields[0]):
            continue
        ref = fields[1]
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/"):]
        target = peeled if name.endswith("^{}") else direct
        if name.endswith("^{}"):
            name = name[:-3]
        if _SEMVER_TAG_RE.fullmatch(name):
            target[name] = fields[0].lower()
    matches = []
    for name, oid in direct.items():
        if peeled.get(name, oid) == head:
            match = _SEMVER_TAG_RE.fullmatch(name)
            matches.append((tuple(int(value) for value in match.groups()), name))
    return max(matches)[1] if matches else None


def _remote_release(root, runner, now, remote="origin"):
    head = _head_oid(root)
    if not head:
        return None
    key = (str(root), str(remote), head)
    with _REMOTE_LOCK:
        entry = _REMOTE_CACHE.get(key)
        if entry:
            ttl = _REMOTE_TTL if entry.get("version") else _REMOTE_MISS_TTL
            if now - entry["checked_at"] < ttl:
                return entry.get("version")
            if entry.get("inflight"):
                return entry.get("version")
        else:
            entry = {"checked_at": float("-inf"), "version": None, "inflight": False}
            _REMOTE_CACHE[key] = entry
        entry["inflight"] = True

    version = None
    try:
        result = runner(
            ["git", "ls-remote", "--tags", str(remote)],
            cwd=str(root), capture_output=True, text=True, timeout=3,
        )
        if getattr(result, "returncode", 1) == 0:
            version = _parse_remote_tags(getattr(result, "stdout", ""), head)
    except (OSError, subprocess.SubprocessError):
        version = None

    with _REMOTE_LOCK:
        entry = _REMOTE_CACHE[key]
        if version:
            entry["version"] = version
        entry["checked_at"] = now
        entry["inflight"] = False
        return entry.get("version")


def _git_dirty(root, runner, head, now):
    key = (str(root), head)
    with _REMOTE_LOCK:
        cached = _DIRTY_CACHE.get(key)
        if cached and now - cached[0] < _DIRTY_TTL:
            return cached[1]
    dirty = False
    try:
        result = runner(
            ["git", "diff-index", "--quiet", "HEAD", "--"],
            cwd=str(root), capture_output=True, text=True, timeout=2,
        )
        dirty = getattr(result, "returncode", 2) == 1
    except (OSError, subprocess.SubprocessError):
        pass
    with _REMOTE_LOCK:
        _DIRTY_CACHE[key] = (now, dirty)
    return dirty


def _state_paths(env):
    home = _absolute_env_path(env, "HOME", Path.home())
    if home is None:
        return None, []
    state_home = _absolute_env_path(env, "XDG_STATE_HOME", home / ".local/state")
    config_home = _absolute_env_path(env, "XDG_CONFIG_HOME", home / ".config")
    claude_home = _absolute_env_path(env, "CLAUDE_CONFIG_DIR", home / ".claude")
    codex_home = _absolute_env_path(env, "CODEX_HOME", home / ".codex")
    if None in (state_home, config_home, claude_home, codex_home):
        return None, []
    activation = [
        ("claude", claude_home / ".harness/activation.json"),
        ("codex", codex_home / ".harness/activation.json"),
        ("opencode", config_home / "opencode/.harness/activation.json"),
    ]
    return state_home / "hearting/distribution.json", activation


def collect(root=None, env=None, runner=subprocess.run, refresh_remote=False, now=None,
            remote="origin", fast_local=False):
    """Return ``version``/``install_method`` plus bounded provenance for Fleet JSON."""
    env = os.environ if env is None else env
    root = _resolved(root or _root_from_module())
    if root is None:
        return {"version": "unknown", "install_method": "unmanaged",
                "source": "fallback", "runtimes": []}

    distribution_path, activation_paths = _state_paths(env)
    distribution = _load_json(distribution_path) if distribution_path else None
    if distribution and distribution.get("schema") == 1:
        version = _valid_version(distribution.get("version"))
        channel = distribution.get("channel", "stable")
        if (version and channel in CHANNELS
                and _same_root(distribution.get("release_root"), root)):
            runtimes = distribution.get("runtimes") or []
            runtimes = sorted({item for item in runtimes if item in RUNTIMES})
            return {"version": version, "install_method": "managed/" + channel,
                    "source": "distribution", "runtimes": runtimes}

    matches = []
    for runtime, path in activation_paths:
        state = _load_json(path)
        mode = state.get("mode") if state else None
        active_exact = bool(state and _same_root(state.get("active_root"), root))
        if (state and state.get("schema") == ACTIVATION_SCHEMA
                and mode in ACTIVATION_MODES
                and (active_exact
                     or _same_root(state.get("source_root"), root))):
            revision = state.get("active_revision")
            revision = (revision.lower() if active_exact and mode == "packaged"
                        and isinstance(revision, str)
                        and re.fullmatch(r"[0-9a-fA-F]{40}", revision) else None)
            matches.append((runtime, mode, revision))

    modes = {mode for _runtime, mode, _revision in matches}
    method = next(iter(modes)) if len(modes) == 1 else "mixed" if modes else "unmanaged"
    version = None
    if refresh_remote and method in {"linked", "unmanaged"}:
        observed = time.monotonic() if now is None else float(now)
        remote_version = _remote_release(root, runner, observed, remote=remote)
        if remote_version:
            head = _head_oid(root)
            dirty = bool(head and _git_dirty(root, runner, head, observed))
            version = remote_version + ("-dirty" if dirty else "")
    local_version = _release_version(root)
    if local_version is None and not fast_local and method in {"linked", "unmanaged"}:
        local_version = _git_version(root, runner)
    head_version = _head_version(root) if method in {"linked", "unmanaged"} else None
    revisions = {revision for _runtime, mode, revision in matches
                 if mode == "packaged" and revision}
    activation_version = next(iter(revisions))[:8] if method == "packaged" and len(revisions) == 1 else None
    version = version or local_version or head_version or activation_version or "unknown"
    return {"version": version, "install_method": method,
            "source": "activation" if matches else "fallback",
            "runtimes": [runtime for runtime, _mode, _revision in matches]}
