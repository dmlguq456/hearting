#!/usr/bin/env python3
"""Fixed-size dispatch state for the prompt/Stop hook performance regression.

This is an internal test helper, not an installed user command. It builds one
self-contained, throw-away state tree under an explicit base (the runtime
directory or a temporary root) and refuses to write anywhere near the live
``~/.local/state/hearting`` tree.

The fixture is sized to the machine's live state (measured 2026-11-03: a
55 MB / ~10,000-row ``jobs.log`` with ~1,600 claimed-and-started depth-1
owner/frame rows, ~660 sessions ledgers, ~7,600 peer rows with two
``.lock-<ref>`` files each), so a hook that still walks global state cannot
accidentally pass:

* sessions ledgers >= 650 (default 660) with >= 1,300 binding rows
* delivery records >= 3,300, of which >= 3,000 are open ``pending``
* peer waiting history >= 7,400 rows (mostly ``received``, a few hundred
  verifiable ``queued``/``pending`` rows for unrelated recipients, each with
  its transfer record) plus two ``.lock-<ref>`` files per row
* ``jobs.log`` exactly 10,000 six-field rows and >= 52 MiB, of which >= 1,500
  are depth-1 owner/frame rows that the asyncRewake hook scans
  (``REGISTRY_DEPTH1_START`` in ``hooks/dispatch-owner-rewake.py``), bound to
  unrelated sessions, ``open`` and ``done`` mixed
* the target's own transcript (fixed size)

Only the target session's own share is small (two open records plus one
predecessor scope through a confirmed succession edge, no owner row of its
own), so doubling the unrelated global state must not move the target hook's
time or its per-record-kind opened-file and listing counts. ``scale``
multiplies the unbounded unrelated state (ledgers, deliveries, peer history,
snapshots) and the registry's unrelated depth-1 rows; those rows replace filler
rows, so the registry keeps the same row count and byte size and only the
per-row work a scan does can grow. The target's record set, transcript and the
bounded steward-marker set stay fixed.

``self_check`` re-derives the target's storage scopes with the real
``storage_recipients`` and verifies every required format/count, so a fixture
that silently degenerates fails before a timing test reads it.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time

TARGET_SESSION = "perf-target"
TARGET_STORAGE = TARGET_SESSION
PREDECESSOR = "perf-origin"
TARGET_RECORDS = 2
DEFAULT_LEDGERS = 660
DEFAULT_BINDINGS = 1320
DEFAULT_DELIVERIES = 3300
DEFAULT_PEER_ROWS = 7500
# Per 1,000 peer rows: live 2026-11-03 held 368 queued and 163 pending of 7,611.
PEER_QUEUED_PER_MILLE = 48
PEER_PENDING_PER_MILLE = 22
PEER_LOCKS_PER_ROW = 2  # live: 14,654 `.lock-<ref>` files for 7,611 rows
DEFAULT_JOBS_ROWS = 10000
# Every unrelated registry row is padded to exactly this many bytes, so the
# registry size is fixed by its row count whatever mix of kinds it holds.
JOBS_ROW_BYTES = 5500
MIN_JOBS_BYTES = 52 * 1024 * 1024
JOBS_FILLER_BYTES = JOBS_ROW_BYTES  # backward-compatible name for ``record_size``
DEFAULT_DEPTH1_ROWS = 1600
DEPTH1_OPEN_EVERY = 16  # 1 in 16 unrelated depth-1 rows is still ``open``
TRANSCRIPT_ROWS = 2000
DEFAULT_SNAPSHOTS = 8
DEFAULT_STEWARD_MARKERS = 12
TERMINAL_PREDECESSOR_RECORDS = 3
FIXTURE_SCHEMA = 2

_MINIMUMS = {
    "ledgers": 650,
    "bindings": 1300,
    "deliveries": 3000,
    "open_pending": 3000,
    "peer_rows": 7400,
    "peer_open_rows": 400,
    "peer_locks": 14800,
    "jobs_rows": 10000,
    "jobs_bytes": MIN_JOBS_BYTES,
    "depth1_rows": 1500,
    "depth1_open": 50,
    "transcript_rows": TRANSCRIPT_ROWS,
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


def _peer_state(index: int) -> str:
    slot = index % 1000
    if slot < PEER_QUEUED_PER_MILLE:
        return "queued"
    if slot < PEER_QUEUED_PER_MILLE + PEER_PENDING_PER_MILLE:
        return "pending"
    return "received"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _empty_private_file(path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)


def _write_peer_rows(peer_root: Path, count: int, *, now: float) -> dict:
    """Peer waiting history: ``received`` rows plus open rows for unrelated recipients.

    An open row is shaped exactly as ``prepare_peer_message`` writes it (row
    plus transfer record, body and source digests), so a full scan verifies it
    instead of quarantining it; ``self_check`` re-reads every open row through
    the real ``_read_pending``. The ``.lock-<ref>`` files are what the send
    path leaves behind: they are not rows, but every listing of the directory
    pays for them.
    """
    pending = _private_dir(peer_root / "peer-messages" / "pending")
    transfers = _private_dir(peer_root / "peer-messages" / "transfers")
    states = {"received": 0, "queued": 0, "pending": 0}
    locks = 0
    for index in range(count):
        ref = f"{index:032x}"
        state = _peer_state(index)
        states[state] += 1
        if state == "received":
            row = {"schema_version": 1, "state": "received", "ref": ref}
        else:
            sender = {"harness": "claude", "session_id": f"perf-peer-sender-{index % 53:03d}"}
            recipient = {"harness": "codex" if index % 3 == 0 else "claude",
                         "session_id": f"perf-peer-recipient-{index:05d}"}
            body = f"perf peer body {index}"
            text = (f"{body}\n\n(peer-from: claude [?] ; pane=- ; "
                    f"sid={sender['session_id']} ; ref={ref})")
            body_sha256 = _sha256(text)
            row = {"schema_version": 1, "ref": ref, "from": sender, "to": recipient,
                   "body_sha256": body_sha256, "source_sha256": _sha256(body),
                   "refs": [], "created": now - 60 - index, "state": state,
                   "text": text, "no_trailer": False, "receipt": "target-form-open"}
            _write(transfers / f"{ref}.json", json.dumps({
                "schema_version": 1, "message_id": ref, "from": sender, "to": recipient,
                "body_sha256": body_sha256, "refs": []}) + "\n")
        _write(pending / f"{ref}.json", json.dumps(row, sort_keys=True) + "\n")
        # The row's own send lock plus one left by an already-pruned row.
        _empty_private_file(pending / f".lock-{ref}")
        locks += 1
        for extra in range(1, PEER_LOCKS_PER_ROW):
            _empty_private_file(pending / f".lock-{0xF0000000 * extra + index:032x}")
            locks += 1
    return {"peer_rows": count, "peer_open_rows": states["queued"] + states["pending"],
            "peer_queued": states["queued"], "peer_pending": states["pending"],
            "peer_locks": locks}


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _padded_row(stamp: str, status: str, slug: str, metadata: dict, size: int,
                pad_key: str = "note") -> str:
    """One six-column registry row of exactly ``size`` bytes (UTF-8, newline included).

    The free-text pad is ``note=`` unless the row carries a real ``note`` (``pad_key``)."""
    head = _attempt_row(0, slug, metadata, status=status).split("\t", 1)[1]
    row = f"{stamp}\t{head}"
    pad = size - len(row.encode("utf-8")) - len("," + pad_key + "=")
    if pad < 0:
        raise ValueError(f"registry row exceeds {size} bytes")
    return row[:-1] + "," + pad_key + "=" + "x" * pad + "\n"


def _write_registry(jobs: Path, *, rows: int, depth1: int, row_bytes: int,
                    target_attempts: list[str], ledgers: int, now: float,
                    depth1_open_every: int = DEPTH1_OPEN_EVERY,
                    min_bytes: int = MIN_JOBS_BYTES, open_rows: list[str] | None = None,
                    history_style: str = "filler", shape: "_HistoryShape | None" = None) -> dict:
    """The jobs registry: target rows, unrelated depth-1 owner rows, then filler.

    The depth-1 rows carry every ``REGISTRY_DEPTH1_START`` stamp of
    ``hooks/dispatch-owner-rewake.py`` and an allowed worker type, so the
    asyncRewake scan resolves each one through ``seat_handover.owns``. Each is
    bound to an unrelated session that has a handover edge in the lineage
    (``perf-from-*`` -> ``perf-holder-*``), as live rows are. Every unrelated
    row has the same byte size, so the registry's size does not depend on how
    many depth-1 rows it holds.
    """
    written = 0
    depth1_open = 0
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
        depth1 = max(0, min(depth1, rows - written))
        holders = max(1, ledgers - 1)
        for index in range(depth1):
            status = "open" if depth1_open_every > 0 and index % depth1_open_every == 0 else "done"
            depth1_open += status == "open"
            parent = f"perf-from-{index % holders + 1:05d}"
            metadata = {
                "capability": "autopilot-code",
                "attempt_schema_version": "2",
                "dispatch_depth": "1",
                "transport": "headless",
                "harness": "claude",
                "parent_sid": parent,
                "parent_harness": "claude",
                "worker_type": "frame" if index % 3 == 2 else "owner",
                "parent_completion_delivery": "claude-parent-runtime",
                "launch_claimed": "1",
                "launch_started": "1",
                "owner_route_id": f"rt-perf-owner-{index:05d}",
                "route_id": f"rt-perf-owner-{index:05d}",
                "route_hash": "sha256:perf-owner",
                "route_node": "execute",
                "attempt_id": f"att-perf-owner-{index:05d}",
            }
            stamp = _iso(now - (300 + index if status == "open" else 86_400 + index))
            handle.write(_padded_row(stamp, status, f"perf-owner-{index:05d}", metadata, row_bytes))
            written += 1
        # Open-route attempt rows (e2e fixtures only; the default has none). A shaped history
        # keeps them where the live registry has its open rows: in the later part of the file
        # (live 2026-11-13: segments 8-14 of 15), so one in-place rewrite of an open row
        # cannot leave a long unchanged prefix behind.
        pending_open = list(open_rows or ())
        open_at = None
        if shape is not None:
            open_at = max(0, (max(0, rows - written - len(pending_open)) * 7) // 10)
        else:
            for raw in pending_open:
                handle.write(raw)
                written += 1
            pending_open = []
        history_total = max(0, rows - written - len(pending_open))
        for index in range(history_total):
            if history_style == "history":
                if pending_open and index == open_at:
                    for raw in pending_open:
                        handle.write(raw)
                        written += 1
                    pending_open = []
                handle.write(_history_row(index, row_bytes, now, history_total, shape))
                written += 1
                continue
            metadata = {
                "attempt_id": f"att-perf-filler-{index:05d}",
                "parent_sid": f"perf-filler-{index % 97:03d}",
                "dispatch_depth": "1",
                "worker_type": "frame",
                "route_id": "rt-filler",
                "route_hash": "sha256:filler",
                "route_node": "execute",
            }
            handle.write(_padded_row(str(1000 + index), "running", "perf-filler",
                                     metadata, row_bytes))
            written += 1
        for raw in pending_open:  # a history too short to reach the placement row
            handle.write(raw)
            written += 1
        # Top up to the byte floor when the row target alone is smaller.
        while handle.tell() < min_bytes:
            metadata = {
                "attempt_id": f"att-perf-pad-{written:05d}",
                "parent_sid": "perf-pad",
                "route_id": "rt-pad",
                "route_hash": "sha256:pad",
                "route_node": "execute",
            }
            handle.write(_padded_row(str(2000 + written), "running", "perf-pad",
                                     metadata, row_bytes))
            written += 1
    os.chmod(jobs, 0o600)
    return {"jobs_rows": written, "jobs_bytes": jobs.stat().st_size,
            "depth1_rows": depth1, "depth1_open": depth1_open}


HISTORY_ROWS_PER_ATTEMPT = 1  # live 2026-11-13: 7,684 rows, 7,684 attempts (one row each)
HISTORY_EXTRA_KEYS = 110      # live attempt rows carry ~130 key=value fields

# Live 2026-11-13 shares of the history rows (counted read-only on the operating registry):
# 6,775 of 7,684 rows carry a route triple over 1,308 distinct routes, 1,504 are owner
# attempts, 133 of them end BLOCKED with no successor (their routes stay open), 672 are
# dead terminal owners. They are fractions of the history, so a doubled history doubles each.
HISTORY_ROUTED_IN_25 = 22        # 88% of the rows carry a route triple
HISTORY_ROUTE_ROWS = (17, 88)    # distinct routes = 17 per 88 routed rows (17% of the rows)
HISTORY_OWNER_SHARE = 0.223      # of the routed rows (19.6% of all rows)
HISTORY_DEAD_OWNER_SHARE = 0.44  # of the owner rows that are not BLOCKED (8.7% of all rows)
HISTORY_BLOCKED_ROUTE_MOD = 10   # every 10th route ends in a BLOCKED owner (1.7% of all rows)
HISTORY_OPEN_ROUTE_MOD = 40      # and one in 40 other routes has not closed
HISTORY_ANSWERED_BLOCKED = 2     # BLOCKED owners that hold a kept, unconsumed answer


def _unit(index: int, salt: int = 0) -> float:
    """A fixed pseudo-random value in [0, 1) per (row, salt): the same fixture every build."""
    digest = hashlib.blake2b(f"{salt}:{index}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big") / 4294967296


def _history_metadata(attempt: str, index: int) -> dict:
    metadata = {
        "capability": "autopilot-code", "attempt_schema_version": "2",
        "dispatch_depth": "2", "transport": "headless", "harness": "claude",
        "parent_sid": f"perf-hist-{index % 211:04d}", "parent_harness": "claude",
        "worker_type": "stage", "route_id": f"rt-hist-{index // 20:05d}",
        "route_hash": "sha256:hist", "route_node": "execute", "attempt_id": attempt,
    }
    for key in range(HISTORY_EXTRA_KEYS):
        metadata[f"k{key:03d}"] = f"v{(index + key) % 97}"
    return metadata


def _routed_ordinal(index: int) -> int:
    """How many rows before ``index`` carry a route triple."""
    return (index // 25) * HISTORY_ROUTED_IN_25 + min(index % 25, HISTORY_ROUTED_IN_25)


def _route_number(ordinal: int) -> int:
    return ordinal * HISTORY_ROUTE_ROWS[0] // HISTORY_ROUTE_ROWS[1]


def _history_status(shape_kind: str | None, attempt_index: int, phase: int) -> str:
    return ("open", "open", "done")[phase + 3 - HISTORY_ROWS_PER_ATTEMPT]


_COMPILE_REAL_ROUTE = r"""
import importlib.util, json, os, sys, tempfile
from pathlib import Path
repo = Path(sys.argv[1])
tmp = tempfile.mkdtemp(prefix="route-compile-")
(Path(tmp) / "core").mkdir()
(Path(tmp) / "core" / "CORE.md").write_text("fixture\n")
os.environ.update(AGENT_HOME=tmp, XDG_STATE_HOME=tmp + "/state", AGENT_DISPATCH_JOBS=tmp + "/state/jobs.log")
Path(tmp, "state").mkdir()
Path(tmp, "state", "jobs.log").write_text("")
sys.path.insert(0, str(repo / "utilities"))
spec = importlib.util.spec_from_file_location("route", repo / "utilities" / "capability-route.py")
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)
gate = {"spec_read": {"satisfied": True, "source": "canonical-prd-sha256"}, "drift_verdict": "within-spec",
        "workflow_mode": "tracked", "artifact_guard": {"satisfied": True, "source": "conductor-prechecked"}}
