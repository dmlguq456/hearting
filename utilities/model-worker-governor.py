#!/usr/bin/env python3
"""Shared admission control for repository-launched model workers."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

from replica_batch_contract import ReplicaBatchContractError, verify_manifest


CLASS_LIMITS = {"dispatch": 5, "distill": 1, "title": 4, "loop": 2}
START_WINDOW_SECONDS = 600
DEFAULT_TOTAL_LIMIT = 5
DEFAULT_START_BUDGET = 20
RESERVATION_ENV = "AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN"
TOKEN_BYTES = 16
CLAIM_RECEIPT_SECONDS = START_WINDOW_SECONDS
BATCH_RESERVATION_KEYS = (
    "reservation_kind",
    "batch_declared_size",
    "batch_admission_count",
    "batch_group",
    "batch_route_id",
    "batch_parent_attempt_id",
    "batch_attempt_id",
    "batch_route_node",
    "batch_harness",
    "batch_fallback_hop",
    "batch_fallback_ordinal",
    "batch_independence",
    "batch_assignment_sha256",
    "batch_model_profile",
    "batch_perspective",
    "batch_parallel_leg_index",
    "batch_peer_count",
    "batch_peer_set",
    "batch_peer_set_sha256",
    # Schema-v1 read compatibility for an exact two-way recovery.
    "batch_peer_attempt_id",
    "batch_peer_state",
    "batch_peer_proof",
    "batch_peer_proof_sha256",
    "batch_manifest",
    "batch_manifest_sha256",
    "batch_leg_sha256",
)
_BATCH_ISSUER_SEAL = object()


class _BatchIssuerCapability:
    """Process-local, one-shot proof of the canonical batch conductor."""

    __slots__ = ("pid", "starttime", "_seal", "_used")

    def __init__(self, pid: int, starttime: str, seal: object) -> None:
        self.pid = pid
        self.starttime = starttime
        self._seal = seal
        self._used = False


def default_root() -> Path:
    """Return a path writable by both the main checkout and linked workers."""
    explicit = os.environ.get("AGENT_MODEL_GOVERNOR_ROOT")
    if explicit:
        return Path(explicit)
    artifact_root = os.environ.get("AGENT_ARTIFACT_ROOT")
    if artifact_root:
        return Path(artifact_root) / ".runtime" / "model-worker-governor"
    resolver = Path(__file__).resolve().with_name("artifact-root.sh")
    if resolver.is_file():
        resolved = subprocess.run(
            [str(resolver), str(Path.cwd())],
            check=False,
            capture_output=True,
            text=True,
        )
        if resolved.returncode == 0 and resolved.stdout.strip():
            return Path(resolved.stdout.strip()) / ".runtime" / "model-worker-governor"
    return Path.home() / ".agent-worker-governor"


def process_observation(pid: int) -> tuple[str, str, str]:
    """Return visibility, starttime and state without merging denial with exit."""

    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", "", ""
    except PermissionError:
        return "inaccessible", "", ""
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESRCH}:
            return "missing", "", ""
        return "inaccessible", "", ""
    try:
        tail = raw[raw.rfind(")") + 2 :].split()
        return "present", tail[19], tail[0]
    except IndexError:
        return "inaccessible", "", ""


def process_starttime(pid: int) -> str | None:
    """Compatibility starttime view; zombies are no longer active owners."""

    visibility, starttime, state = process_observation(pid)
    if visibility != "present" or state == "Z":
        return None
    return starttime


class ProcessGroupObservation(NamedTuple):
    state: str
    members: tuple[tuple[int, str], ...] = ()
    reason: str = ""


def process_group_observation(pgid: int) -> ProcessGroupObservation:
    if pgid <= 0:
        return ProcessGroupObservation("unverifiable", reason="invalid-pgid")
    members: list[tuple[int, str]] = []
    incomplete = ""
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        return ProcessGroupObservation(
            "unverifiable", reason=f"procfs-enumeration:{exc.errno or 'error'}"
        )
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            tail = raw[raw.rfind(")") + 2 :].split()
            if int(tail[2]) == pgid:
                members.append((int(entry.name), tail[0]))
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                continue
            incomplete = f"procfs-member:{entry.name}:{exc.errno or 'error'}"
        except (IndexError, ValueError):
            incomplete = f"procfs-member:{entry.name}:malformed"
    ordered = tuple(sorted(members, key=lambda member: member[0]))
    if any(state != "Z" for _pid, state in ordered):
        return ProcessGroupObservation("populated", ordered, incomplete)
    if incomplete:
        return ProcessGroupObservation("unverifiable", ordered, incomplete)
    return ProcessGroupObservation("empty", ordered)


def process_group_members(pgid: int) -> tuple[tuple[int, str], ...]:
    return process_group_observation(pgid).members


def lease_is_active(lease: object) -> bool:
    if not isinstance(lease, dict):
        return False
    pid = int(lease.get("pid", -1))
    visibility, actual_start, state = process_observation(pid)
    if (
        visibility == "present"
        and state != "Z"
        and actual_start == str(lease.get("starttime", ""))
    ):
        return True
    if visibility == "inaccessible":
        return True
    pgid = int(lease.get("pgid", -1))
    if pgid != pid or lease.get("group_owned") is not True:
        return False
    group = process_group_observation(pgid)
    return group.state != "empty"


def owner_identity_is_active(pid: int, expected_start: str) -> bool:
    visibility, actual_start, state = process_observation(pid)
    if visibility == "inaccessible":
        return True
    return (
        visibility == "present"
        and state != "Z"
        and actual_start == expected_start
    )


def _owned_group_metadata(pid: int) -> dict[str, object]:
    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return {}
    if pgid != pid:
        return {}
    return {"group_owned": True, "pgid": pgid}


def _limits(total: int | None, budget: int | None) -> tuple[int, int]:
    total = total if total is not None else int(os.environ.get("AGENT_MODEL_WORKER_TOTAL", DEFAULT_TOTAL_LIMIT))
    budget = budget if budget is not None else int(os.environ.get("AGENT_MODEL_WORKER_START_BUDGET", DEFAULT_START_BUDGET))
    if total < 1 or budget < 1:
        raise ValueError("model-worker limits must be positive")
    return total, budget


def _state_change(root: str | Path, fn: Callable[[dict[str, Any], float], Any]) -> Any:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    with (root / "lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
                "schema_version": 2,
                "claims": {},
                "leases": {},
                "reservations": {},
                "starts": [],
            }
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"invalid governor state: {exc}") from exc

        if not isinstance(data.get("leases", {}), dict):
            raise ValueError("invalid governor state: leases must be an object")
        if not isinstance(data.get("claims", {}), dict):
            raise ValueError("invalid governor state: claims must be an object")
        if not isinstance(data.get("reservations", {}), dict):
            raise ValueError("invalid governor state: reservations must be an object")
        if not isinstance(data.get("starts", []), list):
            raise ValueError("invalid governor state: starts must be an array")
        schema_version = data.get("schema_version", 1)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ValueError("invalid governor state: schema_version must be an integer")
        if schema_version < 1 or schema_version > 2:
            raise ValueError(f"unsupported governor state schema: {schema_version}")
        data["schema_version"] = 2
        data.setdefault("claims", {})
        data.setdefault("leases", {})
        data.setdefault("reservations", {})
        data.setdefault("starts", [])

        now = time.time()
        data["starts"] = [stamp for stamp in data.get("starts", []) if now - stamp < START_WINDOW_SECONDS]
        data["leases"] = {
            token: lease
            for token, lease in data.get("leases", {}).items()
            if lease_is_active(lease)
        }
        data["reservations"] = {
            token: reservation
            for token, reservation in data.get("reservations", {}).items()
            if isinstance(reservation, dict)
            and owner_identity_is_active(
                int(reservation.get("owner_pid", -1)),
                str(reservation.get("owner_starttime", "")),
            )
        }
        # A short-lived runner may finish before its reserving wrapper observes
        # the transfer. Keep a bounded claim receipt while the claimant lease is
        # live, or until the live owner has had a full observation window.
        data["claims"] = {
            token: claim
            for token, claim in data.get("claims", {}).items()
            if isinstance(claim, dict)
            and (
                token in data["leases"]
                or (
                    owner_identity_is_active(
                        int(claim.get("owner_pid", -1)),
                        str(claim.get("owner_starttime", "")),
                    )
                    and now - float(claim.get("released_at") or claim.get("claimed_at", 0))
                    < CLAIM_RECEIPT_SECONDS
                )
            )
        }
        result = fn(data, now)
        tmp = root / "state.tmp"
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, state_path)
        return result


def _assert_available(
    root: str | Path,
    data: dict[str, Any],
    worker_class: str,
    total: int,
    budget: int,
    count: int = 1,
) -> None:
    root = Path(root)
    if worker_class not in CLASS_LIMITS:
        raise ValueError("unknown worker class")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("reservation count must be a positive integer")
    if os.environ.get("AGENT_MODEL_WORKERS_DISABLED") == "1" or (root / "KILL_SWITCH").exists():
        raise ValueError("model-worker kill switch active")
    leases = data["leases"]
    reservations = data["reservations"]
    occupied = [*leases.values(), *reservations.values()]
    if len(occupied) + count > total:
        raise ValueError("global model-worker cap reached")
    if sum(item.get("class") == worker_class for item in occupied) + count > CLASS_LIMITS[worker_class]:
        raise ValueError(f"{worker_class} class cap reached")
    # Unclaimed reservations hold rolling-budget capacity. Claiming one moves
    # that capacity from ``reservations`` to ``starts`` in the same lock.
    if len(data["starts"]) + len(reservations) + count > budget:
        raise ValueError("rolling model-worker start budget reached")


def check(root: str | Path, worker_class: str, *, total: int | None = None, budget: int | None = None) -> None:
    """Check admission without consuming a rolling-start budget entry."""
    total, budget = _limits(total, budget)
    _state_change(root, lambda data, now: _assert_available(root, data, worker_class, total, budget))


def acquire(
    root: str | Path,
    worker_class: str,
    pid: int | None = None,
    *,
    total: int | None = None,
    budget: int | None = None,
) -> str:
    total, budget = _limits(total, budget)
    pid = os.getpid() if pid is None else pid
    starttime = process_starttime(pid)
    if starttime is None:
        raise ValueError("requesting process identity unavailable")

    def operation(data: dict[str, Any], now: float) -> str:
        if process_starttime(pid) != starttime:
            raise ValueError("requesting process identity changed")
        _assert_available(root, data, worker_class, total, budget)
        token = _new_token(data)
        data["leases"][token] = {
            "class": worker_class,
            "pid": pid,
            "starttime": starttime,
            "acquired_at": now,
            **_owned_group_metadata(pid),
        }
        data["starts"].append(now)
        return token

    return _state_change(root, operation)


def _new_token(data: dict[str, Any]) -> str:
    while True:
        token = secrets.token_hex(TOKEN_BYTES)
        if (
            token not in data["claims"]
            and token not in data["leases"]
            and token not in data["reservations"]
        ):
            return token


def _validate_reservation_token(token: str) -> None:
    if len(token) != TOKEN_BYTES * 2 or any(ch not in "0123456789abcdef" for ch in token):
        raise ValueError("invalid reservation token")


def _process_invokes_exact_python_script(pid: int, expected: Path) -> bool:
    """Return true only when argv's executed script slot is ``expected``.

    Looking for a matching path anywhere in argv is not an invocation proof: a
    ``python -c`` caller can append that path as inert data.  Repository batch
    launches use the current interpreter with the conductor as argv[1], so bind
    both the executable and that exact script slot.
    """

    try:
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        current_interpreter = Path(sys.executable).resolve(strict=True)
        argv = [
            os.fsdecode(raw)
            for raw in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if raw
        ]
        cwd = Path(f"/proc/{pid}/cwd").resolve(strict=True)
    except (OSError, ValueError):
        return False
    if executable != current_interpreter or len(argv) < 2:
        return False
    script_arg = argv[1]
    if not script_arg or script_arg.startswith("-"):
        return False
    try:
        candidate = Path(script_arg).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        return candidate.resolve(strict=False) == expected.resolve(strict=False)
    except (OSError, ValueError):
        return False


def _batch_issuer_is_current_parent(pid: int) -> bool:
    """Accept a batch mint only from this exact repository conductor process."""

    return (
        pid == os.getppid()
        and _process_invokes_exact_python_script(
            pid, Path(__file__).resolve().with_name("dispatch-batch.py")
        )
    )


def _issue_batch_issuer_capability(pid: int) -> _BatchIssuerCapability:
    """Mint an opaque capability after validating the live parent invocation."""

    if not _batch_issuer_is_current_parent(pid):
        raise ValueError("parallel batch reservation issuer is not dispatch-batch")
    starttime = process_starttime(pid)
    if starttime is None:
        raise ValueError("parallel batch reservation issuer identity unavailable")
    return _BatchIssuerCapability(pid, starttime, _BATCH_ISSUER_SEAL)


def _consume_batch_issuer_capability(
    capability: _BatchIssuerCapability | None, pid: int
) -> None:
    """Require the exact verified issuer at the Python API boundary."""

    valid = (
        isinstance(capability, _BatchIssuerCapability)
        and capability._seal is _BATCH_ISSUER_SEAL
        and not capability._used
        and capability.pid == pid
        and process_starttime(pid) == capability.starttime
    )
    if not valid:
        raise ValueError("replica batch reservation issuer capability invalid")
    capability._used = True


def _validate_batch_peer(
    peer: object,
    manifest: dict[str, object],
    manifest_digest: str,
    leg_digests: dict[str, str],
    selected_attempt_ids: list[str],
) -> dict[str, object]:
    """Prove one non-selected member before a one-leg recovery reservation."""

    required = {"agent_home", "attempt_id", "jobs", "route"}
    if not isinstance(peer, dict) or set(peer) != required:
        raise ValueError("parallel batch partial recovery requires exact peer proof")
    paths: dict[str, Path] = {}
    for key in ("agent_home", "jobs", "route"):
        value = peer.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("parallel batch peer paths must be absolute")
        paths[key] = Path(value).resolve(strict=False)
    attempt_id = peer.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("parallel batch peer attempt is invalid")
    members = {
        str(member["attempt_id"]): member for member in manifest["members"]
    }
    if attempt_id not in members or attempt_id in selected_attempt_ids:
        raise ValueError("parallel batch peer is not a non-selected member")

    try:
        route = json.loads(paths["route"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"parallel batch peer route unreadable: {exc}") from exc
    if not isinstance(route, dict) or route.get("route_id") != manifest["route_id"]:
        raise ValueError("parallel batch peer route mismatch")
    group = manifest.get("parallel_group") or manifest.get("replica_group")
    group_nodes = sorted(
        str(node.get("id", ""))
        for node in route.get("nodes", [])
        if isinstance(node, dict)
        and (node.get("parallel_group") or node.get("replica_group")) == group
    )
    if group_nodes != sorted(str(member["route_node"]) for member in manifest["members"]):
        raise ValueError("parallel batch peer route group mismatch")

    from dispatch_contract import (  # local import avoids bootstrap cycles
        DispatchContractError,
        attempt_process_quiescence,
        completion_attempt_readiness,
        completion_marker_is_current,
        dispatch_state_roots,
        parse_registry_metadata,
        validate_attempt_metadata,
    )

    jobs = paths["jobs"]
    try:
        with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ValueError(f"parallel batch peer registry unreadable: {exc}") from exc
    exact = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == attempt_id:
            exact.append((fields, metadata))
    if len(exact) != 1:
        raise ValueError("parallel batch peer row is not unique")
    fields, metadata = exact[0]
    try:
        validate_attempt_metadata(metadata)
    except DispatchContractError as exc:
        raise ValueError(f"parallel batch peer row invalid: {exc.reason}") from exc
    member = members[attempt_id]
    schema_version = int(manifest.get("schema_version", 1))
    reservation_kind = "parallel-batch" if schema_version == 2 else "replica-batch"
    expected = {
        "attempt_id": attempt_id,
        "route_id": str(manifest["route_id"]),
        "route_node": str(member["route_node"]),
        "parent_attempt_id": str(manifest["parent_attempt_id"]),
        "harness": str(member["harness"]),
        "child_harness": str(member["harness"]),
        "fallback_hop": str(member["fallback_hop"]),
        "fallback_ordinal": str(member["fallback_ordinal"]),
        "reservation_kind": reservation_kind,
        "batch_declared_size": str(manifest["declared_size"]),
        "batch_group": str(group),
        "batch_route_id": str(manifest["route_id"]),
        "batch_parent_attempt_id": str(manifest["parent_attempt_id"]),
        "batch_attempt_id": attempt_id,
        "batch_route_node": str(member["route_node"]),
        "batch_harness": str(member["harness"]),
        "batch_fallback_hop": str(member["fallback_hop"]),
        "batch_fallback_ordinal": str(member["fallback_ordinal"]),
        "batch_independence": str(manifest["independence"]),
        "batch_assignment_sha256": str(member["assignment_sha256"]),
        "batch_manifest_sha256": manifest_digest,
        "batch_leg_sha256": leg_digests[attempt_id],
        "launch_claimed": "1",
    }
    if schema_version == 2:
        expected.update({
            "parallel_group": str(group),
            "replica_group": str(group),
            "batch_model_profile": str(member["model_profile"]),
            "batch_perspective": str(member["perspective"]),
            "batch_parallel_leg_index": str(member["parallel_leg_index"]),
        })
    mismatches = [
        key for key, value in expected.items() if metadata.get(key) != value
    ]
    if (
        mismatches
        or os.path.realpath(fields[3]) != os.path.realpath(str(route.get("cwd", "")))
    ):
        raise ValueError(
            "parallel batch peer identity mismatch: " + ",".join(mismatches)
        )

    peer_state = ""
    proof_reason = ""
    if fields[1] in {"open", "running"}:
        process = attempt_process_quiescence(metadata)
        if process.state != "live":
            raise ValueError(f"parallel batch peer is not live: {process.reason}")
        peer_state, proof_reason = "active", process.reason
    elif fields[1] == "done" and metadata.get("note") == "completed-marker":
        node = next(
            (
                value for value in route.get("nodes", [])
                if isinstance(value, dict) and value.get("id") == member["route_node"]
            ),
            None,
        )
        marker_path = next(
            (
                candidate
                for candidate in (
                    root / "completion" / str(manifest["route_id"]) / f"{member['route_node']}.json"
                    for root in dispatch_state_roots(paths["agent_home"], jobs)
                )
                if candidate.is_file()
            ),
            paths["agent_home"] / ".dispatch" / "completion"
            / str(manifest["route_id"]) / f"{member['route_node']}.json",
        )
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"parallel batch peer marker unreadable: {exc}") from exc
        if not isinstance(node, dict) or not completion_marker_is_current(
            route, node, marker_path, marker
        ):
            raise ValueError("parallel batch peer marker is not current")
        readiness = completion_attempt_readiness(
            route, node, marker, jobs, registry_lines=lines
        )
        if readiness.state != "ready":
            raise ValueError(f"parallel batch peer completion not ready: {readiness.reason}")
        peer_state, proof_reason = "completed", readiness.reason
    else:
        raise ValueError("parallel batch peer is terminal without completion")

    proof = {
        "agent_home": str(paths["agent_home"]),
        "attempt_id": attempt_id,
        "jobs": str(jobs),
        "manifest_sha256": manifest_digest,
        "reason": proof_reason,
        "route": str(paths["route"]),
        "state": peer_state,
    }
    encoded = json.dumps(proof, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return {
        "batch_peer_attempt_id": attempt_id,
        "batch_peer_state": peer_state,
        "batch_peer_proof": proof,
        "batch_peer_proof_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def _validate_batch_peers(
    peers: object,
    manifest: dict[str, object],
    manifest_digest: str,
    leg_digests: dict[str, str],
    selected_attempt_ids: list[str],
) -> dict[str, object]:
    """Prove the exact N-1 peer set for a single missing-leg recovery."""

    members = {str(member["attempt_id"]) for member in manifest["members"]}
    expected = members - set(selected_attempt_ids)
    if not isinstance(peers, list) or len(peers) != len(expected):
        raise ValueError("parallel batch recovery requires the exact N-1 peer set")
    supplied = [peer.get("attempt_id") if isinstance(peer, dict) else None for peer in peers]
    if len(set(supplied)) != len(supplied) or set(supplied) != expected:
        raise ValueError("parallel batch recovery peer membership mismatch")
    proofs = [
        _validate_batch_peer(
            peer, manifest, manifest_digest, leg_digests, selected_attempt_ids
        )
        for peer in peers
    ]
    normalized = sorted(
        (proof["batch_peer_proof"] for proof in proofs),
        key=lambda proof: str(proof["attempt_id"]),
    )
    encoded = json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")
    result = {
        "batch_peer_count": len(normalized),
        "batch_peer_set": normalized,
        "batch_peer_set_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }
    if int(manifest.get("schema_version", 1)) == 1 and len(proofs) == 1:
        result.update(proofs[0])
    return result


def reserve(
    root: str | Path,
    worker_class: str,
    count: int,
    pid: int | None = None,
    *,
    total: int | None = None,
    budget: int | None = None,
    batch: dict[str, Any] | None = None,
    batch_issuer: _BatchIssuerCapability | None = None,
) -> list[str]:
    """Atomically reserve ``count`` future leases for one live owner process."""
    total, budget = _limits(total, budget)
    pid = os.getpid() if pid is None else pid
    starttime = process_starttime(pid)
    if starttime is None:
        raise ValueError("reservation owner identity unavailable")
    members: list[dict[str, object]] = []
    batch_common: dict[str, Any] = {}
    if batch is not None:
        _consume_batch_issuer_capability(batch_issuer, pid)
        if (
            not isinstance(batch, dict)
            or set(batch) - {"manifest", "selected_attempt_ids", "peers"}
            or not {"manifest", "selected_attempt_ids"}.issubset(batch)
        ):
            raise ValueError("invalid parallel batch reservation metadata")
        try:
            manifest, manifest_digest, leg_digests = verify_manifest(batch["manifest"])
        except ReplicaBatchContractError as exc:
            raise ValueError(str(exc)) from exc
        selected = batch["selected_attempt_ids"]
        if (
            not isinstance(selected, list)
            or len(selected) != count
            or any(not isinstance(value, str) or not value for value in selected)
            or len(set(selected)) != count
        ):
            raise ValueError("parallel batch selected member count mismatch")
        member_by_attempt = {
            str(member["attempt_id"]): member for member in manifest["members"]
        }
        if any(attempt_id not in member_by_attempt for attempt_id in selected):
            raise ValueError("parallel batch selected member is not declared")
        members = [member_by_attempt[attempt_id] for attempt_id in selected]
        peer_proof: dict[str, object] = {}
        if count == int(manifest["declared_size"]):
            if batch.get("peers") is not None:
                raise ValueError("full parallel batch reservation cannot include peer proof")
        elif count == 1:
            peer_proof = _validate_batch_peers(
                batch.get("peers"), manifest, manifest_digest, leg_digests, selected
            )
        else:
            raise ValueError("parallel batch reservation count must be one or declared size")
        group = manifest.get("parallel_group") or manifest.get("replica_group")
        batch_common = {
            "reservation_kind": (
                "parallel-batch" if int(manifest.get("schema_version", 1)) == 2
                else "replica-batch"
            ),
            "batch_declared_size": manifest["declared_size"],
            "batch_admission_count": count,
            "batch_group": group,
            "batch_route_id": manifest["route_id"],
            "batch_parent_attempt_id": manifest["parent_attempt_id"],
            "batch_independence": manifest["independence"],
            "batch_manifest": manifest,
            "batch_manifest_sha256": manifest_digest,
            **peer_proof,
        }

    def operation(data: dict[str, Any], now: float) -> list[str]:
        if process_starttime(pid) != starttime:
            raise ValueError("reservation owner identity changed")
        _assert_available(root, data, worker_class, total, budget, count)
        tokens = []
        for index in range(count):
            token = _new_token(data)
            reservation = {
                "class": worker_class,
                "owner_pid": pid,
                "owner_starttime": starttime,
                "reserved_at": now,
            }
            if members:
                reservation.update(batch_common)
                reservation.update({
                    "batch_attempt_id": members[index]["attempt_id"],
                    "batch_route_node": members[index]["route_node"],
                    "batch_harness": members[index]["harness"],
                    "batch_fallback_hop": members[index]["fallback_hop"],
                    "batch_fallback_ordinal": members[index]["fallback_ordinal"],
                    "batch_assignment_sha256": members[index]["assignment_sha256"],
                    "batch_leg_sha256": leg_digests[str(members[index]["attempt_id"])],
                })
                if int(manifest.get("schema_version", 1)) == 2:
                    reservation.update({
                        "batch_model_profile": members[index]["model_profile"],
                        "batch_perspective": members[index]["perspective"],
                        "batch_parallel_leg_index": members[index]["parallel_leg_index"],
                    })
            data["reservations"][token] = reservation
            tokens.append(token)
        return tokens

    return _state_change(root, operation)


def reservation_check(
    root: str | Path,
    token: str,
    *,
    worker_class: str | None = None,
    owner_pid: int | None = None,
) -> dict[str, Any]:
    """Return bounded unclaimed/claimed/absent state without consuming a token."""
    _validate_reservation_token(token)

    def operation(data: dict[str, Any], now: float) -> dict[str, Any]:
        reservation = data["reservations"].get(token)
        claim = data["claims"].get(token)
        record = reservation or claim
        if record is None:
            return {"state": "absent"}
        if worker_class is not None and record["class"] != worker_class:
            raise ValueError("reservation class mismatch")
        if owner_pid is not None and int(record["owner_pid"]) != owner_pid:
            raise ValueError("reservation owner mismatch")
        if reservation is not None:
            result = {
                "class": reservation["class"],
                "owner_pid": int(reservation["owner_pid"]),
                "owner_starttime": str(reservation["owner_starttime"]),
                "state": "unclaimed",
            }
            result.update({key: reservation[key] for key in BATCH_RESERVATION_KEYS if key in reservation})
            return result
        result = {
            "claimant_pid": int(claim["claimant_pid"]),
            "claimant_starttime": str(claim["claimant_starttime"]),
            "class": claim["class"],
            "lease_active": token in data["leases"],
            "owner_pid": int(claim["owner_pid"]),
            "owner_starttime": str(claim["owner_starttime"]),
            "state": "claimed",
        }
        result.update({key: claim[key] for key in BATCH_RESERVATION_KEYS if key in claim})
        return result

    return _state_change(root, operation)


def claim_reservation(root: str | Path, token: str, worker_class: str) -> str:
    """Move one reservation into a lease owned by this governor-run process."""
    _validate_reservation_token(token)
    pid = os.getpid()
    starttime = process_starttime(pid)
    if starttime is None:
        raise ValueError("requesting process identity unavailable")

    def operation(data: dict[str, Any], now: float) -> str:
        reservation = data["reservations"].get(token)
        if reservation is None:
            if token in data["leases"]:
                raise ValueError("reservation already claimed")
            raise ValueError("reservation unavailable")
        if reservation["class"] != worker_class:
            raise ValueError("reservation class mismatch")
        if process_starttime(pid) != starttime:
            raise ValueError("requesting process identity changed")
        del data["reservations"][token]
        claim = {
            "claimant_pid": pid,
            "claimant_starttime": starttime,
            "claimed_at": now,
            "class": worker_class,
            "owner_pid": int(reservation["owner_pid"]),
            "owner_starttime": str(reservation["owner_starttime"]),
            "released_at": None,
        }
        claim.update({key: reservation[key] for key in BATCH_RESERVATION_KEYS if key in reservation})
        data["claims"][token] = claim
        data["leases"][token] = {
            "class": worker_class,
            "pid": pid,
            "starttime": starttime,
            "acquired_at": now,
            "reserved_at": reservation["reserved_at"],
            **_owned_group_metadata(pid),
        }
        data["starts"].append(now)
        return token

    return _state_change(root, operation)


def cancel_reservation(root: str | Path, token: str) -> bool:
    """Cancel one unclaimed reservation; never release a claimed lease."""
    _validate_reservation_token(token)

    def operation(data: dict[str, Any], now: float) -> bool:
        if token in data["claims"] or token in data["leases"]:
            raise ValueError("reservation already claimed")
        return data["reservations"].pop(token, None) is not None

    return _state_change(root, operation)


def release(root: str | Path, token: str) -> None:
    def operation(data: dict[str, Any], now: float) -> None:
        lease = data["leases"].get(token)
        release_safe = True
        if isinstance(lease, dict) and lease.get("group_owned") is True:
            pid = int(lease.get("pid", -1))
            pgid = int(lease.get("pgid", -1))
            group = process_group_observation(pgid)
            live_descendants = any(
                member_pid != pid and state != "Z"
                for member_pid, state in group.members
            )
            release_safe = group.state == "empty" or (
                group.state == "populated"
                and not live_descendants
                and not group.reason
            )
            if group.state == "unverifiable":
                release_safe = False
        if release_safe:
            data["leases"].pop(token, None)
        if token in data["claims"] and data["claims"][token].get("released_at") is None:
            data["claims"][token]["released_at"] = now

    _state_change(root, operation)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(default_root()))
    commands = parser.add_subparsers(dest="command", required=True)
    acquire_parser = commands.add_parser("acquire")
    acquire_parser.add_argument("--class", dest="worker_class", required=True)
    acquire_parser.add_argument("--pid", type=int)
    check_parser = commands.add_parser("check")
    check_parser.add_argument("--class", dest="worker_class", required=True)
    reserve_parser = commands.add_parser("reserve")
    reserve_parser.add_argument("--class", dest="worker_class", required=True)
    reserve_parser.add_argument("--count", type=int, required=True)
    reserve_parser.add_argument("--pid", type=int, required=True)
    reserve_parser.add_argument("--batch-manifest")
    reserve_parser.add_argument("--batch-attempt-id", action="append", default=[])
    reserve_parser.add_argument("--batch-peer-agent-home")
    reserve_parser.add_argument("--batch-peer-attempt-id")
    reserve_parser.add_argument("--batch-peer-jobs")
    reserve_parser.add_argument("--batch-peer-route")
    reserve_parser.add_argument("--batch-peers-json")
    reservation_check_parser = commands.add_parser("reservation-check")
    reservation_check_parser.add_argument("--token", required=True)
    reservation_check_parser.add_argument("--class", dest="worker_class")
    reservation_check_parser.add_argument("--pid", type=int, dest="owner_pid")
    cancel_parser = commands.add_parser("cancel")
    cancel_parser.add_argument("--token", required=True)
    release_parser = commands.add_parser("release")
    release_parser.add_argument("--token", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--class", dest="worker_class", required=True)
    run_parser.add_argument("command_argv", nargs=argparse.REMAINDER)
    commands.add_parser("status")
    args = parser.parse_args()

    if args.command == "acquire":
        print(acquire(args.root, args.worker_class, args.pid))
    elif args.command == "check":
        check(args.root, args.worker_class)
        print("governor=available")
    elif args.command == "reserve":
        peer_values = (
            args.batch_peer_agent_home,
            args.batch_peer_attempt_id,
            args.batch_peer_jobs,
            args.batch_peer_route,
        )
        batch_values = (
            args.batch_manifest,
            args.batch_attempt_id,
            *peer_values,
            args.batch_peers_json,
        )
        batch = None
        if any(batch_values):
            if not args.batch_manifest or not args.batch_attempt_id:
                raise ValueError("incomplete parallel batch reservation metadata")
            try:
                manifest = json.loads(args.batch_manifest)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid parallel batch manifest JSON") from exc
            batch = {
                "manifest": manifest,
                "selected_attempt_ids": args.batch_attempt_id,
            }
            if args.batch_peers_json and any(peer_values):
                raise ValueError("plural and legacy singular peer proof surfaces are exclusive")
            if args.batch_peers_json:
                try:
                    peers = json.loads(args.batch_peers_json)
                except json.JSONDecodeError as exc:
                    raise ValueError("invalid parallel batch peers JSON") from exc
                if not isinstance(peers, list):
                    raise ValueError("parallel batch peers JSON must be a list")
                batch["peers"] = peers
            elif any(peer_values):
                if not all(peer_values):
                    raise ValueError("incomplete replica batch peer proof")
                batch["peers"] = [{
                    "agent_home": args.batch_peer_agent_home,
                    "attempt_id": args.batch_peer_attempt_id,
                    "jobs": args.batch_peer_jobs,
                    "route": args.batch_peer_route,
                }]
            batch_issuer = _issue_batch_issuer_capability(args.pid)
        else:
            batch_issuer = None
        tokens = reserve(
            args.root,
            args.worker_class,
            args.count,
            args.pid,
            batch=batch,
            batch_issuer=batch_issuer,
        )
        receipt = {
            "class": args.worker_class,
            "count": len(tokens),
            "owner_pid": args.pid,
            "tokens": tokens,
        }
        if batch is not None:
            receipt["batch_manifest_sha256"] = verify_manifest(batch["manifest"])[1]
        print(json.dumps(receipt, sort_keys=True))
    elif args.command == "reservation-check":
        result = reservation_check(
            args.root,
            args.token,
            worker_class=args.worker_class,
            owner_pid=args.owner_pid,
        )
        print(json.dumps(result, sort_keys=True))
        if result["state"] == "absent":
            return 75
    elif args.command == "cancel":
        cancelled = cancel_reservation(args.root, args.token)
        print(f"reservation={'cancelled' if cancelled else 'absent'}")
    elif args.command == "release":
        release(args.root, args.token)
    elif args.command == "status":
        print(json.dumps(_state_change(args.root, lambda data, now: data), sort_keys=True))
    else:
        command = args.command_argv[1:] if args.command_argv[:1] == ["--"] else args.command_argv
        if not command:
            raise ValueError("worker command is required")
        if RESERVATION_ENV in os.environ:
            token = claim_reservation(
                args.root,
                os.environ[RESERVATION_ENV],
                args.worker_class,
            )
        else:
            token = acquire(args.root, args.worker_class)
        child_env = dict(os.environ)
        child_env.pop(RESERVATION_ENV, None)
        try:
            child = subprocess.Popen(command, env=child_env)
            return_code = child.wait()
            if os.getpgrp() == os.getpid():
                while True:
                    group = process_group_observation(os.getpid())
                    if group.state == "empty" or (
                        group.state == "populated"
                        and not group.reason
                        and not any(
                            member_pid != os.getpid() and state != "Z"
                            for member_pid, state in group.members
                        )
                    ):
                        break
                    time.sleep(0.05)
            return return_code
        finally:
            release(args.root, token)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"model-worker-governor: {exc}", file=sys.stderr)
        raise SystemExit(75)
