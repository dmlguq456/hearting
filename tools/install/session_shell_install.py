"""Create-once PATH attachment; native executables and user config stay intact.

Only our private launcher directory and one exact profile block are owned here.
First global installs seed the default; updates preserve an absent policy.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

import codex_launcher
import paths
import safe_fs

START = b"# >>> hearting session shell >>>"
END = b"# <<< hearting session shell <<<"
NO_FINAL_LF = START + b" no-final-lf"
MARKER = "# hearting-session-shell "
HARNESSES = ("claude", "codex", "opencode")


def _source():
    return paths.agent_home() / "utilities" / "session-shell"


def _client():
    import sys
    utilities = str(paths.agent_home() / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    import session_shell
    return session_shell


def _bin():
    return paths.xdg_data_home() / "hearting" / "session-shell" / "bin"


def _write(path, payload, mode=0o600, *, expected=None):
    current = expected or safe_fs.capture_state(path)
    auth = safe_fs.authority(path, owner="session-shell:exact-owned-leaf", allowed_paths=(path,), expected=current)
    return safe_fs.atomic_write_bytes(auth, payload, mode, create_parents=True)


def _enabled_policy(enabled):
    client = _client()
    before = safe_fs.capture_state(client.policy_path(), include_payload=True)
    try:
        value = client._private_json(client.policy_path(), client.MAX_RECORD_BYTES)
        if not isinstance(value, dict):
            value = {}
    except (OSError, ValueError, TypeError):
        value = {}
    value["enabled"] = enabled
    _write(client.policy_path(), (json.dumps(value, separators=(",", ":")) + "\n").encode(), expected=before)


def _wrapper(harness, native):
    metadata = json.dumps({"harness": harness, "native": native}, separators=(",", ":"))
    entry = paths.runtime_home(harness) / "hearting/utilities/session-shell/entry.py"
    return ("#!/bin/sh\n" + MARKER + metadata + "\n"
            + f"if [ -f {shlex.quote(str(entry))} ]; then\n"
            + f"  exec python3 {shlex.quote(str(entry))} {harness} {shlex.quote(native)} \"$@\"\nfi\n"
            + f"exec {shlex.quote(native)} \"$@\"\n").encode()


def _original(harness, launcher):
    if launcher.exists() or launcher.is_symlink():
        if launcher.is_symlink() or not launcher.is_file() or launcher.stat().st_size > 8192:
            raise ValueError("foreign session shell launcher preserved")
        lines = launcher.read_text().splitlines()
        if len(lines) < 2 or not lines[1].startswith(MARKER):
            raise ValueError("foreign session shell launcher preserved")
        record = json.loads(lines[1][len(MARKER):])
        native = record.get("native")
        if record.get("harness") != harness or not isinstance(native, str) or not os.path.isabs(native):
            raise ValueError("invalid session shell launcher preserved")
        # A changed owned block is user data. Never repair it by overwriting.
        if launcher.read_bytes() != _wrapper(harness, native):
            raise ValueError("changed session shell launcher preserved")
        return native
    search = os.pathsep.join(p for p in os.get_exec_path() if Path(p).absolute() != _bin().absolute())
    return shutil.which(harness, path=search)


def _profile(enabled, *, dry_run=False):
    fish = Path(os.environ.get("SHELL", "")).name.lower() == "fish"
    path = (paths.xdg_config_home() / "fish/conf.d/hearting-session-shell.fish"
            if fish else codex_launcher._profile_path())
    if path is None:
        raise ValueError("cannot identify one supported shell profile")
    codex_launcher._validate_profile_path(path)
    before = safe_fs.capture_state(path, include_payload=True)
    data = before.payload if before.kind == "file" else b""
    line = ("set -gx PATH " + shlex.quote(str(_bin())) + " $PATH\n") if fish else (
        "export PATH=" + shlex.quote(str(_bin())) + ':"$PATH"\n')
    starts, ends = data.count(START), data.count(END)
    if starts != ends or starts > 1:
        raise ValueError("changed session shell profile block preserved")
    if starts:
        a = data.index(START); b = data.index(END, a) + len(END)
        joined = data[a:].startswith(NO_FINAL_LF + b"\n")
        block = (NO_FINAL_LF if joined else START) + b"\n" + line.encode() + END
        if (a and data[a-1:a] != b"\n") or data[a:b] != block:
            raise ValueError("changed session shell profile block preserved")
        if data[b:b+1] == b"\n": b += 1
        # The owned marker records only the separator this block introduced.
        cut = a-1 if joined and a and b == len(data) else a
        after = data if enabled else data[:cut] + data[b:]
    else:
        joined = bool(data and not data.endswith(b"\n"))
        block = (NO_FINAL_LF if joined else START) + b"\n" + line.encode() + END + b"\n"
        after = data + (b"\n" if joined else b"") + block if enabled else data
    if after != data and not dry_run:
        _write(path, after, before.mode or 0o600, expected=before)
    return {"path": str(path), "changed": after != data}


def prepare_assets(*, dry_run=False):
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise ValueError("session shell runtime unavailable; native launchers remain in use")
    spec = importlib.util.spec_from_file_location("hearting_shell_entry", _source() / "entry.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    target = module.asset_root(node)
    try:
        if json.loads((target / "ready.json").read_text()) == {"key": target.name}:
            return target
    except (OSError, ValueError):
        pass
    if dry_run:
        return target
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Build away from the published cache. A failed build cannot replace a
    # working runtime and there is no build/network operation in the launcher.
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=target.parent) as directory:
        stage = Path(directory)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(_source() / name, stage / name)
        result = subprocess.run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=stage,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.returncode:
            raise ValueError("session shell asset build failed; native launchers remain in use")
        subprocess.run([node, "-e", "require('node-pty');require('@xterm/headless');require('@xterm/addon-unicode11')"],
                       cwd=stage, check=True, stdout=subprocess.DEVNULL)
        (stage / "ready.json").write_text(json.dumps({"key": target.name}))
        if target.exists():
            raise ValueError("incomplete session shell asset directory preserved")
        # destructive-ok: reason=publish the prepared runtime assets; boundary=one private staging directory and its absent content-keyed cache destination
        os.rename(stage, target)
    return target


def ensure(*, dry_run=False, enabled=None, default_enabled=False):
    client = _client()
    # Presence is a user choice, even if invalid or unreadable. Seed only the
    # exact absent leaf observed before preparing assets and launchers.
    policy_before = safe_fs.capture_state(client.policy_path())
    seed_default = default_enabled and policy_before.kind == "missing" and enabled is None
    selected = (True if seed_default else client.policy()[0]) if enabled is None else enabled
    if not selected:
        return {"status": "disabled", "changed": False}
    # Validate every owned surface before publishing any launcher or policy.
    planned = []
    for harness in HARNESSES:
        launcher = _bin() / harness
        native = _original(harness, launcher)
        if native:
            planned.append((launcher, _wrapper(harness, native), safe_fs.capture_state(launcher)))
    if not planned:
        raise ValueError("no native interactive CLI found")
    profile = _profile(True, dry_run=True)
    assets = prepare_assets(dry_run=dry_run)
    if not dry_run:
        for launcher, data, before in planned:
            if before.kind != "file" or launcher.read_bytes() != data:
                _write(launcher, data, 0o755, expected=before)
        profile = _profile(True)
        if seed_default:
            _write(client.policy_path(), b'{"enabled":true}\n', expected=policy_before)
    return {"status": "planned" if dry_run else "enabled", "changed": profile["changed"],
            "assets": str(assets), "profile": profile, "launchers": [str(row[0]) for row in planned]}


def set_enabled(enabled, *, dry_run=False):
    client = _client()
    if enabled:
        result = ensure(dry_run=dry_run, enabled=True)
        if not dry_run:
            _enabled_policy(True)
        return result
    # Disabling the policy first makes every retained wrapper transparent even
    # if a user-edited profile prevents removal of its former PATH block.
    if not dry_run:
        _enabled_policy(False)
    profile = _profile(False, dry_run=dry_run)
    return {"status": "disabled", "changed": profile["changed"], "profile": profile}
