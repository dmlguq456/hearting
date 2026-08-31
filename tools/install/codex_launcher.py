"""Install and restore the transparent interactive Codex launcher."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses the same atomic protocol without flock.
    fcntl = None


SCHEMA = 2
SUPPORTED_SCHEMAS = {1, 2}
STATE_NAME = "codex-launcher.json"
LOCK_NAME = "codex-launcher.lock"
PROFILE_START = b"# >>> hearting-codex protected ingress >>>"
PROFILE_END = b"# <<< hearting-codex protected ingress <<<"


class CodexLauncherError(RuntimeError):
    """The launcher cannot be installed or restored without clobbering data."""


class CodexUnavailableError(CodexLauncherError):
    """The real Codex CLI is not installed yet."""


def _home() -> Path:
    raw = os.environ.get("HOME")
    return Path(raw).expanduser() if raw else Path.home()


def default_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else _home() / ".codex"


def default_bin_dir() -> Path:
    raw = os.environ.get("HARNESS_BIN_DIR")
    return Path(raw).expanduser() if raw else default_codex_home() / ".harness" / "bin"


def vendor_bin_dir() -> Path:
    """The standalone vendor location; it is never used for Hearting ingress."""
    return _home() / ".local" / "bin"


def state_path(codex_home: Path) -> Path:
    return codex_home / ".harness" / STATE_NAME


def wrapper_path(bin_dir: Path) -> Path:
    return bin_dir / "codex"


def lock_path(codex_home: Path) -> Path:
    return codex_home / ".harness" / LOCK_NAME


def _profile_path() -> Path | None:
    """Return the only supported startup file for the current shell."""
    shell = Path(os.environ.get("SHELL", "")).name.lower()
    home = _home()
    if shell in {"bash", "sh"}:
        return home / ".bashrc"
    if shell in {"zsh"}:
        return Path(os.environ.get("ZDOTDIR", str(home))) / ".zshrc"
    if shell in {"fish"}:
        config = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
        return config / "fish" / "conf.d" / "hearting-codex.fish"
    return None


def _profile_block(bin_dir: Path) -> bytes:
    path = str(bin_dir)
    return (PROFILE_START + b"\n" +
            f'export PATH="{path}:$PATH"\n'.encode() +
            PROFILE_END + b"\n")


def _validate_profile_path(path: Path) -> None:
    """Fail closed before touching a startup file or any of its parents."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CodexLauncherError(f"unsafe shell profile: {path}")
    current = path.parent
    creation_anchor_checked = False
    while True:
        if current.is_symlink():
            raise CodexLauncherError(f"unsafe shell profile parent: {current}")
        if current.exists():
            info = current.stat()
            shared_temp = stat.S_ISDIR(info.st_mode) and bool(info.st_mode & stat.S_ISVTX) and os.access(current, os.W_OK)
            system_root = current == current.parent
            if not stat.S_ISDIR(info.st_mode):
                raise CodexLauncherError(f"shell profile parent is not owner-writable: {current}")
            if not creation_anchor_checked:
                # The nearest existing ancestor is where mkdir/write starts.
                # It must be writable by this user and either user-owned or a
                # sticky shared-temp directory.  Higher immutable system
                # ancestors such as /home and /var are trusted boundaries,
                # not profile-owned directories.
                if not os.access(current, os.W_OK) or (
                    info.st_uid != os.geteuid() and not shared_temp
                ):
                    raise CodexLauncherError(f"shell profile parent is not owner-writable: {current}")
                creation_anchor_checked = True
            elif (
                not system_root
                and info.st_uid != os.geteuid()
                and not shared_temp
                and os.access(current, os.W_OK)
            ):
                raise CodexLauncherError(f"shell profile parent is not owner-writable: {current}")
        if current.parent == current:
            break
        current = current.parent
    if path.exists():
        info = path.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise CodexLauncherError(f"shell profile is not owner-controlled: {path}")


def _profile_state(bin_dir: Path) -> dict:
    path = _profile_path()
    if path is None:
        return {"path": None, "health": "ambiguous-or-unsupported-profile", "managed": False}
    _validate_profile_path(path)
    data = path.read_bytes() if path.exists() else b""
    blocks = data.count(PROFILE_START)
    ends = data.count(PROFILE_END)
    exact = _profile_block(bin_dir)
    if blocks > 1 or ends > 1 or (blocks != ends):
        return {"path": str(path), "health": "ambiguous-or-unsupported-profile", "managed": False}
    if blocks == 1:
        start = data.find(PROFILE_START)
        end = data.find(PROFILE_END, start) + len(PROFILE_END)
        block = data[start:end]
        if block != exact.rstrip(b"\n"):
            return {"path": str(path), "health": "partial-or-drift", "managed": False}
        return {"path": str(path), "health": "exact", "managed": True}
    return {"path": str(path), "health": "missing", "managed": False}


