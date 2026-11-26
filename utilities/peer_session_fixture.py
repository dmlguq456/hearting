"""Test-only bridge from a simulated window inventory to ``peer_address``'s live sessions.

Production reads live native sessions from procfs (``peer_address.native_sessions``).
Older peer fixtures describe their sessions as a mocked ``agent list`` reply on
``subprocess.run``. ``install`` makes that same description the live-session table,
and consults ``subprocess.run`` only while a test has mocked it, so no test reads
this machine's processes or runs a real terminal multiplexer.
"""
from __future__ import annotations

import json
import subprocess
from unittest import mock

import peer_address


def install(steward):
    """Replace ``peer_address.native_sessions`` for this test process."""
    def native_sessions():
        if not isinstance(subprocess.run, mock.NonCallableMock):
            return []
        try:
            reply = subprocess.run(steward._herdr_argv("agent", "list"),
                                   capture_output=True, text=True, timeout=5)
            agents = ((json.loads(reply.stdout or "") or {}).get("result") or {}).get("agents") or []
        except Exception:
            return []
        server = steward._HERDR_SESSION or "default"
        rows = []
        for agent in agents if isinstance(agents, list) else ():
            if not isinstance(agent, dict) or agent.get("agent") not in peer_address.HARNESSES:
                continue
            session = agent.get("agent_session")
            sid = session.get("value") if isinstance(session, dict) else None
            rows.append({"harness": agent["agent"], "sid": sid if isinstance(sid, str) else "",
                         "pid": 0, "binding": None, "name": agent.get("name"),
                         "env": {"HERDR_PANE_ID": str(agent.get("pane_id") or ""),
                                 "HERDR_SESSION": server}})
        return rows

    peer_address.native_sessions = native_sessions
    return native_sessions
