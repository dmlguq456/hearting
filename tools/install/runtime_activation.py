#!/usr/bin/env python3
"""Cross-runtime source activation for Codex, Claude Code, and OpenCode.

The activation layer is deliberately offline.  It reads one explicit local
source, writes only harness discovery paths plus ``<runtime-home>/.harness``,
and never invokes a runtime CLI, marketplace, package manager, Git fetch, MCP,
or connector.  A small operation journal restores the previous projection on
any failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import paths

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))
UTILITIES_ROOT = Path(__file__).resolve().parents[2] / "utilities"
if str(UTILITIES_ROOT) not in sys.path:
    sys.path.insert(0, str(UTILITIES_ROOT))

import harness_manifest
import dispatch_contract
import model_config
import projector
import retired_memory_state
import safe_fs
import user_model_config
import codex_tui_config
import herdr_integration


RUNTIMES = ("claude", "codex", "opencode")
MODES = ("linked", "packaged")
SCHEMA = 2
# A harness clears inherited caller names and other harnesses' session ids from its own tool commands
# (core/OPERATIONS.md). Claude's half is these `env` keys (blank values); the Codex half is the
# managed block.
CLAUDE_MANAGED_ENV_KEYS: tuple = (
    "AGENT_DISPATCH_CALLER_HARNESS",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "OPENCODE_SESSION_ID",
)
CLAUDE_STATUSLINE_COMMAND = "bash $HOME/.claude/statusline.sh"

SESSION_ACTIONS = {
    "codex": {
        "instructions": "new-session",
        "skill": "auto-detect-reinvoke",
        "agent": "new-session",
        "hook_config": "new-session",
    },
    "claude": {
        "instructions": "new-session",
        "skill": "reinvoke",
        "agent": "new-session",
        "hook_config": "new-session",
    },
    "opencode": {
        "instructions": "restart-required",
        "skill": "restart-required",
        "agent": "restart-required",
        "hook_config": "restart-required",
    },
}

_IGNORE_NAMES = {
    ".git",
    ".dispatch",
    ".agent_reports",
    ".capability-grounding",
    ".claude_reports",
    ".core-grounding",
    ".route-grounding",
    ".spec-grounding",
    ".harness",
    ".venv",
    "__pycache__",
    "node_modules",
}


class ActivationError(RuntimeError):
    """A safe, user-facing activation failure."""


def _validate_scope(runtime: str, scope: str) -> None:
    if scope not in {"global", "project"}:
        raise ActivationError(f"unsupported activation scope: {scope}")
    if scope == "project":
        raise ActivationError(
            f"project-scoped runtime activation is outside Phase 1 for {runtime}; "
            "use global (legacy project install remains separate)"
        )


def validate_scope(runtime: str, scope: str) -> None:
    """Validate a public activation request without changing runtime state."""
    if runtime not in RUNTIMES:
        raise ActivationError(f"unsupported runtime: {runtime}")
    _validate_scope(runtime, scope)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, data: dict) -> None:
    try:
        payload = json.dumps(
            data, sort_keys=True, ensure_ascii=False, indent=2
        ).encode("utf-8") + b"\n"
        current = safe_fs.capture_state(path)
        auth = safe_fs.authority(
            path,
            owner="runtime-activation:state-json",
            allowed_paths=(path,),
            allow_leaf_symlink=False,
            expected=current,
        )
        safe_fs.atomic_write_bytes(auth, payload, 0o600, create_parents=True)
    except safe_fs.SafetyError as exc:
        raise ActivationError(str(exc)) from exc


def _atomic_bytes(path: Path, data: bytes) -> None:
    try:
        current = safe_fs.capture_state(path)
        auth = safe_fs.authority(
            path,
            owner="runtime-activation:config-bytes",
            allowed_paths=(path,),
            allow_leaf_symlink=False,
            expected=current,
        )
        safe_fs.atomic_write_bytes(auth, data, 0o600, create_parents=True)
    except safe_fs.SafetyError as exc:
        raise ActivationError(str(exc)) from exc


def _load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ActivationError(f"invalid activation state: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ActivationError(f"invalid activation state object: {path}")
    return data


def _state_path(runtime: str, scope: str = "global") -> Path:
    return paths.harness_state_dir(runtime, scope) / "activation.json"


def _ensure_runtime_destination(runtime: str, dest: Path, scope: str) -> None:
    """Reject lexical or symlink-parent escapes from the selected runtime home."""
    home = paths.runtime_home(runtime, scope)
    try:
        dest.relative_to(home)
    except ValueError as exc:
        raise ActivationError(f"destination escapes runtime home: {dest}") from exc
    try:
        resolved_home = home.resolve(strict=False)
        resolved_parent = dest.parent.resolve(strict=False)
    except RuntimeError as exc:
        raise ActivationError(f"destination path contains a symlink cycle: {dest}") from exc
    try:
        resolved_parent.relative_to(resolved_home)
    except ValueError as exc:
        raise ActivationError(f"destination parent escapes runtime home: {dest}") from exc


def _validate_state_dir(runtime: str, scope: str) -> None:
    state_dir = paths.harness_state_dir(runtime, scope)
    if state_dir.is_symlink():
        raise ActivationError(f"harness state directory must not be a symlink: {state_dir}")
    if _state_path(runtime, scope).is_symlink():
        raise ActivationError(
            f"activation state must not be a symlink: {_state_path(runtime, scope)}"
        )
    for name in ("transactions", "bundles", "config-backups", "disabled-plugins"):
        child = state_dir / name
        if child.is_symlink():
            raise ActivationError(f"harness state subdirectory must not be a symlink: {child}")
    _ensure_runtime_destination(runtime, state_dir / "activation.json", scope)


def _real_source(source: Optional[str]) -> Path:
    root = Path(source).expanduser() if source else paths.agent_home()
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ActivationError(f"source does not exist: {root}") from exc
    if not root.is_dir():
        raise ActivationError(f"source is not a directory: {root}")
    return root


def _same_tree(left: Path, right: Path) -> bool:
    """True when two paths name one filesystem object, symlinks resolved."""
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except OSError:
        return False


def _validate_external_plugin_conflicts(runtime: str, scope: str) -> None:
    """Read-only conflict gate for native/plugin activation ownership.

    This runs from ``validate_request`` before the installer creates a launcher
    snapshot, runtime snapshot, lock, transaction, or activation record.  The
    installer never disables or quarantines an external plugin; the operator
    must remove the conflicting entry first.
    """

    if runtime == "codex":
        config = _config_path(runtime, scope, "config.toml")
        if not config.is_file():
            if config.exists():
                raise ActivationError(f"invalid Codex config: {config} is not a regular file")
            return
        try:
            text = config.read_text(encoding="utf-8")
            import tomllib

            data = tomllib.loads(text)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ActivationError(f"invalid Codex config: {config}: {exc}") from exc
        plugins = data.get("plugins", {})
        if not isinstance(plugins, dict):
            raise ActivationError(f"invalid Codex plugin table: {config}")
        for name, table in plugins.items():
            if not isinstance(name, str) or name.split("@", 1)[0] != "hearting-codex":
                continue
            if not isinstance(table, dict):
                raise ActivationError(f"invalid Codex plugin table: {config}")
            enabled = table.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ActivationError(f"invalid Codex plugin enabled value: {config}")
            if enabled:
                raise ActivationError(
                    f"external plugin conflict: hearting-codex is active in {config}"
                )
        return

    if runtime == "claude":
        registry = _claude_plugins_path(scope)
        if registry.is_file():
            try:
                data = json.loads(registry.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ActivationError(
                    f"invalid Claude plugin registry: {registry}: {exc}"
                ) from exc
            if not isinstance(data, dict) or not isinstance(data.get("plugins", {}), dict):
                raise ActivationError(f"invalid Claude plugin registry object: {registry}")
            if any(
                isinstance(key, str) and key.split("@", 1)[0] == "hearting-claude"
                for key in data["plugins"]
            ):
                raise ActivationError(
                    f"external plugin conflict: hearting-claude is active in {registry}"
                )
        elif registry.exists():
            raise ActivationError(
                f"invalid Claude plugin registry: {registry} is not a regular file"
            )

        settings = _config_path(runtime, scope, "settings.json")
        if not settings.is_file():
            if settings.exists():
                raise ActivationError(f"invalid Claude settings: {settings} is not a regular file")
            return
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ActivationError(f"invalid Claude settings: {settings}: {exc}") from exc
        if not isinstance(data, dict):
            raise ActivationError(f"invalid Claude settings object: {settings}")
        enabled_plugins = data.get("enabledPlugins", {})
        if enabled_plugins is None:
            return
        if not isinstance(enabled_plugins, dict):
            raise ActivationError(f"Claude settings enabledPlugins is not an object: {settings}")
        if any(
            isinstance(key, str)
            and key.split("@", 1)[0] == "hearting-claude"
            and value is not False
            for key, value in enabled_plugins.items()
        ):
            raise ActivationError(
                f"external plugin conflict: hearting-claude is active in {settings}"
            )
        return

    for config in _opencode_config_paths(scope):
        if not config.is_file():
            if config.exists():
                raise ActivationError(f"invalid OpenCode config: {config} is not a regular file")
            continue
        data = _read_opencode_config(config)
        for key in ("plugin", "plugins"):
            values = data.get(key, [])
            if values is None:
                continue
            if not isinstance(values, list):
                raise ActivationError(f"invalid OpenCode plugin list: {config}")
            if any(_is_harness_npm_plugin_entry(value) for value in values):
                raise ActivationError(
                    f"external plugin conflict: Hearting npm plugin is active in {config}"
                )


def validate_request(
    runtime: str,
    command: str,
    *,
    mode: Optional[str] = None,
    source: Optional[str] = None,
    scope: str = "global",
) -> dict:
    """Validate a complete public request without creating any state.

    Installer orchestration calls this for every selected runtime before the
    first snapshot or lock.  It intentionally performs reads only.
    """

    if runtime not in RUNTIMES:
        raise ActivationError(f"invalid-before-mutation: unsupported runtime: {runtime}")
    try:
        _validate_scope(runtime, scope)
    except ActivationError as exc:
        raise ActivationError(f"invalid-before-mutation: {exc}") from exc
    if command not in {"activate", "refresh", "status", "doctor"}:
        raise ActivationError(
            f"invalid-before-mutation: unknown runtime command: {command}"
        )
    if command == "activate":
        if mode not in MODES:
            raise ActivationError(
                f"invalid-before-mutation: unsupported activation mode: {mode}"
            )
        try:
            root = _real_source(source)
            _validate_source_symlinks(root)
        except ActivationError as exc:
            raise ActivationError(f"invalid-before-mutation: {exc}") from exc
        _validate_external_plugin_conflicts(runtime, scope)
        return {"runtime": runtime, "command": command, "source": str(root)}
    if command == "refresh":
        try:
            state = _load_json(_state_path(runtime, scope))
        except ActivationError as exc:
            raise ActivationError(f"invalid-before-mutation: {exc}") from exc
        if state is None:
            raise ActivationError(
                f"invalid-before-mutation: {runtime} has no activation state"
            )
        stored_mode = state.get("mode")
        stored_source = state.get("source_root")
        if stored_mode not in MODES or not isinstance(stored_source, str):
            raise ActivationError(
                f"invalid-before-mutation: {runtime} activation state is incomplete"
            )
        try:
            root = _real_source(stored_source)
            _validate_source_symlinks(root)
        except ActivationError as exc:
            raise ActivationError(f"invalid-before-mutation: {exc}") from exc
        _validate_external_plugin_conflicts(runtime, scope)
        return {"runtime": runtime, "command": command, "source": str(root)}
    if mode is not None and mode not in MODES:
        raise ActivationError(
            f"invalid-before-mutation: unsupported activation mode: {mode}"
        )
    if source is not None:
        raise ActivationError(
            f"invalid-before-mutation: {command} does not accept a source"
        )
    return {"runtime": runtime, "command": command}


def _sha_path(
    path: Path, digest: "hashlib._Hash", label: str, stack: Optional[set] = None,
    skip=None,
) -> None:
    """`skip` is an optional extra per-path filter, applied ON TOP of `_IGNORE_NAMES`.

    It defaults to None so every existing caller — notably `_bundle_checksum`, whose
    value is persisted in bundle metadata — keeps its exact previous digest.
    """
    stack = set() if stack is None else stack
    try:
        resolved_key = str(path.resolve(strict=False))
    except RuntimeError:
        digest.update(b"C\0" + label.encode() + b"\0")
        return
    if resolved_key in stack:
        digest.update(b"C\0" + label.encode() + b"\0")
        return
    stack.add(resolved_key)
    if path.is_symlink():
        digest.update(b"L\0" + label.encode() + b"\0" + os.readlink(path).encode())
        try:
            target = path.resolve(strict=False)
        except RuntimeError:
            digest.update(b"C\0" + label.encode() + b"\0")
            stack.remove(resolved_key)
            return
        if target.exists():
            _sha_path(target, digest, label + "@target", stack)
        stack.remove(resolved_key)
        return
    if path.is_file():
        digest.update(b"F\0" + label.encode() + b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        stack.remove(resolved_key)
        return
    if path.is_dir():
        digest.update(b"D\0" + label.encode() + b"\0")
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            if child.name in _IGNORE_NAMES or (skip is not None and skip(child)):
                continue
            _sha_path(child, digest, f"{label}/{child.name}", stack, skip)
    stack.remove(resolved_key)


def _digest_paths(items: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    seen = set()
    for item in sorted((Path(p) for p in items), key=lambda p: str(p)):
        key = str(item.resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        if item.exists() or item.is_symlink():
            _sha_path(item, digest, item.name)
        else:
            digest.update(b"M\0" + str(item).encode())
    return digest.hexdigest()


def _tree_digest(root: Path, skip=None) -> str:
    digest = hashlib.sha256()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.name in _IGNORE_NAMES or (skip is not None and skip(child)):
                continue
            _sha_path(child, digest, child.name, None, skip)
    elif root.exists() or root.is_symlink():
        _sha_path(root, digest, root.name, None, skip)
    else:
        digest.update(b"M\0" + str(root).encode())
    return digest.hexdigest()


def _release_content_skip(root: Path) -> Callable[[Path], bool]:
    """One top-level filter shared by cleanliness, copy, and digest.

    A root-level, untracked item (e.g. an unversioned `dist/` build output
    sitting beside the checkout) is not release content: it does not belong
    to the launch/dirty judgment, the packaged copy, or the bundle key. An
    untracked file inside a tracked source directory is unaffected -- it
    stays dirty and stays in the digest, in every mode. Not a git worktree at
    all means there is nothing to distinguish, so nothing is skipped.
    """
    if _git(["rev-parse", "--is-inside-work-tree"], root) != "true":
        return lambda _path: False
    tracked = (_git(["ls-files"], root) or "").splitlines()
    tracked_top = {Path(name).parts[0] for name in tracked if name}
    root = Path(root)

    def _skip(path: Path) -> bool:
        try:
            rel = path.relative_to(root)
        except ValueError:
            return False
        if not rel.parts:
            return False
        # The top-level component decides it, however deep `path` itself is:
        # `git status --untracked-files=all` lists an untracked directory's
        # files individually rather than the directory, so an unversioned
        # `dist/a.tgz` must be judged by `dist`, not by its own name.
        return rel.parts[0] not in tracked_top

    return _skip


def _git(runtime_args: Sequence[str], root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *runtime_args], cwd=str(root), capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def source_revision(root: Path, *, runtime_launch: bool = False) -> str:
    # `runtime_launch` is kept only for caller compatibility (e.g.
    # utilities/capability-route.py). The root-level filter below now applies
    # the same way regardless of its value -- see `_release_content_skip`.
    del runtime_launch
    release_marker = root / "RELEASE_VERSION"
    if release_marker.is_file() and not release_marker.is_symlink():
        try:
            version = release_marker.read_text(encoding="utf-8").strip()
        except OSError:
            version = ""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
            return f"release:{version}:{_tree_digest(root)[:12]}"
    head = _git(["rev-parse", "HEAD"], root)
    if not head:
        return "tree:" + _tree_digest(root)[:20]
    dirty = _git(["status", "--porcelain=v1", "--untracked-files=all"], root) or ""
    # A checkout identifies its versioned source plus additions to existing
    # source directories. Root-level, unversioned work outputs (e.g. an
    # unversioned `dist/` build directory) are not executable release
    # content, in either the launch checkout or the packaged copy.
    skip = _release_content_skip(root)
    dirty = "\n".join(
        line for line in dirty.splitlines()
        if not line.startswith("?? ") or not skip(root / line[3:].split(" -> ")[-1])
    )
    if not dirty:
        return head
    digest = hashlib.sha256(dirty.encode())
    for line in dirty.splitlines():
        rel = line[3:].split(" -> ")[-1]
        candidate = root / rel
        if candidate.exists() and candidate.is_file():
            try:
                digest.update(candidate.read_bytes())
            except OSError:
                pass
    return f"{head}+dirty:{digest.hexdigest()[:12]}"


def _entry(source: Path, dest: Path, surface: str, kind: str = "symlink") -> dict:
    return {
        "source": str(source),
        "dest": str(dest),
        "surface": surface,
        "kind": kind,
    }


def _children(
    source: Path,
    dest: Path,
    surface: str,
    pattern: str = "*",
    allowed: Optional[set[str]] = None,
    exclude: Optional[set[str]] = None,
) -> List[dict]:
    if not source.is_dir():
        return []
    entries = []
    for item in sorted(source.glob(pattern)):
        if exclude is not None and item.name in exclude:
            continue
        identifier = item.stem if item.is_file() else item.name
        if allowed is not None and identifier not in allowed:
            continue
        entries.append(_entry(item, dest / item.name, surface))
    return entries


def _mode_entries(source: Path, dest: Path) -> List[dict]:
    """Project every mode file individually onto its adapter-relative path."""
    if not source.is_dir():
        return []
    entries = []
    for item in sorted(source.glob("*/*.md")):
        entries.append(_entry(item, dest / item.relative_to(source), "agent"))
    return entries


def _kernel_agents(source_root: Path) -> set[str]:
    manifest_path = source_root / harness_manifest.MANIFEST_NAME
    if not manifest_path.is_file():
        return {"memory-scout"}
    try:
        canonical = harness_manifest.load(manifest_path)
    except harness_manifest.ManifestError as exc:
        raise ActivationError(f"invalid canonical manifest: {exc}") from exc
    return set(canonical["kernel"]["agents"])


def _native_agent_catalog(source_root: Path, runtime: str) -> set[str]:
    """Native subagent type names declared by the adapter's model config.

    `CFG_NATIVE_AGENT_CATALOG` is the source of truth that each adapter's
    sync-native-agents.py generator turns into one agent definition carrying a
    derived model+effort pin. Those definitions ship in every release, so an
    activation that projects only the kernel helpers installs none of them.
    """

    config = source_root / "adapters" / runtime / "config" / "models.conf"
    names: set[str] = set()
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return names
    for line in text.splitlines():
        if not line.strip().startswith("CFG_NATIVE_AGENT_CATALOG="):
            continue
        raw = line.split("=", 1)[1].strip().strip('"').strip("'")
        for token in raw.split():
            name, separator, profile = token.partition(":")
            if name and separator and profile:
                names.add(name)
    return names


def _linked_entries(
    runtime: str,
    source_root: Path,
    scope: str = "global",
) -> List[dict]:
    home = paths.runtime_home(runtime, scope)
    entries: List[dict] = []
    # Kernel helpers plus the adapter's native subagent type catalog; both are
    # projected into every activation. Retired team agents stay excluded.
    projected_agents = _kernel_agents(source_root) | _native_agent_catalog(
        source_root, runtime
    )

    if runtime == "codex":
        fixed = [
            (source_root, home / "hearting", "instructions"),
            (source_root / "adapters/codex/AGENTS.md", home / "AGENTS.md", "instructions"),
            (source_root / "core", home / "agent-core", "instructions"),
            (source_root / "capabilities", home / "agent-capabilities", "instructions"),
            (source_root / "roles", home / "agent-roles", "instructions"),
            (source_root / "adapters/codex/bin", home / "agent-bin", "hook_config"),
            (source_root / "adapters/codex/hooks", home / "agent-hooks", "hook_config"),
        ]
        entries.extend(_entry(src, dst, surface) for src, dst, surface in fixed)
        entries.append(_entry(source_root / "adapters/codex/hooks/herdr-agent-state.sh",
                              home / "herdr-agent-state.sh", "hook_config", kind="copy"))
        # A real hooks.json is user-owned. The Codex driver and Herdr migration
        # merge their exact owned entries in place; projecting a whole-file
        # symlink would discard unrelated callbacks and comments.
        hooks_json = home / "hooks.json"
        if not (hooks_json.exists() and not hooks_json.is_symlink()):
            entries.append(_entry(source_root / "adapters/codex/hooks/hooks.json",
                                  hooks_json, "hook_config"))
        entries.extend(
            _mode_entries(
                source_root / "adapters/codex/modes",
                home / "agent-modes",
            )
        )
        entries.extend(
            _children(
                source_root / "adapters/codex/skills",
                home / "skills",
                "skill",
            )
        )
        entries.extend(
            _children(
                source_root / "adapters/codex/agents",
                home / "agents",
                "agent",
                "*.toml",
                allowed=projected_agents,
            )
        )

    elif runtime == "claude":
        fixed = [
            (source_root, home / "hearting", "instructions"),
            (source_root / "adapters/claude/CLAUDE.md", home / "CLAUDE.md", "instructions"),
            (source_root / "core", home / "core", "instructions"),
            (source_root / "capabilities", home / "capabilities", "instructions"),
            (source_root / "roles", home / "roles", "instructions"),
            (source_root / "adapters/claude/bin", home / "bin", "hook_config"),
            (source_root / "adapters/claude/tools", home / "tools", "hook_config"),
            (source_root / "adapters/claude/utilities", home / "utilities", "hook_config"),
            (source_root / "adapters/claude/scaffolds", home / "scaffolds", "hook_config"),
            (source_root / "adapters/claude/statusline.sh", home / "statusline.sh", "hook_config"),
            # Owner-name-set union (INSTALL_LAYOUT.md "owner-name-set
            # reconciliation"): `harness install claude` also projects these
            # three names (projector._CLAUDE_SYMLINK_NAMES). Once activation
            # exists it takes over the union so the same active_root/bundle
            # backs every harness-owned Claude symlink instead of leaving a
            # mixed layout where some names still point at the installer's
            # separate `claude_setting/` projection.
            (source_root / "README.md", home / "README.md", "instructions"),
            (source_root / "manifest.json", home / "manifest.json", "instructions"),
            (source_root / "adapters/claude/loops", home / "loops", "hook_config"),
        ]
        entries.extend(_entry(src, dst, surface) for src, dst, surface in fixed)
        # agent-modes runtime surface retired with the team agents: the unit catalog is
        # already projected through the wholesale `roles` entry (home/roles/units).
        entries.extend(
            _children(
                source_root / "adapters/claude/skills",
                home / "skills",
                "skill",
            )
        )
        entries.extend(
            _children(
                source_root / "adapters/claude/agents",
                home / "agents",
                "agent",
                "*.md",
                allowed=projected_agents,
            )
        )
        entries.extend(
            _children(source_root / "adapters/claude/commands", home / "commands", "skill", "*.md")
        )
        entries.extend(
            _children(source_root / "adapters/claude/hooks", home / "hooks", "hook_config",
                      exclude={"herdr-agent-state.sh"})
        )
        entries.append(_entry(source_root / "hooks/herdr-agent-state.sh",
                              home / "hooks/herdr-agent-state.sh", "hook_config", kind="copy"))

    elif runtime == "opencode":
        fixed = [
            (source_root, home / "hearting", "instructions"),
            (source_root / "adapters/opencode/AGENTS.md", home / "AGENTS.md", "instructions"),
            (source_root / "core", home / "agent-core", "instructions"),
            (source_root / "capabilities", home / "agent-capabilities", "instructions"),
            (source_root / "roles", home / "agent-roles", "instructions"),
        ]
        entries.extend(_entry(src, dst, surface) for src, dst, surface in fixed)
        entries.extend(
            _children(
                source_root / "adapters/opencode/skills",
                home / "skills",
                "skill",
            )
        )
        agent_root = source_root / "adapters/opencode/agents"
        if agent_root.is_dir():
            for item in sorted(agent_root.glob("*/*.md")):
                if item.parent.name not in projected_agents:
                    continue
                entries.append(_entry(item, home / "agents" / item.name, "agent"))
        entries.extend(
            _children(
                source_root / "adapters/opencode/commands",
                home / "commands",
                "skill",
                "*.md",
            )
        )
        plugin = source_root / "adapters/opencode/plugins/hearting-guards.js"
        entries.append(_entry(plugin, home / "plugins/hearting-guards.js", "hook_config"))
        v2_plugin = source_root / "adapters/opencode/plugins/hearting-guards/index.ts"
        entries.append(_entry(v2_plugin, home / "plugins/hearting-guards/index.ts", "hook_config"))
        herdr_plugin = source_root / "adapters/opencode/plugins/herdr-agent-state.js"
        entries.append(_entry(herdr_plugin, home / "plugins/herdr-agent-state.js", "hook_config", kind="copy"))
        tui_entry = source_root / "adapters/opencode/tui/hearting-tui-identity.ts"
        entries.append(_entry(tui_entry, home / "tui/hearting-tui-identity.ts", "hook_config"))
        herdr_tui = source_root / "adapters/opencode/tui/herdr-tui-session.js"
        entries.append(_entry(herdr_tui, home / "herdr-tui-session.js", "hook_config", kind="copy"))
        herdr_tui_v2 = source_root / "adapters/opencode/tui/herdr-opencode/tui.js"
        entries.append(_entry(herdr_tui_v2, home / "herdr-opencode/tui.js", "hook_config", kind="copy"))
        # Owned scoped TUI config: dormant until an owned launch points
        # OPENCODE_TUI_CONFIG at it. Never auto-read (the loader only reads
        # tui.json/tui.jsonc by name), never merged into user files.
        owned_tui = source_root / "adapters/opencode/tui/owned-tui.json"
        entries.append(_entry(owned_tui, home / "tui/hearting-owned-tui.json", "hook_config"))
        owned_cli = source_root / "adapters/opencode/tui/owned-cli.json"
        entries.append(_entry(owned_cli, home / "tui/hearting-owned-cli.json", "hook_config"))
    else:
        raise ActivationError(f"unsupported runtime: {runtime}")

    existing = [entry for entry in entries if Path(entry["source"]).exists()]
    if runtime == "claude" and not (
        source_root / "adapters/claude/statusline.sh"
    ).is_file():
        raise ActivationError(
            f"source {source_root} lacks Claude statusline.sh activation surface"
        )
    required_surfaces = {"instructions", "skill", "agent", "hook_config"}
    present_surfaces = {entry["surface"] for entry in existing}
    missing = sorted(required_surfaces - present_surfaces)
    if missing:
        raise ActivationError(
            f"source {source_root} lacks {runtime} activation surfaces: {','.join(missing)}"
        )
    return existing


def _bundle_ignore(source_root: Path, skip: Callable[[Path], bool]) -> Callable[[str, List[str]], set]:
    # `copytree`'s `ignore` is called once per visited directory; the release-
    # content filter only applies at the root call (`skip` already refuses
    # anything below the top level), matching `_release_content_skip`.
    root_str = str(source_root)

    def _ignore(directory: str, names: List[str]) -> set:
        ignored = {name for name in names if name in _IGNORE_NAMES}
        if directory == root_str:
            ignored.update(name for name in names if skip(source_root / name))
        return ignored

    return _ignore


def _validate_source_symlinks(source_root: Path) -> None:
    """Activation sources may keep relative in-repo links, never outside links."""
    for directory, dirnames, filenames in os.walk(source_root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in _IGNORE_NAMES]
        for name in list(dirnames) + filenames:
            item = Path(directory) / name
            if not item.is_symlink():
                continue
            raw = os.readlink(item)
            if os.path.isabs(raw):
                raise ActivationError(f"activation source has absolute symlink: {item} -> {raw}")
            try:
                target = item.resolve(strict=False)
            except RuntimeError as exc:
                raise ActivationError(f"activation source has a symlink cycle: {item}") from exc
            try:
                target.relative_to(source_root)
            except ValueError as exc:
                raise ActivationError(
                    f"activation source symlink escapes source root: {item} -> {raw}"
                ) from exc


def _managed_release_source(source_root: Path, revision: str) -> bool:
    """True when the activation source is an immutable managed release.

    `source_revision()` returns a `release:<version>:<digest>` revision only for a
    tree carrying a real `RELEASE_VERSION` marker, which is exactly the shape the
    release publisher produces and never what a dev checkout produces. That is the
    one signal for "this tree is already immutable and versioned", so it is also
    the condition under which copying it a second time buys nothing.
    """
    return revision.startswith("release:") and (source_root / "RELEASE_VERSION").is_file()


# destructive-ok: reason=publish or discard one immutable runtime bundle staging tree; boundary=one revision bundle and sibling staging directory below runtime state
def _build_bundle(runtime: str, source_root: Path, revision: str, scope: str) -> Path:
    """Materialize the packaged runtime root for one revision.

    An immutable managed release is *linked*, not copied. `packaged` was designed
    (e681b454, 2026-07-14) to give a runtime a checkout-independent immutable root
    back when the only alternative was a live dev checkout; the managed release
    tree now provides exactly that, per version, on its own. Copying it again
    produced a second path for one content, and `resolve_agent_home()` ranks the
    runtime's own bundle pointer above the managed `current` pointer -- so codex
    and opencode resolved AGENT_HOME to the bundle while the dispatch registry
    sealed `launch_home` at the release, and every launch without an explicit
    AGENT_HOME failed `launch-runtime-root-mismatch` (defect Q). The validity test
    asked "is this a hearting root?" and never "is it the same root everyone else
    agreed on".

    A link makes those two paths one filesystem object, so the question cannot be
    answered two ways. A dev checkout keeps the copy: it is mutable, so a bundle
    still has to freeze it.
    """
    if "+dirty:" in revision:
        raise ActivationError("packaged activation refuses a dirty git source")
    state_dir = paths.harness_state_dir(runtime, scope)
    _validate_source_symlinks(source_root)
    skip = _release_content_skip(source_root)
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", revision)[:48]
    key += "-" + _tree_digest(source_root, skip=skip)[:12]
    bundle = state_dir / "bundles" / key
    bundle_source = bundle / "source"
    metadata_path = bundle / "bundle.json"
    linked = _managed_release_source(source_root, revision)
    _prune_dead_release_bundles(runtime, scope, bundle)
    if metadata_path.exists() and bundle_source.is_dir():
        metadata = _load_json(metadata_path) or {}
        if bool(metadata.get("source_link")) == linked and (
            metadata.get("source_revision") == revision
        ):
            if linked:
                # The link target is the identity; re-walking the tree would only
                # re-derive the release digest the key already carries.
                if bundle_source.is_symlink() and _same_tree(bundle_source, source_root):
                    return bundle_source
            elif metadata.get("checksum") == _tree_digest(bundle_source):
                return bundle_source

    staging = state_dir / "bundles" / (".staging-" + uuid.uuid4().hex)
    staging_source = staging / "source"
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        if linked:
            staging.mkdir(parents=True, exist_ok=True)
            os.symlink(source_root, staging_source, target_is_directory=True)
        else:
            shutil.copytree(
                source_root,
                staging_source,
                symlinks=True,
                ignore=_bundle_ignore(source_root, skip),
            )
        _atomic_json(
            staging / "bundle.json",
            {
                "schema": SCHEMA,
                "runtime": runtime,
                "source_root": str(source_root),
                "source_revision": revision,
                "source_link": linked,
                "checksum": _tree_digest(staging_source),
                "created_at": _utc_now(),
                "external_dependencies": [],
            },
        )
        if bundle.exists():
            # rmtree never follows a symlink into its target, so discarding a
            # linked bundle removes the link and leaves the release alone.
            shutil.rmtree(bundle)
        os.replace(staging, bundle)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return bundle_source


def _prune_dead_release_bundles(runtime: str, scope: str, keep: Path) -> None:
    """Retire linked bundles whose managed release no longer exists.

    A linked bundle is one symlink to its release, so once that release is
    removed the bundle holds nothing but a dangling link. Only a plain bundle
    directory that this activation marked `source_link` is eligible; copied
    bundles, staging directories, foreign entries, bundles whose link still
    resolves and the recorded active bundle are left alone. Cleanup is best
    effort and never fails an activation.
    """
    try:
        state_dir = paths.harness_state_dir(runtime, scope)
        bundles = state_dir / "bundles"
        if not bundles.is_dir() or bundles.is_symlink():
            return
        active = (_load_json(_state_path(runtime, scope)) or {}).get("active_root")
        active_bundle = Path(active).parent if isinstance(active, str) and active else None
        keep_root = keep.resolve(strict=False)
        active_root = active_bundle.resolve(strict=False) if active_bundle else None
        for entry in sorted(bundles.iterdir()):
            try:
                if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                    continue
                if entry.resolve(strict=False) in {keep_root, active_root}:
                    continue
                source = entry / "source"
                metadata_path = entry / "bundle.json"
                if not source.is_symlink() or metadata_path.is_symlink() or not metadata_path.is_file():
                    continue
                if {child.name for child in entry.iterdir()} != {"source", "bundle.json"}:
                    continue
                metadata = _load_json(metadata_path)
                if not metadata or (
                    metadata.get("schema") != SCHEMA
                    or metadata.get("runtime") != runtime
                    or metadata.get("source_link") is not True
                    or not isinstance(metadata.get("source_root"), str)
                    or not isinstance(metadata.get("source_revision"), str)
                    or not metadata["source_revision"].startswith("release:")
                    or not isinstance(metadata.get("checksum"), str)
                ):
                    continue
                if source.resolve(strict=False) != Path(metadata["source_root"]).resolve(strict=False):
                    continue
                try:
                    source.stat()
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
                else:
                    continue
                # destructive-ok: reason=retire one linked runtime bundle whose release was removed; boundary=one real bundle directory below runtime state bundles holding only its exact source and bundle metadata entries
                _remove_path(entry)
            except Exception:
                continue
    except Exception:
        return


def _desired_entries(
    runtime: str,
    mode: str,
    source_root: Path,
    active_root: Path,
    revision: str,
    scope: str,
) -> List[dict]:
    # Both modes use native runtime discovery.  Only the source changes: live
    # repo for linked, immutable local bundle for packaged.
    return _linked_entries(runtime, active_root, scope)


def _native_present(runtime: str, scope: str = "global") -> bool:
    home = paths.runtime_home(runtime, scope)
    state = _load_json(_state_path(runtime, scope))
    if state:
        for item in state.get("owned_paths", []):
            dest = Path(item.get("dest", ""))
            if dest.is_symlink() and item.get("surface") in {
                "instructions", "skill", "agent", "hook_config"
            }:
                return True
    candidates = []
    if runtime == "codex":
        candidates.extend((home / "skills").glob("*"))
        candidates.extend((home / "agents").glob("*.toml"))
    elif runtime == "claude":
        candidates.extend((home / "skills").glob("*"))
        candidates.extend((home / "agents").glob("*.md"))
    elif runtime == "opencode":
        candidates.extend((home / "skills").glob("*"))
        candidates.append(home / "plugins/hearting-guards.js")
        candidates.append(home / "plugins/hearting-guards/index.ts")
    for candidate in candidates:
        if not candidate.is_symlink():
            continue
        if _native_harness_target(runtime, candidate):
            return True
    return False


def _native_harness_target(runtime: str, candidate: Path) -> bool:
    """Recognize only a canonical harness projection, never a path substring."""
    try:
        target = candidate.resolve(strict=False)
    except RuntimeError:
        return False
    for root in (target, *target.parents):
        if not (root / "harness-manifest.json").is_file():
            continue
        try:
            relative = target.relative_to(root).parts
        except ValueError:
            continue
        allowed = {
            "codex": (
                ("adapters", "codex", "skills"),
                ("adapters", "codex", "agents"),
                ("adapters", "codex", "modes"),
                ("codex_setting", "codex-skills"),
                ("codex_setting", "codex-agents"),
                ("codex_setting", "codex-modes"),
            ),
            "claude": (
                ("adapters", "claude", "skills"),
                ("adapters", "claude", "agents"),
                ("adapters", "claude", "agent-modes"),
                ("claude_setting", "skills"),
                ("claude_setting", "agents"),
            ),
            "opencode": (
                ("adapters", "opencode", "skills"),
                ("adapters", "opencode", "agents"),
                ("adapters", "opencode", "commands"),
                ("adapters", "opencode", "plugins"),
            ),
        }[runtime]
        if any(relative[: len(prefix)] == prefix for prefix in allowed):
            return True
    return False


def _discovered_harness_links(runtime: str, scope: str = "global") -> set[Path]:
    """Find bounded native links left by this or an older harness installer."""
    home = paths.runtime_home(runtime, scope)
    patterns = {
        "codex": ("skills/*", "agents/*.toml", "agent-modes/*/*.md"),
        "claude": ("skills/*", "agents/*.md", "agent-modes/*/*.md"),
        "opencode": ("skills/*", "agents/*.md", "commands/*.md",
                     "plugins/hearting-guards/index.ts"),
    }[runtime]
    found: set[Path] = set()
    for pattern in patterns:
        for candidate in home.glob(pattern):
            if candidate.is_symlink() and _native_harness_target(runtime, candidate):
                found.add(candidate)
    return found


def _unexpected_harness_links(
    runtime: str, desired: List[dict], scope: str = "global"
) -> List[Path]:
    desired_paths = {Path(item["dest"]) for item in desired}
    return sorted(
        _discovered_harness_links(runtime, scope) - desired_paths,
        key=lambda item: str(item),
    )


def _opencode_config_paths(scope: str = "global") -> List[Path]:
    result = []
    directory = paths.runtime_home("opencode", scope)
    for name in ("opencode.jsonc", "opencode.json"):
        config = directory / name
        if config.is_symlink():
            raise ActivationError(f"runtime config must not be a symlink: {config}")
        result.append(config)
    return result


def _opencode_npm_present(scope: str = "global") -> bool:
    for config in _opencode_config_paths(scope):
        if not config.is_file():
            continue
        data = _read_opencode_config(config)
        for key in ("plugin", "plugins"):
            values = data.get(key)
            if isinstance(values, list) and any(
                _is_harness_npm_plugin_entry(value) for value in values
            ):
                return True
    return False


def _opencode_jsonc_harness_present(scope: str = "global") -> bool:
    for config in _opencode_config_paths(scope):
        if config.suffix != ".jsonc" or not config.is_file():
            continue
        data = _read_opencode_config(config)
        for key in ("plugin", "plugins"):
            values = data.get(key)
            if isinstance(values, list) and any(
                _is_harness_npm_plugin_entry(value) for value in values
            ):
                return True
    return False


def _jsonc_without_comments(text: str) -> str:
    """Remove JSONC comments without interpreting comment text as config."""
    output: List[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] == "/":
            output.extend((" ", " "))
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] == "*":
            output.extend((" ", " "))
            index += 2
            closed = False
            while index < len(text):
                if text[index] == "*" and index + 1 < len(text) and text[index + 1] == "/":
                    output.extend((" ", " "))
                    index += 2
                    closed = True
                    break
                output.append(text[index] if text[index] in "\r\n" else " ")
                index += 1
            if not closed:
                raise ActivationError("invalid OpenCode JSONC: unterminated block comment")
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _json_without_trailing_commas(text: str) -> str:
    output: List[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _read_opencode_config(config: Path) -> dict:
    try:
        text = config.read_text(encoding="utf-8")
        if config.suffix == ".jsonc":
            text = _json_without_trailing_commas(_jsonc_without_comments(text))
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationError(f"invalid OpenCode config: {config}: {exc}") from exc
    if not isinstance(data, dict):
        raise ActivationError(f"invalid OpenCode config object: {config}")
    return data


def _opencode_plugin_name(value) -> Optional[str]:
    if isinstance(value, str):
        return value
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], dict)
    ):
        return value[0]
    return None


def _is_harness_npm_plugin_entry(value) -> bool:
    name = _opencode_plugin_name(value)
    return name is not None and _is_harness_npm_plugin(name)


def _is_harness_npm_plugin(value: str) -> bool:
    token = value.rstrip("/").rsplit("/", 1)[-1].lower()
    return bool(re.match(r"^hearting(?:-opencode)?(?:@[^/]*)?$", token))


def _config_path(runtime: str, scope: str, *parts: str) -> Path:
    path = paths.runtime_home(runtime, scope).joinpath(*parts)
    _ensure_runtime_destination(runtime, path, scope)
    if path.is_symlink() and not (runtime == "codex" and parts == ("config.toml",)):
        raise ActivationError(f"runtime config must not be a symlink: {path}")
    return path


def _ensure_owned_destination(runtime: str, dest: Path, scope: str) -> None:
    if runtime == "codex":
        config = paths.runtime_home(runtime, scope) / "config.toml"
        if config.is_symlink():
            try:
                if dest == config.resolve(strict=False):
                    return
            except (OSError, RuntimeError) as exc:
                raise ActivationError(f"cannot resolve Codex config target: {config}: {exc}") from exc
    _ensure_runtime_destination(runtime, dest, scope)


def _codex_plugin_ranges(lines: List[str]) -> List[tuple[int, int]]:
    header = re.compile(r'^\s*\[plugins\."?([^"\]]+)"?\]\s*(?:#.*)?$')
    table = re.compile(r"^\s*\[")
    starts = []
    for index, line in enumerate(lines):
        match = header.match(line.rstrip("\r\n"))
        if match and match.group(1).split("@", 1)[0] == "hearting-codex":
            starts.append(index)
    ranges = []
    for start in starts:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if table.match(lines[index]):
                end = index
                break
        ranges.append((start, end))
    return ranges


def _codex_plugin_active(scope: str = "global") -> bool:
    config = _config_path("codex", scope, "config.toml")
    if not config.is_file():
        return False
    try:
        lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return False
    enabled = re.compile(r"^\s*enabled\s*=\s*(true|false)\b", re.IGNORECASE)
    for start, end in _codex_plugin_ranges(lines):
        values = [enabled.match(lines[index]) for index in range(start + 1, end)]
        values = [match.group(1).lower() for match in values if match]
        if not values or values[-1] == "true":
            return True
    return False


def _claude_plugins_path(scope: str = "global") -> Path:
    return _config_path("claude", scope, "plugins", "installed_plugins.json")


def _claude_plugin_active(scope: str = "global") -> bool:
    registry = _claude_plugins_path(scope)
    if registry.is_file():
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = None
        plugins = data.get("plugins", {}) if isinstance(data, dict) else {}
        if isinstance(plugins, dict) and any(
            key.split("@", 1)[0] == "hearting-claude" for key in plugins
        ):
            return True
    settings = _config_path("claude", scope, "settings.json")
    if not settings.is_file():
        return False
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    enabled = data.get("enabledPlugins", {}) if isinstance(data, dict) else {}
    return isinstance(enabled, dict) and any(
        key.split("@", 1)[0] == "hearting-claude" and value is not False
        for key, value in enabled.items()
    )


def _claude_hook_source(active_root: Path) -> Path:
    source = active_root / "adapters/claude/settings.json"
    if not source.is_file():
        raise ActivationError(f"Claude hook settings source missing: {source}")
    return source


def _read_json_object(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ActivationError(f"invalid {label} object: {path}")
    return data


def _claude_managed_values(source: dict) -> dict:
    statusline = source.get("statusLine")
    if (
        not isinstance(statusline, dict)
        or statusline.get("type") != "command"
        or statusline.get("command") != CLAUDE_STATUSLINE_COMMAND
        or (
            "refreshInterval" in statusline
            and (
                not isinstance(statusline["refreshInterval"], int)
                or isinstance(statusline["refreshInterval"], bool)
                or statusline["refreshInterval"] <= 0
            )
        )
    ):
        raise ActivationError("Claude settings source has no valid statusLine command")
    auto_memory_enabled = source.get("autoMemoryEnabled")
    if not isinstance(auto_memory_enabled, bool):
        raise ActivationError("Claude settings source has no valid autoMemoryEnabled")
    managed_env = {}
    if CLAUDE_MANAGED_ENV_KEYS:
        source_env = source.get("env")
        if not isinstance(source_env, dict):
            raise ActivationError("Claude settings source has no env object")
        for key in CLAUDE_MANAGED_ENV_KEYS:
            value = source_env.get(key)
            if not isinstance(value, str):
                raise ActivationError(f"Claude settings source has no valid env.{key}")
            managed_env[key] = value
    return {
        "statusLine": statusline,
        "autoMemoryEnabled": auto_memory_enabled,
        "env": managed_env,
    }


def _hook_keys(item) -> List[str]:
    """Which hooks a group runs: one key per hook, its matcher plus command.

    Timeout, async and other per-hook knobs are the user's to tune, and a user
    may drop one hook from a group. Comparing whole groups made any such group
    look foreign, so each install appended the template copy again and the
    hooks ran twice.
    """
    if not isinstance(item, dict):
        return [json.dumps(item, sort_keys=True)]
    hooks = item.get("hooks")
    return [
        json.dumps([
            item.get("matcher"),
            hook.get("command", "") if isinstance(hook, dict) else json.dumps(hook, sort_keys=True),
        ])
        for hook in (hooks if isinstance(hooks, list) else [])
    ]


def _event_hook_keys(entries) -> set:
    return {key for item in (entries if isinstance(entries, list) else []) for key in _hook_keys(item)}


def _without_hooks(entries: list, drop: set) -> list:
    """Remove the hooks whose key is in drop; a group left empty goes too."""
    kept = []
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("hooks"), list):
            if not set(_hook_keys(item)) & drop:
                kept.append(item)
            continue
        hooks = [
            hook
            for hook, key in zip(item["hooks"], _hook_keys(item))
            if key not in drop
        ]
        if len(hooks) == len(item["hooks"]):
            kept.append(item)
        elif hooks:
            kept.append(dict(item, hooks=hooks))
    return kept


def _merge_claude_settings(
    active_root: Path, previous: Optional[dict], scope: str = "global", *,
    context_window: str | None = None, context_only: bool = False,
    config_path: Path | None = None, native_locked: bool = False,
) -> dict:
    if context_only:
        config = config_path or _config_path("claude", scope, "settings.json")
        key = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
        desired = str(context_window) if context_window is not None else None
        def read_settings():
            data = _read_json_object(config, "Claude settings") if config.exists() else {}
            env = data.get("env", {})
            if not isinstance(env, dict):
                raise ActivationError(f"Claude settings env is not an object: {config}")
            return data, key in env, env.get(key), "env" in data

        data, present, before, env_present = read_settings()
        changed = desired is not None and before != desired
        if changed:
            if not native_locked:
                with safe_fs.TargetLock(config):
                    data, present, before, env_present = read_settings()
                    changed = before != desired
                    if changed:
                        env = data.get("env", {})
                        env[key] = desired
                        data["env"] = env
                        _atomic_json(config, data)
            else:
                env = data.get("env", {})
                env[key] = desired
                data["env"] = env
                _atomic_json(config, data)
        from native_context import _native_key_record
        record = (_native_key_record(previous, "claude", present, before, desired,
                                     extra={"env_present": env_present})
                  if desired is not None else None)
        return {"kind": "claude-context-window-projected", "path": str(config),
                "previous_context_window": before, "context_changed": changed,
                "native_context": {"claude": {"keys": {f"env.{key}": record}}} if record else None,
                "managed_values": {"env": {key: desired}} if desired is not None else {},
                "report_lines": ([f"context window: claude {before or 'unset'} -> {desired} tokens",
                                  f"settings: {config}"] if changed else [])}
    source = _read_json_object(_claude_hook_source(active_root), "Claude hook source")
    source_hooks = source.get("hooks")
    if not isinstance(source_hooks, dict) or not source_hooks:
        raise ActivationError("Claude hook source has no hooks object")
    desired_values = _claude_managed_values(source)
    if context_window is not None:
        desired_values["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(context_window)
    config = config_path or _config_path("claude", scope, "settings.json")
    original = config.read_bytes() if config.exists() else None
    data = _read_json_object(config, "Claude settings") if config.exists() else {}
    context_key = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
    original_env = data.get("env", {})
    context_present = isinstance(original_env, dict) and context_key in original_env
    context_before = original_env.get(context_key) if isinstance(original_env, dict) else None
    env_container_present = "env" in data
    if context_window is not None and "env" in data and not isinstance(data.get("env"), dict):
        raise ActivationError(f"Claude settings env is not an object: {config}")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ActivationError(f"Claude settings hooks is not an object: {config}")
    from claude_settings_config import remove_retired_hooks
    changed = remove_retired_hooks(data)
    added = 0
    previous_hooks = {}
    if previous:
        previous_hooks = previous.get("managed_config", {}).get("claude_hooks", {})
        if not previous_hooks and previous.get("active_root"):
            try:
                old_source = _read_json_object(
                    _claude_hook_source(Path(previous["active_root"])),
                    "previous Claude hook source",
                )
                previous_hooks = old_source.get("hooks", {})
            except ActivationError:
                previous_hooks = {}
    if isinstance(previous_hooks, dict):
        for event, old_entries in previous_hooks.items():
            current = hooks.get(event)
            if not isinstance(current, list) or not isinstance(old_entries, list):
                continue
            old = {json.dumps(item, sort_keys=True) for item in old_entries}
            # A hook the user tuned keeps its values while the release still
            # ships it; once the release drops it, the tuned copy goes too
            # instead of pointing at a script that no longer exists.
            retired = _event_hook_keys(old_entries) - _event_hook_keys(source_hooks.get(event))
            kept = _without_hooks(
                [item for item in current if json.dumps(item, sort_keys=True) not in old],
                retired,
            )
            if kept != current:
                hooks[event] = kept
                changed = True
    for event, entries in source_hooks.items():
        if not isinstance(entries, list):
            raise ActivationError(f"Claude hook source event is not a list: {event}")
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise ActivationError(f"Claude settings hook event is not a list: {event}")
        known = _event_hook_keys(current)
        for item in entries:
            missing = [key for key in _hook_keys(item) if key not in known]
            if not missing:
                continue
            if len(missing) == len(_hook_keys(item)):
                addition = item
            else:
                # Only the hooks this settings file lacks, so the ones already
                # present (possibly tuned) do not run twice.
                addition = _without_hooks([item], set(_hook_keys(item)) - set(missing))[0]
            current.append(addition)
            known.update(missing)
            added += 1
            changed = True

    previous_values = {}
    if previous:
        candidate = previous.get("managed_config", {}).get("claude_values", {})
        if isinstance(candidate, dict):
            previous_values = candidate
    managed_values = {"env": {}}
    conflicts = []
    missing_value = object()

    desired_statusline = desired_values["statusLine"]
    current_statusline = data.get("statusLine", missing_value)
    previous_statusline = previous_values.get("statusLine", missing_value)
    if (
        current_statusline is missing_value
        or current_statusline == desired_statusline
        or (
            previous_statusline is not missing_value
            and current_statusline == previous_statusline
        )
    ):
        if current_statusline != desired_statusline:
            data["statusLine"] = desired_statusline
            changed = True
        managed_values["statusLine"] = desired_statusline
    else:
        conflicts.append("statusLine")

    desired_auto_memory = desired_values["autoMemoryEnabled"]
    current_auto_memory = data.get("autoMemoryEnabled", missing_value)
    previous_auto_memory = previous_values.get("autoMemoryEnabled", missing_value)
    if (
        current_auto_memory is missing_value
        or current_auto_memory == desired_auto_memory
        or (
            previous_auto_memory is not missing_value
            and current_auto_memory == previous_auto_memory
        )
    ):
        if current_auto_memory != desired_auto_memory:
            data["autoMemoryEnabled"] = desired_auto_memory
            changed = True
        managed_values["autoMemoryEnabled"] = desired_auto_memory
    else:
        conflicts.append("autoMemoryEnabled")

    previous_env = previous_values.get("env", {})
    if not isinstance(previous_env, dict):
        previous_env = {}
    # A key a prior release managed but this one no longer does: drop it only
    # if the user never changed it away from the value that release set,
    # mirroring the exact-match hook-entry retirement above.
    retired_env = {
        key: value for key, value in previous_env.items()
        if key not in desired_values["env"]
    }
    current_env = data.get("env", missing_value)
    needs_env_object = bool(desired_values["env"]) or bool(retired_env)
    if current_env is missing_value:
        if needs_env_object:
            current_env = {}
            data["env"] = current_env
            changed = True
    elif not isinstance(current_env, dict):
        if needs_env_object:
            conflicts.append("env")
        current_env = None
    if isinstance(current_env, dict):
        for key, desired_value in desired_values["env"].items():
            current_value = current_env.get(key, missing_value)
            previous_value = previous_env.get(key, missing_value)
            if (
                (key == "CLAUDE_CODE_AUTO_COMPACT_WINDOW" and context_window is not None)
                or
                current_value is missing_value
                or current_value == desired_value
                or (
                    previous_value is not missing_value
                    and current_value == previous_value
                )
            ):
                if current_value != desired_value:
                    current_env[key] = desired_value
                    changed = True
                managed_values["env"][key] = desired_value
            else:
                conflicts.append(f"env.{key}")
        for key, previous_value in retired_env.items():
            current_value = current_env.get(key, missing_value)
            if current_value is missing_value:
                continue
            if current_value == previous_value:
                del current_env[key]
                changed = True
            else:
                conflicts.append(f"env.{key}")

    has_claude_v11 = herdr_integration.version_marker(
        active_root / "hooks/herdr-agent-state.sh", "claude", 11)
    changed = herdr_integration.migrate_hook_data(
        data, "claude", register=has_claude_v11) or changed
    if changed:
        _atomic_json(config, data)
    context_previous = context_before if context_present else None
    native_context = None
    if context_window is not None:
        from native_context import _native_key_record
        record = _native_key_record(previous, "claude", context_present, context_before,
                                   str(context_window), extra={"env_present": env_container_present})
        native_context = {"claude": {"keys": {f"env.{context_key}": record}}}
    return {
        "kind": "claude-settings-merged",
        "path": str(config),
        "added": added,
        "managed_hooks": source_hooks,
        "managed_values": managed_values,
        "conflicts": conflicts,
        "previous_context_window": context_previous,
        "context_changed": context_window is not None and context_previous != str(context_window),
        "native_context": native_context,
        "report_lines": ([f"context window: claude {context_previous or 'unset'} -> {context_window} tokens",
                          f"settings: {config}"] if context_window is not None and context_previous != str(context_window) else []),
    }


def _claude_settings_health(
    active_root: Path, scope: str = "global"
) -> tuple[bool, List[str]]:
    try:
        source = _read_json_object(_claude_hook_source(active_root), "Claude hook source")
        config = _read_json_object(
            _config_path("claude", scope, "settings.json"), "Claude settings"
        )
    except ActivationError:
        return True, []
    expected = source.get("hooks")
    actual = config.get("hooks")
    missing = False
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        missing = True
    else:
        for event, entries in expected.items():
            if not isinstance(entries, list) or not isinstance(actual.get(event), list):
                missing = True
                continue
            if _event_hook_keys(entries) - _event_hook_keys(actual[event]):
                missing = True
        if not _hook_command_files_present(actual):
            missing = True

    try:
        desired_values = _claude_managed_values(source)
    except ActivationError:
        return True, []
    conflicts = []
    if "statusLine" not in config:
        missing = True
    elif config["statusLine"] != desired_values["statusLine"]:
        conflicts.append("statusLine")
    if "autoMemoryEnabled" not in config:
        missing = True
    elif config["autoMemoryEnabled"] != desired_values["autoMemoryEnabled"]:
        conflicts.append("autoMemoryEnabled")
    if desired_values["env"]:
        actual_env = config.get("env")
        if "env" not in config:
            missing = True
        elif not isinstance(actual_env, dict):
            conflicts.append("env")
        else:
            for key, value in desired_values["env"].items():
                if key not in actual_env:
                    missing = True
                elif actual_env[key] != value:
                    conflicts.append(f"env.{key}")

    statusline = paths.runtime_home("claude", scope) / "statusline.sh"
    if not statusline.is_file() or not os.access(statusline, os.X_OK):
        missing = True
    return missing, conflicts


CODEX_IDENTITY_FRAGMENT = ("adapters", "codex", "config", "harness-identity.toml")
CODEX_IDENTITY_BEGIN = (
    "# >>> hearting harness identity (managed by runtime activation; edit outside this block) >>>"
)
CODEX_IDENTITY_END = "# <<< hearting harness identity <<<"
CODEX_IDENTITY_BACKUP = "config.toml.pre-harness-identity"
CODEX_INTERACTIVE_QUESTION_KEY = "default_mode_request_user_input"
CODEX_QUESTION_CONFLICT = "features." + CODEX_INTERACTIVE_QUESTION_KEY
_CODEX_FEATURE_HEADER = re.compile(
    r"""(?m)^[ \t]*\[[ \t]*(?:features|"features"|'features')[ \t]*\][ \t]*(?:#[^\n]*)?(?:\r?\n|\Z)"""
)
_CODEX_FEATURE_LINE = re.compile(
    r"(?m)^[ \t]*" + CODEX_INTERACTIVE_QUESTION_KEY + r"[ \t]*=[ \t]*true[ \t]*(?:#[^\n]*)?(?:\r?\n|\Z)"
)
_CODEX_FEATURE_LINE_TEXT = CODEX_INTERACTIVE_QUESTION_KEY + " = true\n"


