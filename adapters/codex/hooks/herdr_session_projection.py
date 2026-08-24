"""Fail-soft projection of an existing Codex session into an existing Herdr pane."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools"
if str(TOOLS) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(TOOLS))
from fleet.session_handle import sanitize_title, session_handle  # noqa: E402


def _title(session_id: str) -> str:
    try:
        from fleet.titles import read
        value = read(session_id, harness="codex") or {}
        return sanitize_title(value.get("title"))
    except Exception:
        return ""


def project(payload: dict[str, Any] | None, session_id: str, *, worker: bool = False) -> bool:
    if worker or not session_id:
        return True
    pane = os.environ.get("HERDR_PANE_ID", "")
    herdr = shutil.which("herdr")
    handle = session_handle("codex", session_id)
    if not pane or not herdr or not handle:
        return True
    commands = [
        [herdr, "pane", "report-agent-session", "--source", "hearting:codex",
         "--agent", "codex", "--agent-session-id", session_id, pane],
    ]
    title = _title(session_id)
    metadata = [herdr, "pane", "report-metadata", "--source", "hearting:codex",
                "--display-agent", handle]
    if title:
        metadata += ["--title", title[:80]]
    metadata.append(pane)
    commands.append(metadata)
    for command in commands:
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=0.5, check=False)
        except Exception:
            pass
    return True
