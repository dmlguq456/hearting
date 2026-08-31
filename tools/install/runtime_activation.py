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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import paths

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))
UTILITIES_ROOT = Path(__file__).resolve().parents[2] / "utilities"
if str(UTILITIES_ROOT) not in sys.path:
    sys.path.insert(0, str(UTILITIES_ROOT))

import harness_manifest
import model_config
import projector
import safe_fs
import user_model_config


RUNTIMES = ("claude", "codex", "opencode")
MODES = ("linked", "packaged")
SCHEMA = 2
CLAUDE_MANAGED_ENV_KEYS = ("MEM_DISTILL_ENABLE",)
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


def source_revision(root: Path) -> str:
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
) -> List[dict]:
    if not source.is_dir():
        return []
    entries = []
    for item in sorted(source.glob(pattern)):
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
            (source_root / "adapters/codex/hooks/hooks.json", home / "hooks.json", "hook_config"),
        ]
        entries.extend(_entry(src, dst, surface) for src, dst, surface in fixed)
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
            _children(source_root / "adapters/claude/hooks", home / "hooks", "hook_config")
        )

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


def _bundle_ignore(_directory: str, names: List[str]) -> set:
    return {name for name in names if name in _IGNORE_NAMES}


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


# destructive-ok: reason=publish or discard one immutable runtime bundle staging tree; boundary=one revision bundle and sibling staging directory below runtime state
def _build_bundle(runtime: str, source_root: Path, revision: str, scope: str) -> Path:
    if "+dirty:" in revision:
        raise ActivationError("packaged activation refuses a dirty git source")
    state_dir = paths.harness_state_dir(runtime, scope)
    _validate_source_symlinks(source_root)
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", revision)[:48]
    key += "-" + _tree_digest(source_root)[:12]
    bundle = state_dir / "bundles" / key
    bundle_source = bundle / "source"
    metadata_path = bundle / "bundle.json"
    if metadata_path.exists() and bundle_source.is_dir():
        metadata = _load_json(metadata_path) or {}
        actual_checksum = _tree_digest(bundle_source)
        if (
            metadata.get("source_revision") == revision
            and metadata.get("checksum") == actual_checksum
        ):
            return bundle_source

    staging = state_dir / "bundles" / (".staging-" + uuid.uuid4().hex)
    staging_source = staging / "source"
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(
            source_root,
            staging_source,
            symlinks=True,
            ignore=_bundle_ignore,
        )
        _atomic_json(
            staging / "bundle.json",
            {
                "schema": SCHEMA,
                "runtime": runtime,
                "source_root": str(source_root),
                "source_revision": revision,
                "checksum": _tree_digest(staging_source),
                "created_at": _utc_now(),
                "external_dependencies": [],
            },
        )
        if bundle.exists():
            shutil.rmtree(bundle)
        os.replace(staging, bundle)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return bundle_source


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


