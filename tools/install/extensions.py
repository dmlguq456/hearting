#!/usr/bin/env python3
"""Offline, instruction-only external extension lifecycle.

The built-in product manifest remains canonical and immutable.  This module
accepts only a local directory, projects Markdown from one skill into an
immutable XDG snapshot, and links that snapshot into explicitly selected
runtime skill directories.  Executable/plugin surfaces are reported but never
activated.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import paths
import safe_fs

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import harness_manifest


RUNTIMES = ("claude", "codex", "opencode")
NEXT_SESSION_ACTION = {
    "claude": "reinvoke",
    "codex": "new-session",
    "opencode": "restart-required",
}
MANIFEST_FIELDS = {
    "schema_version",
    "publisher",
    "name",
    "version",
    "license",
    "skill_path",
    "requirements",
}
REQUIREMENT_FIELDS = {"scripts", "hooks", "mcp", "connectors", "packages"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_FILES = 1000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_PHYSICAL_ID = 64
IGNORED_DIRS = {".git", ".hg", ".svn", ".venv", "__pycache__", "node_modules"}
PACKAGE_FILES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
    "gemfile",
    "gemfile.lock",
}
SCRIPT_SUFFIXES = {".sh", ".py", ".js", ".ts", ".ps1", ".rb", ".pl"}
SECRET_PATTERNS = (
    ("private-key", re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,255}\b")),
    ("github-fine-grained-token", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,255}\b")),
    ("gitlab-token", re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,255}\b")),
)


class ExtensionError(RuntimeError):
    """Safe extension failure with a stable machine-readable reason."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finding(identifier: str, severity: str, message: str, path: str = "") -> dict:
    result = {"id": identifier, "severity": severity, "message": message}
    if path:
        result["path"] = path
    return result


def _canonical_id(publisher: str, name: str) -> str:
    return f"external/{publisher}/{name}"


