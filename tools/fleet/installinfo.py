"""Resolve the Hearting version and installer method shown by Fleet.

The resolver is deliberately read-only and fail-soft. It accepts installer state only
when that state names the exact Hearting root containing this Fleet, so a stale managed
release or another checkout can never label the current process. ``collect`` is called
once by ``fleet.main``; render ticks only consume the returned dictionary.
"""

import json
import os
import re
import subprocess
from pathlib import Path


RUNTIMES = ("claude", "codex", "opencode")
ACTIVATION_MODES = {"linked", "packaged"}
CHANNELS = {"stable", "pinned"}
ACTIVATION_SCHEMA = 2
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MAX_STATE_BYTES = 256 * 1024


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


def collect(root=None, env=None, runner=subprocess.run):
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
        if (state and state.get("schema") == ACTIVATION_SCHEMA
                and mode in ACTIVATION_MODES
                and (_same_root(state.get("active_root"), root)
                     or _same_root(state.get("source_root"), root))):
            matches.append((runtime, mode))

    modes = {mode for _runtime, mode in matches}
    method = next(iter(modes)) if len(modes) == 1 else "mixed" if modes else "unmanaged"
    version = _release_version(root) or _git_version(root, runner) or "unknown"
    return {"version": version, "install_method": method,
            "source": "activation" if matches else "fallback",
            "runtimes": [runtime for runtime, _mode in matches]}
