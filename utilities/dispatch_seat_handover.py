#!/usr/bin/env python3
"""Same-seat handover: who now answers for a registered depth-1 attempt.

A cleared window (``/clear``, ``/new``) is a new session id at the same herdr pane.  The
registry row of a depth-1 owner/frame the old session (A) launched still says
``parent_sid=A`` -- that stays, it is the registered identity the receipt, digest, worker
environment and gate paths are signed with.  What changes is *who may resume it and who
receives its completion*: the next session at the same pane (B).

The relation is one fact in two files under the session-tidy state folder:

``handover/<seat>.json``   the snapshot ``session_tidy.py enqueue`` takes: the registered
                           depth-1 attempts the tidying session answers for.  A card's
                           text never grants anything; only this runtime-made snapshot does.
``sessions/<seat>.jsonl``  the seat ledger; ``run_hook`` appends one ``event=handover`` row
                           (A -> B, with the bindings) when it sees B's confirmed start.

Rules: one direction (A -> B), never A -> B and A -> C, B -> C only through B's own next
tidy.  The session at the end of a chain answers for every attempt an earlier row bound unless
a later row handed it elsewhere.  Two ways in: a confirmed clear at the same pane and harness (a ``clear`` start source
or the clear booking's own observation; OpenCode has no start hook, so its first message in
a new session after the snapshot), or the official seat change across panes and harnesses
(``peer-steward start --beside`` -> ACK -> ``retire`` from the pane started beside the
predecessor, :func:`record_retire_handover`).  Everything here is read-only except
:func:`record_locked` / :func:`write_snapshot_locked` (caller holds the seat lock) and
:func:`record_retire_handover`.

Two questions, two functions:

* :func:`effective_parent` / :func:`owns` -- which session may resume the attempt (B).
* :func:`storage_recipients` -- which pending-delivery directories (A's, keyed by the
  registered ``parent_sid``) hold records addressed to B, and for which attempts.

Both answers come from one lineage judgment (:func:`_lineage_holder`), so delivery and resume
authority never disagree.  Both answer "the registered parent" whenever there is no relation, so a call without a
handover behaves exactly as before.  No failure here may block a caller: every public
function returns the registered answer on any error.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shlex
import stat
import sys
import time
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ROOT = HERE.parent
SCHEMA = 1
ROW_EVENT = "handover"
WORKER_TYPES = frozenset({"owner", "frame", "review"})
OPEN_ROW_STATUSES = frozenset({"open", "running"})
LINE_MAX_BYTES = 520
# Maximum distinct logical jobs carried by one handover.  A selected job can
# contribute multiple flat attempt/parent bindings.
MAX_BINDINGS = 12
KEEP_ROWS = 40


def _st():
    import session_tidy
    return session_tidy


def _meta(pipe: str) -> dict:
    pairs = {}
    for part in pipe.split(","):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            pairs[key.strip()] = value
    return pairs


def _jobs_key(jobs) -> str:
    return str(Path(jobs).resolve(strict=False))


def route_identity(meta: dict) -> tuple:
    """(route id, route hash, node) of a registry row: owner rows carry the route as ``route_id`` too."""
    return (meta.get("owner_route_id") or meta.get("route_id") or "",
            meta.get("owner_route_hash") or meta.get("route_hash") or "",
            meta.get("route_node") or "")


def eligible_row(meta: dict) -> bool:
    """A route-bound depth-1 owner/frame; depth-2, route-free and unregistered rows never qualify."""
    route, digest, _ = route_identity(meta)
    return (meta.get("dispatch_depth") == "1" and meta.get("worker_type") in WORKER_TYPES
            and bool(meta.get("parent_sid")) and bool(meta.get("attempt_id")) and bool(route) and bool(digest))


def latest_rows(jobs) -> dict:
    """``attempt_id -> (status, metadata)`` from a registry file (last line of an attempt wins)."""
    try:
        from dispatch_registry_cache import registry_view
        return registry_view(Path(jobs)).handover_latest()
    except OSError:
        return {}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _snapshot_dir() -> Path:
    return _st().state_root() / "handover"


def _snapshot_path(seat_key: str) -> Path:
    return _snapshot_dir() / f"{seat_key}.json"


def read_snapshot(seat_key: str) -> Optional[dict]:
    data = _st().read_json(_snapshot_path(seat_key))
    if isinstance(data, dict) and data.get("schema") == SCHEMA and isinstance(data.get("bindings"), list) \
            and isinstance(data.get("from"), dict):
        return data
    return None


def _all_snapshots(read_context=None) -> list[dict]:
    if read_context is not None:
        return list(read_context.handover_snapshots(_all_snapshots_uncached))
    return _all_snapshots_uncached()


def _all_snapshots_uncached() -> list[dict]:
    out = []
    try:
        names = sorted(p for p in _snapshot_dir().glob("*.json") if p.is_file() and not p.is_symlink())
    except OSError:
        return out
    for path in names:
        data = read_snapshot(path.stem)
        if data:
            out.append(data)
    return out


def handover_rows(seat, *, read_context=None) -> list[dict]:
    """The seat ledger's handover rows, oldest first."""
    def load():
        return [e for e in _st()._read_ledger_lines(seat)
                if e.get("event") == ROW_EVENT and e.get("from")]

    rows = (read_context.handover_rows(seat, load) if read_context is not None else load())
    return sorted(rows, key=lambda r: float(r.get("ts", 0) or 0))


