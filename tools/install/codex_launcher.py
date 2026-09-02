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

import safe_fs

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
    current_state = safe_fs.capture_state(path, include_payload=True)
    if current_state.kind != "file" or current_state.payload is None:
        raise CodexLauncherError("ownership-unproved: managed shell profile is not a file")
    data = current_state.payload
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
        try:
            auth = safe_fs.authority(
                path,
                owner="codex-launcher:managed-profile-block",
                allowed_paths=(path,),
                expected=current_state,
            )
            safe_fs.remove_exact(auth)
        except safe_fs.SafetyError as exc:
            raise CodexLauncherError(str(exc)) from exc
    else:
        _atomic_bytes(
            path,
            updated,
            int(current_state.mode or 0o600),
            expected=current_state,
        )
    return {"status": "removed", "path": str(path)}


def _path_precedence(target: Path) -> str:
    entries = [Path(item).expanduser().absolute() for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    target = target.absolute()
    try:
        index = next(i for i, entry in enumerate(entries) if entry / "codex" == target)
    except StopIteration:
        return "missing"
    for entry in entries[:index]:
        candidate = entry / "codex"
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return "shadowed"
        except OSError:
            continue
    return "first"


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
            # Keep the pathname and inode stable. Unlinking a lock file can
            # split waiters between an already-open inode and a newly-created
            # pathname. Crash/kill releases flock without pathname cleanup.
            try:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None

    # destructive-ok: reason=an uninstall endpoint must leave no harness-owned lock file; boundary=only the lock inode this process holds, proved by the fstat/stat identity match below
    def discard_pathname(self) -> None:
        """Unlink the held lock inode at an uninstall endpoint, before unlock.

        Ordinary lock release keeps the pathname stable (see __exit__), but an
        uninstall endpoint must leave nothing harness-owned behind. Unlinking
        while still holding the flock is safe with the enter-side identity
        check: a waiter that acquires the unlinked inode fails that check and
        retries against the (now absent) pathname.
        """
        if self.handle is None:
            return
        try:
            opened = os.fstat(self.handle.fileno())
            current = os.stat(self.path, follow_symlinks=False)
        except OSError:
            return
        if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
            try:
                os.unlink(self.path)
            except OSError:
                pass


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


def _atomic_bytes(
    path: Path,
    payload: bytes,
    mode: int,
    *,
    expected: safe_fs.PathState | None = None,
) -> safe_fs.PathState:
    """Replace one exact launcher-owned leaf without an unlink window."""

    path = path.expanduser().absolute()
    current = expected if expected is not None else safe_fs.capture_state(path)
    try:
        auth = safe_fs.authority(
            path,
            owner="codex-launcher:exact-leaf",
            allowed_paths=(path,),
            expected=current,
        )
        return safe_fs.atomic_write_bytes(
            auth,
            payload,
            mode,
            create_parents=True,
        )
    except safe_fs.SafetyError as exc:
        raise CodexLauncherError(str(exc)) from exc


def _atomic_json(path: Path, value: dict) -> safe_fs.PathState:
    payload = json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    return _atomic_bytes(path, payload, 0o600)


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


def _restore_wrapper(
    target: Path,
    previous: dict,
    *,
    expected: safe_fs.PathState | None = None,
) -> safe_fs.PathState:
    target = target.expanduser().absolute()
    current = expected if expected is not None else safe_fs.capture_state(target)
    if current.kind in {"directory", "other"}:
        raise CodexLauncherError(f"launcher path became an unsupported object: {target}")
    try:
        auth = safe_fs.authority(
            target,
            owner="codex-launcher:wrapper-restore",
            allowed_paths=(target,),
            expected=current,
        )
    except safe_fs.SafetyError as exc:
        raise CodexLauncherError(str(exc)) from exc
    kind = previous.get("kind")
    if kind == "missing":
        try:
            return safe_fs.remove_exact(auth)
        except safe_fs.SafetyError as exc:
            raise CodexLauncherError(str(exc)) from exc
    if kind == "file" and isinstance(previous.get("payload"), bytes):
        return _atomic_bytes(
            target,
            previous["payload"],
            int(previous.get("mode", 0o755)),
            expected=current,
        )
    if kind != "symlink" or not isinstance(previous.get("target"), str):
        raise CodexLauncherError("launcher state has an invalid previous binding")
    try:
        return safe_fs.atomic_write_symlink(
            auth,
            previous["target"],
            create_parents=True,
        )
    except safe_fs.SafetyError as exc:
        raise CodexLauncherError(str(exc)) from exc


def _snapshot_path(path: Path) -> dict:
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.is_file():
        info = path.stat()
        return {"kind": "file", "payload": path.read_bytes(), "mode": stat.S_IMODE(info.st_mode)}
    if path.exists():
        return {"kind": "other"}
    return {"kind": "missing"}


def capture_snapshot(
    *,
    codex_home: Path | None = None,
    bin_dir: Path | None = None,
    operation: str = "activation",
) -> dict:
    """Capture and lock only paths the declared operation may mutate.

    Activation/refresh never owns the shell profile or vendor command, so
    those paths are absent by construction. Full uninstall adds a profile only
    when launcher state proves that an explicit ``manage`` operation owns it.
    """

    if operation not in {"activation", "profile-install", "uninstall"}:
        raise CodexLauncherError(
            f"invalid-before-mutation: unsupported launcher snapshot operation: {operation}"
        )
    home = (codex_home or default_codex_home()).expanduser().absolute()
    target = wrapper_path((bin_dir or default_bin_dir()).expanduser().absolute())
    _validate_home(home, create=False)
    _validate_state_directory(home, create=False)
    state_file = state_path(home)
    state = _load_state(state_file) if state_file.exists() else None
    targets: dict[str, Path] = {"state": state_file, "ingress": target}
    if isinstance(state, dict) and state.get("schema") == 1:
        raw_legacy = state.get("wrapper_path", state.get("ingress_path"))
        if isinstance(raw_legacy, str) and raw_legacy:
            legacy = Path(raw_legacy).expanduser().absolute()
            if legacy != target:
                targets["legacy-ingress"] = legacy
    if operation == "uninstall" and isinstance(state, dict):
        profile = state.get("profile", {})
        if isinstance(profile, dict) and profile.get("policy") == "manage":
            raw_profile = profile.get("path")
            if not isinstance(raw_profile, str) or not raw_profile:
                raise CodexLauncherError(
                    "ownership-unproved: managed shell profile lacks its recorded path"
                )
            profile_path = Path(raw_profile).expanduser().absolute()
            _validate_profile_path(profile_path)
            targets["profile"] = profile_path
    if operation == "profile-install":
        profile_path = _profile_path()
        if profile_path is None:
            raise CodexLauncherError(
                "invalid-before-mutation: shell profile is unsupported for this shell"
            )
        profile_path = profile_path.expanduser().absolute()
        _validate_profile_path(profile_path)
        targets["profile"] = profile_path
    authorities = {
        name: safe_fs.authority(
            path,
            owner=f"codex-launcher:{operation}:{name}",
            allowed_paths=(path,),
        )
        for name, path in targets.items()
    }
    transaction = safe_fs.transaction(authorities)
    try:
        transaction.__enter__()
    except safe_fs.SafetyError as exc:
        raise CodexLauncherError(str(exc)) from exc
    return {
        "schema": 2,
        "operation": operation,
        "codex_home": str(home),
        "ingress": str(target),
        "paths": {
            name: {"path": str(targets[name]), "entry": entry.public()}
            for name, entry in transaction.preimages.items()
        },
        "_transaction": transaction,
    }


def seal_snapshot(snapshot: dict) -> dict:
    transaction = snapshot.get("_transaction")
    if not isinstance(transaction, safe_fs.Transaction):
        raise CodexLauncherError("ownership-unproved: launcher snapshot transaction is absent")
    try:
        postimages = transaction.seal()
    except safe_fs.SafetyError as exc:
        raise CodexLauncherError(str(exc)) from exc
    snapshot["postimages"] = {
        name: entry.public() for name, entry in postimages.items()
    }
    return snapshot["postimages"]


def restore_snapshot(
    snapshot: dict,
    *,
    codex_home: Path | None = None,
    bin_dir: Path | None = None,
    runtime_restore=None,
) -> None:
    """CAS-restore launcher preimages and preserve every unrelated successor."""

    expected_home = Path(snapshot["codex_home"]).expanduser().absolute()
    expected_target = Path(snapshot["ingress"]).expanduser().absolute()
    if codex_home is not None and codex_home.expanduser().absolute() != expected_home:
        raise CodexLauncherError("expected-state-mismatch: snapshot CODEX_HOME changed")
    if bin_dir is not None and wrapper_path(bin_dir.expanduser().absolute()) != expected_target:
        raise CodexLauncherError("expected-state-mismatch: snapshot ingress changed")
    transaction = snapshot.get("_transaction")
    if not isinstance(transaction, safe_fs.Transaction):
        raise CodexLauncherError("ownership-unproved: launcher snapshot transaction is absent")
    try:
        transaction.restore()
    except safe_fs.SafetyError as exc:
        raise CodexLauncherError(str(exc)) from exc
    finally:
        transaction.close()
    if runtime_restore is not None:
        runtime_restore()


def discard_snapshot(snapshot: dict) -> None:
    """Snapshots are in-memory and require no filesystem cleanup."""
    transaction = snapshot.get("_transaction")
    if isinstance(transaction, safe_fs.Transaction):
        transaction.close()
    snapshot.clear()


def _install_impl(
    *,
    codex_home: Path | None = None,
    bin_dir: Path | None = None,
    real_command: str | None = None,
    dry_run: bool = False,
    profile_policy: str = "deny",
    allow_legacy_inplace: bool = False,
    transaction_snapshot: dict | None = None,
) -> dict:
    if (
        transaction_snapshot is not None
        and profile_policy == "manage"
        and transaction_snapshot.get("operation") != "profile-install"
    ):
        raise CodexLauncherError(
            "invalid-before-mutation: managed profile install requires a profile-scoped snapshot"
        )
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
        raise CodexLauncherError("invalid-before-mutation: invalid profile policy")
    profile_candidate = None
    if mode == "protected-path-v1" and profile_policy == "manage":
        profile_candidate = _profile_path()
        if profile_candidate is None:
            raise CodexLauncherError(
                "invalid-before-mutation: shell profile is unsupported for this shell"
            )
        _validate_profile_path(profile_candidate)
    state_file = state_path(codex_home)
    mutation_targets = [state_file, target]
    if mode == "protected-path-v1":
        mutation_targets.append(vendor_bin_dir() / "codex")
    if profile_candidate is not None:
        mutation_targets.append(profile_candidate)
    # This is deliberately before `_validate_home(..., create=True)`: fixture
    # escapes, symlink parents, HOME/root targets, and malformed ambient path
    # selectors must fail without even creating a state directory or lock.
    try:
        for mutation_target in mutation_targets:
            safe_fs.authority(
                mutation_target,
                owner="codex-launcher:request-preflight",
                allowed_paths=(mutation_target,),
            )
    except safe_fs.SafetyError as exc:
        raise CodexLauncherError(f"invalid-before-mutation: {exc}") from exc
    payload = wrapper_bytes()
    payload_digest = _digest(payload)
    current_mode = _validate_home(codex_home, create=not dry_run)
    _validate_state_directory(codex_home, create=not dry_run)
    lock = nullcontext() if dry_run and not state_file.exists() else _LauncherLock(lock_path(codex_home))
    target_locks = (
        nullcontext()
        if transaction_snapshot is not None
        else safe_fs.TargetLocks(mutation_targets)
    )

    def finish(result: dict) -> dict:
        if transaction_snapshot is not None:
            seal_snapshot(transaction_snapshot)
        return result

    with lock, target_locks:
        existing = _load_state(state_file)
        previous_mode = current_mode
        migration_successor = None
        migrated_legacy_target = None
        migrated_legacy_before = None
        migrated_legacy_pre_state = None
        migrated_legacy_post_state = None
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
                migrated_legacy_pre_state = safe_fs.capture_state(
                    existing_target, include_payload=True
                )
                old_digest = str(existing.get("wrapper_sha256", existing.get("ingress_sha256", "")))
                old_is_wrapper = _wrapper_matches(existing_target, old_digest) or _wrapper_matches(existing_target, _digest(wrapper_bytes()))
                schema1_preimage = existing.get("previous_wrapper", {"kind": "missing"})
                if old_is_wrapper:
                    candidate = schema1_preimage.get("target") if isinstance(schema1_preimage, dict) else None
                    candidate = candidate or existing.get("real_command")
                    if not isinstance(candidate, str) or not candidate:
                        raise CodexLauncherError("schema-1 migration lacks a vendor preimage")
                    real = _validate_real_command(Path(candidate), target)
                    migrated_legacy_post_state = _restore_wrapper(
                        existing_target,
                        schema1_preimage,
                        expected=migrated_legacy_pre_state,
                    )
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
                        return finish({"action": "managed-launcher", "status": "unchanged", "target": str(target), "real_command": str(real), "protected": mode == "protected-path-v1", "mode": mode})
                    current_profile = _profile_state(bin_dir)
                    if current_profile.get("health") == "exact" and isinstance(recorded_profile, dict) and recorded_profile.get("policy") == "manage":
                        return finish({"action": "managed-launcher", "status": "unchanged", "target": str(target), "real_command": str(real), "protected": True, "mode": mode, "profile": current_profile})
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
            return finish({"action": "managed-launcher", "status": "planned", "target": str(target), "real_command": str(real), "protected": mode == "protected-path-v1", "mode": mode, "profile": profile})
        profile_before = None
        profile_pre_state = None
        profile_post_state = None
        profile = {"health": "not-applicable"}
        if mode == "protected-path-v1":
            existing_profile = existing.get("profile", {}) if isinstance(existing, dict) else {}
            if profile_policy == "manage":
                profile_path = _profile_path()
                if profile_path is not None:
                    _validate_profile_path(profile_path)
                    profile_state_before = _profile_state(bin_dir)
                    profile_before = _snapshot_path(profile_path)
                    profile_pre_state = safe_fs.capture_state(
                        profile_path, include_payload=True
                    )
                else:
                    profile_state_before = {"health": "ambiguous-or-unsupported-profile"}
                profile = _manage_profile(bin_dir, profile_policy)
                profile_post_state = safe_fs.capture_state(profile_path)
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
        state_pre_state = safe_fs.capture_state(state_file, include_payload=True)
        state_post_state = None
        target_pre_state = safe_fs.capture_state(target, include_payload=True)
        target_post_state = None
        try:
            state_post_state = _atomic_json(state_file, prepared)
            os.chmod(codex_home, previous_mode & ~0o077)
            target_post_state = _atomic_bytes(
                target, payload, 0o755, expected=target_pre_state
            )
            prepared["phase"] = "installed"
            state_post_state = _atomic_json(state_file, prepared)
        except Exception:
            if target_post_state is not None:
                try:
                    auth = safe_fs.authority(
                        target,
                        owner="codex-launcher:install-rollback",
                        allowed_paths=(target,),
                    )
                    safe_fs.cas_restore(auth, target_pre_state, target_post_state)
                except safe_fs.SafetyError as rollback_exc:
                    raise CodexLauncherError(str(rollback_exc)) from rollback_exc
            if (
                migrated_legacy_target is not None
                and migrated_legacy_pre_state is not None
                and migrated_legacy_post_state is not None
            ):
                try:
                    auth = safe_fs.authority(
                        migrated_legacy_target,
                        owner="codex-launcher:migration-rollback",
                        allowed_paths=(migrated_legacy_target,),
                    )
                    safe_fs.cas_restore(
                        auth, migrated_legacy_pre_state, migrated_legacy_post_state
                    )
                except safe_fs.SafetyError as rollback_exc:
                    raise CodexLauncherError(str(rollback_exc)) from rollback_exc
            if profile_pre_state is not None and profile_post_state is not None:
                try:
                    profile_path = Path(str(profile.get("path")))
                    auth = safe_fs.authority(
                        profile_path,
                        owner="codex-launcher:profile-rollback",
                        allowed_paths=(profile_path,),
                    )
                    safe_fs.cas_restore(auth, profile_pre_state, profile_post_state)
                except safe_fs.SafetyError as rollback_exc:
                    raise CodexLauncherError(str(rollback_exc)) from rollback_exc
            os.chmod(codex_home, current_mode if state_existed else previous_mode)
            if state_post_state is not None:
                try:
                    auth = safe_fs.authority(
                        state_file,
                        owner="codex-launcher:state-rollback",
                        allowed_paths=(state_file,),
                    )
                    safe_fs.cas_restore(auth, state_pre_state, state_post_state)
                except safe_fs.SafetyError as rollback_exc:
                    raise CodexLauncherError(str(rollback_exc)) from rollback_exc
            if transaction_snapshot is not None:
                seal_snapshot(transaction_snapshot)
            raise
    return finish({
        "action": "managed-launcher",
        "status": "created" if existing is None else "repaired",
        "target": str(target),
        "real_command": str(real),
        "protected": mode == "protected-path-v1",
        "mode": mode,
        "profile": profile,
    })


def install(
    *,
    codex_home: Path | None = None,
    bin_dir: Path | None = None,
    real_command: str | None = None,
    dry_run: bool = False,
    profile_policy: str = "deny",
    allow_legacy_inplace: bool = False,
    transaction_snapshot: dict | None = None,
) -> dict:
    """Install with an outer profile transaction when explicit management is granted."""

    if dry_run or transaction_snapshot is not None or profile_policy != "manage":
        return _install_impl(
            codex_home=codex_home,
            bin_dir=bin_dir,
            real_command=real_command,
            dry_run=dry_run,
            profile_policy=profile_policy,
            allow_legacy_inplace=allow_legacy_inplace,
            transaction_snapshot=transaction_snapshot,
        )
    snapshot = capture_snapshot(
        codex_home=codex_home,
        bin_dir=bin_dir,
        operation="profile-install",
    )
    try:
        result = _install_impl(
            codex_home=codex_home,
            bin_dir=bin_dir,
            real_command=real_command,
            dry_run=False,
            profile_policy=profile_policy,
            allow_legacy_inplace=allow_legacy_inplace,
            transaction_snapshot=snapshot,
        )
    except Exception:
        if "postimages" not in snapshot:
            seal_snapshot(snapshot)
        restore_snapshot(snapshot)
        raise
    discard_snapshot(snapshot)
    return result


def _uninstall_impl(
    *,
    codex_home: Path | None = None,
    bin_dir: Path | None = None,
    dry_run: bool = False,
    transaction_snapshot: dict | None = None,
) -> dict:
    codex_home = (codex_home or default_codex_home()).expanduser().absolute()
    _validate_home(codex_home, create=False)
    _validate_state_directory(codex_home, create=False)
    state_file = state_path(codex_home)

    def finish(result: dict) -> dict:
        if transaction_snapshot is not None:
            seal_snapshot(transaction_snapshot)
        return result

    with _LauncherLock(lock_path(codex_home)) as launcher_lock:
        state = _load_state(state_file)
        if state is None:
            launcher_lock.discard_pathname()
            return finish({"action": "managed-launcher", "status": "not-installed"})
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
            return finish({"action": "managed-launcher", "status": "planned-restore", "target": str(target)})
        previous = state.get("previous_wrapper")
        if not isinstance(previous, dict):
            raise CodexLauncherError("installed Codex launcher lacks restoration metadata")
        profile = state.get("profile", {})
        profile_path = None
        if isinstance(profile, dict) and profile.get("policy") == "manage":
            raw_profile = profile.get("path")
            if not isinstance(raw_profile, str) or not raw_profile:
                raise CodexLauncherError(
                    "ownership-unproved: managed shell profile lacks its recorded path"
                )
            profile_path = Path(raw_profile).expanduser().absolute()
            _validate_profile_path(profile_path)
        target_locks = (
            nullcontext()
            if transaction_snapshot is not None
            else safe_fs.TargetLocks(
                [state_file, target] + ([profile_path] if profile_path else [])
            )
        )
        with target_locks:
            profile_result = _unmanage_profile(profile, target)
            target_state = safe_fs.capture_state(target)
            _restore_wrapper(target, previous, expected=target_state)
            previous_mode = state.get("previous_codex_home_mode")
            if isinstance(previous_mode, int) and codex_home.is_dir() and not codex_home.is_symlink():
                os.chmod(codex_home, previous_mode)
            state_state = safe_fs.capture_state(state_file)
            try:
                auth = safe_fs.authority(
                    state_file,
                    owner="codex-launcher:installed-state",
                    allowed_paths=(state_file,),
                    expected=state_state,
                )
                safe_fs.remove_exact(auth)
            except safe_fs.SafetyError as exc:
                raise CodexLauncherError(str(exc)) from exc
        launcher_lock.discard_pathname()
        return finish({"action": "managed-launcher", "status": "restored", "target": str(target), "protected": state.get("mode") == "protected-path-v1", "profile": profile_result})


def uninstall(
    *,
    codex_home: Path | None = None,
    bin_dir: Path | None = None,
    dry_run: bool = False,
    transaction_snapshot: dict | None = None,
) -> dict:
    """Uninstall atomically when no invocation-level transaction was supplied."""

    if dry_run or transaction_snapshot is not None:
        return _uninstall_impl(
            codex_home=codex_home,
            bin_dir=bin_dir,
            dry_run=dry_run,
            transaction_snapshot=transaction_snapshot,
        )
    snapshot = capture_snapshot(
        codex_home=codex_home,
        bin_dir=bin_dir,
        operation="uninstall",
    )
    try:
        result = _uninstall_impl(
            codex_home=codex_home,
            bin_dir=bin_dir,
            dry_run=False,
            transaction_snapshot=snapshot,
        )
    except Exception:
        if "postimages" not in snapshot:
            seal_snapshot(snapshot)
        restore_snapshot(snapshot)
        raise
    discard_snapshot(snapshot)
    return result


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