def _write_profile(path: Path, before: bytes, after: bytes) -> None:
    _validate_profile_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    _atomic_bytes(path, after, mode)


def _manage_profile(bin_dir: Path, policy: str) -> dict:
    if policy in {"deny", "manual"}:
        path = _profile_path()
        return {
            "path": str(path) if path else None,
            "health": "not-checked",
            "managed": False,
            "reason": "profile-authorization-required" if policy == "deny" else "manual-profile-source-required",
            "current_terminal": True,
        }
    state = _profile_state(bin_dir)
    if state["health"] == "exact":
        state.update({"managed": True, "current_terminal": False})
        return state
    if state["path"] is None or state["health"] not in {"missing"}:
        raise CodexLauncherError(state.get("health", "ambiguous-or-unsupported-profile"))
    path = Path(state["path"])
    before = path.read_bytes() if path.exists() else b""
    block = _profile_block(bin_dir)
    separator = b"" if not before or before.endswith(b"\n") else b"\n"
    _write_profile(path, before, before + separator + block)
    state = _profile_state(bin_dir)
    state.update({"managed": True, "current_terminal": False})
    return state


def _profile_restore_metadata(before: dict, *, owned: bool) -> dict:
    """Persist only the evidence needed to remove our block safely.

    Shell profiles can contain credentials, so launcher state must never copy
    their bytes.  A digest and length are enough to recognize the untouched
    prefix and remove the separator that Hearting inserted with its block.
    """
    if not owned:
        return {"schema": 1, "owned": False}
    kind = str(before.get("kind", "missing"))
    payload = before.get("payload", b"") if kind == "file" else b""
    if not isinstance(payload, bytes):
        raise CodexLauncherError("shell profile preimage is invalid")
    separator = b"" if not payload or payload.endswith(b"\n") else b"\n"
    return {
        "schema": 1,
        "owned": True,
        "before_kind": kind,
        "before_sha256": _digest(payload),
        "before_length": len(payload),
        "before_mode": int(before.get("mode", 0o600)),
        "separator_hex": separator.hex(),
    }


def _unmanage_profile(profile: dict, target: Path) -> dict:
    """Remove exactly one Hearting-owned profile block, preserving all else."""
    restore = profile.get("restore")
    if profile.get("policy") != "manage" or not isinstance(restore, dict) or not restore.get("owned"):
        return {"status": "not-owned"}
    raw_path = profile.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise CodexLauncherError("managed shell profile lacks its recorded path")
    path = Path(raw_path).expanduser().absolute()
    if not path.exists() and not path.is_symlink():
        return {"status": "already-absent", "path": str(path)}
    _validate_profile_path(path)
    data = path.read_bytes()
    starts = data.count(PROFILE_START)
    ends = data.count(PROFILE_END)
    if starts == 0 and ends == 0:
        return {"status": "already-absent", "path": str(path)}
    if starts != 1 or ends != 1:
        raise CodexLauncherError("managed shell profile block is ambiguous")
    start = data.find(PROFILE_START)
    marker_end = data.find(PROFILE_END, start) + len(PROFILE_END)
    if data[start:marker_end] != _profile_block(target.parent).rstrip(b"\n"):
        raise CodexLauncherError("managed shell profile block has drifted")
    removal_end = marker_end + (1 if data[marker_end:marker_end + 1] == b"\n" else 0)
    removal_start = start
    try:
        before_length = int(restore["before_length"])
        separator = bytes.fromhex(str(restore.get("separator_hex", "")))
    except (KeyError, TypeError, ValueError) as exc:
        raise CodexLauncherError("managed shell profile restore metadata is invalid") from exc
    prefix = data[:before_length]
    if (
        start == before_length + len(separator)
        and data[before_length:start] == separator
        and _digest(prefix) == restore.get("before_sha256")
    ):
        removal_start = before_length
    updated = data[:removal_start] + data[removal_end:]
    if not updated and restore.get("before_kind") == "missing":
        path.unlink()
    else:
        _atomic_bytes(path, updated, stat.S_IMODE(path.stat().st_mode))
    return {"status": "removed", "path": str(path)}


