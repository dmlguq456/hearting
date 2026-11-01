#!/usr/bin/env python3
"""Derived per-recipient dispatch inboxes: discovery only, never authority.

``<dispatch>/recipient-inbox/<harness>/<session_id>.json`` names the delivery
records one recipient may need to open, the edge generation under which it was
observed, and the directory identities that reveal a record which appeared or
disappeared. A hook reads that one small file instead of re-deriving the global
recipient graph; delivery, claim, ACK and recipient authority stay with the
existing readers (``dispatch_seat_handover.record_recipient_binding`` and
``dispatch_seat_handover.storage_recipients``). ``candidates`` are discovery
hints only, never permission.

The runner publishes the other derived observations in the same directory:

``edge-generation.json``
    Monotonic integer bumped by the confirmed lineage-edge writers. A derived
    inbox is current only under the generation it recorded.
``lineage-rows.json``
    Compact relation rows per sessions ledger, keyed by the ledger's exact
    generation tuple. The effect path reuses those rows without reopening an
    unchanged ledger and falls back to the normal full read when the file is
    missing, stale-schema or corrupt.
``run-states.json``
    Public run status/exit for notices that name a run, published by the
    obligation runner. Readers fall back to the notice's own text.
``steward-relations.json``
    The runner's resolved role relations. The Stop pane header is display-only
    and reuses it; authority paths and Fleet keep the live read.

Every read fails open (missing or corrupt -> ``None``/``{}``) so a caller keeps
its existing full path. Writes are atomic tmp+rename with the temporary file in
the destination directory; no reader can observe a partial payload. Directories
are 0700 and files 0600, and symlinks are never followed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

INBOX_SCHEMA_VERSION = 1
EDGE_GENERATION_SCHEMA = 1
LINEAGE_ROWS_SCHEMA = 1
RUN_STATES_SCHEMA = 1
STEWARD_RELATIONS_SCHEMA = 1
INBOX_DIRECTORY = "recipient-inbox"
GENERATION_SUFFIX = ".lock"

_INBOX_FIELDS = frozenset({
    "schema", "edge_generation", "folded_edge_digest", "dir_identities",
    "delivery_ids", "candidates", "observed_at_ns",
})


def dispatch_inbox_root(home=None, *, jobs=None, environ=None) -> Path:
    """The canonical dispatch root inboxes and edge generations live under.

    One derivation, the same one dispatch state itself uses: the write root is
    ``dispatch_contract.dispatch_state_roots(...)[0]``, never a literal and never
    the session-tidy state root. Callers that keep an explicit registry pass
    ``jobs`` so discovery and the derived inbox cannot name different roots.
    """
    from dispatch_contract import dispatch_state_roots

    env = os.environ if environ is None else environ
    agent_home = Path(home) if home is not None else ROOT
    return Path(dispatch_state_roots(agent_home, jobs, environ=env)[0])


def _inbox_directory(dispatch_root) -> Path:
    return Path(dispatch_root).expanduser() / INBOX_DIRECTORY


def _component(value, field: str) -> str:
    text = str(value or "")
    if (not text or text in {".", ".."} or "\\" in text or "\x00" in text
            or "/" in text or os.sep in text or len(text) > 200):
        raise ValueError(f"recipient-inbox-unsafe-{field}")
    return text


def inbox_path(dispatch_root, harness: str, session_id: str) -> Path:
    """``<dispatch>/recipient-inbox/<harness>/<session_id>.json``."""
    return _inbox_directory(dispatch_root) / _component(harness, "harness") / (
        _component(session_id, "session") + ".json")


def edge_generation_path(dispatch_root) -> Path:
    return _inbox_directory(dispatch_root) / "edge-generation.json"


def lineage_rows_path(dispatch_root) -> Path:
    return _inbox_directory(dispatch_root) / "lineage-rows.json"


def run_states_path(dispatch_root) -> Path:
    return _inbox_directory(dispatch_root) / "run-states.json"


def steward_relations_path(dispatch_root) -> Path:
    return _inbox_directory(dispatch_root) / "steward-relations.json"


def pending_record_path(dispatch_root, storage_key: str, delivery_id: str) -> Path:
    """The existing pending-delivery record path, derived from the dispatch root.

    ``dispatch_pending_delivery.record_directory`` owns this rule; the inbox
    mirrors it so a current-ness check reads one record's metadata without
    importing the delivery writer. ``dispatch_recipient_inbox.test.py`` pins the
    two spellings to the same path.
    """
    digest = hashlib.sha256(str(storage_key).encode("utf-8")).hexdigest()
    return Path(dispatch_root) / "pending-delivery" / digest / f"{delivery_id}.json"


# ---------------------------------------------------------------------------
# Atomic private files
# ---------------------------------------------------------------------------

def _ensure_private_directory(path: Path) -> None:
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError("recipient-inbox-unsafe-directory")


def _read_json(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _atomic_write_json(path: Path, payload) -> None:
    path = Path(path)
    _ensure_private_directory(path.parent)
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise OSError("recipient-inbox-unsafe-file")
    except FileNotFoundError:
        pass
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Edge generation
# ---------------------------------------------------------------------------

def read_edge_generation(dispatch_root) -> int:
    """Current generation, or 0 when the file is missing or not readable."""
    payload = _read_json(edge_generation_path(dispatch_root))
    if not isinstance(payload, dict) or payload.get("schema") != EDGE_GENERATION_SCHEMA:
        return 0
    value = payload.get("generation")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value


def bump_edge_generation(dispatch_root) -> int:
    """Increment and return the generation under an exclusive advisory lock.

    Only confirmed lineage-edge writers call this. A missed bump leaves derived
    inboxes stale, which the runner's periodic full reconciliation repairs; it
    never grants stale authority because permission is re-read per record.
    """
    directory = _inbox_directory(dispatch_root)
    _ensure_private_directory(directory)
    lock_path = directory / ("edge-generation" + GENERATION_SUFFIX)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        value = read_edge_generation(dispatch_root) + 1
        _atomic_write_json(edge_generation_path(dispatch_root),
                           {"schema": EDGE_GENERATION_SCHEMA, "generation": value})
        return value
    finally:
        with contextlib.suppress(OSError):
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def edge_digest(edges) -> str:
    """Digest of the remaining confirmed edges, for the inbox freshness key.

    Order is canonical so two observations of the same edge set agree. Writers
    that can drop edges during a fold bump the generation as well; the digest
    additionally pins the exact set an inbox was derived from.
    """
    digest = hashlib.sha256()
    for edge in sorted(tuple(str(part) for part in item) for item in edges or ()):
        digest.update(json.dumps(edge, ensure_ascii=False).encode("utf-8") + b"\n")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Inbox records
# ---------------------------------------------------------------------------

def _invalid_inbox(payload) -> bool:
    if not isinstance(payload, dict):
        return True
    if not _INBOX_FIELDS.issubset(payload):
        return True
    if payload.get("schema") != INBOX_SCHEMA_VERSION:
        return True
    generation = payload.get("edge_generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        return True
    digest = payload.get("folded_edge_digest")
    if digest is not None and not isinstance(digest, str):
        return True
    if not isinstance(payload.get("dir_identities"), dict):
        return True
    if not isinstance(payload.get("delivery_ids"), list):
        return True
    for item in payload["delivery_ids"]:
        if (not isinstance(item, dict) or not isinstance(item.get("storage_key"), str)
                or not item.get("storage_key")
                or not isinstance(item.get("delivery_id"), str) or not item.get("delivery_id")
                or not item["delivery_id"].startswith("delivery-")
                or "/" in item["delivery_id"] or "\\" in item["delivery_id"]
                or not isinstance(item.get("mtime_ns"), int) or isinstance(item.get("mtime_ns"), bool)):
            return True
    if not isinstance(payload.get("candidates"), list):
        return True
    for item in payload["candidates"]:
        if (not isinstance(item, dict) or not isinstance(item.get("storage_key"), str)
                or not item.get("storage_key") or not isinstance(item.get("attempts"), list)):
            return True
    observed = payload.get("observed_at_ns")
    if not isinstance(observed, int) or isinstance(observed, bool) or observed < 0:
        return True
    return False


def read_inbox(dispatch_root, harness: str, session_id: str):
    """The recorded inbox, or ``None`` when absent, corrupt or another schema."""
    try:
        path = inbox_path(dispatch_root, harness, session_id)
    except ValueError:
        return None
    payload = _read_json(path)
    return None if _invalid_inbox(payload) else payload


def write_inbox(dispatch_root, harness: str, session_id: str, payload) -> None:
    """Atomically replace one recipient's inbox; refuse to publish a broken one."""
    data = dict(payload) if isinstance(payload, dict) else {}
    data["schema"] = INBOX_SCHEMA_VERSION
    data.setdefault("observed_at_ns", time.time_ns())
    if _invalid_inbox(data):
        raise ValueError("recipient-inbox-invalid")
    _atomic_write_json(inbox_path(dispatch_root, harness, session_id), data)


