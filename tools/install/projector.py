"""Build symlink projection plans from ``INSTALL_LAYOUT.md``.

Portable capabilities, roles, and core remain the source of truth. This module
only computes links, copies, and delegated actions; runtime drivers apply them.
"""

from pathlib import Path

import paths


# ---------------------------------------------------------------------------
# Claude Code projection (INSTALL_LAYOUT.md "Claude Code Projection")
# ---------------------------------------------------------------------------

_CLAUDE_SYMLINK_NAMES = [
    "CLAUDE.md", "README.md", "core", "commands", "skills", "agents",
    "agent-modes", "hooks", "utilities", "tools", "scaffolds", "loops",
    "manifest.json", "statusline.sh",
]

_CLAUDE_COPY_ONCE_NAMES = ["settings.json", "keybindings.json"]

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
    + [
        {
            "action": "symlink_glob",
            "source_dir": "codex_setting/codex-skills",
            "dest_subdir": "skills",
            "pattern": "*",
        },
        {
            "action": "symlink_glob",
            "source_dir": "codex_setting/codex-agents",
            "dest_subdir": "agents",
            "pattern": "*.toml",
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
    ("plugins/hearting-guards.js", "opencode_setting/opencode-plugins/hearting-guards.js"),
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
        },
        {
            "action": "symlink_glob",
            "source_dir": "opencode_setting/opencode-commands",
            "dest_subdir": "command",
            "pattern": "*.md",
        },
        {
            "action": "merge",
            "note": (
                "non-destructive opencode.json instructions[]/skills.paths merge; "
                "drivers/opencode.py performs the merge and projector only emits intent."
            ),
        },
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

        if action in ("symlink", "copy_once", "seed_once"):
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
                dest_path = dest_dir / match.name
                source_present = match.exists()
                if source_present:
                    entries.append(
                        {
                            "action": "symlink",
                            "source": str(match),
                            "dest": str(dest_path),
                            "source_present": True,
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
    return {rt: _expand(_TABLES.get(rt, []), rt, scope) for rt in runtimes}
