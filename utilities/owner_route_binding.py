#!/usr/bin/env python3
"""Validate the node-less route binding carried by a depth-1 owner."""

from __future__ import annotations

from dataclasses import dataclass
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "capability_route_owner_binding", ROOT / "utilities" / "capability-route.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("capability-route loader unavailable")
ROUTE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ROUTE)


class OwnerRouteBindingError(ValueError):
    pass


OWNER_ROUTE_ATTACHMENT_SCHEMA_VERSION = 1
OWNER_ROUTE_ATTACHMENT_DIR = "owner-route-bindings"
OWNER_ROUTE_ADVANCE_SCHEMA_VERSION = 2
OWNER_ROUTE_ADVANCE_DIR = "owner-route-advances"


@dataclass(frozen=True)
class OwnerRouteBinding:
    route_file: str
    route_id: str
    route_hash: str


@dataclass(frozen=True)
class OwnerRouteAttachment:
    owner_attempt_id: str
    route_file: str
    route_id: str
    route_hash: str
    generation: int
    route_family_key: str
    worktree: str
    repo: str
    capability: str
    capability_mode: str
    artifact_root: str
    parent_session_id: str
    owner_harness: str
    record_id: str
    path: str


@dataclass(frozen=True)
class OwnerRouteAdvance:
    owner_attempt_id: str
    from_route_file: str
    from_route_id: str
    from_route_hash: str
    to_route_file: str
    to_route_id: str
    to_route_hash: str
    from_generation: int
    to_generation: int
    route_family_key: str
    record_id: str
    path: str


def _attachment_root(jobs: str | Path) -> Path:
    path = Path(jobs).expanduser()
    if not path.is_absolute():
        raise OwnerRouteBindingError("owner-route-jobs-path-must-be-absolute")
    return path.resolve(strict=False).parent / OWNER_ROUTE_ATTACHMENT_DIR


def _advance_root(jobs: str | Path) -> Path:
    path = Path(jobs).expanduser()
    if not path.is_absolute():
        raise OwnerRouteBindingError("owner-route-jobs-path-must-be-absolute")
    return path.resolve(strict=False).parent / OWNER_ROUTE_ADVANCE_DIR


@contextlib.contextmanager
def _jobs_lock(jobs: str | Path):
    """Hold the same lock used by registry writers for an exact proof."""
    path = Path(jobs).expanduser()
    if not path.is_absolute() or not path.name:
        raise OwnerRouteBindingError("owner-route-jobs-unreadable")
    lock = Path(f"{path}.lock")
    try:
        with lock.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield path.resolve(strict=False)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise OwnerRouteBindingError("owner-route-jobs-unreadable") from exc


def _registry_snapshot(jobs: Path) -> list[tuple[list[str], dict[str, str]]]:
    try:
        lines = jobs.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise OwnerRouteBindingError("owner-route-jobs-unreadable") from exc
    rows = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        rows.append((fields, meta))
    return rows


def _owner_snapshot(jobs: Path, attempt: str, *,
                    rows: list[tuple[list[str], dict[str, str]]] | None = None
                    ) -> tuple[list[str], dict[str, str]]:
    matches = [
        row for row in (rows if rows is not None else _registry_snapshot(jobs))
        if row[1].get("attempt_id") == attempt
    ]
    if len(matches) != 1:
        raise OwnerRouteBindingError("owner-route-owner-row-not-unique")
    return matches[0]


def _row_binding(meta: dict[str, str]) -> OwnerRouteBinding | None:
    values = (
        meta.get("owner_route_file", ""),
        meta.get("owner_route_id", ""),
        meta.get("owner_route_hash", ""),
    )
    if not any(values):
        return None
    if not all(values):
        raise OwnerRouteBindingError("owner-route-owner-binding-incomplete")
    return OwnerRouteBinding(str(Path(values[0]).resolve()), values[1], values[2])


def _registered_owner_attempt(environ: dict[str, str], *, attachment: bool = False) -> str:
    """Return the exact registered owner attempt, or empty for a non-owner.

    Old launch-bound owners can be recognized by their complete owner-route
    tuple.  A route-less post-launch attachment has no such compatibility
    marker, so it requires the complete typed wrapper identity.
    """
    attempt = environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if not attempt:
        return ""
    owner_context = any(environ.get(key, "") for key in (
        "AGENT_OWNER_ROUTE_FILE", "AGENT_OWNER_ROUTE_ID", "AGENT_OWNER_ROUTE_HASH"
    ))
    typed = {
        "AGENT_DISPATCH_WORKER_TYPE": "owner",
        "AGENT_DISPATCH_DEPTH": "1",
        "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION": "2",
        "AGENT_DISPATCH_EXECUTION_SURFACE": "registered-headless",
        "AGENT_DISPATCH_REGISTERED_WORKER": "1",
    }
    present = any(environ.get(key, "") for key in typed)
    worker_type = environ.get("AGENT_DISPATCH_WORKER_TYPE", "")
    if worker_type and worker_type != "owner":
        return ""
    if not present:
        return attempt if owner_context and not attachment else ""
    for key, expected in typed.items():
        if environ.get(key, "") != expected:
            raise OwnerRouteBindingError(
                f"owner-route-environment-{key.removeprefix('AGENT_DISPATCH_').lower().replace('_', '-')}-mismatch"
            )
    return attempt


