"""SD-123 A50-9: compare the Claude template's declared `defaultMode` against
the user's `~/.claude/settings.json`.

This is a read-only comparator, not an owned config surface: install never
seeds or edits `~/.claude/settings.json` (seed-once — SD-66/DP-23 precedent),
so there is no `ensure()` here, only `validate()`.
"""

from __future__ import annotations

import json
from pathlib import Path

import paths

TEMPLATE_RELPATH = "adapters/claude/settings.json"


def template_path() -> Path:
    return paths.resolve_source(TEMPLATE_RELPATH)


def user_path() -> Path:
    return paths.runtime_home("claude") / "settings.json"


def _default_mode(path: Path):
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return (data.get("permissions") or {}).get("defaultMode")


def validate() -> dict:
    user = user_path()
    template = template_path()
    if not user.is_file():
        return {
            "status": "absent", "ok": True, "path": str(user),
            "detail": "not seeded yet — `harness install` seeds it once",
        }
    try:
        user_mode = _default_mode(user)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "invalid", "ok": False, "path": str(user),
            "detail": f"unreadable or malformed: {exc}",
        }
    try:
        template_mode = _default_mode(template)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "invalid", "ok": False, "path": str(user),
            "detail": f"template unreadable or malformed: {exc}",
        }
    if user_mode == template_mode:
        return {
            "status": "valid", "ok": True, "path": str(user),
            "detail": f"defaultMode={user_mode!r}",
        }
    return {
        "status": "drift", "ok": True, "path": str(user),
        "detail": (
            f"user defaultMode={user_mode!r} differs from template "
            f"defaultMode={template_mode!r} ({template})"
        ),
    }