def _path_precedence(target: Path) -> str:
    entries = [Path(item).expanduser().absolute() for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    try:
        index = next(i for i, entry in enumerate(entries) if entry / "codex" == target.absolute())
    except StopIteration:
        return "missing"
    return "first" if index == 0 else "shadowed"


class _LauncherLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(self.path, flags, 0o600)
            except OSError as exc:
                raise CodexLauncherError(f"unsafe Codex launcher lock: {self.path}") from exc
            self.handle = os.fdopen(fd, "r+b", buffering=0)
            acquired = False
            valid = False
            try:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
                acquired = True
                opened = os.fstat(self.handle.fileno())
                try:
                    current = os.stat(self.path, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if current is not None and (
                    stat.S_ISREG(opened.st_mode)
                    and stat.S_ISREG(current.st_mode)
                    and opened.st_uid == os.geteuid()
                    and current.st_uid == os.geteuid()
                    and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
                ):
                    os.fchmod(self.handle.fileno(), 0o600)
                    valid = True
                    return self
                if current is not None and (
                    not stat.S_ISREG(opened.st_mode)
                    or not stat.S_ISREG(current.st_mode)
                    or opened.st_uid != os.geteuid()
                    or current.st_uid != os.geteuid()
                ):
                    raise CodexLauncherError(f"unsafe Codex launcher lock: {self.path}")
            finally:
                if self.handle is not None and not valid:
                    if fcntl is not None and acquired:
                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                    self.handle.close()
                    self.handle = None

    def __exit__(self, *_):
        if self.handle is not None:
            # Unlink only the inode held by this process and do so before
            # unlocking. A waiter that acquired an already-unlinked inode
            # fails the enter-side identity check and retries against the new
            # pathname, so cleanup never creates two valid lock domains.
            try:
                opened = os.fstat(self.handle.fileno())
                current = os.stat(self.path, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
                    os.unlink(self.path)
            except OSError:
                pass
            try:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def wrapper_bytes() -> bytes:
    return b"""#!/bin/sh
set -eu
codex_runtime_home=${CODEX_HOME:-$HOME/.codex}
launcher=$codex_runtime_home/hearting/utilities/codex-launcher.py
if [ ! -f "$launcher" ]; then
  launcher=$HOME/.codex/hearting/utilities/codex-launcher.py
fi
if [ ! -f "$launcher" ]; then
  echo "hearting: Codex launcher projection is missing from runtime and default homes" >&2
  exit 69
fi
exec python3 "$launcher" "$@"
"""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, payload: bytes, mode: int) -> None:
    if path.is_symlink():
        raise CodexLauncherError(f"refusing atomic write through symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path: Path, value: dict) -> None:
    payload = json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    _atomic_bytes(path, payload, 0o600)


def _load_state(path: Path) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32_768:
        raise CodexLauncherError(f"unsafe Codex launcher state: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexLauncherError(f"invalid Codex launcher state: {path}") from exc
    if not isinstance(value, dict) or value.get("schema") not in SUPPORTED_SCHEMAS:
        raise CodexLauncherError(f"unsupported Codex launcher state: {path}")
    return value


def _validate_home(codex_home: Path, *, create: bool) -> int:
    if not codex_home.is_absolute() or codex_home.is_symlink():
        raise CodexLauncherError(f"CODEX_HOME must be an absolute real directory: {codex_home}")
    if not codex_home.exists():
        if not create:
            return 0o700
        codex_home.mkdir(parents=True, exist_ok=True)
    info = codex_home.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise CodexLauncherError(f"CODEX_HOME is not owned by the current user: {codex_home}")
    return stat.S_IMODE(info.st_mode)


def _validate_state_directory(codex_home: Path, *, create: bool) -> None:
    directory = codex_home / ".harness"
    if directory.is_symlink():
        raise CodexLauncherError(f"Codex harness state directory must not be a symlink: {directory}")
    if not directory.exists():
        if not create:
            return
        directory.mkdir(mode=0o700)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise CodexLauncherError(f"Codex harness state is not owner-controlled: {directory}")


def _absolute_link_target(path: Path) -> Path:
    raw = Path(os.readlink(path))
    return raw if raw.is_absolute() else (path.parent / raw).absolute()


def is_harness_wrapper(command: Path) -> bool:
    """Detect any install's launcher ingress, not just this target path.

    A fresh HOME can inherit a PATH whose `codex` is another installation's
    wrapper; binding to it makes the launcher exec itself forever.
    """
    try:
        if command.is_symlink() or not command.is_file() or command.stat().st_size > 4096:
            return False
        payload = command.read_bytes()
    except OSError:
        return False
    return b"hearting" in payload and b"codex-launcher.py" in payload


def _validate_real_command(command: Path, target: Path) -> Path:
    command = command.expanduser().absolute()
    target = target.absolute()
    try:
        resolved = command.resolve(strict=False)
        protected_root = target.parent.resolve(strict=False)
    except OSError:
        resolved = command
        protected_root = target.parent
    if command == target or resolved == target or resolved.parent == protected_root:
        raise CodexLauncherError("real Codex command resolves to the launcher path")
    if not command.exists() or command.is_dir() or not os.access(command, os.X_OK):
        raise CodexLauncherError(f"real Codex command is unavailable: {command}")
    if is_harness_wrapper(command):
        raise CodexLauncherError(
            f"real Codex command resolves to an hearting launcher wrapper: {command}"
        )
    return command


def _discover_real_command_after_launcher(target: Path) -> Path | None:
    """Find a Codex executable later on PATH, excluding any harness ingress."""

    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_directory:
            continue
        candidate = (Path(raw_directory).expanduser() / "codex").absolute()
        if candidate == target.absolute():
            continue
        if not candidate.exists() or candidate.is_dir() or not os.access(candidate, os.X_OK):
            continue
        if is_harness_wrapper(candidate):
            continue
        return candidate
    return None


def _discover_initial_binding(target: Path, real_command: str | None) -> tuple[Path, dict]:
    discovered = Path(real_command).expanduser() if real_command else None
    if discovered is not None and is_harness_wrapper(discovered.absolute()):
        fallback = _discover_real_command_after_launcher(target)
        if fallback is None:
            raise CodexUnavailableError(
                "requested Codex command is a harness launcher wrapper and no real Codex command was found behind it"
            )
        discovered = fallback
    if discovered is None:
        resolved = shutil.which("codex")
        if not resolved:
            raise CodexUnavailableError("Codex command was not found on PATH")
        discovered = Path(resolved)
        if is_harness_wrapper(discovered.absolute()):
            fallback = _discover_real_command_after_launcher(target)
            if fallback is None:
                raise CodexUnavailableError(
                    "PATH resolves codex to another harness launcher wrapper and no "
                    "real Codex command was found behind it"
                )
            discovered = fallback

    if target.exists() or target.is_symlink():
        if not target.is_symlink():
            # A release interrupted after writing the deterministic ingress but
            # before persisting its state must be recoverable.  Adopt only our
            # byte-exact wrapper and preserve a usable Codex binding for
            # uninstall; every other regular file remains a hard collision.
            if not _wrapper_matches(target, _digest(wrapper_bytes())):
                raise CodexLauncherError(
                    f"refusing to replace a real file at the launcher path: {target}"
                )
            if discovered.absolute() == target.absolute():
                discovered = _discover_real_command_after_launcher(target)
            if discovered is None:
                raise CodexUnavailableError("real Codex command was not found after the launcher")
            real = discovered
            previous = {"kind": "symlink", "target": str(discovered.absolute())}
            return _validate_real_command(real, target), previous
        if discovered.absolute() != target.absolute():
            raise CodexLauncherError(
                f"foreign Codex symlink already occupies the launcher path: {target}"
            )
        raw_target = os.readlink(target)
        real = _absolute_link_target(target)
        previous = {"kind": "symlink", "target": raw_target}
    else:
        real = discovered
        previous = {"kind": "missing"}
    return _validate_real_command(real, target), previous


def _wrapper_matches(target: Path, expected_digest: str) -> bool:
    if target.is_symlink() or not target.is_file():
        return False
    try:
        return _digest(target.read_bytes()) == expected_digest
    except OSError:
        return False


def _current_binding(target: Path) -> dict:
    if not target.exists() and not target.is_symlink():
        return {"kind": "missing"}
    if target.is_symlink():
        return {"kind": "symlink", "target": os.readlink(target)}
    if target.is_file():
        return {
            "kind": "file",
            "payload": target.read_bytes(),
            "mode": stat.S_IMODE(target.stat().st_mode),
        }
    raise CodexLauncherError(f"Codex launcher drift would clobber a foreign path: {target}")


def _restore_wrapper(target: Path, previous: dict) -> None:
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            raise CodexLauncherError(f"launcher path became a directory: {target}")
        target.unlink()
    kind = previous.get("kind")
    if kind == "missing":
        return
    if kind == "file" and isinstance(previous.get("payload"), bytes):
        _atomic_bytes(target, previous["payload"], int(previous.get("mode", 0o755)))
        return
    if kind != "symlink" or not isinstance(previous.get("target"), str):
        raise CodexLauncherError("launcher state has an invalid previous binding")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".restore-tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(previous["target"])
    os.replace(temporary, target)


def _snapshot_path(path: Path) -> dict:
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.is_file():
        info = path.stat()
        return {"kind": "file", "payload": path.read_bytes(), "mode": stat.S_IMODE(info.st_mode)}
    if path.exists():
        return {"kind": "other"}
    return {"kind": "missing"}


def capture_snapshot(*, codex_home: Path | None = None, bin_dir: Path | None = None) -> dict:
    """Capture opaque launcher-owned preimages for installer rollback."""
    home = (codex_home or default_codex_home()).expanduser().absolute()
    target = wrapper_path((bin_dir or default_bin_dir()).expanduser().absolute())
    state = _load_state(state_path(home)) if state_path(home).exists() else None
    profile_path = _profile_path()
    vendor_path = None
    if isinstance(state, dict):
        recorded = state.get("real_command") or state.get("vendor_binding", {}).get("command_path")
        if isinstance(recorded, str) and recorded:
            vendor_path = Path(recorded)
    return {
        "schema": 1,
        "codex_home": str(home),
        "paths": {
            "state": _snapshot_path(state_path(home)),
            "ingress": _snapshot_path(target),
            "lock": _snapshot_path(lock_path(home)),
            # Keep the complete profile and binding preimages in the opaque
            # transaction snapshot.  These are deliberately not inferred at
            # restore time: an updater may have replaced the vendor path.
            "profile": {
                "path": str(profile_path) if profile_path else None,
                "entry": _snapshot_path(profile_path) if profile_path else {"kind": "unsupported"},
            },
            "vendor_binding": {
                "path": str(vendor_path) if vendor_path else None,
                "entry": _snapshot_path(vendor_path) if vendor_path else {"kind": "missing"},
            },
        },
    }


def restore_snapshot(snapshot: dict, *, codex_home: Path | None = None, bin_dir: Path | None = None, runtime_restore=None) -> None:
    """Restore only launcher preimages; runtime restoration is caller-owned."""
    home = (codex_home or Path(snapshot["codex_home"])).expanduser().absolute()
    target = wrapper_path((bin_dir or default_bin_dir()).expanduser().absolute())
    def restore_entry(path: Path, item: dict) -> None:
        if item.get("kind") == "unsupported":
            return
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                raise CodexLauncherError(f"cannot restore over directory: {path}")
            path.unlink()
        if item["kind"] == "file":
            _atomic_bytes(path, item["payload"], item["mode"])
        elif item["kind"] == "symlink":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(item["target"])

    with _LauncherLock(lock_path(home)):
        for path, item in ((state_path(home), snapshot["paths"]["state"]), (target, snapshot["paths"]["ingress"])):
            restore_entry(path, item)
        profile = snapshot["paths"].get("profile", {})
        if profile.get("path"):
            restore_entry(Path(profile["path"]), profile["entry"])
        # A vendor updater owns this path.  Only restore a transaction-owned
        # binding when it is still absent; a changed successor is preserved.
        vendor = snapshot["paths"].get("vendor_binding", {})
        if vendor.get("path") and vendor.get("entry", {}).get("kind") != "missing":
            vendor_path = Path(vendor["path"])
            if not vendor_path.exists() and not vendor_path.is_symlink():
                restore_entry(vendor_path, vendor["entry"])
    if runtime_restore is not None:
        runtime_restore()


def discard_snapshot(snapshot: dict) -> None:
    """Snapshots are in-memory and require no filesystem cleanup."""
    snapshot.clear()


def install(
    *,
    codex_home: Path | None = None,
    bin_dir: Path | None = None,
    real_command: str | None = None,
    dry_run: bool = False,
    profile_policy: str = "deny",
    allow_legacy_inplace: bool = False,
) -> dict:
    codex_home = (codex_home or default_codex_home()).expanduser().absolute()
    requested_bin = bin_dir
    bin_dir = (bin_dir or default_bin_dir()).expanduser().absolute()
    explicit_bin = requested_bin is not None or os.environ.get("HARNESS_BIN_DIR") is not None
    if not explicit_bin and allow_legacy_inplace:
        bin_dir = vendor_bin_dir()
    target = wrapper_path(bin_dir)
    mode = "legacy-inplace-v1" if target.parent == vendor_bin_dir().absolute() else "protected-path-v1"
    if mode == "legacy-inplace-v1" and not allow_legacy_inplace and not explicit_bin:
        raise CodexLauncherError("legacy-inplace-v1 requires explicit compatibility authorization")
    if profile_policy not in {"manage", "manual", "deny"}:
        raise CodexLauncherError("invalid profile policy")
    state_file = state_path(codex_home)
    payload = wrapper_bytes()
    payload_digest = _digest(payload)
    current_mode = _validate_home(codex_home, create=not dry_run)
    _validate_state_directory(codex_home, create=not dry_run)
    lock = nullcontext() if dry_run and not state_file.exists() else _LauncherLock(lock_path(codex_home))
    with lock:
        existing = _load_state(state_file)
        previous_mode = current_mode
        migration_successor = None
        migrated_legacy_target = None
        migrated_legacy_before = None
        if existing is not None:
            existing_target = Path(str(existing.get("wrapper_path", existing.get("ingress_path", ""))))
            if existing_target.absolute() != target.absolute():
                # Schema 1 placed Hearting's wrapper in the vendor directory.
                # Migration is the one supported target change: recover that
                # path under this lock, then persist schema 2 at the protected
                # ingress.  Never replay the schema-1 preimage over a vendor
                # successor that an updater has already installed.
                if existing.get("schema") != 1:
                    raise CodexLauncherError("installed Codex launcher uses a different bin directory")
                migrated_legacy_target = existing_target
                migrated_legacy_before = _current_binding(existing_target)
                old_digest = str(existing.get("wrapper_sha256", existing.get("ingress_sha256", "")))
                old_is_wrapper = _wrapper_matches(existing_target, old_digest) or _wrapper_matches(existing_target, _digest(wrapper_bytes()))
                schema1_preimage = existing.get("previous_wrapper", {"kind": "missing"})
                if old_is_wrapper:
                    candidate = schema1_preimage.get("target") if isinstance(schema1_preimage, dict) else None
                    candidate = candidate or existing.get("real_command")
                    if not isinstance(candidate, str) or not candidate:
                        raise CodexLauncherError("schema-1 migration lacks a vendor preimage")
                    real = _validate_real_command(Path(candidate), target)
                    _restore_wrapper(existing_target, schema1_preimage)
                    migration_successor = {
                        "kind": "preimage-restored",
                        "path": str(existing_target),
                        "command_path": str(real),
                        "resolved_path": str(real.resolve(strict=False)),
                    }
                else:
                    # The updater owns the successor now.  Validate it in
                    # place, record its raw/resolved identity, and leave its
                    # bytes and link target untouched.
                    if migrated_legacy_before.get("kind") == "missing":
                        raise CodexUnavailableError("schema-1 ingress disappeared before migration")
                    real = _validate_real_command(existing_target, target)
                    migration_successor = {
                        **migrated_legacy_before,
                        "path": str(existing_target),
                        "resolved_path": str(real.resolve(strict=False)),
                    }
                previous = {"kind": "missing"}
                rollback_wrapper = _current_binding(target)
            else:
                recorded_real = str(existing.get("real_command", existing.get("vendor_binding", {}).get("command_path", "")))
                try:
                    real = _validate_real_command(Path(recorded_real), target)
                except CodexLauncherError:
                    replacement = Path(real_command).expanduser() if real_command else _discover_real_command_after_launcher(target)
                    if replacement is None:
                        raise CodexUnavailableError("recorded Codex command disappeared and no replacement was found on PATH")
                    real = _validate_real_command(replacement, target)
                previous = existing.get("previous_wrapper", {"kind": "missing"})
                recorded_mode = existing.get("previous_codex_home_mode")
                if isinstance(recorded_mode, int):
                    previous_mode = recorded_mode
                if _wrapper_matches(target, payload_digest) and str(real) == recorded_real:
                    recorded_profile = existing.get("profile", {})
                    if mode != "protected-path-v1" or profile_policy != "manage":
                        return {"action": "managed-launcher", "status": "unchanged", "target": str(target), "real_command": str(real), "protected": mode == "protected-path-v1", "mode": mode}
                    current_profile = _profile_state(bin_dir)
                    if current_profile.get("health") == "exact" and isinstance(recorded_profile, dict) and recorded_profile.get("policy") == "manage":
                        return {"action": "managed-launcher", "status": "unchanged", "target": str(target), "real_command": str(real), "protected": True, "mode": mode, "profile": current_profile}
                recorded_digest = str(existing.get("wrapper_sha256", existing.get("ingress_sha256", "")))
                if target.is_file() and not target.is_symlink() and not _wrapper_matches(target, recorded_digest):
                    raise CodexLauncherError(f"Codex launcher drift would clobber a foreign file: {target}")
                rollback_wrapper = _current_binding(target)
        else:
            real, previous = _discover_initial_binding(target, real_command)
            rollback_wrapper = previous
        if dry_run:
            profile = ({"path": str(_profile_path()) if _profile_path() else None, "health": "not-checked"}
                       if mode == "protected-path-v1" else {"health": "not-applicable"})
            return {"action": "managed-launcher", "status": "planned", "target": str(target), "real_command": str(real), "protected": mode == "protected-path-v1", "mode": mode, "profile": profile}
        profile_before = None
        profile = {"health": "not-applicable"}
        if mode == "protected-path-v1":
            existing_profile = existing.get("profile", {}) if isinstance(existing, dict) else {}
            if profile_policy == "manage":
                profile_path = _profile_path()
                if profile_path is not None:
                    _validate_profile_path(profile_path)
                    profile_state_before = _profile_state(bin_dir)
                    profile_before = _snapshot_path(profile_path)
                else:
                    profile_state_before = {"health": "ambiguous-or-unsupported-profile"}
                profile = _manage_profile(bin_dir, profile_policy)
                profile["restore"] = _profile_restore_metadata(
                    profile_before or {"kind": "missing"},
                    owned=profile_state_before.get("health") == "missing",
                )
            elif isinstance(existing_profile, dict) and existing_profile.get("policy") == "manage":
                # A runtime refresh often uses the default deny policy.  It
                # may update wrapper bytes but must retain ownership evidence
                # for a profile block installed by an earlier explicit grant.
                profile = dict(existing_profile)
            else:
                profile = _manage_profile(bin_dir, profile_policy)
        transaction_id = f"{os.getpid()}-{time.time_ns()}"
        migration = {"from_schema": existing.get("schema") if existing else None, "status": "migrated" if existing and existing.get("schema") == 1 else "none", "transaction_id": transaction_id}
        if existing and existing.get("schema") == 1:
            migration["schema1_preimage"] = existing.get("previous_wrapper")
            migration["validated_successor"] = migration_successor or {"command_path": str(real), "resolved_path": str(real.resolve(strict=False))}
        prepared = {"schema": SCHEMA, "phase": "prepared", "wrapper_path": str(target), "wrapper_sha256": payload_digest, "real_command": str(real), "previous_wrapper": previous, "previous_codex_home_mode": previous_mode, "ingress_path": str(target), "ingress_sha256": payload_digest, "state_home": str(codex_home), "installed_at": time.time(), "updated_at": time.time(), "mode": mode, "profile": {"policy": profile_policy, **profile}, "migration": migration, "vendor_binding": {"command_path": str(real), "kind": "symlink" if real.is_symlink() else "file", "resolved_path": str(real.resolve(strict=False)), "observed_at": time.time()}}
        state_existed = existing is not None
        _atomic_json(state_file, prepared)
        try:
            os.chmod(codex_home, previous_mode & ~0o077)
            if target.exists() or target.is_symlink():
                target.unlink()
            _atomic_bytes(target, payload, 0o755)
            prepared["phase"] = "installed"
            _atomic_json(state_file, prepared)
        except Exception:
            _restore_wrapper(target, rollback_wrapper)
            if migrated_legacy_target is not None and migrated_legacy_before is not None:
                _restore_wrapper(migrated_legacy_target, migrated_legacy_before)
            if profile_before is not None:
                profile_path = Path(str(profile.get("path")))
                if profile_before["kind"] == "missing":
                    profile_path.unlink(missing_ok=True)
                elif profile_before["kind"] == "file":
                    _atomic_bytes(profile_path, profile_before["payload"], profile_before["mode"])
            os.chmod(codex_home, current_mode if state_existed else previous_mode)
            if state_existed:
                _atomic_json(state_file, existing)
            else:
                state_file.unlink(missing_ok=True)
            raise
    return {
        "action": "managed-launcher",
        "status": "created" if existing is None else "repaired",
        "target": str(target),
        "real_command": str(real),
        "protected": mode == "protected-path-v1",
        "mode": mode,
        "profile": profile,
    }


def uninstall(
    *,
    codex_home: Path | None = None,
    bin_dir: Path | None = None,
    dry_run: bool = False,
) -> dict:
    codex_home = (codex_home or default_codex_home()).expanduser().absolute()
    _validate_home(codex_home, create=False)
    _validate_state_directory(codex_home, create=False)
    state_file = state_path(codex_home)
    with _LauncherLock(lock_path(codex_home)):
        state = _load_state(state_file)
        if state is None:
            return {"action": "managed-launcher", "status": "not-installed"}
        target = Path(str(state.get("wrapper_path", "")))
        expected = str(state.get("wrapper_sha256", ""))
        # A protected install is self-describing.  In particular, schema-1
        # migration changes the ingress directory, so uninstall without an
        # explicit bin_dir must follow the recorded schema-2 target rather than
        # reconstructing the caller's current environment.
        requested_target = (
            Path(str(state.get("wrapper_path", ""))).expanduser().absolute()
            if bin_dir is None
            else wrapper_path(bin_dir.expanduser().absolute())
        )
        if target != requested_target:
            raise CodexLauncherError("installed Codex launcher uses a different bin directory")
        if target.exists() and not _wrapper_matches(target, expected):
            raise CodexLauncherError(f"refusing to overwrite modified Codex launcher: {target}")
        if dry_run:
            return {"action": "managed-launcher", "status": "planned-restore", "target": str(target)}
        previous = state.get("previous_wrapper")
        if not isinstance(previous, dict):
            raise CodexLauncherError("installed Codex launcher lacks restoration metadata")
        profile_result = _unmanage_profile(state.get("profile", {}), target)
        _restore_wrapper(target, previous)
        previous_mode = state.get("previous_codex_home_mode")
        if isinstance(previous_mode, int) and codex_home.is_dir() and not codex_home.is_symlink():
            os.chmod(codex_home, previous_mode)
        state_file.unlink(missing_ok=True)
        return {"action": "managed-launcher", "status": "restored", "target": str(target), "protected": state.get("mode") == "protected-path-v1", "profile": profile_result}


def status(*, codex_home: Path | None = None, bin_dir: Path | None = None) -> dict:
    codex_home = (codex_home or default_codex_home()).expanduser().absolute()
    if not codex_home.exists():
        return {"installed": False, "healthy": False, "detail": "not-installed", "protected": False, "path_precedence": "missing", "binding_state": "missing", "migration": {"status": "none"}, "next_action": "run runtime refresh", "reason": "not-installed"}
    _validate_home(codex_home, create=False)
    _validate_state_directory(codex_home, create=False)
    with _LauncherLock(lock_path(codex_home)):
      state = _load_state(state_path(codex_home))
    if state is None:
        return {"installed": False, "healthy": False, "detail": "not-installed", "protected": False, "path_precedence": "missing", "binding_state": "missing", "migration": {"status": "none"}, "next_action": "run runtime refresh", "reason": "not-installed"}
    target = wrapper_path((bin_dir or default_bin_dir()).expanduser().absolute())
    real = Path(str(state.get("real_command", "")))
    real_healthy = (
        real.is_absolute()
        and real != target
        and real.exists()
        and not real.is_dir()
        and os.access(real, os.X_OK)
        and not is_harness_wrapper(real)
    )
    healthy = (
        state.get("phase") == "installed"
        and state.get("wrapper_path", state.get("ingress_path")) == str(target)
        and _wrapper_matches(target, str(state.get("wrapper_sha256", "")))
        and real_healthy
        and (state.get("mode") != "protected-path-v1" or _path_precedence(target) == "first")
    )
    precedence = _path_precedence(target)
    profile = state.get("profile", {})
    return {
        "installed": True,
        "healthy": healthy,
        "detail": "ok" if healthy else ("real-command-unavailable" if not real_healthy else "path-precedence-or-profile-drift"),
        "target": str(target),
        "real_command": state.get("real_command"),
        "protected": state.get("mode") == "protected-path-v1",
        "mode": state.get("mode", "legacy-inplace-v1"),
        "path_precedence": precedence,
        "binding_state": "current" if real_healthy else "unavailable",
        "profile": profile,
        "migration": state.get("migration", {"status": "legacy"}),
        "next_action": None if healthy else "run runtime refresh",
        "reason": "ok" if healthy else ("real-command-unavailable" if not real_healthy else "path-precedence-or-profile-drift"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "uninstall", "status"))
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--bin-dir", type=Path)
    parser.add_argument("--real-command")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--profile-policy", choices=("manage", "manual", "deny"), default="manual")
    parser.add_argument("--allow-legacy-inplace", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.operation == "install":
            result = install(
                codex_home=args.codex_home,
                bin_dir=args.bin_dir,
                real_command=args.real_command,
                dry_run=args.dry_run,
                profile_policy=args.profile_policy,
                allow_legacy_inplace=args.allow_legacy_inplace,
            )
        elif args.operation == "uninstall":
            result = uninstall(
                codex_home=args.codex_home,
                bin_dir=args.bin_dir,
                dry_run=args.dry_run,
            )
        else:
            result = status(codex_home=args.codex_home, bin_dir=args.bin_dir)
    except CodexLauncherError as exc:
        if args.json:
            print(json.dumps({"status": "blocked", "error": str(exc)}))
        else:
            print(f"codex-launcher: blocked: {exc}", file=os.sys.stderr)
        return 3
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
