#!/usr/bin/env python3
"""Generate the Claude-native plugin projection for the portable harness.

Claude is the native runtime — skills/agents live at the repo-root SoT
directly (`adapters/claude/skills/`, `adapters/claude/agents/`), unlike
Codex which needs a sync-native-skills generator first. This is the
*first* Claude sync-native generator: it exists only to physically
materialize the plugin channel (Claude Code plugin installs are
self-contained — a plugin cannot reference `../` outside its own root,
so all content must be copied in, not symlinked).

Mirrors `adapters/codex/bin/sync-native-plugin.py` (const block /
`plugin_json`+`marketplace_json` literals / `write_json` / `sync()` /
`check()`+`check_file()` / `main()`), extended: Codex's generator carries
skills only; this one carries skills + agents + hooks(5: 2 self-contained +
3 spec-pipeline DATA-rebased) + hooks.json + the utility bundle required by
those hooks (agent-home.sh, artifact-root.sh, artifact-snapshot.py)
(INST-OPEN-1, `_internal/hooks_inventory.md` adopt set — cycle 3 adopts the
spec-pipeline trio via hooks.json AGENT_HOME env-prefix rebasing).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

# S-5d (owner-supervisor-liveness, core/OPERATIONS.md §5.10): enforce the
# generated-plugin-JSON mode invariant on every write rather than trusting
# the platform default, since it is umask-dependent and has been observed
# drifting off 0644 across regenerate cycles.
JSON_MODE = 0o644


ROOT = Path(__file__).resolve().parents[3]
ADAPTER = ROOT / "adapters" / "claude"
PLUGIN_NAME = "hearting-claude"
MARKETPLACE_ROOT = ADAPTER / "plugin-marketplace"
MARKETPLACE = MARKETPLACE_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_ROOT = MARKETPLACE_ROOT / "plugins" / PLUGIN_NAME
SKILLS = ADAPTER / "skills"
AGENTS = ADAPTER / "agents"
HOOKS_SOURCE = ROOT / "hooks"
UTILITIES_SOURCE = ROOT / "utilities"
UTIL_BUNDLE = ["agent-home.sh", "artifact-root.sh", "artifact-snapshot.py"]

# INST-OPEN-1 adopt set (_internal/hooks_inventory.md): self-contained +
# fail-open guards, plus (cycle 3, 2026-07-13) the spec-pipeline trio —
# adopted (cycle 3, ${CLAUDE_PLUGIN_DATA} rebasing via hooks.json
# AGENT_HOME env-prefix; see plan 2026-07-13_harness-installer-hooks).
# memory/statusline/dispatch families remain excluded (CLI-install-owned
# state, not self-contained under a plugin cache).
HOOK_ADOPT = [
    "git-state-guard.sh",
    "artifact-guard.sh",
    "spec-skill-gate.sh",
    "spec-read-marker.sh",
    "spec-sync-nudge.sh",
]

# Same-directory helper modules a HOOK_ADOPT script imports/execs at runtime
# (`$SCRIPT_DIR/<helper>`). Copied alongside their owning hook so the plugin
# cache is self-contained, but never registered as their own hooks.json event
# — they have no independent PreToolUse/PostToolUse binding of their own.
HOOK_HELPER_FILES = ["artifact_write_targets.py"]

# event/matcher/shell taken verbatim from adapters/claude/settings.json registration.
_HOOK_EVENTS = {
    "git-state-guard.sh": "PreToolUse",
    "artifact-guard.sh": "PreToolUse",
    "spec-skill-gate.sh": "PreToolUse",
    "spec-read-marker.sh": "PostToolUse",
    "spec-sync-nudge.sh": "PostToolUse",
}
_HOOK_MATCHERS = {
    "git-state-guard.sh": "Edit|Write|MultiEdit|NotebookEdit",
    "artifact-guard.sh": "Edit|Write|MultiEdit|Bash",
    "spec-skill-gate.sh": "Skill",
    "spec-read-marker.sh": "Read",
    "spec-sync-nudge.sh": "Edit|Write|MultiEdit",
}
_HOOK_SHELLS = {
    "git-state-guard.sh": "sh",
    "artifact-guard.sh": "bash",
    "spec-skill-gate.sh": "sh",
    "spec-read-marker.sh": "sh",
    "spec-sync-nudge.sh": "bash",
}
# spec-pipeline trio: rebase state (`.spec-grounding` markers) onto the
# plugin's persistent, update-surviving data dir instead of the default
# agent-home.sh fallback — canonical hook bodies stay unmodified (they
# already honor `AGENT_HOME` as a top-priority env override).
_HOOK_DATA_HOME = {
    "spec-skill-gate.sh",
    "spec-read-marker.sh",
    "spec-sync-nudge.sh",
}


def plugin_json() -> dict:
    return {
        "name": PLUGIN_NAME,
        "description": (
            "Portable agent harness (skills/agents/hooks/.mcp.json/bin) projected for "
            "Claude Code — see PRD .agent_reports/spec/harness-installer/prd.md."
        ),
        "author": {
            "name": "hearting",
        },
    }


def marketplace_json() -> dict:
    return {
        "name": "hearting",
        "owner": {
            "name": "hearting",
        },
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": f"./plugins/{PLUGIN_NAME}",
                "description": (
                    "Portable agent harness — skills/agents/hooks projection for Claude Code."
                ),
            }
        ],
    }


def hooks_json() -> dict:
    """hooks/hooks.json — wrapped under a top-level "hooks" key (plugin schema,
    verified against current Claude Code docs: code.claude.com/docs/en/
    plugins-reference — differs from the flat settings.json shape). Each
    adopted hook registers on its settings.json event (PreToolUse/PostToolUse)
    with its settings.json matcher, path rebased to ${CLAUDE_PLUGIN_ROOT}.
    Hooks in `_HOOK_DATA_HOME` additionally get an `AGENT_HOME=
    "${CLAUDE_PLUGIN_DATA}"` env-prefix on the command — this rebases their
    `.spec-grounding` state onto the plugin's persistent data dir without
    modifying the canonical hook bodies (cycle 3, 2026-07-13; see plan
    2026-07-13_harness-installer-hooks). Event/hook ordering follows
    `HOOK_ADOPT` for determinism (dict insertion order = output order).
    """
    events: dict[str, list] = {}
    for name in HOOK_ADOPT:
        prefix = 'AGENT_HOME="${CLAUDE_PLUGIN_DATA}" ' if name in _HOOK_DATA_HOME else ""
        command = f'{prefix}{_HOOK_SHELLS[name]} "${{CLAUDE_PLUGIN_ROOT}}/hooks/{name}"'
        events.setdefault(_HOOK_EVENTS[name], []).append(
            {
                "matcher": _HOOK_MATCHERS[name],
                "hooks": [{"type": "command", "command": command}],
            }
        )
    return {"hooks": events}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(JSON_MODE)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def sync() -> None:
    if not SKILLS.exists():
        raise SystemExit(f"Claude native skills are missing: {SKILLS}")
    if not AGENTS.exists():
        raise SystemExit(f"Claude native agents are missing: {AGENTS}")
    for name in UTIL_BUNDLE:
        if not (UTILITIES_SOURCE / name).exists():
            raise SystemExit(f"Claude native utility is missing: {UTILITIES_SOURCE / name}")

    write_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json", plugin_json())
    write_json(MARKETPLACE, marketplace_json())

    plugin_skills = PLUGIN_ROOT / "skills"
    if plugin_skills.exists() or plugin_skills.is_symlink():
        shutil.rmtree(plugin_skills)
    shutil.copytree(SKILLS, plugin_skills)

    plugin_agents = PLUGIN_ROOT / "agents"
    if plugin_agents.exists() or plugin_agents.is_symlink():
        shutil.rmtree(plugin_agents)
    shutil.copytree(AGENTS, plugin_agents)

    plugin_hooks = PLUGIN_ROOT / "hooks"
    if plugin_hooks.exists() or plugin_hooks.is_symlink():
        shutil.rmtree(plugin_hooks)
    plugin_hooks.mkdir(parents=True)
    for name in HOOK_ADOPT:
        shutil.copy2(HOOKS_SOURCE / name, plugin_hooks / name)
    for name in HOOK_HELPER_FILES:
        shutil.copy2(HOOKS_SOURCE / name, plugin_hooks / name)
    write_json(plugin_hooks / "hooks.json", hooks_json())

    plugin_utils = PLUGIN_ROOT / "utilities"
    if plugin_utils.exists() or plugin_utils.is_symlink():
        shutil.rmtree(plugin_utils)
    plugin_utils.mkdir(parents=True)
    for name in UTIL_BUNDLE:
        shutil.copy2(UTILITIES_SOURCE / name, plugin_utils / name)


def check_file(path: Path, expected: str, stale: list[str]) -> None:
    if not path.exists() or path.read_text(encoding="utf-8") != expected:
        stale.append(str(path.relative_to(ROOT)))
        return
    if stat.S_IMODE(path.stat().st_mode) != JSON_MODE:
        stale.append(str(path.relative_to(ROOT)) + " (mode)")


def check() -> int:
    stale: list[str] = []

    check_file(
        PLUGIN_ROOT / ".claude-plugin" / "plugin.json",
        json.dumps(plugin_json(), indent=2, ensure_ascii=False) + "\n",
        stale,
    )
    check_file(
        MARKETPLACE,
        json.dumps(marketplace_json(), indent=2, ensure_ascii=False) + "\n",
        stale,
    )
    check_file(
        PLUGIN_ROOT / "hooks" / "hooks.json",
        json.dumps(hooks_json(), indent=2, ensure_ascii=False) + "\n",
        stale,
    )

    # skills: per-skill SKILL.md byte-compare + excess-file detection.
    expected_skills = {
        PLUGIN_ROOT / "skills" / skill.relative_to(SKILLS) for skill in SKILLS.glob("*/SKILL.md")
    }
    for skill in sorted(SKILLS.glob("*/SKILL.md")):
        plugin_skill = PLUGIN_ROOT / "skills" / skill.relative_to(SKILLS)
        if not plugin_skill.exists() or plugin_skill.read_bytes() != skill.read_bytes():
            stale.append(str(plugin_skill.relative_to(ROOT)))
    plugin_skills_dir = PLUGIN_ROOT / "skills"
    if plugin_skills_dir.exists():
        for path in sorted(plugin_skills_dir.glob("*/SKILL.md")):
            if path not in expected_skills:
                stale.append(str(path.relative_to(ROOT)))

    # agents: single-level *.md files, byte-compare + excess-file detection.
    expected_agents = {PLUGIN_ROOT / "agents" / p.name for p in AGENTS.glob("*.md")}
    for agent_file in sorted(AGENTS.glob("*.md")):
        plugin_agent = PLUGIN_ROOT / "agents" / agent_file.name
        if not plugin_agent.exists() or plugin_agent.read_bytes() != agent_file.read_bytes():
            stale.append(str(plugin_agent.relative_to(ROOT)))
    plugin_agents_dir = PLUGIN_ROOT / "agents"
    if plugin_agents_dir.exists():
        for path in sorted(plugin_agents_dir.glob("*.md")):
            if path not in expected_agents:
                stale.append(str(path.relative_to(ROOT)))

    # hooks: adopted set + unregistered same-directory helper files (hooks.json
    # checked separately above).
    expected_hooks = {PLUGIN_ROOT / "hooks" / name for name in HOOK_ADOPT + HOOK_HELPER_FILES}
    for name in HOOK_ADOPT + HOOK_HELPER_FILES:
        src_hook = HOOKS_SOURCE / name
        plugin_hook = PLUGIN_ROOT / "hooks" / name
        if not plugin_hook.exists() or plugin_hook.read_bytes() != src_hook.read_bytes():
            stale.append(str(plugin_hook.relative_to(ROOT)))
    plugin_hooks_dir = PLUGIN_ROOT / "hooks"
    if plugin_hooks_dir.exists():
        for path in sorted(plugin_hooks_dir.glob("*")):
            if path.name == "hooks.json":
                continue
            if path not in expected_hooks:
                stale.append(str(path.relative_to(ROOT)))

    # utilities: bundled shared scripts, byte-compare + excess-file detection.
    expected_utils = {PLUGIN_ROOT / "utilities" / name for name in UTIL_BUNDLE}
    for name in UTIL_BUNDLE:
        src_util = UTILITIES_SOURCE / name
        plugin_util = PLUGIN_ROOT / "utilities" / name
        if not plugin_util.exists() or plugin_util.read_bytes() != src_util.read_bytes():
            stale.append(str(plugin_util.relative_to(ROOT)))
    plugin_utils_dir = PLUGIN_ROOT / "utilities"
    if plugin_utils_dir.exists():
        for path in sorted(plugin_utils_dir.glob("*")):
            if path not in expected_utils:
                stale.append(str(path.relative_to(ROOT)))

    if stale:
        print("Claude native plugin projection is stale:", file=sys.stderr)
        for item in stale:
            print(f"  {item}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated projection")
    args = parser.parse_args()

    if args.check:
        return check()
    sync()
    print(f"generated Claude native plugin projection at {PLUGIN_ROOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