def _directory_identity(path: Path):
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    return [info.st_dev, info.st_ino, info.st_mtime_ns]


def _record_identity(path: Path):
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return info.st_mtime_ns


def is_current(inbox, dispatch_root, *, folded_edge_digest=None) -> bool:
    """Whether a recorded inbox still describes the open record set.

    Edges, named open deliveries and the scopes that could have gained or lost
    a record must all still match. ``folded_edge_digest`` is supplied by a
    caller that just observed the graph; without it the generation remains the
    edge key, and the caller's own graph read decides the digest.
    """
    if _invalid_inbox(inbox):
        return False
    try:
        if inbox["edge_generation"] != read_edge_generation(dispatch_root):
            return False
        if folded_edge_digest is not None and inbox.get("folded_edge_digest") != folded_edge_digest:
            return False
        for scope_dir, expected in inbox["dir_identities"].items():
            actual = _directory_identity(Path(str(scope_dir)))
            if expected is None:
                # The scope did not exist at publication; a directory that
                # appears afterwards may hold a record the inbox never named.
                if actual is not None:
                    return False
                continue
            if actual is None or [int(value) for value in expected] != actual:
                return False
        for item in inbox["delivery_ids"]:
            record = pending_record_path(dispatch_root, item["storage_key"], item["delivery_id"])
            if _record_identity(record) != item["mtime_ns"]:
                return False
    except (OSError, ValueError, TypeError):
        return False
    return True