nested = {"parent_harness": "codex", "parent_transport": "headless",
          "parent_sandbox": route.WRAPPER_PARENT_SANDBOXES["codex"][0], "child_harness": "codex",
          "launch_authority": "conductor", "status": "supported", "probe_source": "fixture-probe",
          "probe_time": "2026-07-16T00:00:00Z", "failure_class": "", "checked_worktree": str(route.ROOT.resolve()),
          "failure_scope": "none", "codex_command": "ok", "retry_on_isolated_worktree": 0}
evidence = {"tuples": [nested], "native_subagent": [{
    "harness": "codex", "transport": "headless", "execution_surface": "codex-native-subagent",
    "registered_worker": False, "status": "supported", "check_source": "fixture-native-check"}]}
compiled = route.compile_route(
    capability="autopilot-code", capability_mode="dev", requested_intensity="standard", cwd=route.ROOT,
    artifact_root=route.ROOT, predicates=[], signals=[], transport="headless", inline_reason=None,
    tracking="tracked", tracked_gate_evidence=gate, dispatch_evidence=evidence)
import capability_route_verify
capability_route_verify.verify_route(dict(compiled), str(route.ROOT))
json.dump(compiled, sys.stdout, sort_keys=True)
"""


def compile_real_route(repo: Path | None = None) -> tuple[dict | None, str]:
    """One route the compiler of this tree really emits, and ``verify_route`` accepts.

    Run in a child with its own AGENT_HOME so nothing of the caller's environment leaks in.
    Returns ``(route, "")`` or ``(None, reason)``: a route that cannot verify makes the
    owner-input decision stop earlier than the live one, and the caller reports that.
    """
    import subprocess
    repo = Path(repo) if repo is not None else Path(__file__).resolve().parents[1]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": tempfile.gettempdir(), "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        done = subprocess.run([sys.executable, "-I", "-c", _COMPILE_REAL_ROUTE, str(repo)],
                              capture_output=True, text=True, env=env, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"compile-failed:{type(exc).__name__}"
    if done.returncode != 0:
        return None, "compile-failed:" + (done.stderr.strip().splitlines() or ["?"])[-1][:160]
    try:
        return json.loads(done.stdout), ""
    except ValueError:
        return None, "compile-output-unreadable"


class _HistoryShape:
    """Route, owner, BLOCKED-owner and dead-owner shape of the e2e history (live ratios).

    Every route a history row names gets a real sealed record (the reader recomputes hash
    and id), a workflow journal and, for a closed route, a ``.outcome.json`` sidecar. A BLOCKED
    owner's route is a clone of one really compiled route (``slug`` changed, hash and id
    recomputed) so the owner-input decision runs ``verify_route`` to its end. Files are written
    on the first row that names the route.
    """

    def __init__(self, base: Path, dispatch_root: Path, *, now: float, real_route: dict | None):
        self.routes_dir = _private_dir(base / "routes" / "hist")
        self.workflow_root = _private_dir(dispatch_root / "workflow")
        self.now = now
        self.real_route = real_route
        self.written: dict[int, dict] = {}
        self.counts = {"rows": 0, "routed": 0, "owner": 0, "blocked": 0, "dead_owner": 0,
                       "routes": 0, "routes_open": 0, "routes_real": 0}
        self.blocked: list[dict] = []   # [{attempt_id, route_id, route_file, jobs_attempt}]

    def route_record(self, number: int, *, blocked: bool) -> dict:
        slug = f"hist-route-{number:05d}"
        if blocked and self.real_route is not None:
            record = dict(self.real_route)
            record["slug"], record["slug_truncated"] = slug, False
            identity = _route_identity()
            digest = identity.route_hash(record)
            record["route_hash"], record["route_id"] = digest, identity.route_id_from_hash(digest)
            self.counts["routes_real"] += 1
            return record
        return e2e_route_record(number, slug=slug)

    def ensure_route(self, number: int) -> dict:
        if number in self.written:
            return self.written[number]
        blocked = number % HISTORY_BLOCKED_ROUTE_MOD == 3
        closed = not blocked and number % HISTORY_OPEN_ROUTE_MOD != 7
        record = self.route_record(number, blocked=blocked)
        path = self.routes_dir / f"{record['route_id']}.json"
        info = {"route_id": record["route_id"], "route_hash": record["route_hash"],
                "route_file": str(path), "blocked": blocked, "closed": closed}
        self.written[number] = info
        self.counts["routes"] += 1
        _write(path, json.dumps(record, sort_keys=True) + "\n")
        at = _iso(self.now - 90_000 - number)
        entry = {"route_id": record["route_id"], "route_hash": record["route_hash"],
                 "workflow_state": "COMPLETE" if closed else "RUNNING", "at": at}
        _write(self.workflow_root / record["route_id"] / "journal.jsonl",
               json.dumps(entry, sort_keys=True) + "\n")
        if closed:
            _write(path.with_name(path.stem + ".outcome.json"), json.dumps({
                "terminal_gate_proven": True, "closed_at": at.replace("Z", "+00:00"),
                "route_id": record["route_id"], "route_hash": record["route_hash"]}) + "\n")
        else:
            self.counts["routes_open"] += 1
        return info

    def shape(self, index: int, attempt: str, status: str, metadata: dict, age: float,
              total: int | None) -> tuple[str, dict, str]:
        """``(status, metadata, pad_key)`` of one history row."""
        self.counts["rows"] += 1
        recent = total is not None and (total - 1 - index) < HISTORY_RECENT_24H_ROWS
        for key in ("route_id", "route_hash", "route_node"):
            metadata.pop(key, None)
        if recent or index % 25 >= HISTORY_ROUTED_IN_25:
            return status, metadata, "note"
        ordinal = _routed_ordinal(index)
        number = _route_number(ordinal)
        last_in_route = _route_number(ordinal + 1) != number
        route = self.ensure_route(number)
        self.counts["routed"] += 1
        metadata.update(route_id=route["route_id"], route_hash=route["route_hash"],
                        route_file=route["route_file"], route_node="execute")
        blocked = route["blocked"] and last_in_route
        owner = blocked or _unit(index) < HISTORY_OWNER_SHARE
        if not owner:
            return status, metadata, "note"
        self.counts["owner"] += 1
        metadata.update(worker_type="owner", dispatch_depth="1", launch_claimed="1",
                        launch_started="1", owner_route_id=route["route_id"],
                        owner_route_hash=route["route_hash"], owner_route_file=route["route_file"])
        if blocked:
            self.counts["blocked"] += 1
            metadata["note"] = "dead-worker-blocked"
            self.blocked.append({"attempt_id": attempt, "route_id": route["route_id"],
                                 "route_file": route["route_file"], "index": index})
            return "done", metadata, "pad"
        if _unit(index, 7) < HISTORY_DEAD_OWNER_SHARE:
            self.counts["dead_owner"] += 1
            metadata["note"] = "dead-worker-exit"
            metadata["failure_class"] = "runtime"
            return "done", metadata, "pad"
        return status, metadata, "note"


def _shape_finish(self, jobs: Path, real_route_reason: str) -> dict:
    """Kept answers for the first BLOCKED owners, and the counts the report records."""
    answered = []
    for item in self.blocked[:HISTORY_ANSWERED_BLOCKED]:
        path = Path(jobs).parent / "supervisor-state" / f"{item['attempt_id']}.input.json"
        _write(path, json.dumps({
            "schema_version": 1, "attempt_id": item["attempt_id"],
            "requests": [{"id": "req-fixture-1", "digest": "sha256:fixture", "state": "retained",
                          "text": "kept answer"}]}, sort_keys=True) + "\n")
        answered.append(item["attempt_id"])
    return {**self.counts, "answered_blocked": answered,
            "blocked_attempts": [item["attempt_id"] for item in self.blocked],
            "real_route_verified": self.real_route is not None, "real_route_reason": real_route_reason}


_HistoryShape.finish = _shape_finish


def _history_row(index: int, row_bytes: int, now: float, total: int | None = None,
                 shape: "_HistoryShape | None" = None) -> str:
    """One finished-attempt row: one row per attempt, rich metadata.

    ``total`` is the history row count: when it cuts the last attempt short, that attempt's
    rows are all ``done`` so history never holds an attempt that looks still open.
    With ``shape`` the row also carries the live route/owner/BLOCKED/dead-owner share.
    """
    attempt_index, phase = divmod(index, HISTORY_ROWS_PER_ATTEMPT)
    attempt = f"att-hist-{attempt_index:06d}"
    status = _history_status(None, attempt_index, phase)
    if total is not None and attempt_index == (total - 1) // HISTORY_ROWS_PER_ATTEMPT:
        status = "done"
    age = _history_age_s(index, total)
    metadata = _history_metadata(attempt, attempt_index)
    pad_key = "note"
    if shape is not None:
        status, metadata, pad_key = shape.shape(index, attempt, status, metadata, age, total)
    elif age < 86_400:
        # A first-screen row bound to a route needs that route's record (``required-details``
        # stays partial without it and Fleet never leaves loading), so the recently finished
        # attempts are standalone dispatches here: they exercise the afterglow and stall
        # windows, not the route-detail path (which the open routes cover).
        for key in ("route_id", "route_hash", "route_node"):
            metadata.pop(key, None)
    return _padded_row(_iso(now - age), status, f"hist-{attempt_index:06d}", metadata, row_bytes, pad_key)


# Live 2026-11-12: of ~7,740 registry rows, ~61 are younger than 6 h and ~161 younger than 24 h
# (the open attempts' rows included). They are what Fleet's 15-minute afterglow and
# 361-minute stall windows act on. The history part takes 50 / 150; the e2e fixture's ~12
# open route rows (a few minutes old) bring the whole registry to the live counts.
HISTORY_RECENT_6H_ROWS = 50
HISTORY_RECENT_24H_ROWS = 150


def _history_age_s(index: int, total: int | None) -> float:
    """Seconds before ``now`` of history row ``index`` (the older rows keep one spread, > 24 h)."""
    if total is not None:
        from_end = total - 1 - index
        if from_end < HISTORY_RECENT_6H_ROWS:
            return (from_end + 0.5) / HISTORY_RECENT_6H_ROWS * 6 * 3600
        if from_end < HISTORY_RECENT_24H_ROWS:
            span = HISTORY_RECENT_24H_ROWS - HISTORY_RECENT_6H_ROWS
            return 6 * 3600 + (from_end - HISTORY_RECENT_6H_ROWS + 0.5) / span * 18 * 3600
    return 86_400 + (900_000 - index) * 5


def e2e_session_id(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def _e2e_route_body(index: int, slug: str | None = None) -> dict:
    """The sealed part of an e2e route record: what the reader hashes.

    Node status is not in a sealed record (the registry rows carry it), so a stage
    move never changes the hash or the id. Shape: schema 2, contract 3, the keys
    ``tools/fleet/route.py::_load_uncached`` checks.
    """
    nodes = [{"id": node_id, "kind": "pipeline-stage", "unit": f"{node_id}/e2e",
              "dispatch_depth": 2} for node_id in ("plan", "execute", "test", "report")]
    return {
        "schema_version": 2, "capability": "autopilot-code", "capability_mode": "dev",
        "requested_intensity": "standard", "effective_intensity": "standard",
        "execution_topology": "staged", "slug": slug or f"e2e-route-{index:05d}",
        "dispatch_contract_version": 3, "owner_dispatch_depth": 1, "max_dispatch_depth": 2,
        "selection": {"shape": "staged", "route_origin": "compose"}, "nodes": nodes,
    }


def _route_identity():
    """``utilities/route_identity.py``: the one hash/id derivation the reader also uses."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import route_identity
    return route_identity