def _owner_row_proof(fields: list[str], meta: dict[str, str], *, route: dict,
                     environ: dict[str, str], binding_mode: str = "any",
                     binding: OwnerRouteBinding | None = None) -> None:
    sealed = _row_binding(meta)
    if binding_mode == "sealed" and sealed != binding:
        raise OwnerRouteBindingError("owner-route-owner-binding-mismatch")
    if binding_mode == "unbound" and sealed is not None:
        raise OwnerRouteBindingError("owner-route-owner-already-bound")
    raw_worktree = str(route.get("cwd") or "")
    if not raw_worktree:
        raise OwnerRouteBindingError("owner-route-owner-worktree-mismatch")
    expected_worktree = str(Path(raw_worktree).resolve(strict=False))
    expected_repo = ""
    try:
        expected_repo = subprocess.check_output(
            ["git", "-C", expected_worktree, "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        # Hermetic callers may provide an explicit sealed repository identity.
        expected_repo = str(route.get("repo") or "")
    if fields[3] != expected_worktree:
        raise OwnerRouteBindingError("owner-route-owner-worktree-mismatch")
    if expected_repo and str(Path(fields[2]).resolve(strict=False)) != str(Path(expected_repo).resolve(strict=False)):
        raise OwnerRouteBindingError("owner-route-owner-repo-mismatch")
    required = {
        "worker_type": "owner", "unit": "_kernel/owner", "attempt_schema_version": "2",
        "dispatch_depth": "1", "registered_worker": "1",
        "execution_surface": "registered-headless",
        "capability": str(route.get("capability") or ""),
        "capability_mode": str(route.get("capability_mode") or ""),
        "artifact_root": str(Path(route.get("artifact_root", "")).resolve(strict=False)),
    }
    if route.get("effective_intensity"):
        required["intensity"] = str(route["effective_intensity"])
    for key, value in required.items():
        if not value or meta.get(key) != value:
            raise OwnerRouteBindingError(f"owner-route-owner-{key.replace('_', '-')}-mismatch")
    harness = environ.get("AGENT_DISPATCH_OWNER_HARNESS") or environ.get("AGENT_DISPATCH_CURRENT_HARNESS")
    if harness and meta.get("owner_harness") != harness:
        raise OwnerRouteBindingError("owner-route-owner-harness-mismatch")
    # The wrapper exports the launch parent's exact session independently of a
    # runtime thread that may advance during continuation.
    parent_session = environ.get("AGENT_DISPATCH_PARENT_SESSION_ID", "")
    if parent_session and meta.get("parent_sid") != parent_session:
        raise OwnerRouteBindingError("owner-route-owner-session-mismatch")
    # `parent_sid` is the session that launched the registered owner.  A
    # continuation's runtime_lineage.thread_id describes execution *inside*
    # that owner and may legitimately advance or fork, so it is a separate axis
    # and must never be compared to the launch-parent session.


def _advance_key(owner_attempt_id: str, route_id: str, route_hash: str) -> str:
    """Stable predecessor directory; candidates inside are target-keyed.

    A continuation route is only a candidate until an exact registered child
    starts on it.  Keeping every candidate under one predecessor directory lets
    an abandoned compile remain inert while two actually started successors
    are still detected as a real conflict.  Resolution never scans globally.
    """
    digest = hashlib.sha256(
        f"{owner_attempt_id}\0{route_id}\0{route_hash}".encode()
    ).hexdigest()
    return digest


def _advance_target_key(route_id: str, route_hash: str) -> str:
    return hashlib.sha256(f"{route_id}\0{route_hash}".encode()).hexdigest() + ".json"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError): os.unlink(name)


def _attachment_key(owner_attempt_id: str) -> str:
    return hashlib.sha256(owner_attempt_id.encode()).hexdigest() + ".json"


def _attachment_payload(owner_attempt_id: str, binding: OwnerRouteBinding, *,
                        generation: int, route_family_key: str, worktree: str,
                        repo: str, capability: str, capability_mode: str,
                        artifact_root: str, parent_session_id: str,
                        owner_harness: str) -> dict:
    if generation != 0:
        raise OwnerRouteBindingError("owner-route-attachment-generation-invalid")
    identity = {
        "schema_version": OWNER_ROUTE_ATTACHMENT_SCHEMA_VERSION,
        "owner_attempt_id": owner_attempt_id,
        "route_file": binding.route_file,
        "route_id": binding.route_id,
        "route_hash": binding.route_hash,
        "generation": generation,
        "route_family_key": route_family_key,
        "worktree": worktree,
        "repo": repo,
        "capability": capability,
        "capability_mode": capability_mode,
        "artifact_root": artifact_root,
        "parent_session_id": parent_session_id,
        "owner_harness": owner_harness,
    }
    if not owner_attempt_id or not all(isinstance(value, str) and value for key, value in identity.items()
                                       if key not in {"schema_version", "generation"}):
        raise OwnerRouteBindingError("owner-route-attachment-context-incomplete")
    identity["record_id"] = "sha256:" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**identity, "published_at": time.time()}


