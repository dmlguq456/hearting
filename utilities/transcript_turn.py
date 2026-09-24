"""One definition of "the current turn" for a Claude transcript.

`hooks/material-route-guard.py` (recall-opportunity turn comparison) and
`hooks/mem-recall-inject.sh`'s embedded prompt-hook python both need the same
answer to "which row is the current user turn", or a candidate-probe receipt
written by one and checked by the other disagrees for reasons neither side can
see (route-guard-recovery D1/D8: 34 of 55 `recall-opportunity-turn-mismatch`
denials over 30 days traced to the hook skipping tool_result rows that this
module skips).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
from typing import Any


def _is_tool_result_user_row(row: dict[str, Any]) -> bool:
    """Return whether a Claude ``type:user`` row is a tool result, not a prompt."""
    message = row.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    blocks = content if isinstance(content, list) else [content]
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in blocks
    )


def transcript_turn_id(path_value: object) -> str:
    """Derive the current Claude turn from the bounded tail of its transcript.

    The current turn is the last ``type:"user"`` row that is not a sidechain
    row, not a tool_result row, not ``isMeta``, and not ``isCompactSummary``.
    Reads only the trailing 1 MiB of the transcript and refuses a symlinked or
    non-regular path.
    """
    if not isinstance(path_value, str) or not path_value:
        return ""
    path = Path(path_value)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            return ""
        with path.open("rb") as handle:
            start = max(0, info.st_size - 1024 * 1024)
            handle.seek(start)
            if start:
                handle.readline()
            lines = handle.read().splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if (not isinstance(row, dict) or row.get("type") != "user"
                or row.get("isSidechain") is True
                or row.get("isMeta") is True
                or row.get("isCompactSummary") is True
                or _is_tool_result_user_row(row)):
            continue
        uid = row.get("uuid")
        if isinstance(uid, str) and uid:
            return f"transcript-user:{uid}"
        material = json.dumps(
            [row.get("timestamp"), row.get("message")],
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        return "transcript-user-hash:" + hashlib.sha256(material.encode()).hexdigest()
    return ""
