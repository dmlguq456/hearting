#!/usr/bin/env python3
"""Strict, runtime-neutral execution access request validation.

The module owns request meaning and receipt vocabulary.  Adapters own only the
spelling of their runtime flags (for example Codex ``--add-dir`` versus App
Server ``--writable-root``).
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
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


def _resolve_request_path(path: Path) -> Path:
    """``Path.resolve(strict=False)`` that still fails on a symlink loop.

    Python 3.13 stopped raising ``RuntimeError`` for a loop in non-strict mode
    and returns the unresolved path instead, so a loop would pass as an exact
    future leaf.  A fully resolved path can never fail with ``ELOOP``.
    """
    resolved = path.resolve(strict=False)
    try:
        os.stat(resolved)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError(f"symlink loop from {str(path)!r}") from exc
    return resolved


def _resolve_paths(raw: list[Path]) -> list[tuple[Path, Path]]:
    resolved_by_literal: list[tuple[Path, Path]] = []
    for literal in raw:
        # Literal and resolved paths are independently subject to the format
        # and broad-root checks.  ``strict=False`` deliberately catches links
        # through existing prefixes while permitting an exact future leaf.
        _validate_path_text(str(literal))
        try:
            resolved = _resolve_request_path(literal)
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
            resolved = _resolve_request_path(raw_path)
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
    if request.writable_roots and runtime.startswith("codex") and file_grade == "none":
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
    if request.writable_roots and file_grade == "none":
        unmet.append("file-enforcement-none")

    if request.network_required:
        if runtime.startswith("codex"):
            if not network_available or network_grade != "os-sandbox":
                raise ExecutionAccessError(
                    "execution-access-enforcement-unavailable:codex-network-role-gated",
                    "network is outside the current Codex launch policy; change the top-level launch request/role",
                )
            if request.network_hosts:
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
