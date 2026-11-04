"""Dispatch contract: model-governor reservations.

Governor responses, the replica batch expectation, reserving, cancelling and
waiting for a governor token, and the governor witness handles.

Split out of `dispatch_contract.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Mapping
from governor_identity import close_witness, create_witness
from route_authority import route_in_force
from replica_batch_contract import DIGEST

from dispatch_contract_base import (
    DispatchContractError,
)

from dispatch_contract_process import (
    _runtime_ancestry_proc_stat, process_launch_identity, process_start_ticks,
)


_GOVERNOR_WITNESS_HANDLES: dict[tuple[str, str], object] = {}


def _governor_witness_key(root: Path, token: str) -> tuple[str, str]:
    return str(root.resolve()), token


def _return_governor_witness(root: Path, token: str, receipt: dict | None = None) -> None:
    key = _governor_witness_key(root, token)
    handle = _GOVERNOR_WITNESS_HANDLES.get(key)
    if handle is None:
        return  # Provided batch token: this wrapper owns no issuer FD.
    if handle.creator_pid != os.getpid():
        return
    if receipt is not None and receipt.get("owner_witness") != handle.binding():
        raise DispatchContractError("model-worker-reservation-claim-mismatch", "owner witness receipt differs")
    _GOVERNOR_WITNESS_HANDLES.pop(key)
    close_witness(handle)


def _governor_json(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    allow_absent: bool = False,
) -> dict[str, object]:
    result = subprocess.run(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        payload = {}
    if (
        result.returncode != 0
        and not (allow_absent and isinstance(payload, dict) and payload.get("state") == "absent")
    ) or not isinstance(payload, dict):
        detail = (result.stderr or result.stdout).strip()[:512] or f"exit-{result.returncode}"
        exc = DispatchContractError("model-worker-governor-denied", detail)
        # A typed admission refusal (`_AdmissionRefused`) puts its JSON on
        # stdout ahead of the stderr sentence `detail` already captured, so
        # `payload` is preserved for a retryable receipt without changing
        # `reason`/`detail`. A kill-switch or validation `ValueError` prints no
        # such JSON, so `payload` stays `{}` and this is a no-op.
        if isinstance(payload, dict) and payload.get("state") == "refused":
            exc.admission = payload
        raise exc
    return payload


def governor_refusal_fields(exc: DispatchContractError) -> dict[str, str]:
    """Retry-shaped receipt fields from a typed governor admission refusal.

    Only a `retryable` refusal (the governor's `_AdmissionRefused` JSON) gets
    these; a kill-switch or validation error carries no `admission` payload at
    all, so this returns `{}` and the adapter's existing refusal receipt is
    unchanged.
    """
    admission = getattr(exc, "admission", None)
    if not isinstance(admission, dict) or admission.get("retryable") is not True:
        return {}

    def field(value: object) -> str:
        return "-" if value is None else str(value)

    return {
        "retryable": "1",
        "refusal": field(admission.get("refusal")),
        "worker_class": field(admission.get("class")),
        "retry_after_seconds": field(admission.get("retry_after_seconds")),
        "frees_at": field(admission.get("frees_at")),
    }


def replica_batch_expectation(
    route_file: str | Path | None,
    route_node: str | None,
    action: str,
    *,
    attempt_id: str = "",
    parent_attempt_id: str = "",
    harness: str = "",
    fallback_hop: str = "",
    fallback_ordinal: int | str | None = None,
    assignment_sha256: str = "",
) -> dict[str, object] | None:
    """Return the exact governor binding required by a parallel route leg.

    A parallel row has no standalone registered form. ``start`` is authorized
    only by a live opaque governor reservation whose immutable provenance was
    created from the complete 2..4-leg manifest by ``dispatch-batch``. The
    function name remains as a one-window adapter import alias.
    """

    if not route_file or not route_node:
        return None
    try:
        route = route_in_force(json.loads(Path(route_file).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise DispatchContractError("route-record-unreadable", str(exc)) from exc
    if not isinstance(route, dict) or not isinstance(route.get("nodes"), list):
        raise DispatchContractError("route-record-invalid", "route nodes must be an array")
    matches = [
        node for node in route["nodes"]
        if isinstance(node, dict) and node.get("id") == route_node
    ]
    if len(matches) != 1:
        raise DispatchContractError("route-node-not-unique", str(route_node))
    node = matches[0]
    group = node.get("parallel_group") or node.get("replica_group")
    if not group:
        return None
    members = [
        candidate for candidate in route["nodes"]
        if isinstance(candidate, dict)
        and (candidate.get("parallel_group") or candidate.get("replica_group")) == group
    ]
    if not 2 <= len(members) <= 4 or any(candidate.get("dispatch_depth") != 2 for candidate in members):
        raise DispatchContractError(
            "parallel-group-contract-invalid", f"group={group} count={len(members)}"
        )
    if action != "start":
        raise DispatchContractError(
            "parallel-group-batch-required",
            f"group={group} action={action}; use dispatch-batch --parallel-group {group} --action start",
        )
    values = {
        "attempt_id": attempt_id,
        "parent_attempt_id": parent_attempt_id,
        "harness": harness,
        "fallback_hop": fallback_hop,
        "fallback_ordinal": str(fallback_ordinal or ""),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise DispatchContractError(
            "parallel-group-batch-binding-missing", ",".join(missing)
        )
    try:
        ordinal = int(str(fallback_ordinal))
    except (TypeError, ValueError) as exc:
        raise DispatchContractError(
            "parallel-group-batch-binding-invalid",
            f"fallback_ordinal={fallback_ordinal}",
        ) from exc
    if ordinal < 1:
        raise DispatchContractError(
            "parallel-group-batch-binding-invalid",
            f"fallback_ordinal={fallback_ordinal}",
        )
    allowed_members: dict[str, list[dict[str, object]]] = {}
    for member in members:
        member_id = str(member.get("id", ""))
        allowed: list[dict[str, object]] = []
        for entry in member.get("fallback_hops", []):
            if not isinstance(entry, dict):
                continue
            hop = entry.get("fallback_hop")
            hop_ordinal = entry.get("ordinal")
            if not isinstance(hop, str) or isinstance(hop_ordinal, bool) or not isinstance(hop_ordinal, int):
                continue
            for candidate in entry.get("candidates", []):
                if not isinstance(candidate, dict) or candidate.get("status") != "supported":
                    continue
                child_harness = candidate.get("child_harness")
                if child_harness not in {"codex", "claude", "opencode"}:
                    continue
                allowed.append({
                    "harness": child_harness,
                    "fallback_hop": hop,
                    "fallback_ordinal": hop_ordinal,
                })
        if not allowed:
            raise DispatchContractError(
                "parallel-group-route-binding-invalid", f"node={member_id}"
            )
        allowed_members[member_id] = allowed
    expected = {
        "reservation_kind": "parallel-batch" if node.get("parallel_group") else "replica-batch",
        "batch_declared_size": len(members),
        "batch_group": str(group),
        "batch_route_id": str(route.get("route_id", "")),
        "batch_parent_attempt_id": parent_attempt_id,
        "batch_attempt_id": attempt_id,
        "batch_route_node": str(route_node),
        "batch_harness": harness,
        "batch_fallback_hop": fallback_hop,
        "batch_fallback_ordinal": ordinal,
        "batch_model_profile": node.get("model_profile"),
        "batch_perspective": node.get("perspective"),
        "batch_parallel_leg_index": node.get("parallel_leg_index"),
        "_batch_route_nodes": sorted(str(member.get("id", "")) for member in members),
        "_batch_allowed_members": allowed_members,
        "_batch_profile_selections": {
            str(member["id"]): {key: member.get(key) for key in
                                ("model_profile", "profile_demand", "profile_selection")}
            for member in members if "profile_selection" in member
        },
    }
    if assignment_sha256:
        if not DIGEST.fullmatch(assignment_sha256):
            raise DispatchContractError(
                "parallel-group-assignment-invalid", assignment_sha256
            )
        expected["batch_assignment_sha256"] = assignment_sha256
    return expected


def _validate_replica_reservation(payload, expected) -> None:
    from route_authority import require_batch_reservation
    require_batch_reservation(payload, expected)


def reserve_governor_token(
    governor: Path,
    root: Path,
    worker_class: str,
    *,
    provided_token: str = "",
    expected_reservation: dict[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    """Reserve one slot, or validate a token atomically reserved by a batch."""

    if provided_token:
        payload = _governor_json(
            [
                sys.executable,
                str(governor),
                "--root",
                str(root),
                "reservation-check",
                "--token",
                provided_token,
                "--class",
                worker_class,
            ],
            allow_absent=True,
        )
        if payload.get("state") != "unclaimed":
            raise DispatchContractError(
                "model-worker-reservation-unavailable", str(payload.get("state", "invalid"))
            )
        _validate_replica_reservation(payload, expected_reservation)
        if expected_reservation is None:
            # A plain slot token (batch kinds were refused above) carries no batch
            # binding for the wrappers to read.
            return provided_token, {}
        return provided_token, payload
    if expected_reservation is not None:
        raise DispatchContractError(
            "parallel-group-batch-required",
            "parallel start requires an exact bound batch reservation",
        )
    handle = create_witness(root, "reservation")
    retained = False
    try:
        payload = _governor_json(
            [
            sys.executable,
            str(governor),
            "--root",
            str(root),
            "reserve",
            "--class",
            worker_class,
            "--count",
            "1",
            "--pid",
            str(os.getpid()),
            "--owner-witness",
            json.dumps(handle.binding(), sort_keys=True, separators=(",", ":")),
            ]
        )
        tokens = payload.get("tokens")
        if (not isinstance(tokens, list) or len(tokens) != 1 or not isinstance(tokens[0], str)
                or len(tokens[0]) != 32 or any(c not in "0123456789abcdef" for c in tokens[0])):
            raise DispatchContractError("model-worker-reservation-invalid", "expected one token")
        _GOVERNOR_WITNESS_HANDLES[_governor_witness_key(root, tokens[0])] = handle
        retained = True
        return tokens[0], {}
    except BaseException:
        if not retained:
            close_witness(handle)
        raise


def cancel_governor_reservation(governor: Path, root: Path, token: str) -> None:
    """Cancel only an unclaimed token; a claimed runner retains its lease."""

    if not token:
        return
    try:
        payload = _governor_json(
            [
                sys.executable,
                str(governor),
                "--root",
                str(root),
                "reservation-check",
                "--token",
                token,
            ]
        )
    except DispatchContractError as exc:
        print(f"model-worker-reservation-cancel-unverified: {exc.reason}", file=sys.stderr)
        return
    if payload.get("state") == "claimed":
        _return_governor_witness(root, token, payload)
        return
    if payload.get("state") != "unclaimed":
        print("model-worker-reservation-cancel-unverified: no exact return receipt", file=sys.stderr)
        return
    completed = subprocess.run(
        [
            sys.executable,
            str(governor),
            "--root",
            str(root),
            "cancel",
            "--token",
            token,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode == 0 and completed.stdout.strip() == "reservation=cancelled":
        _return_governor_witness(root, token)
    elif completed.returncode != 0:
        # Claim may have won after our unclaimed observation. Only its exact
        # transfer receipt can return this issuer's FD; an error alone cannot.
        payload = _governor_json([sys.executable, str(governor), "--root", str(root),
                                  "reservation-check", "--token", token])
        if payload.get("state") == "claimed":
            _return_governor_witness(root, token, payload)


RESERVATION_CLAIM_TIMEOUT_DEFAULT = 60.0
RESERVATION_CLAIM_TIMEOUT_ENV = "AGENT_DISPATCH_RESERVATION_CLAIM_TIMEOUT"


def reservation_claim_timeout() -> float:
    """Claim-wait budget: env override clamped to [1, 600], else the default.

    A route-bound launch runs launch-fence route verification before the
    governed runner claims its reservation, and that verification is
    storage-latency-bound (an 18s wall-clock verify was measured on an
    NFS-backed artifact root). The budget must absorb that pre-claim work; a
    dead child still fails immediately regardless of this value because the
    wait loop exits as soon as the spawned process is gone.
    """

    raw = os.environ.get(RESERVATION_CLAIM_TIMEOUT_ENV, "").strip()
    if not raw:
        return RESERVATION_CLAIM_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return RESERVATION_CLAIM_TIMEOUT_DEFAULT
    return min(max(value, 1.0), 600.0)


def wait_governor_reservation_claim(
    governor: Path,
    root: Path,
    token: str,
    proc: subprocess.Popen,
    *,
    timeout: float | None = None,
    expected_reservation: dict[str, object] | None = None,
    watchdog_receipt: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Observe reserve→runner transfer before the reserving process may exit."""

    claimant_pid = proc.pid
    claimant_start = None
    if watchdog_receipt is not None:
        from review_watchdog import _is_receipt_digest_valid
        watchdog = watchdog_receipt.get("watchdog") or {}
        claimant = watchdog_receipt.get("child") or {}
        observed_watchdog = process_launch_identity(proc.pid)
        if (not isinstance(watchdog, Mapping) or not isinstance(claimant, Mapping)
                or not _is_receipt_digest_valid(watchdog_receipt)
                or watchdog.get("pid") != str(proc.pid)
                or any(watchdog.get(k) != observed_watchdog.get(k) for k in
                       ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns"))):
            raise DispatchContractError("model-worker-reservation-watchdog-mismatch")
        try:
            claimant_pid = int(claimant["pid"])
            claimant_start = str(claimant["pid_start"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DispatchContractError("model-worker-reservation-claim-mismatch") from exc
        observed = _runtime_ancestry_proc_stat(claimant_pid)
        observed_claimant = process_launch_identity(claimant_pid)
        if (not observed or observed["ppid"] != proc.pid
                or any(claimant.get(k) != observed_claimant.get(k) for k in
                       ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns"))):
            raise DispatchContractError("model-worker-reservation-claim-mismatch", "sealed watchdog child differs")
    if timeout is None:
        timeout = reservation_claim_timeout()
    deadline = time.monotonic() + max(0.1, timeout)
    while True:
        payload = _governor_json(
            [
                sys.executable,
                str(governor),
                "--root",
                str(root),
                "reservation-check",
                "--token",
                token,
                "--class",
                "dispatch",
            ],
            allow_absent=True,
        )
        if payload.get("state") == "claimed":
            _validate_replica_reservation(payload, expected_reservation)
            if (
                str(payload.get("claimant_pid", "")) != str(claimant_pid)
                or str(payload.get("claimant_starttime", ""))
                != str(claimant_start or process_start_ticks(claimant_pid) or payload.get("claimant_starttime", ""))
            ):
                raise DispatchContractError(
                    "model-worker-reservation-claim-mismatch",
                    f"expected_pid={claimant_pid} claimant_pid={payload.get('claimant_pid', '-')}",
                )
            _return_governor_witness(root, token, payload)
            return payload
        if payload.get("state") == "absent":
            raise DispatchContractError(
                "model-worker-reservation-lost",
                "reservation disappeared before the governed runner claimed it",
            )
        if proc.poll() is not None or time.monotonic() >= deadline:
            raise DispatchContractError(
                "model-worker-reservation-claim-timeout",
                f"state={payload.get('state', 'unknown')} exit={proc.returncode}",
            )
        time.sleep(0.02)
