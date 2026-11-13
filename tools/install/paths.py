"""Centralized path resolution for the installer.

Honor ``AGENT_HOME`` and ``HOME`` overrides so tests can use temporary homes.
"""

import os
import re
import subprocess
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


def _checkout_of_common_dir(common_dir):
    """The checkout a plain ``<checkout>/.git`` common directory belongs to, else None.

    Only a layout where that name is certain: the directory is called ``.git``, its config
    holds no ``core.worktree`` and does not say ``bare``, and it reads without includes. Any
    other layout (a separate git dir, a bare repository's worktrees, a ``core.worktree``
    setting) and anything unreadable is None, and the caller asks ``git worktree list``.
    """
    try:
        resolved = Path(os.path.realpath(common_dir))
        if resolved.name != ".git":
            return None
        text = (resolved / "config").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line[0] == "[":
            found = re.fullmatch(r"\[\s*([A-Za-z0-9.-]+)\s*\]", line)
            section = found.group(1).lower() if found else "?"
            if section.startswith("include") or (not found and line[1:].lstrip().lower().startswith("include")):
                return None
            continue
        if section == "core":
            key, equals, value = line.partition("=")
            key, value = key.strip().lower(), value.strip().lower()
            if key == "worktree":
                return None
            if key == "bare" and (not equals or value in {"true", "yes", "on", "1"}):
                return None
    primary = resolved.parent
    return primary if primary.is_dir() else None


def _primary_from_worktree_list(root):
    """The first ``git worktree list --porcelain`` entry (the primary), else None."""
    listing = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, timeout=5,
    )
    if listing.returncode != 0:
        return None
    for line in (listing.stdout or "").splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):].strip())
    return None


def primary_checkout(root=None):
    """The repository's PRIMARY worktree, or `root` unchanged when it is not one.

    F-80: linked task worktrees are source-only execution surfaces — the same rule
    `utilities/artifact-root.sh` applies to artifact roots (the first `git worktree list
    --porcelain` entry is the primary). A non-Git tree, an extracted release, or an unavailable
    `git` all return `root` untouched, so a managed release keeps resolving to itself.

    One `git rev-parse` names the common directory; for a plain `<checkout>/.git` layout that
    is the answer. `git worktree list` stats every linked worktree (0.7 s with 200 of them,
    and a start path called it three times), so it is asked only for a layout that rule cannot
    name with certainty (separate git dir, bare repository, `core.worktree`).
    """
    root = Path(root) if root is not None else agent_home()
    try:
        inside = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute",
             "--is-inside-work-tree", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        lines = (inside.stdout or "").splitlines()
        if inside.returncode != 0 or not lines or lines[0].strip() != "true":
            return root
        primary = _checkout_of_common_dir(lines[1].strip()) if len(lines) > 1 else None
        if primary is None:
            primary = _primary_from_worktree_list(root)
            if primary is None:
                return root
            if not primary.is_dir():
                return root
        # Git reports the primary using whatever path the caller reached it by, so
        # the same checkout can be named `/home/<u>/shared_<u>/…` from one worktree and
        # `/mnt/shared/<u>/…` from another. Normalize like `artifact-root.sh`'s
        # `pwd -P`, or the launcher target would depend on where install ran.
        try:
            return primary.resolve(strict=True)
        except (OSError, RuntimeError):
            return primary
    except (OSError, subprocess.SubprocessError):
        return root


def resolve_launcher_source(relpath):
    """Resolve a launcher's source, pinned to the primary checkout.

    F-80 (user 2026-08-16, on the symlink fix being "땜빵의 성격"): `~/.local/bin/*` held
    whatever tree last ran `install`, so the launchers followed a release snapshot on
    2026-08-15 and would have followed a task worktree just as readily — codex was activated
    from one at the time. A launcher outlives any worktree; pinning it to the primary
    checkout is what makes it stop tracking whoever installed last.
    """
    return primary_checkout() / relpath
