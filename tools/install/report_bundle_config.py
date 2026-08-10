"""Create-once user configuration for the shared report bundle store."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union

import paths


ENV_NAME = "REPORT_BUNDLE_ROOT"
SCHEMA_VERSION = 1


def config_path() -> Path:
    return paths.hearting_config_dir() / "report-bundle.json"


def default_root() -> Path:
    return paths.xdg_data_home() / "hearting" / "report-bundles"


def _absolute(value: Union[str, Path]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{ENV_NAME} must be an absolute path: {path}")
    return path.resolve(strict=False)


def _read(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"report bundle config is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid report bundle config: {path}") from exc
    if set(value) != {"schema_version", ENV_NAME} or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"invalid report bundle config schema: {path}")
    return _absolute(value[ENV_NAME])


def resolve(*, optional: bool = False) -> Optional[Path]:
    override = os.environ.get(ENV_NAME)
    if override:
        return _absolute(override)
    path = config_path()
    if not path.exists() and not path.is_symlink():
        if optional:
            return None
        raise ValueError(f"report bundle config is not initialized: {path}")
    return _read(path)


def ensure(requested: Optional[Union[str, Path]] = None, *, dry_run: bool = False) -> dict:
    path = config_path()
    if path.exists() or path.is_symlink():
        root = _read(path)
        return {"status": "preserved", "path": str(path), "root": str(root)}

    root = _absolute(requested or os.environ.get(ENV_NAME) or default_root())
    if dry_run:
        return {"status": "would-create", "path": str(path), "root": str(root)}

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"report bundle root is not a directory: {root}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(
        {"schema_version": SCHEMA_VERSION, ENV_NAME: str(root)},
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        root = _read(path)
        return {"status": "preserved", "path": str(path), "root": str(root)}
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return {"status": "created", "path": str(path), "root": str(root)}


def validate() -> dict:
    path = config_path()
    try:
        root = resolve()
        if root is None or root.is_symlink() or not root.is_dir():
            raise ValueError(f"report bundle root is not a directory: {root}")
    except ValueError as exc:
        return {"status": "invalid", "ok": False, "path": str(path), "detail": str(exc)}
    return {"status": "valid", "ok": True, "path": str(path), "root": str(root)}