def e2e_route_hash(index: int, slug: str | None = None) -> str:
    """The real ``route_hash`` of the index-th e2e route (the reader recomputes it)."""
    return _route_identity().route_hash(_e2e_route_body(index, slug))


def e2e_route_id(index: int) -> str:
    """``rt-`` + 16 hex of the hash: the reader rejects any other id for a record."""
    return _route_identity().route_id_from_hash(e2e_route_hash(index))


def e2e_route_record(index: int, *, slug: str | None = None, stage: str = "execute",
                     state: str = "running", route_hash: str | None = None) -> dict:
    """A route record the Fleet reader accepts (hash recomputed, id derived, schema 2).

    ``stage``/``state`` only move an unhashed ``_fleet_`` key, so rewriting the file
    for a stage move changes its bytes, not its identity.
    """
    body = _e2e_route_body(index, slug)
    digest = route_hash or e2e_route_hash(index, slug)
    return {**body, "route_hash": digest, "route_id": _route_identity().route_id_from_hash(digest),
            "owner_attempt_id": f"att-e2e-owner-{index:05d}",
            "_fleet_e2e_state": f"{stage}:{state}"}


def e2e_open_row(index: int, *, jobs_log: Path, route_file: Path, session_id: str,
                 pid: int | None, pid_start: str | None, log_file: Path | None,
                 row_bytes: int, now: float, stage: str = "execute") -> str:
    metadata = {
        "capability": "autopilot-code", "capability_mode": "dev",
        "attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
        "harness": "claude", "parent_sid": session_id, "parent_harness": "claude",
        "worker_type": "owner", "route_file": str(route_file),
        "route_id": e2e_route_id(index), "route_hash": e2e_route_hash(index),
        "route_node": stage, "attempt_id": f"att-e2e-owner-{index:05d}",
        "launch_claimed": "1", "launch_started": "1",
        "parent_completion_delivery": "claude-parent-runtime",
    }
    if pid:
        metadata["pid"] = str(pid)
        metadata["pid_start"] = str(pid_start or "")
    if log_file is not None:
        metadata["log_file"] = str(log_file)
    for key in range(HISTORY_EXTRA_KEYS):
        metadata[f"k{key:03d}"] = f"v{(index + key) % 97}"
    return _padded_row(_iso(now - 300 - index), "open", f"e2e-owner-{index:05d}",
                       metadata, row_bytes)


