#!/usr/bin/env python3
"""Project this Claude session's identity into its Herdr pane header.

Claude's pinned Herdr v11 hook reports the native session at SessionStart, but no title
or ongoing lifecycle state. This Hearting-owned hook adds display metadata through the
shared projector, so Claude, Codex, and OpenCode put the same tag, steward mark, harness,
and summary in the pane header.

Fail-soft by construction: reads stdin, never writes stdout, and exits 0 on every path.
A registered worker projects nothing — the pane belongs to the interactive session.
"""
import json
import os
from pathlib import Path
import sys
import time

# The hook's own budget starts before the heavy projection imports below (they
# live inside `main`). A Stop that reaches this file under load must still leave
# the idle-receive retry to the next Stop/prompt/runner rather than run it past
# the deadline.
_STARTED_NS = time.monotonic_ns()
RETRY_BUDGET_NS = 3_000_000_000


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return 0
    # Herdr's pane, else the tmux pane this hook runs in; either is only a hint
    # until may_report proves the runtime there.
    tmux_pane = ""
    if not os.environ.get("HERDR_PANE_ID"):
        tmux = os.environ.get("TMUX_PANE", "")
        if not (os.environ.get("TMUX") and tmux.startswith("%") and tmux[1:].isdigit()):
            return 0
        tmux_pane = tmux
    try:
        home = os.environ.get("AGENT_HOME") or os.environ.get("CLAUDE_HOME")
        roots = [Path(home) / "tools"] if home else []
        roots += [parent / "tools" for parent in Path(__file__).resolve().parents]
        for tools in roots:
            if not (tools / "fleet" / "herdr_projection.py").is_file():
                continue
            if str(tools) not in sys.path:
                sys.path.insert(0, str(tools))
            from fleet.herdr_projection import may_report, project
            event = payload.get("hook_event_name")
            # The display report and the idle-receive retry name the same pane.
            # Decide that identity once, then hand the answer to both; the retry
            # would otherwise repeat the full process-identity walk.
            permission = (may_report("claude", session_id, pane_id=tmux_pane or None)
                          if event in {"Stop", "UserPromptSubmit"} else None)
            # The v11 SessionStart hook reports the exact session once. This
            # Hearting hook owns display metadata and never duplicates that report.
            project("claude", session_id, report_session=False, permission=permission,
                    display_relations=True, pane_id=tmux_pane or None)
            if (event in {"SessionStart", "UserPromptSubmit", "Stop"}
                    and os.environ.get("AGENT_SESSION_ROLE", "").lower() != "worker"
                    and os.environ.get("AGENT_DISPATCH_CHILD") != "1"
                    and not os.environ.get("AGENT_DISPATCH_DEPTH")):
                import subprocess
                ensurer = subprocess.Popen(
                    [sys.executable, str(tools.parent / "utilities" / "peer-steward.py"),
                     "__ensure-obligations", "--lifecycle-root",
                     str(Path(__file__).resolve().parents[1])],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, start_new_session=True,
                )
                # Under a test the ensurer (and the runner it starts) ends with the test.
                if str(tools.parent / "utilities") not in sys.path:
                    sys.path.append(str(tools.parent / "utilities"))
                try:
                    from detached_lifetime import bind
                except ImportError:
                    pass
                else:
                    bind(ensurer)
            if (event in {"Stop", "UserPromptSubmit"} and permission
                    and time.monotonic_ns() - _STARTED_NS < RETRY_BUDGET_NS):
                # Reuse the existing idle/receive callback and shared draft guard.
                import importlib.util
                utility = tools.parent / "utilities" / "peer-message.py"
                spec = importlib.util.spec_from_file_location("_peer_idle_message", utility)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.retry_receiver_idle({"harness": "claude", "session_id": session_id},
                                        permission=permission)
            break
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