def _peer_message():
    """Load the shared steward-evidence reader without duplicating its rule."""
    path = HERE / "peer-message.py"
    existing = sys.modules.get("peer_message")
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location("peer_message", str(path))
    if spec is None or spec.loader is None:
        raise ImportError("peer-message-reader-unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _transition_rows(seat_key: Optional[str] = None) -> list[tuple[str, dict]]:
    """Read only confirmed steward transitions and releases from the existing seat ledgers."""
    st = _st()
    directory = st.state_root() / "sessions"
    paths = ([directory / f"{seat_key}.jsonl"] if seat_key else
             sorted(path for path in directory.glob("*.jsonl") if path.is_file() and not path.is_symlink()))
    out = []
    for path in paths:
        if path.is_symlink():
            continue
        key = path.stem
        for row in st._read_ledger_lines(st.Seat("pane", key)):
            if row.get("event") in {ROW_EVENT, "steward-release"}:
                out.append((key, row))
    return out


def _role_states(seat_key: Optional[str] = None) -> tuple[dict, set]:
    states, released = {}, set()
    for key, row in _transition_rows(seat_key):
        state = row.get("steward") if isinstance(row.get("steward"), dict) else {}
        for relation in state.get("roles") or ():
            if not isinstance(relation, dict) or not relation.get("generation"):
                continue
            identity = (key, str(relation["generation"]))
            item = dict(relation)
            item.setdefault("seat_key", key)
            states[identity] = item
        if row.get("event") == "steward-release" and row.get("generation"):
            released.add((key, str(row["generation"])))
    return states, released


def steward_marker_generation(marker: dict) -> str:
    """Stable evidence-generation key, including for source-less legacy markers."""
    encoded = json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return str(marker.get("generation") or digest)


def _marker_relation(harness: str, sid: str, marker: dict, evidence: list[dict], seat_key=None) -> dict:
    encoded = json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return {
        "generation": steward_marker_generation(marker),
        "marker_digest": digest,
        "origin": {"harness": harness, "sid": sid},
        "holder": {"harness": harness, "sid": sid},
        "targets": [dict(target) for target in evidence],
        "lineage": [],
        "seat_key": marker.get("seat_key") or seat_key,
        "released": False,
    }


def resolved_steward_relations(harness: Optional[str] = None, sid: Optional[str] = None, *,
                               seat_key: Optional[str] = None, include_released: bool = False) -> list[dict]:
    """Resolve valid original role evidence to its confirmed current same-seat holder.

    This is a read-only projection. It never creates or copies a marker. Ordinary
    handover rows and session aliases are not role evidence.
    """
    try:
        pm = _peer_message()
        roots, _source = pm.steward_marker_roots()
        markers = pm.read_steward_markers(roots)
        if seat_key is not None:
            states, releases = _role_states(seat_key)
        else:
            keys = set()
            legacy_marker = False
            for snapshot in _all_snapshots():
                key = ((snapshot.get("seat") or {}).get("key")
                       if isinstance(snapshot.get("seat"), dict) else None)
                if key:
                    keys.add(str(key))
            for marker in markers.values():
                key = marker.get("seat_key") if isinstance(marker, dict) else None
                if key:
                    keys.add(str(key))
                else:
                    legacy_marker = True
            if legacy_marker:
                directory = _st().state_root() / "sessions"
                try:
                    keys.update(path.stem for path in directory.glob("*.jsonl")
                                if path.is_file() and not path.is_symlink())
                except OSError:
                    pass
            states, releases = {}, set()
            for key in keys:
                current, revoked = _role_states(key)
                states.update(current)
                releases.update(revoked)
    except Exception:  # noqa: BLE001 - unknown authority is never a new role
        return []
    seen = set()
    out = []
    state_by_generation = {}
    for (key, generation), relation in states.items():
        state_by_generation[generation] = (key, relation)
    marker_reader = getattr(pm, "steward_evidence_targets", None)
    for (marker_harness, marker_sid), marker in markers.items():
        if marker_reader is None:
            continue
        generation = steward_marker_generation(marker)
        saved = state_by_generation.get(generation)
        if saved:
            key, relation = saved
            relation = dict(relation)
        else:
            try:
                relation = _marker_relation(marker_harness, marker_sid, marker,
                                            marker_reader(marker), seat_key)
                key = str(seat_key or "")
            except Exception:  # noqa: BLE001
                continue
        identity = (key, generation)
        relation["released"] = relation.get("released") is True or identity in releases
        holder = relation.get("holder") or {}
        if ((harness is not None and holder.get("harness") != harness)
                or (sid is not None and holder.get("sid") != sid)
                or (seat_key is not None and relation.get("seat_key") not in {None, seat_key})
                or (relation["released"] and not include_released)
                or identity in seen):
            continue
        if seat_key is not None and not relation.get("seat_key"):
            relation["seat_key"] = seat_key
        seen.add(identity)
        out.append(relation)
    for (key, generation), saved in states.items():
        identity = (key, generation)
        if identity in seen:
            continue
        relation = dict(saved)
        relation["released"] = relation.get("released") is True or identity in releases
        holder = relation.get("holder") or {}
        if ((harness is not None and holder.get("harness") != harness)
                or (sid is not None and holder.get("sid") != sid)
                or (seat_key is not None and key != seat_key)
                or (relation["released"] and not include_released)):
            continue
        out.append(relation)
    return out


def _watch_states(seat_key: Optional[str] = None) -> dict[str, tuple[str, dict]]:
    states = {}
    for key, row in _transition_rows(seat_key):
        steward = row.get("steward") if isinstance(row.get("steward"), dict) else {}
        for watch in steward.get("watches") or ():
            if isinstance(watch, dict) and watch.get("watch_id"):
                value = dict(watch)
                value.setdefault("seat_key", key)
                states[str(watch["watch_id"])] = (key, value)
    return states


def _watch_roots(pm) -> list[Path]:
    try:
        roots, _source = pm.steward_marker_roots()
    except Exception:
        roots = []
    own = pm.peer_state_root()
    return list(dict.fromkeys([Path(own), *(Path(root) for root in roots)]))


def watch_ack_valid(path: Path, watch_id: str, harness: str, sid: str) -> bool:
    """An ACK settles only when its readable record names this exact recipient."""
    data = _st().read_json(path)
    if not isinstance(data, dict):
        return False
    return (data.get("watch_id") in {None, watch_id}
            and data.get("session_id") == sid
            and data.get("harness") in {None, harness})


def _write_fsynced_temp(path: Path, payload: bytes) -> Path:
    """Write a complete sibling file and fsync it before any canonical publication."""
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short staged state write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
    except BaseException:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def _ack_inode(path: Path, watch_id: str, harness: str, sid: str):
    """Read one regular ACK inode without following links; classify torn vs stale data."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unsafe", None
    identity = (before.st_dev, before.st_ino)
    if not stat.S_ISREG(before.st_mode):
        return "unsafe", identity
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unsafe", identity
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != identity or not stat.S_ISREG(opened.st_mode):
            return "unsafe", identity
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "torn", identity
    if not isinstance(data, dict):
        return "torn", identity
    if (data.get("watch_id") in {None, watch_id}
            and data.get("session_id") == sid
            and data.get("harness") in {None, harness}):
        return "current", identity
    return "other", identity


def _unlink_torn_ack(path: Path, identity) -> bool:
    """Remove only the same unreadable regular inode observed under the seat lock."""
    if identity is None:
        return False
    try:
        current = path.lstat()
        if (not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != identity):
            return False
        os.unlink(path)
        return True
    except OSError:
        return False


def resolved_watch_references(harness: Optional[str] = None, sid: Optional[str] = None, *,
                              seat_key: Optional[str] = None) -> list[dict]:
    """Return exact, unresolved watch duties and their current confirmed recipient."""
    try:
        pm = _peer_message()
        states = _watch_states(seat_key)
        roots = _watch_roots(pm)
    except Exception:  # noqa: BLE001
        return []
    arms: dict[str, dict] = {}
    for root in roots:
        watch_root = root / "peer-watches"
        try:
            paths = sorted(watch_root.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            if path.name.endswith((".receipt.json", ".ack.json", ".observation.json")) or path.is_symlink():
                continue
            arm = _st().read_json(path)
            if not isinstance(arm, dict) or not arm.get("watch_id"):
                continue
            watch_id = str(arm["watch_id"])
            ack = watch_root / f"{watch_id}.ack.json"
            original = arm.get("steward") or {}
            seat = arm.get("steward_seat") or {}
            value = {
                "watch_id": watch_id,
                "origin": {"harness": original.get("harness"), "sid": original.get("session_id")},
                "holder": {"harness": original.get("harness"), "sid": original.get("session_id")},
                "seat_key": seat.get("key"),
                "storage_root": str(root),
                "server": arm.get("server") or "default",
                "target": arm.get("target"),
                "agent": dict(arm.get("agent") or {}),
                "until": list(arm.get("until") or []),
                "armed_ts": arm.get("armed_ts"),
                "deadline": arm.get("deadline"),
            }
            previous = states.get(watch_id)
            ack_holder = ((previous[1].get("holder") or {}) if previous else value["holder"])
            if watch_ack_valid(ack, watch_id, ack_holder.get("harness"), ack_holder.get("sid")):
                continue
            states.setdefault(watch_id, (str(value.get("seat_key") or ""), value))
            arms[watch_id] = value
    out = []
    for watch_id, (key, saved) in states.items():
        value = dict(saved)
        if not value.get("seat_key") and key:
            value["seat_key"] = key
        root = Path(str(value.get("storage_root") or pm.peer_state_root()))
        arm = arms.get(watch_id)
        if arm is not None:
            identity = {
                field: arm.get(field) for field in
                ("storage_root", "server", "target", "agent", "until", "armed_ts",
                 "deadline", "origin", "seat_key")
            }
            if any(value.get(field) != expected for field, expected in identity.items()):
                continue
        holder = value.get("holder") or {}
        if watch_ack_valid(root / "peer-watches" / f"{watch_id}.ack.json", watch_id,
                           holder.get("harness"), holder.get("sid")):
            continue
        if ((harness is not None and holder.get("harness") != harness)
                or (sid is not None and holder.get("sid") != sid)
                or (seat_key is not None and value.get("seat_key") != seat_key)):
            continue
        out.append(value)
    return out


def _watch_reference_from_arm(watch_id: str, root: Path, arm: dict) -> dict:
    steward = arm.get("steward") or {}
    seat = arm.get("steward_seat") or {}
    return {
        "watch_id": watch_id,
        "origin": {"harness": steward.get("harness"), "sid": steward.get("session_id")},
        "holder": {"harness": steward.get("harness"), "sid": steward.get("session_id")},
        "seat_key": seat.get("key"), "storage_root": str(root),
        "server": arm.get("server") or "default", "target": arm.get("target"),
        "agent": dict(arm.get("agent") or {}), "until": list(arm.get("until") or []),
        "armed_ts": arm.get("armed_ts"), "deadline": arm.get("deadline"),
    }


def resolve_watch(watch_id: str, *, seat_key: Optional[str] = None,
                  include_acked: bool = False) -> Optional[dict]:
    """Resolve one duty by its exact arm and seat ledger without scanning all watches."""
    try:
        pm = _peer_message()
        primary = Path(pm.peer_state_root())
        roots = [primary]
        primary_path = primary / "peer-watches" / f"{watch_id}.json"
        if not primary_path.is_file():
            roots.extend(Path(root) for root in _watch_roots(pm) if Path(root) != primary)
        for root in roots:
            watch_root = root / "peer-watches"
            arm_path = watch_root / f"{watch_id}.json"
            arm = _st().read_json(arm_path)
            if isinstance(arm, dict) and arm.get("watch_id") == watch_id:
                value = _watch_reference_from_arm(watch_id, root, arm)
                key = value.get("seat_key")
                if seat_key is not None and key != seat_key:
                    continue
                if key:
                    saved = _watch_states(key).get(watch_id)
                    if saved:
                        row_key, relation = saved
                        if row_key != key or any(relation.get(field) != value.get(field) for field in
                                ("origin", "seat_key", "storage_root", "server", "target", "agent",
                                 "until", "armed_ts", "deadline")):
                            return None
                        value = dict(relation)
                holder = value.get("holder") or {}
                acked = watch_ack_valid(watch_root / f"{watch_id}.ack.json", watch_id,
                                        holder.get("harness"), holder.get("sid"))
                if acked and not include_acked:
                    return None
                return value
        return None
    except Exception:  # noqa: BLE001 - unreadable lineage remains pending, not retargeted
        return None


def watch_delivery_binding(watch_ids: Iterable[str], *, include_acked: bool = False) -> Optional[dict]:
    """Snapshot the current holders and confirmed handover lineages for exact watches."""
    ids = list(watch_ids)
    if not ids or any(not isinstance(watch_id, str) or not watch_id for watch_id in ids):
        return None
    watches = []
    recipient = None
    for watch_id in ids:
        relation = resolve_watch(watch_id, include_acked=include_acked)
        if not isinstance(relation, dict) or relation.get("watch_id") != watch_id:
            return None
        holder = relation.get("holder") or {}
        current = {"harness": holder.get("harness"), "session_id": holder.get("sid")}
        seat_key = relation.get("seat_key")
        lineage = relation.get("lineage") or []
        if (current["harness"] not in {"claude", "codex", "opencode"}
                or not isinstance(current["session_id"], str) or not current["session_id"]
                or not isinstance(seat_key, str) or not seat_key
                or not isinstance(lineage, list)
                or any(not isinstance(item, dict) for item in lineage)):
            return None
        if recipient is not None and recipient != current:
            return None
        recipient = current
        watches.append({
            "watch_id": watch_id,
            "seat_key": seat_key,
            "holder": current,
            # The writer appends each confirmed A -> B transition here. Keep
            # that lineage as the generation; do not infer a second owner.
            "handover_generation": list(lineage),
        })
    return {"recipient": recipient, "watches": watches}


@contextlib.contextmanager
def watch_delivery_binding_lock(binding: dict):
    """Hold the existing seat writer locks while rechecking a watch binding."""
    watches = binding.get("watches") if isinstance(binding, dict) else None
    if not isinstance(watches, list) or not watches:
        yield False
        return
    ids, keys = [], set()
    for watch in watches:
        if not isinstance(watch, dict):
            yield False
            return
        watch_id, key = watch.get("watch_id"), watch.get("seat_key")
        if (not isinstance(watch_id, str) or not watch_id
                or not isinstance(key, str) or not key):
            yield False
            return
        ids.append(watch_id)
        keys.add(key)
    stack = contextlib.ExitStack()
    try:
        import session_tidy
        for key in sorted(keys):
            stack.enter_context(session_tidy.seat_lock(key))
        current = watch_delivery_binding(ids, include_acked=True)
    except (OSError, ValueError, RuntimeError):
        stack.close()
        yield False
        return
    try:
        yield current == binding
    finally:
        stack.close()


def watch_acknowledgements_valid(binding: dict) -> bool:
    """Confirm every exact watch in a locked current binding has a durable ACK."""
    watches = binding.get("watches") if isinstance(binding, dict) else None
    recipient = binding.get("recipient") if isinstance(binding, dict) else None
    if not isinstance(watches, list) or not watches or not isinstance(recipient, dict):
        return False
    for watch in watches:
        if not isinstance(watch, dict):
            return False
        watch_id, seat_key = watch.get("watch_id"), watch.get("seat_key")
        if (not isinstance(watch_id, str) or not watch_id
                or not isinstance(seat_key, str) or not seat_key):
            return False
        relation = resolve_watch(watch_id, seat_key=seat_key, include_acked=True)
        if not isinstance(relation, dict) or relation.get("watch_id") != watch_id:
            return False
        holder = relation.get("holder") or {}
        if (holder.get("harness") != recipient.get("harness")
                or holder.get("sid") != recipient.get("session_id")):
            return False
        root = Path(str(relation.get("storage_root") or ""))
        if (not root.is_absolute()
                or not watch_ack_valid(root / "peer-watches" / f"{watch_id}.ack.json",
                                       watch_id, recipient.get("harness"),
                                       recipient.get("session_id"))):
            return False
    return True


def acknowledge_watch(watch_id: str, harness: str, sid: str, carrier: str) -> str:
    """Create the original watch ACK only for its revalidated current recipient."""
    try:
        row = resolve_watch(watch_id, include_acked=True)
        if row is None:
            return "unknown"
        holder = row.get("holder") or {}
        if holder.get("harness") != harness or holder.get("sid") != sid:
            return "not-recipient"
        root = Path(str(row.get("storage_root") or ""))
        if not root.is_absolute():
            return "unknown"
        path = root / "peer-watches" / f"{watch_id}.ack.json"
        seat_key = row.get("seat_key")
        lock = _st().seat_lock(str(seat_key)) if seat_key else contextlib.nullcontext()
        with lock:
            if root.is_symlink() or path.parent.is_symlink():
                return "unknown"
            identity_fields = ("seat_key", "storage_root", "server", "target", "agent",
                               "until", "armed_ts", "deadline", "origin")
            for _attempt in range(4):
                current = resolve_watch(watch_id, seat_key=seat_key, include_acked=True)
                if not current:
                    return "unknown"
                current_holder = current.get("holder") or {}
                if current_holder != holder:
                    return "not-recipient"
                if any(current.get(field) != row.get(field) for field in identity_fields):
                    return "unknown"

                status, inode = _ack_inode(path, watch_id, harness, sid)
                if status == "current":
                    return "already"
                if status == "torn":
                    # Re-resolve immediately before removing the exact corrupt inode. A
                    # stale predecessor cannot clear a successor's competing ACK.
                    current = resolve_watch(watch_id, seat_key=seat_key, include_acked=True)
                    if (not current or (current.get("holder") or {}) != holder
                            or any(current.get(field) != row.get(field)
                                   for field in identity_fields)):
                        return "not-recipient" if current else "unknown"
                    if not _unlink_torn_ack(path, inode):
                        continue
                    continue
                if status != "missing":
                    return "unknown"

                payload = (json.dumps({"watch_id": watch_id, "carrier": str(carrier)[:80],
                                      "session_id": sid, "harness": harness,
                                      "ts": _st().now_epoch()}, sort_keys=True) + "\n").encode("utf-8")
                tmp = _write_fsynced_temp(path, payload)
                try:
                    try:
                        # A hard link claims the canonical name atomically without
                        # replacing a competing valid ACK.
                        os.link(tmp, path, follow_symlinks=False)
                    except FileExistsError:
                        continue
                    return "created"
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
            return "unknown"
    except Exception:  # noqa: BLE001 - uncertainty does not consume the watch
        return "unknown"


def collect_snapshot_steward(seat, harness: str, sid: str) -> Optional[dict]:
    roles = resolved_steward_relations(harness, sid, seat_key=seat.key)
    watches = resolved_watch_references(harness, sid, seat_key=seat.key)
    if not roles and not watches:
        return None
    return {"schema": 1, "roles": roles, "watches": watches}


def _revalidate_snapshot_steward(seat, harness: str, sid: str, payload: dict) -> Optional[dict]:
    """Recheck collected role/watch identities inside the seat writer transaction."""
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise RuntimeError("snapshot-steward-payload-invalid")
    current_roles = {str(row.get("generation")): row for row in
                     resolved_steward_relations(seat_key=seat.key, include_released=True)}
    roles = []
    for candidate in payload.get("roles") or ():
        if not isinstance(candidate, dict) or not candidate.get("generation"):
            raise RuntimeError("snapshot-steward-role-invalid")
        current = current_roles.get(str(candidate["generation"]))
        if current is None:
            raise RuntimeError("snapshot-steward-role-unresolved")
        if current.get("released") is True:
            continue
        holder = current.get("holder") or {}
        if holder.get("harness") != harness or holder.get("sid") != sid:
            raise RuntimeError("snapshot-steward-holder-changed")
        roles.append(current)

    current_watches = {str(row.get("watch_id")): row for row in
                       resolved_watch_references(seat_key=seat.key)}
    watches = []
    for candidate in payload.get("watches") or ():
        if not isinstance(candidate, dict) or not candidate.get("watch_id"):
            raise RuntimeError("snapshot-watch-invalid")
        watch_id = str(candidate["watch_id"])
        current = current_watches.get(watch_id)
        if current is None:
            root = Path(str(candidate.get("storage_root") or ""))
            holder = candidate.get("holder") or {}
            if (root.is_absolute() and watch_ack_valid(
                    root / "peer-watches" / f"{watch_id}.ack.json", watch_id,
                    holder.get("harness"), holder.get("sid"))):
                continue
            raise RuntimeError("snapshot-watch-unresolved")
        if any(candidate.get(field) != current.get(field) for field in
               ("seat_key", "storage_root", "server", "target", "agent", "until", "deadline", "origin")):
            raise RuntimeError("snapshot-watch-identity-changed")
        holder = current.get("holder") or {}
        if holder.get("harness") != harness or holder.get("sid") != sid:
            raise RuntimeError("snapshot-watch-holder-changed")
        watches.append(current)
    if not roles and not watches:
        return None
    return {"schema": 1, "roles": roles, "watches": watches}


def _append_transition_row(seat, row: dict) -> None:
    """Atomically publish one fsynced transition before any predecessor is retired.

    Rewriting the bounded seat ledger from its complete newline-terminated prefix also
    discards a torn tail left by an older append writer. A failed staged write/fsync never
    changes the canonical ledger, so a retry still sees the predecessor as holder.
    """
    st = _st()
    path = st._ledger_path(seat)
    st.ensure_dir(path.parent)
    st._reject_symlink(path)
    payload = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    current = st.read_bytes(path)
    if current is None:
        if path.exists() or path.is_symlink():
            raise OSError("unable to read seat ledger before transition")
        current = b""
    complete_end = current.rfind(b"\n") + 1
    replacement = current[:complete_end] + payload
    tmp = _write_fsynced_temp(path, replacement)
    try:
        st._reject_symlink(path)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    # Folding is compaction after the durable transition boundary. If it fails, the
    # canonical row is already committed and must not be reported as an uncommitted hop.
    with contextlib.suppress(Exception):
        st._fold_ledger(seat)


def record_steward_release(seat, harness: str, sid: str, generation: str, now=None) -> bool:
    """Durably revoke one exact role generation under the caller's seat lock."""
    if not generation:
        return False
    rows = [row for _key, row in _transition_rows(seat.key)
            if row.get("event") == "steward-release" and row.get("generation") == generation]
    if rows:
        return True
    _append_transition_row(seat, {
        "ts": _st().now_epoch() if now is None else now,
        "harness": harness, "sid": sid, "event": "steward-release",
        "seat_key": seat.key, "generation": generation,
    })
    return True


def _seat_of(key: str, snapshot: Optional[dict] = None):
    st = _st()
    data = (snapshot or {}).get("seat") if snapshot else None
    if isinstance(data, dict) and data.get("key") == key:
        return st.seat_from_dict(data)
    return st.Seat("pane", key)


def pane_seat(env=None, harness: Optional[str] = None, sid: Optional[str] = None):
    """The proven pane or exact native seat; project seats never hand over."""
    st = _st()
    env = os.environ if env is None else env
    detected, own_sid = st.session_from_env(harness, env)
    seat = st.resolve_seat(harness or detected, env=env, sid=sid or own_sid, project=False)
    return seat if seat.kind in ("pane", "native") else None


@contextlib.contextmanager
def readiness_transition_lock(pane: str, server: str, socket_path: Optional[str] = None,
                             *, wait: Optional[float] = None):
    """Use the existing pane-seat lock while a watch checks and publishes readiness.

    Handover and tidy-claim writers for this pane already take this lock. The
    watch lock is per-watch and does not coordinate those writers.
    """
    if not isinstance(pane, str) or not pane or pane == "-":
        raise ValueError("readiness-pane-identity-invalid")
    st = _st()
    env = {}
    if isinstance(server, str) and server and server != "default":
        env["AGENT_HERDR_SESSION"] = server
    if isinstance(socket_path, str) and socket_path:
        env["HERDR_SOCKET_PATH"] = socket_path
    endpoint = st._endpoint_scope(env)
    seat_key = (st._digest("pane", endpoint, pane) if endpoint
                else st._digest("pane", pane))
    with st.seat_lock(seat_key, wait=wait):
        yield


# ---------------------------------------------------------------------------
# Bindings: which registered attempts a session answers for
# ---------------------------------------------------------------------------

def binding_of(jobs, meta: dict) -> dict:
    """``parent`` is the registered parent, the key its pending records stay stored under."""
    route, digest, node = route_identity(meta)
    return {"jobs": _jobs_key(jobs), "attempt": meta["attempt_id"], "route": route, "hash": digest, "node": node,
            "parent": str(meta.get("parent_sid") or "")}


def _binds(bindings: Iterable[dict], jobs, meta: dict) -> bool:
    """A binding is exact on registry file, route id, route hash and node.  A replacement
    attempt of the same logical node shares all four, so the lineage follows it; the attempt
    id in the binding is the audit record, not a key."""
    key = _jobs_key(jobs)
    route, digest, node = route_identity(meta)
    return any(b.get("jobs") == key and b.get("route") == route and b.get("hash") == digest
               and b.get("node") == node for b in bindings if isinstance(b, dict))


def _lineage_seats(extra=(), *, read_context=None) -> dict:
    """Every seat with a handover snapshot, plus ``extra``; each ledger is read once per read context."""
    seats = {}
    for snapshot in _all_snapshots(read_context):
        data = snapshot.get("seat")
        if isinstance(data, dict) and data.get("key"):
            seats.setdefault(str(data["key"]), _seat_of(str(data["key"]), snapshot))
    for seat in extra:
        if seat is not None:
            seats.setdefault(seat.key, seat)
    return {key: handover_rows(seat, read_context=read_context) for key, seat in seats.items()}


def _row_binds_key(row: dict, key: tuple) -> bool:
    return any(isinstance(b, dict) and (b.get("jobs"), b.get("route"), b.get("hash"), b.get("node")) == key
               for b in row.get("bindings") or ())


def _lineage_holder(rows_by_seat: dict, key: tuple, registered: str, harness: str) -> tuple[str, str]:
    """Who answers for one logical job (jobs, route, hash, node) now.

    The latest row that binds the job names its holder (a later hand-off wins).  From there the
    job follows that seat's later same-seat clear rows out of the holder even when a row does
    not repeat the binding; a cross-pane ``retire`` row moves it only when it binds it.  The
    queried session therefore answers only while it is the end of that chain."""
    latest = None
    for seat_key, rows in rows_by_seat.items():
        for index, row in enumerate(rows):
            if row.get("sid") and _row_binds_key(row, key):
                order = (float(row.get("ts", 0) or 0), index)
                if latest is None or order > latest[0]:
                    latest = (order, seat_key, row)
    if latest is None:
        return registered, harness
    order, seat_key, row = latest
    current, harness = str(row["sid"]), str(row.get("harness") or harness)
    seen = {registered, current}
    for index, later in enumerate(rows_by_seat[seat_key]):
        if (float(later.get("ts", 0) or 0), index) <= order:
            continue
        sid = str(later.get("sid") or "")
        if later.get("from") == current and sid and sid not in seen and later.get("source") != "retire":
            current, harness = sid, str(later.get("harness") or harness)
            seen.add(sid)
    return current, harness


def _effective(meta: dict, jobs=None, *, read_context=None, extra_seats=()) -> tuple[str, str]:
    """``(session, harness)`` answering for ``meta``'s attempt now: the lineage holder of its
    logical job.  The chain may cross panes and harnesses (a seat change hands a Codex parent's
    route to a Claude successor), so the rows of every seat with a snapshot are read together;
    only an exact binding on registry file, route id, route hash and node moves a job."""
    registered = str(meta.get("parent_sid") or "")
    harness = str(meta.get("parent_harness") or "")
    if not registered or jobs is None or not eligible_row(meta):
        return registered, harness
    try:
        key = (_jobs_key(jobs), *route_identity(meta))
        return _lineage_holder(_lineage_seats(extra_seats, read_context=read_context), key, registered, harness)
    except Exception:  # noqa: BLE001 - the registered answer is always safe
        return registered, str(meta.get("parent_harness") or "")


def _lineage_identity_key(binding: dict) -> tuple[str, str, str, str]:
    return (str(binding.get("jobs") or ""), str(binding.get("route") or ""),
            str(binding.get("hash") or ""), str(binding.get("node") or ""))


@contextlib.contextmanager
def binding_mutexes(bindings: Iterable[dict], *, wait: Optional[float] = None):
    """Lock exact logical bindings using the existing seat-lock backend.

    The key exists before a snapshot does; it is synchronization only. Writers
    enter while holding their seat transaction, so effect readers must not take
    a seat lock here (seat -> binding is the sole nested order).
    """
    keys = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        identity = _lineage_identity_key(binding)
        if not identity[0] or not identity[1]:
            continue
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
        keys.add("lineage-binding-" + digest)
    with contextlib.ExitStack() as stack:
        for key in sorted(keys):
            stack.enter_context(_st().seat_lock(key, wait=wait))
        yield


@contextlib.contextmanager
def lineage_effect_lock(meta: dict, jobs, expected_parent: str, *, wait: float = 0.05):
    """Serialize an effect with every writer of this binding, including its first one."""
    registered = str(meta.get("parent_sid") or "")
    readable = True
    stack = contextlib.ExitStack()
    try:
        route, digest, node = route_identity(meta)
        binding = {"jobs": _jobs_key(jobs), "route": route, "hash": digest, "node": node}
        stack.enter_context(binding_mutexes([binding], wait=wait))
        # Validate every readable snapshot and ledger under the same mutex
        # used by publication, clear, and retire writers. A missing snapshot
        # is a valid no-handover state, not a synchronization gap.
        try:
            paths = sorted(_snapshot_dir().glob("*.json")) if _snapshot_dir().exists() else []
            if any(path.is_symlink() or (path.is_file() and read_snapshot(path.stem) is None)
                   for path in paths):
                readable = False
            snapshots = _all_snapshots_uncached() if readable else []
            seats = {}
            for snapshot in snapshots:
                data = snapshot.get("seat")
                if isinstance(data, dict) and data.get("key"):
                    key = str(data["key"])
                    seats.setdefault(key, _seat_of(key, snapshot))
            for seat in seats.values():
                ledger = _st()._ledger_path(seat)
                if not ledger.exists():
                    continue
                raw = _st().read_bytes(ledger)
                if raw is None:
                    readable = False
                    break
                for line in raw.splitlines():
                    if line.strip() and not isinstance(json.loads(line), dict):
                        readable = False
                        break
                if not readable:
                    break
        except (OSError, ValueError, TypeError):
            readable = False
        current = _effective(meta, jobs)[0] if readable else ""
    except (OSError, ValueError, TypeError, RuntimeError, BlockingIOError):
        stack.close()
        yield False
        return
    try:
        yield readable and current == expected_parent
    finally:
        stack.close()


def effective_parent(meta: dict, jobs=None, *, read_context=None) -> str:
    """The session that answers for ``meta``'s attempt now: the registered ``parent_sid``, or
    the end of its A -> B (-> C) handover lineage.  Any failure answers the registered parent."""
    return _effective(meta, jobs, read_context=read_context)[0]


def effective_parent_harness(meta: dict, jobs=None, *, read_context=None) -> str:
    """The harness of :func:`effective_parent` (the registered ``parent_harness`` without a handover)."""
    return _effective(meta, jobs, read_context=read_context)[1]


_OWN_SEATS: dict = {}


def _own_seat(session: str):
    """The caller's own seat, only when ``session`` is the caller; resolved once per process.

    Another session's seats come from handover snapshots, so a check over many registry rows
    never probes panes for sessions that cannot be proven from here."""
    try:
        if _st().session_from_env(None, os.environ)[1] != session:
            return None
        key = (session, os.environ.get("HERDR_PANE_ID", ""), os.environ.get("HERDR_SOCKET_PATH", ""))
        if key not in _OWN_SEATS:
            _OWN_SEATS[key] = pane_seat(None, None, session)
        return _OWN_SEATS[key]
    except Exception:  # noqa: BLE001
        return None


def owns(meta: dict, session: str, jobs=None, *, read_context=None) -> bool:
    """True only for the session that answers for ``meta``'s attempt now.

    Without a registry that is the registered parent.  With one it is the end of the
    handover lineage, so a registered parent that handed the job on no longer owns it.
    For the caller's own session its own seat is read too, like
    :func:`storage_recipients`.  If the handover state cannot be read, ``_effective``
    deliberately falls back to the registered parent, preserving the existing safe
    default.
    """
    if not session:
        return False
    if jobs is None:
        return meta.get("parent_sid") == session
    own = _own_seat(session)
    if own is None:
        return effective_parent(meta, jobs, read_context=read_context) == session
    return _effective(meta, jobs, read_context=read_context, extra_seats=(own,))[0] == session


def storage_recipients(session: str, env=None, harness: Optional[str] = None) -> list:
    """``[(storage key, attempt ids | None)]``: where records addressed to ``session`` live.

    The session's own key first (all its records); then one entry per registered parent it
    took over from, limited to the attempts the lineage still gives it.  A pending record is
    stored under the *registered* parent (A) and keeps its delivery id, digest, lease and
    ack there; only the receiving session changes."""
    out: list = [(session, None)]
    try:
        seat = pane_seat(env, harness, session)
        if seat is None:
            return out
        rows_by_seat = _lineage_seats((seat,))
        owed: dict = {}
        for row in _chain_to(rows_by_seat.get(seat.key) or [], session):     # A -> B -> ... -> session
            bindings = [b for b in row.get("bindings") or () if isinstance(b, dict) and b.get("attempt")]
            for binding in bindings:
                key = (binding.get("jobs"), binding.get("route"), binding.get("hash"), binding.get("node"))
                if _lineage_holder(rows_by_seat, key, str(binding.get("parent") or row["from"]), "")[0] != session:
                    continue                    # a later row handed it elsewhere: not this session's to receive
                owed.setdefault(str(row["from"]), set()).add(binding["attempt"])
                if binding.get("parent"):    # a chain that began at another seat: the registered key
                    owed.setdefault(str(binding["parent"]), set()).add(binding["attempt"])
        for key, attempts in owed.items():
            if key != session:
                out.append((key, frozenset(attempts)))
    except Exception:  # noqa: BLE001
        return [(session, None)]
    return out


def _chain_to(rows: list, session: str) -> list:
    """Rows of the handover chain that ends at ``session`` (the row into it, then the rows into its from)."""
    chain, want, seen = [], session, set()
    while want and want not in seen:
        seen.add(want)
        row = next((r for r in reversed(rows) if r.get("sid") == want), None)
        if row is None:
            break
        chain.append(row)
        want = str(row.get("from") or "")
    return chain


def record_for_session(record: dict, allowed: Optional[frozenset]) -> bool:
    """Whether a record found under a storage key may be delivered to the taker: every attempt
    of it is one the handover bound (or the key is the session's own)."""
    if allowed is None:
        return True
    ids = record.get("attempt_ids")
    return isinstance(ids, list) and bool(ids) and all(i in allowed for i in ids)


# ---------------------------------------------------------------------------
# Snapshot (at enqueue) and the ledger row (at the successor's confirmed start)
# ---------------------------------------------------------------------------

def _default_jobs() -> Optional[Path]:
    env = os.environ.get("AGENT_DISPATCH_JOBS")
    if env:
        return Path(env)
    try:
        from dispatch_contract import resolve_dispatch_state_root
        return resolve_dispatch_state_root(ROOT, None) / "jobs.log"
    except Exception:  # noqa: BLE001
        return None


def _delivery_open(jobs, meta: dict) -> bool:
    """A finished attempt still owes its completion when a pending record for it is open."""
    try:
        import dispatch_pending_delivery as pending
        directory = pending.record_directory(Path(jobs).resolve(strict=False).parent, meta["parent_sid"])
        for path in directory.glob("delivery-*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and meta["attempt_id"] in (value.get("attempt_ids") or ()) \
                    and value.get("state") in pending.OPEN_STATES:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _route_open(jobs, meta: dict) -> bool:
    """An acknowledged terminal attempt can still own an unfinished route."""
    route, digest, _ = route_identity(meta)
    filename = meta.get("owner_route_file") or meta.get("route_file")
    try:
        if meta.get("worker_type") == "owner" and meta.get("owner_route_file"):
            # Follow the existing verified owner generation, not an old closed anchor.
            from owner_route_binding import resolve_owner_route_lifecycle
            binding, status = resolve_owner_route_lifecycle(jobs, owner_attempt_id=meta["attempt_id"])
            if binding is None or status not in {"owner-route-launch-binding",
                    "owner-route-post-launch-attachment", "owner-route-advance-current"}:
                return False
            filename, route, digest = binding.route_file, binding.route_id, binding.route_hash
        if not filename:
            return False
        path = Path(filename)
        value = json.loads(path.read_text(encoding="utf-8"))
        return (isinstance(value, dict) and value.get("route_id") == route
                and value.get("route_hash") == digest
                and not os.path.lexists(path.with_suffix(".outcome.json")))
    except Exception:  # noqa: BLE001 - unreadable identity grants no new ownership
        return False


def _logical_key(binding: dict) -> tuple:
    """The stable logical-job key shared by all attempt and parent bindings."""
    return (_jobs_key(binding.get("jobs") or ""), str(binding.get("route") or ""),
            str(binding.get("hash") or ""), str(binding.get("node") or ""))


def _binding_identity(binding: dict) -> tuple:
    """Exact flat identity: only identical attempt/parent rows are duplicates."""
    return (*_logical_key(binding), str(binding.get("attempt") or ""), str(binding.get("parent") or ""))


def _deduplicate_bindings(bindings: Iterable[dict]) -> list[dict]:
    seen = set()
    result = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        identity = _binding_identity(binding)
        if identity not in seen:
            seen.add(identity)
            result.append(binding)
    return result


def _select_binding_rows(rows: Iterable[tuple[dict, Optional[str]]]) -> list[dict]:
    """Select at most ``MAX_BINDINGS`` logical jobs, then retain their full flat history.

    Group priority is the last candidate position for that logical key.  Active
    groups are ranked first, then eligible finished groups; ties therefore need
    no clock or external ordering input.
    """
    candidates = list(rows)
    groups: dict[tuple, dict] = {}
    seen = set()
    unique: list[tuple[dict, Optional[str]]] = []
    for index, (binding, status) in enumerate(candidates):
        key = _logical_key(binding)
        group = groups.setdefault(key, {"last": index, "active": False})
        group["last"] = index
        group["active"] = group["active"] or status in OPEN_ROW_STATUSES
        identity = _binding_identity(binding)
        if identity not in seen:
            seen.add(identity)
            unique.append((binding, status))

    ranked = sorted(groups, key=lambda key: groups[key]["last"], reverse=True)
    active = [key for key in ranked if groups[key]["active"]]
    finished = [key for key in ranked if not groups[key]["active"]]
    selected = set(active[:MAX_BINDINGS])
    selected.update(finished[:max(0, MAX_BINDINGS - len(selected))])
    return [binding for binding, _status in unique if _logical_key(binding) in selected]


def _handover_binding_keys_for_session(jobs, sid: str, *, read_context=None) -> tuple[tuple[str, str, str], ...]:
    """Current exact jobs/route/node bindings handed along to this session."""
    jobs_key = _jobs_key(jobs)
    keys = set()
    for snapshot in _all_snapshots(read_context):
        seat_data = snapshot.get("seat")
        if not isinstance(seat_data, dict):
            continue
        try:
            seat = _seat_of(str(seat_data.get("key") or ""), snapshot)
            chain = _chain_to(handover_rows(seat, read_context=read_context), sid)
        except Exception:  # noqa: BLE001 - discovery failure leaves no inherited candidates
            continue
        for row in chain:
            for binding in row.get("bindings") or ():
                if not isinstance(binding, dict) or binding.get("jobs") != jobs_key:
                    continue
                route = str(binding.get("route") or "")
                digest = str(binding.get("hash") or "")
                node = str(binding.get("node") or "")
                if route and digest:         # a node-less owner binds as (route, hash, "")
                    keys.add((route, digest, node))
    return tuple(keys)


def _handover_candidate_rows(jobs, sid: str, *, read_context=None):
    from dispatch_registry_cache import registry_view
    view = registry_view(
        Path(jobs),
        append_only=bool(getattr(read_context, "registry_append_only", False)),
    )
    keys = _handover_binding_keys_for_session(jobs, sid, read_context=read_context)
    return view.handover_candidates(sid, keys)


def _handover_candidates(harness: str, sid: str, jobs, rows=None, *, read_context=None) -> list[tuple[dict, str]]:
    """Collect every candidate still allowed by the existing route authority."""
    if read_context is None:
        from peer_read_context import PeerReadContext
        read_context = PeerReadContext()     # one pass reads each seat ledger once, however many candidates
    if rows is None:
        try:
            candidate_rows = ((status, record.handover_metadata())
                              for status, record in _handover_candidate_rows(
                                  jobs, sid, read_context=read_context))
        except OSError:
            candidate_rows = ()
    else:
        candidate_rows = rows.values()
    found = []
    import route_authority
    for status, meta in candidate_rows:
        choice = route_authority.decide_continuation(action="handover", status=status, metadata=meta, jobs=jobs,
            facts={"actor": sid, "eligible": eligible_row(meta), "read_context": read_context})
        if choice.allowed:
            found.append((binding_of(jobs, meta), status))
    return found


def _bindings_with_current_status(bindings: Iterable[dict]) -> list[tuple[dict, Optional[str]]]:
    """Attach status only when the current registry row exactly matches a kept binding."""
    cache: dict[str, object] = {}
    result = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        jobs = str(binding.get("jobs") or "")
        if jobs not in cache:
            try:
                from dispatch_registry_cache import registry_view
                cache[jobs] = registry_view(Path(jobs)) if jobs else None
            except OSError:
                cache[jobs] = None
        view = cache[jobs]
        current = view.handover_attempt(str(binding.get("attempt") or "")) if view else None
        status = None
        if current:
            current_status, meta = current
            try:
                if _binding_identity(binding) == _binding_identity(binding_of(jobs, meta)):
                    status = current_status
            except (KeyError, TypeError):
                pass
        result.append((binding, status))
    return result


def current_bindings(harness: str, sid: str, jobs=None, *, read_context=None) -> list:
    """The selected depth-1 attempts ``sid`` answers for, whatever harness registered them.
    The cap is on distinct logical jobs; each selected job keeps all eligible flat bindings.
    A finished (done, cancelled or killed) attempt counts while its route or completion record is
    still open; node-less owner bindings count like node bindings.
    ``harness`` is kept for callers; ownership alone decides."""
    jobs = jobs or _default_jobs()
    if jobs is None or not Path(jobs).is_file():
        return []
    return _select_binding_rows(_handover_candidates(harness, sid, jobs, read_context=read_context))


def write_snapshot_locked(seat, harness: str, sid: str, *, now: Optional[float] = None, jobs=None) -> int:
    """Take (or drop) the snapshot; return its flat binding count.  Caller holds the seat lock."""
    bindings = collect_snapshot_bindings(harness, sid, jobs=jobs)
    steward = collect_snapshot_steward(seat, harness, sid)
    return publish_snapshot_locked(seat, harness, sid, bindings, now=now, jobs=jobs,
                                   steward_payload=steward)


def collect_snapshot_bindings(harness: str, sid: str, jobs=None) -> list[dict]:
    """Read the registry outside a seat lock; publication performs the identity recheck."""
    return current_bindings(harness, sid, jobs)


def publish_snapshot_locked(seat, harness: str, sid: str, bindings: list[dict], *,
                            now: Optional[float] = None, expected_prompt_seq: Optional[int] = None,
                            expected_card_generation: Optional[int] = None, jobs=None,
                            steward_payload: Optional[dict] = None) -> int:
    """Publish a previously collected snapshot only if the seat's card/input window still matches."""
    st = _st()
    now = st.now_epoch() if now is None else now
    if seat.kind not in ("pane", "native"):
        return 0
    if expected_prompt_seq is not None and st.read_prompt_seq(seat) != expected_prompt_seq:
        raise RuntimeError("snapshot-input-changed")
    card = st.read_latest_card(seat)
    generation = int(card.get("generation", 0) or 0) if card else 0
    if expected_card_generation is not None and generation != expected_card_generation:
        raise RuntimeError("snapshot-card-changed")
    if steward_payload:
        steward_payload = _revalidate_snapshot_steward(seat, harness, sid, steward_payload)
    old_snapshot = read_snapshot(seat.key)
    affected = [*(old_snapshot.get("bindings") or [] if old_snapshot else []), *bindings]
    with binding_mutexes(affected):
        if read_snapshot(seat.key) != old_snapshot:
            raise RuntimeError("snapshot-binding-changed")
        if not bindings and not steward_payload:
            with contextlib.suppress(OSError):
                os.unlink(_snapshot_path(seat.key))
            return 0
        path = _snapshot_path(seat.key)
        st._reject_symlink(path)
        snapshot = {
            "schema": SCHEMA, "seat": seat.as_dict(), "from": {"harness": harness, "sid": sid},
            "at": now, "prompt_seq": st.read_prompt_seq(seat), "card_generation": generation,
            "bindings": bindings, "snapshot_id": os.urandom(16).hex()}
        if steward_payload:
            snapshot["steward"] = steward_payload
        st.atomic_write_json(_snapshot_path(seat.key), snapshot)
        return len(bindings)


def record_locked(seat, harness: str, sid: str, event: str, source: str, now: float) -> Optional[dict]:
    """Append the A -> B row when ``sid`` is the confirmed successor of the snapshot's session
    (caller holds the seat lock).  Returns the row, or None (nothing to do / not confirmed)."""
    st = _st()
    if seat.kind not in ("pane", "native") or event not in ("start", "prompt"):
        return None
    snap = read_snapshot(seat.key)
    if not snap:
        return None
    origin = snap["from"]
    old = str(origin.get("sid") or "")
    if not old or old == sid or origin.get("harness") != harness:
        return None
    rows = handover_rows(seat)
    prior = next((r for r in rows if r.get("from") == old and r.get("sid") == sid), None)
    if prior is not None:
        return prior                                   # duplicate callback is the same transition
    summary = st.session_summary(seat).get((harness, sid))
    if summary is None or float(summary.get("first_seen", 0) or 0) + 1.0 < float(snap.get("at", 0) or 0):
        return None                                    # a session older than the snapshot is not its successor
    if not _confirmed_clear(seat, harness, old, sid, event, source):
        return None
    taken = {(b.get("jobs"), b.get("route"), b.get("hash"), b.get("node")) for r in rows
             if r.get("from") == old for b in r.get("bindings") or () if isinstance(b, dict)}
    snapshot_bindings = [b for b in snap["bindings"] if isinstance(b, dict)]
    bindings = [b for b in snapshot_bindings
                if (b.get("jobs"), b.get("route"), b.get("hash"), b.get("node")) not in taken]
    if len(bindings) != len(snap["bindings"]):
        return None                                    # A -> C while A -> B stands: refused

    # Resolve each role generation again inside the seat transaction. A copied
    # snapshot is not authority after a release or a newer confirmed holder.
    snapshot_steward = snap.get("steward") if isinstance(snap.get("steward"), dict) else {}
    current_roles = {str(row.get("generation")): row for row in
                     resolved_steward_relations(seat_key=seat.key, include_released=True)}
    steward_roles = []
    for candidate in snapshot_steward.get("roles") or ():
        if not isinstance(candidate, dict) or not candidate.get("generation"):
            continue
        current = current_roles.get(str(candidate["generation"]))
        if not current or current.get("released"):
            continue
        holder = current.get("holder") or {}
        if holder.get("harness") != harness or holder.get("sid") != old:
            continue
        adopted = dict(current)
        adopted["holder"] = {"harness": harness, "sid": sid}
        adopted["seat_key"] = seat.key
        adopted["lineage"] = [*(current.get("lineage") or []), {
            "from": old, "to": sid, "snapshot_id": snap.get("snapshot_id") or "legacy"}]
        steward_roles.append(adopted)

    current_watches = {str(row.get("watch_id")): row for row in
                       resolved_watch_references(seat_key=seat.key)}
    steward_watches = []
    for candidate in snapshot_steward.get("watches") or ():
        if not isinstance(candidate, dict) or not candidate.get("watch_id"):
            continue
        current = current_watches.get(str(candidate["watch_id"]))
        if not current:
            continue
        holder = current.get("holder") or {}
        if holder.get("harness") != harness or holder.get("sid") != old:
            continue
        if any(candidate.get(field) != current.get(field) for field in
               ("seat_key", "storage_root", "server", "target", "agent", "until", "deadline", "origin")):
            continue
        adopted = dict(current)
        adopted["holder"] = {"harness": harness, "sid": sid}
        adopted["seat_key"] = seat.key
        adopted["lineage"] = [*(current.get("lineage") or []), {
            "from": old, "to": sid, "snapshot_id": snap.get("snapshot_id") or "legacy"}]
        steward_watches.append(adopted)
    steward_payload = ({"schema": 1, "roles": steward_roles, "watches": steward_watches}
                       if steward_roles or steward_watches else None)
    if not bindings and not steward_payload:
        return None

    # A selected job can acquire a permitted retry after enqueue but before the
    # confirmed clear hook.  Refresh only those selected logical keys, retaining
    # the snapshot rows as the original authority boundary.
    selected_keys = {_logical_key(binding) for binding in snapshot_bindings}
    refreshed = []
    for jobs_path in dict.fromkeys(str(binding.get("jobs") or "") for binding in snapshot_bindings):
        if not jobs_path or not Path(jobs_path).is_file():
            continue
        try:
            # Only attempts the snapshot does not bind yet are asked about: the bound ones are
            # kept anyway, and each answer may verify a whole route inside this start hook.
            jobs_key = _jobs_key(jobs_path)
            bound = {str(b.get("attempt") or "") for b in snapshot_bindings
                     if _jobs_key(b.get("jobs") or "") == jobs_key}
            from dispatch_registry_cache import registry_view
            view = registry_view(Path(jobs_path))
            route_keys = {(route, digest, node) for selected_jobs, route, digest, node in selected_keys
                          if selected_jobs == jobs_key}
            fresh = {}
            for status, record in view.handover_candidates(old, route_keys):
                meta = record.handover_metadata()
                aid = meta.get("attempt_id", "")
                if (aid and aid not in bound
                        and (jobs_key, *route_identity(meta)) in selected_keys):
                    fresh[aid] = (status, meta)
            refreshed.extend(binding for binding, _status in _handover_candidates(harness, old, jobs_path, rows=fresh))
        except Exception:  # noqa: BLE001 - the original snapshot remains a safe fallback
            continue
    bindings = _deduplicate_bindings([*bindings, *refreshed])
    row = {"ts": now, "harness": harness, "sid": sid, "event": ROW_EVENT, "from": old,
           "seat_key": seat.key, "snapshot_id": snap.get("snapshot_id"),
           "source": source or ("first-message" if harness == "opencode" else "reservation"),
           "bindings": bindings, "epoch": int((summary or {}).get("epoch", 0) or 0)}
    if steward_payload:
        row["steward"] = steward_payload
    with binding_mutexes(snapshot_bindings):
        if read_snapshot(seat.key) != snap:
            return None
        _append_transition_row(seat, row)
        refreshed_snapshot = dict(snap)
        refreshed_snapshot["bindings"] = bindings
        if steward_payload:
            refreshed_snapshot["steward"] = steward_payload
        try:
            st.atomic_write_json(_snapshot_path(seat.key), refreshed_snapshot)
        except Exception:  # noqa: BLE001 - the durable ledger row remains the handover authority
            pass
    return row


def record_retire_handover(predecessor_sid: str, predecessor_harness: str, successor_sid: str,
                           successor_harness: str, *, env=None, jobs=None,
                           now: Optional[float] = None, successor_seat=None) -> Optional[dict]:
    """Hand a retired predecessor's routes to its seat successor, across harnesses.

    Called after ``peer-steward retire`` proved the predecessor exited and the caller runs in the
    pane started beside it.  The predecessor's unfinished depth-1 work (registered or inherited)
    are bound to the successor in the successor's seat: one ledger row, whose bindings name the
    registered parent their pending records stay stored under, and the seat's snapshot (now
    naming the successor) that lets :func:`effective_parent` find it.  Registry rows are untouched.
    Returns the row (the existing one when repeated), or None when there is nothing to hand over
    -- including attempts the predecessor already handed to another session.
    A deferred retire supplies the seat proven by its immutable requester and
    original successor mark; its observer need not own that physical pane.
    """
    if not predecessor_sid or not successor_sid or predecessor_sid == successor_sid:
        return None
    st = _st()
    seat = successor_seat or pane_seat(env, successor_harness, successor_sid)
    if seat is None or seat.kind != "pane":
        return None
    jobs = jobs or _default_jobs()
    now = st.now_epoch() if now is None else now
    with st.seat_lock(seat.key):
        existing = next((r for r in handover_rows(seat)
                         if r.get("from") == predecessor_sid and r.get("sid") == successor_sid), None)
        if existing is not None:
            return existing
        # Only what the predecessor answers for now: a registered parent that already handed an
        # attempt on keeps its name but no longer answers for it (never A -> B and A -> C).
        view = None
        rows = {}
        if jobs:
            try:
                from dispatch_registry_cache import registry_view
                view = registry_view(Path(jobs))
                rows = {record.handover_metadata().get("attempt_id", ""): (status,
                         record.handover_metadata())
                        for status, record in _handover_candidate_rows(jobs, predecessor_sid)
                        if record.handover_metadata().get("attempt_id")}
            except OSError:
                view = None
                rows = {}
        possible_rows = _handover_candidates(
            predecessor_harness, predecessor_sid, jobs, rows=rows)
        if not possible_rows:
            return None
        # The seat's snapshot names the session now at it (the successor), so its own later
        # /clear hands everything on; what its own snapshot already bound is kept.
        own_before = read_snapshot(seat.key)
        affected = [*(own_before.get("bindings") or [] if own_before else []),
                    *(binding for binding, _status in possible_rows)]
        with binding_mutexes(affected):
            if read_snapshot(seat.key) != own_before:
                return None
            # Revalidate predecessor ownership while holding every affected
            # logical binding mutex, then commit snapshot and ledger together.
            predecessor_rows = [
                (binding, status) for binding, status in possible_rows
                if binding["attempt"] in rows and effective_parent(
                    rows[binding["attempt"]][1], jobs) == predecessor_sid
            ]
            if not predecessor_rows:
                return None
            kept = own_before["bindings"] if own_before and (
                own_before["from"].get("sid") == successor_sid) else []
            union = _select_binding_rows([*_bindings_with_current_status(kept), *predecessor_rows])
            selected_keys = {_logical_key(binding) for binding in union}
            bindings = _deduplicate_bindings(
                binding for binding, _status in predecessor_rows if _logical_key(binding) in selected_keys
            )
            if not bindings:
                return None
            row = {"ts": now, "harness": successor_harness, "sid": successor_sid, "event": ROW_EVENT,
                   "from": predecessor_sid, "source": "retire", "bindings": bindings}
            st.atomic_write_json(_snapshot_path(seat.key), {
                "schema": SCHEMA, "seat": seat.as_dict(),
                "from": {"harness": successor_harness, "sid": successor_sid}, "at": now,
                "bindings": union})
            path = st._ledger_path(seat)
            st.ensure_dir(path.parent)
            st._reject_symlink(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            return row


def _confirmed_clear(seat, harness: str, old: str, sid: str, event: str, source: str) -> bool:
    if harness == "opencode":
        return True                # no start hook: the first message of a new session after the snapshot
    if event == "start" and source == "clear":
        return True
    try:
        import session_tidy_clear as clear
        booking = clear.read_reservation(seat.key)
    except Exception:  # noqa: BLE001
        return False
    return bool(booking) and booking.get("sid") == old and booking.get("harness") == harness and (
        (booking.get("observed") or {}).get("sid") == sid or booking.get("new_session") == sid)


# ---------------------------------------------------------------------------
# The card line: the verified route and the existing resume command
# ---------------------------------------------------------------------------

def resume_line(seat, harness: str, sid: str) -> str:
    """One line for the successor's card: the routes it took over and the existing resume command.
    Only attempts the ledger shows handed to ``sid`` and the registry still shows open count."""
    try:
        rows = [r for r in handover_rows(seat) if r.get("sid") == sid]
        if not rows:
            return ""
        wanted = [b for r in rows for b in r.get("bindings") or () if isinstance(b, dict)]
        # Each registry is read once and each logical job looked at once: the card hook has
        # seconds, and a handover carries one binding per attempt of the same job.
        registries: dict = {}
        looked: set = set()
        shown: dict = {}
        for b in wanted:
            jobs = b.get("jobs")
            identity = (b.get("route"), b.get("hash"), b.get("node"))
            if not jobs or (jobs, identity) in looked or not Path(jobs).is_file():
                continue
            looked.add((jobs, identity))
            if jobs not in registries:
                grouped: dict = {}
                if _jobs_key(jobs) == jobs:            # a binding names its registry by resolved path
                    for status, meta in latest_rows(jobs).values():
                        if eligible_row(meta):
                            grouped.setdefault(route_identity(meta), []).append((status, meta))
                registries[jobs] = grouped
            for status, meta in registries[jobs].get(identity, ()):
                if status not in OPEN_ROW_STATUSES and status != "done":
                    continue
                # A finished attempt is shown only while its notice is owed and its route is
                # still open: a closed route has nothing to resume, whatever record lingers.
                if not owns(meta, sid, jobs) or (status == "done" and not (
                        _delivery_open(jobs, meta) and _route_open(jobs, meta))):
                    continue
                shown.setdefault((jobs, route_identity(meta)[0]), meta.get("route_file", ""))
        parts = []
        for (jobs, route), route_file in list(shown.items())[:2]:
            if route_file:
                from parent_next_directive import resume_command
                command = resume_command(route_file, jobs, agent_home=ROOT)
                parts.append(f"route={route} 이어서: {command}")
        if not parts:
            return ""
        text = "[이어받은 진행 작업] " + " | ".join(parts)
        return _st()._cut_bytes(text, LINE_MAX_BYTES)[0]
    except Exception:  # noqa: BLE001
        return ""
