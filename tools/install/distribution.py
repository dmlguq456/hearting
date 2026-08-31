#!/usr/bin/env python3
"""Managed Hearting release installation and automatic updates.

This module is standalone and Python-stdlib-only so the release builder can
embed it in the same-tag install.sh asset before a harness root exists. Runtime
activation is delegated to the verified release after extraction.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import Iterable, NamedTuple, Optional


DEFAULT_REPOSITORY = "dmlguq456/hearting"
ARCHIVE_NAME = "hearting.tar.gz"
CHECKSUM_NAME = ARCHIVE_NAME + ".sha256"
STATE_SCHEMA = 1
RUNTIMES = ("claude", "codex", "opencode")
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_CHECKSUM_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 20_000
REQUIRED_RELEASE_FILES = (
    "RELEASE_VERSION",
    "harness-manifest.json",
    "core/CORE.md",
    "tools/install/harness.sh",
    "tools/install/installer.py",
    "tools/install/distribution.py",
    "utilities/compute-hosts",
    "utilities/compute-hosts.py",
    "tools/fleet/fleet.sh",
    "tools/memory/mem.py",
    "tools/memory/protocol_v2.py",
    "tools/memory/git_exchange_v2.py",
    "tools/memory/sync_v2.py",
    "tools/memory/migration_v2.py",
)
TOOL_LAUNCHERS = (
    ("fleet", "tools/fleet/fleet.sh"),
    ("mem", "tools/memory/mem.py"),
    ("compute-hosts", "utilities/compute-hosts"),
)
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(
    rf"^([0-9a-fA-F]{{64}})[ \t]+[*]?{re.escape(ARCHIVE_NAME)}[ \t]*$"
)


class DistributionError(RuntimeError):
    """Safe user-facing distribution failure."""


class _LeafState(NamedTuple):
    """Exact public identity for a standalone-installer mutation target."""

    kind: str
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    size: int | None = None
    digest: str | None = None
    target: str | None = None


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    path = Path(value).expanduser() if value else default.expanduser()
    if not path.is_absolute():
        raise DistributionError(f"{name} must be an absolute path: {path}")
    return path


def _home() -> Path:
    return _env_path("HOME", Path.home())


def _xdg_data_home() -> Path:
    return _env_path("XDG_DATA_HOME", _home() / ".local/share")


def _xdg_state_home() -> Path:
    return _env_path("XDG_STATE_HOME", _home() / ".local/state")


def _xdg_config_home() -> Path:
    return _env_path("XDG_CONFIG_HOME", _home() / ".config")


def data_root() -> Path:
    return _env_path("HARNESS_DATA_ROOT", _xdg_data_home() / "hearting")


def state_root() -> Path:
    return _env_path("HARNESS_STATE_ROOT", _xdg_state_home() / "hearting")


def state_path() -> Path:
    return state_root() / "distribution.json"


def bin_dir() -> Path:
    return _env_path("HARNESS_BIN_DIR", _home() / ".local/bin")


def launcher_path() -> Path:
    return bin_dir() / "hearting"


def legacy_launcher_path() -> Path:
    """`harness` predates the rename to Hearting and stays installed beside it.

    Existing shells, scripts, and scheduler units all reach for `harness`;
    dropping it would break them for no gain, so both names point at the same
    launcher and removing either is a user decision, not an install step.
    """
    return bin_dir() / "harness"


def current_path() -> Path:
    return data_root() / "current"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _canonical_leaf(path: Path) -> Path:
    if not path.is_absolute():
        raise DistributionError(f"mutation target must be absolute: {path}")
    leaf = Path(os.path.abspath(os.fspath(path)))
    current = Path(leaf.anchor)
    for index, part in enumerate(leaf.parts[1:], start=1):
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) and index != len(leaf.parts) - 1:
            raise DistributionError(f"mutation target parent is a symlink: {current}")
        if index != len(leaf.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise DistributionError(f"mutation target parent is not a directory: {current}")
    protected = {Path(leaf.anchor), _home(), *Path(leaf.anchor).parents, *_home().parents}
    if leaf in protected:
        raise DistributionError(f"refusing protected mutation target: {leaf}")
    fixture_raw = os.environ.get("HEARTING_FIXTURE_ROOT")
    if fixture_raw:
        fixture = Path(os.path.abspath(os.fspath(Path(fixture_raw).expanduser())))
        try:
            leaf.relative_to(fixture)
        except ValueError as exc:
            raise DistributionError(
                f"target-outside-fixture: {leaf}: fixture_root={fixture}"
            ) from exc
    return leaf


def _capture_leaf(path: Path) -> _LeafState:
    leaf = _canonical_leaf(path)
    try:
        before = os.lstat(leaf)
    except FileNotFoundError:
        return _LeafState("missing")
    common = {
        "device": before.st_dev,
        "inode": before.st_ino,
        "mode": stat.S_IMODE(before.st_mode),
        "size": before.st_size,
    }
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(leaf)
        after = os.lstat(leaf)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise DistributionError(f"expected-state-mismatch: {leaf}")
        return _LeafState("symlink", target=target, **common)
    if stat.S_ISREG(before.st_mode):
        payload = leaf.read_bytes()
        after = os.lstat(leaf)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise DistributionError(f"expected-state-mismatch: {leaf}")
        return _LeafState(
            "file", digest=hashlib.sha256(payload).hexdigest(), **common
        )
    if stat.S_ISDIR(before.st_mode):
        return _LeafState("directory", **common)
    return _LeafState("other", **common)


def _standalone_lock_root() -> Path:
    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    base = (
        Path("/tmp") if os.name == "posix" else Path(tempfile.gettempdir())
    ) / f"hearting-path-locks-{uid}"
    if not base.exists():
        try:
            os.mkdir(base, 0o700)
        except FileExistsError:
            pass
    info = os.lstat(base)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DistributionError(f"unsafe target-lock root: {base}")
    if hasattr(os, "geteuid") and (
        info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DistributionError(f"target-lock root is not owner-only: {base}")
    return base


@contextlib.contextmanager
def _target_lock(path: Path):
    leaf = _canonical_leaf(path)
    key = hashlib.sha256(os.fsencode(os.fspath(leaf))).hexdigest()
    lock = _standalone_lock_root() / f"{key}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock, flags, 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        opened = os.fstat(handle.fileno())
        current = os.lstat(lock)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (
                hasattr(os, "geteuid")
                and (opened.st_uid != os.geteuid() or current.st_uid != os.geteuid())
            )
        ):
            raise DistributionError(f"unsafe target-lock identity: {lock}")
        os.fchmod(handle.fileno(), 0o600)
        yield
    finally:
        try:
            if "fcntl" in sys.modules:
                sys.modules["fcntl"].flock(handle.fileno(), sys.modules["fcntl"].LOCK_UN)
        finally:
            handle.close()


def _assert_leaf(path: Path, expected: _LeafState) -> None:
    current = _capture_leaf(path)
    if current != expected:
        raise DistributionError(
            f"expected-state-mismatch: {path}: expected={expected} current={current}"
        )


def _fsync_parent(path: Path) -> None:
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


# destructive-ok: reason=commit CAS-validated bytes and discard the helper-created sibling temp; boundary=one exact caller target and one mkstemp sibling
def _atomic_bytes(
    path: Path,
    payload: bytes,
    mode: int = 0o600,
    *,
    expected: _LeafState | None = None,
) -> _LeafState:
    leaf = _canonical_leaf(path)
    with _target_lock(leaf):
        wanted = _capture_leaf(leaf) if expected is None else expected
        _assert_leaf(leaf, wanted)
        if wanted.kind not in {"missing", "file"}:
            raise DistributionError(f"refusing non-file state target: {leaf}")
        leaf.parent.mkdir(parents=True, exist_ok=True)
        _canonical_leaf(leaf)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{leaf.name}.hearting-", dir=leaf.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _assert_leaf(leaf, wanted)
            os.replace(tmp_name, leaf)
            _fsync_parent(leaf)
            return _capture_leaf(leaf)
        finally:
            if fd >= 0:
                os.close(fd)
            # STANDALONE_SAFE_FS_INTERNAL: exact mkstemp sibling owned by this call.
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_json(
    path: Path, value: dict, *, expected: _LeafState | None = None
) -> _LeafState:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n"
    return _atomic_bytes(path, payload, expected=expected)


def _load_state() -> Optional[dict]:
    path = state_path()
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise DistributionError(f"invalid distribution state path: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DistributionError(f"invalid distribution state: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise DistributionError(f"unsupported distribution state schema: {path}")
    required_strings = (
        "repository",
        "version",
        "archive_sha256",
        "release_root",
    )
    if any(not isinstance(value.get(name), str) for name in required_strings):
        raise DistributionError(f"distribution state lacks required string fields: {path}")
    if not _REPOSITORY_RE.fullmatch(value["repository"]):
        raise DistributionError(f"distribution state has an invalid repository: {path}")
    if not _VERSION_RE.fullmatch(value["version"]):
        raise DistributionError(f"distribution state has an invalid version: {path}")
    if not re.fullmatch(r"[0-9a-f]{64}", value["archive_sha256"]):
        raise DistributionError(f"distribution state has an invalid checksum: {path}")
    if not Path(value["release_root"]).is_absolute():
        raise DistributionError(f"distribution state release_root must be absolute: {path}")
    runtimes = value.get("runtimes")
    if (
        not isinstance(runtimes, list)
        or not runtimes
        or any(runtime not in RUNTIMES for runtime in runtimes)
    ):
        raise DistributionError(f"distribution state has invalid runtimes: {path}")
    channel = value.get("channel", "stable")
    if channel not in {"stable", "pinned"}:
        raise DistributionError(f"distribution state has an invalid channel: {path}")
    if channel == "pinned" and not _VERSION_RE.fullmatch(
        value.get("pinned_version", "")
    ):
        raise DistributionError(f"distribution state has an invalid pin: {path}")
    return value


def is_managed() -> bool:
    """Return whether this user has a valid managed distribution state."""
    state = _load_state()
    return bool(state and state.get("release_root") and state.get("version"))


def managed_status() -> Optional[dict]:
    """Return the validated managed-release identity for status surfaces.

    Runtime projection manifests can predate a managed release activation and
    therefore cannot identify the installed distribution version. Keep this
    public view small and derive it from the same validated state that owns
    install and update decisions. (Restored after ecaeedfb dropped it while
    installer.py's status surface still calls it.)
    """

    state = _load_state()
    if state is None:
        return None
    return {
        "channel": "managed-release",
        "version": state["version"],
        "release_root": state["release_root"],
        "runtimes": list(state["runtimes"]),
        "pinned_version": state.get("pinned_version"),
    }


@contextlib.contextmanager
def _distribution_lock():
    root = state_root()
    if root.exists() or root.is_symlink():
        if root.is_symlink() or not root.is_dir():
            raise DistributionError(f"distribution state root must be a directory: {root}")
    lock = root / "distribution.lock"
    if lock.is_symlink():
        raise DistributionError(f"distribution lock must not be a symlink: {lock}")
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a+b")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            try:
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except ImportError as exc:
                raise DistributionError("no supported file-lock implementation") from exc
        yield
    finally:
        try:
            if "fcntl" in sys.modules:
                sys.modules["fcntl"].flock(handle.fileno(), sys.modules["fcntl"].LOCK_UN)
        finally:
            handle.close()


def _validate_repository(repository: str) -> str:
    if not _REPOSITORY_RE.fullmatch(repository):
        raise DistributionError(f"invalid GitHub repository: {repository!r}")
    return repository


def _validate_version(version: str) -> str:
    if not _VERSION_RE.fullmatch(version):
        raise DistributionError(
            "release tag must use only letters, digits, dot, underscore, and dash"
        )
    return version


def _allow_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "file" and os.environ.get("HARNESS_ALLOW_FILE_RELEASES") == "1":
        return
    raise DistributionError(f"release URL must use HTTPS: {url}")


def _request_headers(url: str) -> dict[str, str]:
    """Build release headers without leaking GitHub credentials to overrides."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "hearting-installer/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if urllib.parse.urlparse(url).hostname != "api.github.com":
        return headers
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name)
        if token is None or token == "":
            continue
        if token != token.strip() or any(ord(char) < 32 for char in token):
            raise DistributionError(f"{name} contains invalid whitespace or control characters")
        headers["Authorization"] = "Bearer " + token
        break
    return headers


