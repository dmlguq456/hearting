"""Explicit, restartable small-cycle consolidation. Never runs from a hook.

Backups include the entire cycle, cycle-record bytes, target preimage and day
preimage. The operation journal records effects before acknowledging them.
Only original session record locks -> admission -> day locks are nested.
"""
from __future__ import annotations

import base64
import contextlib
import ctypes
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

import artifact_journal as J
from artifact_identity import IdAllocator, is_well_formed

HOME = ".runtime/journal-migrations/v1"
MAX_BYTES = 256 * 1024


def _json(path):
    return json.loads(Path(path).read_text())


def _save(path, row):
    J.atomic(path, J.canonical(row) + b"\n")


def _fault(point):
    """Patched by isolated failure-injection tests; no operational flag."""


def inventory(path):
    """Exact bounded tree preimage, including directory identity at its root."""
    path = Path(path)
    stamp = path.lstat()
    if not stat.S_ISDIR(stamp.st_mode) or path.is_symlink():
        raise J.JournalError("migration-source-not-directory")
    rows, total = {}, 0
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in sorted(dirs + files):
            child = Path(current) / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise J.JournalError("migration-source-unsupported")
            rel = child.relative_to(path).as_posix()
            if stat.S_ISDIR(mode):
                rows[rel + "/"] = None
                continue
            total += child.stat().st_size
            if total > MAX_BYTES or len(rows) > 200:
                raise J.JournalError("migration-source-too-large")
            fd = os.open(child, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            rows[rel] = hashlib.sha256(raw).hexdigest()
    return {"entries": rows, "bytes": total, "identity": [stamp.st_dev, stamp.st_ino]}


def _same_tree(path, expected, *, identity=False):
    try:
        actual = inventory(path)
        return actual["entries"] == expected["entries"] and (not identity or actual["identity"] == expected["identity"])
    except (OSError, ValueError):
        return False


def _sync_tree(path):
    for current, _dirs, files in os.walk(path):
        for name in files:
            fd = os.open(Path(current) / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _owners(root, route_id, env):
    import turn_change_record as turns
    owners = []
    for harness in turns.HARNESSES:
        for path in sorted((turns.state_dir(env) / harness).glob("*.json")):
            if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
                continue
            state = turns._read(path)
            entry = (state.get("records") or {}).get(str(root), {})
            if entry.get("route_id") == route_id:
                owners.append({"harness": harness, "sid": path.stem, "path": str(path),
                               "writer_protocol": entry.get("writer_protocol")})
    return owners


def candidate(root, cycle_id, env=None):
    import artifact_producer as P
    import inline_finish
    import turn_change_record as turns
    root = Path(root).resolve()
    env = os.environ if env is None else env
    result = {"cycle_id": cycle_id, "eligible": False}
    try:
        record = P.read_cycle_record(root, cycle_id)
        if not record or record.get("state") != "sealed" or record.get("deleted_at") or record.get("journal_migration"):
            raise J.JournalError("not-an-unmigrated-closed-cycle")
        route_path = J.safe_path(root, Path(record["route_file"]), missing=False)
        route = _json(route_path)
        import route_identity
        if route_identity.route_hash(route) != route.get("route_hash"):
            raise J.JournalError("route-identity-unknown")
        if (route.get("effective_intensity") != "direct" or (route.get("selection") or {}).get("shape") != "direct"
                or route.get("execution_topology") != "inline" or route.get("parent_cycle_id")):
            raise J.JournalError("not-small-direct-shape")
        outcome = _json(route_path.with_suffix(".outcome.json"))
        if not outcome.get("terminal_gate_proven") or outcome.get("route_hash") != record["route_hash"]:
            raise J.JournalError("closure-unproven")
        pending = inline_finish.pending_state(root, route["route_id"])
        if not pending or pending.get("schema") != "inline_finish_v1" or pending.get("state") != "finished":
            raise J.JournalError("unsupported-or-pending-writer")
        if P._review_lease_observation(root, cycle_id) in {"live", "unobservable"}:
            raise J.JournalError("live-or-unknown-review")
        automatic = turns.is_automatic_route(route)
        owners = _owners(root, route["route_id"], env)
        if automatic and not owners:
            raise J.JournalError("automatic-writer-owner-unknown")
        if automatic and len(owners) != 1:
            raise J.JournalError("automatic-writer-owner-ambiguous")
        if automatic and any(owner["writer_protocol"] != "journal-v1" for owner in owners):
            raise J.JournalError("automatic-writer-protocol-unsupported")
        directory = P.cycle_dir(root, record["campaign_id"], cycle_id, record)
        J.safe_path(root, directory, missing=False)
        image = inventory(directory)
        files = [name for name in image["entries"] if not name.endswith("/")]
        allowed = {".cycle.json", "manifest.json", "artifacts/auto-record-turns.md"}
        if (not files or any(name not in allowed and not name.startswith("artifacts/finish-evidence/") for name in files)
                or any(name.endswith("/") and name not in {"artifacts/", "artifacts/finish-evidence/"}
                       for name in image["entries"])
                or not any(name.startswith("artifacts/finish-evidence/") for name in files)):
            raise J.JournalError("planned-or-unknown-payload-composition")
        campaign = P.read_campaign(root, record["campaign_id"])
        at = record.get("started_on")
        datetime.fromisoformat(at.replace("Z", "+00:00"))
        day = P.campaign_dir(root, record["campaign_id"], campaign) / "journals" / (at[:10] + ".md")
        result.update(eligible=True, source=str(directory.relative_to(root)), image=image,
                      record=record, route=route, owners=owners, automatic=automatic,
                      journal=str(day.relative_to(root)), at=at, campaign_key=campaign["key"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["reason"] = str(exc)[:200]
    return result


def dry_run(root, env=None):
    import artifact_producer as P
    return {"schema": "journal-migration-plan/v1", "candidates": [
        candidate(root, record["cycle_id"], env) for record in P.list_cycle_records(Path(root))]}


@contextlib.contextmanager
def _record_locks(owners):
    import turn_change_record as turns
    with contextlib.ExitStack() as stack:
        for path in sorted({str(turns._record_lock(Path(owner["path"]))) for owner in owners}):
            lock = stack.enter_context(open(path, "a+b"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise J.JournalError("migration-writer-busy-preserved") from exc
        yield


@contextlib.contextmanager
def _writer_locks(root, choice):
    # Explicit finish/review updates use their existing finish lock. Automatic
    # writers already take record -> finish -> admission in this same order.
    with _record_locks(choice["owners"]):
        path = J.safe_path(root, Path(root) / ".runtime/inline-finish/v1" /
                           choice["route"]["route_id"] / "finish.lock", missing=False)
        with path.open("r+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise J.JournalError("migration-finish-busy-preserved") from exc
            yield


@contextlib.contextmanager
def _admission(root):
    import artifact_admission as A
    fd = A._acquire_lock(root, A.LOCK_TIMEOUT_DEFAULT)
    try:
        yield
    finally:
        A._release_lock(root, fd)


def _preimage(path):
    if path.is_symlink():
        raise J.JournalError("migration-metadata-unsafe")
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else None


def _replace_key(path, before, after):
    """Compare/exchange just the operation's key; callers preserve other fields."""
    current = _preimage(path)
    if current == after:
        return
    if current != before:
        raise J.JournalError("migration-metadata-conflict")
    if after is None:
        path.unlink(missing_ok=True)
    else:
        J.atomic(path, base64.b64decode(after))


def _operation(root, cycle_id):
    rows = []
    folder = J.safe_path(root, Path(root) / HOME / cycle_id)
    for path in sorted(folder.glob("*/operation.json")):
        J.safe_path(root, path, missing=False)
        row = _json(path)
        if row["cycle_id"] == cycle_id:
            rows.append((path, row))
    active = [(path, row) for path, row in rows if row.get("state") != "restored"]
    if len(active) > 1:
        raise J.JournalError("migration-operation-ambiguous")
    return active[0] if active else (None, None)


def apply(root, cycle_id, env=None):
    import artifact_producer as P
    root = Path(root).resolve()
    if not is_well_formed(cycle_id, "cycle"):
        raise J.JournalError("migration-cycle-invalid")
    path, op = _operation(root, cycle_id)
    choice = candidate(root, cycle_id, env) if op is None else op["candidate"]
    if not choice["eligible"]:
        return {"state": "skipped", "cycle_id": cycle_id, "reason": choice.get("reason")}
    # A late writer and orphan carrier take this SAME original-session lock.
    with _writer_locks(root, choice):
        with _admission(root):
            path, op = _operation(root, cycle_id)
            if op is None:
                fresh = candidate(root, cycle_id, env)
                if not fresh["eligible"]:
                    return {"state": "skipped", "reason": fresh.get("reason")}
                choice = fresh
                oid = IdAllocator().allocate("event")
                path = root / HOME / cycle_id / oid / "operation.json"
                op = {"schema": "journal-migration/v1", "operation_id": oid, "cycle_id": cycle_id,
                      "created_at": datetime.now().astimezone().isoformat(),
                      "state": "prepared", "candidate": choice, "entries": []}
                _save(path, op)
        home = path.parent
        source = J.safe_path(root, root / choice["source"])
        backup = home / "backup"
        record_path = P.cycle_record_path(root, cycle_id)
        target_path = J._target_path(root, choice["route"]["route_id"])
        day = root / choice["journal"]
        if op["state"] == "prepared":
            # Copying is outside admission and does not hold the short state lock.
            _fault("before-backup")
            if backup.exists() and not _same_tree(backup, choice["image"]):
                shutil.rmtree(backup)
            if not backup.exists():
                shutil.copytree(source, backup)
            if not _same_tree(backup, choice["image"]):
                raise J.JournalError("migration-backup-verification-failed")
            _sync_tree(backup)
            op.update(state="backed-up", record_preimage=_preimage(record_path),
                      target_preimage=_preimage(target_path))
            _save(path, op); _fault("after-backup")
        with _admission(root):
            if op["state"] in {"committed", "restoring", "restored"}:
                return {"state": op["state"], "operation_id": op["operation_id"]}
            if op["state"] == "backed-up":
                if not _same_tree(source, choice["image"], identity=True):
                    raise J.JournalError("migration-source-changed-preserved")
                if _preimage(record_path) != op["record_preimage"] or _preimage(target_path) != op["target_preimage"]:
                    raise J.JournalError("migration-metadata-changed-preserved")
                with J.day_lock(root, day):
                    # Exact day preimage is durable BEFORE any journal mutation.
                    if "day_preimage" not in op:
                        op["day_preimage"] = _preimage(day)
                    _save(path, op)
                texts = []
                for name in sorted(choice["image"]["entries"]):
                    if name.startswith("artifacts/") and not name.endswith("/"):
                        texts.append(f"### Original {name}\n\n" + (backup / name).read_text())
                binding = {"schema": "journal-target/v1", "route_id": choice["route"]["route_id"],
                    "route_hash": choice["route"]["route_hash"], "campaign_id": choice["record"]["campaign_id"],
                    "campaign_key": choice["campaign_key"], "at": choice["at"], "path": choice["journal"]}
                _fault("before-entry")
                row = J.append(root, binding, origin={"migration": op["operation_id"], "cycle": cycle_id},
                    summary=choice["record"].get("title") or "Consolidated direct record", body="\n".join(texts),
                    results=[{"path": choice["route"]["cwd"], "version": {"kind": "unknown"}, "source": "legacy-record"}],
                    at=choice["at"], session=({"harness": choice["owners"][0]["harness"], "sid": choice["owners"][0]["sid"]}
                                             if choice["owners"] else {}))
                _fault("after-entry-effect")
                op.update(entries=[{"path": row["reference"], "digest": row["digest"],
                                    "artifact_id": row["artifact_id"], "revision_id": row["revision_id"]}], state="entry-written")
                op["target_postimage"] = base64.b64encode(J.canonical(binding) + b"\n").decode()
                _save(path, op); _fault("after-entry")
            if op["state"] == "entry-written":
                current = _json(record_path)
                if current != json.loads(base64.b64decode(op["record_preimage"])):
                    if current.get("journal_migration", {}).get("operation_id") != op["operation_id"]:
                        raise J.JournalError("migration-record-conflict")
                current["journal_migration"] = {"operation_id": op["operation_id"],
                    "backup": str(backup.relative_to(root)), "source": choice["source"], "entry": op["entries"][0]}
                op["record_migration_postimage"] = current["journal_migration"]
                _save(path, op)
                _save(record_path, current)
                _replace_key(target_path, op["target_preimage"], op["target_postimage"])
                _fault("after-rebind-effect")
                op.update(state="rebound"); _save(path, op); _fault("after-rebind")
            if op["state"] == "rebound":
                retired = home / "retired"
                _fault("before-retire")
                if source.exists():
                    if not _same_tree(source, choice["image"], identity=True):
                        raise J.JournalError("migration-source-changed-preserved")
                    _rename_no_replace(source, retired)
                if not _same_tree(retired, choice["image"], identity=True):
                    raise J.JournalError("migration-retired-changed-preserved")
                _fault("after-retire-effect")
                op.update(state="committed"); _save(path, op); _fault("after-commit")
        # The backup is verified; retired is a second preserved original, not a
        # writable target. Keeping it makes interruption recovery non-destructive.
        J.publish_views(root)
        return {"state": op["state"], "operation_id": op["operation_id"], "entries": op["entries"]}


def _rename_no_replace(source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    source_fd, target_fd = J.directory_fd(Path(source).parent), J.directory_fd(Path(target).parent)
    try:
        if rename(source_fd, os.fsencode(Path(source).name), target_fd, os.fsencode(Path(target).name), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(target))
        os.fsync(source_fd); os.fsync(target_fd)
    finally:
        os.close(source_fd); os.close(target_fd)


def restore(root, cycle_id, env=None):
    import artifact_producer as P
    root = Path(root).resolve()
    path, op = _operation(root, cycle_id)
    if op is None:
        # Lost final response: the completed same operation remains readable.
        done = sorted((root / HOME / cycle_id).glob("*/operation.json"), key=lambda p: _json(p).get("created_at", ""))
        if done and _json(done[-1]).get("state") == "restored":
            return {"state": "restored", "operation_id": _json(done[-1])["operation_id"], "replay": True}
        raise J.JournalError("migration-operation-missing")
    if op["state"] not in {"committed", "restoring"}:
        raise J.JournalError("migration-apply-incomplete")
    choice = op["candidate"]
    with _writer_locks(root, choice):
        home = path.parent
        source = J.safe_path(root, root / choice["source"])
        backup = home / "backup"
        if not _same_tree(backup, choice["image"]):
            raise J.JournalError("migration-backup-drift")
        if op["state"] == "committed":
            op.update(state="restoring", restore={"phase": "intent"}); _save(path, op)
        state = op["restore"]
        if state["phase"] == "intent":
            stage = home / "restore-staging"
            if stage.exists():
                # This private operation-owned staging name was never published.
                if stage.is_symlink():
                    raise J.JournalError("restore-staging-conflict")
                shutil.rmtree(stage)
            _fault("before-restore-stage")
            shutil.copytree(backup, stage)
            if not _same_tree(stage, choice["image"]):
                raise J.JournalError("restore-staging-verification-failed")
            _sync_tree(stage)
            stamp = stage.stat()
            state.update(phase="staged", staging_identity=[stamp.st_dev, stamp.st_ino]); _save(path, op)
            _fault("after-restore-stage")
        with _admission(root):
            if state["phase"] == "staged":
                _fault("before-restore-publish")
                expected = {**choice["image"], "identity": state["staging_identity"]}
                if source.exists():
                    # Same bytes alone are insufficient: publication kept the
                    # staged directory's dev/inode, recorded before the rename.
                    if not _same_tree(source, expected, identity=True):
                        raise J.JournalError("restore-destination-conflict")
                else:
                    if not _same_tree(home / "restore-staging", expected, identity=True):
                        raise J.JournalError("restore-staging-conflict")
                    _rename_no_replace(home / "restore-staging", source)
                if not _same_tree(source, expected, identity=True):
                    raise J.JournalError("restore-publish-conflict")
                _fault("after-restore-publish-effect")
                state.update(phase="published"); _save(path, op); _fault("after-restore-publish")
            if state["phase"] == "published":
                day = root / choice["journal"]
                with J.day_lock(root, day):
                    parsed = J.read_day(day, limit=100000, byte_limit=J.WRITE_BYTES)
                    if parsed["coverage"] != "complete":
                        raise J.JournalError("restore-journal-conflict")
                    owned = {row["revision_id"]: row for row in op["entries"]}
                    for row in parsed["rows"]:
                        if row.get("origin", {}).get("migration") == op["operation_id"]:
                            if row["revision_id"] not in owned or row["digest"] != owned[row["revision_id"]]["digest"]:
                                raise J.JournalError("restore-entry-conflict")
                    raw = day.read_bytes()
                    # Remove only exact own blocks; all later entries and any
                    # unrelated prose retain their bytes, including anchors.
                    for row in parsed["rows"]:
                        if row["revision_id"] in owned:
                            raw = raw.replace(J._render(row), b"", 1)
                    _fault("before-restore-entry")
                    J.atomic(day, raw)
                    _fault("after-restore-entry-effect")
                state.update(phase="entry-restored"); _save(path, op); _fault("after-restore-entry")
            if state["phase"] == "entry-restored":
                record_path = P.cycle_record_path(root, cycle_id)
                current = _json(record_path)
                old = json.loads(base64.b64decode(op["record_preimage"]))
                migration = current.get("journal_migration")
                if migration is not None:
                    if migration != op["record_migration_postimage"]:
                        raise J.JournalError("restore-record-conflict")
                    del current["journal_migration"]
                    # Preserve unrelated record fields that appeared later.
                    if current == old:
                        J.atomic(record_path, base64.b64decode(op["record_preimage"]))
                    else:
                        _save(record_path, current)
                _replace_key(J._target_path(root, choice["route"]["route_id"]),
                             op["target_postimage"], op["target_preimage"])
                _fault("after-restore-metadata-effect")
                state.update(phase="metadata-restored"); _save(path, op); _fault("after-restore-metadata")
            op.update(state="restored"); _save(path, op); _fault("after-restore-complete")
        J.publish_views(root)
        return {"state": "restored", "operation_id": op["operation_id"], "replay": False}


def resolve_backup(root, record):
    migration = record.get("journal_migration")
    if not migration:
        return None
    path = J.safe_path(root, Path(root) / migration["backup"], missing=False)
    binding = _json(path / ".cycle.json")
    if binding.get("cycle_id") != record["cycle_id"]:
        raise J.JournalError("migration-backup-binding-invalid")
    return path


def evidence_path(route, path):
    """Resolve an old inline proof by its exact finish's cycle identity.

    A newly created file at a retired spelling is not the old cycle's proof.
    This lookup reads two small existing records, never an operation's large
    day preimage or a root-wide backup census.
    """
    import inline_finish
    import artifact_producer_store as store
    if not isinstance(route.get("artifact_root"), str) or not Path(route["artifact_root"]).is_absolute():
        return Path(path)
    root = Path(route["artifact_root"])
    slot = inline_finish.pending_state(root, route["route_id"])
    if not slot or slot.get("schema") != "inline_finish_v1":
        return Path(path)
    intent = slot.get("intent") or {}
    record = store.read_cycle_record(root, intent.get("cycle_id", ""))
    migration = (record or {}).get("journal_migration")
    if not migration:
        return Path(path)
    if intent.get("route_hash") != route["route_hash"]:
        raise J.JournalError("migration-evidence-identity-mismatch")
    source = root / migration["source"]
    try:
        relative = Path(path).relative_to(source)
    except ValueError as exc:
        raise J.JournalError("migration-evidence-scope-mismatch") from exc
    return J.safe_path(root, resolve_backup(root, record) / relative, missing=False)


def resolve_old_path(path):
    path = Path(path)
    for parent in path.parents:
        if not (parent / HOME).is_dir():
            continue
        import artifact_producer_store as store
        for record in store.list_cycle_records(parent):
            migration = record.get("journal_migration")
            if not migration:
                continue
            source = parent / migration["source"]
            try:
                rel = path.relative_to(source)
            except ValueError:
                continue
            backup = J.safe_path(parent, resolve_backup(parent, record) / rel, missing=False)
            return backup
    return path