def _attachment_from_payload(payload: dict, path: Path) -> OwnerRouteAttachment:
    if not isinstance(payload, dict) or payload.get("schema_version") != OWNER_ROUTE_ATTACHMENT_SCHEMA_VERSION:
        raise OwnerRouteBindingError("owner-route-attachment-record-invalid")
    expected = dict(payload)
    expected.pop("record_id", None); expected.pop("published_at", None)
    record_id = "sha256:" + hashlib.sha256(
        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if payload.get("record_id") != record_id:
        raise OwnerRouteBindingError("owner-route-attachment-record-invalid")
    keys = (
        "owner_attempt_id", "route_file", "route_id", "route_hash", "generation",
        "route_family_key", "worktree", "repo", "capability", "capability_mode",
        "artifact_root", "parent_session_id", "owner_harness", "record_id",
    )
    if any(key not in payload for key in keys):
        raise OwnerRouteBindingError("owner-route-attachment-record-invalid")
    string_keys = tuple(key for key in keys if key not in {"generation"})
    if (
        any(not isinstance(payload[key], str) or not payload[key] for key in string_keys)
        or isinstance(payload["generation"], bool)
        or payload["generation"] != 0
        or not Path(payload["route_file"]).is_absolute()
        or not Path(payload["worktree"]).is_absolute()
        or not Path(payload["repo"]).is_absolute()
        or not Path(payload["artifact_root"]).is_absolute()
    ):
        raise OwnerRouteBindingError("owner-route-attachment-record-invalid")
    return OwnerRouteAttachment(**{key: payload[key] for key in keys}, path=str(path))


def publish_owner_route_attachment(jobs: str | Path, *, owner_attempt_id: str,
                                   binding: OwnerRouteBinding, generation: int,
                                   route_family_key: str, worktree: str, repo: str,
                                   capability: str, capability_mode: str,
                                   artifact_root: str, parent_session_id: str,
                                   owner_harness: str) -> OwnerRouteAttachment:
    """Atomically attach the first immutable route to one exact owner attempt."""
    payload = _attachment_payload(
        owner_attempt_id, binding, generation=generation,
        route_family_key=route_family_key, worktree=worktree, repo=repo,
        capability=capability, capability_mode=capability_mode,
        artifact_root=artifact_root, parent_session_id=parent_session_id,
        owner_harness=owner_harness,
    )
    root = _attachment_root(jobs); root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / _attachment_key(owner_attempt_id)
    lock_path = root / ".lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                attachment = _attachment_from_payload(existing, path)
            except (OSError, ValueError, TypeError) as exc:
                raise OwnerRouteBindingError("owner-route-attachment-record-invalid") from exc
            if attachment.record_id != payload["record_id"]:
                raise OwnerRouteBindingError("owner-route-attachment-competing-route")
            payload = existing
        else:
            _atomic_json(path, payload)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return _attachment_from_payload(payload, path)


def _advance_payload(owner_attempt_id: str, source: OwnerRouteBinding,
                     target: OwnerRouteBinding, *, from_generation: int,
                     to_generation: int, route_family_key: str = "") -> dict:
    if to_generation != from_generation + 1:
        raise OwnerRouteBindingError("owner-route-advance-generation-invalid")
    identity = {
        "schema_version": OWNER_ROUTE_ADVANCE_SCHEMA_VERSION,
        "owner_attempt_id": owner_attempt_id,
        "from_route_file": source.route_file, "from_route_id": source.route_id,
        "from_route_hash": source.route_hash,
        "to_route_file": target.route_file, "to_route_id": target.route_id,
        "to_route_hash": target.route_hash,
        "from_generation": from_generation, "to_generation": to_generation,
        "route_family_key": route_family_key,
    }
    identity["record_id"] = "sha256:" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**identity, "published_at": time.time()}


def _advance_record_id(payload: dict) -> str:
    identity = dict(payload)
    identity.pop("record_id", None)
    identity.pop("published_at", None)
    return "sha256:" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _advance_from_payload(payload: dict, path: Path) -> OwnerRouteAdvance:
    keys = (
        "owner_attempt_id", "from_route_file", "from_route_id", "from_route_hash",
        "to_route_file", "to_route_id", "to_route_hash", "from_generation",
        "to_generation", "route_family_key", "record_id",
    )
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != OWNER_ROUTE_ADVANCE_SCHEMA_VERSION
        or any(key not in payload for key in keys)
        or payload.get("record_id") != _advance_record_id(payload)
    ):
        raise OwnerRouteBindingError("owner-route-advance-record-invalid")
    string_keys = tuple(
        key for key in keys
        if key not in {"from_generation", "to_generation"}
    )
    if (
        any(not isinstance(payload[key], str) for key in string_keys)
        or any(not payload[key] for key in string_keys if key != "route_family_key")
        or any(
            isinstance(payload[key], bool) or not isinstance(payload[key], int)
            for key in ("from_generation", "to_generation")
        )
        or payload["from_generation"] < 0
        or payload["to_generation"] != payload["from_generation"] + 1
        or not Path(payload["from_route_file"]).is_absolute()
        or not Path(payload["to_route_file"]).is_absolute()
    ):
        raise OwnerRouteBindingError("owner-route-advance-record-invalid")
    return OwnerRouteAdvance(
        **{key: payload[key] for key in keys}, path=str(path)
    )


def publish_owner_route_advance(jobs: str | Path, *, owner_attempt_id: str,
                                source: OwnerRouteBinding, target: OwnerRouteBinding,
                                from_generation: int, to_generation: int,
                                route_family_key: str = "") -> OwnerRouteAdvance:
    """Atomically publish one immutable predecessor -> successor candidate."""
    if not owner_attempt_id or not isinstance(owner_attempt_id, str):
        raise OwnerRouteBindingError("owner-route-attempt-id-invalid")
    payload = _advance_payload(owner_attempt_id, source, target,
                               from_generation=from_generation,
                               to_generation=to_generation,
                               route_family_key=route_family_key)
    root = _advance_root(jobs); root.mkdir(mode=0o700, parents=True, exist_ok=True)
    predecessor = root / _advance_key(
        owner_attempt_id, source.route_id, source.route_hash
    )
    predecessor.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = predecessor / _advance_target_key(target.route_id, target.route_hash)
    lock_path = root / ".lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                existing_record = _advance_from_payload(existing, path)
            except (OSError, ValueError, TypeError) as exc:
                raise OwnerRouteBindingError("owner-route-advance-record-invalid") from exc
            if existing_record.record_id == payload["record_id"]:
                payload = existing
            else:
                raise OwnerRouteBindingError("owner-route-advance-record-invalid")
        else:
            _atomic_json(path, payload)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return _advance_from_payload(payload, path)