def _codex_feature_value(data: dict):
    """``(present, value)``; ``features`` that is not a table is reported as a non-true value."""
    features = data.get("features")
    if features is None:
        return False, None
    if not isinstance(features, dict):
        return True, None
    if CODEX_INTERACTIVE_QUESTION_KEY not in features:
        return False, None
    return True, features[CODEX_INTERACTIVE_QUESTION_KEY]


def _codex_feature_state(text: str) -> str:
    """``true`` | ``missing`` | ``conflict`` for the interactive question feature in ``text``."""
    import tomllib

    try:
        data = tomllib.loads(text) if text.strip() else {}
    except ValueError:
        return "conflict"
    present, value = _codex_feature_value(data)
    if not present:
        return "missing"
    return "true" if value is True else "conflict"


def _codex_feature_insert(text: str, previous: Optional[dict]):
    """``(text, record, conflict)``: make the feature true without touching a user byte.

    An existing ``true`` is respected as written (a prior receipt is carried only while its key
    line is still present exactly once). A missing key is inserted as one line under a real
    ``[features]`` header, or under a new header at the end; every candidate is parsed and may
    differ from the original only by that key. Anything else is a conflict and stays untouched.
    """
    import copy
    import tomllib

    try:
        data = tomllib.loads(text) if text.strip() else {}
    except ValueError:
        return text, None, CODEX_QUESTION_CONFLICT
    present, value = _codex_feature_value(data)
    if present:
        if value is not True:
            return text, None, CODEX_QUESTION_CONFLICT
        carried = previous if isinstance(previous, dict) and previous.get("line") else None
        if carried and len(_CODEX_FEATURE_LINE.findall(text)) != 1:
            carried = None
        return text, carried, None
    if data.get("features") is not None and not isinstance(data.get("features"), dict):
        return text, None, CODEX_QUESTION_CONFLICT
    expected = copy.deepcopy(data)
    expected.setdefault("features", {})[CODEX_INTERACTIVE_QUESTION_KEY] = True

    def accepted(candidate: str) -> bool:
        try:
            return tomllib.loads(candidate) == expected
        except ValueError:
            return False

    for match in _CODEX_FEATURE_HEADER.finditer(text):
        lead = "" if match.group(0).endswith("\n") else "\n"
        candidate = text[:match.end()] + lead + _CODEX_FEATURE_LINE_TEXT + text[match.end():]
        if accepted(candidate):
            return candidate, {"line": _CODEX_FEATURE_LINE_TEXT, "header": ""}, None
    header = ""
    if text:
        header = ("" if text.endswith("\n") else "\n") + "\n"
    header += "[features]\n"
    candidate = text + header + _CODEX_FEATURE_LINE_TEXT
    if accepted(candidate):
        return candidate, {"line": _CODEX_FEATURE_LINE_TEXT, "header": header}, None
    return text, None, CODEX_QUESTION_CONFLICT