def parse_canonical_id(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if (
        len(parts) != 3
        or parts[0] != "external"
        or len(parts[1]) > 64
        or len(parts[2]) > 64
        or not NAME_RE.fullmatch(parts[1])
        or not NAME_RE.fullmatch(parts[2])
    ):
        raise ExtensionError(
            "invalid-canonical-id",
            "canonical id must be external/<publisher>/<skill> using lowercase hyphen names",
        )
    return parts[1], parts[2]


def physical_id(canonical_id: str) -> str:
    publisher, name = parse_canonical_id(canonical_id)
    suffix = hashlib.sha256(canonical_id.encode("utf-8")).hexdigest()[:12]
    readable = f"external-{publisher}-{name}"
    prefix = readable[: MAX_PHYSICAL_ID - len(suffix) - 1].rstrip("-")
    return f"{prefix}-{suffix}"


def _source_root(source: str | os.PathLike[str]) -> Path:
    value = os.fspath(source)
    if "://" in value or value.startswith("git@"):
        raise ExtensionError("remote-source", "extension source must be an existing local directory")
    path = Path(value).expanduser()
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExtensionError("source-missing", f"extension source is unavailable: {path}") from exc
    if not path.is_dir():
        raise ExtensionError("source-not-directory", f"extension source is not a directory: {path}")
    return path


def _validate_manifest(entries: list[dict]) -> dict:
    matches = [item for item in entries if item["path"] == "extension.json"]
    if len(matches) != 1:
        raise ExtensionError("manifest-missing", "extension.json is missing from source census")
    manifest_entry = matches[0]
    if manifest_entry["type"] != "file":
        raise ExtensionError("manifest-type", "extension.json must be a regular source file")
    manifest_bytes = manifest_entry["data"]
    try:
        data = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtensionError("manifest-invalid", "extension.json is invalid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ExtensionError("manifest-invalid", "extension.json root must be an object")
    if set(data) != MANIFEST_FIELDS:
        raise ExtensionError(
            "manifest-schema",
            "extension.json fields must exactly match the v1 extension schema",
        )
    if data["schema_version"] != 1:
        raise ExtensionError("manifest-schema", "extension.json schema_version must be 1")
    for key in ("publisher", "name"):
        if (
            not isinstance(data[key], str)
            or len(data[key]) > 64
            or not NAME_RE.fullmatch(data[key])
        ):
            raise ExtensionError(
                "manifest-name",
                f"extension.json {key} must use lowercase alphanumeric hyphen form",
            )
    for key in ("version", "license", "skill_path"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ExtensionError("manifest-schema", f"extension.json {key} must be a string")
    requirements = data["requirements"]
    if not isinstance(requirements, dict) or set(requirements) != REQUIREMENT_FIELDS:
        raise ExtensionError(
            "manifest-schema",
            "extension.json requirements fields must be scripts/hooks/mcp/connectors/packages",
        )
    for key, values in requirements.items():
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item.strip() for item in values
        ):
            raise ExtensionError(
                "manifest-schema", f"extension.json requirements.{key} must be a string list"
            )
    skill_rel = Path(data["skill_path"])
    if skill_rel.is_absolute() or ".." in skill_rel.parts or skill_rel in {Path("."), Path("")}:
        raise ExtensionError("skill-path", "skill_path must be a non-empty relative path without '..'")
    skill_text = skill_rel.as_posix()
    if not any(
        item["path"] == skill_text and item["type"] == "directory" for item in entries
    ):
        raise ExtensionError("skill-path", f"skill_path does not exist: {data['skill_path']}")
    data["_skill_rel"] = skill_text
    data["_canonical_id"] = _canonical_id(data["publisher"], data["name"])
    data["_physical_id"] = physical_id(data["_canonical_id"])
    data["_manifest_checksum"] = hashlib.sha256(manifest_bytes).hexdigest()
    return data


def _surface_for(path: str) -> set[str]:
    lower = path.lower()
    parts = lower.split("/")
    name = parts[-1]
    suffix = Path(name).suffix
    result: set[str] = set()
    if "scripts" in parts or "bin" in parts or suffix in SCRIPT_SUFFIXES:
        result.add("scripts")
    if "hooks" in parts or name == "hooks.json":
        result.add("hooks")
    if "mcp" in parts or name in {".mcp.json", "mcp.json"}:
        result.add("mcp")
    if "connectors" in parts or "apps" in parts or name.endswith(".app.json"):
        result.add("connectors")
    if name in PACKAGE_FILES:
        result.add("packages")
    if (
        lower.endswith(".claude-plugin/plugin.json")
        or lower.endswith(".codex-plugin/plugin.json")
        or "/plugins/" in f"/{lower}"
    ):
        result.add("plugins")
    return result


def _scan_secret(data: bytes) -> list[str]:
    return [identifier for identifier, pattern in SECRET_PATTERNS if pattern.search(data)]


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _read_census_fd(
    fd: int,
    remaining: int,
    *,
    force: bool,
    expected: Optional[os.stat_result] = None,
) -> tuple[bytes, int, bool]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise OSError("census target is not a regular file")
    changed = bool(
        expected
        and (before.st_dev != expected.st_dev or before.st_ino != expected.st_ino)
    )
    size = before.st_size
    if size > MAX_FILE_BYTES or (size > remaining and not force):
        return b"", size, changed
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    remaining_read = MAX_FILE_BYTES + 1
    while remaining_read:
        chunk = os.read(fd, min(65536, remaining_read))
        if not chunk:
            break
        chunks.append(chunk)
        remaining_read -= len(chunk)
    data = b"".join(chunks)
    after = os.fstat(fd)
    changed = changed or (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or len(data) != after.st_size
    )
    return data, after.st_size, changed


def _internal_link_parts(relative: Path, target: str) -> tuple[str, ...]:
    target_path = Path(target)
    if target_path.is_absolute():
        raise ValueError("absolute source symlinks are not supported")
    parts = list(relative.parent.parts)
    for part in target_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError("symlink escapes source")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise ValueError("symlink target is empty")
    return tuple(parts)


def _open_root_relative(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current)
            os.close(current)
            current = next_fd
        result = os.open(parts[-1], _file_open_flags(), dir_fd=current)
        return result
    finally:
        os.close(current)


def _census(root: Path) -> tuple[list[dict], str, list[dict], list[str]]:
    entries: list[dict] = []
    findings: list[dict] = []
    surfaces: set[str] = set()
    file_count = 0
    total_bytes = 0

    try:
        root_fd = os.open(root, _directory_open_flags())
    except OSError as exc:
        raise ExtensionError("source-race", "source root cannot be opened without symlinks") from exc

    def visit(directory_fd: int, relative: Path) -> None:
        nonlocal file_count, total_bytes
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError:
            findings.append(
                _finding(
                    "unreadable-directory",
                    "blocking",
                    "directory cannot be read",
                    relative.as_posix(),
                )
            )
            return
        for name in names:
            rel = relative / name
            rel_text = rel.as_posix()
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                findings.append(
                    _finding(
                        "unreadable-entry",
                        "blocking",
                        "entry metadata cannot be read",
                        rel_text,
                    )
                )
                continue

            link_target = ""
            file_fd: Optional[int] = None
            if stat.S_ISLNK(info.st_mode):
                try:
                    link_target = os.readlink(name, dir_fd=directory_fd)
                    target_parts = _internal_link_parts(rel, link_target)
                except (OSError, ValueError):
                    findings.append(
                        _finding(
                            "symlink-escape",
                            "blocking",
                            "symlink target is absolute, empty, or escapes source",
                            rel_text,
                        )
                    )
                    continue
                try:
                    file_fd = _open_root_relative(root_fd, target_parts)
                    target_info = os.fstat(file_fd)
                except OSError:
                    if file_fd is not None:
                        os.close(file_fd)
                    findings.append(
                        _finding(
                            "invalid-symlink",
                            "blocking",
                            "symlink is broken, cyclic, or traverses another symlink",
                            rel_text,
                        )
                    )
                    continue
                if stat.S_ISDIR(target_info.st_mode):
                    os.close(file_fd)
                    findings.append(
                        _finding(
                            "directory-symlink",
                            "blocking",
                            "directory symlinks are not supported by the v1 projection",
                            rel_text,
                        )
                    )
                    continue
                if not stat.S_ISREG(target_info.st_mode):
                    os.close(file_fd)
                    findings.append(
                        _finding(
                            "special-symlink",
                            "blocking",
                            "symlink target is not a regular file",
                            rel_text,
                        )
                    )
                    continue
                entry_type = "link"
                expected = None
            elif stat.S_ISDIR(info.st_mode):
                entries.append(
                    {"path": rel_text, "type": "directory", "link": "", "data": b""}
                )
                if name in IGNORED_DIRS:
                    continue
                try:
                    child_fd = os.open(name, _directory_open_flags(), dir_fd=directory_fd)
                    opened = os.fstat(child_fd)
                except OSError:
                    findings.append(
                        _finding(
                            "source-race",
                            "blocking",
                            "directory changed or became a symlink during census",
                            rel_text,
                        )
                    )
                    continue
                try:
                    if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                        findings.append(
                            _finding(
                                "source-race",
                                "blocking",
                                "directory changed during census",
                                rel_text,
                            )
                        )
                    visit(child_fd, rel)
                finally:
                    os.close(child_fd)
                continue
            elif stat.S_ISREG(info.st_mode):
                try:
                    file_fd = os.open(name, _file_open_flags(), dir_fd=directory_fd)
                except OSError:
                    findings.append(
                        _finding(
                            "source-race",
                            "blocking",
                            "file changed or became a symlink during census",
                            rel_text,
                        )
                    )
                    continue
                entry_type = "file"
                expected = info
            else:
                findings.append(
                    _finding(
                        "special-file",
                        "blocking",
                        "special filesystem entry is unsupported",
                        rel_text,
                    )
                )
                continue

            file_count += 1
            try:
                data, measured_size, changed = _read_census_fd(
                    file_fd,
                    max(0, MAX_TOTAL_BYTES - total_bytes),
                    force=(
                        rel_text == "extension.json"
                        or rel_text.endswith("/SKILL.md")
                    ),
                    expected=expected,
                )
            except OSError:
                findings.append(
                    _finding(
                        "unreadable-file",
                        "blocking",
                        "file cannot be read safely",
                        rel_text,
                    )
                )
                os.close(file_fd)
                continue
            os.close(file_fd)
            total_bytes += measured_size
            if changed:
                findings.append(
                    _finding(
                        "source-race",
                        "blocking",
                        "file changed while it was being inspected",
                        rel_text,
                    )
                )
            if file_count > MAX_FILES:
                findings.append(
                    _finding("file-count-limit", "blocking", f"source exceeds {MAX_FILES} files")
                )
            if measured_size > MAX_FILE_BYTES:
                findings.append(
                    _finding(
                        "file-size-limit",
                        "blocking",
                        f"file exceeds {MAX_FILE_BYTES} bytes",
                        rel_text,
                    )
                )
            if total_bytes > MAX_TOTAL_BYTES:
                findings.append(
                    _finding(
                        "total-size-limit",
                        "blocking",
                        f"source exceeds {MAX_TOTAL_BYTES} bytes",
                    )
                )
            for secret_id in _scan_secret(data):
                findings.append(
                    _finding(
                        f"secret-{secret_id}",
                        "blocking",
                        "high-confidence secret pattern detected; value omitted",
                        rel_text,
                    )
                )
            surfaces.update(_surface_for(rel_text))
            entries.append(
                {"path": rel_text, "type": entry_type, "link": link_target, "data": data}
            )

    try:
        visit(root_fd, Path())
    finally:
        os.close(root_fd)
    digest = hashlib.sha256()
    digest.update(b"source-digest-v1\0")
    for entry in sorted(entries, key=lambda item: item["path"]):
        for value in (entry["type"], entry["path"], entry["link"]):
            encoded = value.encode("utf-8", "surrogateescape")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        data = entry["data"]
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return entries, digest.hexdigest(), findings, sorted(surfaces)


def _frontmatter(data: bytes, expected_name: str) -> dict:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtensionError("skill-frontmatter", "SKILL.md must be UTF-8") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ExtensionError("skill-frontmatter", "SKILL.md must start with YAML frontmatter")
    try:
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise ExtensionError("skill-frontmatter", "SKILL.md frontmatter is not closed") from exc
    values: dict[str, str] = {}
    for line in lines[1:closing]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        values[key.strip()] = value.strip().strip("'\"")
    if not values.get("description"):
        raise ExtensionError("skill-frontmatter", "SKILL.md frontmatter requires description")
    if values.get("name") and values["name"] != expected_name:
        raise ExtensionError(
            "skill-frontmatter", "SKILL.md name must match extension.json name before projection"
        )
    return {"text": text, "lines": lines, "closing": closing, "values": values}


def _entry(entries: list[dict], relative: str) -> dict:
    matches = [item for item in entries if item["path"] == relative]
    if len(matches) != 1 or matches[0]["type"] == "directory":
        raise ExtensionError("skill-frontmatter", f"required skill file missing: {relative}")
    return matches[0]


def _git_provenance(source: Path) -> dict:
    def git(cwd: Path, *args: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    root = git(source, "rev-parse", "--show-toplevel")
    if not root:
        return {"kind": "local-directory", "root": str(source)}
    git_root = Path(root).resolve()
    try:
        relative = source.relative_to(git_root).as_posix()
    except ValueError:
        relative = "."
    status = git(git_root, "status", "--porcelain", "--", relative)
    branch = git(git_root, "symbolic-ref", "--short", "-q", "HEAD")
    return {
        "kind": "local-git",
        "root": str(git_root),
        "revision": git(git_root, "rev-parse", "HEAD"),
        "ref": branch or "detached",
        "dirty": bool(status),
    }


def _inspect(source: str | os.PathLike[str]) -> dict:
    root = _source_root(source)
    entries, source_checksum, findings, surfaces = _census(root)
    manifest = _validate_manifest(entries)
    skill_md = f"{manifest['_skill_rel']}/SKILL.md"
    frontmatter = _frontmatter(_entry(entries, skill_md)["data"], manifest["name"])
    raw_requirements = manifest["requirements"]
    requirements = {
        key: {"declared": bool(raw_requirements[key]), "count": len(raw_requirements[key])}
        for key in sorted(REQUIREMENT_FIELDS)
    }
    declared = sorted(key for key, values in raw_requirements.items() if values)
    inactive = sorted(set(surfaces) | set(declared))
    if "packages" in inactive:
        findings.append(
            _finding(
                "external-dependency",
                "blocking",
                "package/dependency metadata requires external execution and is unsupported in v1",
            )
        )
    for surface in inactive:
        if surface != "packages":
            findings.append(
                _finding(
                    f"inactive-{surface}",
                    "advisory",
                    f"{surface} surface remains inactive; only Markdown skill content is projected",
                )
            )
    license_files = sorted(
        item["path"]
        for item in entries
        if item["type"] != "directory"
        and Path(item["path"]).name.lower().startswith(("license", "copying"))
    )
    parity = {
        runtime: {
            "status": "full" if not inactive else "degraded",
            "loss_reasons": [] if not inactive else [f"inactive:{item}" for item in inactive],
        }
        for runtime in RUNTIMES
    }
    safe_version = (
        "[redacted]" if _scan_secret(manifest["version"].encode("utf-8")) else manifest["version"]
    )
    safe_license = (
        "[redacted]" if _scan_secret(manifest["license"].encode("utf-8")) else manifest["license"]
    )
    report = {
        "status": "blocked"
        if any(item["severity"] == "blocking" for item in findings)
        else "ready",
        "canonical_id": manifest["_canonical_id"],
        "physical_id": manifest["_physical_id"],
        "source": str(root),
        "source_checksum": source_checksum,
        "manifest_checksum": manifest["_manifest_checksum"],
        "manifest": {
            "schema_version": 1,
            "publisher": manifest["publisher"],
            "name": manifest["name"],
            "version": safe_version,
            "license": safe_license,
            "skill_path": manifest["_skill_rel"],
        },
        "requirements": requirements,
        "detected_surfaces": surfaces,
        "inactive_surfaces": inactive,
        "external_dependency_required": "packages" in inactive,
        "license": {
            "declared": safe_license,
            "files": license_files,
            "status": "declared-with-file" if license_files else "declared-only",
        },
        "provenance": _git_provenance(root),
        "parity": parity,
        "findings": sorted(
            findings, key=lambda item: (item["severity"], item["id"], item.get("path", ""))
        ),
        "_entries": entries,
        "_frontmatter": frontmatter,
    }
    return report


def _public_report(report: dict) -> dict:
    return {key: value for key, value in report.items() if not key.startswith("_")}


def inspect_source(source: str | os.PathLike[str]) -> dict:
    """Inspect a local source without mutating registry, snapshots, or runtimes."""
    return _public_report(_inspect(source))


def _rewrite_skill_name(data: bytes, old_name: str, new_name: str) -> bytes:
    parsed = _frontmatter(data, old_name)
    lines = list(parsed["lines"])
    closing = parsed["closing"]
    replaced = False
    for index in range(1, closing):
        if lines[index].split(":", 1)[0].strip() == "name" and ":" in lines[index]:
            lines[index] = f"name: {new_name}"
            replaced = True
            break
    if not replaced:
        lines.insert(1, f"name: {new_name}")
    suffix = "\n" if parsed["text"].endswith("\n") else ""
    return ("\n".join(lines) + suffix).encode("utf-8")


def _projection_digest(root: Path) -> str:
    directory_fd = _open_data_directory(root)
    digest = hashlib.sha256()
    digest.update(b"projection-digest-v1\0")

    def visit(current_fd: int, relative: Path) -> None:
        for name in sorted(os.listdir(current_fd)):
            path_text = (relative / name).as_posix()
            try:
                info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            except OSError as exc:
                raise ExtensionError(
                    "snapshot-unreadable", f"snapshot entry cannot be read: {path_text}"
                ) from exc
            encoded = path_text.encode("utf-8")
            if stat.S_ISDIR(info.st_mode):
                digest.update(b"D\0")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                try:
                    child_fd = os.open(name, _directory_open_flags(), dir_fd=current_fd)
                except OSError as exc:
                    raise ExtensionError(
                        "snapshot-symlink",
                        f"snapshot directory changed or is a symlink: {path_text}",
                    ) from exc
                try:
                    visit(child_fd, relative / name)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ExtensionError(
                    "snapshot-symlink", f"snapshot contains non-regular entry: {path_text}"
                )
            try:
                file_fd = os.open(name, _file_open_flags(), dir_fd=current_fd)
                with os.fdopen(file_fd, "rb") as handle:
                    data = handle.read(MAX_FILE_BYTES + 1)
            except OSError as exc:
                raise ExtensionError(
                    "snapshot-unreadable", f"snapshot file cannot be read: {path_text}"
                ) from exc
            digest.update(b"F\0")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)

    try:
        visit(directory_fd, Path())
    finally:
        os.close(directory_fd)
    return digest.hexdigest()


def _snapshot_key(source_checksum: str, projection_checksum: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"snapshot-v1\0")
    digest.update(source_checksum.encode("ascii"))
    digest.update(b"\0")
    digest.update(projection_checksum.encode("ascii"))
    return digest.hexdigest()


def _ensure_plain_dir(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ExtensionError("unsafe-state-path", f"{label} must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise ExtensionError("unsafe-state-path", f"{label} must be a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ExtensionError("unsafe-state-path", f"{label} became a symlink: {path}")


def _extension_root(kind: str, *, create: bool) -> Path:
    try:
        if kind == "state":
            base = paths.xdg_state_home()
        elif kind == "data":
            base = paths.xdg_data_home()
        else:
            raise ExtensionError("internal-path", f"unknown extension root kind: {kind}")
    except ValueError as exc:
        raise ExtensionError("invalid-xdg-root", str(exc)) from exc
    current = base
    for index, part in enumerate(("hearting", "extensions")):
        label = f"XDG {kind} root" if index == 0 else f"extension {kind} root"
        if create:
            _ensure_plain_dir(current, label)
        else:
            if current.is_symlink() or not current.is_dir():
                raise ExtensionError(
                    "unsafe-state-path", f"{label} is missing, invalid, or a symlink: {current}"
                )
        current = current / part
    if create:
        _ensure_plain_dir(current, f"extension {kind} root")
    elif current.is_symlink() or not current.is_dir():
        raise ExtensionError(
            "unsafe-state-path",
            f"extension {kind} root is missing, invalid, or a symlink: {current}",
        )
    return current


def _ensure_extension_root(kind: str) -> Path:
    return _extension_root(kind, create=True)


def _data_relative(path: Path) -> tuple[Path, tuple[str, ...]]:
    root = _extension_root("data", create=False)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ExtensionError("unsafe-snapshot-path", "snapshot path escapes XDG data root") from exc
    return root, relative.parts


def _open_data_directory(path: Path) -> int:
    root, parts = _data_relative(path)
    current = os.open(root, _directory_open_flags())
    try:
        for part in parts:
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except OSError as exc:
        os.close(current)
        raise ExtensionError(
            "unsafe-snapshot-path",
            f"snapshot path is missing, invalid, or traverses a symlink: {path}",
        ) from exc


def _ensure_snapshot_parents(publisher: str, name: str) -> Path:
    root = _ensure_extension_root("data")
    current = root
    current_fd = os.open(root, _directory_open_flags())
    try:
        for part in (publisher, name):
            current = current / part
            try:
                os.mkdir(part, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            except OSError as exc:
                raise ExtensionError(
                    "unsafe-snapshot-path",
                    f"snapshot parent is invalid or a symlink: {current}",
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
    finally:
        os.close(current_fd)
    return current


def _snapshot_path(entry: dict) -> Path:
    root = _extension_root("data", create=False)
    current = root
    for part in (entry["publisher"], entry["name"]):
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise ExtensionError(
                "unsafe-snapshot-path",
                f"snapshot parent is missing, invalid, or a symlink: {current}",
            )
    return current / entry["snapshot_key"]


# destructive-ok: reason=discard failed prepared extension snapshots; boundary=one digest-named staging directory under the extension snapshot root
def _prepare_snapshot(report: dict) -> tuple[Path, str, str, bool, Optional[Path]]:
    if report["status"] != "ready":
        raise ExtensionError("inspection-blocked", "extension has blocking inspection findings")
    manifest = report["manifest"]
    parent = _ensure_snapshot_parents(manifest["publisher"], manifest["name"])
    temp = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(parent)))
    created = False
    try:
        skill_prefix = manifest["skill_path"].rstrip("/") + "/"
        projected = 0
        for entry in report["_entries"]:
            relative = entry["path"]
            if entry["type"] == "directory" or not relative.startswith(skill_prefix):
                continue
            projected_rel = relative[len(skill_prefix) :]
            if not projected_rel or Path(projected_rel).suffix.lower() != ".md":
                continue
            destination = temp / projected_rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = entry["data"]
            if projected_rel == "SKILL.md":
                data = _rewrite_skill_name(
                    data, manifest["name"], report["physical_id"]
                )
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ExtensionError(
                    "projection-encoding", f"projected Markdown is not UTF-8: {projected_rel}"
                ) from exc
            destination.write_bytes(data)
            os.chmod(destination, 0o444)
            projected += 1
        if projected == 0 or not (temp / "SKILL.md").is_file():
            raise ExtensionError("projection-empty", "skill projection must contain SKILL.md")
        staged_frontmatter = _frontmatter(
            (temp / "SKILL.md").read_bytes(), report["physical_id"]
        )
        if staged_frontmatter["values"].get("name") != report["physical_id"]:
            raise ExtensionError("projection-name", "projected SKILL.md name was not namespaced")
        for file_path in temp.rglob("*"):
            if file_path.is_file():
                for secret_id in _scan_secret(file_path.read_bytes()):
                    raise ExtensionError(
                        f"secret-{secret_id}",
                        f"projected snapshot contains a secret pattern: {file_path.relative_to(temp)}",
                    )
        projection_checksum = _projection_digest(temp)
        second = _inspect(report["source"])
        if (
            second["source_checksum"] != report["source_checksum"]
            or second["manifest_checksum"] != report["manifest_checksum"]
            or second["canonical_id"] != report["canonical_id"]
            or second["manifest"] != report["manifest"]
        ):
            raise ExtensionError(
                "source-changed",
                "extension source identity or bytes changed during snapshot materialization",
            )
        key = _snapshot_key(report["source_checksum"], projection_checksum)
        final = parent / key
        if final.is_symlink():
            raise ExtensionError("snapshot-symlink", f"snapshot path is a symlink: {final}")
        if final.exists():
            if not final.is_dir() or _projection_digest(final) != projection_checksum:
                raise ExtensionError(
                    "snapshot-mismatch", "existing snapshot does not match its projection checksum"
                )
            _writable_rmtree(temp)
            return final, projection_checksum, key, False, None
        else:
            for directory in sorted(
                (path for path in temp.rglob("*") if path.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                os.chmod(directory, 0o555)
            os.chmod(temp, 0o555)
            created = True
        return final, projection_checksum, key, created, temp
    except Exception:
        if temp.exists():
            _writable_rmtree(temp)
        raise


# destructive-ok: reason=publish a prepared immutable extension snapshot; boundary=one verified staging directory and digest-named snapshot destination
def _commit_prepared_snapshot(
    temporary: Optional[Path], final: Path, projection_checksum: str
) -> None:
    if temporary is None:
        if _projection_digest(final) != projection_checksum:
            raise ExtensionError("snapshot-mismatch", "reused snapshot changed before commit")
        return
    if final.exists() or final.is_symlink():
        raise ExtensionError("snapshot-race", f"snapshot destination appeared during commit: {final}")
    if temporary.parent != final.parent:
        raise ExtensionError("snapshot-race", "staging and final snapshot parents differ")
    parent_fd = _open_data_directory(final.parent)
    try:
        os.rename(
            temporary.name,
            final.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except OSError as exc:
        raise ExtensionError("snapshot-race", "snapshot commit failed without replacement") from exc
    finally:
        os.close(parent_fd)
    if _projection_digest(final) != projection_checksum:
        raise ExtensionError("snapshot-mismatch", "committed snapshot failed digest verification")


# destructive-ok: reason=remove a no-follow extension-owned tree; boundary=one caller-validated snapshot staging or projection tree via dirfd walk
def _writable_rmtree(path: Path) -> None:
    root, parts = _data_relative(path)
    if not parts:
        raise ExtensionError("unsafe-cleanup", "refusing to remove extension data root")
    if not path.exists() and not path.is_symlink():
        return

    def remove_at(parent_fd: int, name: str, label: Path) -> None:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise ExtensionError("unsafe-cleanup", f"cleanup target cannot be read: {label}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ExtensionError("unsafe-cleanup", f"cleanup target is not a directory: {label}")
        try:
            child_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise ExtensionError(
                "unsafe-cleanup", f"cleanup target changed or became a symlink: {label}"
            ) from exc
        try:
            os.fchmod(child_fd, 0o700)
            for child in sorted(os.listdir(child_fd)):
                child_info = os.stat(child, dir_fd=child_fd, follow_symlinks=False)
                child_label = label / child
                if stat.S_ISDIR(child_info.st_mode):
                    remove_at(child_fd, child, child_label)
                elif stat.S_ISREG(child_info.st_mode):
                    os.unlink(child, dir_fd=child_fd)
                else:
                    raise ExtensionError(
                        "unsafe-cleanup", f"cleanup tree contains symlink/special entry: {child_label}"
                    )
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)

    parent_fd = os.open(root, _directory_open_flags())
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _directory_open_flags(), dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        remove_at(parent_fd, parts[-1], path)
    except OSError as exc:
        raise ExtensionError(
            "unsafe-cleanup", f"cleanup path changed or traverses a symlink: {path}"
        ) from exc
    finally:
        os.close(parent_fd)


def _registry_path() -> Path:
    return paths.extension_state_dir() / "registry.json"


def _journal_path() -> Path:
    return paths.extension_state_dir() / "transaction.json"


def _atomic_json(path: Path, data: dict) -> safe_fs.PathState:
    return _atomic_bytes(path, _json_bytes(data))


def _json_bytes(data: dict) -> bytes:
    return (
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _atomic_bytes(path: Path, data: bytes) -> safe_fs.PathState:
    try:
        with safe_fs.TargetLock(path):
            current = safe_fs.capture_state(path)
            auth = safe_fs.authority(
                path,
                owner="extensions:registry-or-journal",
                allowed_paths=(path,),
                allow_leaf_symlink=False,
                expected=current,
            )
            return safe_fs.atomic_write_bytes(auth, data, 0o600, create_parents=True)
    except safe_fs.SafetyError as exc:
        raise ExtensionError("unsafe-state-path", str(exc)) from exc


def _default_registry() -> dict:
    return {"schema_version": 1, "generation": 0, "extensions": {}}


def _validate_registry(data: Any) -> dict:
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "generation", "extensions"}
        or data.get("schema_version") != 1
        or not isinstance(data.get("generation"), int)
        or data["generation"] < 0
        or not isinstance(data.get("extensions"), dict)
    ):
        raise ExtensionError("registry-invalid", "extension registry does not match schema v1")
    for canonical, entry in data["extensions"].items():
        if not isinstance(canonical, str):
            raise ExtensionError("registry-invalid", "registry canonical ids must be strings")
        publisher, name = parse_canonical_id(canonical)
        required = {
            "publisher",
            "name",
            "physical_id",
            "source",
            "source_checksum",
            "projection_checksum",
            "snapshot_key",
            "version",
            "license",
            "runtimes",
            "created_at",
            "updated_at",
            "provenance",
            "parity",
            "runtime_roots",
        }
        if not isinstance(entry, dict) or set(entry) != required:
            raise ExtensionError("registry-invalid", f"registry entry fields are invalid: {canonical}")
        string_fields = {
            "publisher",
            "name",
            "physical_id",
            "source",
            "source_checksum",
            "projection_checksum",
            "snapshot_key",
            "version",
            "license",
            "created_at",
            "updated_at",
        }
        if not all(isinstance(entry[field], str) for field in string_fields):
            raise ExtensionError("registry-invalid", f"registry string fields are invalid: {canonical}")
        runtimes = entry["runtimes"]
        parity = entry["parity"]
        runtime_roots = entry["runtime_roots"]
        if (
            not isinstance(runtimes, list)
            or not all(isinstance(runtime, str) for runtime in runtimes)
            or not isinstance(entry["provenance"], dict)
            or not isinstance(parity, dict)
            or set(parity) != set(RUNTIMES)
            or not isinstance(runtime_roots, dict)
            or set(runtime_roots) != set(runtimes)
            or not all(
                isinstance(runtime, str) and isinstance(root, str)
                for runtime, root in runtime_roots.items()
            )
        ):
            raise ExtensionError("registry-invalid", f"registry typed fields are invalid: {canonical}")
        for runtime, value in parity.items():
            if (
                not isinstance(value, dict)
                or set(value) != {"status", "loss_reasons"}
                or value["status"] not in {"full", "degraded"}
                or not isinstance(value["loss_reasons"], list)
                or not all(isinstance(reason, str) for reason in value["loss_reasons"])
            ):
                raise ExtensionError(
                    "registry-invalid", f"registry parity is invalid: {canonical}:{runtime}"
                )
        if (
            entry["publisher"] != publisher
            or entry["name"] != name
            or entry["physical_id"] != physical_id(canonical)
            or not all(
                CHECKSUM_RE.fullmatch(entry[field] or "")
                for field in ("source_checksum", "projection_checksum", "snapshot_key")
            )
            or entry["snapshot_key"]
            != _snapshot_key(entry["source_checksum"], entry["projection_checksum"])
            or runtimes != sorted(set(runtimes))
            or not set(runtimes).issubset(RUNTIMES)
        ):
            raise ExtensionError("registry-invalid", f"registry entry invariants failed: {canonical}")
    return data


def _load_registry() -> dict:
    return _load_registry_state()[0]


def _load_registry_state() -> tuple[dict, Optional[bytes]]:
    path = _registry_path()
    if path.is_symlink():
        raise ExtensionError("registry-symlink", f"registry must not be a symlink: {path}")
    if not path.exists():
        return _default_registry(), None
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtensionError("registry-invalid", f"extension registry cannot be read: {path}") from exc
    return _validate_registry(data), raw


@contextmanager
def _mutation_lock() -> Iterator[None]:
    state = _ensure_extension_root("state")
    for item in (_registry_path(), _journal_path(), state / ".lock"):
        if item.is_symlink():
            raise ExtensionError("unsafe-state-path", f"extension state path is a symlink: {item}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(state / ".lock", flags, 0o600)
    except OSError as exc:
        raise ExtensionError("lock-unavailable", "extension writer lock cannot be opened") from exc
    try:
        if os.environ.get("HARNESS_EXTENSION_LOCK_NONBLOCK") == "1":
            flags_lock = fcntl.LOCK_EX | fcntl.LOCK_NB
        else:
            flags_lock = fcntl.LOCK_EX
        try:
            fcntl.flock(fd, flags_lock)
        except BlockingIOError as exc:
            raise ExtensionError("concurrent-writer", "another extension mutation is active") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _runtime_destination(runtime: str, physical: str, *, create: bool) -> Path:
    if runtime not in RUNTIMES:
        raise ExtensionError("runtime-invalid", f"unsupported runtime: {runtime}")
    try:
        home = paths.runtime_home(runtime)
    except ValueError as exc:
        raise ExtensionError("invalid-runtime-root", str(exc)) from exc
    if home.is_symlink():
        raise ExtensionError("runtime-home-symlink", f"runtime home must not be a symlink: {home}")
    if home.exists() and not home.is_dir():
        raise ExtensionError("runtime-home-type", f"runtime home is not a directory: {home}")
    if create:
        home.mkdir(parents=True, exist_ok=True)
    skills = home / "skills"
    if skills.is_symlink():
        raise ExtensionError("runtime-parent-symlink", f"runtime skills path is a symlink: {skills}")
    if skills.exists() and not skills.is_dir():
        raise ExtensionError("runtime-parent-type", f"runtime skills path is not a directory: {skills}")
    if create:
        skills.mkdir(exist_ok=True)
    destination = skills / physical
    try:
        destination.relative_to(home)
        destination.parent.resolve(strict=False).relative_to(home.resolve(strict=False))
    except (ValueError, RuntimeError) as exc:
        raise ExtensionError("runtime-path-escape", f"runtime destination escapes home: {destination}") from exc
    return destination


def _link_target(path: Path) -> Optional[str]:
    if path.is_symlink():
        return os.readlink(path)
    return None


def _preflight_destination(
    runtime: str,
    physical: str,
    *,
    expected: Optional[Path],
    require_missing: bool = False,
) -> dict:
    destination = _runtime_destination(runtime, physical, create=False)
    try:
        preimage = safe_fs.capture_state(destination)
    except safe_fs.SafetyError as exc:
        raise ExtensionError("ownership-drift", str(exc)) from exc
    if require_missing:
        if preimage.kind != "missing":
            raise ExtensionError(
                "destination-collision", f"refusing to replace existing runtime path: {destination}"
            )
        return {
            "runtime": runtime,
            "dest": str(destination),
            "before": "missing",
            "target": None,
            "preimage": preimage.public(),
            "postimage": preimage.public(),
        }
    if preimage.kind != "symlink":
        raise ExtensionError(
            "ownership-drift", f"owned runtime link is missing or replaced: {destination}"
        )
    raw = preimage.link_target
    if expected is None or raw != str(expected):
        raise ExtensionError(
            "ownership-drift", f"runtime link target differs from registry ownership: {destination}"
        )
    return {
        "runtime": runtime,
        "dest": str(destination),
        "before": "symlink",
        "target": raw,
        "preimage": preimage.public(),
        "postimage": preimage.public(),
    }


def _atomic_symlink(
    target: Path, destination: Path, expected: safe_fs.PathState
) -> safe_fs.PathState:
    try:
        with safe_fs.TargetLock(destination):
            auth = safe_fs.authority(
                destination,
                owner="extensions:sealed-runtime-projection",
                allowed_paths=(destination,),
                expected=expected,
            )
            return safe_fs.atomic_write_symlink(
                auth,
                str(target),
                create_parents=True,
                target_is_directory=True,
            )
    except safe_fs.SafetyError as exc:
        raise ExtensionError("expected-state-mismatch", str(exc)) from exc


def _remove_exact_leaf(
    path: Path, expected: safe_fs.PathState, *, owner: str
) -> safe_fs.PathState:
    try:
        with safe_fs.TargetLock(path):
            auth = safe_fs.authority(
                path,
                owner=owner,
                allowed_paths=(path,),
                expected=expected,
            )
            return safe_fs.remove_exact(auth)
    except safe_fs.SafetyError as exc:
        raise ExtensionError("expected-state-mismatch", str(exc)) from exc


def _create_record_parent(record: dict, physical: str) -> Path:
    destination = _runtime_destination(record["runtime"], physical, create=True)
    if str(destination) != record["dest"]:
        raise ExtensionError("journal-invalid", "runtime destination changed after preflight")
    return destination


def _after_link_change(count: int) -> None:
    hard = os.environ.get("HARNESS_EXTENSION_HARD_EXIT_AFTER")
    if hard and count >= int(hard):
        os._exit(97)
    fail = os.environ.get("HARNESS_EXTENSION_FAIL_AFTER")
    if fail and count >= int(fail):
        raise ExtensionError("injected-failure", "injected extension transaction failure")


def _registry_state_hash(raw: Optional[bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(b"registry-state-v1\0")
    if raw is None:
        digest.update(b"missing")
    else:
        digest.update(b"present\0")
        digest.update(raw)
    return digest.hexdigest()


def _journal_records_valid(journal: dict) -> dict:
    canonical = journal.get("canonical_id")
    if not isinstance(canonical, str):
        raise ExtensionError("journal-invalid", "transaction canonical id is invalid")
    publisher, name = parse_canonical_id(canonical)
    physical = physical_id(canonical)
    operation = journal.get("operation")
    status = journal.get("status")
    if operation not in {"add", "update", "remove"} or status not in {"applying", "committed"}:
        raise ExtensionError("journal-invalid", "transaction operation or status is invalid")
    if journal.get("physical_id") != physical:
        raise ExtensionError("journal-invalid", "transaction physical id is invalid")
    runtimes = journal.get("runtimes")
    records = journal.get("records")
    if (
        not isinstance(runtimes, list)
        or not all(isinstance(runtime, str) for runtime in runtimes)
        or not runtimes
        or runtimes != sorted(set(runtimes))
        or not set(runtimes).issubset(RUNTIMES)
        or not isinstance(records, list)
        or len(records) != len(runtimes)
    ):
        raise ExtensionError("journal-invalid", "transaction runtime records are invalid")
    before = _validate_registry(journal.get("registry_before"))
    after = _validate_registry(journal.get("registry_after"))
    before_exists = journal.get("registry_before_exists")
    before_encoded = journal.get("registry_before_b64")
    if not isinstance(before_exists, bool):
        raise ExtensionError("journal-invalid", "transaction prior registry presence is invalid")
    if before_exists:
        if not isinstance(before_encoded, str):
            raise ExtensionError("journal-invalid", "transaction prior registry bytes are missing")
        try:
            before_raw = base64.b64decode(before_encoded.encode("ascii"), validate=True)
            parsed_before = json.loads(before_raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExtensionError("journal-invalid", "transaction prior registry bytes are invalid") from exc
        if parsed_before != before:
            raise ExtensionError("journal-invalid", "transaction prior registry bytes disagree")
    else:
        if before_encoded is not None or before != _default_registry():
            raise ExtensionError("journal-invalid", "missing prior registry state is inconsistent")
        before_raw = None
    after_raw = _json_bytes(after)
    if (
        journal.get("registry_before_hash") != _registry_state_hash(before_raw)
        or journal.get("registry_after_hash") != _registry_state_hash(after_raw)
        or journal.get("registry_before_generation") != before["generation"]
        or journal.get("registry_after_generation") != after["generation"]
        or after["generation"] != before["generation"] + 1
    ):
        raise ExtensionError("journal-invalid", "transaction registry CAS fields are invalid")
    old = before["extensions"].get(canonical)
    new = after["extensions"].get(canonical)
    for entry in (old, new):
        if entry is not None:
            try:
                _assert_runtime_roots(entry)
            except ExtensionError as exc:
                raise ExtensionError(
                    "journal-invalid", "transaction runtime roots no longer match"
                ) from exc
    if operation == "add" and old is not None:
        raise ExtensionError("journal-invalid", "add journal identity already exists")
    if operation == "add" and new is None:
        raise ExtensionError("journal-invalid", "add journal result identity is missing")
    if operation in {"update", "remove"} and old is None:
        raise ExtensionError("journal-invalid", "journal prior registry ownership is missing")
    if operation == "update" and new is None:
        raise ExtensionError("journal-invalid", "update journal result identity is missing")
    if operation == "remove" and new is not None:
        raise ExtensionError("journal-invalid", "remove journal retained the identity")
    before_other = dict(before["extensions"])
    before_other.pop(canonical, None)
    after_other = dict(after["extensions"])
    after_other.pop(canonical, None)
    if before_other != after_other:
        raise ExtensionError("journal-invalid", "transaction changed unrelated registry entries")
    old_snapshot = _snapshot_path(old) if old else None
    key = journal.get("new_snapshot_key")
    if operation in {"add", "update"}:
        if not isinstance(key, str) or not CHECKSUM_RE.fullmatch(key):
            raise ExtensionError("journal-invalid", "transaction snapshot key is invalid")
        if new["snapshot_key"] != key or new["runtimes"] != runtimes:
            raise ExtensionError("journal-invalid", "transaction result entry disagrees with journal")
        new_snapshot = _snapshot_path(new)
        if journal.get("new_snapshot") != str(new_snapshot):
            raise ExtensionError("journal-invalid", "transaction snapshot path is invalid")
        created = journal.get("created_snapshot")
        if created not in {None, str(new_snapshot)}:
            raise ExtensionError("journal-invalid", "transaction created snapshot is invalid")
    else:
        new_snapshot = None
        if any(
            journal.get(field) is not None
            for field in ("new_snapshot", "new_snapshot_key", "created_snapshot")
        ):
            raise ExtensionError("journal-invalid", "remove journal must not name a new snapshot")
    checked = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "runtime",
            "dest",
            "before",
            "target",
            "expected_after",
            "preimage",
            "postimage",
        }:
            raise ExtensionError("journal-invalid", "transaction record fields are invalid")
        runtime = record["runtime"]
        expected_dest = _runtime_destination(runtime, physical, create=False)
        if runtime not in runtimes or record["dest"] != str(expected_dest):
            raise ExtensionError("journal-invalid", "transaction destination is not canonical")
        if record["before"] not in {"missing", "symlink"}:
            raise ExtensionError("journal-invalid", "transaction prior state is invalid")
        if operation == "add":
            valid_state = record["before"] == "missing" and record["target"] is None
        else:
            valid_state = (
                record["before"] == "symlink"
                and record["target"] == str(old_snapshot)
            )
        expected_after = str(new_snapshot) if new_snapshot else None
        if not valid_state or record["expected_after"] != expected_after:
            raise ExtensionError("journal-invalid", "transaction link ownership is invalid")
        try:
            preimage = safe_fs.PathState.from_public(record["preimage"])
            postimage = safe_fs.PathState.from_public(record["postimage"])
        except safe_fs.SafetyError as exc:
            raise ExtensionError(
                "journal-invalid", "transaction exact state is invalid"
            ) from exc
        if (
            (record["before"] == "missing" and preimage.kind != "missing")
            or (
                record["before"] == "symlink"
                and (
                    preimage.kind != "symlink"
                    or preimage.link_target != record["target"]
                )
            )
        ):
            raise ExtensionError(
                "journal-invalid", "transaction preimage disagrees with prior state"
            )
        intended_post = (
            (expected_after is None and postimage.kind == "missing")
            or (
                expected_after is not None
                and postimage.kind == "symlink"
                and postimage.link_target == expected_after
            )
        )
        if postimage != preimage and not intended_post:
            raise ExtensionError(
                "journal-invalid", "transaction postimage is not prior or intended state"
            )
        checked.append(record)
    if sorted(record["runtime"] for record in checked) != runtimes:
        raise ExtensionError("journal-invalid", "transaction runtime records do not match")
    if (publisher, name) != (journal.get("publisher"), journal.get("name")):
        raise ExtensionError("journal-invalid", "transaction identity fields do not agree")
    return {
        "records": checked,
        "before": before,
        "before_raw": before_raw,
        "after": after,
        "after_raw": after_raw,
    }


def _restore_record(record: dict) -> None:
    destination = Path(record["dest"])
    try:
        preimage = safe_fs.PathState.from_public(record["preimage"])
        postimage = safe_fs.PathState.from_public(record["postimage"])
        current = safe_fs.capture_state(destination)
        if current == preimage:
            return
        if current != postimage:
            raise safe_fs.SafetyError(
                "concurrent-successor",
                destination,
                f"postimage={postimage.public()} current={current.public()}",
            )
        if preimage.kind == "missing":
            _remove_exact_leaf(
                destination,
                current,
                owner="extensions:rollback-created-runtime-projection",
            )
        elif preimage.kind == "symlink" and preimage.link_target is not None:
            _atomic_symlink(Path(preimage.link_target), destination, current)
        else:
            raise safe_fs.SafetyError(
                "ownership-unproved",
                destination,
                f"unsupported extension preimage kind: {preimage.kind}",
            )
        return
    except (safe_fs.SafetyError, ExtensionError) as exc:
        raise ExtensionError(
            "recovery-conflict", f"cannot restore transaction path: {destination}: {exc}"
        ) from exc


def _recover_journal_locked() -> bool:
    path = _journal_path()
    if path.is_symlink():
        raise ExtensionError("journal-symlink", f"transaction journal must not be a symlink: {path}")
    if not path.exists():
        return False
    try:
        journal_state = safe_fs.capture_state(path)
    except safe_fs.SafetyError as exc:
        raise ExtensionError("journal-invalid", "transaction journal cannot be sealed") from exc
    if journal_state.kind != "file":
        raise ExtensionError("journal-invalid", "transaction journal must be a regular file")
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtensionError("journal-invalid", "transaction journal cannot be read") from exc
    if not isinstance(journal, dict) or journal.get("schema_version") != 1:
        raise ExtensionError("journal-invalid", "transaction journal does not match schema v1")
    if safe_fs.capture_state(path) != journal_state:
        raise ExtensionError("journal-invalid", "transaction journal changed while reading")
    validated = _journal_records_valid(journal)
    records = validated["records"]
    registry_path = _registry_path()
    if registry_path.is_symlink():
        raise ExtensionError("recovery-conflict", "registry became a symlink during recovery")
    try:
        current_raw = registry_path.read_bytes() if registry_path.exists() else None
    except OSError as exc:
        raise ExtensionError("recovery-conflict", "current registry cannot be read") from exc
    current_hash = _registry_state_hash(current_raw)
    before_hash = journal["registry_before_hash"]
    after_hash = journal["registry_after_hash"]
    if journal.get("status") == "committed":
        if current_hash != after_hash:
            raise ExtensionError(
                "recovery-conflict", "committed journal does not match current registry"
            )
        _remove_exact_leaf(
            path,
            journal_state,
            owner="extensions:discard-committed-journal",
        )
        return True
    if journal.get("status") != "applying":
        raise ExtensionError("journal-invalid", "transaction journal status is invalid")
    if current_hash not in {before_hash, after_hash}:
        raise ExtensionError(
            "recovery-conflict", "current registry is neither transaction before nor after state"
        )
    for record in reversed(records):
        _restore_record(record)
    before = validated["before"]
    before_raw = validated["before_raw"]
    if before_raw is None:
        current_registry = safe_fs.capture_state(registry_path)
        _remove_exact_leaf(
            registry_path,
            current_registry,
            owner="extensions:restore-missing-registry",
        )
    else:
        _atomic_bytes(registry_path, before_raw)
    created = journal.get("created_snapshot")
    if created:
        publisher, name = parse_canonical_id(journal["canonical_id"])
        expected = _snapshot_path(validated["after"]["extensions"][journal["canonical_id"]])
        if created != str(expected):
            raise ExtensionError("journal-invalid", "transaction snapshot path is not canonical")
        used = any(
            _snapshot_path(entry) == expected for entry in before["extensions"].values()
        )
        if not used and expected.exists():
            # destructive-ok: reason=discard one transaction-created immutable snapshot; boundary=journal-sealed digest snapshot below extension data
            _writable_rmtree(expected)
    _remove_exact_leaf(
        path,
        journal_state,
        owner="extensions:discard-recovered-journal",
    )
    return True


def _new_entry(report: dict, projection: str, key: str, runtimes: list[str], old: dict | None) -> dict:
    now = _utc_now()
    manifest = report["manifest"]
    return {
        "publisher": manifest["publisher"],
        "name": manifest["name"],
        "physical_id": report["physical_id"],
        "source": report["source"],
        "source_checksum": report["source_checksum"],
        "projection_checksum": projection,
        "snapshot_key": key,
        "version": manifest["version"],
        "license": manifest["license"],
        "runtimes": sorted(runtimes),
        "runtime_roots": {
            runtime: str(
                _runtime_destination(runtime, report["physical_id"], create=False).parent.parent
            )
            for runtime in sorted(runtimes)
        },
        "created_at": old["created_at"] if old else now,
        "updated_at": now,
        "provenance": report["provenance"],
        "parity": report["parity"],
    }


def _assert_runtime_roots(entry: dict) -> None:
    for runtime in entry["runtimes"]:
        current = str(
            _runtime_destination(runtime, entry["physical_id"], create=False).parent.parent
        )
        if entry["runtime_roots"].get(runtime) != current:
            raise ExtensionError(
                "runtime-root-changed",
                f"{runtime} runtime root differs from the installation ownership lock",
            )


def _inactive_from_parity(parity: dict) -> list[str]:
    losses = set()
    for value in parity.values():
        for reason in value.get("loss_reasons", []):
            if isinstance(reason, str) and reason.startswith("inactive:"):
                losses.add(reason.split(":", 1)[1])
    return sorted(losses)


def _result_fields(
    *,
    report: Optional[dict],
    entry: dict,
    snapshot: Optional[Path],
) -> dict:
    runtimes = entry["runtimes"]
    return {
        "source_checksum": entry["source_checksum"],
        "projection_checksum": entry["projection_checksum"],
        "snapshot_key": entry["snapshot_key"],
        "snapshot": str(snapshot) if snapshot else None,
        "inactive_surfaces": (
            report["inactive_surfaces"] if report else _inactive_from_parity(entry["parity"])
        ),
        "next_session_action": {
            runtime: NEXT_SESSION_ACTION[runtime] for runtime in runtimes
        },
        "runtime_projection": {
            runtime: str(_runtime_destination(runtime, entry["physical_id"], create=False))
            for runtime in runtimes
        },
    }


def _write_journal(
    operation: str,
    report_or_entry: dict,
    records: list[dict],
    registry_before: dict,
    registry_before_raw: Optional[bytes],
    registry_after: dict,
    *,
    new_snapshot: Optional[Path],
    new_snapshot_key: Optional[str],
    created_snapshot: bool,
) -> dict:
    canonical = report_or_entry.get("canonical_id")
    if canonical:
        publisher = report_or_entry["manifest"]["publisher"]
        name = report_or_entry["manifest"]["name"]
        physical = report_or_entry["physical_id"]
    else:
        publisher = report_or_entry["publisher"]
        name = report_or_entry["name"]
        canonical = _canonical_id(publisher, name)
        physical = report_or_entry["physical_id"]
    journal = {
        "schema_version": 1,
        "status": "applying",
        "operation": operation,
        "canonical_id": canonical,
        "publisher": publisher,
        "name": name,
        "physical_id": physical,
        "runtimes": sorted(record["runtime"] for record in records),
        "records": records,
        "registry_before": json.loads(json.dumps(registry_before)),
        "registry_before_exists": registry_before_raw is not None,
        "registry_before_b64": (
            base64.b64encode(registry_before_raw).decode("ascii")
            if registry_before_raw is not None
            else None
        ),
        "registry_before_hash": _registry_state_hash(registry_before_raw),
        "registry_before_generation": registry_before["generation"],
        "registry_after": json.loads(json.dumps(registry_after)),
        "registry_after_hash": _registry_state_hash(_json_bytes(registry_after)),
        "registry_after_generation": registry_after["generation"],
        "new_snapshot": str(new_snapshot) if new_snapshot else None,
        "new_snapshot_key": new_snapshot_key,
        "created_snapshot": str(new_snapshot) if new_snapshot and created_snapshot else None,
    }
    _atomic_json(_journal_path(), journal)
    return journal


def _finish_transaction(journal: dict) -> None:
    journal["status"] = "committed"
    committed = _atomic_json(_journal_path(), journal)
    _remove_exact_leaf(
        _journal_path(),
        committed,
        owner="extensions:discard-committed-journal",
    )


# destructive-ok: reason=garbage-collect an unreferenced immutable snapshot; boundary=one validated digest snapshot below the snapshot root
def _cleanup_snapshot(entry: dict, registry: dict) -> None:
    candidate = _snapshot_path(entry)
    if any(_snapshot_path(item) == candidate for item in registry["extensions"].values()):
        return
    root = paths.extension_data_dir()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ExtensionError("unsafe-cleanup", "snapshot cleanup escaped data root") from exc
    if candidate.exists():
        _writable_rmtree(candidate)


def _selected_runtimes(values: list[str] | tuple[str, ...] | None) -> list[str]:
    raw = list(values or RUNTIMES)
    if "all" in raw:
        raw = list(RUNTIMES)
    if not raw or not set(raw).issubset(RUNTIMES):
        raise ExtensionError("runtime-invalid", "runtime selection is empty or invalid")
    return sorted(set(raw))


# destructive-ok: reason=discard uncommitted extension staging on failure; boundary=one transaction-created prepared snapshot directory
def add(source: str, runtimes: list[str] | None = None) -> dict:
    report = _inspect(source)
    selected = _selected_runtimes(runtimes)
    if report["status"] != "ready":
        raise ExtensionError("inspection-blocked", "extension inspection has blocking findings")
    with _mutation_lock():
        recovered = _recover_journal_locked()
        registry, registry_raw = _load_registry_state()
        canonical = report["canonical_id"]
        if canonical in registry["extensions"]:
            raise ExtensionError("already-installed", f"extension is already installed: {canonical}")
        harness_manifest.load()
        records = [
            _preflight_destination(
                runtime, report["physical_id"], expected=None, require_missing=True
            )
            for runtime in selected
        ]
        snapshot, projection, key, created, temporary = _prepare_snapshot(report)
        for record in records:
            record["expected_after"] = str(snapshot)
        entry = _new_entry(report, projection, key, selected, None)
        registry_after = json.loads(json.dumps(registry))
        registry_after["extensions"][canonical] = entry
        registry_after["generation"] += 1
        try:
            journal = _write_journal(
                "add",
                report,
                records,
                registry,
                registry_raw,
                registry_after,
                new_snapshot=snapshot,
                new_snapshot_key=key,
                created_snapshot=created,
            )
        except Exception:
            if temporary is not None and temporary.exists():
                _writable_rmtree(temporary)
            raise
        try:
            _commit_prepared_snapshot(temporary, snapshot, projection)
            for count, record in enumerate(records, 1):
                postimage = _atomic_symlink(
                    snapshot,
                    _create_record_parent(record, report["physical_id"]),
                    safe_fs.PathState.from_public(record["preimage"]),
                )
                record["postimage"] = postimage.public()
                _atomic_json(_journal_path(), journal)
                _after_link_change(count)
            _atomic_json(_registry_path(), registry_after)
            _finish_transaction(journal)
        except Exception:
            try:
                _recover_journal_locked()
            finally:
                if temporary is not None and temporary.exists():
                    _writable_rmtree(temporary)
            raise
        result = {
            "operation": "add",
            "changed": True,
            "recovered": recovered,
            "canonical_id": canonical,
            "physical_id": report["physical_id"],
            "runtimes": selected,
            "parity": report["parity"],
        }
        result.update(_result_fields(report=report, entry=entry, snapshot=snapshot))
        return result


# destructive-ok: reason=discard uncommitted extension staging on failure; boundary=one transaction-created prepared snapshot directory
def update(canonical_id: str, source: Optional[str] = None) -> dict:
    publisher, name = parse_canonical_id(canonical_id)
    with _mutation_lock():
        recovered = _recover_journal_locked()
        registry, registry_raw = _load_registry_state()
        old = registry["extensions"].get(canonical_id)
        if old is None:
            raise ExtensionError("not-installed", f"extension is not installed: {canonical_id}")
        _assert_runtime_roots(old)
        report = _inspect(source or old["source"])
        if report["canonical_id"] != canonical_id:
            raise ExtensionError("identity-changed", "update source canonical id differs from registry")
        if report["status"] != "ready":
            raise ExtensionError("inspection-blocked", "extension inspection has blocking findings")
        harness_manifest.load()
        old_snapshot = _snapshot_path(old)
        if (
            old_snapshot.is_symlink()
            or not old_snapshot.is_dir()
            or _projection_digest(old_snapshot) != old["projection_checksum"]
        ):
            raise ExtensionError("snapshot-drift", "owned extension snapshot is missing or modified")
        records = [
            _preflight_destination(
                runtime, old["physical_id"], expected=old_snapshot, require_missing=False
            )
            for runtime in old["runtimes"]
        ]
        snapshot, projection, key, created, temporary = _prepare_snapshot(report)
        if (
            key == old["snapshot_key"]
            and projection == old["projection_checksum"]
            and report["source_checksum"] == old["source_checksum"]
        ):
            result = {
                "operation": "update",
                "changed": False,
                "recovered": recovered,
                "canonical_id": canonical_id,
                "physical_id": old["physical_id"],
                "runtimes": old["runtimes"],
                "parity": report["parity"],
            }
            result.update(_result_fields(report=report, entry=old, snapshot=snapshot))
            return result
        for record in records:
            record["expected_after"] = str(snapshot)
        new_entry = _new_entry(report, projection, key, old["runtimes"], old)
        registry_after = json.loads(json.dumps(registry))
        registry_after["extensions"][canonical_id] = new_entry
        registry_after["generation"] += 1
        try:
            journal = _write_journal(
                "update",
                report,
                records,
                registry,
                registry_raw,
                registry_after,
                new_snapshot=snapshot,
                new_snapshot_key=key,
                created_snapshot=created,
            )
        except Exception:
            if temporary is not None and temporary.exists():
                _writable_rmtree(temporary)
            raise
        try:
            _commit_prepared_snapshot(temporary, snapshot, projection)
            for count, record in enumerate(records, 1):
                postimage = _atomic_symlink(
                    snapshot,
                    _create_record_parent(record, old["physical_id"]),
                    safe_fs.PathState.from_public(record["preimage"]),
                )
                record["postimage"] = postimage.public()
                _atomic_json(_journal_path(), journal)
                _after_link_change(count)
            _atomic_json(_registry_path(), registry_after)
            _finish_transaction(journal)
        except Exception:
            try:
                _recover_journal_locked()
            finally:
                if temporary is not None and temporary.exists():
                    _writable_rmtree(temporary)
            raise
        _cleanup_snapshot(old, registry_after)
        entry = registry_after["extensions"][canonical_id]
        result = {
            "operation": "update",
            "changed": True,
            "recovered": recovered,
            "canonical_id": canonical_id,
            "physical_id": old["physical_id"],
            "runtimes": old["runtimes"],
            "parity": report["parity"],
        }
        result.update(_result_fields(report=report, entry=entry, snapshot=snapshot))
        return result


# destructive-ok: reason=remove exact extension projections sealed in registry and journal; boundary=closed destination set for one canonical extension id
def remove(canonical_id: str) -> dict:
    parse_canonical_id(canonical_id)
    with _mutation_lock():
        recovered = _recover_journal_locked()
        registry, registry_raw = _load_registry_state()
        old = registry["extensions"].get(canonical_id)
        if old is None:
            raise ExtensionError("not-installed", f"extension is not installed: {canonical_id}")
        _assert_runtime_roots(old)
        old_snapshot = _snapshot_path(old)
        if (
            old_snapshot.is_symlink()
            or not old_snapshot.is_dir()
            or _projection_digest(old_snapshot) != old["projection_checksum"]
        ):
            raise ExtensionError("snapshot-drift", "owned extension snapshot is missing or modified")
        records = [
            _preflight_destination(
                runtime, old["physical_id"], expected=old_snapshot, require_missing=False
            )
            for runtime in old["runtimes"]
        ]
        for record in records:
            record["expected_after"] = None
        registry_after = json.loads(json.dumps(registry))
        del registry_after["extensions"][canonical_id]
        registry_after["generation"] += 1
        journal = _write_journal(
            "remove",
            old,
            records,
            registry,
            registry_raw,
            registry_after,
            new_snapshot=None,
            new_snapshot_key=None,
            created_snapshot=False,
        )
        try:
            for count, record in enumerate(records, 1):
                destination = Path(record["dest"])
                postimage = _remove_exact_leaf(
                    destination,
                    safe_fs.PathState.from_public(record["preimage"]),
                    owner="extensions:remove-owned-runtime-projection",
                )
                record["postimage"] = postimage.public()
                _atomic_json(_journal_path(), journal)
                _after_link_change(count)
            _atomic_json(_registry_path(), registry_after)
            _finish_transaction(journal)
        except Exception:
            _recover_journal_locked()
            raise
        _cleanup_snapshot(old, registry_after)
        result = {
            "operation": "remove",
            "changed": True,
            "recovered": recovered,
            "canonical_id": canonical_id,
            "physical_id": old["physical_id"],
            "runtimes": old["runtimes"],
            "parity": old["parity"],
        }
        result.update(_result_fields(report=None, entry=old, snapshot=None))
        return result
