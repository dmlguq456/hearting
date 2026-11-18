"""Upgrade migration for the user's `~/.claude/settings.json`.

The managed install merges only hooks, statusLine, env, autoMemoryEnabled and idleCompaction
into the user's file and never writes `permissions`, so Hearting does not read
or judge permission settings (`harness config status` has no row for them).
This module removes retired harness hook registrations, preserving the user's
own settings, and carries the one default Claude reads from its global config
file instead of settings.json (the empty-prompt left arrow).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Claude reads this key only from its global config file (`~/.claude.json`, or
# `$CLAUDE_CONFIG_DIR/.claude.json`); it ignores it in settings.json. `false`
# turns off the empty-prompt left arrow that backgrounds a foreground session
# (a fork under a new session id). `/bg`, `/background`, `claude agents` and
# the other ways to open the list stay available.
LEFT_ARROW_KEY = "leftArrowOpensAgents"


# Exact retired harness entrypoints. Keep this migration list after source removal
# so upgrades from an older copy-once settings file cannot execute missing hooks.
_RETIRED_HOOKS = (
    "material-route-guard.py", "artifact-guard.sh", "core-first-guard.sh",
    "spec-skill-gate.sh", "worktree-path-guard.sh", "runtime-root-guard.sh",
    "git-state-guard.sh", "core-read-marker.sh", "pretooluse-write-guard.py",
)

# These entrypoints still serve prompts. Retire only their former startup
# registrations, including the run-hook.sh alias and direct-file spellings.
_RETIRED_EVENT_HOOKS = {"SessionStart": (
    "local-evidence-inject", "local-evidence-inject.sh",
    "dispatch-session-sweep", "dispatch-session-sweep.py",
)}


# The SessionEnd `mem.py sync` registration older releases shipped (D-82: memory
# now exchanges after writes and reads, not at session end). Only the managed
# spelling matches: the worker-guard `sh -c` wrapper around the harness's own
# `$HOME/.claude/tools/memory/mem.py`. A user's own `mem sync` hook is left alone.
_RETIRED_SESSION_END_MEM_SYNC = re.compile(
    r"""^sh -c 'if \[ [^']*AGENT_SESSION_ROLE[^']* \]; then exit 0; fi; exec """
    r"""(?:env MEM_DUMP_PUSH=1 )?python3 "\$HOME/\.claude/tools/memory/mem\.py" """
    r"""sync(?: --json)?(?: >/dev/null)?'$"""
)


# The installed harness root reached by path instead of through a runtime-home
# hooks link: the `<runtime-home>/hearting` projection (and the `$HOME/hearting`
# linked checkout), the managed `current` pointer, or one release directory.
# Settings that called a guard this way still name an owned file after the guard
# is retired, so the registration must retire with it.
_OWNED_ROOT_HOOK_PATH = re.compile(
    r"""/hearting/(?:current/|releases/[^/\s"']+/)?hooks/"""
)


def retire_hook_registrations(path: Path, *, dry_run: bool = False) -> dict:
    """Remove only retired Hearting commands, retaining user settings and hooks."""
    import safe_fs

    result = {"action": "retire-hook-registrations", "dest": str(path), "status": "unchanged"}
    # Linked projections already track their source. Never rewrite through them.
    if path.is_symlink() or not path.exists():
        return result
    try:
        before = safe_fs.capture_state(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        changed = remove_retired_hooks(data)
        if not changed:
            return result
        result["status"] = "planned" if dry_run else "updated"
        if not dry_run:
            auth = safe_fs.authority(path, owner="hearting-retired-hook-registrations",
                                     allowed_paths=(path,), expected=before)
            safe_fs.atomic_write_bytes(auth, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(), path.stat().st_mode & 0o777)
        return result
    except (OSError, ValueError, TypeError, AttributeError, safe_fs.SafetyError) as exc:
        return {**result, "status": "blocked", "detail": str(exc)}


def remove_retired_hooks(data: dict) -> bool:
    """Filter known retired harness registrations in a settings object in place."""
    events = data.get("hooks", {})
    changed = False
    for event, groups in list(events.items()):
        remaining = []
        for group in groups:
            kept = []
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                # Match owned path spellings, not a user script with the same basename.
                owned = any(token in command for token in (
                    '/.claude/hooks/', '/adapters/codex/hooks/',
                    '${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/',
                    '$AGENT_HOME/hooks/', '${AGENT_HOME}/hooks/',
                    '$root/adapters/codex/hooks/run-hook.sh',
                )) or bool(_OWNED_ROOT_HOOK_PATH.search(command))
                retired = any(re.search(r'(?<![\w.-])' + re.escape(name) + r'(?![\w.-])', command)
                              for name in (*_RETIRED_HOOKS, *_RETIRED_EVENT_HOOKS.get(event, ())))
                retired = retired or ('worker-state-compact.py' in command and 'guard-write' in command)
                if event == "SessionEnd" and _RETIRED_SESSION_END_MEM_SYNC.match(command):
                    owned = retired = True
                if owned and retired:
                    changed = True
                else:
                    kept.append(hook)
            if kept:
                remaining.append({**group, "hooks": kept})
        if remaining:
            events[event] = remaining
        elif groups:
            del events[event]
    return changed


def apply_left_arrow_default(path: Path, *, owned: bool) -> tuple[bool, str | None]:
    """Write the left-arrow default only when the global config lacks the key.

    Returns ``(owned_now, notice)``. A key the user already set (either value)
    is never touched; ownership then carries only while the value is still the
    ``false`` Hearting wrote. A file Hearting cannot read or replace safely is
    left alone and reported, never an install failure.
    """
    import safe_fs

    try:
        if path.is_symlink():
            return owned, f"left-arrow default skipped: {path} is a symlink"
        before = safe_fs.capture_state(path)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            return owned, f"left-arrow default skipped: {path} is not a JSON object"
        if LEFT_ARROW_KEY in data:
            return owned and data[LEFT_ARROW_KEY] is False, None
        data[LEFT_ARROW_KEY] = False
        auth = safe_fs.authority(path, owner="hearting-claude-left-arrow-default",
                                 allowed_paths=(path,), allow_leaf_symlink=False, expected=before)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        safe_fs.atomic_write_bytes(
            auth, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(), mode)
        return True, ("claude: left arrow on an empty prompt no longer sends the conversation to the "
                      f"background ({LEFT_ARROW_KEY}=false in {path}; set it to true in /config to undo)")
    except (OSError, ValueError, safe_fs.SafetyError) as exc:
        return owned, f"left-arrow default skipped: {str(exc).splitlines()[0][:160]}"


def release_left_arrow_default(path: Path, *, dry_run: bool = False) -> bool:
    """Remove the key at uninstall only while it is still the value Hearting wrote."""
    import safe_fs

    try:
        if path.is_symlink() or not path.exists():
            return False
        before = safe_fs.capture_state(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get(LEFT_ARROW_KEY, None) is not False:
            return False
        if dry_run:
            return True
        del data[LEFT_ARROW_KEY]
        auth = safe_fs.authority(path, owner="hearting-claude-left-arrow-default",
                                 allowed_paths=(path,), allow_leaf_symlink=False, expected=before)
        safe_fs.atomic_write_bytes(
            auth, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(),
            path.stat().st_mode & 0o777)
        return True
    except (OSError, ValueError, safe_fs.SafetyError):
        return False
