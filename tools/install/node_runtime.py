#!/usr/bin/env python3
"""Ensure a usable Node.js runtime at install time (user-space, verified).

Claude plugin lifecycle hooks (openai-codex SessionStart/SessionEnd/Stop,
agent-note SessionEnd) execute ``node``; a host without it raises a hook
error at every session boundary. Policy (2026-08-21 user decision, matching
the Cairn Node dependency policy): reuse a compatible Node >= 20.9.0 from
PATH; otherwise install the latest verified LTS into user space and expose
it on the launcher bin dir. Every failure degrades to a warning — ensure
never raises and never blocks the install.

Standalone and Python-stdlib-only, like ``host_probes.py``. Ownership
boundary: only symlinks this module itself created (targets inside our
node root) are ever replaced; a foreign ``node`` on PATH or a foreign file
at the expose path is reported, never touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

MIN_NODE = (20, 9, 0)
DIST_INDEX_URL = "https://nodejs.org/dist/index.json"
DIST_BASE_URL = "https://nodejs.org/dist"
INDEX_LIMIT = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
EXPOSED = ("node", "npm", "npx")

_ARCHES = {"x86_64": "x64", "aarch64": "arm64"}


def _result(status: str, detail: str) -> dict:
    return {"id": "host.node-runtime", "status": status, "detail": detail}


def _node_root() -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data / "hearting" / "node"


def _bin_dir() -> Path:
    return Path(os.environ.get("HARNESS_BIN_DIR") or Path.home() / ".local" / "bin")


def _parse_version(text: str) -> tuple | None:
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _current_node_version() -> tuple | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        probe = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if probe.returncode != 0:
        return None
    return _parse_version(probe.stdout)


def _fetch_bytes(url: str, limit: int) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "hearting-installer/1"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise OSError(f"response exceeds size limit: {url}")
    return payload


def _latest_lts() -> str:
    rows = json.loads(_fetch_bytes(DIST_INDEX_URL, INDEX_LIMIT))
    for row in rows:
        if isinstance(row, dict) and row.get("lts") and row.get("version"):
            return row["version"]
    raise OSError("no LTS entry in the Node.js dist index")


def _expected_checksum(version: str, archive_name: str) -> str:
    text = _fetch_bytes(f"{DIST_BASE_URL}/{version}/SHASUMS256.txt", INDEX_LIMIT).decode(
        "utf-8", errors="replace"
    )
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive_name:
            return parts[0]
    raise OSError(f"no checksum for {archive_name}")


def _download_verified(url: str, destination: Path, expected: str) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "hearting-installer/1"}
    )
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("xb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise OSError("node archive exceeds size limit")
                digest.update(chunk)
                handle.write(chunk)
    if digest.hexdigest() != expected:
        raise OSError(f"checksum mismatch for {url}")


def _extract(archive: Path, staging: Path, top_level: str) -> Path:
    subprocess.run(
        ["tar", "-xJf", str(archive), "-C", str(staging)],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    extracted = staging / top_level
    if not (extracted / "bin" / "node").is_file():
        raise OSError(f"archive did not contain {top_level}/bin/node")
    return extracted


def _owned_link(path: Path, root: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        target = Path(os.readlink(path))
    except OSError:
        return False
    if not target.is_absolute():
        target = path.parent / target
    try:
        return str(target.resolve()).startswith(str(root.resolve()) + os.sep)
    except OSError:
        return False


def _expose(install_dir: Path, root: Path) -> list:
    """Symlink node/npm/npx into the bin dir; never replace a foreign entry."""
    bin_dir = _bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    skipped = []
    current = root / "current"
    tmp_link = root / ".current.tmp"
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(install_dir)
    tmp_link.replace(current)
    for name in EXPOSED:
        source = current / "bin" / name
        if not source.exists():
            continue
        link = bin_dir / name
        if link.exists() or link.is_symlink():
            if not _owned_link(link, root):
                skipped.append(name)
                continue
            link.unlink()
        link.symlink_to(source)
    return skipped


def ensure_node() -> dict:
    """Reuse a compatible node, else install a verified LTS. Never raises."""
    try:
        if os.environ.get("HARNESS_NO_NODE_INSTALL") == "1":
            return _result("ok", "node ensure skipped (HARNESS_NO_NODE_INSTALL=1)")
        found = _current_node_version()
        if found is not None and found >= MIN_NODE:
            return _result(
                "ok", "reusing node v%d.%d.%d from PATH" % found
            )
        arch = _ARCHES.get(platform.machine())
        if platform.system() != "Linux" or arch is None:
            return _result(
                "warning",
                f"no compatible node and no managed build for "
                f"{platform.system()}/{platform.machine()}; install Node >= "
                "%d.%d.%d manually" % MIN_NODE,
            )
        version = _latest_lts()
        root = _node_root()
        install_dir = root / version
        top_level = f"node-{version}-linux-{arch}"
        if not (install_dir / "bin" / "node").is_file():
            archive_name = f"{top_level}.tar.xz"
            expected = _expected_checksum(version, archive_name)
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=root) as staging:
                staging_path = Path(staging)
                archive = staging_path / archive_name
                _download_verified(
                    f"{DIST_BASE_URL}/{version}/{archive_name}", archive, expected
                )
                extracted = _extract(archive, staging_path, top_level)
                if install_dir.exists():
                    shutil.rmtree(install_dir)
                extracted.rename(install_dir)
        smoke = subprocess.run(
            [str(install_dir / "bin" / "node"), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if smoke.returncode != 0 or _parse_version(smoke.stdout) is None:
            return _result(
                "warning", f"installed node failed its version check: {smoke.stderr.strip()}"
            )
        skipped = _expose(install_dir, root)
        detail = f"installed node {version} at {install_dir}"
        if found is not None:
            detail += "; existing node v%d.%d.%d on PATH is below %d.%d.%d and was left untouched" % (
                found + MIN_NODE
            )
        if skipped:
            detail += (
                f"; kept foreign {'/'.join(skipped)} in {_bin_dir()} — "
                f"use {_node_root() / 'current' / 'bin'} directly"
            )
        elif str(_bin_dir()) not in os.environ.get("PATH", "").split(os.pathsep):
            detail += f"; add {_bin_dir()} to PATH"
        return _result("installed", detail)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip().splitlines()
        return _result(
            "warning", f"node install failed: {stderr[0] if stderr else exc}"
        )
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return _result("warning", f"node install failed: {exc}")