def _verified_binding(binding: OwnerRouteBinding, *, expected_cwd: str | None = None) -> tuple[dict, dict]:
    try:
        raw = json.loads(Path(binding.route_file).read_text(encoding="utf-8"))
        verified = ROUTE.verify_route(raw, expected_cwd=expected_cwd)
    except (OSError, ValueError, TypeError) as exc:
        raise OwnerRouteBindingError("owner-route-binding-route-invalid") from exc
    if (verified.get("route_id"), verified.get("route_hash")) != (
        binding.route_id, binding.route_hash
    ):
        raise OwnerRouteBindingError("owner-route-binding-hash-mismatch")
    return raw, verified


def _resolve_attachment_locked(jobs: Path, owner_attempt_id: str,
                               fields: list[str], meta: dict[str, str]) -> OwnerRouteBinding | None:
    path = _attachment_root(jobs) / _attachment_key(owner_attempt_id)
    if not path.exists():
        return None
    try:
        attachment = _attachment_from_payload(
            json.loads(path.read_text(encoding="utf-8")), path
        )
    except (OSError, ValueError, TypeError) as exc:
        raise OwnerRouteBindingError("owner-route-attachment-record-invalid") from exc
    if attachment.owner_attempt_id != owner_attempt_id or attachment.generation != 0:
        raise OwnerRouteBindingError("owner-route-attachment-record-invalid")
    binding = OwnerRouteBinding(
        str(Path(attachment.route_file).resolve()),
        attachment.route_id,
        attachment.route_hash,
    )
    _, route = _verified_binding(binding, expected_cwd=attachment.worktree)
    route_checks = (
        (int(route.get("advance_generation") or 0) == 0,
         "owner-route-attachment-generation-invalid"),
        (not route.get("source_route_supersession"),
         "owner-route-attachment-source-invalid"),
        (route.get("owner_attempt_id") == owner_attempt_id,
         "owner-route-attachment-owner-mismatch"),
        (route.get("route_family_key") == attachment.route_family_key,
         "owner-route-attachment-family-mismatch"),
        (str(Path(route.get("cwd", "")).resolve(strict=False)) == attachment.worktree,
         "owner-route-attachment-worktree-mismatch"),
        (route.get("capability") == attachment.capability,
         "owner-route-attachment-capability-mismatch"),
        (route.get("capability_mode") == attachment.capability_mode,
         "owner-route-attachment-mode-mismatch"),
        (str(Path(route.get("artifact_root", "")).resolve(strict=False)) == attachment.artifact_root,
         "owner-route-attachment-artifact-root-mismatch"),
    )
    for valid, reason in route_checks:
        if not valid:
            raise OwnerRouteBindingError(reason)
    _owner_row_proof(
        fields, meta, route=route,
        environ={
            "AGENT_DISPATCH_PARENT_SESSION_ID": attachment.parent_session_id,
            "AGENT_DISPATCH_OWNER_HARNESS": attachment.owner_harness,
        },
        binding_mode="unbound",
    )
    if str(Path(fields[3]).resolve(strict=False)) != attachment.worktree:
        raise OwnerRouteBindingError("owner-route-attachment-worktree-mismatch")
    if str(Path(fields[2]).resolve(strict=False)) != attachment.repo:
        raise OwnerRouteBindingError("owner-route-attachment-repo-mismatch")
    if meta.get("parent_sid", "") != attachment.parent_session_id:
        raise OwnerRouteBindingError("owner-route-attachment-session-mismatch")
    if meta.get("owner_harness", "") != attachment.owner_harness:
        raise OwnerRouteBindingError("owner-route-attachment-harness-mismatch")
    return binding


def resolve_owner_route_lifecycle(jobs: str | Path, *, owner_attempt_id: str,
                                  sealed_binding: OwnerRouteBinding | None = None,
                                  sealed_generation: int | None = None
                                  ) -> tuple[OwnerRouteBinding | None, str]:
    """Resolve launch binding or post-launch attachment, then verified advances."""
    canonical_jobs = Path(jobs).expanduser()
    if not canonical_jobs.is_absolute() or not canonical_jobs.is_file():
        raise OwnerRouteBindingError("owner-route-jobs-unreadable")
    canonical_jobs = canonical_jobs.resolve(strict=False)
    # Registry writers replace a complete row while holding jobs.log.lock.
    # Readers intentionally take one unlocked snapshot: reconcile callbacks can
    # already hold that same external lock, and re-locking here deadlocks. The
    # attachment/advance files themselves are atomic write-once records, so a
    # before/after snapshot is safe and the next Fleet tick observes the other
    # side of a concurrent transition.
    rows = _registry_snapshot(canonical_jobs)
    fields, meta = _owner_snapshot(
        canonical_jobs, owner_attempt_id, rows=rows
    )
    row_binding = _row_binding(meta)
    if sealed_binding is not None and row_binding != sealed_binding:
        raise OwnerRouteBindingError("owner-route-owner-binding-mismatch")
    attachment_path = _attachment_root(canonical_jobs) / _attachment_key(owner_attempt_id)
    if row_binding is not None:
        if attachment_path.exists():
            raise OwnerRouteBindingError("owner-route-attachment-conflicts-sealed-binding")
        anchor = row_binding
        anchor_status = "owner-route-launch-binding"
    else:
        anchor = _resolve_attachment_locked(canonical_jobs, owner_attempt_id, fields, meta)
        anchor_status = "owner-route-post-launch-attachment"
    if anchor is None:
        return None, "owner-route-binding-absent"
    current, advance_status = resolve_owner_route_advance(
        jobs, owner_attempt_id=owner_attempt_id, anchor=anchor,
        anchor_generation=sealed_generation, registry_rows=rows,
    )
    if advance_status in {"owner-route-advance-current"}:
        return current, advance_status
    return current, anchor_status if advance_status == "owner-route-advance-absent" else advance_status


