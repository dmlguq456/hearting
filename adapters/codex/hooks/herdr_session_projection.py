"""Fail-soft projection of an existing Codex session into an existing Herdr pane.

The shape of that projection — `[<tag>] [⚑ ]<harness>` plus the existing summary — is
shared with Claude and OpenCode in `tools/fleet/herdr_projection.py`, because a pane
header that means one thing in one harness and another thing in the next is exactly the
identity confusion it exists to remove. This module stays as the Codex hook entry point
its two lifecycle hooks already call.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
from fleet.herdr_projection import may_report as _may_report  # noqa: E402
from fleet.herdr_projection import project as _project  # noqa: E402


def _state_hook_owns_report(payload: dict[str, Any] | None, session_id: str,
                            *, worker: bool) -> bool:
    """Let the installed v10 callback own an embedded, fully proven report."""
    if (not isinstance(payload, dict)
            or payload.get("hook_event_name") != "SessionStart"
            or not payload.get("transcript_path")
            or not os.environ.get("HERDR_SOCKET_PATH")
            or not os.environ.get("HERDR_PANE_ID")
            or (os.environ.get("CODEX_THREAD_ID")
                and os.environ["CODEX_THREAD_ID"] != session_id)):
        return False
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    if not (codex_home / "herdr-agent-state.sh").is_file():
        return False
    try:
        return _may_report("codex", session_id, worker=worker,
                           pane_id=os.environ["HERDR_PANE_ID"])
    except Exception:
        return False


def project(payload: dict[str, Any] | None, session_id: str, *, worker: bool = False,
            request_recovery: bool = True) -> bool:
    lifecycle = {}
    if isinstance(payload, dict) and payload.get("hook_event_name") == "SessionStart":
        source = payload.get("source")
        if source in {"startup", "resume", "clear", "compact", "fork"}:
            lifecycle = {"session_seq": time.time_ns(), "session_start_source": source}
    # The installed v10 callback owns embedded reporting when its exact TUI,
    # pane, socket and transcript proof all pass. Otherwise this existing
    # projection reports directly or uses its deferred exact-TUI proof path.
    report_session = not _state_hook_owns_report(payload, session_id, worker=worker)
    # A display header, like the Claude projection hook: the steward mark reads
    # the runner-published relations instead of walking every seat ledger.
    projected = _project("codex", session_id, worker=worker,
                         report_session=report_session, display_relations=True, **lifecycle)
    if request_recovery and not worker and session_id:
        try:
            utilities = ROOT / "utilities"
            if str(utilities) not in sys.path:
                sys.path.insert(0, str(utilities))
            from peer_obligations import request_obligation_recovery
            request_obligation_recovery(lifecycle_root=ROOT)
        except Exception:
            pass
    return projected