def _codex_feature_remove(text: str, record: Optional[dict]) -> str:
    """Remove only the recorded key line (and its activation-created header once empty).

    The user's own value wins: a key that is no longer ``true`` (or is gone) leaves the text as
    it is, and a header that still holds any user key is never removed or merged away.
    """
    import copy
    import tomllib

    if not isinstance(record, dict) or not record.get("line"):
        return text
    try:
        data = tomllib.loads(text) if text.strip() else {}
    except ValueError:
        return text
    present, value = _codex_feature_value(data)
    if not present or value is not True:
        return text
    expected = copy.deepcopy(data)
    del expected["features"][CODEX_INTERACTIVE_QUESTION_KEY]
    stripped = None
    for match in _CODEX_FEATURE_LINE.finditer(text):
        candidate = text[:match.start()] + text[match.end():]
        try:
            if tomllib.loads(candidate) == expected:
                stripped = candidate
                break
        except ValueError:
            continue
    if stripped is None:
        return text
    header = record.get("header")
    if isinstance(header, str) and header:
        # Only an activation-created header whose table holds no user key goes: removing it
        # must parse to exactly the data the file had without the key line.
        without_header = copy.deepcopy(expected)
        if without_header.get("features") == {}:
            del without_header["features"]
        position = stripped.find(header)
        while position != -1:
            candidate = stripped[:position] + stripped[position + len(header):]
            try:
                if tomllib.loads(candidate) == without_header:
                    return candidate
            except ValueError:
                pass
            position = stripped.find(header, position + 1)
    return stripped


