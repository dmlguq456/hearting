"""User configuration for the shared protocol-v2 memory exchange.

Only one runtime keeps a settings file that can carry environment defaults, so
exchange policy written there would leave the other adapters syncing
local-only. This module owns the portable file all three read, and `mem`
overlays the process environment on top of it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union

import paths


SCHEMA_VERSION = 1
DEFAULT_REF = "refs/heads/hearting-memory-v2"
FIELDS = ("enabled", "remote_url", "ref", "exchange_dir")


def config_path() -> Path:
    return paths.hearting_config_dir() / "memory-sync.json"


def default_exchange_dir() -> Path:
    """Per-host exchange root; the transport requires a private location."""
    return paths.xdg_state_home() / "hearting" / "memory-sync" / "exchange"


def _absolute(value: Union[str, Path], label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path: {path}")
    return path.resolve(strict=False)


def _validate_ref(ref: str) -> str:
    if not ref.startswith("refs/heads/") or ".." in ref or ref.endswith(("/", ".lock")):
        raise ValueError(f"ref must be a full refs/heads/* ref: {ref}")
    return ref


def _validate_remote(remote: str) -> str:
    remote = remote.strip()
    if not remote or remote.startswith("-") or any(
            char in remote for char in ("\x00", "\n", "\r")):
        raise ValueError("remote URL is missing or invalid")
    return remote


def read(path: Optional[Path] = None) -> dict:
    path = path or config_path()
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"memory sync config is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid memory sync config: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"invalid memory sync config schema: {path}")
    return value


def resolve(*, optional: bool = False) -> Optional[dict]:
    path = config_path()
    if not path.exists() and not path.is_symlink():
        if optional:
            return None
        raise ValueError(f"memory sync config is not initialized: {path}")
    return read(path)


def write(*, remote_url: str, ref: Optional[str] = None,
          exchange_dir: Optional[Union[str, Path]] = None,
          enabled: bool = True, dry_run: bool = False) -> dict:
    """Record the exchange policy, replacing any previous one."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "enabled": bool(enabled),
        "remote_url": _validate_remote(remote_url),
        "ref": _validate_ref(ref or DEFAULT_REF),
        "exchange_dir": str(_absolute(
            exchange_dir or os.environ.get("MEM_SYNC_DIR")
            or default_exchange_dir(), "exchange_dir")),
    }
    path = config_path()
    if dry_run:
        return {"status": "would-write", "path": str(path), **payload}

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return {"status": "written", "path": str(path), **payload}


def validate() -> dict:
    path = config_path()
    try:
        value = resolve(optional=True)
    except ValueError as exc:
        return {"status": "invalid", "path": str(path), "detail": str(exc)}
    if value is None:
        return {"status": "absent", "path": str(path)}
    return {"status": "ok", "path": str(path),
            **{key: value.get(key) for key in FIELDS}}
