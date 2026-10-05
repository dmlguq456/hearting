#!/usr/bin/env python3
"""Strict, runtime-neutral execution access request validation.

The module owns request meaning and receipt vocabulary.  Adapters own only the
spelling of their runtime flags (for example Codex ``--add-dir`` versus App
Server ``--writable-root``).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path, PurePath
import re
import stat
from typing import Iterable, Mapping


SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_ROOTS = 16
MAX_PATH_LENGTH = 4096
MAX_HOSTS = 32
MAX_TEXT_LENGTH = 500
MAX_JSON_DEPTH = 64
MAX_TASK_TARGET_SCRIPT_BYTES = 1024 * 1024

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "writable_roots",
        "read_roots",
        "network",
        "enforcement_required",
        "justification",
    }
)
_NETWORK_FIELDS = frozenset({"required", "reason", "hosts"})
_RUNTIMES = frozenset(
    {"codex-exec", "codex-app-server", "claude-cli", "claude-supervisor", "opencode"}
)
_PATH_BAD = re.compile(r"[\x00-\x1f\x7f,=]|[*?\[\]{}]")
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?\Z"
)
_PIPE_VALUE = re.compile(r"^[A-Za-z0-9._:;/-]+$")


class ExecutionAccessError(ValueError):
    """A typed refusal safe for a pre-launch CLI boundary."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class AccessContext:
    worktree: Path
    artifact_root: Path
    dispatch_state_root: Path
    agent_home: Path
    home: Path
    config_roots: tuple[Path, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        worktree: str | Path,
        artifact_root: str | Path,
        dispatch_state_root: str | Path,
        agent_home: str | Path,
        environ: Mapping[str, str] | None = None,
    ) -> "AccessContext":
        env = os.environ if environ is None else environ
        home = Path(env.get("HOME") or str(Path.home())).resolve(strict=False)
        roots: list[Path] = []
        for value in (
            env.get("CODEX_HOME") or str(home / ".codex"),
            env.get("CLAUDE_CONFIG_DIR") or str(home / ".claude"),
            env.get("XDG_CONFIG_HOME") or str(home / ".config"),
        ):
            path = Path(value).expanduser()
            if path.is_absolute():
                roots.append(path.resolve(strict=False))
        return cls(
            worktree=Path(worktree).resolve(strict=False),
            artifact_root=Path(artifact_root).resolve(strict=False),
            dispatch_state_root=Path(dispatch_state_root).resolve(strict=False),
            agent_home=Path(agent_home).resolve(strict=False),
            home=home,
            config_roots=tuple(_unique_paths(roots)),
        )


@dataclass(frozen=True)
class ExecutionAccessRequest:
    writable_roots: tuple[Path, ...]
    read_roots: tuple[Path, ...]
    network_required: bool
    network_reason: str
    network_hosts: tuple[str, ...]
    enforcement_required: str
    justification: tuple[tuple[str, str], ...]
    request_sha256: str
    source_path: Path


@dataclass(frozen=True)
class ParentGrant:
    writable_roots: tuple[Path, ...]
    read_roots: tuple[Path, ...] = ()
    network_allowed: bool = False
    boundary: str = "parent-effective-grant"
    sandbox: str = "unknown"
    file_enforcement: str = "unknown"
    network_enforcement: str = "unknown"


@dataclass(frozen=True)
class ResolvedTaskTargets:
    manifest_path: Path
    manifest_sha256: str
    selected_names: tuple[str, ...]
    writable_roots: tuple[Path, ...]


def _read_bounded_regular_file(path: Path, limit: int) -> bytes:
    """Read a small input without following a symlink or accepting a special file."""

    descriptor: int | None = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise OSError("input must be a non-symlink regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        after = os.fstat(descriptor)
        if (not stat.S_ISREG(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
            raise OSError("input changed while opening")
        if after.st_size > limit:
            raise OSError(f"input exceeds {limit} bytes")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise OSError(f"input exceeds {limit} bytes")
        return raw
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid",
            f"target manifest is not safely readable: {exc}",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _task_target_reference(route: Mapping[str, object]) -> tuple[tuple[str, ...], Path] | None:
    """Resolve only the explicit `루트 목록과 경로` input/table pair."""

    work_request = route.get("work_request")
    cwd_value = route.get("cwd")
    if not isinstance(work_request, dict) or not isinstance(cwd_value, str):
        return None
    text = work_request.get("text")
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "## 입력"), None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(lines))
                if lines[index].startswith("## ")), len(lines))
    input_lines = lines[start + 1:end]
    target_rows = [line for line in input_lines if line.startswith("- 루트 목록과 경로: ")]
    if not target_rows:
        return None
    if len(target_rows) != 1:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS target reference is ambiguous"
        )
    preview_rows = [line for line in input_lines if line.startswith("- 미리보기(사용자가 본 것): ")]
    if len(preview_rows) != 1:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS target requires one paired preview reference"
        )
    target = target_rows[0]
    match = re.fullmatch(
        r"- 루트 목록과 경로: ([A-Za-z0-9_./-]+) 의 ROOTS 표\(([^()]*)\)", target
    )
    if not match:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS target reference has an unsupported shape"
    )
    manifest_reference, names_text = match.groups()
    if manifest_reference != "previews/run_all.sh":
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS input is not the supported direct preview table"
        )
    names = tuple(part.strip() for part in names_text.split(","))
    if (not names or len(names) > MAX_ROOTS or any(not re.fullmatch(r"[A-Za-z0-9_-]+", n) for n in names)
            or len(names) != len(set(names))):
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS target names are ambiguous"
        )
    preview_match = re.fullmatch(
        r"- 미리보기\(사용자가 본 것\): ([A-Za-z0-9_./-]+)/previews/<루트>\.md",
        preview_rows[0],
    )
    if not preview_match:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "preview and ROOTS paths are not a supported pair"
        )
    preview_base = Path(preview_match.group(1))
    if preview_base.is_absolute() or ".." in preview_base.parts:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "preview reference must stay relative to its source root"
        )
    if preview_base.parts[:1] == (".agent_reports",):
        try:
            route_root_value = Path(str(route.get("artifact_root") or ""))
            if not route_root_value.is_absolute():
                raise ValueError("route artifact root must be absolute")
            canonical_root = route_root_value.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid",
                f"route artifact root is unavailable: {type(exc).__name__}",
            ) from exc
        relative_base = Path(*preview_base.parts[1:])
        manifest = canonical_root / relative_base / manifest_reference
        try:
            manifest.resolve(strict=False).relative_to(canonical_root)
        except ValueError as exc:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid", "ROOTS input escapes the canonical artifact root"
            ) from exc
    else:
        if preview_base.parts[:1] and preview_base.parts[0].startswith(".agent_reports"):
            raise ExecutionAccessError(
                "execution-access-target-input-invalid",
                "artifact-relative references must use the canonical .agent_reports prefix",
            )
        source_root = Path(cwd_value).expanduser().resolve(strict=False)
        manifest = source_root / preview_base / manifest_reference
        try:
            manifest.resolve(strict=False).relative_to(source_root)
        except ValueError as exc:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid", "ROOTS input escapes the route source directory"
            ) from exc
    return names, manifest


