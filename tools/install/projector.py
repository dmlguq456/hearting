"""Build symlink projection plans from ``INSTALL_LAYOUT.md``.

Portable capabilities, roles, and core remain the source of truth. This module
only computes links, copies, and delegated actions; runtime drivers apply them.
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import paths


OPENCODE_GUARD_LEAVES = ("plugins/hearting-guards.js", "plugins/hearting-guards/index.ts")
OPENCODE_GUARD_IMPORT = 'from "../hearting-guards.js"'


def opencode_generation():
    """Observe the existing bounded resolver without conflating unknown with V1."""
    utilities = str(Path(__file__).resolve().parents[2] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    import opencode_runtime
    version = opencode_runtime.runtime_version()
    return "unknown" if version is None else "v2" if version[0] >= 2 else "v1"


def opencode_activation(home):
    try:
        state = json.loads((home / ".harness/activation.json").read_text())
        return state if isinstance(state, dict) and state.get("runtime") == "opencode" else {}
    except (OSError, ValueError):
        return {}


def opencode_guard_leaf(dest, home):
    return dest in {home / leaf for leaf in OPENCODE_GUARD_LEAVES}


def render_opencode_plugin(entrypoint, module, marker=OPENCODE_GUARD_IMPORT):
    text = entrypoint.read_text(encoding="utf-8")
    if marker not in text:
        raise ValueError(f"{entrypoint} lacks {marker}")
    return text.replace(marker, f"from {json.dumps(str(module))}", 1)


def _guard_disabled(dest):
    # Observe only explicit siblings of this leaf (or its package directory).
    # lstat also sees a deliberately disabled dangling symlink.
    siblings = [dest]
    if dest.name == "index.ts":
        siblings.append(dest.parent)
    found = []
    for leaf in siblings:
        if not leaf.parent.is_dir():
            continue
        pattern = re.compile(re.escape(leaf.name) + r"\.(?:disabled(?:-.+)?|(?:v1|dup)-disabled-.+)$")
        for candidate in sorted(leaf.parent.iterdir()):
            if pattern.fullmatch(candidate.name):
                try:
                    candidate.lstat()
                except FileNotFoundError:
                    continue
                found.append(str(candidate))
    return found


def _guard_manifest(home, scope):
    import manifest
    try:
        receipt, _ = manifest.load_ownership_manifest(home / ".harness/manifest.json", "opencode", scope)
        return receipt or {}
    except (OSError, ValueError):
        return {}


def opencode_guard_layout(source_root, home, scope="global"):
    """One read-only carrier decision for activation, legacy projection and checks.

    ``preserve`` entries carry prior receipts unchanged through an unknown,
    ambiguous layout. They never authorize a write or adopt user bytes.
    """
    generation = opencode_generation()
    state = opencode_activation(home)
    leaves = [home / leaf for leaf in OPENCODE_GUARD_LEAVES]
    receipts = {item.get("dest"): item for item in state.get("owned_paths", [])
                if isinstance(item, dict) and item.get("dest") in {str(p) for p in leaves}}
    present = [p.exists() or p.is_symlink() for p in leaves]
    disabled = [_guard_disabled(p) if not exists else [] for p, exists in zip(leaves, present)]
    module = source_root / "adapters/opencode/plugins/hearting-guards.js"
    template = source_root / "adapters/opencode/plugins/hearting-guards/index.ts"
    legacy_digest = _guard_manifest(home, scope).get("files", {}).get(OPENCODE_GUARD_LEAVES[1])
    if legacy_digest and str(leaves[1]) not in receipts:
        receipts[str(leaves[1])] = {"dest": str(leaves[1]), "kind": "render", "source": str(template),
                                  "content_sha256": legacy_digest, "legacy": True}
    issues, entries = [], []
    reason = f"observed {generation}"
    selected = None
    preserve_binding = False
    if generation != "unknown":
        selected = 1 if generation == "v2" else 0
    elif sum(present) == 1:
        selected = present.index(True)
        preserve_binding = True
        reason = "generation unknown; preserve existing carrier and binding"
    elif not any(present) and len(receipts) == 1:
        selected = leaves.index(Path(next(iter(receipts))))
        prior = receipts[str(leaves[selected])]
        if prior.get("source"):
            if selected == 0:
                module = Path(prior["source"])
            else:
                template = Path(prior["source"])
                module = template.parent.parent / "hearting-guards.js"
        reason = "generation unknown; restore single prior carrier receipt"
        if prior.get("legacy") and not disabled[selected]:
            try:
                content = render_opencode_plugin(template, module)
                if hashlib.sha256(content.encode()).hexdigest() != prior["content_sha256"]:
                    raise ValueError("prior guard render binding is unresolved")
            except (OSError, ValueError) as exc:
                issues.append(f"generation unknown; {exc}")
                selected = None
    elif not any(present) and not receipts and not any(disabled):
        selected = 0
        reason = "generation unknown; legacy JS compatibility fallback"
    elif not any(present) and any(disabled):
        reason = "generation unknown; explicitly disabled carrier remains absent"
        if any(str(leaf) in receipts and not disabled[index] for index, leaf in enumerate(leaves)):
            issues.append("generation unknown; unresolved prior carrier remains missing")
    else:
        reason = "generation unknown; preserve unresolved existing layout"
        issues.append(reason)

    if selected is not None and disabled[selected]:
        reason += "; selected carrier explicitly disabled"
        selected = None
    if selected is not None:
        dest = leaves[selected]
        try:
            if preserve_binding:
                if selected == 0:
                    if not dest.is_symlink() or not dest.is_file():
                        raise ValueError(f"unresolved guard carrier: {dest}")
                    module = dest.resolve(strict=True)
                    if not (module == source_root / "adapters/opencode/plugins/hearting-guards.js"
                            or (module.parts[-4:] == ("adapters", "opencode", "plugins", "hearting-guards.js")
                                and native_harness_target("opencode", module))):
                        raise ValueError(f"foreign guard carrier: {dest}")
                    # Keep the link spelling as well as its resolved binding.
                    link_source = os.readlink(dest)
                else:
                    if dest.is_symlink() or not dest.is_file():
                        raise ValueError(f"unresolved guard package: {dest}")
                    match = re.search(r'\bfrom ("(?:[^"\\]|\\.)*")', dest.read_text())
                    if not match:
                        raise ValueError(f"guard package lacks direct import: {dest}")
                    module = Path(json.loads(match.group(1)))
                    if not module.is_absolute() or not module.is_file():
                        raise ValueError(f"guard module missing: {module}")
            entry = {"action": "symlink" if selected == 0 else "render",
                     "source": str(module if selected == 0 else template),
                     "dest": str(dest), "guard": True, "detail": reason,
                     "dependencies": [str(module)], "source_present": True}
            if selected == 0 and preserve_binding:
                entry["source"] = link_source
                entry["preserve"] = True
            if selected == 1:
                content = render_opencode_plugin(template, module)
                if preserve_binding and dest.read_text() != content:
                    raise ValueError(f"guard package import/content mismatch: {dest}")
                entry.update(content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest())
                if preserve_binding:
                    entry["preserve"] = True
            if not module.is_file():
                issues.append(f"guard module missing: {module}")
            entries.append(entry)
        except (OSError, ValueError, RuntimeError) as exc:
            issues.append(str(exc))
            if generation != "unknown":
                # Keep the required destination visible; never downgrade it to skip.
                entries.append({"action": "render" if selected else "symlink",
                                "source": str(template if selected else module),
                                "dest": str(dest), "guard": True, "source_present": False})
            else:
                selected = None
    if generation == "unknown" and issues:
        entries = [dict(item, action=item["kind"], guard=True, preserve=True,
                        detail=reason, source_present=True) for item in receipts.values()]
    return {"generation": generation, "detail": reason, "entries": entries,
            "disabled": [p for group in disabled for p in group], "issues": issues}


def opencode_guard_health(layout):
    issues = list(layout["issues"])
    for entry in layout["entries"]:
        if entry.get("preserve"):
            continue
        dest = Path(entry["dest"])
        for dependency in entry.get("dependencies", []):
            if not Path(dependency).is_file():
                issues.append(f"guard module missing: {dependency}")
        try:
            if entry["action"] == "render":
                if dest.is_symlink() or not dest.is_file() or dest.read_text() != entry.get("content"):
                    issues.append(f"guard package missing or import/content mismatch: {dest}")
            elif not dest.is_symlink() or dest.resolve() != Path(entry["source"]).resolve():
                issues.append(f"guard link missing or binding mismatch: {dest}")
        except (OSError, UnicodeError, RuntimeError):
            issues.append(f"guard carrier unreadable: {dest}")
    return issues


def opencode_guard_owned(entry, home, scope="global"):
    """Exact existing receipt, never a same-root path heuristic."""
    dest = Path(entry["dest"])
    for prior in opencode_activation(home).get("owned_paths", []):
        if prior.get("dest") != str(dest):
            continue
        if prior.get("kind") == "symlink" and dest.is_symlink():
            return os.readlink(dest) == prior.get("source")
        if prior.get("kind") == "render" and dest.is_file() and not dest.is_symlink():
            return hashlib.sha256(dest.read_bytes()).hexdigest() == prior.get("content_sha256")
        return False
    try:
        digest = _guard_manifest(home, scope).get("files", {}).get(dest.relative_to(home).as_posix())
        return bool(digest and not dest.is_symlink() and dest.is_file()
                    and hashlib.sha256(dest.read_bytes()).hexdigest() == digest)
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Claude Code projection (INSTALL_LAYOUT.md "Claude Code Projection")
# ---------------------------------------------------------------------------

_CLAUDE_SYMLINK_NAMES = [
    "CLAUDE.md", "README.md", "core", "commands", "skills", "agents",
    "agent-modes", "utilities", "tools", "scaffolds", "loops",
    "manifest.json", "statusline.sh",
]

_CLAUDE_COPY_ONCE_NAMES = ["settings.json", "keybindings.json"]


def claude_installer_owned_names():
    """Public owner-name-set for `harness install claude` (INSTALL_LAYOUT.md
    "owner-name-set reconciliation"). Runtime activation unions this with its
    own native discovery names so the two legitimate owners of
    ``$HOME/.claude`` symlinks converge on one set instead of drifting apart.
    """
    return set(_CLAUDE_SYMLINK_NAMES)


def native_harness_target(runtime: str, candidate: Path) -> bool:
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


def native_leaf_preservation(runtime: str, source: Path, dest: Path) -> str | None:
    """Return why a per-leaf native projection must leave ``dest`` alone.

    Only absent destinations, links already pointing at ``source``, links into
    the harness's own generated tree, and dangling links may be (re)written.
    A real file/directory or a resolving link elsewhere belongs to the user.
    """
    if not dest.is_symlink():
        if dest.exists():
            return f"user-owned {'directory' if dest.is_dir() else 'file'} preserved: {dest}"
        return None
    try:
        if os.readlink(dest) == str(source):
            return None
    except OSError:
        return None
    if not dest.exists() or native_harness_target(runtime, dest):
        return None
    return f"user-owned symlink to {dest.resolve()} preserved: {dest}"


_CLAUDE_TABLE = (
    [
        {"action": "symlink", "source": f"claude_setting/{name}", "dest_name": name}
        for name in _CLAUDE_SYMLINK_NAMES
    ]
    + [
        {"action": "copy_once", "source": f"claude_setting/{name}", "dest_name": name}
        for name in _CLAUDE_COPY_ONCE_NAMES
    ]
    + [{
        "action": "seed_once",
        "source": "adapters/claude/config/models.conf",
        "dest_name": "agent-config/models.conf",
    }]
    + [{
        "action": "symlink_glob",
        "source_dir": "adapters/claude/hooks",
        "dest_subdir": "hooks",
        "pattern": "*",
        "exclude": ["herdr-agent-state.sh"],
    }, {
        "action": "copy_managed",
        "source": "hooks/herdr-agent-state.sh",
        "dest_name": "hooks/herdr-agent-state.sh",
        "integration_id": "claude",
    }]
    + [
        # Delegate the Windows-only path without reimplementing install-windows.sh.
        {"action": "delegate", "cmd": ["bash", "adapters/claude/bin/install-windows.sh"]},
    ]
)


# ---------------------------------------------------------------------------
# Codex projection (INSTALL_LAYOUT.md "Codex Projection")
# ---------------------------------------------------------------------------

# (destination under runtime_home, source relpath); an empty source points to AGENT_HOME.
_CODEX_FIXED_SYMLINKS = [
    ("hearting", ""),
    ("AGENTS.md", "codex_setting/AGENTS.md"),
    ("hearting-readme.md", "codex_setting/README.md"),
    ("agent-core", "codex_setting/core"),
    ("agent-capabilities", "codex_setting/capabilities"),
    ("agent-roles", "codex_setting/roles"),
    ("agent-bin", "codex_setting/bin"),
    ("agent-tools", "codex_setting/tools"),
    ("agent-utilities", "codex_setting/utilities"),
    ("agent-scaffolds", "codex_setting/scaffolds"),
    ("agent-skills", "codex_setting/codex-skills"),
    ("agent-modes", "codex_setting/codex-modes"),
    ("agent-agents", "codex_setting/codex-agents"),
    ("agent-hooks", "codex_setting/codex-hooks"),
    ("hooks.json", "codex_setting/codex-hooks/hooks.json"),
]

_CODEX_TABLE = (
    [
        {"action": "symlink", "source": source, "dest_name": dest_name}
        for dest_name, source in _CODEX_FIXED_SYMLINKS
    ]
    + [{
        "action": "seed_once",
        "source": "adapters/codex/config/models.conf",
        "dest_name": "agent-config/models.conf",
    }]
    + [{
        "action": "copy_managed",
        "source": "adapters/codex/hooks/herdr-agent-state.sh",
        "dest_name": "herdr-agent-state.sh",
        "integration_id": "codex",
    }]
    + [
        {
            "action": "symlink_glob",
            "source_dir": "codex_setting/codex-skills",
            "dest_subdir": "skills",
            "pattern": "*",
            "native_leaf": True,
        },
        {
            "action": "symlink_glob",
            "source_dir": "codex_setting/codex-agents",
            "dest_subdir": "agents",
            "pattern": "*.toml",
            "native_leaf": True,
            # Project scope redirects agents into the project's local .codex/agents/.
            "project_scope_override": True,
        },
    ]
)


# ---------------------------------------------------------------------------
# OpenCode projection (INSTALL_LAYOUT.md "OpenCode Projection")
# Keep the locally validated singular OpenCode wiring; INST-OPEN-4 remains open.
# ---------------------------------------------------------------------------

_OPENCODE_FIXED_SYMLINKS = [
    ("hearting", ""),
    # Deliberately not projected as `AGENTS.md`: that exact name is what OpenCode
    # auto-loads from the global config home, and `runtime activate` owns it as
    # an immutable snapshot link. Claiming it here would overwrite that with a
    # mutable checkout path (INST-D-34/35).
    ("agent-agents.md", "opencode_setting/AGENTS.md"),
    ("hearting-readme.md", "opencode_setting/README.md"),
    ("agent-core", "opencode_setting/core"),
    ("agent-capabilities", "opencode_setting/capabilities"),
    ("agent-roles", "opencode_setting/roles"),
    ("agent-bin", "opencode_setting/bin"),
    ("agent-tools", "opencode_setting/tools"),
    ("agent-utilities", "opencode_setting/utilities"),
    ("agent-skills", "opencode_setting/opencode-skills"),
    ("agent-agents", "opencode_setting/opencode-agents"),
    ("agent-commands", "opencode_setting/opencode-commands"),
    ("tui/hearting-tui-identity.ts", "adapters/opencode/tui/hearting-tui-identity.ts"),
    ("tui/hearting-owned-tui.json", "adapters/opencode/tui/owned-tui.json"),
    ("tui/hearting-owned-cli.json", "adapters/opencode/tui/owned-cli.json"),
]

_OPENCODE_TABLE = (
    [
        {"action": "symlink", "source": source, "dest_name": dest_name}
        for dest_name, source in _OPENCODE_FIXED_SYMLINKS
    ]
    + [{
        "action": "seed_once",
        "source": "adapters/opencode/config/models.conf",
        "dest_name": "agent-config/models.conf",
    }]
    + [
        {
            "action": "symlink_glob",
            "source_dir": "opencode_setting/opencode-agents",
            "dest_subdir": "agent",
            "pattern": "*/*.md",
            "native_leaf": True,
        },
        {
            "action": "symlink_glob",
            "source_dir": "opencode_setting/opencode-commands",
            "dest_subdir": "command",
            "pattern": "*.md",
            "native_leaf": True,
        },
        {
            "action": "merge",
            "note": (
                "non-destructive opencode.json skills.paths merge, plus the "
                "instructions[] bootstrap entry only when the config-home "
                "AGENTS.md auto-load is absent; "
                "drivers/opencode.py performs the merge and projector only emits intent."
            ),
        },
    ]
    + [
        {"action": "copy_managed", "source": "adapters/opencode/plugins/herdr-agent-state.js",
         "dest_name": "plugins/herdr-agent-state.js", "integration_id": "opencode"},
        {"action": "copy_managed", "source": "adapters/opencode/tui/herdr-tui-session.js",
         "dest_name": "herdr-tui-session.js", "integration_id": "opencode-tui"},
        {"action": "copy_managed", "source": "adapters/opencode/tui/herdr-opencode/tui.js",
         "dest_name": "herdr-opencode/tui.js", "integration_id": "opencode-tui-v2"},
    ]
)


_TABLES = {
    "claude": _CLAUDE_TABLE,
    "codex": _CODEX_TABLE,
    "opencode": _OPENCODE_TABLE,
}


def _resolve_source_path(relpath):
    """Resolve relpath; an empty value denotes agent_home itself."""
    if relpath == "":
        return paths.agent_home()
    return paths.resolve_source(relpath)


def _dest_dir_for(runtime, scope, dest_subdir):
    """Compute a symlink-glob destination, including project-scoped Codex agents."""
    if runtime == "codex" and dest_subdir == "agents" and scope == "project":
        # Only the Codex agent glob moves into project-local .codex/agents/.
        return Path.cwd() / ".codex" / "agents"
    return paths.runtime_home(runtime, scope) / dest_subdir


def _expand(table, runtime, scope):
    """Expand abstract table rows into concrete plan entries."""
    runtime_home = paths.runtime_home(runtime, scope)
    entries = []

    for item in table:
        action = item["action"]

        if action in ("symlink", "copy_once", "seed_once", "copy_managed"):
            source_relpath = item["source"]
            source_path = _resolve_source_path(source_relpath)
            dest_path = runtime_home / item["dest_name"]
            source_present = source_path.exists()
            if source_present:
                entries.append(
                    {
                        "action": action,
                        "source": str(source_path),
                        "dest": str(dest_path),
                        "source_present": True,
                        **({"integration_id": item["integration_id"]}
                           if action == "copy_managed" else {}),
                    }
                )
            else:
                entries.append(
                    {
                        "action": "skip",
                        "reason": f"source absent: {source_path}",
                        "dest": str(dest_path),
                    }
                )

        elif action == "delegate":
            entries.append({"action": "delegate", "cmd": item["cmd"], "source_present": True})

        elif action == "merge":
            entries.append({"action": "merge", "note": item["note"], "source_present": True})

        elif action == "symlink_glob":
            source_dir_relpath = item["source_dir"]
            source_dir = paths.resolve_source(source_dir_relpath)
            dest_dir = _dest_dir_for(runtime, scope, item["dest_subdir"])
            if not source_dir.exists():
                # A missing source directory produces no entries and is not an error.
                continue
            for match in sorted(source_dir.glob(item["pattern"])):
                if match.name in item.get("exclude", ()):
                    continue
                dest_path = dest_dir / match.name
                source_present = match.exists()
                if source_present:
                    entries.append(
                        {
                            "action": "symlink",
                            "source": str(match),
                            "dest": str(dest_path),
                            "source_present": True,
                            **({"native_leaf": True} if item.get("native_leaf") else {}),
                        }
                    )
                else:
                    entries.append(
                        {
                            "action": "skip",
                            "reason": f"source absent: {match}",
                            "dest": str(dest_path),
                        }
                    )

        else:
            raise ValueError(f"unknown projection action: {action!r}")

    return entries


def plan(runtimes, scope="global"):
    """Return resolved projection plans by runtime.

    Return shape: ``{runtime: [entry, ...]}``.
    {"action": "symlink"|"copy_once"|"seed_once", "source": str, "dest": str, "source_present": True}
    {"action": "delegate", "cmd": [...], "source_present": True}
    {"action": "merge", "note": str, "source_present": True}
    {"action": "skip", "reason": str, "dest": str}
    """
    plans = {rt: _expand(_TABLES.get(rt, []), rt, scope) for rt in runtimes}
    if "opencode" in plans:
        home = paths.runtime_home("opencode", scope)
        state = opencode_activation(home)
        root = Path(state.get("active_root") or paths.agent_home())
        layout = opencode_guard_layout(root, home, scope)
        plans["opencode"].extend(layout["entries"])
    return plans