def _proc_start_ticks(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().rsplit(b")", 1)[1].split()[19].decode()
    except (OSError, IndexError):
        return None


def _write_e2e_state(fixture: dict, *, open_routes: int, live_sessions: int,
                     worker_streams: bool, session_pids: list, jobs: Path, home: Path,
                     work: Path, row_bytes: int, now: float) -> list[str]:
    """Sessions, route records, ledger lines and worker streams for the e2e fixture.

    Returns the open-route registry rows. The route-chain ledger lines are
    written by the real ``route_chain.append`` so the Fleet reader meets what it
    meets in production. Everything here lives under the fixture ``base``.
    """
    import sys
    root = str(Path(__file__).resolve().parents[1])
    for entry in (root, str(Path(root) / "tools")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    from fleet import route_chain
    base = Path(fixture["base"])
    routes_dir = _private_dir(base / "routes")
    # The reader (``attempt_view.owned_attempt_log_path``) admits a worker stream only
    # inside ``<registry dir>/logs`` and only as ``<name>.<attempt_id>.claude.jsonl``.
    streams_dir = _private_dir(Path(jobs).parent / "logs")
    sessions_dir = _private_dir(home / ".claude" / "sessions")
    cwd = _private_dir(work / "e2e-project")
    sessions, routes, streams, rows = [], [], [], []
    for index in range(live_sessions):
        pid, start = (session_pids[index] if index < len(session_pids) else (None, None))
        sid = e2e_session_id(index)
        record = {"pid": pid or 100000 + index, "sessionId": sid, "cwd": str(cwd),
                  "startedAt": int((now - 3600) * 1000), "procStart": start,
                  "kind": "interactive", "entrypoint": "cli", "status": "idle",
                  "updatedAt": int(now * 1000), "version": "e2e"}
        _write(sessions_dir / f"{record['pid']}.json", json.dumps(record) + "\n")
        sessions.append({"index": index, "session_id": sid, "pid": record["pid"],
                         "pid_start": start, "cwd": str(cwd), "harness": "claude"})
    for index in range(open_routes):
        owner = sessions[index % len(sessions)] if sessions else None
        sid = owner["session_id"] if owner else e2e_session_id(index)
        route_file = routes_dir / f"{e2e_route_id(index)}.json"
        record = e2e_route_record(index)
        _write(route_file, json.dumps(record, sort_keys=True) + "\n")
        log_file = None
        if worker_streams:
            log_file = streams_dir / f"e2e.att-e2e-owner-{index:05d}.claude.jsonl"
            _write(log_file, json.dumps({
                "type": "system", "subtype": "init", "session_id": sid}) + "\n")
            streams.append(str(log_file))
        with applied_env(fixture):
            line = route_chain.build_line(record, event="start", harness="claude",
                                          session_id=sid, route_file=route_file, now=now - 300 - index)
            route_chain.append("claude", sid, line)
        owner_pid, owner_start = (session_pids[live_sessions + index]
                                  if live_sessions + index < len(session_pids) else (None, None))
        rows.append(e2e_open_row(
            index, jobs_log=jobs, route_file=route_file, session_id=sid,
            pid=owner_pid, pid_start=owner_start, log_file=log_file,
            row_bytes=row_bytes, now=now))
        routes.append({"index": index, "route_id": e2e_route_id(index),
                       "route_file": str(route_file), "session_id": sid,
                       "attempt_id": f"att-e2e-owner-{index:05d}",
                       "owner_pid": owner_pid, "owner_pid_start": owner_start,
                       "log_file": str(log_file) if log_file else None})
    # ``sessions`` already names the ledger directory in the base fixture.
    fixture["e2e_sessions"] = sessions
    fixture["e2e_routes"] = routes
    fixture["e2e_streams"] = streams
    return rows


def _write_transcript(path: Path, rows: int) -> None:
    """The target's own transcript: fixed size, it does not grow with ``scale``."""
    _private_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        for index in range(rows):
            role = "user" if index % 2 == 0 else "assistant"
            handle.write(json.dumps({
                "type": role, "sessionId": TARGET_SESSION, "uuid": f"perf-turn-{index:05d}",
                "message": {"role": role, "content": f"perf transcript turn {index} " + "y" * 400},
            }, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


STALL_NOTICES = 6  # live 2026-11-13 cold profile: six stall notices checked per collection pass


def _write_stall_shape(head: dict, jobs: Path, *, count: int = STALL_NOTICES) -> dict:
    """A watchdog record and a stall delivery for the first ``count`` open route attempts.

    Written by the real writers (``update_attempt_stall_observation`` then ``materialize``) with
    the execution probe answering "absent" for that moment only, as the supervision tests do, so
    every later Fleet pass finds a stall delivery on those attempts and checks ``notice_is_current``
    for it. Returns what was written, or ``{"written": 0, "reason": ...}`` when it could not be.
    """
    from unittest import mock
    root = str(Path(__file__).resolve().parents[1])
    for entry in (root, str(Path(root) / "utilities")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    attempts = [f"att-e2e-owner-{index:05d}" for index in range(count)]
    written, reason = [], ""
    saved = {key: os.environ.get(key) for key in head["env"]}
    os.environ.update(head["env"])
    try:
        import dispatch_contract
        import dispatch_supervision as supervision
        import peer_obligations
        for attempt in attempts:
            rows = supervision._rows(Path(jobs), {attempt})
            row = rows.get(attempt)
            if not row:
                reason = "row-missing:" + attempt
                continue
            with mock.patch.object(dispatch_contract, "attempt_execution_observation",
                                   return_value=dispatch_contract.AttemptExecutionObservation(
                                       "absent", "carrier-only")), \
                 mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
                 mock.patch.object(peer_obligations, "ensure_normal_runner"):
                observation = supervision.update_attempt_stall_observation(
                    Path(jobs), attempt, now=time.time(), row=row, worktree=str(Path(head["base"]) / "work"))
                if not observation or observation.get("state") != "stalled":
                    reason = f"not-stalled:{attempt}:{(observation or {}).get('state')}"
                    continue
                records = supervision.materialize(Path(jobs), {attempt}, reason="attempt-stalled")
            if records:
                written.append(attempt)
            else:
                reason = "no-delivery:" + attempt
    except Exception as exc:  # noqa: BLE001 -- the fixture reports it instead of hiding it
        reason = f"{type(exc).__name__}:{exc}"[:200]
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return {"written": len(written), "attempts": written, "reason": reason}


def build(base: str | Path | None = None, *, scale: int = 1, jobs_rows: int = DEFAULT_JOBS_ROWS,
          record_size: int = JOBS_ROW_BYTES, prime: bool = True,
          prime_via: str = "hook", depth1_rows: int | None = None,
          open_routes: int = 0, live_sessions: int = 0, worker_streams: bool = False,
          history_rows: int | None = None, session_pids: list | None = None,
          depth1_open_every: int = DEPTH1_OPEN_EVERY,
          min_jobs_bytes: int = MIN_JOBS_BYTES) -> dict:
    """Build one fixed fixture tree and return its paths, env and counts.

    The keyword-only e2e options default to the original behaviour: with none
    of them set the tree is exactly what it was. ``history_rows`` makes the
    unrelated registry rows finished-attempt history (about three rows per
    attempt, rich metadata) and fixes the registry row count at that number;
    ``open_routes``/``live_sessions``/``worker_streams`` add open route
    attempts, native session files and worker stream logs for end-to-end
    latency measurement.
    """
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

    # Peer waiting history: the receiver's full scan opens every retained file;
    # a recipient index lets an exact-recipient read skip it.
    now = time.time()
    peer = _write_peer_rows(peer_root, DEFAULT_PEER_ROWS * scale, now=now)
    peer_rows = peer["peer_rows"]

    # jobs.log: exactly ``jobs_rows`` rows and a fixed byte size at every
    # scale. Only the share of unrelated depth-1 owner rows grows with
    # ``scale`` (they replace filler rows), so a scan's per-row cost shows up
    # as growth while its read volume does not change.
    home_work = _private_dir(base / "work")
    open_rows: list[str] = []
    e2e_head: dict = {}
    if open_routes or live_sessions or worker_streams:
        e2e_head = {"base": str(base), "env": {
            "HOME": str(home), "XDG_STATE_HOME": str(xdg),
            "HARNESS_STATE_ROOT": str(state), "AGENT_DISPATCH_JOBS": str(jobs),
            "AGENT_PEER_LEDGER_ROOT": str(peer_root)}}
        open_rows = _write_e2e_state(
            e2e_head, open_routes=open_routes, live_sessions=live_sessions,
            worker_streams=worker_streams, session_pids=list(session_pids or []),
            jobs=jobs, home=home, work=home_work, row_bytes=record_size, now=now)
    history_shape = None
    real_route_reason = ""
    if history_rows is not None:
        real_route, real_route_reason = compile_real_route()
        history_shape = _HistoryShape(base, dispatch_root, now=now, real_route=real_route)
    registry = _write_registry(
        jobs, rows=history_rows if history_rows is not None else jobs_rows,
        depth1=DEFAULT_DEPTH1_ROWS * scale if depth1_rows is None else depth1_rows,
        row_bytes=record_size, target_attempts=target_attempts, ledgers=ledgers, now=now,
        depth1_open_every=depth1_open_every, min_bytes=min_jobs_bytes,
        open_rows=open_rows,
        history_style="history" if history_rows is not None else "filler", shape=history_shape)
    shape_report = None
    if history_shape is not None:
        shape_report = history_shape.finish(jobs, real_route_reason)
        if e2e_head.get("env") and open_routes:
            shape_report["stall"] = _write_stall_shape(e2e_head, jobs, count=min(STALL_NOTICES, open_routes))

    transcript = home / ".claude" / "projects" / "-perf-work" / f"{TARGET_SESSION}.jsonl"
    _write_transcript(transcript, TRANSCRIPT_ROWS)
    work = _private_dir(base / "work")

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
        "transcript": str(transcript),
        "work": str(work),
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
            "snapshots": snapshots,
            "steward_markers": markers + 1,
            "transcript_rows": TRANSCRIPT_ROWS,
            "terminal_records": len(terminal_delivery_ids),
            **peer,
            **registry,
            **deliveries,
        },
        "terminal_delivery_ids": terminal_delivery_ids,
    }
    for key in ("e2e_sessions", "e2e_routes", "e2e_streams"):
        if key in e2e_head:
            fixture[key] = e2e_head[key]
    if shape_report is not None:
        fixture["history_shape"] = shape_report
    if prime:
        prime_inbox(fixture, via_runner=prime_via == "runner")
    # The measurement tool reads this to replay the fixture's exact environment.
    _write(base / "fixture.json", json.dumps(fixture, indent=2, sort_keys=True) + "\n")
    return fixture


def _load_by_path(name: str, path: Path):
    """Load a module whose filename is hyphenated (once per process)."""
    import sys
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through it
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _peer_steward_module():
    """The runner module, loaded by path because its filename is hyphenated."""
    return _load_by_path("_hook_perf_peer_steward", Path(__file__).with_name("peer-steward.py"))


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
            # The runner publishes every recipient's index; the target is
            # reached through both its Claude and its Codex hooks.
            for harness in ("claude", "codex"):
                module._publish_recipient_index(
                    pending_dir, {"harness": harness,
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

    pending_dir = Path(fixture["peer_root"]) / "peer-messages" / "pending"
    pending_files = list(pending_dir.glob("*.json"))
    if len(pending_files) < _MINIMUMS["peer_rows"]:
        raise AssertionError("peer waiting history is smaller than the fixed size")
    counts["peer_locks_seen"] = sum(1 for name in os.listdir(pending_dir)
                                    if name.startswith(".lock-"))
    if counts["peer_locks_seen"] < _MINIMUMS["peer_locks"]:
        raise AssertionError("peer pending holds fewer lock files than the fixed size")
    # Every open row must survive the real verifier, or a full scan would
    # quarantine it and the fixture would change under the measurement.
    peer_message = _load_by_path("_hook_perf_peer_message_check",
                                 Path(__file__).with_name("peer-message.py"))
    open_rows = 0
    with applied_env(fixture):
        roots, _source = peer_message.steward_marker_roots()
        for path in pending_files:
            if json.loads(path.read_text(encoding="utf-8")).get("state") == "received":
                continue
            row = peer_message._read_pending(path.stem, roots=roots)
            if not row or row.get("state") not in {"queued", "pending"}:
                raise AssertionError(f"open peer row did not verify: {path.name}")
            if row["to"].get("session_id") == fixture["target_session"]:
                raise AssertionError(f"open peer row addressed to the target: {path.name}")
            open_rows += 1
    counts["peer_open_rows_seen"] = open_rows
    if open_rows < _MINIMUMS["peer_open_rows"]:
        raise AssertionError("peer pending holds fewer verifiable open rows than the fixed size")

    # The asyncRewake scan's rows, judged by the hook's own constants: enough
    # unrelated claimed-and-started depth-1 rows (open and done), and none
    # bound to the target, which therefore has no owner to wait for.
    hooks = Path(__file__).resolve().parents[1] / "hooks"
    rewake = _load_by_path("_hook_perf_owner_rewake", hooks / "dispatch-owner-rewake.py")
    latest = {}
    with jobs.open("r", encoding="utf-8") as handle:
        for line in handle:
            columns = line.rstrip("\n").split("\t")
            metadata = rewake._registry_metadata(columns[5])
            latest[metadata.get("attempt_id", "")] = (columns[1], metadata)
    depth1 = {"open": 0, "done": 0}
    for status, metadata in latest.values():
        if (status not in depth1
                or any(metadata.get(key) != value
                       for key, value in rewake.REGISTRY_DEPTH1_START.items())
                or not rewake._worker_type_is_depth1(metadata.get("worker_type"))):
            continue
        if metadata.get("parent_sid") in {fixture["target_session"], fixture["predecessor"]}:
            raise AssertionError("the target must own no depth-1 owner row")
        depth1[status] += 1
    counts["depth1_rows_seen"] = depth1["open"] + depth1["done"]
    counts["depth1_open_seen"] = depth1["open"]
    if (counts["depth1_rows_seen"] < _MINIMUMS["depth1_rows"]
            or depth1["open"] < _MINIMUMS["depth1_open"] or depth1["done"] < 1):
        raise AssertionError(f"registry depth-1 owner rows below the fixed size: {depth1}")

    with Path(fixture["transcript"]).open("rb") as handle:
        counts["transcript_rows_seen"] = sum(1 for _line in handle)
    if counts["transcript_rows_seen"] < _MINIMUMS["transcript_rows"]:
        raise AssertionError("target transcript is smaller than the fixed size")

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
    parser.add_argument("--depth1-rows", type=int, default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    fixture = build(args.base or None, scale=args.scale, jobs_rows=args.jobs_rows,
                    depth1_rows=args.depth1_rows)
    if args.check:
        fixture["counts"] = self_check(fixture)
    print(json.dumps(fixture, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