def _plugin_roots(runtime: str, scope: str = "global") -> List[Path]:
    home = paths.runtime_home(runtime, scope)
    if runtime == "codex":
        marker, expected = ".codex-plugin/plugin.json", "hearting-codex"
    elif runtime == "claude":
        marker, expected = ".claude-plugin/plugin.json", "hearting-claude"
    else:
        return []
    roots = []
    cache = home / "plugins/cache"
    if not cache.is_dir():
        return roots
    for manifest in cache.glob(f"**/{marker}"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("name") == expected:
            roots.append(manifest.parent.parent)
    return sorted(set(roots), key=lambda item: str(item))


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
        "opencode": ("skills/*", "agents/*.md", "commands/*.md"),
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


def _config_backup(runtime: str, scope: str, path: Path, original: bytes) -> dict:
    digest = hashlib.sha256(original).hexdigest()
    backup = (
        paths.harness_state_dir(runtime, scope)
        / "config-backups"
        / f"{path.name}.{digest}"
    )
    if not backup.exists():
        _atomic_bytes(backup, original)
    return {"path": str(backup), "sha256": digest}


def _config_path(runtime: str, scope: str, *parts: str) -> Path:
    path = paths.runtime_home(runtime, scope).joinpath(*parts)
    _ensure_runtime_destination(runtime, path, scope)
    if path.is_symlink():
        raise ActivationError(f"runtime config must not be a symlink: {path}")
    return path


def _ensure_owned_destination(runtime: str, dest: Path, scope: str) -> None:
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


def _disable_codex_plugin(scope: str = "global") -> Optional[dict]:
    config = _config_path("codex", scope, "config.toml")
    if not config.is_file():
        return None
    original = config.read_bytes()
    try:
        lines = original.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise ActivationError(f"Codex config is not UTF-8: {config}") from exc
    ranges = _codex_plugin_ranges(lines)
    if not ranges:
        return None
    enabled = re.compile(
        r"^(\s*enabled\s*=\s*)(true|false)(\s*(?:#.*)?(?:\r?\n)?)$",
        re.IGNORECASE,
    )
    changed = False
    offset = 0
    for raw_start, raw_end in ranges:
        start, end = raw_start + offset, raw_end + offset
        found = False
        for index in range(start + 1, end):
            match = enabled.match(lines[index])
            if not match:
                continue
            found = True
            if match.group(2).lower() != "false":
                lines[index] = f"{match.group(1)}false{match.group(3)}"
                changed = True
        if not found:
            lines.insert(end, "enabled = false\n")
            offset += 1
            changed = True
    if not changed:
        return None
    _atomic_bytes(config, "".join(lines).encode("utf-8"))
    return {
        "kind": "codex-plugin-disabled",
        "path": str(config),
        "disabled": ["hearting-codex"],
        "backup": _config_backup("codex", scope, config, original),
    }


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


def _disable_claude_plugin(scope: str = "global") -> Optional[dict]:
    registry = _claude_plugins_path(scope)
    if not registry.is_file():
        return None
    original = registry.read_bytes()
    try:
        data = json.loads(original.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationError(f"invalid Claude plugin registry: {registry}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("plugins", {}), dict):
        raise ActivationError(f"invalid Claude plugin registry object: {registry}")
    plugins = data.setdefault("plugins", {})
    removed = [
        key for key in plugins if key.split("@", 1)[0] == "hearting-claude"
    ]
    if not removed:
        return None
    for key in removed:
        del plugins[key]
    _atomic_json(registry, data)
    return {
        "kind": "claude-plugin-disabled",
        "path": str(registry),
        "disabled": removed,
        "backup": _config_backup("claude", scope, registry, original),
    }


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
    source_env = source.get("env")
    if not isinstance(source_env, dict):
        raise ActivationError("Claude settings source has no env object")
    managed_env = {}
    for key in CLAUDE_MANAGED_ENV_KEYS:
        value = source_env.get(key)
        if not isinstance(value, str):
            raise ActivationError(f"Claude settings source has no valid env.{key}")
        managed_env[key] = value
    return {"statusLine": statusline, "env": managed_env}


def _merge_claude_settings(
    active_root: Path, previous: Optional[dict], scope: str = "global"
) -> dict:
    source = _read_json_object(_claude_hook_source(active_root), "Claude hook source")
    source_hooks = source.get("hooks")
    if not isinstance(source_hooks, dict) or not source_hooks:
        raise ActivationError("Claude hook source has no hooks object")
    desired_values = _claude_managed_values(source)
    config = _config_path("claude", scope, "settings.json")
    original = config.read_bytes() if config.exists() else None
    data = _read_json_object(config, "Claude settings") if config.exists() else {}
    enabled_plugins = data.get("enabledPlugins")
    if enabled_plugins is not None and not isinstance(enabled_plugins, dict):
        raise ActivationError(f"Claude settings enabledPlugins is not an object: {config}")
    disabled_plugins = []
    if isinstance(enabled_plugins, dict):
        for key, value in list(enabled_plugins.items()):
            if key.split("@", 1)[0] == "hearting-claude" and value is not False:
                enabled_plugins[key] = False
                disabled_plugins.append(key)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ActivationError(f"Claude settings hooks is not an object: {config}")
    changed = bool(disabled_plugins)
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
            kept = [item for item in current if json.dumps(item, sort_keys=True) not in old]
            if len(kept) != len(current):
                hooks[event] = kept
                changed = True
    for event, entries in source_hooks.items():
        if not isinstance(entries, list):
            raise ActivationError(f"Claude hook source event is not a list: {event}")
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise ActivationError(f"Claude settings hook event is not a list: {event}")
        known = {json.dumps(item, sort_keys=True) for item in current}
        for item in entries:
            marker = json.dumps(item, sort_keys=True)
            if marker in known:
                continue
            current.append(item)
            known.add(marker)
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

    current_env = data.get("env", missing_value)
    if current_env is missing_value:
        current_env = {}
        data["env"] = current_env
        changed = True
    if not isinstance(current_env, dict):
        conflicts.append("env")
    else:
        previous_env = previous_values.get("env", {})
        if not isinstance(previous_env, dict):
            previous_env = {}
        for key, desired_value in desired_values["env"].items():
            current_value = current_env.get(key, missing_value)
            previous_value = previous_env.get(key, missing_value)
            if (
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

    if changed:
        _atomic_json(config, data)
    return {
        "kind": "claude-settings-merged",
        "path": str(config),
        "disabled": disabled_plugins,
        "added": added,
        "managed_hooks": source_hooks,
        "managed_values": managed_values,
        "conflicts": conflicts,
        "backup": (
            _config_backup("claude", scope, config, original)
            if changed and original is not None
            else None
        ),
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
            present = {json.dumps(item, sort_keys=True) for item in actual[event]}
            if any(json.dumps(item, sort_keys=True) not in present for item in entries):
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
            for hook in entry.get("hooks", []):
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


def _disable_opencode_npm(scope: str = "global") -> List[dict]:
    """Remove only explicit hearting npm entries from JSON config.

    JSONC is intentionally read-only because stdlib cannot preserve comments.
    The caller receives the original bytes for transaction rollback; removed
    entries are recorded under the harness-owned state directory.
    """
    changes = []
    for config in _opencode_config_paths(scope):
        if config.suffix != ".json" or not config.is_file():
            continue
        original = config.read_bytes()
        data = _read_opencode_config(config)
        removed = []
        changed = False
        for key in ("plugin", "plugins"):
            values = data.get(key)
            if not isinstance(values, list):
                continue
            keep = []
            for value in values:
                if _is_harness_npm_plugin_entry(value):
                    removed.append(_opencode_plugin_name(value))
                    changed = True
                else:
                    keep.append(value)
            data[key] = keep
        if not changed:
            continue
        _atomic_json(config, data)
        changes.append(
            {
                "kind": "opencode-plugin-disabled",
                "path": str(config),
                "disabled": removed,
                "backup": _config_backup("opencode", scope, config, original),
            }
        )
    return changes


def _runtime_config_paths(runtime: str, scope: str = "global") -> List[Path]:
    if runtime == "codex":
        return [_config_path(runtime, scope, "config.toml")]
    if runtime == "claude":
        return [
            _config_path(runtime, scope, "settings.json"),
            _claude_plugins_path(scope),
        ]
    return _opencode_config_paths(scope)


def _prepare_runtime_config(
    runtime: str, active_root: Path, previous: Optional[dict], scope: str = "global"
) -> List[dict]:
    if runtime == "codex":
        changes = [_disable_codex_plugin(scope)]
    elif runtime == "claude":
        changes = [
            _merge_claude_settings(active_root, previous, scope),
            _disable_claude_plugin(scope),
        ]
    else:
        changes = _disable_opencode_npm(scope)
    return [change for change in changes if change]


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


def _safe_existing_dest(
    dest: Path, kind: str, owned: set, allowed_link_roots: Sequence[Path]
) -> None:
    if not (dest.exists() or dest.is_symlink()):
        return
    if str(dest) in owned:
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
    if dest in {*_runtime_config_paths(runtime, scope), _state_path(runtime, scope)}:
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
    expected_plugin = {
        "codex": "hearting-codex",
        "claude": "hearting-claude",
    }.get(runtime)
    if expected_plugin:
        plugin_cache = home / "plugins" / "cache"
        try:
            plugin_relative = dest.relative_to(plugin_cache)
        except ValueError:
            return False
        return expected_plugin in plugin_relative.parts
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
) -> set:
    """Treat activation.json as untrusted input before using deletion paths."""
    # Profile activation adopts only links that resolve to a canonical harness
    # manifest. This lets a refresh remove stale native links left by the
    # legacy all-skills installer without touching user-owned entries.
    trusted = {
        str(path) for path in _discovered_harness_links(runtime, scope)
    }
    if not state:
        return trusted
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

    home = paths.runtime_home(runtime, scope)
    plugin_prefixes = {
        "codex": home / "plugins/cache/hearting/hearting-codex",
        "claude": home / "plugins/cache/hearting/hearting-claude",
    }
    for item in state.get("owned_paths", []):
        value = item.get("dest")
        if not value:
            continue
        dest = Path(value)
        _ensure_runtime_destination(runtime, dest, scope)
        if value in allowed:
            trusted.add(value)
            continue
        if item.get("kind") == "symlink" and dest.is_symlink():
            raw_target = Path(os.readlink(dest))
            target = raw_target if raw_target.is_absolute() else dest.parent / raw_target
            try:
                resolved_target = target.resolve(strict=False)
            except RuntimeError:
                resolved_target = None
            if resolved_target is not None and _journal_dest_allowed(runtime, dest, scope):
                for root in roots:
                    try:
                        resolved_target.relative_to(root.resolve(strict=False))
                        trusted.add(value)
                        break
                    except (ValueError, RuntimeError):
                        continue
            if value in trusted:
                continue
        prefix = plugin_prefixes.get(runtime)
        if item.get("kind") == "copytree" and prefix is not None:
            try:
                dest.relative_to(prefix)
            except ValueError:
                continue
            trusted.add(value)
    return trusted


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
    owned = _trusted_owned(runtime, previous, desired, scope)
    desired_by_dest = {entry["dest"]: entry for entry in desired}
    removal = [Path(item) for item in owned if item not in desired_by_dest]

    # Plugins are outside both activation modes.  Existing harness caches are
    # retained under .harness/disabled-plugins, while registry entries are
    # disabled by the protected config transaction.
    quarantine = _plugin_roots(runtime, scope)
    allowed_link_roots = list(source_roots)
    if previous:
        for key in ("source_root", "active_root"):
            if previous.get(key):
                allowed_link_roots.append(Path(previous[key]))

    changed: List[dict] = []
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

        for dest in removal + quarantine + [Path(item["dest"]) for item in desired]:
            key = str(dest)
            if key in seen:
                continue
            seen.add(key)
            _ensure_owned_destination(runtime, dest, scope)
            kind = desired_by_dest.get(key, {}).get("kind", "remove")
            quarantine_owned = owned | {str(path) for path in quarantine}
            _safe_existing_dest(dest, kind, quarantine_owned, allowed_link_roots)
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

        for dest in removal + quarantine:
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
            for record in snapshots:
                if record["dest"] == str(dest):
                    record["postimage"] = safe_fs.PathState("missing").public()
                    break
            _write_journal(journal_path, runtime, "applying", snapshots)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if item["kind"] == "copytree":
                _remove_path(dest)
                shutil.copytree(source, dest, symlinks=False)
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
            changed.append(dict(item))
            operation_count += 1
            if fail_after and operation_count >= fail_after:
                raise ActivationError(f"injected failure after operation {operation_count}")

        disabled = []
        disabled_root = state_dir / "disabled-plugins" / uuid.uuid4().hex
        for record in snapshots:
            if record["dest"] not in {str(path) for path in quarantine}:
                continue
            if record["state"] == "moved":
                disabled_root.mkdir(parents=True, exist_ok=True)
                target = disabled_root / Path(record["dest"]).name
                backup = Path(record["backup"])
                if backup.is_dir():
                    shutil.copytree(backup, target)
                else:
                    shutil.copy2(backup, target)
                disabled.append(str(target))
            elif record["state"] == "symlink":
                disabled.append(f"symlink:{record['target']}")
        if disabled:
            changed.append(
                {
                    "source": "runtime-plugin-cache",
                    "dest": str(disabled_root),
                    "surface": "disabled-plugin",
                    "kind": "quarantine",
                    "disabled": disabled,
                }
            )

        owned_entries = [item for item in changed if item.get("kind") != "quarantine"]
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
    return [item for item in changed if item.get("kind") != "quarantine"]


def _projection_digest(entries: List[dict]) -> str:
    return _digest_paths(Path(item["source"]) for item in entries)


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
    state = _load_json(_state_path(runtime, scope))
    roots: List[Path] = []
    if source:
        roots.append(_real_source(source))
    if state:
        for key in ("source_root", "active_root"):
            if state.get(key):
                roots.append(Path(state[key]))
    try:
        roots.append(paths.agent_home().resolve(strict=True))
    except (OSError, RuntimeError):
        pass

    discovery = set()
    for root in roots:
        try:
            discovery.update(Path(item["dest"]) for item in _linked_entries(runtime, root, scope))
        except ActivationError:
            continue

    full_copy_paths = [
        paths.harness_state_dir(runtime, scope),
        *_runtime_config_paths(runtime, scope),
        *_plugin_roots(runtime, scope),
    ]
    candidate_paths = list(full_copy_paths)
    candidate_paths.extend(sorted(discovery, key=lambda item: str(item)))
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
            preserve_names = (
                ("managed-sessions",)
                if dest == paths.harness_state_dir(runtime, scope)
                else ()
            )
            record = _copy_snapshot(
                dest, backup_root, len(records), preserve_names=preserve_names)
            record["_preimage"] = safe_fs.capture_state(
                dest, exclude_names=preserve_names
            )
            records.append(record)
        for dest in sorted(discovery, key=lambda item: str(item)):
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
    return {
        "runtime": runtime,
        "scope": scope,
        "root": str(snapshot_root),
        "records": records,
        "_locks": locks,
        "_sealed": False,
    }


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
    _validate_state_dir(runtime, scope)
    _recover_transactions(runtime, scope)
    if runtime == "opencode" and _opencode_jsonc_harness_present(scope):
        raise ActivationError(
            "OpenCode JSONC contains an enabled harness npm plugin; remove that exact "
            "plugin entry before native activation (comments are not rewritten automatically)"
        )
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

    def commit_state(owned):
        config_changes = _prepare_runtime_config(runtime, active_root, previous, scope)
        disabled = [
            item
            for change in config_changes
            for item in change.get("disabled", [])
        ]
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
            "disabled_external_entries": disabled,
            "config_backups": [
                change["backup"] for change in config_changes if change.get("backup")
            ],
            "managed_config": {
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
                "model_config": model_config_action,
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
        protected_paths=(*_runtime_config_paths(runtime, scope), previous_path),
        commit_callback=commit_state,
    )
    return status(runtime, scope)


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
        desired_destinations = {item["dest"] for item in entries}
        for item in state.get("owned_paths", []):
            dest = Path(item.get("dest", ""))
            if (
                item.get("kind") == "symlink"
                and str(dest) not in desired_destinations
                and dest.is_symlink()
                and _journal_dest_allowed(runtime, dest, scope)
            ):
                missing = True
    except ActivationError:
        entries, digest, missing, stale, unexpected = [], None, True, False, []

    config_conflicts: List[str] = []
    if runtime == "claude":
        config_missing, config_conflicts = _claude_settings_health(active_root, scope)
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

    duplicates = duplicate_sources(runtime, scope)
    duplicates.extend(
        f"unmanaged-extra:{path.relative_to(paths.runtime_home(runtime, scope))}"
        for path in unexpected
    )
    duplicates.extend(f"config-conflict:{item}" for item in config_conflicts)
    if duplicates:
        freshness = "duplicate"
        if config_conflicts:
            next_action = (
                f"resolve Claude settings conflicts ({','.join(config_conflicts)}), "
                "then harness runtime refresh --runtime claude"
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
    """Report only which surface owns the runtime's executable, never mutate it.

    Claude Code's native updater replaces its own binary in the background, so
    the deliberate absence of a Hearting `claude` wrapper must be reported as a
    distinct healthy contract (`hearting_ingress=none`) rather than an omission
    that looks like a missed integration. Codex's protected launcher status is
    owned and reported by `codex_launcher.status()`; this merely names that the
    installer should look there, so this runtime-neutral engine never inspects
    or writes shell profiles or executable bindings itself.
    """
    if runtime == "claude":
        return {"owner": "vendor", "hearting_ingress": "none"}
    if runtime == "codex":
        return {"owner": "hearting", "hearting_ingress": "see-managed-launcher"}
    return {"owner": "vendor", "hearting_ingress": "none"}


def refresh(runtime: str, scope: str = "global") -> dict:
    _validate_scope(runtime, scope)
    state = _load_json(_state_path(runtime, scope))
    if state is None:
        raise ActivationError(f"{runtime} has no activation state")
    return activate(runtime, state["mode"], state["source_root"], scope)


def _unmerge_claude_settings(
    state: dict, scope: str, dry_run: bool = False
) -> List[str]:
    """Remove only exact values the activation record still proves it owns."""
    managed_config = state.get("managed_config", {})
    if not isinstance(managed_config, dict):
        return []
    managed_hooks = managed_config.get("claude_hooks", {})
    managed_values = managed_config.get("claude_values", {})
    if not isinstance(managed_hooks, dict):
        managed_hooks = {}
    if not isinstance(managed_values, dict):
        managed_values = {}
    if not managed_hooks and not managed_values:
        return []
    config = _config_path("claude", scope, "settings.json")
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
            managed_set = {json.dumps(item, sort_keys=True) for item in entries}
            kept = [
                item
                for item in current
                if json.dumps(item, sort_keys=True) not in managed_set
            ]
            if len(kept) != len(current):
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
            if current_env.get(key) == value:
                current_env.pop(key)
                changed = True
        if not current_env:
            data.pop("env")
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
        return {"runtime": runtime, "status": "not-active", "removed": [], "restored_configs": []}
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
    trusted = _trusted_owned(runtime, state, desired, scope)
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
    restored = (
        _unmerge_claude_settings(state, scope, dry_run=dry_run)
        if runtime == "claude"
        else []
    )
    if not dry_run:
        bundles = paths.harness_state_dir(runtime, scope) / "bundles"
        if bundles.is_dir() and not bundles.is_symlink():
            _remove_path(bundles)
        _remove_path(state_file)
    return {
        "runtime": runtime,
        "status": "planned" if dry_run else "deactivated",
        "removed": removed,
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