def _read_url(url: str, limit: int) -> bytes:
    _allow_url(url)
    request = urllib.request.Request(url, headers=_request_headers(url))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            _allow_url(response.geturl())
            payload = response.read(limit + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise DistributionError(f"release request failed: {url}: {exc}") from exc
    if len(payload) > limit:
        raise DistributionError(f"release response exceeds size limit: {url}")
    return payload


def _release_api_url(repository: str, version: str) -> str:
    override = os.environ.get("HARNESS_RELEASE_INDEX_URL")
    if override:
        return override.replace("{version}", urllib.parse.quote(version, safe=""))
    repository = _validate_repository(repository)
    if version == "latest":
        return f"https://api.github.com/repos/{repository}/releases/latest"
    return (
        f"https://api.github.com/repos/{repository}/releases/tags/"
        + urllib.parse.quote(_validate_version(version), safe="")
    )


def _release_metadata(repository: str, version: str) -> dict:
    url = _release_api_url(repository, version)
    try:
        value = json.loads(_read_url(url, MAX_METADATA_BYTES))
    except json.JSONDecodeError as exc:
        raise DistributionError(f"invalid release metadata: {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise DistributionError(f"release metadata must be an object: {url}")
    tag = value.get("tag_name")
    if not isinstance(tag, str):
        raise DistributionError("release metadata lacks tag_name")
    tag = _validate_version(tag)
    if version != "latest" and tag != version:
        raise DistributionError(
            f"release metadata tag mismatch: requested={version} returned={tag}"
        )
    assets = value.get("assets")
    if not isinstance(assets, list):
        raise DistributionError("release metadata lacks assets")
    selected = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if name not in {ARCHIVE_NAME, CHECKSUM_NAME}:
            continue
        if name in selected:
            raise DistributionError(f"release has duplicate asset: {name}")
        download_url = asset.get("browser_download_url")
        if not isinstance(download_url, str):
            raise DistributionError(f"release asset lacks download URL: {name}")
        _allow_url(download_url)
        selected[name] = download_url
    missing = sorted({ARCHIVE_NAME, CHECKSUM_NAME} - set(selected))
    if missing:
        raise DistributionError("release is missing asset(s): " + ", ".join(missing))
    return {"version": tag, "assets": selected, "metadata_url": url}


def _expected_checksum(url: str) -> str:
    try:
        text = _read_url(url, MAX_CHECKSUM_BYTES).decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise DistributionError(f"{CHECKSUM_NAME} must be ASCII") from exc
    matches = []
    for line in text.splitlines():
        match = _SHA256_RE.fullmatch(line)
        if match:
            matches.append(match.group(1).lower())
    if len(matches) != 1:
        raise DistributionError(
            f"{CHECKSUM_NAME} must contain exactly one SHA-256 entry for {ARCHIVE_NAME}"
        )
    return matches[0]


# destructive-ok: reason=discard failed or mismatched release download; boundary=one mkstemp download owned by this call
def _download_archive(url: str, destination: Path, expected: str) -> None:
    _allow_url(url)
    request = urllib.request.Request(
        url, headers={"User-Agent": "hearting-installer/1"}
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            _allow_url(response.geturl())
            with destination.open("xb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise DistributionError("release archive exceeds size limit")
                    digest.update(chunk)
                    handle.write(chunk)
    except (OSError, urllib.error.URLError) as exc:
        destination.unlink(missing_ok=True)
        raise DistributionError(f"release download failed: {url}: {exc}") from exc
    actual = digest.hexdigest()
    if actual != expected:
        destination.unlink(missing_ok=True)
        raise DistributionError(
            f"release checksum mismatch: expected={expected} actual={actual}"
        )


def _normal_member_path(name: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise DistributionError(f"invalid archive member path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.parts[0] != "hearting":
        raise DistributionError(f"archive member escapes release root: {name}")
    return path


def _normal_link_target(member: tarfile.TarInfo, path: PurePosixPath) -> None:
    target = member.linkname
    if not target or "\\" in target:
        raise DistributionError(f"invalid archive link target: {member.name} -> {target}")
    pure = PurePosixPath(target)
    if pure.is_absolute():
        raise DistributionError(f"archive link is absolute: {member.name} -> {target}")
    combined = path.parent.joinpath(pure) if member.issym() else pure
    normalized = []
    for part in combined.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not normalized:
                raise DistributionError(
                    f"archive link escapes release root: {member.name} -> {target}"
                )
            normalized.pop()
        else:
            normalized.append(part)
    if not normalized or normalized[0] != "hearting":
        raise DistributionError(
            f"archive link escapes release root: {member.name} -> {target}"
        )


def _safe_extract(archive: Path, extraction_root: Path, version: str) -> Path:
    extracted_bytes = 0
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = bundle.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise DistributionError("release archive has an invalid member count")
        for member in members:
            path = _normal_member_path(member.name)
            if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                raise DistributionError(
                    f"release archive contains a special file: {member.name}"
                )
            if not (
                member.isfile()
                or member.isdir()
                or member.issym()
                or member.islnk()
            ):
                raise DistributionError(
                    f"release archive contains unsupported member: {member.name}"
                )
            if member.isfile():
                extracted_bytes += member.size
                if extracted_bytes > MAX_EXTRACTED_BYTES:
                    raise DistributionError("release archive expands beyond size limit")
            if member.issym() or member.islnk():
                _normal_link_target(member, path)
        extraction_root.mkdir(parents=True, exist_ok=False)
        bundle.extractall(extraction_root)

    root = extraction_root / "hearting"
    if root.is_symlink() or not root.is_dir():
        raise DistributionError("release archive lacks one hearting root")
    resolved_root = root.resolve(strict=True)
    for relative in REQUIRED_RELEASE_FILES:
        candidate = root / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise DistributionError(f"release archive lacks required file: {relative}")
        try:
            candidate.resolve(strict=True).relative_to(resolved_root)
        except ValueError as exc:
            raise DistributionError(f"required release file escapes root: {relative}") from exc
    marker = (root / "RELEASE_VERSION").read_text(encoding="utf-8").strip()
    if marker != version:
        raise DistributionError(
            f"release marker mismatch: metadata={version} archive={marker!r}"
        )
    return root


def _release_metadata_path(root: Path) -> Path:
    return root / ".hearting-release.json"


# destructive-ok: reason=publish verified staging by atomic rename; boundary=one versioned staging directory and absent release destination
def _publish_release(
    extracted_root: Path, version: str, checksum: str
) -> tuple[Path, bool]:
    root = data_root()
    if root.exists() or root.is_symlink():
        if root.is_symlink() or not root.is_dir():
            raise DistributionError(f"distribution data root must be a directory: {root}")
    releases = root / "releases"
    if releases.is_symlink():
        raise DistributionError(f"release directory must not be a symlink: {releases}")
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / _validate_version(version)
    metadata = {
        "schema": STATE_SCHEMA,
        "version": version,
        "archive_sha256": checksum,
        "published_at": _utc_now(),
    }
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise DistributionError(f"invalid existing release root: {target}")
        existing_path = _release_metadata_path(target)
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DistributionError(f"existing release lacks valid metadata: {target}") from exc
        if (
            existing.get("version") != version
            or existing.get("archive_sha256") != checksum
        ):
            raise DistributionError(
                f"existing release conflicts with downloaded asset: {target}"
            )
        return target, False
    _atomic_json(_release_metadata_path(extracted_root), metadata)
    os.replace(extracted_root, target)
    return target, True


def _read_link(path: Path) -> Optional[str]:
    if path.is_symlink():
        return os.readlink(path)
    if path.exists():
        raise DistributionError(f"managed pointer collides with a regular path: {path}")
    return None


# destructive-ok: reason=commit a CAS-validated symlink and discard the helper-created sibling temp; boundary=one exact managed pointer and one mkstemp sibling
def _atomic_symlink(
    path: Path,
    target: Path | str,
    *,
    expected: _LeafState | None = None,
) -> _LeafState:
    leaf = _canonical_leaf(path)
    with _target_lock(leaf):
        wanted = _capture_leaf(leaf) if expected is None else expected
        _assert_leaf(leaf, wanted)
        if wanted.kind not in {"missing", "symlink"}:
            raise DistributionError(f"refusing non-pointer target: {leaf}")
        leaf.parent.mkdir(parents=True, exist_ok=True)
        _canonical_leaf(leaf)
        descriptor, raw_temp = tempfile.mkstemp(
            prefix=f".{leaf.name}.hearting-", dir=leaf.parent
        )
        os.close(descriptor)
        temporary = Path(raw_temp)
        # STANDALONE_SAFE_FS_INTERNAL: convert this call's empty sibling temp.
        temporary.unlink()
        try:
            temporary.symlink_to(target)
            _assert_leaf(leaf, wanted)
            os.replace(temporary, leaf)
            _fsync_parent(leaf)
            return _capture_leaf(leaf)
        finally:
            # STANDALONE_SAFE_FS_INTERNAL: exact sibling temp owned by this call.
            temporary.unlink(missing_ok=True)


# destructive-ok: reason=remove one CAS-validated standalone-installer leaf; boundary=one exact caller target under its canonical target lock
def _remove_exact(path: Path, expected: _LeafState) -> _LeafState:
    leaf = _canonical_leaf(path)
    with _target_lock(leaf):
        _assert_leaf(leaf, expected)
        if expected.kind == "missing":
            return expected
        if expected.kind not in {"file", "symlink"}:
            raise DistributionError(f"refusing unsupported removal target: {leaf}")
        leaf.unlink()
        _fsync_parent(leaf)
        return _capture_leaf(leaf)


def _restore_link(path: Path, previous: _LeafState, postimage: _LeafState) -> None:
    current = _capture_leaf(path)
    if current == previous:
        return
    if current != postimage:
        raise DistributionError(
            f"concurrent-successor: {path}: postimage={postimage} current={current}"
        )
    if previous.kind == "missing":
        _remove_exact(path, current)
    elif previous.kind == "symlink" and previous.target is not None:
        _atomic_symlink(path, previous.target, expected=current)
    else:
        raise DistributionError(f"cannot restore unsupported pointer preimage: {path}")


def _restore_bytes(
    path: Path, previous: _LeafState, postimage: _LeafState, payload: bytes | None
) -> None:
    current = _capture_leaf(path)
    if current == previous:
        return
    if current != postimage:
        raise DistributionError(
            f"concurrent-successor: {path}: postimage={postimage} current={current}"
        )
    if previous.kind == "missing":
        _remove_exact(path, current)
    elif previous.kind == "file" and payload is not None:
        _atomic_bytes(
            path,
            payload,
            int(previous.mode or 0o600),
            expected=current,
        )
    else:
        raise DistributionError(f"cannot restore unsupported file preimage: {path}")


def _assert_rollback_candidate(
    path: Path, previous: _LeafState, postimage: _LeafState
) -> None:
    current = _capture_leaf(path)
    if current not in {previous, postimage}:
        raise DistributionError(
            f"concurrent-successor: {path}: postimage={postimage} current={current}"
        )


def _launcher_is_harness_link(path: Path) -> bool:
    if not path.is_symlink():
        return False
    raw = Path(os.readlink(path))
    target = raw if raw.is_absolute() else path.parent / raw
    return target.as_posix().endswith("/tools/install/harness.sh")


def _launcher_destination(path: Path) -> Optional[Path]:
    if not path.is_symlink():
        return None
    try:
        raw = Path(os.readlink(path))
        target = raw if raw.is_absolute() else path.parent / raw
        return target.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _managed_runtime_bundle_roots() -> set[Path]:
    """Return immutable runtime bundle sources owned by this installer.

    Activation advances before shared launchers are repointed, so the launcher
    can legitimately still reference the immediately previous bundle.  Scan
    the installer-owned bundle containers instead of trusting only the current
    activation record.  Symlinked bundle entries and source roots remain
    foreign.
    """
    roots = set()
    for runtime in RUNTIMES:
        bundles = _runtime_home(runtime) / ".harness" / "bundles"
        try:
            entries = list(bundles.iterdir())
        except OSError:
            continue
        for entry in entries:
            source = entry / "source"
            try:
                if (
                    not entry.is_symlink()
                    and entry.is_dir()
                    and not source.is_symlink()
                    and source.is_dir()
                ):
                    roots.add(source.resolve(strict=False))
            except OSError:
                continue
    return roots


def _known_tool_launcher_roots(state: Optional[dict]) -> set[Path]:
    roots = {_home() / "hearting", _home() / "agent_setting"}
    if state and state.get("release_root"):
        roots.add(Path(state["release_root"]))
    current = _launcher_destination(current_path())
    if current is not None:
        roots.add(current)
    for runtime in RUNTIMES:
        activation = _activation_state(runtime)
        source = activation.get("source_root") if activation else None
        if source and Path(source).is_absolute():
            roots.add(Path(source))
    roots.update(_managed_runtime_bundle_roots())
    return {root.resolve(strict=False) for root in roots}


def _tool_launcher_is_owned(
    path: Path, relative_source: str, state: Optional[dict]
) -> bool:
    try:
        raw = Path(os.readlink(path))
        lexical = raw if raw.is_absolute() else path.parent / raw
        if lexical.absolute() == (current_path() / relative_source).absolute():
            return True
    except OSError:
        return False
    destination = _launcher_destination(path)
    if destination is None:
        return False
    return destination in {
        (root / relative_source).resolve(strict=False)
        for root in _known_tool_launcher_roots(state)
    }


def _snapshot_tool_launchers(state: Optional[dict]) -> dict[str, _LeafState]:
    snapshot = {}
    for name, relative_source in TOOL_LAUNCHERS:
        path = bin_dir() / name
        if path.exists() and not path.is_symlink():
            raise DistributionError(
                f"Hearting launcher already exists and is not owned: {path}"
            )
        if path.is_symlink() and not _tool_launcher_is_owned(
            path, relative_source, state
        ):
            raise DistributionError(f"Hearting launcher is a foreign symlink: {path}")
        snapshot[name] = _capture_leaf(path)
    return snapshot


def _install_tool_launchers(
    root: Path,
    expected_states: dict[str, _LeafState] | None = None,
    postimages: dict[str, _LeafState] | None = None,
) -> bool:
    changed = False
    for name, relative_source in TOOL_LAUNCHERS:
        source = root / relative_source
        if source.is_symlink() or not source.is_file():
            raise DistributionError(
                f"managed release lacks launcher source: {relative_source}"
            )
        path = bin_dir() / name
        desired = current_path() / relative_source
        before = (
            expected_states[name]
            if expected_states is not None
            else _capture_leaf(path)
        )
        if before.kind not in {"missing", "symlink"}:
            raise DistributionError(f"Hearting launcher is not a pointer: {path}")
        if before.kind != "symlink" or Path(before.target or "") != desired:
            after = _atomic_symlink(path, desired, expected=before)
            changed = True
        else:
            after = before
        if postimages is not None:
            postimages[name] = after
    return changed


def _restore_tool_launchers(
    snapshot: dict[str, _LeafState], postimages: dict[str, _LeafState]
) -> None:
    for name, _relative_source in TOOL_LAUNCHERS:
        _restore_link(bin_dir() / name, snapshot[name], postimages[name])


def _repair_managed_pointers(state: dict) -> bool:
    target = Path(state["release_root"])
    if target.is_symlink() or not target.is_dir():
        raise DistributionError(f"managed release root is missing or unsafe: {target}")
    try:
        metadata = json.loads(
            _release_metadata_path(target).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise DistributionError(f"managed release metadata is invalid: {target}") from exc
    if (
        metadata.get("version") != state.get("version")
        or metadata.get("archive_sha256") != state.get("archive_sha256")
    ):
        raise DistributionError(f"managed release metadata does not match state: {target}")
    resolved_target = target.resolve(strict=True)
    for relative in REQUIRED_RELEASE_FILES:
        candidate = target / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise DistributionError(f"managed release lacks required file: {relative}")
        try:
            candidate.resolve(strict=True).relative_to(resolved_target)
        except ValueError as exc:
            raise DistributionError(
                f"managed release file escapes root: {relative}"
            ) from exc

    tool_preimages = _snapshot_tool_launchers(state)

    changed = False
    current = current_path()
    current_raw = _read_link(current)
    current_target = None
    if current_raw is not None:
        raw = Path(current_raw)
        current_target = raw if raw.is_absolute() else current.parent / raw
    if current_target is None or current_target.resolve(strict=False) != resolved_target:
        _atomic_symlink(current, target, expected=_capture_leaf(current))
        changed = True

    launcher = launcher_path()
    if launcher.exists() and not launcher.is_symlink():
        raise DistributionError(f"harness launcher already exists and is not owned: {launcher}")
    if launcher.is_symlink() and not _launcher_is_harness_link(launcher):
        raise DistributionError(f"harness launcher is a foreign symlink: {launcher}")
    desired = current / "tools/install/harness.sh"
    if not launcher.is_symlink() or Path(os.readlink(launcher)) != desired:
        _atomic_symlink(launcher, desired, expected=_capture_leaf(launcher))
        changed = True

    legacy = legacy_launcher_path()
    if legacy.exists() and not legacy.is_symlink():
        raise DistributionError(f"harness launcher already exists and is not owned: {legacy}")
    if legacy.is_symlink() and not _launcher_is_harness_link(legacy):
        raise DistributionError(f"harness launcher is a foreign symlink: {legacy}")
    if not legacy.is_symlink() or Path(os.readlink(legacy)) != desired:
        _atomic_symlink(legacy, desired, expected=_capture_leaf(legacy))
        changed = True
    if _install_tool_launchers(target, tool_preimages):
        changed = True
    return changed


def _runtime_home(runtime: str) -> Path:
    if runtime == "claude":
        return _env_path("CLAUDE_CONFIG_DIR", _home() / ".claude")
    if runtime == "codex":
        return _env_path("CODEX_HOME", _home() / ".codex")
    if runtime == "opencode":
        return _xdg_config_home() / "opencode"
    raise DistributionError(f"unknown runtime: {runtime}")


def _activation_state(runtime: str) -> Optional[dict]:
    path = _runtime_home(runtime) / ".harness/activation.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _selected_update_runtimes(state: dict, requested: Iterable[str]) -> tuple[list[str], dict]:
    old_root = Path(state["release_root"]).resolve(strict=False)
    selected = []
    skipped = {}
    for runtime in requested:
        activation = _activation_state(runtime)
        if not activation:
            skipped[runtime] = "missing"
            continue
        mode = activation.get("mode")
        source_value = activation.get("source_root")
        source = Path(source_value).resolve(strict=False) if source_value else None
        if mode == "linked":
            skipped[runtime] = "linked"
        elif mode != "packaged" or source != old_root:
            skipped[runtime] = "foreign"
        else:
            selected.append(runtime)
    return selected, skipped


def _release_projection_referenced(target: Path) -> bool:
    resolved = target.resolve(strict=False)
    current = current_path()
    if current.is_symlink():
        try:
            if current.resolve(strict=False) == resolved:
                return True
        except OSError:
            return True
    for runtime in RUNTIMES:
        activation = _activation_state(runtime)
        source = activation.get("source_root") if activation else None
        if source and Path(source).resolve(strict=False) == resolved:
            return True
    return False


def _activate_release(root: Path, runtimes: Iterable[str]) -> dict:
    selected = list(runtimes)
    if not selected:
        return {"runtimes": [], "session_action": {}}
    command = ["sh", str(root / "tools/install/harness.sh"), "runtime", "activate"]
    for runtime in selected:
        command.extend(["--runtime", runtime])
    command.extend(
        [
            "--mode",
            "packaged",
            "--source",
            str(root),
            "--json",
        ]
    )
    env = os.environ.copy()
    env["AGENT_HOME"] = str(root)
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=300
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = None
    if result.returncode != 0 or not isinstance(report, dict):
        detail = (result.stderr or result.stdout).strip()
        raise DistributionError(
            f"runtime activation failed ({result.returncode}): {detail[:1000]}"
        )
    rows = report.get("runtimes")
    if not isinstance(rows, list):
        rows = [report]
    return {
        "runtimes": selected,
        "session_action": {
            row["runtime"]: row.get("session_action")
            for row in rows
            if isinstance(row, dict) and row.get("runtime")
        },
        "report": report,
    }


def _reconcile_codex_launcher() -> dict:
    """Reconcile the protected Codex ingress for same-release repairs.

    This deliberately imports the launcher implementation from the active
    release instead of duplicating its transaction here.  Callers hold the
    distribution lock, so the launcher lock is acquired second.
    """
    try:
        import codex_launcher
    except (ImportError, OSError) as exc:
        return {
            "action": "managed-launcher",
            "status": "skipped-unavailable",
            "detail": f"launcher module unavailable: {exc}",
        }
    try:
        return codex_launcher.install(profile_policy="deny")
    except codex_launcher.CodexUnavailableError as exc:
        return {
            "action": "managed-launcher",
            "status": "skipped-unavailable",
            "detail": str(exc),
        }
    except codex_launcher.CodexLauncherError as exc:
        raise DistributionError(f"Codex launcher reconciliation failed: {exc}") from exc


def _write_distribution_state(
    value: dict, *, expected: _LeafState | None = None
) -> _LeafState:
    if os.environ.get("HARNESS_TEST_FAIL_STATE_COMMIT") == "1":
        raise DistributionError("injected distribution state commit failure")
    value = dict(value)
    value.pop("profile", None)
    return _atomic_json(state_path(), value, expected=expected)


def _relative_to_release(value_path: Path, candidate: Path) -> Optional[Path]:
    """Return `value_path`'s path relative to `candidate`, or None if it is
    not actually nested under it.

    Tries the literal (pointer-form) spelling first -- a chain-(3) writer
    records its self-ref paths in AGENT_HOME's pointer form verbatim
    (`dispatch_contract.resolve_agent_home`'s "stored/compared state paths
    must keep pointer form"), and `candidate` here carries that same
    spelling family. Falls back to a resolved-identity comparison
    (`agent_home_equivalent`'s approach) for a sidecar recorded under a
    different but equivalent spelling. `Path.relative_to` is a real
    path-component match, not a string prefix -- unlike the `str.startswith`
    this replaces, it cannot mistake `.../v0.9.1/...` for a path nested
    under `.../v0.9` (review round-5 S-1).
    """

    try:
        return value_path.relative_to(candidate)
    except ValueError:
        pass
    try:
        return value_path.resolve(strict=False).relative_to(candidate.resolve(strict=False))
    except ValueError:
        return None


def _reanchor_succeeded_attempt_links(directory: Path, old_release: Path, new_release: Path) -> bool:
    """Re-anchor a succeeded sidecar's self-referential paths (review Q-1).

    A `<node>.<attempt>.attempt.json` sidecar records its own location
    (`completion_marker`, `completion_marker_history`); readers verify that
    identity, so a byte-for-byte copy at a new root evaluates as missing and
    a same-attempt republish afterward hard-fails write_once's byte-identity
    check. Mirrors utilities/capability-route.py's
    `_rewrite_migrated_attempt_links` in effect -- same two keys, same
    `json.dumps(..., indent=2, ensure_ascii=False) + "\\n"` serialization
    (review N-5) -- so a republish comparing against a fresh write_once()
    call sees identical bytes. Only these two keys are rewritten; everything
    else, and the origin directory, stays untouched (design constraint 7).

    Returns False if a sidecar could not be read or re-anchored so the
    caller can withhold the candidate release from deletion instead of
    treating a silently skipped or partially applied re-anchor as success
    (review round-5 S-2).
    """

    ok = True
    for link_path in directory.glob("*.attempt.json"):
        try:
            link = json.loads(link_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ok = False
            continue
        changed = False
        for key in ("completion_marker", "completion_marker_history"):
            value = link.get(key)
            if not isinstance(value, str):
                continue
            relative = _relative_to_release(Path(value), old_release)
            if relative is not None:
                link[key] = str(new_release / relative)
                changed = True
        if changed:
            try:
                link_path.write_text(
                    json.dumps(link, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                ok = False
    return ok


_REGISTRY_CARRY_NAMES = ("jobs.log", "jobs.log.lock")
_REGISTRY_ROW_RANK = {"open": 0, "running": 1, "done": 2}


def _registry_row_identity(fields: list[str]) -> tuple[str, ...] | None:
    """stdlib-only mirror of utilities/dispatch_contract.py::_row_identity."""
    if len(fields) != 6:
        return None
    metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
    if metadata.get("attempt_id"):
        return ("attempt", metadata["attempt_id"])
    route_id = metadata.get("route_id")
    route_node = metadata.get("route_node")
    parent = metadata.get("parent")
    if route_id and route_node and parent:
        return ("legacy", route_id, route_node, parent, fields[4])
    return None


def _registry_row_rank(fields: list[str]) -> int | None:
    """None for an unknown state word -- never let it lose a comparison."""
    if len(fields) != 6:
        return None
    return _REGISTRY_ROW_RANK.get(fields[1])


def _merge_registry_rows(
    live_lines: list[str], stale_lines: list[str]
) -> tuple[list[str], bool]:
    """Pure row-wise merge. Returns (merged_lines, ok).

    ``ok=False`` means "processed without losing a row, but the merge
    cannot vouch for completeness" -- the caller must write nothing and
    treat this the same as a copy failure (candidate release retained).
    """

    def _prepare(raw_lines: list[str]) -> tuple[list[dict], bool]:
        prepared: list[dict] = []
        seen_identity: dict[tuple[str, ...], int] = {}
        dropped_duplicate = False
        for line in raw_lines:
            if not line.strip():
                continue
            fields = line.split("\t")
            identity = _registry_row_identity(fields)
            rank = _registry_row_rank(fields)
            entry = {"line": line, "fields": fields, "identity": identity, "rank": rank}
            if identity is None:
                prepared.append(entry)
                continue
            if identity in seen_identity:
                # Rule 6: two rows in one file sharing an identity is not a
                # normal registry shape. Keep the earliest-occurring row with
                # the highest rank as the representative and drop the rest
                # from this file; the caller decides whether that matters.
                index = seen_identity[identity]
                existing_rank = prepared[index]["rank"]
                existing_rank = -1 if existing_rank is None else existing_rank
                new_rank = -1 if rank is None else rank
                if new_rank > existing_rank:
                    prepared[index] = entry
                dropped_duplicate = True
                continue
            seen_identity[identity] = len(prepared)
            prepared.append(entry)
        return prepared, dropped_duplicate

    live_rows, live_dropped_duplicate = _prepare(live_lines)
    stale_rows, _stale_dropped_duplicate = _prepare(stale_lines)

    ok = True
    if live_dropped_duplicate:
        # A live registry should never legitimately hold two rows for the
        # same attempt identity -- unlike the stale side (rule 6 below),
        # this is not an expected shape, so treat it as incomplete.
        ok = False
    if any(row["identity"] is None and len(row["fields"]) != 6 for row in live_rows):
        ok = False
    if any(row["identity"] is None and len(row["fields"]) != 6 for row in stale_rows):
        ok = False
    # Rule 6 asymmetry: a duplicate identity dropped from the *stale* file
    # is never reported as data loss here, because that file's release is
    # rmtree-d by this function's caller immediately after this call
    # succeeds -- there is no surviving copy of the stale file for a
    # dropped duplicate to have been the only record of, so the live
    # registry (the sole surviving copy of record) loses nothing.

    live_identity_index = {
        row["identity"]: index
        for index, row in enumerate(live_rows)
        if row["identity"] is not None
    }
    live_literals = {row["line"] for row in live_rows}
    merged = [row["line"] for row in live_rows]

    stale_identity_seen: set[tuple[str, ...]] = set()
    for row in stale_rows:
        identity = row["identity"]
        if identity is None:
            if row["line"] not in live_literals:
                merged.append(row["line"])
                live_literals.add(row["line"])
            continue
        if identity in stale_identity_seen:
            continue
        stale_identity_seen.add(identity)
        if identity in live_identity_index:
            index = live_identity_index[identity]
            live_rank = live_rows[index]["rank"]
            stale_rank = row["rank"]
            if live_rank is not None and stale_rank is not None and stale_rank > live_rank:
                merged[index] = row["line"]
            # Rule 5: a rank tie, or either side's rank unknown, preserves
            # the live row untouched.
        else:
            merged.append(row["line"])
            live_literals.add(row["line"])

    # Rule 8 -- post-write invariants. A violation here means the rules
    # above disagree with their own contract; refuse to write rather than
    # let that disagreement pass silently.
    def _identity_ranks(rows: list[dict]) -> dict[tuple[str, ...], int | None]:
        return {row["identity"]: row["rank"] for row in rows if row["identity"] is not None}

    live_identity_ranks = _identity_ranks(live_rows)
    stale_identity_ranks = _identity_ranks(stale_rows)
    required_identities = set(live_identity_ranks) | set(stale_identity_ranks)

    merged_identity_ranks: dict[tuple[str, ...], list[int | None]] = {}
    for line in merged:
        fields = line.split("\t")
        identity = _registry_row_identity(fields)
        if identity is not None:
            merged_identity_ranks.setdefault(identity, []).append(_registry_row_rank(fields))

    if any(len(ranks) != 1 for ranks in merged_identity_ranks.values()):
        return [], False
    for identity in required_identities:
        if identity not in merged_identity_ranks:
            return [], False
        merged_rank = merged_identity_ranks[identity][0]
        candidate_ranks = [
            rank
            for rank in (live_identity_ranks.get(identity), stale_identity_ranks.get(identity))
            if rank is not None
        ]
        # An unranked merged row (unrecognized state word) is a legitimate
        # rule-5 outcome -- e.g. live keeps a row whose own state word this
        # merge does not recognize rather than comparing it away -- so a
        # None merged rank is excluded from this check the same way a None
        # live/stale rank is excluded from `candidate_ranks` above.
        if candidate_ranks and merged_rank is not None and merged_rank < max(candidate_ranks):
            return [], False

    required_literals = {row["line"] for row in live_rows if row["identity"] is None} | {
        row["line"] for row in stale_rows if row["identity"] is None
    }
    if not required_literals <= set(merged):
        return [], False

    return merged, ok


@contextlib.contextmanager
def _registry_lock(jobs: Path):
    """flock the registry's `<jobs>.lock`, mirroring `_distribution_lock()`."""
    jobs.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{jobs}.lock")
    if lock_path.is_symlink():
        raise OSError(f"dispatch registry lock must not be a symlink: {lock_path}")
    handle = lock_path.open("a+b")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if "fcntl" in sys.modules:
                sys.modules["fcntl"].flock(handle.fileno(), sys.modules["fcntl"].LOCK_UN)
        finally:
            handle.close()


# destructive-ok: reason=atomically replace one dispatch registry; boundary=one validated jobs registry and its mkstemp sibling
def _atomic_registry_write(jobs: Path, lines: list[str]) -> None:
    """stdlib mirror of `dispatch_contract._atomic_registry_replace`."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{jobs.name}.carry-", dir=str(jobs.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as registry:
            registry.write("\n".join(lines) + "\n" if lines else "")
            registry.flush()
            os.fsync(registry.fileno())
        os.replace(tmp_name, jobs)
        dir_fd = os.open(str(jobs.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _succeed_registry_rows(stale_jobs: Path, live_jobs: Path) -> bool:
    """Merge `stale_jobs` into `live_jobs` row-wise with terminal precedence,
    instead of the byte-copy `_succeed_dispatch_state` uses for every other
    file under `.dispatch`. A byte copy either skips the whole live registry
    (destination exists) or clobbers it outright; neither preserves rows the
    live registry gained after the stale snapshot was taken. Never holds
    both registries' locks at once: the stale file is read and released
    before the live lock is acquired, so there is no lock-ordering deadlock
    surface with any other reader/writer of these two files.
    """
    if not stale_jobs.is_file():
        return True

    def _reject(reason: str) -> None:
        print(
            "harness release: dispatch registry carry-forward rejected for "
            f"{stale_jobs}: {reason}",
            file=sys.stderr,
        )

    try:
        with _registry_lock(stale_jobs):
            stale_text = stale_jobs.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _reject("stale-unreadable")
        return False

    try:
        with _registry_lock(live_jobs):
            try:
                live_text = (
                    live_jobs.read_text(encoding="utf-8", errors="replace")
                    if live_jobs.is_file()
                    else ""
                )
            except OSError:
                _reject("live-unreadable")
                return False
            merged, ok = _merge_registry_rows(live_text.splitlines(), stale_text.splitlines())
            if not ok:
                # Rule 3/6 malformed-row rejections and rule 8 invariant
                # failures both collapse to this single boolean from
                # _merge_registry_rows; there is no finer-grained "reason"
                # signal to report than the fact that the merge could not
                # vouch for completeness.
                _reject("merge-invariant-violated")
                return False
            if merged == live_text.splitlines():
                # No-op merge: skip the write entirely rather than
                # `os.replace`-ing an identical file. A repeat carry-forward
                # call (e.g. a second rotation's succession pass over
                # already-fully-merged state) must never mint a new inode or
                # ctime for unchanged content -- SD-112 B-1 depends on the
                # stable registry's device/inode/ctime staying put across
                # rotations that have nothing new to carry.
                return True
            try:
                _atomic_registry_write(live_jobs, merged)
            except OSError:
                _reject("write-failed")
                return False
    except OSError:
        _reject("lock-unavailable")
        return False
    return True


def _mode_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _env_absolute(value: str, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DistributionError(f"{field} must be an absolute path: {path}")
    return path


def stable_state_root(environ: dict[str, str]) -> Path:
    """Standalone stdlib-only mirror of `utilities/dispatch_contract.py`'s
    `stable_state_root()` (SD-112 decision 6). The installer cannot import
    `utilities/` -- it is embedded in the install.sh asset before a harness
    root exists -- so this copy is bound to that runtime helper by a parity
    fixture instead of a shared import: both must resolve
    `HARNESS_STATE_ROOT` / `XDG_STATE_HOME` / `HOME` from the *passed*
    mapping only (never `os.environ` directly) to the same absolute path,
    `.../dispatch`, for the same env matrix.
    """

    harness_state_root = environ.get("HARNESS_STATE_ROOT")
    if harness_state_root:
        base = _env_absolute(harness_state_root, "HARNESS_STATE_ROOT")
    else:
        xdg_state_home = environ.get("XDG_STATE_HOME")
        if xdg_state_home:
            base = _env_absolute(xdg_state_home, "XDG_STATE_HOME") / "hearting"
        else:
            home = environ.get("HOME")
            if not home:
                raise DistributionError(
                    "dispatch-state-root-unresolved: none of HARNESS_STATE_ROOT, "
                    "XDG_STATE_HOME, HOME are set"
                )
            base = _env_absolute(home, "HOME") / ".local" / "state" / "hearting"
    return base / "dispatch"


def _ensure_new_directory_mode(path: Path, mode: int) -> None:
    """Force `mode` only when `path` doesn't exist yet -- an existing
    symlink, non-directory, or a different mode is a typed refusal, never a
    chmod of something this process didn't just create (SD-112 §13.33.2-(7),
    mirrors `dispatch_contract._ensure_new_directory_mode`)."""

    if path.is_symlink():
        raise DistributionError(f"dispatch-state-root-unwritable: {path}: refusing symlink")
    if path.exists():
        if not path.is_dir():
            raise DistributionError(f"dispatch-state-root-unwritable: {path}: not a directory")
        existing = _mode_bits(path)
        if existing != mode:
            raise DistributionError(
                f"dispatch-state-root-mode-violation: {path}: "
                f"mode={oct(existing)} expected={oct(mode)}"
            )
        return
    path.mkdir(parents=True, exist_ok=False)
    os.chmod(path, mode)


def _ensure_new_file_mode(path: Path, mode: int) -> None:
    """`_ensure_new_directory_mode` for a stable-root file (the migration
    journal): force `mode` only on first creation, never chmod an existing
    file."""

    if path.is_symlink():
        raise DistributionError(f"dispatch-state-root-unwritable: {path}: refusing symlink")
    if path.exists():
        if not path.is_file():
            raise DistributionError(f"dispatch-state-root-unwritable: {path}: not a regular file")
        existing = _mode_bits(path)
        if existing != mode:
            raise DistributionError(
                f"dispatch-state-root-mode-violation: {path}: "
                f"mode={oct(existing)} expected={oct(mode)}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    os.close(fd)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _alias_digest(hex_digest: str) -> str:
    """Journal digests use the repository-wide `sha256:<64 hex>` spelling.

    `_sha256_file`/`_scoped_tree_digest` return bare hex because the copy
    verifier only ever compares them to each other. The migration journal is
    read by `dispatch_contract._alias_record_valid`, which fails closed on any
    digest that is not well formed, so every value that reaches a record has
    to carry the prefix -- the same spelling `route_hash` and the launch
    compatibility tuple already use.
    """

    return "sha256:" + hex_digest


def _migration_relative_files(root: Path) -> list[str]:
    """Every regular file under `root`, relative-posix, excluding the
    registry (merged separately with terminal precedence, not byte-compared)."""

    if not root.is_dir():
        return []
    out = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(root).as_posix()
            if relative in _REGISTRY_CARRY_NAMES:
                continue
            out.append(relative)
    return sorted(out)


def _scoped_tree_digest(root: Path, relatives: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in relatives:
        path = root / relative
        file_digest = _sha256_file(path) if path.is_file() else ""
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _tree_total_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def _count_open_rows(jobs_path: Path) -> int:
    if not jobs_path.is_file():
        return 0
    try:
        text = jobs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) >= 2 and fields[1] == "open":
            count += 1
    return count


MIGRATION_ALIAS_RECORD_VERSION = 1
MIGRATION_JOURNAL_FILENAME = "migration-journal.jsonl"


def _migration_journal_path(stable_root: Path) -> Path:
    return stable_root / MIGRATION_JOURNAL_FILENAME


def _append_migration_journal(stable_root: Path, record: dict) -> None:
    path = _migration_journal_path(stable_root)
    try:
        _ensure_new_file_mode(path, 0o600)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise DistributionError(
            f"dispatch-state-root-unwritable: journal write failed: {path}: {exc}"
        ) from exc


def _read_migration_journal(stable_root: Path) -> list[dict]:
    path = _migration_journal_path(stable_root)
    if not path.is_file():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _latest_completed_alias(stable_root: Path, legacy_jobs_path: Path) -> Optional[dict]:
    target_path = str(Path(legacy_jobs_path).expanduser().resolve(strict=False))
    match = None
    for record in _read_migration_journal(stable_root):
        if record.get("record_version") != MIGRATION_ALIAS_RECORD_VERSION:
            continue
        if record.get("status") != "completed":
            continue
        legacy = record.get("legacy_jobs_identity")
        if not isinstance(legacy, dict) or legacy.get("path") != target_path:
            continue
        match = record  # append-only journal; the latest completed record wins
    return match


def _dispatch_migration_promoted(environ: dict[str, str]) -> bool:
    """M4 promotion switch (decision 3): true once *any* structurally-valid
    `completed` migration-alias record exists in the stable journal. This is
    the one and only signal `_succeed_dispatch_state()` uses to retarget."""

    try:
        stable_root = stable_state_root(environ)
    except DistributionError:
        return False
    for record in _read_migration_journal(stable_root):
        if (
            record.get("record_version") == MIGRATION_ALIAS_RECORD_VERSION
            and record.get("status") == "completed"
        ):
            return True
    return False


# destructive-ok: reason=remove a successful migration probe; boundary=one probe created inside the selected stable state root
def _migration_m0_preflight(source_dispatch: Path, stable_root: Path) -> dict:
    """M0 preflight (SD-112 §13.33.2-(4)): create/verify the stable root is
    writable, measure cross-device-ness and legacy inventory (open row
    count, total bytes -- SD-OPEN-14 observability). Failure is a typed
    refusal that blocks the entire `harness update`, never just pruning."""

    try:
        stable_root.parent.mkdir(parents=True, exist_ok=True)
        _ensure_new_directory_mode(stable_root, 0o700)
        probe = stable_root / f".m0-write-probe-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise DistributionError(f"dispatch-state-root-unwritable: {stable_root}: {exc}") from exc

    cross_device = True
    if source_dispatch.is_dir():
        try:
            cross_device = os.stat(source_dispatch).st_dev != os.stat(stable_root).st_dev
        except OSError:
            cross_device = True
    return {
        "cross_device": cross_device,
        "open_rows": _count_open_rows(source_dispatch / "jobs.log"),
        "total_bytes": _tree_total_bytes(source_dispatch),
    }


# destructive-ok: reason=commit a verified migration copy; boundary=one destination and its mkstemp sibling under stable state
def _atomic_migrate_copy(source: Path, destination: Path) -> bool:
    """M2 per-file atomic copy for the new migration path only (decision 8):
    copy to a same-directory `.tmp`, verify the digest, fsync the file,
    `os.replace`, fsync the parent directory. Cross-device safe -- this never
    relies on `os.rename` across filesystems, always a real copy. Returns
    False (leaving no partial destination) on any verification failure."""

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_digest = _sha256_file(source)
    except OSError:
        return False
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.migrate-", dir=str(destination.parent)
    )
    ok = False
    try:
        with os.fdopen(fd, "wb") as tmp_handle, open(source, "rb") as src:
            shutil.copyfileobj(src, tmp_handle)
            tmp_handle.flush()
            os.fsync(tmp_handle.fileno())
        if _sha256_file(Path(tmp_name)) != source_digest:
            return False
        os.replace(tmp_name, destination)
        ok = True
    except OSError:
        return False
    finally:
        if not ok and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    dir_fd = os.open(str(destination.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return True


def _migrate_tree_additive(source_root: Path, target_root: Path) -> bool:
    """M2 additive copy + the copy-side half of M3 verification, mirroring
    `_succeed_dispatch_state`'s additive discipline (destination existing
    wins, nothing already there is ever clobbered) but per-file atomic
    (`_atomic_migrate_copy`) instead of `shutil.copy2` -- this is the new
    migration copy path only; `_succeed_dispatch_state`'s own `copy2`
    semantics are unchanged (decision 8)."""

    ok = True
    touched_dirs: set[Path] = set()
    for source in source_root.rglob("*"):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(source_root)
        if relative.as_posix() in _REGISTRY_CARRY_NAMES:
            continue
        if relative.as_posix() == MIGRATION_JOURNAL_FILENAME:
            continue
        destination = target_root / relative
        if destination.exists():
            continue
        if not _atomic_migrate_copy(source, destination):
            ok = False
            continue
        touched_dirs.add(destination.parent)
    if not _succeed_registry_rows(source_root / "jobs.log", target_root / "jobs.log"):
        ok = False
    for source in source_root.rglob("*.attempt.json"):
        if not source.is_file():
            continue
        destination = target_root / source.relative_to(source_root)
        if destination.exists():
            touched_dirs.add(destination.parent)
    for directory in touched_dirs:
        if not _reanchor_succeeded_attempt_links(directory, source_root, target_root):
            ok = False
    return ok


def _migration_verify(source_root: Path, target_root: Path) -> bool:
    """M3: every source file (outside the registry-carry set) exists at its
    destination -- a retry-safe re-check independent of what the copy pass's
    own return value claimed (mirrors `_succeed_dispatch_state`'s review V-1
    guard: a no-op retry pass must not read as success unless the files are
    actually all there)."""

    for source in source_root.rglob("*"):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(source_root)
        if relative.as_posix() in _REGISTRY_CARRY_NAMES:
            continue
        if relative.as_posix() == MIGRATION_JOURNAL_FILENAME:
            continue
        if not (target_root / relative).is_file():
            return False
    return True


def run_dispatch_state_migration(
    source_dispatch: Path, *, environ: dict[str, str] | None = None
) -> dict:
    """M0-M6 (SD-112 §13.33.2-(4)): migrate one release-embedded `.dispatch`
    tree into the stable canonical dispatch state root, verify it, and (M4)
    append a `completed` migration-alias record once verified -- the one
    switch `_succeed_dispatch_state()` and `_migration_deletion_precondition`
    use to retarget. Idempotent per source jobs.log identity+content: a
    second call against the same, unchanged source is a no-op
    (`already-completed`, zero copies). M1-M3/M5 failures never raise --
    they abort just this migration attempt (status `aborted`); only M0
    (stable root unwritable) raises and blocks the caller's whole update."""

    env = os.environ if environ is None else environ
    stable_root = stable_state_root(env)
    m0 = _migration_m0_preflight(source_dispatch, stable_root)

    if not source_dispatch.is_dir() or not any(source_dispatch.iterdir()):
        return {"status": "no-op", "migration_id": None, **m0}

    source_jobs = source_dispatch / "jobs.log"
    legacy_path_identity = str(source_jobs.expanduser().resolve(strict=False))
    relatives = _migration_relative_files(source_dispatch)
    source_digest = _alias_digest(_scoped_tree_digest(source_dispatch, relatives))
    # A legacy tree with no jobs.log has no registry to alias. The tree still
    # migrates and still promotes, but the resulting record carries an empty
    # digest and is therefore not a claimable alias -- continuation falls
    # through to the compat window, which is the correct answer here.
    source_jobs_digest = (
        _alias_digest(_sha256_file(source_jobs)) if source_jobs.is_file() else ""
    )

    existing = _latest_completed_alias(stable_root, source_jobs)
    if (
        existing is not None
        and existing.get("legacy_jobs_identity", {}).get("content_digest") == source_jobs_digest
        and existing.get("source_digest") == source_digest
    ):
        return {"status": "already-completed", "migration_id": existing.get("migration_id"), **m0}

    migration_id = hashlib.sha256(
        f"{legacy_path_identity}:{source_digest}:{source_jobs_digest}".encode("utf-8")
    ).hexdigest()[:32]

    legacy_identity = {"path": legacy_path_identity, "content_digest": source_jobs_digest}
    _append_migration_journal(
        stable_root,
        {
            "record_version": MIGRATION_ALIAS_RECORD_VERSION,
            "migration_id": migration_id,
            "status": "open",
            "legacy_jobs_identity": legacy_identity,
            "target_root": str(stable_root),
            "started_at": _utc_now(),
            "source_inventory_digest": source_digest,
        },
    )  # M1

    copy_ok = _migrate_tree_additive(source_dispatch, stable_root)  # M2
    verify_ok = copy_ok and _migration_verify(source_dispatch, stable_root)  # M3

    if not verify_ok:
        _append_migration_journal(
            stable_root,
            {
                "record_version": MIGRATION_ALIAS_RECORD_VERSION,
                "migration_id": migration_id,
                "status": "aborted",
                "reason": "copy-or-verify-failed",
                "legacy_jobs_identity": legacy_identity,
            },
        )  # M5
        return {"status": "aborted", "migration_id": migration_id, **m0}

    target_jobs = stable_root / "jobs.log"
    target_jobs_digest = (
        _alias_digest(_sha256_file(target_jobs)) if target_jobs.is_file() else ""
    )
    _append_migration_journal(
        stable_root,
        {
            "record_version": MIGRATION_ALIAS_RECORD_VERSION,
            "migration_id": migration_id,
            "status": "completed",
            "legacy_jobs_identity": legacy_identity,
            "stable_jobs_identity": {
                "path": str(target_jobs.expanduser().resolve(strict=False)),
                "content_digest": target_jobs_digest,
            },
            "source_digest": source_digest,
            "target_digest": _alias_digest(_scoped_tree_digest(stable_root, relatives)),
            "completed_at": _utc_now(),
        },
    )  # M4
    return {"status": "completed", "migration_id": migration_id, **m0}


def _migration_deletion_precondition(candidate: Path, environ: dict[str, str]) -> tuple[bool, str]:
    """Source-deletion precondition (SD-112 §13.33.2-(5)): once migration is
    promoted, a candidate's leftover `.dispatch` may be deleted only after
    (1) the journal is `completed` (implied by promotion), (2) its
    legacy-bound writers are quiescent/reconciled -- the caller's
    `_succeed_dispatch_state(candidate)` carry-forward already did that sweep
    (M6) -- and (3) the final delta digest verifies clean here. Any
    undecidable or failed state is a typed refusal
    (`dispatch-state-migration-blocked-live-attempt`), never a
    warn-and-continue; the retention floor in `_cleanup_releases` is a time
    delay, not evidence, and plays no part in this decision."""

    if not _dispatch_migration_promoted(environ):
        return True, ""
    stale_dispatch = candidate / ".dispatch"
    if not stale_dispatch.is_dir():
        return True, ""
    stable_root = stable_state_root(environ)
    for relative in _migration_relative_files(stale_dispatch):
        target = stable_root / relative
        if not target.is_file():
            return False, "dispatch-state-migration-blocked-live-attempt:delta-unreconciled"
        try:
            if _sha256_file(stale_dispatch / relative) != _sha256_file(target):
                return False, "dispatch-state-migration-blocked-live-attempt:delta-digest-mismatch"
        except OSError:
            return False, "dispatch-state-migration-blocked-live-attempt:delta-unreadable"
    return True, ""


def _release_attempt_ids(dispatch_root: Path) -> set[str] | None:
    """`attempt_id=` set from `<dispatch_root>/jobs.log`. Absence is an empty
    set (nothing to prove); a read/parse failure is `None` (proof
    impossible) -- the two are never conflated (SD-115 §13.34.3-(2))."""

    jobs = dispatch_root / "jobs.log"
    if not jobs.is_file():
        return set()
    try:
        text = jobs.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    ids: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 6:
            return None
        for item in fields[5].split(","):
            if item.startswith("attempt_id="):
                value = item[len("attempt_id="):]
                if value:
                    ids.add(value)
    return ids


def _retention_containment_precondition(
    candidate: Path, environ: dict[str, str]
) -> tuple[bool, str]:
    """SD-115 axis 1 (§13.34.3-(2)): a fourth, additive precondition on
    release deletion -- `attempt_ids(stable_after) superset-of
    attempt_ids(release_local_before)`. Any undecidable input (unresolved
    destination, unreadable registry) refuses exactly like a broken
    containment proof: this precondition is never the one that is silently
    skipped."""

    before = _release_attempt_ids(candidate / ".dispatch")
    surviving = _surviving_dispatch_root(candidate, environ)
    if surviving is None:
        return False, "dispatch-registry-retention-unproven:surviving-root-unresolved"
    after = _release_attempt_ids(surviving)
    if before is None or after is None:
        return False, "dispatch-registry-retention-unproven:registry-unreadable"
    if not before.issubset(after):
        return False, f"dispatch-registry-retention-unproven:missing:{len(before - after)}"
    return True, ""


def _surviving_dispatch_root(candidate: Path, environ: dict[str, str]) -> Path | None:
    """Resolve the dispatch root that survives this candidate's pruning
    (SD-115 R1, plan.md §3): extracted verbatim from
    `_succeed_dispatch_state`'s promotion branch so the containment gate
    below and the carry-forward above can never disagree about the
    destination. `attempt_ids(stable_after)` in the spec means this root --
    whichever root carry-forward actually lands on -- not the stable root
    read literally, since a pre-promotion update never touches the stable
    root at all.

    Returns `None` when the destination cannot be determined (a resolution
    failure) or when `candidate` is itself the only live release (self is
    never a carry-forward destination)."""

    if _dispatch_migration_promoted(environ):
        # Decision 3: the *only* switch is an M4 `completed` journal record.
        # Post-promotion every carry-forward retargets straight to the
        # stable root -- never `stable_state_root()/"dispatch"` (that would
        # double-nest; `stable_state_root()` already *is* the dispatch root).
        try:
            return stable_state_root(environ)
        except DistributionError:
            return None
    try:
        live_release = current_path().resolve(strict=True)
    except OSError:
        return None
    if live_release == candidate:
        return None
    return live_release / ".dispatch"


def _succeed_dispatch_state(candidate: Path) -> bool:
    """Carry a pruned release's `.dispatch` tree forward into the live
    release before the candidate is deleted.

    A process that never had `AGENT_DISPATCH_JOBS` in its environment
    resolves dispatch state (registry, logs, supervisor-state, completion
    markers) beneath whichever release `current` pointed at when it ran
    (chain (3) in utilities/dispatch_contract.py). Rotation later retargets
    `current` and this function's caller deletes the old release outright,
    which would silently destroy that state -- exactly the loss
    core/OPERATIONS.md P0 §5.12 says must never happen. Copying is
    additive-only: anything already present under the live release's
    `.dispatch` wins, so an in-progress writer on the live release can never
    be clobbered by a stale copy -- except the registry itself, which is
    merged row-wise with terminal precedence so a carried snapshot can
    never revert a live terminal row to open.

    Returns False if any file failed to copy, so the caller can leave the
    candidate release in place instead of rmtree-ing state that was never
    fully carried forward (review Q-6/P-3).
    """

    stale_dispatch = candidate / ".dispatch"
    if not stale_dispatch.is_dir():
        return True
    live_dispatch = _surviving_dispatch_root(candidate, os.environ)
    if live_dispatch is None:
        # base parity (C47-9): when `candidate` is itself the current live
        # release (pre-promotion), there is no destination to carry state
        # into and none needed -- base returned True for exactly this
        # branch, so this stays True even though the containment gate reads
        # the same `None` as `surviving-root-unresolved` (self-containment is
        # not something to prove).
        if not _dispatch_migration_promoted(os.environ):
            try:
                if current_path().resolve(strict=True) == candidate:
                    return True
            except OSError:
                pass
        return False
    touched_dirs: set[Path] = set()
    ok = True
    for source in stale_dispatch.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(stale_dispatch)
        if relative.as_posix() in _REGISTRY_CARRY_NAMES:
            continue
        destination = live_dispatch / relative
        if destination.exists():
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        except OSError:
            ok = False
            continue
        touched_dirs.add(destination.parent)
    if not _succeed_registry_rows(
        stale_dispatch / "jobs.log", live_dispatch / "jobs.log"
    ):
        ok = False
    # Review V-1: a retry pass copies nothing (every destination already
    # exists), so re-anchor verification must not be derived from the copy
    # set alone -- that let a first-pass failure (malformed or unwritable
    # sidecar) turn into success on the next call and release the candidate
    # for deletion while the live copy was still wrong. Re-inspect every
    # destination directory that holds attempt links carried from this
    # candidate: re-anchoring is idempotent for already-correct files, and a
    # still-broken sidecar keeps the succession False until repaired.
    for source in stale_dispatch.rglob("*.attempt.json"):
        if not source.is_file():
            continue
        destination = live_dispatch / source.relative_to(stale_dispatch)
        if destination.exists():
            touched_dirs.add(destination.parent)
    for directory in touched_dirs:
        if not _reanchor_succeeded_attempt_links(directory, stale_dispatch, live_dispatch):
            ok = False
    return ok


def _stable_registry_snapshot(environ: dict[str, str]) -> list[dict]:
    """Parse the stable per-user registry once, outside `_cleanup_releases`'s
    per-candidate loop (decision 2), into attempt-id-normalized open rows
    that carry a `launch_home`. An absent stable registry returns an empty
    snapshot -- candidate/live-release evaluation keeps working exactly as
    before (decision 2, "부재는 빈 snapshot"). An unreadable, malformed, or
    internally-conflicting registry is a typed refusal
    (`registry-unreadable:<stable-jobs-path>`) that stops cleanup/update
    outright -- never a silent skip, since silently skipping an unparseable
    stable registry could quietly stop protecting a release it should be
    protecting.

    Normalization: a byte-identical duplicate line collapses to one row: an
    `open`+`terminal` (`running`/`done`) duplicate for the same attempt id
    keeps only the terminal row (a stale open row must never hold a release
    hostage once its attempt finished); two *different* open rows for the
    same attempt id with different `launch_home` values are a genuine
    conflict, not a duplicate, and fail closed the same way.
    """

    try:
        stable_root = stable_state_root(environ)
    except DistributionError:
        return []
    jobs = stable_root / "jobs.log"
    if not jobs.is_file():
        return []
    try:
        text = jobs.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DistributionError(f"registry-unreadable:{jobs}") from exc

    rank = {"open": 0, "running": 1, "done": 2}
    by_attempt: dict[str, dict] = {}
    seen_lines: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        if line in seen_lines:
            continue
        seen_lines.add(line)
        fields = line.split("\t")
        if len(fields) < 6:
            raise DistributionError(f"registry-unreadable:{jobs}")
        status = fields[1]
        attempt_id = fields[4]
        pipe = fields[5]
        launch_home = None
        for item in pipe.split(","):
            if item.startswith("launch_home="):
                launch_home = item[len("launch_home="):]
                break
        entry = {
            "status": status,
            "launch_home": launch_home,
            "rank": rank.get(status, 0),
            "fields": fields,
        }
        existing = by_attempt.get(attempt_id)
        if existing is None:
            by_attempt[attempt_id] = entry
            continue
        if existing["rank"] == entry["rank"] == 0 and existing["launch_home"] != entry["launch_home"]:
            raise DistributionError(f"registry-unreadable:{jobs}")
        if entry["rank"] > existing["rank"]:
            by_attempt[attempt_id] = entry
    return [entry for entry in by_attempt.values() if entry["status"] == "open"]


def _release_reference_registries(candidate: Path) -> list[Path]:
    """Registries that can name `candidate` as a launch home.

    Two registries can hold an open row pointing at this release: the
    release's own `.dispatch/jobs.log` (state-root chain (3) -- a session
    that never had `AGENT_DISPATCH_JOBS` registers directly under whichever
    release `current` pointed at when it ran), and the live release's
    `.dispatch/jobs.log` (a row `_succeed_dispatch_state()` already carried
    forward before this candidate is deleted).
    """

    registries = [candidate / ".dispatch" / "jobs.log"]
    try:
        live_release = current_path().resolve(strict=True)
    except OSError:
        return registries
    registries.append(live_release / ".dispatch" / "jobs.log")
    return registries


def _release_in_use(candidate: Path, stable_snapshot: list[dict]) -> tuple[bool, str]:
    """Return (in_use, reason). Undecidable evidence returns (True, ...).

    `stable_snapshot` is `_stable_registry_snapshot()`'s pre-parsed result --
    the third reference-registry source (decision 2): the stable per-user
    registry's `launch_home`-bearing open rows. `launch_home`, `commonpath`,
    and the two-registry `is_live_registry` branch below are byte-for-byte
    unchanged; the stable source is purely additive and evaluated after them.
    A stable-registry open row with no `launch_home` is never candidate-local
    authority on its own (unlike a candidate-local legacy row) -- it is not
    scoped to any one release.
    """

    try:
        candidate_real = os.path.realpath(candidate)
    except OSError:
        candidate_real = str(candidate)

    for registry in _release_reference_registries(candidate):
        is_live_registry = registry.parent.parent != candidate
        if not registry.is_file():
            continue
        try:
            text = registry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return True, f"registry-unreadable:{registry}"
        for line in text.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) < 6:
                return True, f"registry-row-unparsable:{registry}"
            if fields[1] != "open":
                continue
            pipe = fields[5]
            launch_home = None
            for item in pipe.split(","):
                if item.startswith("launch_home="):
                    launch_home = item[len("launch_home="):]
                    break
            if launch_home is not None:
                try:
                    home_real = os.path.realpath(launch_home)
                    common = os.path.commonpath((candidate_real, home_real))
                except (OSError, ValueError):
                    continue
                if common == candidate_real:
                    attempt_id = None
                    for item in pipe.split(","):
                        if item.startswith("attempt_id="):
                            attempt_id = item[len("attempt_id="):]
                            break
                    identifier = attempt_id or fields[4]
                    return True, f"open-attempt:{identifier}"
                continue
            if not is_live_registry:
                return True, "legacy-open-row-in-release-registry"

    for entry in stable_snapshot:
        launch_home = entry["launch_home"]
        if launch_home is None:
            continue
        try:
            home_real = os.path.realpath(launch_home)
            common = os.path.commonpath((candidate_real, home_real))
        except (OSError, ValueError):
            continue
        if common == candidate_real:
            fields = entry["fields"]
            pipe = fields[5]
            attempt_id = None
            for item in pipe.split(","):
                if item.startswith("attempt_id="):
                    attempt_id = item[len("attempt_id="):]
                    break
            identifier = attempt_id or fields[4]
            return True, f"open-attempt:{identifier}"
    return False, ""


def _canonical_json(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _gap_event_id(row: dict) -> str:
    payload = {key: value for key, value in row.items() if key != "event_id"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _gap_id(from_ts: str, to_ts: str, evidence_digest: str) -> str:
    digest = hashlib.sha256(f"{from_ts}|{to_ts}|{evidence_digest}".encode("utf-8")).hexdigest()
    return f"gap-{digest[:16]}"


def _forced_prune_gap_interval(candidate: Path) -> tuple[str, str]:
    now_ts = _utc_now()
    jobs = candidate / ".dispatch" / "jobs.log"
    if not jobs.is_file():
        return now_ts, now_ts
    try:
        text = jobs.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return now_ts, now_ts
    timestamps = [line.split("\t", 1)[0] for line in text.splitlines() if line.strip()]
    if not timestamps:
        return now_ts, now_ts
    return min(timestamps), max(timestamps)


def _commit_forced_prune_gap_record(candidate: Path, environ: dict[str, str], reason: str) -> bool:
    """Installer-local mirror of `dispatch_registry_inventory.record_gap()`
    (SD-115 §13.34.3-(2) operator override). The installer cannot import
    `utilities/` (see `stable_state_root()` docstring), so this writer
    duplicates the runtime leaf's JSON row shape byte-for-byte; a parity
    fixture keeps the two in sync. Deletion proceeds only after this
    returns True."""

    try:
        state_root = stable_state_root(environ)
    except DistributionError:
        return False
    from_ts, to_ts = _forced_prune_gap_interval(candidate)
    before = _release_attempt_ids(candidate / ".dispatch") or set()
    evidence_digest = "sha256:" + hashlib.sha256(
        _canonical_json({"missing_attempt_ids": sorted(before), "reason": reason}).encode("utf-8")
    ).hexdigest()
    inventory_root = state_root / "inventory"
    lock_path = inventory_root / "gaps.lock"
    inventory_root.mkdir(parents=True, exist_ok=True)
    with open(str(lock_path), "a+b") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            row = {
                "schema_version": 1,
                "gap_id": _gap_id(from_ts, to_ts, evidence_digest),
                "from_ts": from_ts,
                "to_ts": to_ts,
                "evidence_digest": evidence_digest,
                "cited_ledger_snapshot_digest": evidence_digest,
                "recoverable": False,
                "discovered_by": "forced-prune",
                "recorded_at": _utc_now(),
            }
            row["event_id"] = _gap_event_id(row)
            try:
                fd = os.open(str(inventory_root / "gaps.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, (_canonical_json(row) + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
            except OSError:
                return False
            return True
        finally:
            try:
                if "fcntl" in sys.modules:
                    sys.modules["fcntl"].flock(handle.fileno(), sys.modules["fcntl"].LOCK_UN)
            except OSError:
                pass


# destructive-ok: reason=prune only retention-proved version directories; boundary=canonical children of the managed releases root
def _cleanup_releases(keep: set[Path], *, force_prune_unproven: bool = False) -> None:
    releases = data_root() / "releases"
    if not releases.is_dir() or releases.is_symlink():
        return
    # Parsed once, outside the per-candidate loop (decision 2) -- an
    # unreadable/malformed stable registry raises here and aborts the whole
    # cleanup pass rather than being silently re-attempted per candidate.
    stable_snapshot = _stable_registry_snapshot(os.environ)
    candidates = sorted(
        (path for path in releases.iterdir() if path.is_dir() and not path.is_symlink()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    retained = 0
    for candidate in candidates:
        if candidate in keep or retained < 2:
            retained += 1
            continue
        in_use, why = _release_in_use(candidate, stable_snapshot)
        if in_use:
            print(
                f"harness release: {candidate} is still referenced by a live dispatch "
                f"attempt ({why}); keeping it instead of deleting it",
                file=sys.stderr,
            )
            continue
        if not _succeed_dispatch_state(candidate):
            print(
                f"harness release: dispatch state carry-forward incomplete for "
                f"{candidate}; keeping the release instead of deleting it",
                file=sys.stderr,
            )
            continue
        # Both preconditions are "unproven-ness" checks -- a content mismatch
        # (real corruption, e.g. B-7/B-8's out-of-band divergent copy) and a
        # broken/missing dispatch-registry containment proof are the same
        # kind of gap, and both route through the identical
        # force_prune_unproven + gap-precommit gate (SD-115 §13.34.3-(2)).
        # `_release_in_use`/`_succeed_dispatch_state` above are never
        # force-overridable: those guard live/unmigrated state, not proof.
        unproven_reasons = []
        migration_ok, migration_reason = _migration_deletion_precondition(candidate, os.environ)
        if not migration_ok:
            unproven_reasons.append(migration_reason)
        containment_ok, containment_reason = _retention_containment_precondition(candidate, os.environ)
        if not containment_ok:
            unproven_reasons.append(containment_reason)
        if unproven_reasons:
            if not force_prune_unproven:
                for reason in unproven_reasons:
                    print(
                        f"harness release: {reason} for {candidate}; keeping it "
                        "instead of deleting it",
                        file=sys.stderr,
                    )
                continue
            gap_committed = all(
                _commit_forced_prune_gap_record(candidate, os.environ, reason)
                for reason in unproven_reasons
            )
            if not gap_committed:
                print(
                    f"harness release: registry-gap record commit failed for {candidate}; "
                    "keeping it instead of deleting it",
                    file=sys.stderr,
                )
                continue
        shutil.rmtree(candidate, ignore_errors=True)


# destructive-ok: reason=rollback or discard one managed release staging transaction; boundary=version target staging root and state leaf selected by this invocation
def _install_or_update(
    *,
    repository: str,
    version: str,
    runtimes: Iterable[str],
    bootstrap: bool,
    channel: str,
    pinned_version: Optional[str],
    force_prune_unproven: bool = False,
) -> dict:
    repository = _validate_repository(repository)
    if version != "latest":
        _validate_version(version)
    requested = list(dict.fromkeys(runtimes))
    if not requested or any(runtime not in RUNTIMES for runtime in requested):
        raise DistributionError("at least one valid runtime is required")

    data_root().parent.mkdir(parents=True, exist_ok=True)
    with _distribution_lock():
        # M0 preflight (SD-112 §13.33.2-(4)/B-10): a stable dispatch state
        # root that cannot be created/written refuses the entire update --
        # never just prune -- before any download, activation, or rotation
        # happens. `dispatch-state-root-unwritable` propagates unchanged.
        try:
            _current_legacy_dispatch = current_path().resolve(strict=True) / ".dispatch"
        except OSError:
            _current_legacy_dispatch = current_path() / ".dispatch"
        _migration_m0_preflight(_current_legacy_dispatch, stable_state_root(os.environ))

        previous_state = _load_state()
        if bootstrap and previous_state:
            bootstrap = False
        if not bootstrap and not previous_state:
            raise DistributionError("no managed release is installed")
        release = _release_metadata(repository, version)
        checksum = _expected_checksum(release["assets"][CHECKSUM_NAME])
        if (
            previous_state
            and previous_state.get("version") == release["version"]
            and previous_state.get("archive_sha256") == checksum
        ):
            previous_state_leaf = _capture_leaf(state_path())
            previous_state_bytes = state_path().read_bytes()
            state_postimage = previous_state_leaf
            try:
                repaired = _repair_managed_pointers(previous_state)
                launcher_result = _reconcile_codex_launcher()
                previous_state["channel"] = channel
                previous_state["pinned_version"] = pinned_version
                previous_state["last_checked_at"] = _utc_now()
                state_postimage = _write_distribution_state(
                    previous_state, expected=previous_state_leaf
                )
            except Exception as original_error:
                rollback_error = None
                try:
                    _restore_bytes(
                        state_path(),
                        previous_state_leaf,
                        state_postimage,
                        previous_state_bytes,
                    )
                except Exception as exc:
                    rollback_error = str(exc)
                if rollback_error:
                    raise DistributionError(
                        "same-release reconfiguration failed and rollback was "
                        f"incomplete: {rollback_error}"
                    )
                if isinstance(original_error, DistributionError):
                    raise
                raise DistributionError(
                    f"same-release reconfiguration failed: {original_error}"
                ) from original_error
            if force_prune_unproven:
                # An explicit force pass on an already-current release still
                # walks the prune step: the flag is the operator's decision to
                # retire unproven superseded releases, independent of whether
                # this call changed the active version.
                _cleanup_releases(
                    {Path(previous_state["release_root"])},
                    force_prune_unproven=True,
                )
            return {
                "status": "repaired" if repaired else "up-to-date",
                "version": release["version"],
                "release_root": previous_state["release_root"],
                "archive_sha256": checksum,
                "runtimes": [],
                "skipped": {},
                "session_action": {},
                "launcher": launcher_result,
            }

        if bootstrap:
            selected = requested
            skipped = {}
        else:
            selected, skipped = _selected_update_runtimes(previous_state, requested)

        current = current_path()
        launcher = launcher_path()
        previous_current_raw = _read_link(current)
        previous_current = _capture_leaf(current)
        if (
            (previous_current_raw is None and previous_current.kind != "missing")
            or (
                previous_current_raw is not None
                and (
                    previous_current.kind != "symlink"
                    or previous_current.target != previous_current_raw
                )
            )
        ):
            raise DistributionError(f"current pointer changed during preflight: {current}")
        if launcher.exists() and not launcher.is_symlink():
            raise DistributionError(f"harness launcher already exists and is not owned: {launcher}")
        if launcher.is_symlink() and not _launcher_is_harness_link(launcher):
            raise DistributionError(f"harness launcher is a foreign symlink: {launcher}")
        previous_launcher = _capture_leaf(launcher)
        if previous_launcher.kind not in {"missing", "symlink"}:
            raise DistributionError(f"harness launcher changed during preflight: {launcher}")
        legacy_launcher = legacy_launcher_path()
        if legacy_launcher.exists() and not legacy_launcher.is_symlink():
            raise DistributionError(
                f"harness launcher already exists and is not owned: {legacy_launcher}"
            )
        if legacy_launcher.is_symlink() and not _launcher_is_harness_link(
            legacy_launcher
        ):
            raise DistributionError(
                f"harness launcher is a foreign symlink: {legacy_launcher}"
            )
        previous_legacy_launcher = _capture_leaf(legacy_launcher)
        if previous_legacy_launcher.kind not in {"missing", "symlink"}:
            raise DistributionError(
                f"harness launcher changed during preflight: {legacy_launcher}"
            )
        previous_tool_launchers = _snapshot_tool_launchers(previous_state)
        post_tool_launchers = dict(previous_tool_launchers)
        previous_state_leaf = _capture_leaf(state_path())
        previous_state_bytes = state_path().read_bytes() if state_path().is_file() else None
        old_root = (
            Path(previous_state["release_root"])
            if previous_state and previous_state.get("release_root")
            else None
        )
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(data_root().parent)))
        archive = staging / ARCHIVE_NAME
        published = False
        target = None
        activation = {"runtimes": [], "session_action": {}}
        post_current = previous_current
        post_launcher = previous_launcher
        post_legacy_launcher = previous_legacy_launcher
        state_postimage = previous_state_leaf
        try:
            _download_archive(release["assets"][ARCHIVE_NAME], archive, checksum)
            extracted = _safe_extract(archive, staging / "extract", release["version"])
            target, published = _publish_release(extracted, release["version"], checksum)
            activation = _activate_release(target, selected)
            post_current = _atomic_symlink(
                current, target, expected=previous_current
            )
            post_launcher = _atomic_symlink(
                launcher,
                current / "tools/install/harness.sh",
                expected=previous_launcher,
            )
            post_legacy_launcher = _atomic_symlink(
                legacy_launcher,
                current / "tools/install/harness.sh",
                expected=previous_legacy_launcher,
            )
            _install_tool_launchers(
                target, previous_tool_launchers, post_tool_launchers
            )
            next_state = {
                "schema": STATE_SCHEMA,
                "repository": repository,
                "version": release["version"],
                "archive_sha256": checksum,
                "release_root": str(target),
                "runtimes": (
                    previous_state.get("runtimes", requested)
                    if previous_state
                    else requested
                ),
                "updated_at": _utc_now(),
                "last_checked_at": _utc_now(),
                "metadata_url": release["metadata_url"],
                "auto_update": (
                    previous_state.get("auto_update")
                    if previous_state
                    else {"status": "pending"}
                ),
                "channel": channel,
                "pinned_version": pinned_version,
            }
            state_postimage = _write_distribution_state(
                next_state, expected=previous_state_leaf
            )
        except Exception as original_error:
            rollback_error = None
            rollback_conflict = False
            rollback_paths = [
                (current, previous_current, post_current),
                (launcher, previous_launcher, post_launcher),
                (
                    legacy_launcher,
                    previous_legacy_launcher,
                    post_legacy_launcher,
                ),
                (state_path(), previous_state_leaf, state_postimage),
            ]
            rollback_paths.extend(
                (
                    bin_dir() / name,
                    previous_tool_launchers[name],
                    post_tool_launchers[name],
                )
                for name, _relative_source in TOOL_LAUNCHERS
            )
            try:
                for rollback_path, preimage, postimage in rollback_paths:
                    _assert_rollback_candidate(rollback_path, preimage, postimage)
            except Exception as exc:
                rollback_error = str(exc)
                rollback_conflict = True
            if not rollback_conflict and old_root and selected:
                try:
                    _activate_release(old_root, selected)
                except Exception as exc:
                    rollback_error = str(exc)
            try:
                if not rollback_conflict:
                    _restore_link(current, previous_current, post_current)
                    _restore_link(launcher, previous_launcher, post_launcher)
                    _restore_link(
                        legacy_launcher,
                        previous_legacy_launcher,
                        post_legacy_launcher,
                    )
                    _restore_tool_launchers(
                        previous_tool_launchers, post_tool_launchers
                    )
                    _restore_bytes(
                        state_path(),
                        previous_state_leaf,
                        state_postimage,
                        previous_state_bytes,
                    )
            except Exception as exc:
                rollback_error = rollback_error or str(exc)
                rollback_conflict = True
            if (
                not rollback_conflict
                and published
                and target
                and target.exists()
                and not _release_projection_referenced(target)
            ):
                shutil.rmtree(target, ignore_errors=True)
            if rollback_error:
                raise DistributionError(
                    f"release transaction failed and rollback was incomplete: {rollback_error}"
                )
            if isinstance(original_error, DistributionError):
                raise
            raise DistributionError(
                f"release transaction failed: {original_error}"
            ) from original_error
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        keep = {target}
        if old_root:
            keep.add(old_root)
            # Succession is owed at ROTATION, not at deletion. `keep` plus the
            # `retained < 2` floor in _cleanup_releases means the release we
            # just rotated away from is never a prune candidate, so the
            # prune-time call below never fired for it: on 2026-08-20 v2.55.6
            # held 19 registered attempt rows while the live v2.55.8 registry
            # had 3, which reset balanced allocation's round-robin history and
            # pinned every owner to the declared_order head. Copying is
            # additive-only (live release wins), so an in-progress writer on
            # the live release cannot be clobbered, and the prune-time call
            # stays as a second net.
            if not _succeed_dispatch_state(old_root):
                print(
                    "harness release: dispatch state carry-forward incomplete for "
                    f"{old_root}; rotated state may be stranded until the next update",
                    file=sys.stderr,
                )
        _cleanup_releases(keep, force_prune_unproven=force_prune_unproven)
        # M1-M6: migrate whatever legacy dispatch state just landed under the
        # newly-activated release into the stable root and, once verified,
        # promote it (M4). Unlike M0 above, a migration-internal failure here
        # never fails the update that already succeeded -- it is recorded
        # `aborted` in the journal and retried on the next update call.
        try:
            run_dispatch_state_migration(target / ".dispatch", environ=os.environ)
        except DistributionError as exc:
            print(
                f"harness release: dispatch state migration deferred: {exc}",
                file=sys.stderr,
            )
        return {
            "status": "installed" if previous_state is None else "updated",
            "version": release["version"],
            "previous_version": previous_state.get("version") if previous_state else None,
            "release_root": str(target),
            "archive_sha256": checksum,
            "runtimes": activation["runtimes"],
            "skipped": skipped,
            "session_action": activation["session_action"],
            "launcher": (
                activation.get("report", {}).get("managed_launcher")
                if isinstance(activation.get("report"), dict)
                else None
            ),
        }


def bootstrap(
    repository: str,
    version: str,
    runtimes: Iterable[str],
    auto_update: bool,
) -> dict:
    channel = "stable" if version == "latest" else "pinned"
    result = _install_or_update(
        repository=repository,
        version=version,
        runtimes=runtimes,
        bootstrap=True,
        channel=channel,
        pinned_version=None if channel == "stable" else version,
    )
    if auto_update:
        try:
            scheduler = enable_auto_update()
        except Exception as exc:
            scheduler = {
                "status": "manual",
                "kind": scheduler_kind(),
                "detail": f"scheduler setup failed; run harness update manually: {exc}",
            }
    else:
        scheduler = {"status": "disabled", "kind": scheduler_kind()}
    try:
        _record_auto_update(scheduler)
    except Exception as exc:
        scheduler["detail"] = (
            scheduler.get("detail", "") + f"; scheduler state was not recorded: {exc}"
        ).strip("; ")
    result["auto_update"] = scheduler
    result["launcher"] = str(launcher_path())
    result["path_hint"] = str(bin_dir())
    return result


def update(
    version: Optional[str] = None,
    runtimes: Optional[Iterable[str]] = None,
    automatic: bool = False,
    force_prune_unproven: bool = False,
) -> dict:
    state = _load_state()
    if not state:
        raise DistributionError("no managed release is installed")
    channel = state.get("channel", "stable")
    pinned_version = state.get("pinned_version")
    if automatic and channel == "pinned":
        return {
            "status": "pinned",
            "version": state["version"],
            "release_root": state["release_root"],
            "archive_sha256": state["archive_sha256"],
            "runtimes": [],
            "skipped": {},
            "session_action": {},
        }
    if version is None:
        requested_version = pinned_version if channel == "pinned" else "latest"
    elif version == "latest":
        requested_version = "latest"
        channel = "stable"
        pinned_version = None
    else:
        requested_version = _validate_version(version)
        channel = "pinned"
        pinned_version = requested_version
    return _install_or_update(
        repository=state["repository"],
        version=requested_version,
        runtimes=runtimes or state.get("runtimes", RUNTIMES),
        bootstrap=False,
        channel=channel,
        pinned_version=pinned_version,
        force_prune_unproven=force_prune_unproven,
    )


def scheduler_kind() -> str:
    platform = os.environ.get("HARNESS_TEST_PLATFORM", sys.platform)
    if platform.startswith("linux"):
        return "systemd-user"
    if platform == "darwin":
        return "launch-agent"
    return "unsupported"


def _systemd_paths() -> tuple[Path, Path]:
    root = _xdg_config_home() / "systemd/user"
    return root / "hearting-update.service", root / "hearting-update.timer"


def _systemd_quote(path: Path) -> str:
    value = str(path).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if "\n" in value or "\r" in value:
        raise DistributionError("scheduler path contains a newline")
    return f'"{value}"'


def _scheduler_environment() -> dict[str, str]:
    values = {
        "HOME": str(_home()),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "XDG_CONFIG_HOME": str(_xdg_config_home()),
        "XDG_DATA_HOME": str(_xdg_data_home()),
        "XDG_STATE_HOME": str(_xdg_state_home()),
        "HARNESS_BIN_DIR": str(bin_dir()),
    }
    for name in (
        "HARNESS_DATA_ROOT",
        "HARNESS_STATE_ROOT",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
    ):
        if os.environ.get(name):
            values[name] = str(_env_path(name, Path("/unused")))
    return values


def _systemd_environment_line(name: str, value: str) -> str:
    escaped = (
        f"{name}={value}"
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
    )
    if "\n" in escaped or "\r" in escaped:
        raise DistributionError("scheduler environment contains a newline")
    return f'Environment="{escaped}"'


def _owned_scheduler_state(path: Path, kind: str) -> _LeafState:
    state = _capture_leaf(path)
    if state.kind == "missing":
        return state
    if state.kind != "file":
        raise DistributionError(f"scheduler unit is not an owned file: {path}")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DistributionError(f"scheduler unit cannot be read: {path}") from exc
    if _capture_leaf(path) != state:
        raise DistributionError(f"scheduler unit changed while reading: {path}")
    owned = False
    if kind == "systemd-service":
        owned = (
            b"Description=Update Hearting managed release\n" in payload
            and b"\nExecStart=" in payload
            and payload.endswith(b" update --auto\n")
        )
    elif kind == "systemd-timer":
        owned = (
            b"Description=Check for Hearting updates daily\n" in payload
            and b"\nOnUnitActiveSec=24h\n" in payload
            and payload.endswith(b"WantedBy=timers.target\n")
        )
    elif kind == "launch-agent":
        try:
            value = plistlib.loads(payload)
        except Exception:
            value = None
        arguments = value.get("ProgramArguments") if isinstance(value, dict) else None
        owned = (
            isinstance(value, dict)
            and value.get("Label") == "com.hearting.update"
            and isinstance(arguments, list)
            and len(arguments) >= 3
            and arguments[-2:] == ["update", "--auto"]
        )
    if not owned:
        raise DistributionError(f"ownership-unproved: foreign scheduler unit: {path}")
    return state


def _write_systemd_units() -> tuple[Path, Path]:
    service, timer = _systemd_paths()
    service_before = _owned_scheduler_state(service, "systemd-service")
    timer_before = _owned_scheduler_state(timer, "systemd-timer")
    environment = "\n".join(
        _systemd_environment_line(name, value)
        for name, value in _scheduler_environment().items()
    )
    service_body = (
        "[Unit]\n"
        "Description=Update Hearting managed release\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"{environment}\n"
        f"ExecStart={_systemd_quote(launcher_path())} update --auto\n"
    )
    timer_body = (
        "[Unit]\n"
        "Description=Check for Hearting updates daily\n\n"
        "[Timer]\n"
        "OnBootSec=10m\n"
        "OnUnitActiveSec=24h\n"
        "RandomizedDelaySec=2h\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    _atomic_bytes(
        service, service_body.encode("utf-8"), 0o644, expected=service_before
    )
    _atomic_bytes(timer, timer_body.encode("utf-8"), 0o644, expected=timer_before)
    return service, timer


def _launch_agent_path() -> Path:
    return _home() / "Library/LaunchAgents/com.hearting.update.plist"


def _write_launch_agent() -> Path:
    path = _launch_agent_path()
    before = _owned_scheduler_state(path, "launch-agent")
    payload = plistlib.dumps(
        {
            "Label": "com.hearting.update",
            "ProgramArguments": [str(launcher_path()), "update", "--auto"],
            "RunAtLoad": False,
            "StartInterval": 86400,
            "ProcessType": "Background",
            "EnvironmentVariables": _scheduler_environment(),
        },
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )
    _atomic_bytes(path, payload, 0o644, expected=before)
    return path


def _run_scheduler(command: list[str]) -> tuple[bool, str]:
    if os.environ.get("HARNESS_SCHEDULER_NO_ACTIVATE") == "1":
        return False, "scheduler activation skipped by environment"
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (result.stderr or result.stdout).strip()
    return result.returncode == 0, detail or f"exit={result.returncode}"


_PROBE_DETAIL_LIMIT = 200


def _probe_command(command: list[str]) -> tuple[bool, str]:
    """Run one exact, read-only scheduler inspection command."""
    allowed = {
        (
            "systemctl", "--user", "show", "hearting-update.timer",
            "--property=LoadState,ActiveState,UnitFileState,LastTriggerUSec",
        ),
        (
            "systemctl", "--user", "show", "hearting-update.service",
            "--property=Result,ExecMainCode,ExecMainStatus",
        ),
        ("launchctl", "print", f"gui/{os.getuid()}/com.hearting.update"),
    }
    if tuple(command) not in allowed:
        return False, "status probe command is not allowlisted"
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[:_PROBE_DETAIL_LIMIT]
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, (detail or f"exit={result.returncode}")[:_PROBE_DETAIL_LIMIT]
    # Parsers need the complete stdout payload. Only user-facing diagnostics
    # are bounded; truncating `launchctl print` here can discard its exit data.
    return True, (result.stdout or result.stderr or "").strip()


def _probe_result(kind: str, detail: str = "") -> dict:
    return {
        "probe": "unavailable",
        "loaded": None,
        "active": None,
        "enabled": None,
        "last_trigger": None,
        "last_result": None,
        "exit_status": None,
        "detail": detail[:_PROBE_DETAIL_LIMIT],
    }


def _key_values(output: str) -> dict[str, str]:
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip()
    return values


def _probe_systemd() -> dict:
    _, timer = _systemd_paths()
    timer_cmd = [
        "systemctl", "--user", "show", timer.name,
        "--property=LoadState,ActiveState,UnitFileState,LastTriggerUSec",
    ]
    ok, timer_output = _probe_command(timer_cmd)
    if not ok:
        return _probe_result("systemd", timer_output)
    timer_values = _key_values(timer_output)
    required = ("LoadState", "ActiveState", "UnitFileState")
    if any(key not in timer_values for key in required):
        return _probe_result("systemd", "systemd timer output is incomplete")
    loaded_states = {"loaded": True, "not-found": False, "error": False, "masked": False}
    active_states = {"active": True, "inactive": False, "failed": False, "deactivating": False, "activating": False}
    enabled_states = {"enabled": True, "enabled-runtime": True, "disabled": False}
    if (timer_values["LoadState"] not in loaded_states
            or timer_values["ActiveState"] not in active_states
            or timer_values["UnitFileState"] not in enabled_states):
        return _probe_result("systemd", "systemd timer state is unrecognized")
    last_trigger = timer_values.get("LastTriggerUSec", "").strip()
    if last_trigger.casefold() in {"", "n/a", "never"}:
        last_trigger = None
    result = _probe_result("systemd")
    result.update(
        probe="ok",
        loaded=loaded_states[timer_values["LoadState"]],
        active=active_states[timer_values["ActiveState"]],
        enabled=enabled_states[timer_values["UnitFileState"]],
        last_trigger=last_trigger,
        detail="systemd user timer inspected",
    )
    if last_trigger is None:
        result["detail"] = "systemd user timer inspected; no recorded trigger"
        return result
    _, service = _systemd_paths()
    service_cmd = [
        "systemctl", "--user", "show", service.name,
        "--property=Result,ExecMainCode,ExecMainStatus",
    ]
    service_ok, service_output = _probe_command(service_cmd)
    if not service_ok:
        result["detail"] = (
            "systemd user timer inspected; last result unavailable: "
            + service_output
        )[:_PROBE_DETAIL_LIMIT]
        return result
    service_values = _key_values(service_output)
    service_result = service_values.get("Result")
    if service_result:
        result["last_result"] = service_result
    status = service_values.get("ExecMainStatus")
    if status and re.fullmatch(r"-?\d+", status):
        result["exit_status"] = int(status)
    return result


def _probe_launch_agent() -> dict:
    domain = f"gui/{os.getuid()}/com.hearting.update"
    ok, output = _probe_command(["launchctl", "print", domain])
    if not ok:
        return _probe_result("launch-agent", output)
    state_match = re.search(r"^\s*state\s*=\s*(.*?)\s*$", output, re.MULTILINE)
    if not state_match:
        return _probe_result("launch-agent", "launchctl output has no state")
    state_text = state_match.group(1).lower()
    state = re.sub(r"[\s_-]+", "", state_text)
    # A periodic LaunchAgent normally has no running process between triggers.
    # Successful `launchctl print` proves that launchd has the job loaded; only
    # a throttled job is treated as not currently armed for healthy scheduling.
    active_states = {"running": True, "waiting": True, "exited": True,
                     "notrunning": True, "throttled": False}
    if state not in active_states:
        return _probe_result("launch-agent", "launchctl state is unrecognized")
    result = _probe_result("launch-agent")
    result.update(probe="ok", loaded=True, active=active_states[state], enabled=True,
                  detail=f"LaunchAgent inspected (state={state_text})")
    exit_match = re.search(
        r"^\s*last exit code\s*=\s*(.*?)\s*$",
        output,
        re.MULTILINE | re.IGNORECASE,
    )
    if exit_match:
        value = exit_match.group(1).strip()
        if value.casefold() in {"(never exited)", "never exited", "n/a"}:
            return result
        if not re.fullmatch(r"-?\d+", value):
            return _probe_result("launch-agent", "launchctl last exit code is invalid")
        result["exit_status"] = int(value)
        result["last_result"] = "success" if result["exit_status"] == 0 else "failed"
    return result


def _probe_scheduler(kind: str) -> dict:
    if kind == "systemd-user":
        return _probe_systemd()
    if kind == "launch-agent":
        return _probe_launch_agent()
    result = _probe_result(kind, "automatic updates are unsupported on this platform")
    result["probe"] = "unsupported"
    return result


def _scheduler_health(kind: str, configured: bool, probe: dict) -> str:
    if kind == "unsupported":
        return "unsupported"
    if not configured:
        return "degraded" if probe.get("probe") == "ok" and probe.get("loaded") else "disabled"
    if probe.get("probe") != "ok":
        return "unknown"
    if any(probe.get(key) is False for key in ("loaded", "active", "enabled")):
        return "degraded"
    if probe.get("exit_status") not in (None, 0):
        return "degraded"
    if probe.get("last_result") not in (None, "success", "Succeeded"):
        return "degraded"
    return "ok"


def enable_auto_update() -> dict:
    with _distribution_lock():
        state = _load_state()
        if not state:
            raise DistributionError(
                "auto-update requires a managed release; install one with install.sh first"
            )
        _repair_managed_pointers(state)
    kind = scheduler_kind()
    if kind == "systemd-user":
        service, timer = _write_systemd_units()
        ok_reload, reload_detail = _run_scheduler(["systemctl", "--user", "daemon-reload"])
        ok_enable, enable_detail = _run_scheduler(
            ["systemctl", "--user", "enable", "--now", timer.name]
        )
        active = ok_reload and ok_enable
        return {
            "status": "active" if active else "configured-manual",
            "kind": kind,
            "files": [str(service), str(timer)],
            "detail": enable_detail if not active else "timer enabled",
            "reload_detail": reload_detail,
        }
    if kind == "launch-agent":
        path = _write_launch_agent()
        domain = f"gui/{os.getuid()}"
        _run_scheduler(["launchctl", "bootout", domain, str(path)])
        active, detail = _run_scheduler(["launchctl", "bootstrap", domain, str(path)])
        return {
            "status": "active" if active else "configured-manual",
            "kind": kind,
            "files": [str(path)],
            "detail": detail,
        }
    return {
        "status": "manual",
        "kind": kind,
        "files": [],
        "detail": "automatic updates are unsupported; run harness update",
    }


def disable_auto_update() -> dict:
    kind = scheduler_kind()
    removed = []
    detail = ""
    if kind == "systemd-user":
        service, timer = _systemd_paths()
        _run_scheduler(["systemctl", "--user", "disable", "--now", timer.name])
        for path, unit_kind in (
            (service, "systemd-service"),
            (timer, "systemd-timer"),
        ):
            before = _owned_scheduler_state(path, unit_kind)
            if before.kind != "missing":
                _remove_exact(path, before)
                removed.append(str(path))
        _run_scheduler(["systemctl", "--user", "daemon-reload"])
    elif kind == "launch-agent":
        path = _launch_agent_path()
        domain = f"gui/{os.getuid()}"
        _run_scheduler(["launchctl", "bootout", domain, str(path)])
        before = _owned_scheduler_state(path, "launch-agent")
        if before.kind != "missing":
            _remove_exact(path, before)
            removed.append(str(path))
    else:
        detail = "automatic updates are unsupported on this platform"
    result = {
        "status": "disabled",
        "kind": kind,
        "files": removed,
        "detail": detail,
    }
    _record_auto_update(result)
    return result


def auto_update_status() -> dict:
    kind = scheduler_kind()
    if kind == "systemd-user":
        paths = _systemd_paths()
    elif kind == "launch-agent":
        paths = (_launch_agent_path(),)
    else:
        paths = ()
    configured = bool(paths) and all(path.is_file() and not path.is_symlink() for path in paths)
    state = _load_state()
    probe = _probe_scheduler(kind)
    channel = state.get("channel", "stable") if state else None
    pinned_version = state.get("pinned_version") if state and channel == "pinned" else None
    return {
        "status": "configured" if configured else "disabled",
        "kind": kind,
        "files": [str(path) for path in paths],
        "recorded": state.get("auto_update") if state else None,
        "version": state.get("version") if state else None,
        "channel": channel,
        "pinned_version": pinned_version,
        "health": _scheduler_health(kind, configured, probe),
        "scheduler": probe,
    }


def _record_auto_update(result: dict) -> None:
    with _distribution_lock():
        state = _load_state()
        if not state:
            return
        state["auto_update"] = result
        state["updated_at"] = _utc_now()
        _write_distribution_state(state)


def auto_update(operation: str) -> dict:
    """Manage the supported OS user scheduler."""
    if operation == "enable":
        result = enable_auto_update()
        _record_auto_update(result)
        return result
    if operation == "disable":
        return disable_auto_update()
    if operation == "status":
        return auto_update_status()
    raise DistributionError(f"unsupported auto-update operation: {operation}")


def _runtime_values(values: Optional[list[str]]) -> list[str]:
    if not values or "all" in values:
        return list(RUNTIMES)
    return list(dict.fromkeys(values))


def _print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    print(f"harness release: {result.get('status')} version={result.get('version', '-')}")
    if result.get("launcher"):
        print(f"launcher: {result['launcher']}")
    if result.get("runtimes"):
        print("activated: " + ", ".join(result["runtimes"]))
    for runtime, reason in result.get("skipped", {}).items():
        print(f"skipped: {runtime} ({reason})")
    scheduler = result.get("auto_update")
    if scheduler:
        print(f"auto-update: {scheduler['status']} ({scheduler['kind']})")
    if result.get("path_hint"):
        print(f"PATH: ensure {result['path_hint']} is on PATH")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hearting-distribution")
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap_parser = sub.add_parser("bootstrap")
    bootstrap_parser.add_argument(
        "--repository", default=os.environ.get("HARNESS_REPOSITORY", DEFAULT_REPOSITORY)
    )
    bootstrap_parser.add_argument(
        "--version", default=os.environ.get("HARNESS_VERSION", "latest")
    )
    bootstrap_parser.add_argument(
        "--runtime", action="append", choices=[*RUNTIMES, "all"]
    )
    bootstrap_parser.add_argument(
        "--no-auto-update",
        action="store_true",
        default=os.environ.get("HARNESS_NO_AUTO_UPDATE") == "1",
    )
    bootstrap_parser.add_argument("--json", action="store_true")
    update_parser = sub.add_parser("update")
    update_parser.add_argument(
        "--version", default=os.environ.get("HARNESS_VERSION")
    )
    update_parser.add_argument(
        "--runtime", action="append", choices=[*RUNTIMES, "all"]
    )
    update_parser.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)
    update_parser.add_argument("--force-prune-unproven", action="store_true")
    update_parser.add_argument("--json", action="store_true")
    auto_parser = sub.add_parser("auto-update")
    auto_parser.add_argument("operation", choices=["status", "enable", "disable"])
    auto_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "bootstrap":
            result = bootstrap(
                args.repository,
                args.version,
                _runtime_values(args.runtime),
                not args.no_auto_update,
            )
        elif args.command == "update":
            result = update(
                args.version,
                _runtime_values(args.runtime) if args.runtime else None,
                args.auto,
                args.force_prune_unproven,
            )
        else:
            result = auto_update(args.operation)
        _print_result(result, args.json)
        return 0
    except DistributionError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"harness release: failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