# ---------------------------------------------------------------------------
# Runner-published derived observations
# ---------------------------------------------------------------------------

def read_lineage_rows(dispatch_root):
    """The compact relation rows, or ``None`` when unusable."""
    payload = _read_json(lineage_rows_path(dispatch_root))
    if not isinstance(payload, dict) or payload.get("schema") != LINEAGE_ROWS_SCHEMA:
        return None
    if not isinstance(payload.get("ledgers"), dict):
        return None
    return payload


def write_lineage_rows(dispatch_root, payload) -> None:
    if (not isinstance(payload, dict) or not isinstance(payload.get("ledgers"), dict)
            or not isinstance(payload.get("directory"), str) or not payload["directory"]):
        raise ValueError("lineage-rows-invalid")
    data = dict(payload)
    data["schema"] = LINEAGE_ROWS_SCHEMA
    data.setdefault("generated_at", time.time())
    _atomic_write_json(lineage_rows_path(dispatch_root), data)


def read_run_states(dispatch_root) -> dict:
    """Public run states; ``{}`` for a missing, corrupt or foreign-shaped file."""
    try:
        payload = _read_json(run_states_path(dispatch_root))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != RUN_STATES_SCHEMA:
        return {}
    return payload if isinstance(payload.get("runs"), dict) else {}


def write_run_states(dispatch_root, payload) -> None:
    """Atomically publish the runner's public run states."""
    data = {"schema": RUN_STATES_SCHEMA, "generated_at": time.time(), "runs": {}}
    if isinstance(payload, dict):
        runs = payload.get("runs")
        if isinstance(runs, dict):
            data["runs"] = dict(runs)
        if "generated_at" in payload:
            data["generated_at"] = payload["generated_at"]
    _atomic_write_json(run_states_path(dispatch_root), data)


def read_steward_relations(dispatch_root):
    """The runner-published role relations, or ``None`` when absent/unusable.

    Display-only observation for the Stop header. ``None`` tells the caller to
    keep its existing live read; authority paths never consume this file.
    """
    try:
        payload = _read_json(steward_relations_path(dispatch_root))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != STEWARD_RELATIONS_SCHEMA:
        return None
    relations = payload.get("relations")
    if not isinstance(relations, list) or not all(isinstance(item, dict) for item in relations):
        return None
    return relations


def write_steward_relations(dispatch_root, relations) -> None:
    """Atomically publish the resolved role relations for display readers."""
    if (not isinstance(relations, (list, tuple))
            or not all(isinstance(item, dict) for item in relations)):
        raise ValueError("steward-relations-invalid")
    _atomic_write_json(steward_relations_path(dispatch_root), {
        "schema": STEWARD_RELATIONS_SCHEMA,
        "generated_at": time.time(),
        "relations": [dict(item) for item in relations],
    })
