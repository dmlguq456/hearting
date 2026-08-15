"""Shared hermetic fixtures for protocol-v2 tests.

The helpers deliberately expose the small public surface the tests need. Keeping
all fixture construction here makes the protocol tests readable and gives the
implementation one obvious compatibility seam while the v2 modules are new.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
from typing import Any, Iterable, Mapping


MEMORY_DIR = Path(__file__).resolve().parents[1]
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))


def load_module(name: str):
    """Import one of the sibling v2 modules with an actionable failure."""

    try:
        return importlib.import_module(name)
    except ImportError as exc:  # pragma: no cover - exercised only during rollout
        raise AssertionError(
            f"tools/memory/{name}.py is required by the v2 test suite"
        ) from exc


def public_callable(module: Any, *names: str):
    """Resolve a documented operation across a narrow set of spelling aliases."""

    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate
    joined = ", ".join(names)
    raise AssertionError(f"{module.__name__} must expose one of: {joined}")


def canonical_bytes(value: Any) -> bytes:
    protocol = load_module("protocol_v2")
    fn = public_callable(
        protocol, "canonical_json_bytes", "canonical_bytes", "canonical_dumps"
    )
    encoded = fn(value)
    if isinstance(encoded, str):
        encoded = encoded.encode("utf-8")
    if not isinstance(encoded, bytes):
        raise AssertionError("canonical encoder must return bytes or UTF-8 text")
    return encoded


def canonical_loads(raw: bytes) -> Any:
    protocol = load_module("protocol_v2")
    fn = public_callable(
        protocol,
        "parse_canonical_json",
        "canonical_loads",
        "decode_canonical_json",
    )
    return fn(raw)


def operation_path(op_id: str) -> str:
    protocol = load_module("protocol_v2")
    fn = public_callable(protocol, "operation_path", "op_path")
    return str(fn(op_id))


def _frontiers(record_id: str, heads: Iterable[str]) -> list[dict[str, Any]]:
    return [{"heads": sorted(heads), "record_id": record_id}]


def record_post_state(
    record_id: str,
    body: str | None,
    *,
    pending: bool = False,
    project_key: str = "project-alpha",
) -> dict[str, Any]:
    """Return the exact complete RECORD_COLS wire fixture for schema minor 0."""
    text = body or ""
    is_global = project_key == "global"
    return {
        "aliases": [],
        "artifact_refs": [],
        "body": text,
        "canonical_id": record_id,
        "capsule_version": 1,
        "created": "2026-08-15",
        "cwd_origin": None if is_global else project_key,
        "delivery_state": "pending" if pending else "ordinary",
        "entities": [],
        "expires": None if pending else "2026-09-05",
        "headline": text.splitlines()[0][:240] if text else "fixture",
        "id": record_id,
        "injection_flag": 0,
        "last_accessed": "2026-08-15",
        "links": [],
        "scope": "global" if is_global else "project",
        "source": "stdlib-test",
        "status": "active",
        "strength": 1,
        "superseded_by": None,
        "tags": [],
        "tier": "working",
        "topics": [],
        "type": "note",
        "updated": "2026-08-15",
    }


def tombstone_evidence(
    record_id: str,
    *,
    action: str = "test-deletion",
    pending: bool = False,
    prior_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return exact authoring-compatible destructive evidence."""
    prior = dict(prior_state) if prior_state is not None else record_post_state(
        record_id, "prior state", pending=pending
    )
    return {
        "action": action,
        "pending": prior.get("delivery_state") == "pending",
        "prior_digest": hashlib.sha256(canonical_bytes(prior)).hexdigest(),
        "record_id": record_id,
    }


