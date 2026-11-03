#!/usr/bin/env python3
"""Warn-only host fitness probes run at install time.

Standalone and Python-stdlib-only, matching ``distribution.py`` — these probes
may run before a harness root exists. Every probe returns ``status in
{"ok", "warning"}`` and never raises; a probe result is advisory only and
must never influence ``install``'s exit code (``verify`` checks the PyYAML
probe, which framed routes need; ``runtime doctor`` prints it as one line).
"""
import importlib.util
import shlex
import shutil
import subprocess
import sys


def probe_node() -> dict:
    path = shutil.which("node")
    if path is None:
        managed = _managed_node()
        if managed is not None:
            return {
                "id": "host.node",
                "status": "ok",
                "detail": f"managed install at {managed} (not yet on this shell's PATH)",
            }
        return {
            "id": "host.node",
            "status": "warning",
            "detail": (
                "node not found on PATH; Claude plugin lifecycle hooks "
                "(openai-codex Stop/SessionEnd) will raise a hook error at "
                "every session boundary"
            ),
        }
    return {"id": "host.node", "status": "ok", "detail": path}


def _managed_node():
    try:
        import node_runtime
    except ImportError:
        return None
    candidate = node_runtime._node_root() / "current" / "bin" / "node"
    return candidate if candidate.is_file() else None


def probe_bwrap_userns() -> dict:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return {
            "id": "host.bwrap-userns",
            "status": "ok",
            "detail": "bwrap not installed; skipped (a different sandbox backend may apply)",
        }
    try:
        result = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "true"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "id": "host.bwrap-userns",
            "status": "warning",
            "detail": f"probe could not complete: {exc}",
        }
    if result.returncode == 0:
        return {"id": "host.bwrap-userns", "status": "ok", "detail": "bwrap sandbox init succeeded"}
    stderr_first_line = (result.stderr or "").strip().splitlines()[0] if result.stderr else ""
    detail = (
        f"registered codex child dispatch will die dead-worker-blocked at spawn: {stderr_first_line}. "
        "Likely cause: kernel.apparmor_restrict_unprivileged_userns=1 with no AppArmor userns "
        "exception profile for bwrap (observed 2026-08-21: 'setting up uid map: Permission denied' / "
        "'loopback: Failed RTM_NEWADDR: Operation not permitted'). "
        "Recommended: add a userns-allowing AppArmor profile for bwrap; a global sysctl relaxation "
        "is not recommended."
    )
    return {"id": "host.bwrap-userns", "status": "warning", "detail": detail}


def probe_herdr() -> dict:
    path = shutil.which("herdr")
    if path is None:
        return {
            "id": "host.herdr",
            "status": "warning",
            "detail": (
                "herdr CLI not found (optional): peer windows (peer-steward start/watch) "
                "and question notifications are off; inside tmux, pane titles (Claude), peer "
                "delivery and automatic /clear use tmux, elsewhere they are off; "
                "core work is unaffected"
            ),
        }
    try:
        result = subprocess.run(
            ["herdr", "--version"], capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "id": "host.herdr",
            "status": "warning",
            "detail": f"probe could not complete: {exc}",
        }
    if result.returncode == 0:
        return {"id": "host.herdr", "status": "ok", "detail": (result.stdout or path).strip()}
    return {
        "id": "host.herdr",
        "status": "warning",
        "detail": f"herdr --version exited {result.returncode}",
    }


def probe_pyyaml() -> dict:
    """PyYAML for the Python running the harness: a framed route reads its frame proposals with it."""
    python = shlex.quote(sys.executable or "python3")
    try:
        found = importlib.util.find_spec("yaml") is not None
    except (ImportError, ValueError):
        found = False
    if found:
        return {"id": "host.pyyaml", "status": "ok", "detail": f"importable by {python}"}
    return {
        "id": "host.pyyaml",
        "status": "warning",
        "detail": (
            f"PyYAML is not importable by {python}: work that starts with the planning "
            "step (a framed route, the default when no shape is given) reads its plan "
            "proposal with it and stops before planning without it. Install it for that "
            f"Python (e.g. `{python} -m pip install --user pyyaml`, or the OS package such "
            "as python3-yaml); a non-framed shape (direct, solo or staged) runs without it"
        ),
    }


def run() -> list:
    return [probe_node(), probe_bwrap_userns(), probe_herdr(), probe_pyyaml()]
