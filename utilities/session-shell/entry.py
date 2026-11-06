#!/usr/bin/env python3
"""Opt-in, single-start ingress; the original argv and permission ingress survive."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import secrets
import subprocess
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
    lock = (Path(__file__).parent / "package-lock.json").read_bytes()
    abi = subprocess.check_output([node, "-p", "process.versions.modules"], timeout=5).strip()
    return hashlib.sha256(lock + b"\0" + abi).hexdigest()[:24]


def asset_root(node):
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "hearting" / "session-shell" / asset_key(node)


def wrapped_command(harness, command):
    if harness == "codex":
        return [command[0], "-c", 'tui.status_line=["thread-id","context-used"]', *command[1:]]
    return list(command)


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
    try:
        assets = asset_root(node)
        ready = json.loads((assets / "ready.json").read_text())
        if ready != {"key": assets.name} or not (assets / "node_modules/node-pty").is_dir():
            original()
    except (OSError, ValueError, subprocess.SubprocessError):
        original()
    # The launcher owns the PTY. A display worker only borrows its descriptor.
    import pty_owner
    size = os.get_terminal_size(1)
    if size.columns < 20 or size.lines < 8:
        original()
    with tempfile.TemporaryDirectory(prefix="hearting-shell-", dir="/tmp") as directory:
        env = dict(os.environ, HEARTING_SHELL_DIRECTORY=directory,
                   HEARTING_SHELL_SOCKET=str(Path(directory) / "control.sock"),
                   HEARTING_SHELL_TOKEN=secrets.token_hex(32),
                   HEARTING_SHELL_PYTHON=sys.executable, NODE_PATH=str(assets / "node_modules"))
        try:
            return pty_owner.run(harness, command, wrapped_command(harness, command), node,
                                 Path(__file__).with_name("host.cjs"), env, directory)
        except pty_owner.BeforeStartError:
            original()


if __name__ == "__main__":
    raise SystemExit(main())