def _candidate_started_by_owner(
    rows: list[tuple[list[str], dict[str, str]]], *, owner_attempt_id: str,
    candidate: OwnerRouteAdvance, source_route: dict,
) -> bool:
    """Return whether an exact registered child adopted this candidate.

    Compiling a continuation does not by itself move the owner: compilation can
    be abandoned before any child starts.  Current-contract child rows survive
    process exit, so the adoption proof remains available after restart.
    """
    relevant = [
        (fields, meta) for fields, meta in rows
        if meta.get("parent_attempt_id") == owner_attempt_id
        and meta.get("route_id") == candidate.to_route_id
    ]
    if not relevant:
        return False
    expected_file = str(Path(candidate.to_route_file).resolve(strict=False))
    expected_worktree = str(
        Path(source_route.get("cwd", "")).resolve(strict=False)
    )
    for fields, meta in relevant:
        proof = {
            "attempt_schema_version": "2",
            "dispatch_depth": "2",
            "registered_worker": "1",
            "execution_surface": "registered-headless",
            "route_id": candidate.to_route_id,
            "route_hash": candidate.to_route_hash,
        }
        if (
            any(meta.get(key) != value for key, value in proof.items())
            or meta.get("worker_type") == "owner"
            or not meta.get("route_node")
            or not meta.get("attempt_id")
            or not all(meta.get(key) for key in (
                "route_file", "capability", "capability_mode", "artifact_root",
            ))
            or str(Path(fields[3]).resolve(strict=False)) != expected_worktree
            or meta.get("capability") != source_route.get("capability")
            or meta.get("capability_mode") != source_route.get("capability_mode")
            or str(Path(meta.get("artifact_root", "")).resolve(strict=False))
            != str(Path(source_route.get("artifact_root", "")).resolve(strict=False))
            or str(Path(meta.get("route_file", "")).resolve(strict=False))
            != expected_file
        ):
            raise OwnerRouteBindingError(
                "owner-route-advance-child-evidence-invalid"
            )
    return any(meta.get("launch_started") == "1" for _fields, meta in relevant)


def resolve_owner_route_advance(
    jobs: str | Path, *, owner_attempt_id: str, anchor: OwnerRouteBinding,
    anchor_generation: int | None = None,
    registry_rows: list[tuple[list[str], dict[str, str]]] | None = None,
) -> tuple[OwnerRouteBinding, str]:
    """Follow only verified, child-adopted predecessor candidates.

    Each predecessor directory may contain several immutable compilation
    candidates.  Zero started candidates leaves the current generation in
    place; one advances it; two are a real competing-successor conflict.  This
    is the distinction that keeps a childless abandoned compile inert without
    choosing by timestamp.
    """
    root = _advance_root(jobs)
    first_path = root / _advance_key(
        owner_attempt_id, anchor.route_id, anchor.route_hash
    )
    if registry_rows is None:
        jobs_path = Path(jobs).expanduser()
        registry_rows = _registry_snapshot(jobs_path) if jobs_path.is_file() else []
    first_candidates = list(first_path.glob("*.json")) if first_path.is_dir() else []
    try:
        _current_raw, current_route = _verified_binding(anchor)
        route_generation = int(current_route.get("advance_generation") or 0)
    except (OwnerRouteBindingError, ValueError, TypeError) as exc:
        if not first_candidates:
            return anchor, "owner-route-advance-anchor-unresolvable"
        raise OwnerRouteBindingError("owner-route-advance-anchor-invalid") from exc
    if anchor_generation is None:
        anchor_generation = route_generation
    elif route_generation != anchor_generation:
        raise OwnerRouteBindingError("owner-route-advance-generation-invalid")
    current, generation, seen = anchor, anchor_generation, set()
    while generation < 256:
        key = (current.route_id, current.route_hash)
        if key in seen:
            return current, "owner-route-advance-loop"
        seen.add(key)
        predecessor = root / _advance_key(
            owner_attempt_id, current.route_id, current.route_hash
        )
        paths = sorted(predecessor.glob("*.json")) if predecessor.is_dir() else []
        if not paths:
            return current, (
                "owner-route-advance-absent"
                if generation == anchor_generation
                else "owner-route-advance-current"
            )
        adopted = []
        for path in paths:
            try:
                candidate = _advance_from_payload(
                    json.loads(path.read_text(encoding="utf-8")), path
                )
            except (OSError, ValueError, TypeError) as exc:
                raise OwnerRouteBindingError(
                    "owner-route-advance-record-invalid"
                ) from exc
            if candidate.owner_attempt_id != owner_attempt_id:
                raise OwnerRouteBindingError("owner-route-advance-record-invalid")
            if path.name != _advance_target_key(
                candidate.to_route_id, candidate.to_route_hash
            ):
                raise OwnerRouteBindingError("owner-route-advance-record-invalid")
            if (
                candidate.from_route_id != current.route_id
                or candidate.from_route_hash != current.route_hash
                or str(Path(candidate.from_route_file).resolve(strict=False))
                != str(Path(current.route_file).resolve(strict=False))
            ):
                raise OwnerRouteBindingError("owner-route-advance-source-mismatch")
            if (
                candidate.from_generation != generation
                or candidate.to_generation != generation + 1
            ):
                raise OwnerRouteBindingError(
                    "owner-route-advance-generation-invalid"
                )
            if (
                current_route.get("owner_attempt_id") == owner_attempt_id
                and candidate.route_family_key
                and candidate.route_family_key
                != current_route.get("route_family_key")
            ):
                raise OwnerRouteBindingError("owner-route-advance-family-mismatch")
            if _candidate_started_by_owner(
                registry_rows, owner_attempt_id=owner_attempt_id,
                candidate=candidate, source_route=current_route,
            ):
                adopted.append(candidate)
        if len(adopted) > 1:
            raise OwnerRouteBindingError(
                "owner-route-advance-competing-successor"
            )
        if not adopted:
            return current, (
                "owner-route-advance-pending"
                if generation == anchor_generation
                else "owner-route-advance-current"
            )
        record = adopted[0]
        target_path = Path(record.to_route_file)
        if not target_path.is_absolute():
            raise OwnerRouteBindingError("owner-route-advance-target-invalid")
        try:
            raw_target = json.loads(target_path.read_text(encoding="utf-8"))
            verified_target = ROUTE.verify_route(raw_target)
            if (
                verified_target.get("route_id") != record.to_route_id
                or verified_target.get("route_hash") != record.to_route_hash
            ):
                raise OwnerRouteBindingError(
                    "owner-route-advance-target-hash-mismatch"
                )
            target = OwnerRouteBinding(
                str(target_path.resolve()), verified_target["route_id"],
                verified_target["route_hash"],
            )
        except (OSError, ValueError, TypeError, OwnerRouteBindingError) as exc:
            raise OwnerRouteBindingError("owner-route-advance-target-invalid") from exc
        edge = raw_target.get("source_route_supersession") or {}
        if (
            edge.get("from_route_id") != current.route_id
            or edge.get("from_route_hash") != current.route_hash
        ):
            raise OwnerRouteBindingError(
                "owner-route-advance-supersession-mismatch"
            )
        if (raw_target.get("source_route_id"), raw_target.get("source_route_hash")) != (
            current.route_id, current.route_hash
        ):
            raise OwnerRouteBindingError("owner-route-advance-source-mismatch")
        if int(verified_target.get("advance_generation") or 0) != generation + 1:
            raise OwnerRouteBindingError("owner-route-advance-generation-invalid")
        target_owner = verified_target.get("owner_attempt_id")
        if target_owner != owner_attempt_id:
            raise OwnerRouteBindingError("owner-route-advance-owner-mismatch")
        for field, reason in (
            ("cwd", "owner-route-advance-worktree-mismatch"),
            ("capability", "owner-route-advance-capability-mismatch"),
            ("capability_mode", "owner-route-advance-mode-mismatch"),
            ("artifact_root", "owner-route-advance-artifact-root-mismatch"),
        ):
            if current_route.get(field) and verified_target.get(field) != current_route.get(field):
                raise OwnerRouteBindingError(reason)
        if (
            record.route_family_key
            and record.route_family_key != verified_target.get("route_family_key")
        ):
            raise OwnerRouteBindingError("owner-route-advance-family-mismatch")
        if (
            current_route.get("owner_attempt_id") == owner_attempt_id
            and current_route.get("route_family_key")
            != verified_target.get("route_family_key")
        ):
            raise OwnerRouteBindingError("owner-route-advance-family-mismatch")
        current, generation = target, generation + 1
        current_route = verified_target
    raise OwnerRouteBindingError("owner-route-advance-generation-ceiling")


