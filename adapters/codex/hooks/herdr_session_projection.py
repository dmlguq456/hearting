"""Fail-soft projection of an existing Codex session into an existing Herdr pane."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools"
if str(TOOLS) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(TOOLS))
from fleet.session_handle import clip_cells, sanitize_title, session_handle  # noqa: E402


def _title(session_id: str) -> str:
    try:
        from fleet.titles import read
        value = read(session_id, harness="codex") or {}
        return sanitize_title(value.get("title"))
    except Exception:
        return ""


def _metadata_formatter() -> Path:
    override = os.environ.get("HERDR_SESSION_METADATA_FORMATTER")
    if override:
        return Path(override).expanduser()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config / "hearting" / "herdr-session-metadata"


def _display_metadata(session_id: str, title: str, fallback: str) -> tuple[str, str]:
    """Resolve an optional user formatter without making it runtime authority."""
    formatter = _metadata_formatter()
    if not formatter.is_file() or not os.access(formatter, os.X_OK):
        return fallback, title
    try:
        result = subprocess.run(
            [str(formatter), "--harness", "codex", "--session-id", session_id,
             "--summary", title],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=0.2, check=False,
        )
        if result.returncode or len(result.stdout.encode("utf-8")) > 4096:
            return fallback, title
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            return fallback, title
        display_agent = sanitize_title(value.get("display_agent"))
        custom_title = sanitize_title(value.get("title"))
        if not display_agent:
            return fallback, title
        return clip_cells(display_agent, 24), clip_cells(custom_title, 48)
    except Exception:
        return fallback, title


def project(payload: dict[str, Any] | None, session_id: str, *, worker: bool = False) -> bool:
    if worker or not session_id:
        return True
    pane = os.environ.get("HERDR_PANE_ID", "")
    herdr = shutil.which("herdr")
    handle = session_handle("codex", session_id)
    if not pane or not herdr or not handle:
        return True
    commands = [
        [herdr, "pane", "report-agent-session", pane, "--source", "herdr:codex",
         "--agent", "codex", "--agent-session-id", session_id],
    ]
    title = _title(session_id)
    display_agent, title = _display_metadata(session_id, title, handle)
    metadata = [herdr, "pane", "report-metadata", pane, "--source", "herdr:codex",
                "--display-agent", display_agent]
    if title:
        metadata += ["--title", clip_cells(title, 48)]
    commands.append(metadata)
    for command in commands:
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=0.5, check=False)
        except Exception:
            pass
    return True
