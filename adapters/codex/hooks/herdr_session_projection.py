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
from fleet.session_handle import clip_cells, display_name, resolve_display_inputs, sanitize_title  # noqa: E402


def _runtime_name(session_id: str) -> str | None:
    """F-99a ① Codex runtime_name (latest ``thread_name``), falling back to the ②
    hearting session-name registry — same rule as the Fleet Codex collector's
    ``_thread_runtime_names`` (Q-2: automatic titles land in the same field as a
    user-set name, so any name here is honestly treated as ①)."""
    try:
        from fleet.collectors.codex import _home, _thread_runtime_names
        name = _thread_runtime_names(_home()).get(session_id)
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        pass
    try:
        return resolve_display_inputs("codex", session_id).get("runtime_name")
    except Exception:
        return None


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
        # `fallback` is now the F-99 canonical name (a real title, not a fixed-width
        # sid8 handle) — clip it the same way the formatter-success branch below does.
        return clip_cells(fallback, 24), title
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
    if not pane or not herdr:
        return True
    title = _title(session_id)
    canonical_name = display_name(
        "codex", session_id, runtime_name=_runtime_name(session_id),
        registry_name=None, title=title, slug=None, cwd=None)
    if not canonical_name:
        return True
    commands = [
        [herdr, "pane", "report-agent-session", pane, "--source", "herdr:codex",
         "--agent", "codex", "--agent-session-id", session_id],
    ]
    display_agent, title = _display_metadata(session_id, title, canonical_name)
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
