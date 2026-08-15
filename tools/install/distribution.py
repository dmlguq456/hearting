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
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional


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
    "tools/fleet/fleet.sh",
    "tools/memory/mem.py",
    "tools/memory/protocol_v2.py",
    "tools/memory/git_exchange_v2.py",
    "tools/memory/sync_v2.py",
)
TOOL_LAUNCHERS = (
    ("fleet", "tools/fleet/fleet.sh"),
    ("mem", "tools/memory/mem.py"),
)
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(
    rf"^([0-9a-fA-F]{{64}})[ \t]+[*]?{re.escape(ARCHIVE_NAME)}[ \t]*$"
)


class DistributionError(RuntimeError):
    """Safe user-facing distribution failure."""


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


def _atomic_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise DistributionError(f"refusing to replace symlink state file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _atomic_json(path: Path, value: dict) -> None:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n"
    _atomic_bytes(path, payload)


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


def _atomic_symlink(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_link(path: Path, previous: Optional[str]) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        raise DistributionError(f"cannot restore pointer over regular path: {path}")
    if previous is not None:
        path.symlink_to(previous)


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


def _snapshot_tool_launchers(state: Optional[dict]) -> dict[str, Optional[str]]:
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
        snapshot[name] = os.readlink(path) if path.is_symlink() else None
    return snapshot


def _install_tool_launchers(root: Path) -> bool:
    changed = False
    for name, relative_source in TOOL_LAUNCHERS:
        source = root / relative_source
        if source.is_symlink() or not source.is_file():
            raise DistributionError(
                f"managed release lacks launcher source: {relative_source}"
            )
        path = bin_dir() / name
        desired = current_path() / relative_source
        if not path.is_symlink() or Path(os.readlink(path)) != desired:
            _atomic_symlink(path, desired)
            changed = True
    return changed


def _restore_tool_launchers(snapshot: dict[str, Optional[str]]) -> None:
    for name, _relative_source in TOOL_LAUNCHERS:
        _restore_link(bin_dir() / name, snapshot[name])


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

    _snapshot_tool_launchers(state)

    changed = False
    current = current_path()
    current_raw = _read_link(current)
    current_target = None
    if current_raw is not None:
        raw = Path(current_raw)
        current_target = raw if raw.is_absolute() else current.parent / raw
    if current_target is None or current_target.resolve(strict=False) != resolved_target:
        _atomic_symlink(current, target)
        changed = True

    launcher = launcher_path()
    if launcher.exists() and not launcher.is_symlink():
        raise DistributionError(f"harness launcher already exists and is not owned: {launcher}")
    if launcher.is_symlink() and not _launcher_is_harness_link(launcher):
        raise DistributionError(f"harness launcher is a foreign symlink: {launcher}")
    desired = current / "tools/install/harness.sh"
    if not launcher.is_symlink() or Path(os.readlink(launcher)) != desired:
        _atomic_symlink(launcher, desired)
        changed = True

    legacy = legacy_launcher_path()
    if legacy.exists() and not legacy.is_symlink():
        raise DistributionError(f"harness launcher already exists and is not owned: {legacy}")
    if legacy.is_symlink() and not _launcher_is_harness_link(legacy):
        raise DistributionError(f"harness launcher is a foreign symlink: {legacy}")
    if not legacy.is_symlink() or Path(os.readlink(legacy)) != desired:
        _atomic_symlink(legacy, desired)
        changed = True
    if _install_tool_launchers(target):
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


def _write_distribution_state(value: dict) -> None:
    if os.environ.get("HARNESS_TEST_FAIL_STATE_COMMIT") == "1":
        raise DistributionError("injected distribution state commit failure")
    value = dict(value)
    value.pop("profile", None)
    _atomic_json(state_path(), value)


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
    be clobbered by a stale copy.

    Returns False if any file failed to copy, so the caller can leave the
    candidate release in place instead of rmtree-ing state that was never
    fully carried forward (review Q-6/P-3).
    """

    stale_dispatch = candidate / ".dispatch"
    if not stale_dispatch.is_dir():
        return True
    try:
        live_release = current_path().resolve(strict=True)
    except OSError:
        return False
    if live_release == candidate:
        return True
    live_dispatch = live_release / ".dispatch"
    touched_dirs: set[Path] = set()
    ok = True
    for source in stale_dispatch.rglob("*"):
        if not source.is_file():
            continue
        destination = live_dispatch / source.relative_to(stale_dispatch)
        if destination.exists():
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        except OSError:
            ok = False
            continue
        touched_dirs.add(destination.parent)
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
        if not _reanchor_succeeded_attempt_links(directory, candidate, live_release):
            ok = False
    return ok


def _cleanup_releases(keep: set[Path]) -> None:
    releases = data_root() / "releases"
    if not releases.is_dir() or releases.is_symlink():
        return
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
        if not _succeed_dispatch_state(candidate):
            print(
                f"harness release: dispatch state carry-forward incomplete for "
                f"{candidate}; keeping the release instead of deleting it",
                file=sys.stderr,
            )
            continue
        shutil.rmtree(candidate, ignore_errors=True)


def _install_or_update(
    *,
    repository: str,
    version: str,
    runtimes: Iterable[str],
    bootstrap: bool,
    channel: str,
    pinned_version: Optional[str],
) -> dict:
    repository = _validate_repository(repository)
    if version != "latest":
        _validate_version(version)
    requested = list(dict.fromkeys(runtimes))
    if not requested or any(runtime not in RUNTIMES for runtime in requested):
        raise DistributionError("at least one valid runtime is required")

    data_root().parent.mkdir(parents=True, exist_ok=True)
    with _distribution_lock():
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
            previous_state_bytes = state_path().read_bytes()
            try:
                repaired = _repair_managed_pointers(previous_state)
                previous_state["channel"] = channel
                previous_state["pinned_version"] = pinned_version
                previous_state["last_checked_at"] = _utc_now()
                _write_distribution_state(previous_state)
            except Exception as original_error:
                rollback_error = None
                try:
                    _atomic_bytes(state_path(), previous_state_bytes)
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
            return {
                "status": "repaired" if repaired else "up-to-date",
                "version": release["version"],
                "release_root": previous_state["release_root"],
                "archive_sha256": checksum,
                "runtimes": [],
                "skipped": {},
                "session_action": {},
            }

        if bootstrap:
            selected = requested
            skipped = {}
        else:
            selected, skipped = _selected_update_runtimes(previous_state, requested)

        current = current_path()
        launcher = launcher_path()
        previous_current = _read_link(current)
        if launcher.exists() and not launcher.is_symlink():
            raise DistributionError(f"harness launcher already exists and is not owned: {launcher}")
        if launcher.is_symlink() and not _launcher_is_harness_link(launcher):
            raise DistributionError(f"harness launcher is a foreign symlink: {launcher}")
        previous_launcher = os.readlink(launcher) if launcher.is_symlink() else None
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
        previous_legacy_launcher = (
            os.readlink(legacy_launcher) if legacy_launcher.is_symlink() else None
        )
        previous_tool_launchers = _snapshot_tool_launchers(previous_state)
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
        try:
            _download_archive(release["assets"][ARCHIVE_NAME], archive, checksum)
            extracted = _safe_extract(archive, staging / "extract", release["version"])
            target, published = _publish_release(extracted, release["version"], checksum)
            activation = _activate_release(target, selected)
            _atomic_symlink(current, target)
            _atomic_symlink(launcher, current / "tools/install/harness.sh")
            _atomic_symlink(legacy_launcher, current / "tools/install/harness.sh")
            _install_tool_launchers(target)
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
            _write_distribution_state(next_state)
        except Exception as original_error:
            rollback_error = None
            if old_root and selected:
                try:
                    _activate_release(old_root, selected)
                except Exception as exc:
                    rollback_error = str(exc)
            try:
                _restore_link(current, previous_current)
                _restore_link(launcher, previous_launcher)
                _restore_link(legacy_launcher, previous_legacy_launcher)
                _restore_tool_launchers(previous_tool_launchers)
                if previous_state_bytes is None:
                    state_path().unlink(missing_ok=True)
                else:
                    _atomic_bytes(state_path(), previous_state_bytes)
            except Exception as exc:
                rollback_error = rollback_error or str(exc)
            if published and target and target.exists():
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
        _cleanup_releases(keep)
        return {
            "status": "installed" if previous_state is None else "updated",
            "version": release["version"],
            "previous_version": previous_state.get("version") if previous_state else None,
            "release_root": str(target),
            "archive_sha256": checksum,
            "runtimes": activation["runtimes"],
            "skipped": skipped,
            "session_action": activation["session_action"],
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


def _write_systemd_units() -> tuple[Path, Path]:
    service, timer = _systemd_paths()
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
    _atomic_bytes(service, service_body.encode("utf-8"), 0o644)
    _atomic_bytes(timer, timer_body.encode("utf-8"), 0o644)
    return service, timer


def _launch_agent_path() -> Path:
    return _home() / "Library/LaunchAgents/com.hearting.update.plist"


def _write_launch_agent() -> Path:
    path = _launch_agent_path()
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
    _atomic_bytes(path, payload, 0o644)
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
        for path in (service, timer):
            if path.exists() or path.is_symlink():
                path.unlink()
                removed.append(str(path))
        _run_scheduler(["systemctl", "--user", "daemon-reload"])
    elif kind == "launch-agent":
        path = _launch_agent_path()
        domain = f"gui/{os.getuid()}"
        _run_scheduler(["launchctl", "bootout", domain, str(path)])
        if path.exists() or path.is_symlink():
            path.unlink()
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