def read_roots_data(path: str | Path, selected_names: Iterable[str]) -> ResolvedTaskTargets:
    """Read only the delimited ROOTS data block; never execute its shell script."""

    manifest = Path(path).expanduser()
    if not manifest.is_absolute():
        manifest = manifest.absolute()
    try:
        resolved_manifest = manifest.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", f"ROOTS input path is invalid: {type(exc).__name__}"
        ) from exc
    raw = _read_bounded_regular_file(manifest, MAX_TASK_TARGET_SCRIPT_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS data is not UTF-8"
        ) from exc
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line == "done <<'ROOTS'"]
    if len(starts) != 1:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "expected one exact ROOTS data block"
        )
    start = starts[0] + 1
    try:
        end = lines.index("ROOTS", start)
    except ValueError as exc:
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "ROOTS data block is unterminated"
        ) from exc
    rows: dict[str, Path] = {}
    for line in lines[start:end]:
        if not line or line.startswith("#"):
            continue
        name, separator, root_text = line.partition("|")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not root_text:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid", "ROOTS row has an invalid shape"
            )
        if name in rows:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid", f"duplicate ROOTS row: {name}"
            )
        try:
            literal = Path(root_text)
            if not literal.is_absolute() or ".." in literal.parts:
                raise ValueError("root must be an exact absolute path")
            root = literal.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExecutionAccessError(
                "execution-access-target-input-invalid", f"invalid root for {name}: {exc}"
            ) from exc
        rows[name] = root
    names = tuple(selected_names)
    if len(names) != len(set(names)) or any(name not in rows for name in names):
        raise ExecutionAccessError(
            "execution-access-target-input-invalid", "a selected ROOTS name is missing or duplicated"
        )
    return ResolvedTaskTargets(
        manifest_path=resolved_manifest,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        selected_names=names,
        writable_roots=tuple(_unique_paths(rows[name] for name in names)),
    )


def resolve_task_targets(route: Mapping[str, object]) -> ResolvedTaskTargets | None:
    """Resolve targets named directly by a route's structured input section."""

    reference = _task_target_reference(route)
    if reference is None:
        return None
    names, manifest = reference
    return read_roots_data(manifest, names)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ExecutionAccessError("execution-access-cache-conflict", "cache path is a symlink")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ExecutionAccessError(
            "execution-access-cache-unavailable", f"cannot persist prepared request: {exc}"
        ) from exc