def _codex_identity_fragment(active_root: Path):
    """``(block_body, set_values, excluded_keys)`` of the shipped fragment, or ``None``.

    A release that ships no fragment (an older source) simply manages nothing.
    """
    path = Path(active_root).joinpath(*CODEX_IDENTITY_FRAGMENT)
    if not path.is_file():
        return None
    import tomllib

    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ActivationError(f"invalid Codex identity fragment: {path}: {exc}") from exc
    policy = data.get("shell_environment_policy")
    policy = policy if isinstance(policy, dict) else {}
    set_values = policy.get("set") if isinstance(policy.get("set"), dict) else {}
    filters = policy.get("filters") if isinstance(policy.get("filters"), dict) else {}
    if not set_values and not filters:
        raise ActivationError(f"Codex identity fragment has no shell_environment_policy: {path}")
    body = "".join(
        line + "\n" for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    return body, dict(set_values), sorted(key for key, action in filters.items() if action == "exclude")


def _codex_identity_block(body: str) -> str:
    return f"{CODEX_IDENTITY_BEGIN}\n{body}{CODEX_IDENTITY_END}\n"


def _codex_identity_split(text: str):
    """``(before, block, after)`` around the delimited block, ``(text, None, "")`` when absent.

    ``block`` includes both marker lines and the final newline. Unbalanced or repeated markers
    raise ``ValueError``: the block cannot be located, so it is not touched.
    """
    begins = text.count(CODEX_IDENTITY_BEGIN)
    ends = text.count(CODEX_IDENTITY_END)
    if begins == 0 and ends == 0:
        return text, None, ""
    if begins != 1 or ends != 1:
        raise ValueError("unbalanced harness identity markers")
    start = text.index(CODEX_IDENTITY_BEGIN)
    stop = text.index(CODEX_IDENTITY_END)
    if stop < start or (start and text[start - 1] != "\n"):
        raise ValueError("misplaced harness identity markers")
    end = text.find("\n", stop)
    end = len(text) if end == -1 else end + 1
    return text[:start], text[start:end], text[end:]


def _codex_identity_assess(text: str, set_values: dict, excluded: List[str]):
    """``(status, conflicts)`` of an already parsed config text without the managed block.

    ``satisfied``: the effective policy already carries the fragment's values; ``clear``: no
    policy key is in the way (the block can be added); ``conflict``: it cannot be merged safely.
    """
    import tomllib

    data = tomllib.loads(text) if text.strip() else {}
    policy = data.get("shell_environment_policy")
    if policy is None:
        return "clear", []
    if not isinstance(policy, dict):
        return "conflict", ["shell_environment_policy"]
    conflicts = [f"shell_environment_policy.{key}" for key in ("exclude", "include_only") if key in policy]
    user_set = policy.get("set")
    user_filters = policy.get("filters")
    if user_set is not None and not isinstance(user_set, dict):
        conflicts.append("shell_environment_policy.set")
    if user_filters is not None and not isinstance(user_filters, dict):
        conflicts.append("shell_environment_policy.filters")
    if conflicts:
        return "conflict", conflicts
    set_ok = all((user_set or {}).get(key) == value for key, value in set_values.items())
    filters_ok = all((user_filters or {}).get(key) == "exclude" for key in excluded)
    if set_ok and filters_ok:
        return "satisfied", []
    # A second `[shell_environment_policy.set|filters]` header beside the user's own table
    # would be a duplicate table, so a partial user table cannot take the block. Only the tables
    # the fragment itself defines can clash.
    owned = (("set", user_set, set_values), ("filters", user_filters, excluded))
    clashing = [f"shell_environment_policy.{key}" for key, table, wanted in owned
                if wanted and table is not None]
    return ("conflict", clashing) if clashing else ("clear", [])


def _codex_identity_candidate_ok(text: str, set_values: dict, excluded: List[str]) -> bool:
    import tomllib

    try:
        policy = (tomllib.loads(text).get("shell_environment_policy") or {})
    except ValueError:
        return False
    if not isinstance(policy, dict) or "exclude" in policy or "include_only" in policy:
        return False
    user_set = policy.get("set") if isinstance(policy.get("set"), dict) else {}
    filters = policy.get("filters") if isinstance(policy.get("filters"), dict) else {}
    return (
        all(user_set.get(key) == value for key, value in set_values.items())
        and all(filters.get(key) == "exclude" for key in excluded)
    )


# destructive-ok: reason=atomically publish one managed config file or discard its own temporary; boundary=the target file and one mkstemp sibling in its directory
def _atomic_text(path: Path, text: str, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = None
    try:
        existing_mode = path.stat().st_mode & 0o777
    except OSError:
        pass
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, existing_mode if existing_mode is not None else (mode or 0o600))
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_codex_config_text(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Atomically update Codex's resolved config target without replacing its link."""
    try:
        write_path = path.resolve(strict=False) if path.is_symlink() else path
    except (OSError, RuntimeError) as exc:
        raise ActivationError(f"cannot resolve Codex config target: {path}: {exc}") from exc
    if path.is_symlink() and not write_path.parent.is_dir():
        raise FileNotFoundError(f"Codex config target directory is missing: {write_path.parent}")
    _atomic_text(write_path, text, mode)


def _merge_codex_identity_config(
    active_root: Path, previous: Optional[dict], scope: str = "global", *,
    pre_projection_exists: bool | None = None, pre_projection_bytes: bytes | None = None,
    file_created: bool | None = None,
) -> dict:
    """Merge the fragment into ``$CODEX_HOME/config.toml`` as one delimited block.

    Never refuses and never rewrites what the user owns: a policy that cannot be merged safely
    is reported in ``conflicts`` and the file stays byte-identical. Invalid TOML takes the same
    ``ActivationError`` path as the plugin conflict check that already ran.
    """
    result = {"kind": "codex-config-merged", "managed_config": None, "conflicts": []}
    fragment = _codex_identity_fragment(active_root)
    if fragment is None:
        return result
    body, set_values, excluded = fragment
    config = _config_path("codex", scope, "config.toml")
    result["path"] = str(config)
    if config.exists() and not config.is_file():
        raise ActivationError(f"invalid Codex config: {config} is not a regular file")
    original = ""
    if config.exists():
        try:
            original = config.read_text(encoding="utf-8")
            import tomllib

            tomllib.loads(original)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ActivationError(f"invalid Codex config: {config}: {exc}") from exc
    backup_original = original
    if pre_projection_exists is False:
        backup_original = ""
    elif pre_projection_exists is True and pre_projection_bytes is not None:
        try:
            backup_original = pre_projection_bytes.decode("utf-8")
            import tomllib
            tomllib.loads(backup_original)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ActivationError(f"invalid Codex config preimage: {config}: {exc}") from exc
    previous_record = {}
    if previous:
        candidate = previous.get("managed_config", {}).get("codex_identity")
        if isinstance(candidate, dict):
            previous_record = candidate
    block = _codex_identity_block(body)
    try:
        before, existing, after = _codex_identity_split(original)
    except ValueError:
        result["conflicts"].append("shell_environment_policy.managed-block")
        return result
    without_block = before + after if existing is not None else original
    status, conflicts = _codex_identity_assess(without_block, set_values, excluded)
    if status == "conflict":
        result["conflicts"] = conflicts
        return result
    identity_text = original
    identity_inserted = None
    if status != "satisfied":
        if existing == block:
            recorded = previous_record.get("inserted")
            identity_inserted = recorded if isinstance(recorded, str) and block in recorded else block
        else:
            if existing is not None:
                recorded = previous_record.get("inserted")
                if isinstance(recorded, str) and recorded in original:
                    without_block = original.replace(recorded, "", 1)
                status, conflicts = _codex_identity_assess(without_block, set_values, excluded)
                if status == "conflict":
                    result["conflicts"] = conflicts
                    return result
            separator = ""
            if without_block:
                separator = ("" if without_block.endswith("\n") else "\n") + "\n"
            identity_text = without_block + separator + block
            if not _codex_identity_candidate_ok(identity_text, set_values, excluded):
                result["conflicts"] = ["shell_environment_policy"]
                return result
            identity_inserted = separator + block
    elif existing == block:
        recorded = previous_record.get("inserted")
        if isinstance(recorded, str) and recorded and original.count(recorded) == 1 and block in recorded:
            # Retain uninstall ownership when the user's policy now independently
            # satisfies the fragment but the exact activation block is unchanged.
            identity_inserted = recorded
    final_text, feature_record, feature_conflict = _codex_feature_insert(
        identity_text, previous_record.get("feature")
    )
    if feature_conflict:
        # A user-owned or unsafe `features` form stays byte-for-byte intact, while the
        # required identity block is still merged and remains removable by its receipt.
        final_text = identity_text
        feature_record = None
        result["conflicts"] = [feature_conflict]
    if final_text != original:
        if config.exists() and file_created is not True:
            backup = config.with_name(CODEX_IDENTITY_BACKUP)
            if not backup.exists():
                _atomic_text(backup, backup_original)
        _atomic_codex_config_text(config, final_text)
    if identity_inserted is not None or feature_record is not None:
        result["managed_config"] = {"inserted": identity_inserted, "feature": feature_record}
    return result


def _unmerge_codex_identity_config(
    state: dict, scope: str, dry_run: bool = False
) -> List[str]:
    """Independently unmerge the question feature line, the identity block and the
    exact-match native window key.

    The feature key line goes first (validated by parsing, so a user key is never moved), then
    the identity block as one exact span. A feature the user changed or removed is left alone.
    """
    from native_context import restore_codex_context, _publish
    managed = (state.get("managed_config") or {}) if isinstance(state, dict) else {}
    record = managed.get("codex_identity") if isinstance(managed, dict) else None
    inserted = record.get("inserted") if isinstance(record, dict) else None
    feature = record.get("feature") if isinstance(record, dict) else None
    native = managed.get("native_context") if isinstance(managed, dict) else None
    codex_native = native.get("codex", {}) if isinstance(native, dict) else {}
    keys = codex_native.get("keys", {}) if isinstance(codex_native, dict) else {}
    window_record = keys.get("model_context_window") if isinstance(keys, dict) else None
    has_native = isinstance(window_record, dict)
    baseline = window_record.get("baseline") if has_native else None
    valid_native = (
        has_native
        and type(window_record.get("projected")) is int
        and window_record["projected"] > 0
        and isinstance(baseline, dict)
        and type(baseline.get("present")) is bool
        and (not baseline["present"] or
             (type(baseline.get("value")) is int and baseline["value"] > 0))
    )
    config = _config_path("codex", scope, "config.toml")
    if not config.is_file():
        return []
    try:
        original_bytes = config.read_bytes()
        text = original_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    remaining = codex_tui_config.restore_startup_update_default_text(
        text, record.get("startup_default") if isinstance(record, dict) else None)
    remaining = _codex_feature_remove(remaining, feature)
    try:
        remaining = codex_tui_config.restore_status_line_text(remaining, managed.get("codex_tui"))
    except codex_tui_config.CodexTuiConfigError:
        pass
    block_removed = isinstance(inserted, str) and bool(inserted) and remaining.count(inserted) == 1
    if block_removed:
        remaining = remaining.replace(inserted, "", 1)
    try:
        restored, _ = restore_codex_context(remaining.encode("utf-8"), window_record)
        remaining = restored.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        remaining = remaining
    trust_record = managed.get("codex_hook_trust") if isinstance(managed, dict) else None
    if trust_record:
        try:
            import codex_hook_trust
            remaining, trust_reports = codex_hook_trust.restore_trust_text(remaining, trust_record)
            for line in trust_reports:
                print(line, file=sys.stderr)
        except (ImportError, ValueError) as exc:
            print(f"Codex hook trust restore skipped: {str(exc).splitlines()[0][:180]}", file=sys.stderr)
    backup = config.with_name(CODEX_IDENTITY_BACKUP)
    if valid_native and isinstance(codex_native.get("file_created"), bool):
        created_by_activation = codex_native["file_created"]
    elif isinstance(native, dict) and "codex" in native:
        created_by_activation = False
    else:
        # Legacy records: activation backs up a config it found, so a removed block with no
        # backup means activation created this file. Without that block the file is the user's.
        created_by_activation = block_removed and not backup.exists()
    changed = remaining.encode("utf-8") != original_bytes
    removed_file = not remaining.strip() and created_by_activation
    try:
        write_path = config.resolve(strict=False) if config.is_symlink() else config
    except (OSError, RuntimeError) as exc:
        raise ActivationError(f"cannot resolve Codex config target: {config}: {exc}") from exc
    if not dry_run:
        if removed_file:
            try:
                with safe_fs.TargetLock(write_path):
                    current = safe_fs.capture_state(write_path, include_payload=True)
                    if (current.kind != "file" or current.payload != original_bytes
                            or os.lstat(write_path).st_nlink != 1):
                        return []
                    auth = safe_fs.authority(write_path, owner="runtime-deactivate:codex-config",
                                             allowed_paths=(write_path,), allow_leaf_symlink=False,
                                             expected=current)
                    safe_fs.remove_exact(auth, expected=current)
            except safe_fs.SafetyError as exc:
                raise ActivationError(str(exc)) from exc
        elif changed:
            try:
                _publish(write_path, remaining.encode("utf-8"),
                         owner="runtime-deactivate:codex-native-context", source=original_bytes)
            except ValueError as exc:
                raise ActivationError(str(exc)) from exc
    return [str(config)] if changed or removed_file else []


def _seed_opencode_tui_entry(active_root: Path, previous: Optional[dict], scope: str = "global") -> dict:
    """Absent-only TUI registration for the hearting TUI identity entry.

    Writes the registration-only ``tui.json`` into the managed OpenCode
    runtime home only when no ``tui.json``/``tui.jsonc`` exists there. An
    existing file is user-owned by definition: it stays byte-identical and
    the bare identity stays unsupported on that host (reported, never merged
    around). Existing plugins, options, and disables are preserved because
    nothing present is ever edited.
    """
    result: dict = {"kind": "opencode-tui-seeded", "managed_config": None, "status": "skipped"}
    template = active_root / "adapters" / "opencode" / "tui" / "tui.json"
    if not template.is_file():
        result["status"] = "template-missing"
        return result
    try:
        text = template.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ActivationError(f"invalid OpenCode TUI template: {template}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ActivationError(f"invalid OpenCode TUI template: {template}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("plugin"), list):
        raise ActivationError(
            f"invalid OpenCode TUI template: {template}: registration-only plugin list required"
        )
    home = paths.runtime_home("opencode", scope)
    target = home / "tui.json"
    override = home / "tui.jsonc"
    result["path"] = str(target)
    if override.is_symlink() or override.is_file():
        result["status"] = "user-managed"
        return result
    if override.exists():
        raise ActivationError(f"invalid OpenCode TUI config: {override} is not a regular file")
    previous_record: dict = {}
    if previous:
        candidate = previous.get("managed_config", {}).get("opencode_tui")
        if isinstance(candidate, dict):
            previous_record = candidate
    # Ownership comes only from the prior activation record, never from byte
    # equality: a user file identical to the template stays user-owned, and a
    # symlink (user successor by shape) is never claimed or followed.
    if target.is_symlink():
        result["status"] = "user-managed"
        return result
    if target.is_file():
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ActivationError(f"invalid OpenCode TUI config: {target}: {exc}") from exc
        recorded = previous_record.get("seeded")
        if isinstance(recorded, str) and recorded and current == recorded:
            result["status"] = "present"
            result["managed_config"] = {"seeded": recorded}
            return result
        result["status"] = "user-managed"
        return result
    if target.exists():
        raise ActivationError(f"invalid OpenCode TUI config: {target} is not a regular file")
    # destructive-ok: reason=create the registration-only tui.json activation owns, only when absent; boundary=the new file alone, user files untouched
    _atomic_text(target, text)
    result["status"] = "seeded"
    result["managed_config"] = {"seeded": text}
    return result


def _unseed_opencode_tui_entry(state: dict, scope: str = "global", dry_run: bool = False) -> List[str]:
    """Remove the tui.json activation seeded, only while it is still exactly what it wrote.

    A symlink, an unrecorded file, or user-changed bytes are a successor and
    stay untouched through the normal safe-filesystem removal.
    """
    managed = (state.get("managed_config") or {}) if isinstance(state, dict) else {}
    record = managed.get("opencode_tui") if isinstance(managed, dict) else None
    seeded = record.get("seeded") if isinstance(record, dict) else None
    if not isinstance(seeded, str) or not seeded:
        return []
    target = paths.runtime_home("opencode", scope) / "tui.json"
    if target.is_symlink() or not target.is_file():
        return []
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if text != seeded:
        return []
    if not dry_run:
        try:
            _remove_path(target)
        except ActivationError:
            return []
    return [str(target)]


def _codex_identity_health(active_root: Path, scope: str = "global") -> tuple[bool, List[str]]:
    """``(missing, conflicts)`` for the Codex identity policy; a release without it is healthy."""
    try:
        fragment = _codex_identity_fragment(active_root)
    except ActivationError:
        return True, []
    if fragment is None:
        return False, []
    body, set_values, excluded = fragment
    try:
        config = _config_path("codex", scope, "config.toml")
        text = config.read_text(encoding="utf-8") if config.is_file() else ""
        before, existing, after = _codex_identity_split(text)
    except (ActivationError, OSError, UnicodeDecodeError):
        return True, []
    except ValueError:
        return False, ["shell_environment_policy.managed-block"]
    if existing is not None and existing != _codex_identity_block(body):
        return True, []
    try:
        import tomllib

        tomllib.loads(text)
        status, conflicts = _codex_identity_assess(before + after, set_values, excluded)
    except ValueError:
        return True, []
    feature = _codex_feature_state(text)
    if feature == "conflict":
        conflicts = list(conflicts) + [CODEX_QUESTION_CONFLICT]
    if status == "conflict":
        return False, conflicts
    if feature == "conflict":
        return False, conflicts
    if feature == "missing":
        return True, []
    if status == "satisfied":
        return False, []
    return existing is None, []


_HOOK_COMMAND_PATH_RE = re.compile(r"\$HOME/[^\s\"']+")


def _hook_command_files_present(hooks: dict) -> bool:
    """A registered hook whose command file is gone breaks every matching event."""
    home = os.environ.get("HOME") or str(Path.home())
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            hooks_list = entry.get("hooks")
            for hook in hooks_list if isinstance(hooks_list, list) else []:
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command")
                if not isinstance(command, str):
                    continue
                for raw in _HOOK_COMMAND_PATH_RE.findall(command):
                    candidate = Path(raw.replace("$HOME", home, 1))
                    if candidate.suffix and not candidate.exists():
                        return False
    return True


def _activation_protected_paths(runtime: str, scope: str = "global") -> List[Path]:
    """Protect user config leaves that integration migration edits additively."""
    paths_owned = [_state_path(runtime, scope)]
    if runtime == "claude":
        paths_owned.append(_config_path(runtime, scope, "settings.json"))
    elif runtime == "codex":
        paths_owned.append(paths.runtime_home(runtime, scope) / "hooks.json")
        config = _config_path(runtime, scope, "config.toml")
        paths_owned.append(config)
        if config.is_symlink():
            try:
                target = config.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ActivationError(f"cannot resolve Codex config target: {config}: {exc}") from exc
            if target != config:
                paths_owned.append(target)
        paths_owned.append(_config_path(runtime, scope, CODEX_IDENTITY_BACKUP))
    elif runtime == "opencode":
        home = paths.runtime_home(runtime, scope)
        paths_owned.extend(home / name for name in (
            "tui.json", "tui.jsonc", "cli.json", "cli.jsonc",
        ))
    return paths_owned


def _prepare_runtime_config(
    runtime: str, active_root: Path, previous: Optional[dict], scope: str = "global", *,
    native_locked: bool = False,
) -> List[dict]:
    from native_context import NativeContextError, project_context_window
    if runtime == "claude":
        try:
            projected = project_context_window(
                runtime, runtime=paths.runtime_home(runtime, scope), source_root=active_root,
                previous=previous, scope=scope, native_locked=native_locked,
                claude_merge=lambda **kwargs: _merge_claude_settings(active_root, previous, scope, **kwargs),
            )
        except NativeContextError as exc:
            raise ActivationError(str(exc)) from exc
        result = projected.get("merge", {})
        result.setdefault("report_lines", projected["report_lines"])
        return [result]
    if runtime == "codex":
        try:
            projected = project_context_window(runtime, runtime=paths.runtime_home(runtime, scope),
                                               source_root=active_root, previous=previous, scope=scope,
                                               native_locked=native_locked)
        except NativeContextError as exc:
            raise ActivationError(str(exc)) from exc
        native = projected.get("native_context") or {}
        codex_record = native.get("codex", {}) if isinstance(native, dict) else {}
        result = _merge_codex_identity_config(
            active_root, previous, scope,
            pre_projection_exists=projected.get("pre_projection_exists"),
            pre_projection_bytes=projected.get("pre_projection_bytes"),
            file_created=codex_record.get("file_created") if isinstance(codex_record, dict) else None,
        )
        config = _config_path("codex", scope, "config.toml")
        fragment = active_root / "adapters" / "codex" / "config" / "tui-statusline.toml"
        try:
            original = config.read_text(encoding="utf-8") if config.exists() else ""
        except (OSError, UnicodeError) as exc:
            result.setdefault("conflicts", []).extend(("tui.status_line", "check_for_update_on_startup"))
            result["tui_status_line_detail"] = str(exc)
            result["startup_default_detail"] = str(exc)
        else:
            updated = original
            prior_managed = (previous or {}).get("managed_config", {})
            prior_managed = prior_managed if isinstance(prior_managed, dict) else {}
            prior_identity = prior_managed.get("codex_identity")
            prior_identity = prior_identity if isinstance(prior_identity, dict) else {}
            try:
                statusline_updated = codex_tui_config.merge_status_line_from_fragment(updated, fragment)
                result["tui_record"] = codex_tui_config.status_line_record(
                    updated, statusline_updated, prior_managed.get("codex_tui"))
                updated = statusline_updated
                result["tui_status_line_changed"] = updated != original
            except (OSError, UnicodeError, codex_tui_config.CodexTuiConfigError) as exc:
                result.setdefault("conflicts", []).append("tui.status_line")
                result["tui_status_line_detail"] = str(exc)

            before_startup = updated
            startup_record = None
            try:
                startup_updated = codex_tui_config.merge_startup_update_default_text(before_startup)
                startup_record = codex_tui_config.startup_update_default_record(
                    before_startup, startup_updated, prior_identity.get("startup_default"))
                updated = startup_updated
                result["startup_default_changed"] = updated != before_startup
            except (UnicodeError, codex_tui_config.CodexTuiConfigError) as exc:
                result.setdefault("conflicts", []).append("check_for_update_on_startup")
                result["startup_default_detail"] = str(exc)

            if startup_record:
                managed_config = result.get("managed_config")
                managed_config = dict(managed_config) if isinstance(managed_config, dict) else {}
                managed_config["startup_default"] = startup_record
                result["managed_config"] = managed_config
            if updated != original:
                _atomic_codex_config_text(config, updated)
        result.setdefault("report_lines", []).extend(projected["report_lines"])
        result["context_projection"] = projected
        result["native_context"] = native
        herdr_hook = herdr_integration.migrate_hook_file(
            paths.runtime_home("codex", scope) / "hooks.json", "codex",
            source_root=active_root,
        )
        return [result, herdr_hook]
    if runtime == "opencode":
        seeded = _seed_opencode_tui_entry(active_root, previous, scope)
        registration = herdr_integration.migrate_opencode_configs(
            paths.runtime_home("opencode", scope), active_root,
            previous=(previous or {}).get("managed_config", {}).get("herdr_opencode"),
        )
        if seeded.get("status") == "seeded":
            registration["managed_config"]["files"].append({
                "path": "tui.json",
                "added": [herdr_integration.OPENCODE_IDENTITY,
                          herdr_integration.OPENCODE_TUI_V1],
                "created": True,
                "seeded": seeded["managed_config"]["seeded"],
            })
        return [seeded, registration]
    return []


def duplicate_sources(runtime: str, scope: str = "global") -> List[str]:
    native = _native_present(runtime, scope)
    if runtime == "codex":
        return ["native+plugin"] if native and _codex_plugin_active(scope) else []
    if runtime == "claude":
        return ["native+plugin"] if native and _claude_plugin_active(scope) else []
    local_plugin = (paths.runtime_home(runtime, scope) / "plugins/hearting-guards.js").exists()
    return ["local+npm"] if local_plugin and _opencode_npm_present(scope) else []


def _remove_path(path: Path) -> None:
    current = safe_fs.capture_state(path)
    if current.kind == "missing":
        return
    try:
        auth = safe_fs.authority(
            path.expanduser().absolute(),
            owner="runtime-activation:validated-owned-destination",
            allowed_paths=(path.expanduser().absolute(),),
            expected=current,
        )
        safe_fs.remove_exact(auth, recursive=current.kind == "directory")
    except safe_fs.SafetyError as exc:
        raise ActivationError(str(exc)) from exc


_HERDR_INSTALL_HEADER = re.compile(r"^(?:#|//)\s*installed by herdr\s*$", re.M)
_HERDR_INTEGRATION_ID = re.compile(r"^(?:#|//)\s*HERDR_INTEGRATION_ID=(\S+)\s*$", re.M)


def _herdr_integration_id(path: Path) -> Optional[str]:
    """The integration id of a file carrying herdr's own install header, else None."""
    try:
        head = path.read_bytes()[:4096].decode("utf-8")
    except (OSError, UnicodeError):
        return None
    if not _HERDR_INSTALL_HEADER.search(head):
        return None
    match = _HERDR_INTEGRATION_ID.search(head)
    return match.group(1) if match else None


def _safe_existing_dest(
    dest: Path, kind: str, owned: set, allowed_link_roots: Sequence[Path],
    source: Optional[Path] = None,
) -> None:
    if not (dest.exists() or dest.is_symlink()):
        return
    if str(dest) in owned:
        return
    # herdr overwrites its own integration file on reinstall; hearting now ships the
    # same integration, so adopt that file (the caller snapshots it first).
    if (source is not None and kind != "copytree" and not dest.is_symlink() and dest.is_file()
            and _herdr_integration_id(source) is not None
            and _herdr_integration_id(dest) == _herdr_integration_id(source)):
        return
    if dest.is_symlink():
        target = dest.resolve(strict=False)
        for root in allowed_link_roots:
            try:
                target.relative_to(root.resolve(strict=False))
                return
            except ValueError:
                continue
        raise ActivationError(f"foreign destination symlink collision: {dest} -> {target}")
    if kind == "copytree":
        marker_names = (".codex-plugin", ".claude-plugin")
        if dest.is_dir() and any((dest / marker).is_dir() for marker in marker_names):
            return
    raise ActivationError(f"destination collision; refusing to overwrite: {dest}")


def _preservable_user_changes(
    state: dict, desired: List[dict], user_changed: dict
) -> dict:
    """Return only changed paths that refresh would preserve as collisions."""
    allowed_link_roots = [
        Path(state[key]) for key in ("source_root", "active_root") if state.get(key)
    ]
    desired_by_dest = {item["dest"]: item for item in desired}
    preserved = {}
    for value, prior in user_changed.items():
        entry = desired_by_dest.get(value)
        try:
            _safe_existing_dest(
                Path(value),
                entry.get("kind", "remove") if entry else "remove",
                set(),
                allowed_link_roots,
                Path(entry["source"]) if entry and entry.get("source") else None,
            )
        except ActivationError as exc:
            if str(exc).startswith((
                "foreign destination symlink collision:",
                "destination collision; refusing to overwrite:",
            )):
                preserved[value] = prior
            else:
                raise
    return preserved


def _snapshot_record(dest: Path, backup_root: Path, index: int) -> dict:
    record = {
        "dest": str(dest),
        "state": "missing",
        "backup": None,
        "target": None,
        "preimage": safe_fs.capture_state(dest).public(),
    }
    if dest.is_symlink():
        record.update(state="symlink", target=os.readlink(dest))
    elif dest.exists():
        backup = backup_root / str(index)
        record.update(state="moved", backup=str(backup))
    return record


def _copy_snapshot(
    dest: Path,
    backup_root: Path,
    index: int,
    preserve_names: Sequence[str] = (),
) -> dict:
    if not dest.exists() and not dest.is_symlink():
        return {
            "dest": str(dest),
            "state": "missing-copy",
            "backup": None,
            "target": None,
            "preimage": safe_fs.capture_state(dest).public(),
        }
    if dest.is_symlink():
        return {
            "dest": str(dest),
            "state": "symlink-copy",
            "backup": None,
            "target": os.readlink(dest),
            "preimage": safe_fs.capture_state(dest).public(),
        }
    backup = backup_root / f"protected-{index}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_dir():
        preserved = set(preserve_names)
        for name in preserved:
            candidate = dest / name
            if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
                raise ActivationError(
                    f"preserved runtime state must be a real directory: {candidate}"
                )

        def ignore(directory: str, names: List[str]) -> List[str]:
            if Path(directory) != dest:
                return []
            return sorted(preserved.intersection(names))

        shutil.copytree(dest, backup, symlinks=True, ignore=ignore)
    else:
        shutil.copy2(dest, backup)
    return {
        "dest": str(dest),
        "state": "copied",
        "backup": str(backup),
        "target": None,
        "preserve_names": sorted(preserved) if dest.is_dir() else [],
        "preimage": safe_fs.capture_state(
            dest, exclude_names=preserve_names
        ).public(),
    }


def _restore(records: List[dict]) -> None:
    for record in reversed(records):
        dest = Path(record["dest"])
        state = record["state"]
        if "preimage" not in record or "postimage" not in record:
            raise ActivationError(
                f"ownership-unproved: rollback record lacks exact state: {dest}"
            )
        preserve_names = set(record.get("preserve_names") or ())
        try:
            preimage = safe_fs.PathState.from_public(record["preimage"])
            postimage = safe_fs.PathState.from_public(record["postimage"])
            current = safe_fs.capture_state(dest, exclude_names=preserve_names)
        except safe_fs.SafetyError as exc:
            raise ActivationError(str(exc)) from exc
        if current == preimage:
            continue
        if current != postimage:
            raise ActivationError(
                f"concurrent-successor: rollback preserves changed target: {dest}"
            )
        if state == "moved" and not Path(record["backup"]).exists():
            # Journal was flushed before the move and the process died between
            # those two operations.  The original destination is still intact.
            continue
        preserve_in_place = state == "copied" and preserve_names and dest.is_dir()
        if not preserve_in_place and state not in {"symlink", "symlink-copy"}:
            _remove_path(dest)
        if state in {"symlink", "symlink-copy"}:
            try:
                auth = safe_fs.authority(
                    dest,
                    owner="runtime-activation:journal-symlink-restore",
                    allowed_paths=(dest,),
                    expected=current,
                )
                safe_fs.atomic_write_symlink(
                    auth, record["target"], create_parents=True
                )
            except safe_fs.SafetyError as exc:
                raise ActivationError(str(exc)) from exc
        elif state == "moved":
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(record["backup"], str(dest))
        elif state == "copied":
            backup = Path(record["backup"])
            if backup.is_dir() and preserve_in_place:
                for child in dest.iterdir():
                    if child.name not in preserve_names:
                        _remove_path(child)
                shutil.copytree(backup, dest, symlinks=True, dirs_exist_ok=True)
            else:
                _remove_path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if backup.is_dir():
                    shutil.copytree(backup, dest, symlinks=True)
                else:
                    shutil.copy2(backup, dest)


def _write_journal(path: Path, runtime: str, status_value: str, records: List[dict]) -> None:
    _atomic_json(
        path,
        {
            "schema": SCHEMA,
            "runtime": runtime,
            "status": status_value,
            "records": records,
            "updated_at": _utc_now(),
        },
    )


def _journal_dest_allowed(runtime: str, dest: Path, scope: str) -> bool:
    home = paths.runtime_home(runtime, scope)
    if dest in set(_activation_protected_paths(runtime, scope)):
        return True
    exact = {
        "codex": {
            "hearting", "AGENTS.md", "agent-core", "agent-capabilities",
            "agent-roles", "agent-bin", "agent-hooks", "hooks.json", "agent-modes",
        },
        # Claude's activation-native names unioned with
        # `projector.claude_installer_owned_names()` (the same canonical
        # owner-name-set `_linked_entries()` unions in). A journal recording a
        # union-only name such as README.md/manifest.json/loops must stay
        # recoverable after a crash, or a partial reprojection can never be
        # repaired (INSTALL_LAYOUT.md "owner-name-set reconciliation").
        "claude": (
            {
                "hearting", "CLAUDE.md", "core", "capabilities", "roles",
                "agent-modes", "bin", "tools", "utilities", "scaffolds",
                "statusline.sh",
            }
            | projector.claude_installer_owned_names()
        ),
        "opencode": {
            "hearting", "AGENTS.md", "agent-core", "agent-capabilities",
            "agent-roles",
        },
    }[runtime]
    if dest.parent == home and dest.name in exact:
        return True
    containers = {
        "codex": {"skills", "agents"},
        "claude": {"skills", "agents", "commands", "hooks"},
        "opencode": {"skills", "agents", "commands"},
    }[runtime]
    try:
        relative = dest.relative_to(home)
    except ValueError:
        return False
    if len(relative.parts) == 2 and relative.parts[0] in containers:
        return True
    if (
        runtime in {"codex", "claude"}
        and len(relative.parts) == 3
        and relative.parts[0] == "agent-modes"
        and relative.parts[2].endswith(".md")
    ):
        return True
    if runtime == "opencode" and relative.parts == (
        "plugins", "hearting-guards.js"
    ):
        return True
    if runtime == "opencode" and relative.parts == (
        "plugins", "hearting-guards", "index.ts"
    ):
        return True
    return False


# destructive-ok: reason=remove only validated completed or recovered transaction roots; boundary=canonical transaction children below one runtime harness state directory
def _recover_transactions(runtime: str, scope: str = "global") -> None:
    tx_parent = paths.harness_state_dir(runtime, scope) / "transactions"
    if not tx_parent.is_dir():
        return
    for tx_root in sorted(tx_parent.iterdir(), key=lambda item: item.name):
        if not tx_root.is_dir() or tx_root.is_symlink():
            raise ActivationError(f"invalid activation transaction directory: {tx_root}")
        journal_path = tx_root / "journal.json"
        journal = _load_json(journal_path)
        if journal is None:
            # A process can die after mkdir and before the first atomic journal
            # replace.  An empty directory has no operation to recover; any
            # other journal-less contents remain suspicious and block.
            if not any(tx_root.iterdir()):
                tx_root.rmdir()
                continue
            raise ActivationError(f"activation transaction lacks journal: {tx_root}")
        if journal.get("runtime") != runtime or not isinstance(journal.get("records"), list):
            raise ActivationError(f"invalid activation transaction journal: {journal_path}")
        records = journal["records"]
        for record in records:
            dest = Path(record.get("dest", ""))
            _ensure_owned_destination(runtime, dest, scope)
            if not _journal_dest_allowed(runtime, dest, scope):
                raise ActivationError(f"transaction destination is not harness-owned: {dest}")
            state_value = record.get("state")
            if state_value not in {
                "missing", "symlink", "moved", "missing-copy", "symlink-copy", "copied"
            }:
                raise ActivationError(f"invalid transaction record state: {state_value}")
            if "preimage" not in record or "postimage" not in record:
                raise ActivationError(
                    f"ownership-unproved: transaction lacks exact state: {dest}"
                )
            backup = record.get("backup")
            if backup:
                try:
                    backup_root = (tx_root / "backup").resolve(strict=False)
                    resolved_backup = Path(backup).resolve(strict=False)
                    resolved_backup.relative_to(backup_root)
                except (ValueError, RuntimeError) as exc:
                    raise ActivationError(
                        f"transaction backup escapes journal root: {backup}"
                    ) from exc
                if state_value == "copied" and not resolved_backup.exists():
                    raise ActivationError(f"transaction config backup is missing: {backup}")
        if journal.get("status") != "committed":
            _restore(records)
        shutil.rmtree(tx_root)


def _trusted_owned(
    runtime: str, state: Optional[dict], desired: List[dict], scope: str
) -> tuple[set, dict]:
    """Treat activation.json as untrusted input before using deletion paths."""
    # Profile activation adopts only links that resolve to a canonical harness
    # manifest. This lets a refresh remove stale native links left by the
    # legacy all-skills installer without touching user-owned entries.
    trusted = {
        str(path) for path in _discovered_harness_links(runtime, scope)
    }
    if not state:
        return trusted, {}
    allowed = {item["dest"] for item in desired}
    roots = []
    for key in ("source_root", "active_root"):
        value = state.get(key)
        if value:
            roots.append(Path(value))
    for root in roots:
        try:
            allowed.update(item["dest"] for item in _linked_entries(runtime, root, scope))
        except ActivationError:
            pass

    user_changed = {}
    for item in state.get("owned_paths", []):
        value = item.get("dest")
        if not value:
            continue
        dest = Path(value)
        _ensure_runtime_destination(runtime, dest, scope)
        if value not in allowed and not _journal_dest_allowed(runtime, dest, scope):
            trusted.discard(value)
            continue
        if not (dest.exists() or dest.is_symlink()):
            trusted.discard(value)
            continue

        matches = False
        if item.get("kind") == "symlink" and dest.is_symlink():
            try:
                current_target = os.readlink(dest)
                matches = current_target == item.get("source")
                if not matches and _journal_dest_allowed(runtime, dest, scope):
                    raw_target = Path(current_target)
                    target = raw_target if raw_target.is_absolute() else dest.parent / raw_target
                    resolved_target = target.resolve(strict=False)
                    matches = any(
                        resolved_target.is_relative_to(root.resolve(strict=False))
                        for root in roots
                    )
            except (OSError, RuntimeError, ValueError):
                matches = False
        elif item.get("kind") == "copy" and not dest.is_symlink() and dest.is_file():
            try:
                current_digest = hashlib.sha256(dest.read_bytes()).hexdigest()
                expected = item.get("digest")
                if isinstance(expected, str):
                    matches = current_digest == expected
                else:
                    source = Path(item.get("source", ""))
                    matches = source.is_file() and not source.is_symlink() and (
                        dest.read_bytes() == source.read_bytes()
                    )
            except OSError:
                matches = False
        elif item.get("kind") == "copytree" and not dest.is_symlink() and dest.is_dir():
            try:
                expected = item.get("digest")
                if isinstance(expected, str):
                    matches = _tree_digest(dest) == expected
                else:
                    source = Path(item.get("source", ""))
                    matches = source.is_dir() and _tree_digest(dest) == _tree_digest(source)
            except (OSError, RuntimeError):
                matches = False

        if matches and (value in allowed or _journal_dest_allowed(runtime, dest, scope)):
            trusted.add(value)
        else:
            trusted.discard(value)
            user_changed[value] = item
    return trusted, user_changed


# destructive-ok: reason=discard the active runtime transaction after commit or exact rollback; boundary=one UUID transaction root created below runtime harness state
def _apply_transaction(
    runtime: str,
    desired: List[dict],
    previous: Optional[dict],
    mode: str,
    scope: str,
    source_roots: Sequence[Path] = (),
    protected_paths: Sequence[Path] = (),
    commit_callback=None,
) -> List[dict]:
    state_dir = paths.harness_state_dir(runtime, scope)
    tx_root = state_dir / "transactions" / uuid.uuid4().hex
    backup_root = tx_root / "backup"
    owned, user_changed = _trusted_owned(runtime, previous, desired, scope)
    desired_by_dest = {entry["dest"]: entry for entry in desired}
    removal = [Path(item) for item in owned if item not in desired_by_dest]
    removal.extend(Path(item) for item in user_changed if item not in desired_by_dest)

    allowed_link_roots = list(source_roots)
    if previous:
        for key in ("source_root", "active_root"):
            if previous.get(key):
                allowed_link_roots.append(Path(previous[key]))

    changed: List[dict] = []
    preserved: List[dict] = []
    snapshots: List[dict] = []
    seen = set()
    fail_after = int(os.environ.get("HARNESS_RUNTIME_FAIL_AFTER", "0") or "0")
    operation_count = 0
    journal_path = tx_root / "journal.json"
    tx_root.mkdir(parents=True, exist_ok=True)
    _write_journal(journal_path, runtime, "preparing", snapshots)

    try:
        for dest in protected_paths:
            _ensure_owned_destination(runtime, dest, scope)
            record = _copy_snapshot(dest, backup_root, len(snapshots))
            record["postimage"] = record["preimage"]
            snapshots.append(record)
            _write_journal(journal_path, runtime, "preparing", snapshots)

        for dest in removal + [Path(item["dest"]) for item in desired]:
            key = str(dest)
            if key in seen:
                continue
            seen.add(key)
            _ensure_owned_destination(runtime, dest, scope)
            kind = desired_by_dest.get(key, {}).get("kind", "remove")
            source = desired_by_dest.get(key, {}).get("source")
            try:
                _safe_existing_dest(dest, kind, owned, allowed_link_roots,
                                    Path(source) if source else None)
            except ActivationError as exc:
                prior = user_changed.get(key)
                if prior is None or not str(exc).startswith((
                    "foreign destination symlink collision:",
                    "destination collision; refusing to overwrite:",
                )):
                    raise
                preserved.append(dict(prior))
                continue
            record = _snapshot_record(dest, backup_root, len(snapshots))
            record["postimage"] = (
                safe_fs.PathState("missing").public()
                if record["state"] == "moved"
                else record["preimage"]
            )
            snapshots.append(record)
            _write_journal(journal_path, runtime, "preparing", snapshots)
            if record["state"] == "moved":
                Path(record["backup"]).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), record["backup"])

        _write_journal(journal_path, runtime, "applying", snapshots)

        for dest in removal:
            if any(entry.get("dest") == str(dest) for entry in preserved):
                continue
            for record in snapshots:
                if record["dest"] == str(dest):
                    record["postimage"] = safe_fs.PathState("missing").public()
                    break
            _write_journal(journal_path, runtime, "applying", snapshots)
            _remove_path(dest)
            for record in snapshots:
                if record["dest"] == str(dest):
                    record["postimage"] = safe_fs.capture_state(dest).public()
                    break
            _write_journal(journal_path, runtime, "applying", snapshots)
            operation_count += 1
            if fail_after and operation_count >= fail_after:
                raise ActivationError(f"injected failure after operation {operation_count}")

        for item in desired:
            source = Path(item["source"])
            dest = Path(item["dest"])
            if any(entry.get("dest") == str(dest) for entry in preserved):
                continue
            for record in snapshots:
                if record["dest"] == str(dest):
                    record["postimage"] = safe_fs.PathState("missing").public()
                    break
            _write_journal(journal_path, runtime, "applying", snapshots)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if item["kind"] == "copytree":
                _remove_path(dest)
                shutil.copytree(source, dest, symlinks=False)
            elif item["kind"] == "copy":
                current = safe_fs.capture_state(dest)
                try:
                    auth = safe_fs.authority(
                        dest,
                        owner=f"runtime-activation:{runtime}:{item['surface']}:copy",
                        allowed_paths=(dest,), allow_leaf_symlink=True,
                        expected=current,
                    )
                    safe_fs.atomic_write_bytes(
                        auth, source.read_bytes(), source.stat().st_mode & 0o777,
                        expected=current, create_parents=True,
                    )
                except (OSError, safe_fs.SafetyError) as exc:
                    raise ActivationError(str(exc)) from exc
            else:
                current = safe_fs.capture_state(dest)
                try:
                    auth = safe_fs.authority(
                        dest,
                        owner=f"runtime-activation:{runtime}:{item['surface']}",
                        allowed_paths=(dest,),
                        expected=current,
                    )
                    safe_fs.atomic_write_symlink(
                        auth, str(source), create_parents=True
                    )
                except safe_fs.SafetyError as exc:
                    raise ActivationError(str(exc)) from exc
            for record in snapshots:
                if record["dest"] == str(dest):
                    record["postimage"] = safe_fs.capture_state(dest).public()
                    break
            _write_journal(journal_path, runtime, "applying", snapshots)
            applied = dict(item)
            if item["kind"] == "copy":
                applied["digest"] = hashlib.sha256(dest.read_bytes()).hexdigest()
            elif item["kind"] == "copytree":
                applied["digest"] = _tree_digest(dest)
            changed.append(applied)
            operation_count += 1
            if fail_after and operation_count >= fail_after:
                raise ActivationError(f"injected failure after operation {operation_count}")

        owned_entries = changed + preserved
        if commit_callback is not None:
            try:
                commit_callback(owned_entries)
            finally:
                protected = {str(path) for path in protected_paths}
                for record in snapshots:
                    if record["dest"] in protected:
                        record["postimage"] = safe_fs.capture_state(
                            Path(record["dest"]),
                            exclude_names=record.get("preserve_names") or (),
                        ).public()
                _write_journal(journal_path, runtime, "applying", snapshots)
        _write_journal(journal_path, runtime, "committed", snapshots)
    except Exception:
        for record in snapshots:
            if "postimage" not in record:
                record["postimage"] = safe_fs.capture_state(
                    Path(record["dest"]),
                    exclude_names=record.get("preserve_names") or (),
                ).public()
        _restore(snapshots)
        shutil.rmtree(tx_root, ignore_errors=True)
        raise

    shutil.rmtree(tx_root, ignore_errors=True)
    return changed


