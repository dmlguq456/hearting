#!/usr/bin/env python3
"""Fixed-size dispatch state for the prompt/Stop hook performance regression.

This is an internal test helper, not an installed user command. It builds one
self-contained, throw-away state tree under an explicit base (the runtime
directory or a temporary root) and refuses to write anywhere near the live
``~/.local/state/hearting`` tree.

The fixture is intentionally larger than the measured live state so a hook
that still walks global state cannot accidentally pass:

* sessions ledgers >= 550 (default 600) with >= 1,000 binding rows
* delivery records >= 3,300, of which >= 3,000 are open ``pending``
* peer waiting history >= 6,000 files
* ``jobs.log`` >= 49 MiB and >= 9,391 six-field rows

Only the target session's own share is small (two open records plus one
predecessor scope through a confirmed succession edge), so doubling the
unrelated global state must not move the target hook's time or opened-file
count. ``scale`` multiplies the unbounded unrelated state (ledgers, deliveries,
peer history, snapshots); the jobs registry, the target's record set, and the
bounded steward-marker set stay fixed.

``self_check`` re-derives the target's storage scopes with the real
``storage_recipients`` and verifies every required format/count, so a fixture
that silently degenerates fails before a timing test reads it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import tempfile
import time

TARGET_SESSION = "perf-target"
TARGET_STORAGE = TARGET_SESSION
PREDECESSOR = "perf-origin"
TARGET_RECORDS = 2
DEFAULT_LEDGERS = 600
DEFAULT_BINDINGS = 1100
DEFAULT_DELIVERIES = 3300
DEFAULT_PEER_ROWS = 6300
DEFAULT_JOBS_ROWS = 9700
MIN_JOBS_BYTES = 49 * 1024 * 1024
JOBS_FILLER_BYTES = 5120
DEFAULT_SNAPSHOTS = 8
DEFAULT_STEWARD_MARKERS = 12
TERMINAL_PREDECESSOR_RECORDS = 3
FIXTURE_SCHEMA = 1

_MINIMUMS = {
    "ledgers": 550,
    "bindings": 1000,
    "deliveries": 3000,
    "open_pending": 3000,
    "peer_rows": 6000,
    "jobs_rows": 9391,
    "jobs_bytes": MIN_JOBS_BYTES,
}


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _write(path: Path, data: str) -> None:
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(data, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _refuse_live(base: Path) -> Path:
    """A fixture must never sit inside the live per-user state tree."""
    resolved = Path(base).expanduser().resolve(strict=False)
    live = [
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "hearting",
        Path.home() / ".local" / "state" / "hearting",
    ]
    for candidate in live:
        try:
            if resolved == candidate or resolved.is_relative_to(candidate):
                raise ValueError(f"fixture base is inside live state: {resolved}")
        except OSError:
            continue
    return resolved


def _binding(jobs: Path, attempt: str, route: str, digest: str, node: str,
             parent: str) -> dict:
    return {"jobs": str(jobs), "attempt": attempt, "route": route, "hash": digest,
            "node": node, "parent": parent}


def _attempt_row(ts: int, slug: str, metadata: dict, *, status: str = "running") -> str:
    return ("%d\t%s\t/w\t/w\t%s\t%s\n" % (
        ts, status, slug, ",".join(f"{key}={value}" for key, value in metadata.items())))


def _record(*, delivery_id: str, attempt_id: str, parent_sid: str, jobs: Path,
            parent_attempt: str = "att-perf-parent",
            route_id: str = "rt-perf") -> dict:
    receipt = {
        "schema_version": 2,
        "state": "ready",
        "parent_attempt_id": parent_attempt,
        "job_registry": str(jobs),
        "children": [{
            "attempt_id": attempt_id,
            "status": "done",
            "readiness": "ready",
            "required_action": "advance-completed",
            "delivery_classification": "success",
        }],
    }
    from dispatch_pending_delivery import _canonical_receipt_digest, recipient_digest
    receipt_digest = _canonical_receipt_digest(receipt)
    return {
        "schema_version": 1,
        "delivery_id": delivery_id,
        "recipient_kind": "claude-parent-runtime",
        "recipient_digest": recipient_digest(parent_sid),
        "session_generation": "fixture",
        "session_generation_supported": "0",
        "attempt_ids": [attempt_id],
        "parent_attempt_id": parent_attempt,
        "route_id": route_id,
        "route_node": "execute",
        "receipt_digest": receipt_digest,
        "receipt": receipt,
        "row_revisions": {attempt_id: "rev-1"},
        "state": "pending",
        "created_at_ns": 1,
        "claimed_at_ns": None,
        "claim_owner": None,
        "claim_deadline_ns": None,
        "claim_authority": "",
        "attempts": 0,
        "last_attempt_at_ns": None,
        "acked_at_ns": None,
        "acked_by": None,
        "expiry_reason": None,
        "lineage": [],
    }


def _write_deliveries(root: Path, count: int, *, target: int, target_session: str,
                      jobs: Path) -> dict:
    """Distribute open records across many recipients; keep the target's share fixed."""
    total = 0
    open_count = 0
    per_key = 6
    key_index = 0
    while total < count:
        storage_key = TARGET_STORAGE if key_index == 0 else f"perf-other-{key_index:05d}"
        for offset in range(per_key):
            if key_index == 0 and offset >= target:
                break
            if total >= count:
                break
            delivery_id = "delivery-" + f"{total:064x}"[:64]
            attempt_id = (f"att-perf-target-{offset}" if key_index == 0
                          else f"att-perf-{key_index:05d}-{offset}")
            record = _record(
                delivery_id=delivery_id, attempt_id=attempt_id,
                parent_sid=storage_key, jobs=jobs,
                # Independent completions: a batch grouping must not fold the
                # target's two samples into one claim.
                parent_attempt=(f"att-perf-parent-{offset}" if key_index == 0
                                else "att-perf-parent"),
                route_id=(f"rt-perf-{offset}" if key_index == 0 else "rt-perf"))
            path = root / "pending-delivery" / record["recipient_digest"] / f"{delivery_id}.json"
            _write(path, json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            total += 1
            if record["state"] == "pending":
                open_count += 1
        key_index += 1
    return {"deliveries": total, "open_pending": open_count}


def _write_terminal_records(root: Path, count: int, *, storage_key: str, jobs: Path) -> list[str]:
    """Terminal records under a granted predecessor scope.

    ``delivery_ids`` is the open-delivery list, so a runner prefill that names
    these would make a carrier keep trying to claim an already-ACKed delivery.
    They stay in the fixture so that path is measured, never only unit-tested.
    """
    delivery_ids = []
    for offset in range(count):
        delivery_id = "delivery-" + f"{0xFEED0000 + offset:064x}"
        attempt_id = f"att-perf-terminal-{offset}"
        record = _record(delivery_id=delivery_id, attempt_id=attempt_id,
                         parent_sid=storage_key, jobs=jobs,
                         parent_attempt="att-perf-terminal-parent",
                         route_id="rt-perf-terminal")
        record.update({
            "state": "acked",
            "claimed_at_ns": 1,
            "claim_owner": "perf-fixture",
            "claim_deadline_ns": None,
            "claim_authority": "deliverer-unproven",
            "acked_at_ns": 1,
            "acked_by": "perf-fixture",
        })
        path = root / "pending-delivery" / record["recipient_digest"] / f"{delivery_id}.json"
        _write(path, json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        delivery_ids.append(delivery_id)
    return delivery_ids


def build(base: str | Path | None = None, *, scale: int = 1, jobs_rows: int = DEFAULT_JOBS_ROWS,
          record_size: int = JOBS_FILLER_BYTES, prime: bool = True,
          prime_via: str = "hook") -> dict:
    """Build one fixed fixture tree and return its paths, env and counts."""
    if scale < 1:
        raise ValueError("scale must be >= 1")
    if base is None:
        base = Path(tempfile.mkdtemp(prefix="hearting-hook-perf-"))
    base = _refuse_live(Path(base))
    home = _private_dir(base / "home")
    xdg = _private_dir(base / "xdg")
    state = _private_dir(base / "state")
    peer_root = _private_dir(base / "peer")
    dispatch_root = _private_dir(state / "dispatch")
    jobs = dispatch_root / "jobs.log"
    sessions = _private_dir(xdg / "hearting" / "session-tidy" / "sessions")
    pending = _private_dir(peer_root / "peer-messages" / "pending")

    ledgers = DEFAULT_LEDGERS * scale
    target_attempts = [f"att-perf-target-{index}" for index in range(TARGET_RECORDS)]
    target_bindings = [
        _binding(jobs, attempt, f"rt-perf-{index}", "sha256:perf", "execute", TARGET_SESSION)
        for index, attempt in enumerate(target_attempts)
    ]

    # One confirmed succession edge gives the target a predecessor scope; every
    # other ledger is unrelated global history.
    edge_rows = [{
        "ts": 1, "event": "succession", "source": "tidy-clear", "harness": "claude",
        "sid": TARGET_SESSION, "from": PREDECESSOR,
        "bindings": target_bindings,
    }]
    _write(sessions / "perf-target.jsonl",
           "".join(json.dumps(row, sort_keys=True) + "\n" for row in edge_rows))
    unrelated_ledgers = 0
    unrelated_bindings = 0
    for index in range(1, ledgers):
        holder = f"perf-holder-{index:05d}"
        rows = []
        for slot in range(2):
            attempt = f"att-perf-other-{index:05d}-{slot}"
            digest = f"sha256:{index:06d}{slot}"
            rows.append(_binding(jobs, attempt, f"rt-perf-{index:05d}-{slot}", digest,
                                 "execute", f"perf-parent-{index:05d}"))
        row = {
            "ts": 1 + index,
            "event": "handover",
            "source": "retire",
            "harness": "claude",
            "sid": holder,
            "from": {"harness": "claude", "sid": f"perf-from-{index:05d}"},
            "bindings": rows,
        }
        _write(sessions / f"perf-{index:05d}.jsonl",
               json.dumps(row, sort_keys=True) + "\n")
        unrelated_ledgers += 1
        unrelated_bindings += len(rows)

    deliveries = _write_deliveries(dispatch_root, DEFAULT_DELIVERIES * scale,
                                   target=TARGET_RECORDS, target_session=TARGET_SESSION,
                                   jobs=jobs)
    terminal_delivery_ids = _write_terminal_records(
        dispatch_root, TERMINAL_PREDECESSOR_RECORDS, storage_key=PREDECESSOR, jobs=jobs)

    # Peer waiting history: the receiver's scan still opens every retained file.
    peer_rows = DEFAULT_PEER_ROWS * scale
    for index in range(peer_rows):
        ref = f"{index:032x}"
        _write(pending / f"{ref}.json",
               json.dumps({"schema_version": 1, "state": "received", "ref": ref},
                          sort_keys=True) + "\n")

    # jobs.log: >= 9,391 six-field rows and >= 49 MiB. Its content is fixed
    # across scale: only unrelated ledgers/deliveries/peer rows scale, so the
    # d2 comparison sees an identical registry.
    filler = "x" * max(0, record_size)
    written = 0
    with jobs.open("w", encoding="utf-8") as handle:
        for index, attempt in enumerate(target_attempts):
            metadata = {
                "attempt_id": attempt,
                "parent_sid": TARGET_SESSION,
                "parent_harness": "claude",
                "dispatch_depth": "1",
                "worker_type": "frame",
                "route_id": f"rt-perf-{index}",
                "route_hash": "sha256:perf",
                "route_node": "execute",
                "delivery_classification": "success",
            }
            handle.write(_attempt_row(100 + index, "perf-target", metadata, status="done"))
            written += 1
        extra = max(0, jobs_rows - written)
        for index in range(extra):
            metadata = {
                "attempt_id": f"att-perf-filler-{index:05d}",
                "parent_sid": f"perf-filler-{index % 97:03d}",
                "dispatch_depth": "1",
                "worker_type": "frame",
                "route_id": "rt-filler",
                "route_hash": "sha256:filler",
                "route_node": "execute",
                "note": filler,
            }
            handle.write(_attempt_row(1000 + index, "perf-filler", metadata))
            written += 1
        # Top up to the byte floor when the row target alone is smaller.
        while handle.tell() < MIN_JOBS_BYTES:
            metadata = {
                "attempt_id": f"att-perf-pad-{written:05d}",
                "parent_sid": "perf-pad",
                "route_id": "rt-pad",
                "route_hash": "sha256:pad",
                "route_node": "execute",
                "note": filler,
            }
            handle.write(_attempt_row(2000 + written, "perf-pad", metadata))
            written += 1
    os.chmod(jobs, 0o600)

    # Handover snapshots and steward markers are read by the live role path.
    # The legacy marker has no seat key on purpose: it makes the live resolver
    # walk every sessions ledger, which is exactly the cost the Stop header
    # must stop paying.
    handover = _private_dir(xdg / "hearting" / "session-tidy" / "handover")
    snapshots = DEFAULT_SNAPSHOTS * scale
    for index in range(snapshots):
        key = f"perf-snapshot-{index:04d}"
        _write(handover / f"{key}.json", json.dumps({
            "schema": 1,
            "seat": {"kind": "pane", "key": key, "pane": f"wPerf:p{index}",
                     "harness": "claude", "project_key": ""},
            "from": {"harness": "claude", "sid": f"perf-from-{index:04d}"},
            "at": 1.0,
            "bindings": [],
        }, sort_keys=True) + "\n")

    steward_root = _private_dir(peer_root / "peer-steward" / "claude")
    # Markers are a bounded seat set, not per-session history: keep the count
    # fixed so the scale comparison attributes growth to unbounded state.
    markers = DEFAULT_STEWARD_MARKERS
    for index in range(markers + 1):
        legacy = index == markers
        sid = "perf-legacy-steward" if legacy else f"perf-steward-{index:04d}"
        marker = {
            "schema_version": 1, "harness": "claude", "session_id": sid,
            "since": 1, "generation": f"{index:032x}",
            "targets": {sid: {"harness": "claude", "session_id": sid,
                              "kind": "watch", "ts": "1"}},
        }
        if not legacy:
            marker["seat_key"] = f"perf-seat-{index:04d}"
        _write(steward_root / f"{sid}.json",
               json.dumps(marker, sort_keys=True) + "\n")

    fixture = {
        "schema": FIXTURE_SCHEMA,
        "base": str(base),
        "home": str(home),
        "xdg": str(xdg),
        "state": str(state),
        "dispatch_root": str(dispatch_root),
        "jobs": str(jobs),
        "sessions": str(sessions),
        "peer_root": str(peer_root),
        "target_session": TARGET_SESSION,
        "target_storage": TARGET_STORAGE,
        "predecessor": PREDECESSOR,
        "env": {
            "HOME": str(home),
            "XDG_STATE_HOME": str(xdg),
            "HARNESS_STATE_ROOT": str(state),
            "AGENT_DISPATCH_JOBS": str(jobs),
            "AGENT_PEER_LEDGER_ROOT": str(peer_root),
        },
        "counts": {
            "ledgers": 1 + unrelated_ledgers,
            "bindings": 1 + unrelated_bindings,
            "peer_rows": peer_rows,
            "snapshots": snapshots,
            "steward_markers": markers + 1,
            "jobs_rows": written,
            "jobs_bytes": jobs.stat().st_size,
            "terminal_records": len(terminal_delivery_ids),
            **deliveries,
        },
        "terminal_delivery_ids": terminal_delivery_ids,
    }
    if prime:
        prime_inbox(fixture, via_runner=prime_via == "runner")
    # The measurement tool reads this to replay the fixture's exact environment.
    _write(base / "fixture.json", json.dumps(fixture, indent=2, sort_keys=True) + "\n")
    return fixture


def _peer_steward_module():
    """The runner module, loaded by path because its filename is hyphenated."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_hook_perf_peer_steward", Path(__file__).with_name("peer-steward.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prime_inbox(fixture: dict, *, via_runner: bool = False) -> dict:
    """Publish the target's derived inbox exactly as a warm first prompt would.

    The fixture is generated by the current code, so the pre-warm uses the real
    discovery path once. A read-only measurement then sees the warm contract;
    the pre-inbox code ignores the file and pays its own global derivation.
    ``via_runner`` uses the obligation runner's prefill instead, so the
    steady-state path that filters terminal records is measured too.
    """
    import dispatch_recipient_inbox as inbox
    import dispatch_seat_handover as handover
    import dispatch_session_sweep as sweep
    with applied_env(fixture):
        root = Path(fixture["dispatch_root"])
        peer_root = Path(fixture["peer_root"])
        if via_runner:
            steward = _peer_steward_module()
            payload = steward._prefill_recipient_inbox(
                root, "claude", fixture["target_session"],
                deadline_ns=time.monotonic_ns() + 60 * 1_000_000_000)
            fixture["inbox_primed_by"] = "runner"
        else:
            sweep.addressed_records(root, "claude-parent-runtime", fixture["target_session"])
            payload = inbox.read_inbox(root, "claude", fixture["target_session"])
            fixture["inbox_primed_by"] = "hook"
        fixture["inbox_primed"] = bool(payload and inbox.is_current(payload, root))
        terminal_ids = set(fixture.get("terminal_delivery_ids") or ())
        if payload and terminal_ids & {item["delivery_id"] for item in payload["delivery_ids"]}:
            raise AssertionError("primed inbox named a terminal record")
        # The runner publishes the compact relation rows; the measurement
        # fixture stands in for that published observation.
        try:
            fixture["lineage_rows"] = handover.publish_lineage_rows(root)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            fixture["lineage_rows"] = None
        # The Stop header's display relation. The legacy marker forces the live
        # resolver through every ledger; publishing it once is the runner's job.
        try:
            relations = handover.resolved_steward_relations(include_released=True)
            inbox.write_steward_relations(root, relations)
            fixture["steward_relations"] = len(relations)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            fixture["steward_relations"] = None
        # A warm recipient index for the exact target: the Stop retry reads the
        # index and the file-name set instead of opening every retained row.
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_hook_perf_peer_message", Path(__file__).with_name("peer-message.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            pending_dir = peer_root / "peer-messages" / "pending"
            names = sorted(path.name for path in pending_dir.glob("*.json"))
            module._publish_recipient_index(
                pending_dir, {"harness": "claude",
                              "session_id": fixture["target_session"]},
                [], names, dispatch_root=peer_root)
            fixture["peer_index_primed"] = True
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError):
            fixture["peer_index_primed"] = False
    return fixture


@contextlib.contextmanager
def applied_env(fixture: dict):
    """Apply the fixture environment only for the duration of a measurement."""
    saved = {key: os.environ.get(key) for key in fixture["env"]}
    os.environ.update(fixture["env"])
    try:
        yield fixture
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def self_check(fixture: dict) -> dict:
    """Verify every format/count minimum and the target's real storage scope."""
    counts = dict(fixture["counts"])
    for name, minimum in _MINIMUMS.items():
        if counts.get(name, 0) < minimum:
            raise AssertionError(f"fixture {name}={counts.get(name)} < {minimum}")

    jobs = Path(fixture["jobs"])
    with jobs.open("rb") as handle:
        rows = 0
        for line in handle:
            rows += 1
            if len(line.rstrip(b"\n").split(b"\t")) != 6:
                raise AssertionError(f"jobs.log row {rows} is not six tab-separated fields")
    counts["jobs_rows_seen"] = rows
    if rows < _MINIMUMS["jobs_rows"] or jobs.stat().st_size < _MINIMUMS["jobs_bytes"]:
        raise AssertionError("jobs.log is smaller than the required fixed size")

    pending_files = list((Path(fixture["peer_root"]) / "peer-messages" / "pending").glob("*.json"))
    if len(pending_files) < _MINIMUMS["peer_rows"]:
        raise AssertionError("peer waiting history is smaller than the fixed size")

    open_records = 0
    for path in (Path(fixture["dispatch_root"]) / "pending-delivery").glob("*/delivery-*.json"):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("state") == "pending":
                open_records += 1
        except (OSError, ValueError):
            raise AssertionError(f"unreadable delivery record: {path}") from None
    counts["open_pending_seen"] = open_records
    if open_records < _MINIMUMS["open_pending"]:
        raise AssertionError("open pending deliveries are below the fixed size")

    terminal_ids = set(fixture.get("terminal_delivery_ids") or ())
    terminal_records = 0
    import dispatch_pending_delivery as pending
    predecessor_dir = pending.record_directory(Path(fixture["dispatch_root"]),
                                               fixture["predecessor"])
    for path in predecessor_dir.glob("delivery-*.json"):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("state") != "pending":
                terminal_records += 1
        except (OSError, ValueError):
            raise AssertionError(f"unreadable predecessor record: {path}") from None
    counts["terminal_records_seen"] = terminal_records
    if terminal_records < TERMINAL_PREDECESSOR_RECORDS or len(terminal_ids) < TERMINAL_PREDECESSOR_RECORDS:
        raise AssertionError("the predecessor scope has no terminal records")

    import dispatch_seat_handover as handover
    from peer_read_context import PeerReadContext
    with applied_env(fixture):
        scopes = dict(handover.storage_recipients(
            fixture["target_session"], harness="claude", read_context=PeerReadContext()))
    if fixture["target_storage"] not in scopes:
        raise AssertionError(f"target storage {fixture['target_storage']!r} missing from {sorted(scopes)}")
    if fixture["predecessor"] not in scopes:
        raise AssertionError(f"predecessor scope {fixture['predecessor']!r} missing from {sorted(scopes)}")
    counts["target_scopes"] = sorted(scopes)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="")
    parser.add_argument("--scale", type=int, default=1)
    parser.add_argument("--jobs-rows", type=int, default=DEFAULT_JOBS_ROWS)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    fixture = build(args.base or None, scale=args.scale, jobs_rows=args.jobs_rows)
    if args.check:
        fixture["counts"] = self_check(fixture)
    print(json.dumps(fixture, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
