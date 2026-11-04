"""Memory: `mem migration` parser and helpers.

The migration command parser, operation and join identities, manifests and
phase receipts, graveyard sources, artifact registration and verification,
envelope publishing, and v1 snapshot dumps.

Split out of `mem.py`, which re-exports every name defined here.
"""
import argparse
import functools
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
import migration_v2
import protocol_v2
import sync_v2

from mem_base import (
    _canonical_record_state, _default_headline, _row_to_meta, DELIVERY_STATES, RECORD_COLS,
    RECORD_STATUSES,
)


# ---------- CLI ----------
def _recall_limit(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return parsed


def _migration_leaf(parser, *, mutating=False, epoch=True):
    """Attach the D-71 common contract to one exact migration leaf."""
    if epoch:
        parser.add_argument("--epoch", required=True)
    parser.add_argument("--json", dest="json_output", action="store_true")
    if mutating:
        parser.add_argument("--expect", required=True,
                            help="Current durable migration state digest")
        parser.add_argument("--apply", action="store_true",
                            help="Apply the CAS transition (default: deterministic dry-run)")
    return parser


def _configure_migration_parser(sub):
    """Install the exact public ``mem migration`` namespace from D-71–D-77."""
    migration = sub.add_parser(
        "migration", help="Operate a sealed existing-store protocol-v2 cutover"
    )
    phases = migration.add_subparsers(dest="migration_cmd", required=True,
                                      parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))

    status = _migration_leaf(phases.add_parser("status", help="Read migration state"))
    status.add_argument("--store")
    inspect = _migration_leaf(phases.add_parser("inspect", help="Inspect a store read-only"))
    inspect.add_argument("--store", required=True)
    _migration_leaf(phases.add_parser(
        "capabilities", help="Report writer/fence capability read-only"
    ))

    roster = phases.add_parser("roster", help="Seal membership or final evidence")
    roster_phases = roster.add_subparsers(dest="migration_roster_cmd", required=True,
                                          parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    member = _migration_leaf(roster_phases.add_parser("membership-seal"), mutating=True)
    member.add_argument("--member", action="append", default=[], required=True)
    member.add_argument("--retirement", action="append", default=[])
    member.add_argument("--out", required=True)
    evidence = _migration_leaf(roster_phases.add_parser("evidence-seal"), mutating=True)
    evidence.add_argument("--membership", required=True)
    evidence.add_argument("--replica-evidence", action="append", default=[], required=True)
    evidence.add_argument("--out", required=True)

    # ``snapshot`` is both a mutating leaf and the parent of read-only
    # ``snapshot verify`` in the normative grammar.  Keep its leaf arguments
    # optional here and validate the create form in the dispatcher.
    snapshot = phases.add_parser("snapshot", help="Create or verify a snapshot")
    snapshot.add_argument("--epoch")
    snapshot.add_argument("--json", dest="json_output", action="store_true")
    snapshot.add_argument("--expect")
    snapshot.add_argument("--apply", action="store_true")
    snapshot_phases = snapshot.add_subparsers(dest="migration_snapshot_cmd",
                                              parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    snapshot_verify = _migration_leaf(snapshot_phases.add_parser("verify"), epoch=False)
    snapshot_verify.add_argument("--manifest", required=True)
    snapshot.add_argument("--membership")
    snapshot.add_argument("--replica")
    snapshot.add_argument("--store")
    snapshot.add_argument("--out")

    seed = phases.add_parser("seed", help="Plan, build, verify, or publish seed objects")
    seed_phases = seed.add_subparsers(dest="migration_seed_cmd", required=True,
                                      parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    seed_plan = _migration_leaf(seed_phases.add_parser("plan"))
    seed_plan.add_argument("--snapshot", required=True)
    seed_plan.add_argument("--kind", choices=("snapshot", "delta"), required=True)
    seed_build = _migration_leaf(seed_phases.add_parser("build"), mutating=True)
    seed_build.add_argument("--membership", required=True)
    seed_build.add_argument("--snapshot", required=True)
    seed_build.add_argument("--kind", choices=("snapshot", "delta"), required=True)
    seed_build.add_argument("--source", required=True)
    seed_build.add_argument("--out", required=True)
    seed_verify = _migration_leaf(seed_phases.add_parser("verify"))
    seed_verify.add_argument("--seed-manifest", required=True)
    seed_publish = _migration_leaf(seed_phases.add_parser("publish"), mutating=True)
    seed_publish.add_argument("--evidence", required=True)
    seed_publish.add_argument("--seed-manifest", action="append", default=[], required=True)
    seed_publish.add_argument("--checkout", required=True)
    seed_publish.add_argument("--ref", required=True)

    fence = phases.add_parser("fence", help="Plan, arm, or activate old-writer fence")
    fence_phases = fence.add_subparsers(dest="migration_fence_cmd", required=True,
                                        parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    fence_plan = _migration_leaf(fence_phases.add_parser("plan"))
    fence_plan.add_argument("--membership", required=True)
    fence_arm = _migration_leaf(fence_phases.add_parser("arm"), mutating=True)
    fence_arm.add_argument("--membership", required=True)
    fence_arm.add_argument("--capabilities", action="append", default=[], required=True)
    fence_activate = _migration_leaf(fence_phases.add_parser("activate"), mutating=True)
    fence_activate.add_argument("--membership", required=True)
    fence_activate.add_argument("--barrier-receipt", action="append", default=[], required=True)

    barrier = phases.add_parser("barrier", help="Enter the final semantic-writer barrier")
    barrier_phases = barrier.add_subparsers(dest="migration_barrier_cmd", required=True,
                                            parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    barrier_enter = _migration_leaf(barrier_phases.add_parser("enter"), mutating=True)
    barrier_enter.add_argument("--replica", required=True)

    delta = phases.add_parser("delta", help="Drain the captured post-snapshot tail")
    delta_phases = delta.add_subparsers(dest="migration_delta_cmd", required=True,
                                        parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    delta_drain = _migration_leaf(delta_phases.add_parser("drain"), mutating=True)
    delta_drain.add_argument("--replica", required=True)
    delta_drain.add_argument("--snapshot", required=True)
    delta_drain.add_argument("--fence-receipt", required=True)
    delta_drain.add_argument("--out", required=True)

    no_tail = phases.add_parser("no-tail", help="Verify captured-tail completeness")
    no_tail_phases = no_tail.add_subparsers(dest="migration_no_tail_cmd", required=True,
                                            parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    no_tail_verify = _migration_leaf(no_tail_phases.add_parser("verify"))
    no_tail_verify.add_argument("--replica", required=True)
    no_tail_verify.add_argument("--snapshot", required=True)
    no_tail_verify.add_argument("--delta", required=True)
    no_tail_verify.add_argument("--fence-receipt", required=True)

    fold = _migration_leaf(phases.add_parser("fold"), mutating=True)
    fold.add_argument("--evidence", required=True)
    fold.add_argument("--checkout", required=True)
    compare = _migration_leaf(phases.add_parser("compare"))
    compare.add_argument("--evidence", required=True)
    compare.add_argument("--report", action="append", default=[], required=True)
    compare.add_argument("--ref", required=True)
    activate = _migration_leaf(phases.add_parser("activate"), mutating=True)
    activate.add_argument("--equality", required=True)
    activate.add_argument("--fence-receipt", action="append", default=[], required=True)

    # Joining is a bootstrap, not a cutover: a provably empty store needs no
    # snapshot, seed, barrier, or equality proof, so it gets one leaf instead
    # of the eleven-phase operator protocol.
    join = phases.add_parser(
        "join", help="Join a provably fresh store to an existing exchange")
    join.add_argument("--epoch", help="Defaults to a value derived from this "
                                      "replica and the protected ref")
    join.add_argument("--json", dest="json_output", action="store_true")
    join.add_argument("--apply", action="store_true",
                      help="Apply the join (default: deterministic dry-run)")
    join.add_argument("--sync", action="store_true",
                      help="Run one exchange sync after a successful join")

    rollback = phases.add_parser("rollback", help="Prepare, verify, apply, or close rollback")
    rollback_phases = rollback.add_subparsers(dest="migration_rollback_cmd", required=True,
                                              parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    rollback_prepare = _migration_leaf(rollback_phases.add_parser("prepare"), mutating=True)
    rollback_prepare.add_argument("--equality", required=True)
    rollback_prepare.add_argument("--out", required=True)
    rollback_verify = _migration_leaf(rollback_phases.add_parser("verify"))
    rollback_verify.add_argument("--bundle", required=True)
    rollback_export = _migration_leaf(rollback_phases.add_parser("export-v1"), mutating=True)
    rollback_export.add_argument("--bundle", required=True)
    rollback_export.add_argument("--out", required=True)
    rollback_apply = _migration_leaf(rollback_phases.add_parser("apply"), mutating=True)
    rollback_apply.add_argument("--bundle", required=True)
    rollback_apply.add_argument("--target", required=True)
    rollback_close = _migration_leaf(rollback_phases.add_parser("close"), mutating=True)
    rollback_close.add_argument("--bundle", required=True)
    rollback_close.add_argument(
        "--apply-receipt", action="append", default=[], required=True)


class _MigrationReadConnection(sqlite3.Connection):
    """Connection that removes an optional WAL-safe private read clone."""

    _migration_clone_root = None

    def close(self):
        clone_root = self._migration_clone_root
        try:
            super().close()
        finally:
            if clone_root is not None:
                shutil.rmtree(clone_root, ignore_errors=True)
                self._migration_clone_root = None


def _migration_emit(payload, *, json_output=False):
    result = dict(payload)
    # A receipt's canonical bytes are durable authority. Never append display
    # defaults after its digest has been computed.
    if "receipt_digest" not in result and "manifest_digest" not in result:
        result.setdefault("schema_version", 1)
        result.setdefault("protocol_major", 2)
        result.setdefault("changed", False)
        result.setdefault("status", "local-only")
        result.setdefault("reason", "ok")
        result.setdefault("blocker_ids", [])
        result.setdefault("required_action", "none")
    if json_output:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False))
    else:
        print(f"[migration] {result.get('phase', 'status')}: "
              f"{result.get('status', 'local-only')} "
              f"({result.get('reason', 'ok')})")
        print(f"  state={result.get('migration_state', 'legacy')} "
              f"changed={str(bool(result.get('changed', False))).lower()}")
        if result.get("state_digest"):
            print(f"  state_digest={result['state_digest']}")
        if result.get("receipt_digest"):
            print(f"  receipt_digest={result['receipt_digest']}")
        if result.get("required_action") not in (None, "", "none"):
            print(f"  required_action={result['required_action']}")
    if "exit_code" in result:
        return int(result["exit_code"])
    status = result.get("status", "local-only")
    if status == "planned":
        return 0
    return 2 if status == "hard-failure" else (
        1 if status not in ("local-only", "remote-confirmed") else 0
    )


def _migration_operation(args):
    command = args.migration_cmd
    child = getattr(args, f"migration_{command.replace('-', '_')}_cmd", None)
    return f"{command}.{child}" if child else command


def _migration_join_identity(replica, ref):
    """Derive one stable epoch so a repeated join is the same join."""
    return hashlib.sha256(
        f"fresh-join:{replica}:{ref}".encode()).hexdigest()[:32]


def _migration_join_proof(kind, *, epoch, replica, ref, state):
    return migration_v2.digest_json({
        "kind": kind, "epoch_id": epoch, "replica_id": replica,
        "protected_ref": ref, "object_count": int(state["object_count"]),
        "legacy_nonempty": bool(state["legacy_nonempty"]),
    })


def _migration_require_snapshot_create(args):
    if args.migration_snapshot_cmd == "verify":
        return
    missing = [name for name in ("epoch", "expect", "membership", "replica", "store", "out")
               if not getattr(args, name, None)]
    if missing:
        raise sync_v2.SyncInvariantError(
            "snapshot creation requires " + ",".join(f"--{name}" for name in missing)
        )


def _migration_manifest(module, path):
    return module.load_manifest(Path(path))


def _migration_input_digest(inputs):
    normalized = {
        str(key): (value if isinstance(value, str)
                   and re.fullmatch(r"[0-9a-f]{64}", value)
                   else migration_v2.digest_json(value))
        for key, value in sorted(inputs.items())
    }
    return migration_v2.digest_json(normalized)


def _migration_phase_receipt(epoch, replica_id, phase, membership_digest,
                             state_receipt, *, extra=None):
    details = {"state_receipt": state_receipt,
               "state_digest": state_receipt["state_digest"]}
    details.update(dict(extra or {}))
    return migration_v2.create_phase_receipt(epoch_id=epoch,
        replica_id=replica_id, phase=phase,
        membership_digest=membership_digest, state_receipt=state_receipt,
        extra=details)


def _migration_write_exact(path, raw):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise migration_v2.MigrationError("migration-artifact-equivocation")
        return False
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return True


def _legacy_graveyard_defaults(state, record_id):
    """Fill columns a pre-capsule deletion log never carried.

    Rows deleted before the capsule/status columns existed keep no
    ``canonical_id``, ``status``, ``capsule_version``, ``delivery_state``, or
    ``headline``. The live schema migration writes exactly these defaults for a
    surviving row, so reconstructing the prior state uses the same rule rather
    than inventing state or emitting an invalid post-state.
    """
    state = dict(state)
    if state.get("status") not in RECORD_STATUSES:
        state["status"] = "active"
    if state.get("delivery_state") not in DELIVERY_STATES:
        state["delivery_state"] = "ordinary"
    state["canonical_id"] = state.get("canonical_id") or record_id
    state["capsule_version"] = state.get("capsule_version") or 1
    if not state.get("headline"):
        state["headline"] = _default_headline(state.get("body") or "")
    # The live column backfill recomputes a zero/absent flag from the body, and
    # the protocol rejects a post-state that claims zero for a guarded body.
    if not state.get("injection_flag"):
        state["injection_flag"] = (
            1 if protocol_v2.INJECTION_RE.search(state.get("body") or "") else 0)
    try:
        strength = int(state.get("strength") or 1)
    except (TypeError, ValueError):
        strength = 1
    state["strength"] = max(1, strength)
    return state


def _migration_graveyard_source(raw, snapshot_path):
    snapshot = migration_v2.verify_snapshot(snapshot_path)
    backup = Path(snapshot_path).resolve().parent / snapshot["backup"]["path"]
    con = sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        live = {str(row[0]) for row in con.execute("SELECT id FROM records")}
        # A record the snapshot already covers with v2 operations owns its own
        # lineage: seeding a fresh root from the legacy deletion log would
        # contradict that head. Its tombstone is verified, never re-created.
        names = {str(row[0]) for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        covered = set()
        for table in ("sync_frontier", "sync_transactional_graveyard",
                      "sync_graveyard"):
            if table in names:
                covered |= {str(row[0]) for row in con.execute(
                    f'SELECT record_id FROM "{table}"')}
    finally:
        con.close()
    latest = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            source = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise migration_v2.MigrationError(
                "legacy-graveyard-json-invalid", str(exc)) from exc
        record_id = source.get("id") if isinstance(source, dict) else None
        if not isinstance(record_id, str) or not record_id:
            raise migration_v2.MigrationError("legacy-graveyard-record-invalid")
        latest[record_id] = (source, line)
    entries = []
    for record_id, (source, line) in sorted(latest.items()):
        if record_id in live or record_id in covered:
            continue
        prior = _canonical_record_state(
            _legacy_graveyard_defaults(
                {key: source.get(key) for key in RECORD_COLS}, record_id))
        tombstone = {"action": str(source.get("_action") or "legacy-delete"),
            "pending": prior.get("delivery_state") == "pending",
            "prior_digest": hashlib.sha256(
                protocol_v2.canonical_bytes(prior)).hexdigest(),
            "record_id": record_id}
        payload = {"schema_version": 1, "record_id": record_id,
            "prior_state": prior, "tombstone": tombstone,
            "recovery_evidence_digest": hashlib.sha256(line).hexdigest()}
        entries.append({**payload,
            "entry_digest": migration_v2.digest_json(payload)})
    # Consumers read this artifact as canonical JSONL (``graveyard.jsonl``), so
    # each entry needs its own line separator; bare concatenation parsed as one
    # oversized line and failed every multi-entry store at snapshot time.
    return b"".join(migration_v2.canonical_bytes(item) + b"\n"
                    for item in entries)


def _migration_validate_snapshot_graveyard(con, operations):
    """Prove every v2 graveyard row is already covered by a sealed object."""
    by_id = {item["op_id"]: item["payload"] for item in operations}

    def tombstone(op_id, record_id):
        payload = by_id.get(str(op_id))
        if payload is None:
            raise migration_v2.MigrationError("unseeded-graveyard-evidence")
        matches = [mutation.get("tombstone")
                   for mutation in payload.get("mutations", ())
                   if mutation.get("record_id") == str(record_id)
                   and mutation.get("tombstone") is not None]
        if len(matches) != 1:
            raise migration_v2.MigrationError("unseeded-graveyard-evidence")
        return matches[0]

    tables = {str(row[0]) for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "sync_transactional_graveyard" in tables:
        for op_id, record_id, action, prior_raw, evidence in con.execute(
                "SELECT destructive_op_id,record_id,action,prior_state_bytes,"
                "evidence_digest FROM sync_transactional_graveyard "
                "ORDER BY destructive_op_id,record_id"):
            raw = bytes(prior_raw)
            try:
                prior = protocol_v2.canonical_loads(raw)
            except Exception as exc:
                raise migration_v2.MigrationError(
                    "unseeded-graveyard-evidence", str(exc)) from exc
            sealed = tombstone(op_id, record_id)
            if protocol_v2.canonical_bytes(prior) != raw \
                    or hashlib.sha256(raw).hexdigest() != str(evidence) \
                    or prior.get("id") != str(record_id) \
                    or sealed.get("action") != str(action) \
                    or sealed.get("prior_digest") != str(evidence):
                raise migration_v2.MigrationError("unseeded-graveyard-evidence")
    if "sync_graveyard" in tables:
        for op_id, record_id, tombstone_raw in con.execute(
                "SELECT destructive_op_id,record_id,tombstone_bytes "
                "FROM sync_graveyard ORDER BY destructive_op_id,record_id"):
            raw = bytes(tombstone_raw)
            try:
                decoded = protocol_v2.canonical_loads(raw)
            except Exception as exc:
                raise migration_v2.MigrationError(
                    "unseeded-graveyard-evidence", str(exc)) from exc
            if protocol_v2.canonical_bytes(decoded) != raw \
                    or decoded != tombstone(op_id, record_id):
                raise migration_v2.MigrationError("unseeded-graveyard-evidence")


def _migration_register_artifact(con, *, epoch, kind, digest, path, receipt,
                                 replica=None):
    sync_v2.record_migration_artifact(con, epoch_id=epoch,
        artifact_kind=kind, manifest_digest=digest,
        local_path=Path(path).resolve(strict=True), replica_id=replica,
        receipt_digest=receipt["receipt_digest"])


def _migration_verify_phase_receipts(values, *, epoch, phase, membership):
    expected = [row["replica_id"] for row in membership["members"]]
    receipts = migration_v2.verify_phase_receipts(values, epoch_id=epoch,
        phase=phase, membership_digest=membership["manifest_digest"],
        expected_replica_ids=expected)
    for receipt in receipts:
        state = receipt.get("extra", {}).get("state_receipt")
        if not isinstance(state, dict):
            raise migration_v2.MigrationError("phase-state-receipt-missing")
        migration_v2.verify_phase_receipt(receipt, state_receipt=state)
    return receipts


def _migration_file_inputs(args):
    values = {}
    for name, value in vars(args).items():
        if name.startswith("_"):
            continue
        if name in {"apply", "expect", "json_output", "cmd"} or value in (None, [], False):
            continue
        if name.startswith("migration_"):
            continue
        if name in {"checkout", "store", "out", "target"}:
            values[name] = migration_v2.digest_json(str(value))
            continue
        sequence = value if isinstance(value, list) else [value]
        if name in {"member", "retirement", "replica_evidence", "capabilities",
                    "barrier_receipt", "seed_manifest", "report",
                    "apply_receipt"} or name in {
                        "membership", "manifest", "snapshot", "source", "evidence",
                        "fence_receipt", "delta", "equality", "bundle"}:
            manifests = [migration_v2.load_manifest(item) for item in sequence]
            digests = [item.get("manifest_digest") or item.get("receipt_digest")
                       or migration_v2.digest_json(item) for item in manifests]
            values[name] = digests if isinstance(value, list) else digests[0]
        else:
            values[name] = value
    return values


def _migration_verify_capability(value):
    report = migration_v2.load_manifest(value)
    contract = report.get("writer_contract")
    if not isinstance(contract, dict) \
            or report.get("schema_version") != 1 \
            or report.get("writer_protocol_major") != 2 \
            or report.get("writer_capability_hash") != migration_v2.digest_json(contract) \
            or not isinstance(report.get("replica_id"), str) \
            or not report["replica_id"]:
        raise migration_v2.MigrationError("writer-capability-report-invalid")
    required = {
        "protocol_major": 2,
        "semantic_funnel": "_capture_v2_operation",
        "sqlite_functions": ["hearting_cutover_authority",
                             "hearting_writer_protocol_major"],
        "fence_triggers": ["records:insert", "records:update", "records:delete"],
        "legacy_import_guard": True,
        "replica_rotation_guard": True,
    }
    if contract != required:
        raise migration_v2.MigrationError("writer-capability-contract-unsupported")
    return report


def _migration_verify_replica_evidence(values, membership):
    rows, no_tail = [], {}
    for value in values:
        row = migration_v2.load_manifest(value)
        source = row.get("no_tail_report")
        if source is None:
            raise migration_v2.MigrationError("evidence-no-tail-report-missing")
        report = migration_v2.load_manifest(source)
        claimed = report.get("manifest_digest")
        payload = dict(report)
        payload.pop("manifest_digest", None)
        replica = row.get("replica_id")
        if claimed != migration_v2.digest_json(payload) \
                or claimed != row.get("no_tail_digest") \
                or report.get("kind") != "no-tail" \
                or report.get("proven") is not True \
                or report.get("epoch_id") != membership["epoch_id"] \
                or report.get("membership_digest") != membership["manifest_digest"] \
                or report.get("replica_id") != replica:
            raise migration_v2.MigrationError("evidence-no-tail-report-invalid")
        if replica in no_tail:
            raise migration_v2.MigrationError("evidence-replica-duplicate")
        rows.append(row)
        no_tail[replica] = (report, source if isinstance(source, str) else None)
    expected = {item["replica_id"] for item in membership["members"]}
    if set(no_tail) != expected:
        raise migration_v2.MigrationError("evidence-roster-incomplete")
    return rows, no_tail


def _migration_validate_seed_namespaces(membership, operations):
    """Keep every sealed operation inside its author replica's roster keys."""
    keys_by_replica = {
        str(member["replica_id"]): set(map(str, member["logical_project_keys"]))
        for member in membership["members"]
    }
    for operation in operations:
        payload = operation.get("payload") if isinstance(operation, dict) else None
        if not isinstance(payload, dict):
            raise migration_v2.MigrationError("seed-operation-invalid")
        allowed = keys_by_replica.get(str(payload.get("replica_id", "")))
        if allowed is None or str(payload.get("project_key", "")) not in allowed:
            raise migration_v2.MigrationError("seed-namespace-outside-membership")


def _migration_operation_tree(snapshot):
    return migration_v2.digest_json({"objects": [
        {"op_id": op_id,
         "sha256": hashlib.sha256(
             snapshot.raw_objects[protocol_v2.operation_path(op_id)]).hexdigest()}
        for op_id in sorted(snapshot.operations)
    ]})


def _migration_publish_envelopes(paths):
    """Re-read sealed seed/delta objects once with no-follow digest checks."""
    envelopes, manifests = [], []
    for path in paths:
        try:
            manifest = migration_v2.verify_seed_manifest(path)
        except migration_v2.MigrationError:
            manifest = migration_v2.verify_delta_manifest(path)
        manifests.append(manifest)
        root = Path(path).resolve().parent
        for row in manifest["objects"]:
            object_path = root / row["path"]
            fd = os.open(object_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise migration_v2.MigrationError("seed-object-not-regular")
                chunks = []
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(fd)
            raw = b"".join(chunks)
            envelope = protocol_v2.canonical_loads(raw)
            if len(raw) != row["bytes"] \
                    or hashlib.sha256(raw).hexdigest() != row["sha256"] \
                    or envelope.get("op_id") != row["op_id"]:
                raise migration_v2.MigrationError("publish-object-raced")
            envelopes.append(envelope)
    return manifests, envelopes


def _migration_bundle_artifact_files(artifacts):
    """Re-verify registered local artifact identities and copy exact bytes."""
    files = {}
    for artifact in artifacts:
        root = Path(artifact["local_path"])
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        matched = False
        owner = artifact["replica_id"] or "shared"
        for path in candidates:
            if path.is_symlink():
                raise migration_v2.MigrationError("rollback-artifact-symlink")
            if not path.is_file():
                continue
            relative = path.name if root.is_file() else path.relative_to(root).as_posix()
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = b""
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    raw += chunk
            finally:
                os.close(fd)
            if path.suffix == ".json":
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    value = None
                if isinstance(value, dict) and (
                        value.get("manifest_digest") == artifact["manifest_digest"]
                        or value.get("receipt_digest") == artifact["manifest_digest"]):
                    matched = True
            target = (f"artifacts/{artifact['artifact_kind']}/"
                      f"{owner}/{relative}")
            if target in files and files[target] != raw:
                raise migration_v2.MigrationError("rollback-artifact-collision")
            files[target] = raw
        if not matched:
            raise migration_v2.MigrationError("rollback-artifact-identity-missing")
    return files


def _migration_artifact_manifest_path(artifact, name):
    try:
        root = Path(artifact["local_path"]).resolve(strict=True)
    except OSError as exc:
        raise migration_v2.MigrationError(
            "rollback-artifact-file-missing", str(exc)) from exc
    path = root if root.is_file() else root / name
    if path.is_symlink() or not path.is_file():
        raise migration_v2.MigrationError("rollback-artifact-file-missing")
    return path


def _migration_snapshot_v1_dump(snapshot_path):
    snapshot = migration_v2.verify_snapshot(snapshot_path)
    backup = Path(snapshot_path).resolve().parent / snapshot["backup"]["path"]
    con = sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        rows = con.execute(
            f"SELECT {', '.join(RECORD_COLS)} FROM records ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    chunks = []
    for row in rows:
        meta, body = _row_to_meta(row)
        chunks.append(migration_v2.canonical_bytes(
            _canonical_record_state({**meta, "body": body})))
    return b"".join(chunks)


def _migration_table_rows(con, table):
    names = [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')]
    if not names:
        return []
    result = []
    for row in con.execute(f'SELECT * FROM "{table}" ORDER BY 1'):
        normalized = {}
        for name, value in zip(names, row):
            if isinstance(value, bytes):
                normalized[name] = {"bytes": len(value),
                    "sha256": hashlib.sha256(value).hexdigest()}
            else:
                normalized[name] = value
        result.append(normalized)
    return result
