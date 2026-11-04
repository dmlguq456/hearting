"""Upgrade migration for the user's `~/.claude/settings.json`.

The managed install merges only hooks, statusLine, env, autoMemoryEnabled and idleCompaction
into the user's file and never writes `permissions`, so Hearting does not read
or judge permission settings (`harness config status` has no row for them).
This module only removes retired harness hook registrations, preserving the
user's own settings.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


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
