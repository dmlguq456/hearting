#!/usr/bin/env python3
"""Opt-in, single-start ingress; the original argv and permission ingress survive."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "utilities"))
import session_shell


def interactive(harness, args):
    if any(a in {"--help", "-h", "--version", "-V", "--print", "-p"} for a in args) and harness != "codex":
        return False
    if harness == "claude":
        return not any(a == "-p" or a.startswith("--print=") or a.startswith("--output-format") or a.startswith("--input-format") for a in args) and not (
            args and args[0] in {"auth", "doctor", "update", "install", "mcp", "plugin", "agents", "setup-token"})
    if harness == "opencode":
        return not (args and args[0] in {"run", "serve", "web", "auth", "models", "mcp", "upgrade", "uninstall", "stats", "export", "import", "db", "debug", "github", "acp"})
    # Reuse the existing Codex command classifier without changing its ingress.
    import importlib.util
    spec = importlib.util.spec_from_file_location("shell_codex_launcher", ROOT / "utilities/codex-launcher.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.should_manage(args)


def asset_key(node):
    import hashlib
    import subprocess
    lock = (Path(__file__).parent / "package-lock.json").read_bytes()
    abi = subprocess.check_output([node, "-p", "process.versions.modules"], timeout=5).strip()
    return hashlib.sha256(lock + b"\0" + abi).hexdigest()[:24]


def asset_root(node):
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "hearting" / "session-shell" / asset_key(node)


def wrapped_command(harness, command):
    # A configuration override selects Codex's embedded backend. Preserve its
    # native shared-daemon ingress and per-thread queue instead.
    return list(command)


def herdr_environment(harness, environment):
    """Herdr's official foreground-wrapper hint, scoped to this eligible exec."""
    if (environment.get("HERDR_ENV") != "1" or environment.get("HERDR_AGENT")
            or any(not environment.get(key) for key in
                   ("HERDR_PANE_ID", "HERDR_BIN_PATH", "HERDR_SOCKET_PATH"))):
        return None
    return dict(environment, HERDR_AGENT=harness)


def _exit_on_signal(signum, _frame):
    raise SystemExit(128 + signum)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] not in session_shell.HARNESSES:
        raise SystemExit("internal session shell ingress requires a native executable")
    harness, executable, *native_args = args
    command = [executable, *native_args]
    def original():
        os.execvpe(executable, command, os.environ)
    if (os.environ.get("HEARTING_SHELL_SOCKET") or not sys.stdin.isatty() or not sys.stdout.isatty()
            or not session_shell.policy()[0] or not interactive(harness, native_args)):
        original()
    node = shutil.which("node")
    if not node:
        original()
    # The launcher owns the PTY. A display worker only borrows its descriptor.
    import pty_owner
    size = os.get_terminal_size(1)
    if size.columns < 20 or size.lines < 8:
        original()
    hinted = herdr_environment(harness, os.environ)
    if hinted is not None:
        # Herdr inspects the foreground process's initial environment. Updating
        # os.environ after startup is insufficient on Linux. Re-exec this same
        # PID before starting any native child; the explicit hint prevents a loop.
        os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve()), *args], hinted)
    with tempfile.TemporaryDirectory(prefix="hearting-shell-", dir="/tmp") as directory:
        # A closed window (SIGHUP) or a stop request (SIGTERM) ends this owner
        # through the normal unwind, so the per-session directory is removed.
        for sig in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, _exit_on_signal)
        env = dict(os.environ, HEARTING_SHELL_DIRECTORY=directory,
                   HEARTING_SHELL_SOCKET=str(Path(directory) / "control.sock"),
                   HEARTING_SHELL_TOKEN=os.urandom(32).hex(),
                   HEARTING_SHELL_PYTHON=sys.executable,
                   HEARTING_SHELL_LAYOUT=json.dumps(session_shell.display_policy(), separators=(",", ":")))
        try:
            return pty_owner.run(harness, command, wrapped_command(harness, command), node,
                                 Path(__file__).with_name("host.cjs"), env, directory)
        except pty_owner.BeforeStartError:
            original()


if __name__ == "__main__":
    raise SystemExit(main())