def _projection_digest(entries: List[dict]) -> str:
    return _digest_paths(Path(item["source"]) for item in entries)


def _activation_owned_paths(
    runtime: str, source: Optional[str] = None, scope: str = "global"
) -> List[Path]:
    """Return the closed invocation rollback set.

    The runtime-local ``.harness`` directory is a container, not an owned leaf.
    Only its activation record and installer-created namespaces are admitted;
    runtime dispatch, managed sessions, and all unknown siblings remain outside
    the set even when they sit beneath that directory.
    """
    state = _load_json(_state_path(runtime, scope))
    roots: List[Path] = []
    if source:
        roots.append(_real_source(source))
    if state:
        for key in ("source_root", "active_root"):
            value = state.get(key)
            if isinstance(value, str):
                roots.append(Path(value))
    try:
        roots.append(paths.agent_home().resolve(strict=True))
    except (OSError, RuntimeError):
        pass

    destinations: List[Path] = [
        _state_path(runtime, scope),
        *(paths.harness_state_dir(runtime, scope) / name for name in (
            "transactions", "bundles", "config-backups", "disabled-plugins"
        )),
    ]
    if runtime == "claude":
        destinations.append(_config_path(runtime, scope, "settings.json"))
    elif runtime == "codex":
        destinations.extend(_activation_protected_paths(runtime, scope)[1:])

    discovery: set[Path] = set()
    for root in roots:
        try:
            discovery.update(Path(item["dest"]) for item in _linked_entries(runtime, root, scope))
        except ActivationError:
            continue
    destinations.extend(sorted(discovery, key=lambda item: str(item)))

    # A previous activation may have owned a projection that the current source
    # no longer contains.  Admit only the same projection policy's exact leaves;
    # legacy plugin/quarantine records are intentionally inert.
    if state:
        for item in state.get("owned_paths", []):
            if not isinstance(item, dict) or not isinstance(item.get("dest"), str):
                continue
            candidate = Path(item["dest"])
            if _journal_dest_allowed(runtime, candidate, scope):
                destinations.append(candidate)

    result: List[Path] = []
    seen: set[str] = set()
    for destination in destinations:
        key = str(destination)
        if key in seen:
            continue
        seen.add(key)
        result.append(destination)
    return result