def _environment_jobs(jobs: str | Path, environ: dict[str, str]) -> Path:
    jobs_path = Path(jobs).expanduser()
    if not jobs_path.is_absolute() or not jobs_path.is_file():
        raise OwnerRouteBindingError("owner-route-jobs-unreadable")
    if jobs_path.resolve(strict=False) != Path(
        environ.get("AGENT_DISPATCH_JOBS", jobs)
    ).expanduser().resolve(strict=False):
        raise OwnerRouteBindingError("owner-route-jobs-path-mismatch")
    return jobs_path.resolve(strict=False)


def _payload_binding(route: dict) -> OwnerRouteBinding:
    route_file = str(route.get("route_file") or "")
    route_id = str(route.get("route_id") or "")
    route_hash = str(route.get("route_hash") or "")
    if not route_file or not route_id or not route_hash:
        raise OwnerRouteBindingError("owner-route-context-incomplete")
    return OwnerRouteBinding(str(Path(route_file).resolve()), route_id, route_hash)


def publish_owner_route_attachment_from_environment(
    jobs: str | Path, *, target_route: dict, environ: dict[str, str]
) -> OwnerRouteAttachment | None:
    """Attach generation 0 only for a typed, active, route-less owner."""
    attempt = _registered_owner_attempt(environ, attachment=True)
    if not attempt:
        return None
    marker_values = tuple(environ.get(key, "") for key in (
        "AGENT_OWNER_ROUTE_FILE", "AGENT_OWNER_ROUTE_ID", "AGENT_OWNER_ROUTE_HASH"
    ))
    if any(marker_values):
        if not all(marker_values):
            raise OwnerRouteBindingError("owner-route-context-incomplete")
        raise OwnerRouteBindingError("owner-route-owner-already-bound")
    _environment_jobs(jobs, environ)
    target = _payload_binding(target_route)
    with _jobs_lock(jobs) as canonical_jobs:
        fields, meta = _owner_snapshot(canonical_jobs, attempt)
        if fields[1] not in {"open", "running"}:
            raise OwnerRouteBindingError("owner-route-owner-row-ineligible")
        _, verified = _verified_binding(target, expected_cwd=target_route.get("cwd"))
        _owner_row_proof(
            fields, meta, route=verified, environ=environ, binding_mode="unbound"
        )
        checks = (
            (int(verified.get("advance_generation") or 0) == 0,
             "owner-route-attachment-generation-invalid"),
            (not verified.get("source_route_supersession"),
             "owner-route-attachment-source-invalid"),
            (verified.get("owner_attempt_id") == attempt,
             "owner-route-attachment-owner-mismatch"),
            (bool(verified.get("route_family_key")),
             "owner-route-attachment-family-mismatch"),
        )
        for valid, reason in checks:
            if not valid:
                raise OwnerRouteBindingError(reason)
        attachment = publish_owner_route_attachment(
            canonical_jobs,
            owner_attempt_id=attempt,
            binding=target,
            generation=0,
            route_family_key=str(verified["route_family_key"]),
            worktree=str(Path(fields[3]).resolve(strict=False)),
            repo=str(Path(fields[2]).resolve(strict=False)),
            capability=str(meta.get("capability") or ""),
            capability_mode=str(meta.get("capability_mode") or ""),
            artifact_root=str(Path(meta.get("artifact_root", "")).resolve(strict=False)),
            parent_session_id=str(meta.get("parent_sid") or ""),
            owner_harness=str(meta.get("owner_harness") or ""),
        )
        fields2, meta2 = _owner_snapshot(canonical_jobs, attempt)
        if fields2 != fields or meta2 != meta or fields2[1] not in {"open", "running"}:
            raise OwnerRouteBindingError("owner-route-owner-row-raced")
        return attachment