def _lab_run_root(route: Mapping[str, object], node: str) -> Path | None:
    """Read normal lab run storage from the existing inventory loader only."""

    if node != "owner" or route.get("capability") != "autopilot-lab":
        return None
    path = Path(__file__).resolve().parent / "compute-hosts.py"
    spec = importlib.util.spec_from_file_location("_execution_compute_hosts", path)
    if spec is None or spec.loader is None:
        raise ExecutionAccessError("execution-access-compute-inventory-invalid", "inventory loader unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.load_config()["run_root"]
    except module.ConfigError as exc:
        if exc.status in {"missing", "template"}:
            return None
        raise ExecutionAccessError("execution-access-compute-inventory-invalid", str(exc)) from exc


def prepare_task_request(
    route: Mapping[str, object], jobs: str | Path, *, node: str = "owner"
) -> Path | None:
    """Prepare named targets and lab run storage with the existing request schema."""

    # The explicit typed request keeps precedence over preview-table input.
    explicit = request_path(None)
    lab_owner = node == "owner" and route.get("capability") == "autopilot-lab"
    targets = None if lab_owner and explicit is not None else resolve_task_targets(route)
    run_root = _lab_run_root(route, node)
    if targets is None and run_root is None:
        return None
    route_id = route.get("route_id")
    route_hash = route.get("route_hash")
    if not isinstance(route_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", route_id):
        raise ExecutionAccessError("execution-access-route-invalid", "route id is missing or invalid")
    if not isinstance(route_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", route_hash):
        raise ExecutionAccessError("execution-access-route-invalid", "route hash is missing or invalid")
    state_root = Path(jobs).expanduser().resolve(strict=False).parent
    directory = state_root / "execution-access" / "routes" / route_id
    request_path_value = directory / "request.json"
    context = AccessContext.build(
        worktree=str(route.get("cwd") or ""),
        artifact_root=str(route.get("artifact_root") or ""),
        dispatch_state_root=state_root,
        agent_home=Path(__file__).resolve().parents[1],
    )
    supplied = load_request(explicit, context=context) if run_root is not None and explicit is not None else None
    writable = list(targets.writable_roots if targets else ())
    if run_root is not None:
        writable.append(run_root)
    if supplied is not None:
        writable.extend(supplied.writable_roots)
    roots = [str(path) for path in _unique_paths(writable)]
    justification = {root: "Directly named approved task target" for root in roots}
    if run_root is not None:
        justification[str(run_root)] = "Compute-hosts inventory run_root for lab resource work"
    if supplied is not None:
        justification.update(dict(supplied.justification))
    request = {
        "schema_version": SCHEMA_VERSION,
        "writable_roots": roots,
        "read_roots": [str(path) for path in supplied.read_roots] if supplied else [],
        "network": {
            "required": supplied.network_required if supplied else False,
            "reason": supplied.network_reason if supplied else "",
            "hosts": list(supplied.network_hosts) if supplied else [],
        },
        "enforcement_required": supplied.enforcement_required if supplied else "any",
        "justification": justification,
    }
    # Use the same validator as load_request before publishing a prepared file.
    # Inventory defaults never bypass broad-root, symlink or request limits.
    validated = _validate_request(request, source=request_path_value, context=context)
    request_bytes = _canonical_json_bytes(request)
    request_digest = validated.request_sha256
    binding = {
        "schema_version": 1,
        "route_id": route_id,
        "route_hash": route_hash,
        "artifact_root": str(Path(str(route.get("artifact_root") or "")).resolve(strict=False)),
        "manifest_path": str(targets.manifest_path) if targets else None,
        "manifest_sha256": targets.manifest_sha256 if targets else None,
        "selected_names": list(targets.selected_names) if targets else [],
        "writable_roots": roots,
        "request_sha256": request_digest,
    }
    binding_bytes = (json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    binding_path = directory / "binding.json"
    try:
        existing_request = _read_bounded_regular_file(request_path_value, MAX_REQUEST_BYTES) if request_path_value.exists() else None
        existing_binding = _read_bounded_regular_file(binding_path, MAX_REQUEST_BYTES) if binding_path.exists() else None
    except OSError as exc:
        raise ExecutionAccessError("execution-access-cache-conflict", "prepared request cache is unreadable") from exc
    if existing_request is not None or existing_binding is not None:
        if existing_request != request_bytes or existing_binding != binding_bytes:
            raise ExecutionAccessError(
                "execution-access-cache-conflict",
                "the route binding or execution access input changed after request preparation",
            )
        return request_path_value
    _atomic_write(request_path_value, request_bytes)
    _atomic_write(binding_path, binding_bytes)
    return request_path_value


@dataclass(frozen=True)
class ExecutionAccessGrant:
    request_sha256: str
    writable_roots: tuple[Path, ...]
    read_roots: tuple[Path, ...]
    additional_writable_roots: tuple[Path, ...]
    absorbed_writable_roots: tuple[Path, ...]
    network: str
    file_enforcement: str
    network_enforcement: str
    unmet: tuple[str, ...]
    source_path: Path | None = None


def request_path(
    cli_value: str | None, environ: Mapping[str, str] | None = None
) -> Path | None:
    """Resolve the sole request surface without touching the file."""

    env = os.environ if environ is None else environ
    value = (
        cli_value
        if cli_value is not None
        else env.get("AGENT_DISPATCH_EXECUTION_ACCESS_FILE")
    )
    return Path(value) if value is not None else None


def _safe_subject(value: object) -> str:
    raw = str(value)
    if raw and len(raw) <= 160 and _PIPE_VALUE.fullmatch(raw):
        return raw
    return "sha256-" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _reject(prefix: str, subject: object, detail: str) -> None:
    raise ExecutionAccessError(f"{prefix}:{_safe_subject(subject)}", detail)


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionAccessError(
                "execution-access-invalid-json", f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _json_depth_is_bounded(raw: bytes) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                in_string = False
            continue
        if byte == ord('"'):
            in_string = True
        elif byte in (ord("["), ord("{")):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return False
        elif byte in (ord("]"), ord("}")):
            depth = max(0, depth - 1)
    return True


def _read_request(path: Path) -> object:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise OSError("request must be a non-symlink regular file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise OSError("request must be a non-symlink regular file")
        if info.st_size > MAX_REQUEST_BYTES:
            raise OSError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = MAX_REQUEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_REQUEST_BYTES:
            raise OSError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExecutionAccessError(
            "execution-access-unreadable", f"request file is not safely readable: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not _json_depth_is_bounded(raw):
        raise ExecutionAccessError(
            "execution-access-invalid-json",
            f"request JSON exceeds maximum nesting depth {MAX_JSON_DEPTH}",
        )
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates)
    except ExecutionAccessError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ExecutionAccessError(
            "execution-access-invalid-json", "request file is not valid UTF-8 JSON"
        ) from exc


def _field_error(field: str, detail: str) -> None:
    raise ExecutionAccessError(
        f"execution-access-field-invalid:{_safe_subject(field)}", detail
    )


def _one_line(value: object, field: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        _field_error(field, f"{field} must be a string")
    if (not allow_empty and not value) or len(value) > MAX_TEXT_LENGTH:
        _field_error(field, f"{field} has invalid length")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        _field_error(field, f"{field} must be one line")
    return value


def _validate_path_text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_LENGTH:
        _reject("execution-access-path-invalid", value, "path must be a bounded string")
    if _URI.match(value) or value.startswith("~") or _PATH_BAD.search(value):
        _reject("execution-access-path-invalid", value, "path syntax is not allowed")
    pure = PurePath(value)
    if not pure.is_absolute() or ".." in pure.parts:
        _reject("execution-access-path-invalid", value, "path must be exact and absolute")
    return value


def _raw_path_list(value: object, field: str) -> list[Path]:
    if not isinstance(value, list):
        _field_error(field, f"{field} must be a list")
    if any(not isinstance(item, str) for item in value):
        _field_error(field, f"{field} items must be strings")
    return [Path(item) for item in value]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _proper_ancestor(candidate: Path, target: Path) -> bool:
    return candidate != target and _is_within(target, candidate)


def _is_top_level(path: Path) -> bool:
    return path == Path("/") or len(path.parts) == 2


def _broad_root(path: Path, context: AccessContext) -> bool:
    candidate = path.resolve(strict=False)
    if _is_top_level(candidate) or candidate == context.home:
        return True
    exact_forbidden = (
        context.dispatch_state_root,
        context.agent_home,
        *context.config_roots,
    )
    if candidate in exact_forbidden:
        return True
    sensitive = (
        context.home,
        context.dispatch_state_root,
        context.agent_home,
        context.worktree,
        context.artifact_root,
        *context.config_roots,
    )
    return any(_proper_ancestor(candidate, target) for target in sensitive)


def _resolve_paths(raw: list[Path]) -> list[tuple[Path, Path]]:
    resolved_by_literal: list[tuple[Path, Path]] = []
    for literal in raw:
        # Literal and resolved paths are independently subject to the format
        # and broad-root checks.  ``strict=False`` deliberately catches links
        # through existing prefixes while permitting an exact future leaf.
        _validate_path_text(str(literal))
        try:
            resolved = literal.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            _reject(
                "execution-access-path-invalid",
                literal,
                f"path cannot be resolved safely: {type(exc).__name__}",
            )
        _validate_path_text(str(resolved))
        resolved_by_literal.append((literal, resolved))

    return resolved_by_literal


def _validate_path_boundaries(
    resolved_by_literal: list[tuple[Path, Path]], context: AccessContext
) -> None:
    """Apply phase 6 symlink checks before phase 7 broad-root checks."""

    for literal, resolved in resolved_by_literal:
        for other_literal, other_resolved in resolved_by_literal:
            if literal == other_literal or not _proper_ancestor(other_literal, literal):
                continue
            if not _is_within(resolved, other_resolved):
                _reject(
                    "execution-access-path-symlink-escape",
                    literal,
                    "a declared descendant resolves outside its declared ancestor",
                )

    for literal, resolved in resolved_by_literal:
        if _broad_root(literal, context):
            _reject("execution-access-root-too-broad", literal, "literal root is too broad")
        if _broad_root(resolved, context):
            _reject("execution-access-root-too-broad", literal, "resolved root is too broad")


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    return [Path(value) for value in sorted({str(Path(path)) for path in paths})]


def _normalize_host(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 260:
        _field_error("network.hosts", "host must be a bounded string")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in value) or "," in value or "=" in value:
        _field_error("network.hosts", "host contains an unsafe delimiter")
    host = value
    port_text: str | None = None
    if value.startswith("["):
        match = re.fullmatch(r"\[([0-9A-Fa-f:]+)\](?::([0-9]{1,5}))?", value)
        if not match:
            _field_error("network.hosts", "invalid bracketed IPv6 host")
        try:
            host = f"[{ipaddress.IPv6Address(match.group(1)).compressed}]"
        except ipaddress.AddressValueError:
            _field_error("network.hosts", "invalid bracketed IPv6 host")
        port_text = match.group(2)
    elif value.count(":") <= 1:
        host, separator, candidate_port = value.partition(":")
        port_text = candidate_port if separator else None
        dotted = re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", host)
        if dotted:
            try:
                host = str(ipaddress.IPv4Address(host))
            except ipaddress.AddressValueError:
                _field_error("network.hosts", "invalid IPv4 host")
        elif not _HOSTNAME.fullmatch(host):
            _field_error("network.hosts", "invalid host")
        host = host.lower().rstrip(".")
    else:
        _field_error("network.hosts", "IPv6 hosts must be bracketed")
    canonical_port = ""
    if port_text is not None:
        if not re.fullmatch(r"[0-9]{1,5}", port_text):
            _field_error("network.hosts", "port must use ASCII decimal digits")
        port_number = int(port_text, 10)
        if not 1 <= port_number <= 65535:
            _field_error("network.hosts", "invalid port")
        canonical_port = str(port_number)
    return host + (f":{canonical_port}" if canonical_port else "")


def load_request(path: str | Path, *, context: AccessContext) -> ExecutionAccessRequest:
    """Read and validate one ``execution_access_v1`` request in spec order."""

    source = Path(path)
    data = _read_request(source)
    return _validate_request(data, source=source, context=context)


def _validate_request(
    data: object, *, source: Path, context: AccessContext
) -> ExecutionAccessRequest:
    """Shared validation for explicit files and normally prepared lab requests."""

    if not isinstance(data, dict):
        _field_error("request", "top-level JSON must be an object")

    version = data.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        rendered = version if type(version) is int else "unknown"
        raise ExecutionAccessError(
            f"execution-access-schema-unsupported:v{rendered}",
            "only execution_access_v1 schema_version=1 is supported",
        )

    unknown = next((key for key in data if key not in _TOP_LEVEL_FIELDS), None)
    if unknown is not None:
        _field_error(str(unknown), "unknown top-level field")
    for required in ("writable_roots", "read_roots", "network"):
        if required not in data:
            _field_error(required, "required field is missing")

    writable_raw = _raw_path_list(data["writable_roots"], "writable_roots")
    read_raw = _raw_path_list(data["read_roots"], "read_roots")

    network = data["network"]
    if not isinstance(network, dict):
        _field_error("network", "network must be an object")
    unknown_network = next((key for key in network if key not in _NETWORK_FIELDS), None)
    if unknown_network is not None:
        _field_error(f"network.{unknown_network}", "unknown network field")
    required = network.get("required", False)
    if type(required) is not bool:
        _field_error("network.required", "network.required must be boolean")
    reason = _one_line(network.get("reason", ""), "network.reason")
    hosts_value = network.get("hosts", [])
    if not isinstance(hosts_value, list) or len(hosts_value) > MAX_HOSTS:
        _field_error("network.hosts", f"network.hosts must contain at most {MAX_HOSTS} items")
    hosts = tuple(sorted(set(_normalize_host(host) for host in hosts_value)))
    if not required and (reason or hosts):
        _field_error("network", "reason/hosts require network.required=true")
    if required and not reason:
        _field_error("network.reason", "required network access needs a one-line reason")

    enforcement = data.get("enforcement_required", "any")
    if not isinstance(enforcement, str) or enforcement not in {"any", "os-sandbox"}:
        _field_error("enforcement_required", "expected any or os-sandbox")

    justification_value = data.get("justification", {})
    if not isinstance(justification_value, dict):
        _field_error("justification", "justification must be an object")
    justification_raw: list[tuple[Path, str]] = []
    for key, value in justification_value.items():
        if not isinstance(key, str):
            _field_error("justification", "justification keys must be paths")
        justification_raw.append(
            (Path(key), _one_line(value, f"justification.{key}", allow_empty=False))
        )

    # Phase ⑤ begins only after every unknown-key and type check in phase ④.
    if len(writable_raw) + len(read_raw) > MAX_ROOTS:
        _reject(
            "execution-access-path-invalid",
            "root-count",
            f"at most {MAX_ROOTS} total roots are supported",
        )
    for raw_path in (*writable_raw, *read_raw):
        _validate_path_text(str(raw_path))
    for raw_path, _ in justification_raw:
        _validate_path_text(str(raw_path))

    # Resolution and broad-root checks happen only after all phase ⑤ syntax
    # checks have passed, preserving first-failure semantics and partial grant 0.
    resolved_paths = _resolve_paths([*writable_raw, *read_raw])
    _validate_path_boundaries(resolved_paths, context)
    writable_count = len(writable_raw)
    writable = tuple(
        _unique_paths(resolved for _, resolved in resolved_paths[:writable_count])
    )
    read = tuple(
        _unique_paths(resolved for _, resolved in resolved_paths[writable_count:])
    )
    declared = set(writable) | set(read)
    justification: dict[str, str] = {}
    for raw_path, text in justification_raw:
        try:
            resolved = raw_path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            _reject(
                "execution-access-path-invalid",
                raw_path,
                f"justification path cannot be resolved safely: {type(exc).__name__}",
            )
        if resolved not in declared:
            _field_error("justification", "justification key is not a declared root")
        justification[str(resolved)] = text

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "writable_roots": [str(path) for path in writable],
        "read_roots": [str(path) for path in read],
        "network": {"required": required, "reason": reason, "hosts": list(hosts)},
        "enforcement_required": enforcement,
        "justification": dict(sorted(justification.items())),
    }
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    try:
        source_path = source.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExecutionAccessError(
            "execution-access-unreadable",
            f"request path cannot be resolved safely: {type(exc).__name__}",
        ) from exc
    return ExecutionAccessRequest(
        writable_roots=writable,
        read_roots=read,
        network_required=required,
        network_reason=reason,
        network_hosts=hosts,
        enforcement_required=enforcement,
        justification=tuple(sorted(justification.items())),
        request_sha256=digest,
        source_path=source_path,
    )


def _covered(path: Path, roots: Iterable[Path]) -> bool:
    return any(_is_within(path, root.resolve(strict=False)) for root in roots)


def assert_within_parent(
    request: ExecutionAccessRequest,
    parent: ParentGrant | None,
    *,
    is_child: bool,
) -> None:
    """Enforce child ⊆ parent without interpreting missing context as unlimited."""

    if not is_child:
        return
    if parent is None:
        raise ExecutionAccessError(
            "execution-access-exceeds-parent:parent-grant-unknown",
            "parent effective grant is unavailable; restart with the request at the top-level launch",
        )
    for root in request.writable_roots:
        if not _covered(root, parent.writable_roots):
            _reject(
                "execution-access-exceeds-parent",
                root,
                f"writable root exceeds {parent.boundary}; change the top-level launch request",
            )
    for root in request.read_roots:
        if not _covered(root, (*parent.read_roots, *parent.writable_roots)):
            _reject(
                "execution-access-exceeds-parent",
                root,
                f"read root exceeds {parent.boundary}; change the top-level launch request",
            )
    if request.network_required and not parent.network_allowed:
        raise ExecutionAccessError(
            "execution-access-exceeds-parent:network",
            f"network exceeds {parent.boundary}; change the top-level launch request",
        )


def build_grant(
    request: ExecutionAccessRequest,
    *,
    runtime: str,
    default_writable_roots: Iterable[str | Path] = (),
    network_available: bool = False,
    effective_sandbox: str = "workspace-write",
    gpu_resource_scope: bool = False,
    parent: ParentGrant | None = None,
) -> ExecutionAccessGrant:
    """Compute the effective explicit grant; never create runtime argv."""

    if runtime not in _RUNTIMES:
        raise ExecutionAccessError(
            f"execution-access-enforcement-unavailable:{_safe_subject(runtime)}",
            "runtime has no execution access projection",
        )
    defaults = tuple(Path(path).resolve(strict=False) for path in default_writable_roots)
    absorbed = tuple(root for root in request.writable_roots if _covered(root, defaults))
    additional = tuple(root for root in request.writable_roots if not _covered(root, defaults))

    if runtime.startswith("codex"):
        file_grade = "os-sandbox" if effective_sandbox == "workspace-write" else "none"
        network_grade = "os-sandbox" if effective_sandbox == "workspace-write" else "none"
    elif runtime.startswith("claude") or runtime == "opencode":
        file_grade = "tool-permission"
        network_grade = "none"
    else:  # pragma: no cover - guarded above
        file_grade = network_grade = "none"

    unmet: list[str] = []
    gpu_logical = (gpu_resource_scope and runtime.startswith("codex")
                   and effective_sandbox == "danger-full-access"
                   and request.enforcement_required == "any")
    # Nested foreground Codex children inside a workspace-write owner run with
    # danger-full-access to avoid nesting the mount sandbox; their writes remain
    # enforced by the outer parent sandbox. When the parent record carries
    # os-sandbox and the request is already proven within it, honor that outer
    # enforcement instead of refusing. GPU resource policy keeps its own
    # logical-request path (file_enforcement none); general scaffold children
    # must not inherit that policy.
    parent_inherited = (
        parent is not None
        and not gpu_resource_scope
        and runtime.startswith("codex")
        and effective_sandbox == "danger-full-access"
        and request.enforcement_required == "any"
        and parent.sandbox == "workspace-write"
        and parent.file_enforcement == "os-sandbox"
        and all(_covered(root, parent.writable_roots) for root in request.writable_roots)
        and all(
            _covered(root, (*parent.read_roots, *parent.writable_roots))
            for root in request.read_roots
        )
    )
    if parent_inherited:
        file_grade = "os-sandbox"
    if (request.writable_roots and runtime.startswith("codex") and file_grade == "none"
            and not gpu_logical and not parent_inherited):
        sandbox_subject = (
            "codex-read-only"
            if effective_sandbox == "read-only"
            else "codex-file-sandbox"
        )
        raise ExecutionAccessError(
            f"execution-access-enforcement-unavailable:{sandbox_subject}",
            "the effective Codex sandbox cannot project requested writable roots",
        )
    if request.read_roots:
        unmet.append("read-roots-unprojected")
    if (request.writable_roots or gpu_logical) and file_grade == "none":
        unmet.append("file-enforcement-none")
    if gpu_logical:
        unmet.append("network-enforcement-none")

    if request.network_required:
        if runtime.startswith("codex"):
            if not network_available or (network_grade != "os-sandbox" and not gpu_logical):
                raise ExecutionAccessError(
                    "execution-access-enforcement-unavailable:codex-network-role-gated",
                    "network is outside the current Codex launch policy; change the top-level launch request/role",
                )
            if gpu_logical:
                network = "granted-unenforced"
                unmet.append("network-unenforced-codex")
                if request.network_hosts:
                    unmet.append("network-hosts-unenforced")
            elif request.network_hosts:
                if request.enforcement_required == "os-sandbox":
                    raise ExecutionAccessError(
                        "execution-access-enforcement-unavailable:codex-network-hosts",
                        "Codex boolean network access cannot enforce the requested host allowlist",
                    )
                network = "granted-unenforced"
                unmet.append("network-hosts-unenforced")
            else:
                network = "enforced"
        else:
            network = "granted-unenforced"
            unmet.append(f"network-unenforced-{runtime.split('-', 1)[0]}")
    else:
        network = "not-requested"

    relevant_grades = []
    if request.writable_roots or request.read_roots:
        relevant_grades.append(file_grade)
    if request.network_required:
        relevant_grades.append(network_grade)
    if request.enforcement_required == "os-sandbox":
        if request.read_roots or any(grade != "os-sandbox" for grade in relevant_grades):
            raise ExecutionAccessError(
                f"execution-access-enforcement-unavailable:{runtime}",
                "the requested axes are not enforced by an OS sandbox on this runtime",
            )

    return ExecutionAccessGrant(
        request_sha256=request.request_sha256,
        writable_roots=request.writable_roots,
        read_roots=(),
        additional_writable_roots=additional,
        absorbed_writable_roots=absorbed,
        network=network,
        file_enforcement=file_grade,
        network_enforcement=network_grade,
        unmet=tuple(sorted(set(unmet))),
        source_path=request.source_path,
    )


def bind_request(
    cli_value: str | None,
    *,
    environ: Mapping[str, str] | None,
    context: AccessContext,
    is_child: bool,
    parent: ParentGrant | None,
    runtime: str,
    default_writable_roots: Iterable[str | Path] = (),
    network_available: bool = False,
    effective_sandbox: str = "workspace-write",
    gpu_resource_scope: bool = False,
) -> ExecutionAccessGrant | None:
    """Resolve, validate, constrain, and grade an explicit request.

    The ``None`` fast path is intentionally first and side-effect free so an
    absent request cannot alter legacy adapter assembly.
    """

    source = request_path(cli_value, environ)
    if source is None:
        return None
    request = load_request(source, context=context)
    assert_within_parent(request, parent, is_child=is_child)
    return build_grant(
        request,
        runtime=runtime,
        default_writable_roots=default_writable_roots,
        network_available=network_available,
        effective_sandbox=effective_sandbox,
        gpu_resource_scope=gpu_resource_scope,
        parent=parent,
    )


def _canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def publish_effective_grant(
    *,
    jobs: str | Path,
    attempt_id: str,
    route_id: str,
    route_hash: str,
    runtime: str,
    sandbox: str,
    grant: ExecutionAccessGrant | None,
    default_writable_roots: Iterable[str | Path],
    network_allowed: bool,
    execution_selection: Mapping[str, object] | None = None,
) -> tuple[Path, str]:
    """Publish the exact filesystem/network effect used by one attempt."""

    state_root = Path(jobs).expanduser().resolve(strict=False).parent
    if (not re.fullmatch(r"[A-Za-z0-9._-]+", attempt_id)
            or not re.fullmatch(r"[A-Za-z0-9._-]+", route_id)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", route_hash)):
        raise ExecutionAccessError("execution-access-attempt-invalid", "attempt route identity is incomplete")
    defaults = [Path(root).expanduser().resolve(strict=False) for root in default_writable_roots]
    requested = list(grant.writable_roots) if grant is not None else []
    writable = [str(path) for path in _unique_paths((*defaults, *requested))]
    read = [str(path) for path in _unique_paths(grant.read_roots if grant else ())]
    request_file = grant.source_path if grant is not None else None
    record = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "route_id": route_id,
        "route_hash": route_hash,
        "runtime": runtime,
        "sandbox": sandbox,
        "request_path": str(request_file) if request_file else None,
        "request_sha256": grant.request_sha256 if grant else None,
        "writable_roots": writable,
        "read_roots": read,
        "network_allowed": bool(network_allowed),
        "file_enforcement": grant.file_enforcement if grant else (
            "os-sandbox" if runtime.startswith("codex") and sandbox == "workspace-write"
            else "tool-permission" if runtime.startswith(("claude", "opencode")) else "none"
        ),
        "network_enforcement": grant.network_enforcement if grant else (
            "os-sandbox" if runtime.startswith("codex") and sandbox == "workspace-write" else "none"
        ),
    }
    if (grant is not None and runtime.startswith("codex")
            and sandbox == "danger-full-access" and grant.file_enforcement == "none"):
        record.update({"boundary": "logical-request", "unmet": list(grant.unmet),
                       "os_filesystem_enforced": False, "os_network_enforced": False})
        record["network_allowed"] = bool(network_allowed or grant.network == "granted-unenforced")
    if execution_selection is not None and execution_selection.get("gpu_scope") is True:
        record["execution_sandbox_selection"] = dict(execution_selection)
        if runtime.startswith("codex") and sandbox == "danger-full-access":
            record.update({"boundary": "logical-request" if grant else "logical-defaults",
                           "os_filesystem_enforced": False, "os_network_enforced": False})
    raw = _canonical_json_bytes(record)
    digest = hashlib.sha256(raw).hexdigest()
    path = state_root / "execution-access" / "attempts" / attempt_id / "effective.json"
    if not _is_within(path.resolve(strict=False), state_root):
        raise ExecutionAccessError("execution-access-record-outside-state", str(path))
    if path.exists():
        try:
            prior = _read_bounded_regular_file(path, MAX_REQUEST_BYTES)
        except ExecutionAccessError:
            raise
        if prior != raw:
            raise ExecutionAccessError(
                "execution-access-record-conflict", "attempt already has a different effective grant"
            )
    else:
        _atomic_write(path, raw)
    return path, digest


def _exact_attempt_metadata(jobs: str | Path, attempt_id: str) -> dict[str, str]:
    matches: list[dict[str, str]] = []
    try:
        lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ExecutionAccessError("execution-access-parent-row-unreadable", str(exc)) from exc
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6 or fields[1] not in {"open", "running"}:
            continue
        try:
            metadata = _pairs_no_duplicates([
                (part.split("=", 1)[0], part.split("=", 1)[1])
                for part in fields[5].split(",") if "=" in part
            ])
        except (ExecutionAccessError, IndexError):
            continue
        if metadata.get("attempt_id") == attempt_id:
            matches.append({str(key): str(value) for key, value in metadata.items()})
    if len(matches) != 1:
        raise ExecutionAccessError(
            "execution-access-parent-row-invalid", f"expected one live attempt row, found {len(matches)}"
        )
    return matches[0]


def load_parent_effective_grant(
    *,
    jobs: str | Path,
    parent_attempt_id: str,
    context: AccessContext,
) -> ParentGrant:
    """Load the effective record published by the exact live parent row."""

    state_root = Path(jobs).expanduser().resolve(strict=False).parent
    row = _exact_attempt_metadata(jobs, parent_attempt_id)
    path_value = row.get("execution_access_effective_file", "")
    digest_value = row.get("execution_access_effective_sha256", "")
    expected_path = state_root / "execution-access" / "attempts" / parent_attempt_id / "effective.json"
    if path_value != str(expected_path) or not re.fullmatch(r"[0-9a-f]{64}", digest_value):
        raise ExecutionAccessError(
            "execution-access-parent-record-missing", "live parent row has no canonical effective record"
        )
    try:
        raw = _read_bounded_regular_file(expected_path, MAX_REQUEST_BYTES)
    except ExecutionAccessError as exc:
        raise ExecutionAccessError("execution-access-parent-record-invalid", exc.detail) from exc
    if hashlib.sha256(raw).hexdigest() != digest_value:
        raise ExecutionAccessError(
            "execution-access-parent-record-digest-mismatch", "effective grant digest does not match live row"
        )
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ExecutionAccessError) as exc:
        raise ExecutionAccessError("execution-access-parent-record-invalid", "effective record is invalid JSON") from exc
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ExecutionAccessError("execution-access-parent-record-invalid", "effective record schema is unsupported")
    route_prefix = ""
    if (not row.get("route_id") and not row.get("route_hash")
            and row.get("dispatch_depth") == "1" and row.get("worker_type") == "owner"
            and row.get("unit") == "_kernel/owner" and row.get("owner_route_file")):
        route_prefix = "owner_"
    for key, row_key in (("attempt_id", "attempt_id"),
                         ("route_id", route_prefix + "route_id"),
                         ("route_hash", route_prefix + "route_hash")):
        if not isinstance(record.get(key), str) or not record.get(key) or record[key] != row.get(row_key):
            raise ExecutionAccessError("execution-access-parent-record-identity-mismatch", key)
    if record.get("runtime") not in _RUNTIMES or record.get("sandbox") != row.get("runtime_sandbox"):
        raise ExecutionAccessError("execution-access-parent-record-identity-mismatch", "runtime/sandbox")
    for key in ("writable_roots", "read_roots"):
        roots = record.get(key)
        if (not isinstance(roots, list) or any(not isinstance(root, str) for root in roots)
                or any(not Path(root).is_absolute() or str(Path(root).resolve(strict=False)) != root for root in roots)):
            raise ExecutionAccessError("execution-access-parent-record-invalid", f"invalid {key}")
    if type(record.get("network_allowed")) is not bool:
        raise ExecutionAccessError("execution-access-parent-record-invalid", "network_allowed must be boolean")
    request_path_value = record.get("request_path")
    request_digest = record.get("request_sha256")
    if request_path_value is not None:
        request = load_request(request_path_value, context=context)
        if request.request_sha256 != request_digest:
            raise ExecutionAccessError("execution-access-parent-request-changed", "request digest changed")
    elif request_digest is not None:
        raise ExecutionAccessError("execution-access-parent-record-invalid", "request digest has no request path")
    return ParentGrant(
        writable_roots=tuple(Path(root) for root in record["writable_roots"]),
        read_roots=tuple(Path(root) for root in record["read_roots"]),
        network_allowed=record["network_allowed"],
        sandbox=str(record.get("sandbox") or "unknown"),
        file_enforcement=str(record.get("file_enforcement") or "unknown"),
        network_enforcement=str(record.get("network_enforcement") or "unknown"),
    )


def receipt_fields(grant: ExecutionAccessGrant) -> dict[str, str]:
    """Return exactly the five pipe-safe registry facts required by SD-141."""

    enforcement = grant.file_enforcement
    if not grant.writable_roots and not grant.read_roots and grant.network != "not-requested":
        enforcement = grant.network_enforcement
    fields = {
        "execution_access_request": grant.request_sha256[:16],
        "execution_access_roots": str(len(grant.writable_roots)),
        "execution_access_network": grant.network,
        "execution_access_enforcement": enforcement,
        "execution_access_unmet": ";".join(grant.unmet) if grant.unmet else "none",
    }
    for key, value in fields.items():
        if not _PIPE_VALUE.fullmatch(value):
            raise ExecutionAccessError(
                "execution-access-receipt-unsafe", f"unsafe receipt value for {key}"
            )
    return fields


def receipt_fragment(grant: ExecutionAccessGrant | None) -> str:
    """Render the optional canonical registry suffix."""

    if grant is None:
        return ""
    return "".join(f",{key}={value}" for key, value in receipt_fields(grant).items())


def adapter_default_roots(args: object, *roots: Iterable[str | Path]) -> tuple[Path, ...]:
    """Flatten adapter-computed defaults for absorption tests/builders."""

    values: list[Path] = []
    worktree = getattr(args, "worktree", None)
    if worktree:
        values.append(Path(worktree).resolve(strict=False))
    for group in roots:
        values.extend(Path(value).resolve(strict=False) for value in group)
    artifact = getattr(args, "artifact_root", None)
    if artifact:
        values.append(Path(artifact).resolve(strict=False))
    report = getattr(args, "report_bundle_root", None)
    if report:
        values.append(Path(report).resolve(strict=False))
    return tuple(_unique_paths(values))
