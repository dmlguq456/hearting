"""Fail-closed proof that a Codex parent predates no effective hook definition."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import uuid
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_contract import resolve_dispatch_state_root  # noqa: E402


@dataclass(frozen=True)
class ParentDefinitionProof:
    eligible: bool
    reason: str
    parent_start_ms: int | None = None
    definition_ms: int | None = None
    definition_digest: str | None = None


def parent_start_ms(session_id: str) -> int | None:
    try:
        value = uuid.UUID(str(session_id))
    except (ValueError, AttributeError, TypeError):
        return None
    if value.version != 7 or value.variant != uuid.RFC_4122:
        return None
    return value.int >> 80


def _mtime_ms(path: Path) -> int:
    return math.floor(path.stat().st_mtime_ns / 1_000_000)


def _unavailable(reason: str, digest: str | None = None) -> ParentDefinitionProof:
    return ParentDefinitionProof(False, reason, definition_digest=digest)


def prove_parent_definition(
    session_id: str,
    *,
    hooks_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    lock_path: Path | str | None = None,
    now_ms: Callable[[], int] | None = None,
    stat_ms: Callable[[Path], int] = _mtime_ms,
) -> ParentDefinitionProof:
    """Return whether a UUIDv7 parent could have loaded the effective hooks.

    ``hooks_path`` is resolved before hashing and statting. The ledger stores the
    first observed timestamp for each content hash, so a checkout rewrite with
    identical bytes cannot make an old parent appear new.
    """
    start = parent_start_ms(session_id)
    if start is None:
        return _unavailable("parent-id-format-unproven")
    path = Path(hooks_path or (Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "hooks.json"))
    try:
        effective = path.resolve(strict=True)
        payload = effective.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        seed_ms = int(stat_ms(effective))
    except (OSError, ValueError, TypeError):
        return _unavailable("hook-definition-unreadable")

    # ``now_ms`` is an injectable seam for callers/tests that need a stable
    # clock. The proof intentionally derives definition age from target mtime
    # on first observation; wall-clock time must never make an unknown hash
    # look older than it is.
    if now_ms is not None:
        now_ms()
    harness_home = Path(os.environ.get("AGENT_HOME", "~/.codex")).expanduser()
    ledger = Path(ledger_path) if ledger_path is not None else resolve_dispatch_state_root(harness_home) / "codex-hook-definition-ledger.json"
    lock = Path(lock_path) if lock_path is not None else ledger.with_name(ledger.name + ".lock")
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if ledger.exists():
                    if not ledger.is_file():
                        return _unavailable("definition-ledger-unavailable", digest)
                    raw = json.loads(ledger.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
                        return _unavailable("definition-ledger-unavailable", digest)
                    entries = raw.get("entries")
                    if not isinstance(entries, dict):
                        return _unavailable("definition-ledger-unavailable", digest)
                    for value in entries.values():
                        if (
                            not isinstance(value, dict)
                            or not isinstance(value.get("first_seen_ms"), int)
                            or value.get("source") not in {"mtime-seed", "observed"}
                        ):
                            return _unavailable("definition-ledger-unavailable", digest)
                else:
                    raw, entries = {"schema_version": 1, "entries": {}}, {}
                entry = entries.get(digest)
                if isinstance(entry, dict) and isinstance(entry.get("first_seen_ms"), int):
                    definition_ms = entry["first_seen_ms"]
                elif entry is not None:
                    return _unavailable("definition-ledger-unavailable", digest)
                else:
                    definition_ms = seed_ms
                    entries[digest] = {"first_seen_ms": definition_ms, "source": "mtime-seed"}
                    raw["entries"] = entries
                    fd, temporary = tempfile.mkstemp(prefix=ledger.name + ".", dir=str(ledger.parent))
                    try:
                        os.fchmod(fd, 0o600)
                        with os.fdopen(fd, "w", encoding="utf-8") as stream:
                            json.dump(raw, stream, separators=(",", ":"), sort_keys=True)
                            stream.write("\n")
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, ledger)
                    finally:
                        try:
                            os.unlink(temporary)
                        except FileNotFoundError:
                            pass
                    try:
                        os.chmod(ledger, 0o600)
                    except OSError:
                        return _unavailable("definition-ledger-unavailable", digest)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _unavailable("definition-ledger-unavailable", digest)

    reason = "parent-definition-proven" if start >= definition_ms else "parent-older-than-definition"
    return ParentDefinitionProof(start >= definition_ms, reason, start, definition_ms, digest)