def publish_owner_route_advance_from_environment(jobs: str | Path, *, source_route: dict,
                                                  target_route: dict, environ: dict[str, str]) -> OwnerRouteAdvance | None:
    """Publish only when the continuation is running as the registered owner."""
    attempt = _registered_owner_attempt(environ)
    if not attempt:
        return None
    _environment_jobs(jobs, environ)
    marker_values = tuple(environ.get(key, "") for key in (
        "AGENT_OWNER_ROUTE_FILE", "AGENT_OWNER_ROUTE_ID", "AGENT_OWNER_ROUTE_HASH"
    ))
    if any(marker_values) and not all(marker_values):
        raise OwnerRouteBindingError("owner-route-context-incomplete")
    source_payload = dict(source_route)
    if not source_payload.get("route_file") and all(marker_values):
        # Compatibility for generation-0 continuations launched with the
        # original wrapper tuple. Multi-hop callers always pass the exact
        # source path sealed by capability-route.py.
        source_payload["route_file"] = marker_values[0]
    source = _payload_binding(source_payload)
    target = _payload_binding(target_route)
    environment_binding = (
        OwnerRouteBinding(str(Path(marker_values[0]).resolve()), marker_values[1], marker_values[2])
        if all(marker_values) else None
    )
    with _jobs_lock(jobs) as canonical_jobs:
        fields, meta = _owner_snapshot(canonical_jobs, attempt)
        if fields[1] not in {"open", "running"}:
            raise OwnerRouteBindingError("owner-route-owner-row-ineligible")
        row_binding = _row_binding(meta)
        if environment_binding is not None and environment_binding != row_binding:
            raise OwnerRouteBindingError("owner-route-owner-binding-mismatch")
        if row_binding is not None:
            anchor = row_binding
            binding_mode = "sealed"
        else:
            anchor = _resolve_attachment_locked(canonical_jobs, attempt, fields, meta)
            binding_mode = "unbound"
        if anchor is None:
            raise OwnerRouteBindingError("owner-route-binding-absent")
        _, verified_source = _verified_binding(source, expected_cwd=source_route.get("cwd"))
        _, verified_target = _verified_binding(
            target, expected_cwd=target_route.get("cwd") or source_route.get("cwd")
        )
        _owner_row_proof(
            fields, meta, route=verified_source, environ=environ,
            binding_mode=binding_mode, binding=row_binding,
        )
        source_generation = int(verified_source.get("advance_generation") or 0)
        target_generation = int(verified_target.get("advance_generation") or 0)
        edge = verified_target.get("source_route_supersession") or {}
        if target_generation != source_generation + 1:
            raise OwnerRouteBindingError("owner-route-advance-generation-invalid")
        if (edge.get("from_route_id"), edge.get("from_route_hash")) != (
            source.route_id, source.route_hash
        ):
            raise OwnerRouteBindingError("owner-route-advance-supersession-mismatch")
        if verified_target.get("owner_attempt_id") != attempt:
            raise OwnerRouteBindingError("owner-route-advance-owner-mismatch")
        for field, reason in (
            ("cwd", "owner-route-advance-worktree-mismatch"),
            ("capability", "owner-route-advance-capability-mismatch"),
            ("capability_mode", "owner-route-advance-mode-mismatch"),
            ("artifact_root", "owner-route-advance-artifact-root-mismatch"),
        ):
            if verified_target.get(field) != verified_source.get(field):
                raise OwnerRouteBindingError(reason)
        if verified_source.get("owner_attempt_id") == attempt and (
            verified_source.get("route_family_key") != verified_target.get("route_family_key")
        ):
            raise OwnerRouteBindingError("owner-route-advance-family-mismatch")
        current, status = resolve_owner_route_advance(
            canonical_jobs, owner_attempt_id=attempt, anchor=anchor,
        )
        if status not in {
            "owner-route-advance-absent", "owner-route-advance-pending",
            "owner-route-advance-current",
        }:
            raise OwnerRouteBindingError("owner-route-current-binding-conflict")
        # `current == source` covers a first candidate, an exact replay before
        # child adoption, or another still-unadopted candidate. `current ==
        # target` is replay after adoption. Anything else is a downgrade or an
        # unrelated source and cannot publish into this predecessor set.
        if current not in (source, target):
            raise OwnerRouteBindingError("owner-route-source-not-current")
        # Re-read under the still-held canonical lock. Registry close/replacement
        # therefore cannot race the advance record commit.
        fields2, meta2 = _owner_snapshot(canonical_jobs, attempt)
        if fields2 != fields or meta2 != meta or fields2[1] not in {"open", "running"}:
            raise OwnerRouteBindingError("owner-route-owner-row-raced")
        return publish_owner_route_advance(
            canonical_jobs, owner_attempt_id=attempt, source=source, target=target,
            from_generation=source_generation,
            to_generation=target_generation,
            route_family_key=str(target_route.get("route_family_key") or ""),
        )