def payload(
    *,
    replica_id: str,
    counter: int,
    record_id: str,
    body: str | None,
    parents: Iterable[str] = (),
    frontier: Iterable[str] = (),
    kind: str = "put",
    pending: bool = False,
    project_key: str = "project-alpha",
    reason: str | None = None,
    prior_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete, deliberately small semantic-transaction payload."""

    mutation: dict[str, Any] = {
        "mutation_ordinal": 0,
        "record_id": record_id,
    }
    if kind in {"tombstone", "force-tombstone"}:
        mutation["tombstone"] = tombstone_evidence(
            record_id,
            action=reason or "test-deletion",
            pending=pending,
            prior_state=prior_state,
        )
    else:
        mutation["post_state"] = record_post_state(
            record_id, body, pending=pending, project_key=project_key
        )
    return {
        "counter": counter,
        "frontiers": _frontiers(record_id, frontier),
        "kind": kind,
        "mutations": [mutation],
        "parents": sorted(parents),
        "project_key": project_key,
        "protocol_major": 2,
        "provenance": {"actor": "stdlib-test", "reason": reason or "fixture"},
        "replica_id": replica_id,
        "schema_minor": 0,
    }


def envelope(payload_value: Mapping[str, Any]) -> dict[str, Any]:
    payload_dict = dict(payload_value)
    op_id = hashlib.sha256(canonical_bytes(payload_dict)).hexdigest()
    return {"op_id": op_id, "payload": payload_dict}


def make_operation(**kwargs: Any) -> dict[str, Any]:
    """Return the exact wire envelope used by reducers and exchange tests."""

    protocol = load_module("protocol_v2")
    payload_value = payload(**kwargs)
    for name in ("build_operation", "make_operation", "create_operation"):
        builder = getattr(protocol, name, None)
        if callable(builder):
            return as_mapping(builder(payload_value))
    return envelope(payload_value)


def validate_operation(operation: Mapping[str, Any], path: str | None = None) -> Any:
    protocol = load_module("protocol_v2")
    fn = public_callable(protocol, "validate_operation", "parse_operation")
    if path is None:
        return fn(dict(operation))
    try:
        return fn(dict(operation), path=path)
    except TypeError:
        return fn(dict(operation), path)


def classify_operations(operations: Iterable[Mapping[str, Any]]) -> Any:
    protocol = load_module("protocol_v2")
    fn = public_callable(protocol, "classify_operations", "classify_operation_set")
    return fn([dict(op) for op in operations])


def fold_operations(operations: Iterable[Mapping[str, Any]]) -> Any:
    protocol = load_module("protocol_v2")
    fn = public_callable(protocol, "fold_operations", "fold_operation_set", "fold")
    return fn([dict(op) for op in operations])


def as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    for name in ("to_dict", "as_dict", "to_wire"):
        fn = getattr(value, name, None)
        if callable(fn):
            converted = fn()
            if isinstance(converted, Mapping):
                return dict(converted)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise AssertionError(f"expected mapping-like result, got {type(value).__name__}")


def field(value: Any, name: str, *aliases: str, default: Any = None) -> Any:
    mapping = as_mapping(value)
    for key in (name, *aliases):
        if key in mapping:
            return mapping[key]
    return default


def id_set(value: Any, name: str, *aliases: str) -> set[str]:
    raw = field(value, name, *aliases, default=())
    if isinstance(raw, Mapping):
        return {str(key) for key in raw}
    result = set()
    for item in raw or ():
        if isinstance(item, str):
            result.add(item)
        else:
            item_id = field(item, "op_id", "id", default=None)
            if item_id is not None:
                result.add(str(item_id))
    return result


def record_state(result: Any, record_id: str) -> dict[str, Any] | None:
    records = field(
        result, "materialized", "records", "projection", "materialized_records"
    )
    if not isinstance(records, Mapping) or record_id not in records:
        return None
    return as_mapping(records[record_id])


def conflict_state(result: Any, record_id: str) -> Any:
    conflicts = field(result, "conflicts", "conflict_records", default={})
    if isinstance(conflicts, Mapping):
        return conflicts.get(record_id)
    for item in conflicts or ():
        if field(item, "record_id", default=None) == record_id:
            return item
    return None


def stable_result(value: Any) -> bytes:
    """Canonicalize a public result for exact permutation comparison."""

    return canonical_bytes(as_mapping(value))


def git(cwd: Path, *args: str, input_bytes: bytes | None = None) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_EMAIL": "v2-tests@example.invalid",
            "GIT_AUTHOR_NAME": "v2 tests",
            "GIT_COMMITTER_EMAIL": "v2-tests@example.invalid",
            "GIT_COMMITTER_NAME": "v2 tests",
        }
    )
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *args],
        check=True,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return completed.stdout.decode("utf-8", errors="strict").strip()


def init_bare(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "--bare")
    git(path, "symbolic-ref", "HEAD", "refs/heads/memory-v2")
    return path


def commit_bare_tree(
    repo: Path,
    entries: Mapping[str, tuple[str, bytes]],
    *,
    parent: str | None = None,
    message: str = "fixture",
) -> str:
    """Create a commit in a bare repository without a checkout."""

    tree: dict[str, Any] = {}
    for path, value in entries.items():
        node = tree
        parts = Path(path).parts
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def write_tree(node: Mapping[str, Any]) -> str:
        lines = []
        for name in sorted(node):
            value = node[name]
            if isinstance(value, Mapping):
                oid = write_tree(value)
                lines.append(f"040000 tree {oid}\t{name}\n")
            else:
                mode, content = value
                oid = git(repo, "hash-object", "-w", "--stdin", input_bytes=content)
                lines.append(f"{mode} blob {oid}\t{name}\n")
        return git(repo, "mktree", input_bytes="".join(lines).encode("utf-8"))

    tree_id = write_tree(tree)
    args = ["commit-tree", tree_id]
    if parent:
        args.extend(["-p", parent])
    return git(repo, *args, input_bytes=(message + "\n").encode("utf-8"))


def json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def ensure_sync_schema(connection: sqlite3.Connection) -> None:
    sync = load_module("sync_v2")
    fn = public_callable(
        sync, "ensure_sync_schema", "initialize_schema", "init_sync_schema"
    )
    fn(connection)


def allocate_counter(connection: sqlite3.Connection, replica_id: str) -> int:
    sync = load_module("sync_v2")
    fn = public_callable(sync, "allocate_counter", "next_counter")
    return int(fn(connection, replica_id))


def record_local_operation(
    connection: sqlite3.Connection, operation: Mapping[str, Any]
) -> Any:
    sync = load_module("sync_v2")
    fn = public_callable(
        sync,
        "record_local_operation",
        "enqueue_local_operation",
        "capture_local_operation",
    )
    return fn(connection, dict(operation))


def transition_outbox(
    connection: sqlite3.Connection,
    op_id: str,
    state: str,
    evidence: Mapping[str, Any] | None = None,
) -> Any:
    sync = load_module("sync_v2")
    fn = public_callable(sync, "transition_outbox", "advance_outbox")
    return fn(connection, op_id, state, evidence)


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def new_exchange(root: Path, remote: Path, ref: str):
    module = load_module("git_exchange_v2")
    cls = getattr(module, "GitExchange", None)
    if cls is None:
        raise AssertionError("git_exchange_v2 must expose GitExchange")
    parameters = inspect.signature(cls).parameters
    aliases = {
        "root": root,
        "exchange_root": root,
        "repo": root,
        "repo_path": root,
        "remote": str(remote),
        "remote_url": str(remote),
        "ref": ref,
        "remote_ref": ref,
    }
    kwargs = {name: aliases[name] for name in parameters if name in aliases}
    if kwargs:
        return cls(**kwargs)
    return cls(root, str(remote), ref)


def exchange_publish(exchange: Any, operations: Iterable[Mapping[str, Any]]) -> Any:
    fn = public_callable(exchange, "publish_operations", "publish", "sync")
    values = [dict(op) for op in operations]
    try:
        return fn(values)
    except TypeError:
        return fn(operations=values)


def exchange_fetch_validate(exchange: Any) -> Any:
    fn = public_callable(
        exchange, "fetch_validate", "fetch_and_validate", "fetch"
    )
    return fn()


def exchange_fresh_confirm(exchange: Any, op_id: str) -> bool:
    fn = public_callable(exchange, "fresh_confirm", "confirm_operation", "confirm")
    return bool(fn(op_id))


def ref_paths(repo: Path, ref: str) -> list[str]:
    output = git(repo, "ls-tree", "-r", "--name-only", ref)
    return output.splitlines() if output else []


def remote_policy(environ: Mapping[str, str]) -> dict[str, Any]:
    sync = load_module("sync_v2")
    fn = public_callable(
        sync, "remote_policy", "resolve_remote_policy", "remote_mode"
    )
    return as_mapping(fn(dict(environ)))
