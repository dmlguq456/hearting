"""Centralized path resolution for the installer.

Honor ``AGENT_HOME`` and ``HOME`` overrides so tests can use temporary homes.
"""

import os
from pathlib import Path


def _absolute_env_path(name, default):
    raw = os.environ.get(name)
    path = Path(raw).expanduser() if raw else Path(default).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path: {path}")
    return path


def agent_home():
    """Return the active harness root (``AGENT_HOME``).

    Use the environment override when set; otherwise find the nearest source
    root by its portable manifest/core markers. This supports both Git
    checkouts and extracted managed releases.
    """
    env_home = os.environ.get("AGENT_HOME")
    if env_home:
        return Path(env_home)

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (
            (parent / "harness-manifest.json").is_file()
            and (parent / "core/CORE.md").is_file()
        ):
            return parent

    raise RuntimeError(
        "could not resolve agent_home: AGENT_HOME is unset and "
        f"no harness-manifest.json/core/CORE.md root was found above {here}."
    )


def runtime_home(runtime, scope="global"):
    """Return the installation home for a runtime and scope."""
    if runtime == "claude":
        return _absolute_env_path("CLAUDE_CONFIG_DIR", Path.home() / ".claude")
    if runtime == "codex":
        return _absolute_env_path("CODEX_HOME", Path.home() / ".codex")
    if runtime == "opencode":
        if scope == "project":
            return Path.cwd() / ".opencode"
        return xdg_config_home() / "opencode"
    raise ValueError(f"unknown runtime: {runtime!r}")


def opencode_data_home():
    """Return OpenCode's runtime-owned data directory (read-only to installer)."""
    return xdg_data_home() / "opencode"


def xdg_config_home():
    """Freedesktop config root; relative overrides are rejected."""
    return _absolute_env_path("XDG_CONFIG_HOME", Path.home() / ".config")


def hearting_config_dir():
    """User-owned cross-runtime policy; never stored in a runtime's config home."""
    return xdg_config_home() / "hearting"


def xdg_state_home():
    """Freedesktop state root, honoring an explicit test/runtime override."""
    return _absolute_env_path("XDG_STATE_HOME", Path.home() / ".local" / "state")


def xdg_data_home():
    """Freedesktop data root, honoring an explicit test/runtime override."""
    return _absolute_env_path("XDG_DATA_HOME", Path.home() / ".local" / "share")


def extension_state_dir():
    """Harness-owned state for optional external extensions."""
    return xdg_state_home() / "hearting" / "extensions"


def extension_data_dir():
    """Harness-owned immutable snapshots for optional external extensions."""
    return xdg_data_home() / "hearting" / "extensions"


def harness_state_dir(runtime, scope="global"):
    """Return the installer-owned state directory (``<runtime_home>/.harness``)."""
    return runtime_home(runtime, scope) / ".harness"


def resolve_source(relpath):
    """Resolve a repository-relative path to an absolute path."""
    return agent_home() / relpath


def source_exists(relpath):
    """Return whether a source exists, following directory symlinks."""
    return resolve_source(relpath).exists()