@dataclass(frozen=True)
class QuickOwnerRouteBinding:
    route_file: str
    route_id: str
    route_hash: str
    route_node: str
    registry_digest: str
    write_scope: str
    completion_gate: str


def derive_quick_owner_binding(route_file: str | Path, *, worktree: str | Path,
                               capability: str, capability_mode: str,
                               intensity: str, harness: str) -> QuickOwnerRouteBinding:
    """Derive the complete node tuple for the quick one-shot owner."""
    path = Path(route_file).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        route = ROUTE.verify_route(raw, expected_cwd=str(Path(worktree).resolve()))
    except (OSError, TypeError, ValueError) as exc:
        raise OwnerRouteBindingError("owner-route-verification-failed") from exc
    if route.get("effective_intensity") != "quick":
        raise OwnerRouteBindingError("quick-owner-route-required")
    validate_owner_route_binding(path, worktree=worktree, capability=capability,
                                 capability_mode=capability_mode, intensity=intensity,
                                 harness=harness)
    node = next((n for n in route.get("nodes", []) if n.get("id") == "one-shot"), None)
    if not isinstance(node, dict):
        raise OwnerRouteBindingError("quick-owner-node-missing")
    scope = node.get("write_scope")
    if isinstance(scope, list):
        scope = ";".join(str(x) for x in scope)
    fields = (route.get("route_id"), route.get("route_hash"), route.get("registry_digest"),
              scope, node.get("completion_gate"))
    if not all(isinstance(x, str) and x for x in fields):
        raise OwnerRouteBindingError("quick-owner-binding-incomplete")
    return QuickOwnerRouteBinding(str(path), fields[0], fields[1], "one-shot", fields[2], fields[3], fields[4])


def _supported_owner_harnesses(route: dict) -> set[str]:
    if route.get("effective_intensity") == "quick":
        rows, field = route.get("registered_headless_candidates") or [], "harness"
    else:
        rows = (route.get("dispatch_evidence") or {}).get("tuples") or []
        field = "parent_harness"
    return {
        str(row.get(field))
        for row in rows
        if isinstance(row, dict) and row.get("status") == "supported"
    }


def validate_owner_route_binding(
    route_file: str | Path,
    *,
    worktree: str | Path,
    capability: str,
    capability_mode: str,
    intensity: str,
    harness: str,
    expected_route_id: str = "",
    expected_route_hash: str = "",
) -> OwnerRouteBinding:
    path = Path(route_file).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        route = ROUTE.verify_route(raw, expected_cwd=str(Path(worktree).resolve()))
    except (OSError, TypeError, ValueError) as exc:
        raise OwnerRouteBindingError("owner-route-verification-failed") from exc
    checks = (
        (route.get("schema_version") == 2, "owner-route-schema-invalid"),
        (route.get("capability") == capability, "owner-route-capability-mismatch"),
        (route.get("capability_mode") == capability_mode, "owner-route-mode-mismatch"),
        (route.get("effective_intensity") == intensity, "owner-route-intensity-mismatch"),
        (route.get("owner_dispatch_depth") == 1, "owner-route-depth-mismatch"),
        (harness in _supported_owner_harnesses(route), "owner-route-harness-mismatch"),
        (
            not expected_route_id or route.get("route_id") == expected_route_id,
            "owner-route-id-mismatch",
        ),
        (
            not expected_route_hash or route.get("route_hash") == expected_route_hash,
            "owner-route-hash-mismatch",
        ),
    )
    for valid, reason in checks:
        if not valid:
            raise OwnerRouteBindingError(reason)
    return OwnerRouteBinding(
        route_file=str(path),
        route_id=str(route["route_id"]),
        route_hash=str(route["route_hash"]),
    )


def binding_from_environment(
    environ: dict[str, str],
    **expected: str,
) -> OwnerRouteBinding | None:
    route_file = environ.get("AGENT_OWNER_ROUTE_FILE", "")
    route_id = environ.get("AGENT_OWNER_ROUTE_ID", "")
    route_hash = environ.get("AGENT_OWNER_ROUTE_HASH", "")
    if not route_file and not route_id and not route_hash:
        return None
    if not route_file or not route_id or not route_hash:
        raise OwnerRouteBindingError("owner-route-binding-incomplete")
    return validate_owner_route_binding(
        route_file,
        expected_route_id=route_id,
        expected_route_hash=route_hash,
        **expected,
    )


def validate_runtime_requirements(
    route: dict,
    node_id: str,
    *,
    supported: set[str] | None = None,
) -> None:
    nodes = route.get("nodes")
    node = next(
        (
            item for item in nodes or []
            if isinstance(item, dict) and item.get("id") == node_id
        ),
        None,
    )
    if node is None:
        raise OwnerRouteBindingError("route-node-unknown")
    requirements = node.get("runtime_requirements") or []
    unsupported = set(requirements).difference(supported or set())
    if "loopback-listen" in unsupported:
        raise OwnerRouteBindingError("loopback-only-unsupported")
    if unsupported:
        raise OwnerRouteBindingError("runtime-requirement-unsupported")
