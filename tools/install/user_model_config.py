"""Seed Hearting's per-runtime, user-owned model configuration once."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from pathlib import Path

import safe_fs


class UserModelConfigError(RuntimeError):
    """The user model-config path cannot be created without guessing ownership."""


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def _legacy_codex_projection(config_dir: Path, shipped: Path) -> bool:
    if not config_dir.is_symlink():
        return False
    try:
        return config_dir.resolve(strict=True) == shipped.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


# destructive-ok: reason=discard the exclusive-create payload temp; boundary=mkstemp sibling created inside the validated config directory
def seed_model_config(
    adapter: str,
    source: str | Path,
    runtime_home: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    """Create ``agent-config/models.conf`` only when no user copy exists."""
    if adapter not in {"claude", "codex", "opencode"}:
        raise UserModelConfigError(f"unknown adapter: {adapter!r}")
    shipped = Path(source)
    home = Path(runtime_home)
    if not home.is_absolute():
        raise UserModelConfigError(f"runtime home must be absolute: {home}")
    if not _regular_file(shipped):
        raise UserModelConfigError(f"shipped model config is not a regular file: {shipped}")

    config_dir = home / "agent-config"
    destination = config_dir / "models.conf"
    migrated = False

    if config_dir.is_symlink():
        if adapter != "codex" or not _legacy_codex_projection(config_dir, shipped):
            raise UserModelConfigError(f"foreign agent-config symlink collision: {config_dir}")
        migrated = True
    elif config_dir.exists() and not config_dir.is_dir():
        raise UserModelConfigError(f"agent-config is not a directory: {config_dir}")

    if not migrated and destination.is_symlink():
        raise UserModelConfigError(f"models.conf must not be a symlink: {destination}")
    if not migrated and destination.exists():
        if not _regular_file(destination):
            raise UserModelConfigError(f"models.conf is not a regular file: {destination}")
        return {
            "action": "seed_once",
            "source": str(shipped),
            "dest": str(destination),
            "status": "unchanged",
            "detail": "existing user-owned model config preserved byte-for-byte",
        }

    if dry_run:
        detail = (
            "would migrate legacy Codex link and seed user copy"
            if migrated
            else "would seed user copy"
        )
        return {
            "action": "seed_once",
            "source": str(shipped),
            "dest": str(destination),
            "status": "planned",
            "detail": detail,
        }

    if migrated:
        try:
            current = safe_fs.capture_state(config_dir)
            auth = safe_fs.authority(
                config_dir,
                owner="model-config:legacy-codex-projection",
                allowed_paths=(config_dir,),
                expected=current,
            )
            safe_fs.remove_exact(auth)
        except safe_fs.SafetyError as exc:
            raise UserModelConfigError(str(exc)) from exc
    config_dir.mkdir(parents=True, exist_ok=True)

    fd, temporary = tempfile.mkstemp(prefix=".models.conf.", dir=str(config_dir))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(shipped.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(shipped.stat().st_mode))
        try:
            os.link(temporary, destination)
            status = "migrated" if migrated else "created"
        except FileExistsError:
            if destination.is_symlink() or not _regular_file(destination):
                raise UserModelConfigError(
                    f"models.conf appeared with an unsafe type: {destination}"
                )
            status = "unchanged"
    finally:
        Path(temporary).unlink(missing_ok=True)

    return {
        "action": "seed_once",
        "source": str(shipped),
        "dest": str(destination),
        "status": status,
        "detail": (
            "legacy Codex projection migrated to a user-owned copy"
            if status == "migrated"
            else "user-owned model config seeded once; updates and uninstall preserve it"
            if status == "created"
            else "existing user-owned model config preserved byte-for-byte"
        ),
    }


def verification_check(check_id: str, path: str | Path):
    """Build a read-only verifier for the user-owned regular-file boundary."""
    destination = Path(path)

    def check() -> dict[str, object]:
        ok = _regular_file(destination)
        return {
            "id": check_id,
            "ok": ok,
            "detail": (
                f"user model config present: {destination}"
                if ok
                else f"missing or non-regular user model config: {destination}"
            ),
        }

    return check


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", required=True, choices=("claude", "codex", "opencode"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--runtime-home", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = seed_model_config(
            args.adapter,
            args.source,
            args.runtime_home,
            dry_run=args.dry_run,
        )
    except UserModelConfigError as exc:
        print(json.dumps({"status": "blocked", "detail": str(exc)}))
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