def _test_after_capture_handshake(runtime: str, snapshot: dict) -> None:
    """Fixture-only acknowledgement barrier for an external dispatch writer."""
    fixture_raw = os.environ.get("HEARTING_FIXTURE_ROOT")
    handshake_raw = os.environ.get("HARNESS_RUNTIME_TEST_AFTER_CAPTURE_DIR")
    selected = os.environ.get("HARNESS_RUNTIME_TEST_AFTER_CAPTURE_RUNTIME")
    if not handshake_raw and not selected:
        return
    if selected != runtime:
        return
    if not fixture_raw or not handshake_raw:
        raise ActivationError("invalid after-capture fixture handshake configuration")

    fixture = Path(fixture_raw).expanduser().absolute().resolve(strict=False)
    directory = Path(handshake_raw).expanduser()
    if not directory.is_absolute():
        raise ActivationError("after-capture handshake directory must be absolute")
    directory = directory.resolve(strict=False)
    try:
        directory.relative_to(fixture)
    except ValueError as exc:
        raise ActivationError(
            f"after-capture handshake directory escapes fixture root: {directory}"
        ) from exc
    directory.mkdir(parents=True, exist_ok=True)

    canonical_raw = os.environ.get("AGENT_DISPATCH_JOBS")
    if not canonical_raw:
        raise ActivationError("after-capture fixture requires AGENT_DISPATCH_JOBS")
    canonical_path = Path(canonical_raw).expanduser()
    if not canonical_path.is_absolute():
        raise ActivationError("AGENT_DISPATCH_JOBS must be absolute for fixture handshake")
    canonical_jobs = canonical_path.resolve(strict=False)
    try:
        selection = dispatch_contract.resolve_global_registry(
            paths.agent_home(),
            str(canonical_path),
            1,
            "start",
            environ=os.environ,
        )
    except Exception as exc:
        raise ActivationError(f"after-capture canonical dispatch resolution failed: {exc}") from exc
    if selection.path != canonical_jobs:
        raise ActivationError(
            "after-capture canonical dispatch mismatch: "
            f"exported={canonical_jobs} resolved={selection.path}"
        )

    snapshot_root = Path(snapshot["root"]).resolve(strict=False)
    try:
        snapshot_root.relative_to(fixture)
    except ValueError as exc:
        raise ActivationError(
            f"after-capture snapshot root escapes fixture root: {snapshot_root}"
        ) from exc

    ready = directory / "ready.json"
    ack = directory / "ack.json"
    for path in (ready, ack):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ActivationError(f"after-capture handshake path is not a regular file: {path}")
        if path.exists():
            # destructive-ok: reason=replace one stale fixture handshake file; boundary=ready.json or ack.json below the contained test directory
            path.unlink()

    nonce = uuid.uuid4().hex
    ready_payload = {
        "schema": 1,
        "phase": "after-capture-before-activate",
        "runtime": runtime,
        "nonce": nonce,
        "canonical_jobs": str(canonical_jobs),
        "snapshot_root": str(snapshot_root),
    }
    temporary = directory / f".ready-{nonce}.tmp"
    try:
        temporary.write_text(
            json.dumps(ready_payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        # destructive-ok: reason=atomically publish the exact fixture ready signal; boundary=ready.json below the contained test directory
        os.replace(temporary, ready)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if ack.is_file() and not ack.is_symlink():
                try:
                    data = json.loads(ack.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ActivationError(f"invalid after-capture acknowledgement: {ack}: {exc}") from exc
                expected = {
                    "schema", "phase", "runtime", "nonce", "canonical_jobs",
                    "appended_sha256", "created_entries",
                }
                if set(data) != expected:
                    raise ActivationError(f"after-capture acknowledgement shape mismatch: {ack}")
                if (
                    data.get("schema") != 1
                    or data.get("phase") != "writer-complete"
                    or data.get("runtime") != runtime
                    or data.get("nonce") != nonce
                    or data.get("canonical_jobs") != str(canonical_jobs)
                    or not isinstance(data.get("appended_sha256"), str)
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", data["appended_sha256"])
                    or not isinstance(data.get("created_entries"), list)
                    or not all(isinstance(item, str) and item for item in data["created_entries"])
                ):
                    raise ActivationError(f"after-capture acknowledgement identity mismatch: {ack}")
                return
            time.sleep(0.01)
    finally:
        if temporary.exists() or temporary.is_symlink():
            # destructive-ok: reason=remove one bounded fixture handshake temporary; boundary=one temporary file below the contained test directory
            temporary.unlink()
    raise ActivationError(f"after-capture acknowledgement timed out: {ack}")


# destructive-ok: reason=discard a failed invocation snapshot; boundary=one mkdtemp rollback root created by this capture
def capture_runtime_state(
    runtime: str, source: Optional[str] = None, scope: str = "global"
) -> dict:
    """Capture the bounded activation surface for invocation-level rollback."""
    if runtime not in RUNTIMES:
        raise ActivationError(f"unsupported runtime: {runtime}")
    _validate_scope(runtime, scope)
    _validate_state_dir(runtime, scope)
    _recover_transactions(runtime, scope)
    full_copy_paths = _activation_owned_paths(runtime, source, scope)
    candidate_paths = list(full_copy_paths)
    locks = safe_fs.TargetLocks(candidate_paths)
    try:
        locks.__enter__()
    except safe_fs.SafetyError as exc:
        raise ActivationError(str(exc)) from exc
    snapshot_root = Path(tempfile.mkdtemp(prefix=f"harness-{runtime}-rollback-"))
    backup_root = snapshot_root / "backup"
    records: List[dict] = []
    seen = set()
    try:
        for dest in full_copy_paths:
            key = str(dest)
            if key in seen:
                continue
            seen.add(key)
            _ensure_owned_destination(runtime, dest, scope)
            record = _copy_snapshot(
                dest, backup_root, len(records))
            record["_preimage"] = safe_fs.capture_state(
                dest
            )
            records.append(record)
        for dest in full_copy_paths:
            key = str(dest)
            if key in seen:
                continue
            seen.add(key)
            _ensure_runtime_destination(runtime, dest, scope)
            if dest.exists() and not dest.is_symlink():
                # A regular foreign destination makes activation block before
                # mutation, so it does not need a potentially unbounded copy.
                continue
            record = _copy_snapshot(dest, backup_root, len(records))
            record["_preimage"] = safe_fs.capture_state(dest)
            records.append(record)
    except Exception:
        shutil.rmtree(snapshot_root, ignore_errors=True)
        locks.__exit__(None, None, None)
        raise
    snapshot = {
        "runtime": runtime,
        "scope": scope,
        "root": str(snapshot_root),
        "records": records,
        "_locks": locks,
        "_sealed": False,
    }
    try:
        _test_after_capture_handshake(runtime, snapshot)
    except Exception:
        discard_runtime_state(snapshot)
        raise
    return snapshot


def seal_runtime_state(snapshot: dict) -> None:
    """Seal exact postimages while the invocation locks remain held."""

    if snapshot.get("_sealed"):
        return
    for record in snapshot["records"]:
        postimage = safe_fs.capture_state(
            Path(record["dest"]),
            exclude_names=record.get("preserve_names") or (),
        )
        record["_postimage"] = postimage
        # `_restore()` is also used for crash-journal recovery and therefore
        # consumes the redacted/public form.  Seal both representations from
        # the same observation while the canonical target locks are held.
        record["postimage"] = postimage.public()
    snapshot["_sealed"] = True


def restore_runtime_state(snapshot: dict) -> None:
    runtime = snapshot["runtime"]
    scope = snapshot["scope"]
    if not snapshot.get("_sealed"):
        raise ActivationError("ownership-unproved: runtime snapshot postimage is not sealed")
    restore_records = []
    for record in snapshot["records"]:
        dest = Path(record["dest"])
        _ensure_owned_destination(runtime, dest, scope)
        current = safe_fs.capture_state(
            dest, exclude_names=record.get("preserve_names") or ()
        )
        preimage = record.get("_preimage")
        postimage = record.get("_postimage")
        if not isinstance(preimage, safe_fs.PathState) or not isinstance(
            postimage, safe_fs.PathState
        ):
            raise ActivationError(
                f"ownership-unproved: runtime snapshot state is incomplete: {dest}"
            )
        if current == preimage:
            continue
        if current != postimage:
            raise ActivationError(
                f"concurrent-successor: runtime rollback preserves changed target: {dest}"
            )
        restore_records.append(record)
    try:
        _restore(restore_records)
    finally:
        locks = snapshot.get("_locks")
        if isinstance(locks, safe_fs.TargetLocks):
            locks.__exit__(None, None, None)
            snapshot["_locks"] = None


# destructive-ok: reason=discard a consumed invocation snapshot; boundary=exact mkdtemp rollback root carried by the snapshot object
def discard_runtime_state(snapshot: dict) -> None:
    locks = snapshot.get("_locks")
    if isinstance(locks, safe_fs.TargetLocks):
        locks.__exit__(None, None, None)
        snapshot["_locks"] = None
    shutil.rmtree(snapshot["root"], ignore_errors=True)


def _entries_healthy(entries: List[dict]) -> tuple[bool, bool]:
    missing = False
    stale = False
    for item in entries:
        source = Path(item["source"])
        dest = Path(item["dest"])
        if not (dest.exists() or dest.is_symlink()):
            missing = True
            continue
        if item.get("kind") == "copytree":
            if _tree_digest(dest) != _tree_digest(source):
                stale = True
        elif item.get("kind") == "copy":
            try:
                if (
                    dest.is_symlink()
                    or not dest.is_file()
                    or dest.read_bytes() != source.read_bytes()
                    or (dest.stat().st_mode & 0o777) != (source.stat().st_mode & 0o777)
                ):
                    stale = True
            except OSError:
                stale = True
        elif not dest.is_symlink() or dest.resolve(strict=False) != source.resolve(strict=False):
            stale = True
    return missing, stale


def _bundle_checksum(active_root: Path) -> Optional[str]:
    metadata_path = active_root.parent / "bundle.json"
    if not metadata_path.is_file() or not active_root.is_dir():
        return None
    metadata = _load_json(metadata_path) or {}
    expected = metadata.get("checksum")
    if not isinstance(expected, str):
        return None
    if metadata.get("source_link"):
        # A linked bundle asserts two things and both must hold: the link still
        # names the release it recorded, AND that release still hashes to what was
        # sealed. Verifying only the link would make `bundle_stale` unfalsifiable
        # (status compares this value against the one it just came from), and a
        # release replaced in place under the same version tag would read as fresh.
        recorded = metadata.get("source_root")
        if not isinstance(recorded, str) or not active_root.is_symlink():
            return None
        if not _same_tree(active_root, Path(recorded)):
            return None
    return expected if _tree_digest(active_root) == expected else None


def _capture_replaced_installer_targets(
    previous: Optional[dict], desired: List[dict]
) -> dict:
    """Durable, write-once-per-dest record of what a dest pointed at before
    activation first took it over (INSTALL_LAYOUT.md "owner-name-set
    reconciliation"). Must run before `_apply_transaction` mutates any dest.

    Carries every prior entry forward byte-for-byte -- a refresh or
    re-activation never overwrites an already-recorded original target, only
    adds an entry the first time a *new* dest becomes activation-owned (e.g. a
    future owner-name-set union growing).
    """
    carried: dict = dict((previous or {}).get("replaced_installer_symlink_targets") or {})
    previously_owned_dests = {
        item.get("dest") for item in (previous or {}).get("owned_paths", []) if item.get("dest")
    }
    for item in desired:
        dest_str = item["dest"]
        if dest_str in carried or dest_str in previously_owned_dests:
            continue
        dest = Path(dest_str)
        if not dest.is_symlink():
            continue
        try:
            carried[dest_str] = os.readlink(dest)
        except OSError:
            continue
    return carried


def activate(
    runtime: str,
    mode: str,
    source: Optional[str] = None,
    scope: str = "global",
) -> dict:
    if runtime not in RUNTIMES:
        raise ActivationError(f"unsupported runtime: {runtime}")
    if mode not in MODES:
        raise ActivationError(f"unsupported activation mode: {mode}")

    _validate_scope(runtime, scope)
    _validate_external_plugin_conflicts(runtime, scope)
    _validate_state_dir(runtime, scope)
    _recover_transactions(runtime, scope)
    source_root = _real_source(source)
    _validate_source_symlinks(source_root)
    revision = source_revision(source_root)
    previous_path = _state_path(runtime, scope)
    previous = _load_json(previous_path)

    active_root = source_root
    if mode == "packaged":
        active_root = _build_bundle(runtime, source_root, revision, scope)

    # This is an independent user preference seed, not an activation-owned
    # projection. A later refresh, rollback, or uninstall must not remove it.
    try:
        model_config_action = user_model_config.seed_model_config(
            runtime,
            active_root / "adapters" / runtime / "config" / "models.conf",
            paths.runtime_home(runtime, scope),
        )
    except user_model_config.UserModelConfigError as exc:
        raise ActivationError(str(exc)) from exc

    desired = _desired_entries(runtime, mode, source_root, active_root, revision, scope)
    digest = _projection_digest(desired)
    packaged_checksum = _bundle_checksum(active_root) if mode == "packaged" else None
    if mode == "packaged" and packaged_checksum is None:
        raise ActivationError(f"packaged bundle checksum mismatch: {active_root}")
    # Must run before _apply_transaction touches any dest below -- this is
    # the only point where "what did the installer leave here" is still
    # observable for a dest activation is about to adopt for the first time.
    replaced_installer_symlink_targets = _capture_replaced_installer_targets(previous, desired)
    context_report_lines: List[str] = []

    def commit_state(owned):
        local_hook_trust_record = None
        config_changes = _prepare_runtime_config(runtime, active_root, previous, scope,
                                                 native_locked=True)
        for change in config_changes:
            context_report_lines.extend(change.get("report_lines", []))
        previous_managed = (previous or {}).get("managed_config", {})
        if runtime == "codex" and scope == "global":
            try:
                import codex_hook_trust
                prior_trust = previous_managed.get("codex_hook_trust") if isinstance(previous_managed, dict) else None
                hooks_file = active_root / "adapters/codex/hooks/hooks.json"
                trust_result = codex_hook_trust.record_managed_trust(
                    paths.runtime_home("codex", scope), hooks_file, prior_trust
                )
                local_hook_trust_record = trust_result.get("record")
                context_report_lines.extend(trust_result.get("report_lines", []))
            except Exception as exc:
                context_report_lines.append(
                    f"Codex hook trust skipped: {str(exc).splitlines()[0][:180]}"
                )
        previous_native = previous_managed.get("native_context", {}) if isinstance(previous_managed, dict) else {}
        native_context = dict(previous_native) if isinstance(previous_native, dict) else {}
        projected_native = next((change.get("native_context") for change in config_changes
                                 if isinstance(change.get("native_context"), dict)), None)
        if projected_native:
            for adapter_name, adapter_record in projected_native.items():
                old_adapter = native_context.get(adapter_name, {})
                old_adapter = dict(old_adapter) if isinstance(old_adapter, dict) else {}
                new_adapter = dict(adapter_record) if isinstance(adapter_record, dict) else {}
                old_keys = old_adapter.get("keys", {})
                new_keys = new_adapter.get("keys", {})
                merged_keys = dict(old_keys) if isinstance(old_keys, dict) else {}
                if isinstance(new_keys, dict):
                    merged_keys.update(new_keys)
                old_adapter.update(new_adapter)
                old_adapter["keys"] = merged_keys
                native_context[adapter_name] = old_adapter
        state = {
            "schema": SCHEMA,
            "runtime": runtime,
            "mode": mode,
            "scope": scope,
            "source_root": str(source_root),
            "source_revision": revision,
            "active_root": str(active_root),
            "active_revision": revision,
            "bundle_checksum": packaged_checksum,
            "activated_projection_digest": digest,
            "owned_paths": owned,
            "discovery_paths": [item["dest"] for item in owned],
            "session_action": SESSION_ACTIONS[runtime],
            "external_dependencies": [],
            "managed_config": {
                "native_context": native_context,
                "claude_hooks": next(
                    (
                        change["managed_hooks"]
                        for change in config_changes
                        if change.get("kind") == "claude-settings-merged"
                    ),
                    {},
                ),
                "claude_values": next(
                    (
                        change["managed_values"]
                        for change in config_changes
                        if change.get("kind") == "claude-settings-merged"
                    ),
                    {},
                ),
                "claude_conflicts": next(
                    (
                        change["conflicts"]
                        for change in config_changes
                        if change.get("kind") == "claude-settings-merged"
                    ),
                    [],
                ),
                "codex_identity": next(
                    (
                        change.get("managed_config")
                        for change in config_changes
                        if change.get("kind") == "codex-config-merged"
                        and change.get("managed_config")
                    ),
                    None,
                ),
                "codex_tui": next(
                    (change["tui_record"] for change in config_changes if "tui_record" in change),
                    (previous.get("managed_config") or {}).get("codex_tui") if previous else None,
                ),
                "opencode_tui": next(
                    (
                        change.get("managed_config")
                        for change in config_changes
                        if change.get("kind") == "opencode-tui-seeded"
                        and change.get("managed_config")
                    ),
                    None,
                ),
                "codex_herdr_hook": next(
                    (change.get("managed_config") for change in config_changes
                     if change.get("action") == "herdr-codex-session-hook"
                     and change.get("managed_config")),
                    previous_managed.get("codex_herdr_hook") if isinstance(previous_managed, dict) else None,
                ),
                "herdr_opencode": next(
                    (change.get("managed_config") for change in config_changes
                     if change.get("kind") == "herdr-opencode-registration"),
                    previous_managed.get("herdr_opencode") if isinstance(previous_managed, dict) else None,
                ),
                "codex_conflicts": next(
                    (
                        change["conflicts"]
                        for change in config_changes
                        if change.get("kind") == "codex-config-merged"
                    ),
                    [],
                ),
                "model_config": model_config_action,
                "codex_hook_trust": local_hook_trust_record or (
                    previous_managed.get("codex_hook_trust") if isinstance(previous_managed, dict) else None
                ),
            },
            "replaced_installer_symlink_targets": replaced_installer_symlink_targets,
            "activated_at": _utc_now(),
        }
        _atomic_json(previous_path, state)

    _apply_transaction(
        runtime,
        desired,
        previous,
        mode,
        scope,
        source_roots=(source_root, active_root),
        protected_paths=_activation_protected_paths(runtime, scope),
        commit_callback=commit_state,
    )
    retired_memory_state.retire()  # distiller-era state files, once; silent
    report = status(runtime, scope)
    report["preserved"] = list(report.get("user_changed", []))
    report["report_lines"] = list(context_report_lines) + list(report.get("report_lines", []))
    return report


def _status_missing(runtime: str, scope: str) -> dict:
    config_path = paths.runtime_home(runtime, scope) / "agent-config" / "models.conf"
    return {
        "runtime": runtime,
        "mode": None,
        "source_root": None,
        "source_revision": None,
        "active_revision": None,
        "projection_digest": None,
        "discovery_paths": [],
        "duplicate_sources": duplicate_sources(runtime, scope),
        "user_changed": [],
        "preserved": [],
        "report_lines": [],
        "config_conflicts": [],
        "model_config_path": str(config_path),
        "model_config_present": config_path.is_file() and not config_path.is_symlink(),
        "model_config_source": None,
        "model_config_reason": "activation-missing",
        "freshness": "missing",
        "session_action": SESSION_ACTIONS[runtime],
        "external_dependencies": [],
        "session_consistency": "not-active",
        "next_action": f"harness runtime activate --runtime {runtime}",
    }


def status(runtime: str, scope: str = "global") -> dict:
    if runtime not in RUNTIMES:
        raise ActivationError(f"unsupported runtime: {runtime}")
    _validate_scope(runtime, scope)
    state = _load_json(_state_path(runtime, scope))
    if state is None:
        return _status_missing(runtime, scope)

    source_root = Path(state["source_root"])
    active_root = Path(state.get("active_root") or state["source_root"])
    source_rev = source_revision(source_root) if source_root.exists() else "missing"
    changed_owned_items = {}
    user_changed_items = {}
    try:
        entries = _desired_entries(
            runtime,
            state["mode"],
            source_root,
            active_root,
            state["active_revision"],
            scope,
        )
        digest = _projection_digest(entries)
        missing, stale = _entries_healthy(entries)
        unexpected = _unexpected_harness_links(runtime, entries, scope)
        _, changed_owned_items = _trusted_owned(runtime, state, entries, scope)
        user_changed_items = _preservable_user_changes(
            state, entries, changed_owned_items
        )
        desired_destinations = {item["dest"] for item in entries}
        for item in state.get("owned_paths", []):
            dest = Path(item.get("dest", ""))
            if (
                item.get("kind") == "symlink"
                and str(dest) not in desired_destinations
                and str(dest) not in changed_owned_items
                and dest.is_symlink()
                and _journal_dest_allowed(runtime, dest, scope)
            ):
                missing = True
    except ActivationError:
        entries, digest, missing, stale, unexpected = [], None, True, False, []
        changed_owned_items = {}
        user_changed_items = {}

    config_conflicts: List[str] = []
    if runtime == "claude":
        config_missing, config_conflicts = _claude_settings_health(active_root, scope)
        if config_missing:
            missing = True
    elif runtime == "codex":
        config_missing, config_conflicts = _codex_identity_health(active_root, scope)
        if config_missing:
            missing = True
    model_config_path = paths.runtime_home(runtime, scope) / "agent-config" / "models.conf"
    model_config_present = model_config_path.is_file() and not model_config_path.is_symlink()
    try:
        _, model_receipt = model_config.resolve_config(
            runtime,
            runtime=paths.runtime_home(runtime, scope),
            source_root=active_root,
        )
        model_config_source = model_receipt.source
        model_config_reason = model_receipt.reason
    except model_config.ModelConfigError as exc:
        model_config_source = "unavailable"
        model_config_reason = f"shipped-unusable:{exc}"
        missing = True
    bundle_stale = False
    if state.get("mode") == "packaged":
        current_bundle_checksum = _bundle_checksum(active_root)
        bundle_stale = (
            current_bundle_checksum is None
            or current_bundle_checksum != state.get("bundle_checksum")
        )

    user_changed = sorted(user_changed_items)
    duplicates = duplicate_sources(runtime, scope)
    runtime_home = paths.runtime_home(runtime, scope)
    duplicates.extend(
        f"user-changed:{Path(value).relative_to(runtime_home).as_posix()}"
        for value in user_changed
    )
    duplicates.extend(
        f"unmanaged-extra:{path.relative_to(paths.runtime_home(runtime, scope))}"
        for path in unexpected
    )
    duplicates.extend(f"config-conflict:{item}" for item in config_conflicts)
    if duplicates:
        freshness = "duplicate"
        if config_conflicts and user_changed:
            label = "Claude settings" if runtime == "claude" else f"{runtime.capitalize()} config"
            conflict_paths = ",".join(config_conflicts)
            next_action = (
                f"resolve {label} conflicts ({conflict_paths}) and review preserved "
                f"user-changed paths ({', '.join(user_changed)}), then "
                f"harness runtime refresh --runtime {runtime}"
            )
        elif user_changed:
            next_action = (
                f"review preserved user-changed paths ({', '.join(user_changed)}); restore or remove "
                f"them before harness runtime refresh --runtime {runtime}"
            )
        elif config_conflicts:
            label = "Claude settings" if runtime == "claude" else f"{runtime.capitalize()} config"
            next_action = (
                f"resolve {label} conflicts ({','.join(config_conflicts)}), "
                f"then harness runtime refresh --runtime {runtime}"
            )
        else:
            next_action = (
                f"harness runtime activate --runtime {runtime} --mode {state['mode']} "
                f"--source {source_root}"
            )
    elif missing:
        freshness = "missing"
        next_action = f"harness runtime refresh --runtime {runtime}"
    elif stale or bundle_stale:
        freshness = "cache-stale"
        next_action = f"harness runtime refresh --runtime {runtime}"
    elif state["mode"] == "packaged" and source_rev != state["active_revision"]:
        freshness = "source-ahead"
        next_action = f"harness runtime refresh --runtime {runtime}"
    elif digest != state.get("activated_projection_digest"):
        freshness = "session-reload-needed"
        action_values = list(SESSION_ACTIONS[runtime].values())
        next_action = "restart-required" if "restart-required" in action_values else "new-session"
    else:
        freshness = "fresh"
        next_action = "none"

    return {
        "runtime": runtime,
        "mode": state["mode"],
        "source_root": str(source_root),
        "source_revision": source_rev,
        "active_revision": state["active_revision"] if state["mode"] == "packaged" else source_rev,
        "active_root": str(active_root),
        "bundle_checksum": state.get("bundle_checksum"),
        "projection_digest": digest,
        "discovery_paths": [item["dest"] for item in entries],
        "duplicate_sources": duplicates,
        "user_changed": user_changed,
        "preserved": user_changed,
        "report_lines": [f"preserved user-changed: {value}" for value in user_changed],
        "config_conflicts": config_conflicts,
        "model_config_path": str(model_config_path),
        "model_config_present": model_config_present,
        "model_config_source": model_config_source,
        "model_config_reason": model_config_reason,
        "freshness": freshness,
        "session_action": SESSION_ACTIONS[runtime],
        "external_dependencies": [],
        "session_consistency": (
            "pinned-immutable-root"
            if state["mode"] == "packaged"
            else "mutable-linked-debug"
        ),
        "next_action": next_action,
        "executable_ingress": _executable_ingress_report(runtime),
    }


def _executable_ingress_report(runtime: str) -> dict:
    """Both interactive CLIs remain vendor-owned after launcher retirement."""
    return {"owner": "vendor", "hearting_ingress": "none"}


def reconnect_peer_obligations(active_root: str | Path) -> bool:
    """Reuse the existing reconnect callback after a successful activation."""
    root = Path(active_root).resolve()
    utility = root / "utilities" / "peer-steward.py"
    if not utility.is_file():
        return False
    env = os.environ.copy()
    env["AGENT_HOME"] = str(root)
    try:
        subprocess.Popen(
            [sys.executable, str(utility), "__ensure-obligations",
             "--lifecycle-root", str(root)], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def refresh(runtime: str, scope: str = "global") -> dict:
    _validate_scope(runtime, scope)
    state = _load_json(_state_path(runtime, scope))
    if state is None:
        raise ActivationError(f"{runtime} has no activation state")
    return activate(runtime, state["mode"], state["source_root"], scope)


def _unmerge_claude_settings(
    state: dict, scope: str, dry_run: bool = False, *, config_path: Path | None = None
) -> List[str]:
    """Remove what the activation record still proves it owns.

    Hooks match by matcher and command, so a copy the user tuned leaves with
    the hooks it runs; statusLine and env values must still match exactly.
    """
    managed_config = state.get("managed_config", {})
    if not isinstance(managed_config, dict):
        return []
    managed_hooks = managed_config.get("claude_hooks", {})
    managed_values = managed_config.get("claude_values", {})
    native_context = managed_config.get("native_context", {})
    if not isinstance(managed_hooks, dict):
        managed_hooks = {}
    if not isinstance(managed_values, dict):
        managed_values = {}
    if not managed_hooks and not managed_values and not native_context:
        return []
    config = config_path or _config_path("claude", scope, "settings.json")
    if not config.exists():
        return []
    try:
        data = _read_json_object(config, "Claude settings")
    except ActivationError:
        return []
    changed = False
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event, entries in managed_hooks.items():
            current = hooks.get(event)
            if not isinstance(current, list) or not isinstance(entries, list):
                continue
            kept = _without_hooks(current, _event_hook_keys(entries))
            if kept != current:
                changed = True
                if kept:
                    hooks[event] = kept
                else:
                    hooks.pop(event)

    managed_statusline = managed_values.get("statusLine")
    if managed_statusline is not None and data.get("statusLine") == managed_statusline:
        data.pop("statusLine")
        changed = True
    managed_env = managed_values.get("env", {})
    current_env = data.get("env")
    if isinstance(managed_env, dict) and isinstance(current_env, dict):
        for key, value in managed_env.items():
            if key == "CLAUDE_CODE_AUTO_COMPACT_WINDOW":
                continue
            if current_env.get(key) == value:
                current_env.pop(key)
                changed = True
        if not current_env:
            data.pop("env")
    claude_native = native_context.get("claude", {}) if isinstance(native_context, dict) else {}
    native_keys = claude_native.get("keys", {}) if isinstance(claude_native, dict) else {}
    window_record = native_keys.get("env.CLAUDE_CODE_AUTO_COMPACT_WINDOW") if isinstance(native_keys, dict) else None
    current_env = data.get("env")
    if isinstance(current_env, dict):
        key = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
        from native_context import _native_restore_action
        action, baseline = _native_restore_action(
            window_record, key in current_env, current_env.get(key), adapter="claude")
        if action == "restore":
            current_env[key] = baseline
            changed = True
        elif action == "delete":
            current_env.pop(key)
            changed = True
            if not current_env and isinstance(window_record, dict) and window_record.get("env_present") is False:
                data.pop("env", None)
    if changed and not dry_run:
        _atomic_json(config, data)
    return [str(config)] if changed else []


def deactivate(runtime: str, scope: str = "global", dry_run: bool = False) -> dict:
    """Remove activation-owned projections so uninstall leaves no harness surface.

    Only paths the activation state provably owns are touched; user-owned files
    and foreign links survive via the same trust gate refresh uses.
    """
    if runtime not in RUNTIMES:
        raise ActivationError(f"unsupported runtime: {runtime}")
    _validate_scope(runtime, scope)
    state_file = _state_path(runtime, scope)
    state = _load_json(state_file)
    if state is None:
        return {"runtime": runtime, "status": "not-active", "removed": [],
                "preserved": [], "restored_configs": [], "report_lines": []}
    desired: List[dict] = []
    try:
        source_root = Path(state["source_root"])
        active_root = Path(state.get("active_root") or state["source_root"])
        desired = _desired_entries(
            runtime,
            state["mode"],
            source_root,
            active_root,
            state["active_revision"],
            scope,
        )
    except ActivationError:
        desired = []
    trusted, user_changed_items = _trusted_owned(runtime, state, desired, scope)
    preserved = sorted(user_changed_items)
    desired_by_dest = {item["dest"]: item for item in desired}
    replaced_installer_symlink_targets = state.get("replaced_installer_symlink_targets") or {}
    removed = []
    restored_installer_links = []
    for value in sorted(trusted):
        dest = Path(value)
        if not dest.is_symlink() and not dest.exists():
            continue
        original_target = replaced_installer_symlink_targets.get(value)
        restore_here = False
        if original_target is not None and dest.is_symlink():
            # Restore the installer's exact prior target only when the link
            # activation put there is still exactly what activation put
            # there -- a user who repointed it after activation keeps their
            # change untouched (INSTALL_LAYOUT.md owner-name-set
            # reconciliation, deactivate/uninstall restore condition).
            desired_entry = desired_by_dest.get(value)
            try:
                current_raw_target = os.readlink(dest)
            except OSError:
                current_raw_target = None
            if (
                desired_entry is not None
                and current_raw_target is not None
                and current_raw_target == desired_entry["source"]
            ):
                restore_here = True
        if not dry_run:
            _remove_path(dest)
            if restore_here:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.symlink_to(original_target)
        if restore_here:
            restored_installer_links.append(value)
        else:
            removed.append(value)
    restored = []
    if runtime == "claude":
        restored = _unmerge_claude_settings(state, scope, dry_run=dry_run)
    elif runtime == "codex":
        restored = _unmerge_codex_identity_config(state, scope, dry_run=dry_run)
        managed_hook = (state.get("managed_config") or {}).get("codex_herdr_hook")
        hook_path = paths.runtime_home("codex", scope) / "hooks.json"
        if herdr_integration.remove_managed_hook_file(hook_path, managed_hook, dry_run=dry_run):
            restored.append(str(hook_path))
    elif runtime == "opencode":
        restored = _unseed_opencode_tui_entry(state, scope, dry_run=dry_run)
        managed_registration = (state.get("managed_config") or {}).get("herdr_opencode")
        restored.extend(herdr_integration.remove_opencode_configs(
            paths.runtime_home("opencode", scope), managed_registration or {}, dry_run=dry_run
        ))
    if not dry_run:
        bundles = paths.harness_state_dir(runtime, scope) / "bundles"
        if bundles.is_dir() and not bundles.is_symlink():
            _remove_path(bundles)
        _remove_path(state_file)
    return {
        "runtime": runtime,
        "status": "planned" if dry_run else "deactivated",
        "removed": removed,
        "preserved": preserved,
        "report_lines": [f"preserved user-changed: {value}" for value in preserved],
        "restored_installer_links": restored_installer_links,
        "restored_configs": restored,
    }


# The subtrees every install surface ships identically at a given version. A whole-tree
# digest cannot be compared across surface KINDS — a packaged runtime bundle is built from
# a checkout while the managed release is an extracted archive, so they differ in files
# that carry no behavior (`.hearting-release.json`, VCS metadata). These seven are the
# portable contract plus the executable trees, and they are byte-identical between a
# bundle and a release cut from the same revision (verified 2026-08-19 against v2.54.0).
SHARED_SURFACE_SUBTREES = (
    "adapters", "capabilities", "core", "hooks", "roles", "tools", "utilities",
)


def _surface_ephemeral(path: Path) -> bool:
    """Runtime output that lives inside the source tree and must not read as skew.

    `.gitignore` declares `loops/*.log` and `adapters/*/loops/*.log` ephemeral — loop runs
    write them where they run. A packaged bundle is COPIED from a checkout and carries
    whatever the working tree happened to hold; a release archive is built from tracked
    content and never does. Digesting them made `adapters` differ permanently with no code
    difference at all (observed 2026-08-19: oncall.log and study.log, months old). A
    standing false alarm is worse than no check, so they are excluded here.

    Scoped to a `loops` parent rather than the `.log` suffix, because the repository also
    tracks real `.log` fixtures under `tests/fixtures/route/` whose content IS source.
    """
    return path.suffix == ".log" and path.parent.name == "loops"


def _surface_digests(root: Path) -> Dict[str, str]:
    return {
        name: _tree_digest(root / name, skip=_surface_ephemeral)
        for name in SHARED_SURFACE_SUBTREES
    }


def surface_skew(release_root: Optional[Path] = None, scope: str = "global") -> dict:
    """Compare every install surface against the others, by content.

    The runtime surfaces (`~/.claude`, `~/.codex`, `~/.config/opencode`) and the managed
    release tree are updated by DIFFERENT commands — `harness runtime activate|refresh`
    versus `harness update` — so they drift apart silently. `status`/`doctor` only ever
    asked "is this runtime fresh against its source", which cannot see that the tree the
    CLI launchers run from is a different version: `~/.local/bin/{fleet,mem,harness}` all
    resolve into the managed release, and that surface appeared in no diagnostic at all.
    On 2026-08-19 a runtime activation left the release tree behind and `fleet` kept
    running the old renderer with every existing check reporting success.

    Identity is a per-subtree content digest rather than a revision string because the two
    surface kinds label themselves differently (a git SHA versus a semver tag) and neither
    records the other's label. Comparing content also names WHICH subtree diverged, which
    is the actionable part — `tools` diverging is exactly the failure above.

    A surface that is not installed is reported absent and never counts as skew.
    """
    surfaces: List[dict] = []
    for runtime in RUNTIMES:
        state = _load_json(_state_path(runtime, scope))
        root = Path(state["active_root"]) if state and state.get("active_root") else None
        present = bool(root and root.is_dir())
        surfaces.append({
            "name": runtime,
            "kind": "runtime",
            "root": str(root) if root else None,
            "label": (state or {}).get("active_revision"),
            "present": present,
            "digests": _surface_digests(root) if present else {},
        })
    if release_root is not None:
        root = Path(release_root)
        present = root.is_dir()
        marker = _load_json(root / ".hearting-release.json") if present else None
        surfaces.append({
            "name": "release",
            "kind": "managed-release",
            "root": str(root),
            "label": (marker or {}).get("version"),
            "present": present,
            "digests": _surface_digests(root) if present else {},
        })
    live = [surface for surface in surfaces if surface["present"]]
    skewed = []
    for name in SHARED_SURFACE_SUBTREES:
        seen = {surface["digests"][name] for surface in live}
        if len(seen) > 1:
            skewed.append({
                "subtree": name,
                "groups": sorted(
                    {
                        digest: sorted(
                            s["name"] for s in live if s["digests"][name] == digest
                        )
                        for digest in seen
                    }.values()
                ),
            })
    return {
        "surfaces": surfaces,
        "skewed": skewed,
        "ok": not skewed,
        "compared": [surface["name"] for surface in live],
    }


BUNDLE_STATE_DIR_NAMES = (".agent_reports", ".claude_reports")
BUNDLE_STATE_MAX_FINDINGS = 8


def _bundle_source_is_link(active_root: Path) -> bool:
    """True when this bundle addresses its source by symlink rather than copying it."""
    return Path(active_root).is_symlink()


def bundle_runtime_state(active_root: Path) -> List[str]:
    """Return runtime-state directories written inside an immutable release bundle.

    ``<runtime-home>/.harness/bundles/<id>/source`` is replaced wholesale on
    update, so anything written under it is silently lost and breaks the bundle's
    byte-identity with the release it was built from.  Only the ACTIVE bundle is
    scanned: retired bundles are inert, and reporting them would pin every later
    install to a historical mistake.

    ``utilities/artifact-root.sh`` refuses to select such a root, so a finding
    here is either residue from before that refusal or a writer that does not
    resolve through it.  The walk runs on the ``doctor`` path only, never in
    ``status`` -- the latter is on the statusline and verify hot paths.
    """

    parts = active_root.parts
    if ".harness" not in parts or "bundles" not in parts:
        return []
    if _bundle_source_is_link(active_root):
        # A linked bundle owns no tree: walking it would descend into the managed
        # release and report the release's own contents as bundle residue. The
        # docstring's premise ("replaced wholesale on update, so writes are lost")
        # is true of a copy and false of a link -- a release is versioned by path
        # and its cleanliness is the release's invariant, not this bundle's.
        return []
    found: List[str] = []
    for current, dirnames, _files in os.walk(active_root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in BUNDLE_STATE_DIR_NAMES:
            if name in dirnames:
                found.append(str(Path(current) / name))
                dirnames.remove(name)
        if len(found) >= BUNDLE_STATE_MAX_FINDINGS:
            break
    return sorted(found)


def doctor(runtime: str, strict: bool = False, scope: str = "global") -> dict:
    report = status(runtime, scope)
    hard = {"missing", "cache-stale", "duplicate", "unsupported"}
    if strict:
        hard.add("source-ahead")
    ok = report["freshness"] not in hard
    active_root = report.get("active_root")
    state_in_bundle = bundle_runtime_state(Path(active_root)) if active_root else []
    # Advisory by default, a failure under --strict, mirroring how `source-ahead`
    # is promoted above: the projection links can be perfectly healthy while the
    # immutable tree behind them is being written to.
    if strict and state_in_bundle:
        ok = False
    return {
        "runtime": runtime,
        "ok": ok,
        "strict": strict,
        "freshness": report["freshness"],
        "duplicate_sources": report["duplicate_sources"],
        "config_conflicts": report.get("config_conflicts", []),
        "bundle_runtime_state": state_in_bundle,
        "next_action": report["next_action"],
        "status": report,
    }
