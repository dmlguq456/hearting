"""Dispatch contract: attempt metadata validation and the headless policy.

Split out of `dispatch_contract.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from route_authority import depth1_supported_harnesses, route_in_force

from dispatch_contract_base import (
    ATTEMPT_SCHEMA_VERSION, DispatchContractError, EXECUTION_SURFACES, FALLBACK_HOPS,
    SESSION_CHAIN_ID_RE, SUBSESSION_ID_RE, WRAPPER_TRANSPORTS,
)


def _registered_worker(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"1", "true"}:
        return True
    if str(value).lower() in {"0", "false"}:
        return False
    raise DispatchContractError("invalid-registered-worker", str(value))


def validate_attempt_metadata(
    metadata: dict[str, object],
    *,
    registered_headless_wrapper: bool = False,
) -> None:
    """Validate independent v20 attempt axes before claim, spawn, or completion."""

    try:
        schema_version = int(metadata.get("attempt_schema_version", 0))
        dispatch_depth = int(metadata.get("dispatch_depth", -1))
    except (TypeError, ValueError) as exc:
        raise DispatchContractError("invalid-attempt-metadata", str(exc)) from exc
    retry_of = metadata.get("automatic_retry_of")
    if retry_of is not None and not re.fullmatch(r"att-[A-Za-z0-9._-]{1,240}", str(retry_of)):
        raise DispatchContractError("retry-predecessor-invalid", str(retry_of))
    if schema_version != ATTEMPT_SCHEMA_VERSION:
        raise DispatchContractError(
            "legacy-attempt-row-read-only",
            f"attempt schema v{schema_version or 1} cannot be claimed or completed",
        )
    if any(key in metadata for key in ("depth", "owner_depth", "max_depth")):
        raise DispatchContractError(
            "bare-dispatch-depth-field",
            "current attempt metadata accepts dispatch_depth only",
        )
    if dispatch_depth not in {0, 1, 2}:
        raise DispatchContractError("invalid-dispatch-depth", str(dispatch_depth))

    transport = str(metadata.get("transport", ""))
    surface = str(metadata.get("execution_surface", ""))
    fallback_hop = str(metadata.get("fallback_hop", ""))
    registered = _registered_worker(metadata.get("registered_worker"))
    if transport not in WRAPPER_TRANSPORTS:
        raise DispatchContractError("invalid-transport", transport)
    if surface not in EXECUTION_SURFACES:
        raise DispatchContractError("invalid-execution-surface", surface)
    if fallback_hop not in FALLBACK_HOPS and not (
        dispatch_depth == 0 and fallback_hop == ""
    ):
        raise DispatchContractError("invalid-fallback-hop", fallback_hop)
    if dispatch_depth == 0 and (
        surface != "inline"
        or registered
        or transport != "interactive"
        or fallback_hop
    ):
        raise DispatchContractError("direct-attempt-axes-mismatch", surface)
    if surface == "claude-agent-team-teammate":
        raise DispatchContractError(
            "teammate-not-dispatch-attempt",
            "Claude agent-team teammates carry peer-session lifecycle, not dispatch depth",
        )
    if registered != (surface == "registered-headless"):
        raise DispatchContractError("attempt-registration-surface-mismatch", surface)
    if registered and transport != "headless":
        raise DispatchContractError("registered-worker-transport-mismatch", transport)
    if surface == "registered-headless" and fallback_hop not in {
        "same-harness-headless",
        "cross-harness-headless",
    }:
        raise DispatchContractError("registered-worker-fallback-mismatch", fallback_hop)
    native_surfaces = {"codex-native-subagent", "claude-subagent"}
    if surface in native_surfaces and (
        fallback_hop != "native-subagent" or transport != "headless"
    ):
        raise DispatchContractError(
            "native-surface-axes-mismatch",
            f"transport={transport},fallback_hop={fallback_hop}",
        )
    if surface == "inline" and dispatch_depth > 0 and fallback_hop != "inline":
        raise DispatchContractError("inline-surface-fallback-mismatch", fallback_hop)
    if registered_headless_wrapper and (surface != "registered-headless" or not registered):
        raise DispatchContractError("headless-wrapper-surface-mismatch", surface)

    # A route stage owns one semantic gate; optional sub-sessions are only
    # execution-capacity attempts below that stage. Legacy/current ordinary
    # attempts omit these fields and remain stage-authoritative.
    subsession_id = str(metadata.get("subsession_id", ""))
    raw_authority = metadata.get("stage_authority", "1")
    stage_authority = _registered_worker(raw_authority)
    if subsession_id:
        if not SUBSESSION_ID_RE.fullmatch(subsession_id):
            raise DispatchContractError("subsession-id-invalid", subsession_id)
        if stage_authority:
            raise DispatchContractError("subsession-stage-authority-forbidden", subsession_id)
        if dispatch_depth != 2:
            raise DispatchContractError("subsession-depth-invalid", str(dispatch_depth))
        required = (
            "route_id", "route_node", "session_chain_id", "subsession_index",
            "subsession_count", "subsession_mode", "subsession_purpose",
            "phase_brief", "phase_brief_sha256", "state_ledger", "fixed_files_sha256",
            "narrow_verify_sha256", "expected_round_trips",
        )
        missing = [key for key in required if not str(metadata.get(key, ""))]
        if missing:
            raise DispatchContractError("subsession-metadata-missing", ",".join(missing))
        chain_id = str(metadata["session_chain_id"])
        if not SESSION_CHAIN_ID_RE.fullmatch(chain_id):
            raise DispatchContractError("session-chain-id-invalid", chain_id)
        mode = str(metadata["subsession_mode"])
        if mode not in {"serial", "parallel"}:
            raise DispatchContractError("subsession-mode-invalid", mode)
        purpose = str(metadata["subsession_purpose"])
        if purpose not in {"planned", "gap-retry"}:
            raise DispatchContractError("subsession-purpose-invalid", purpose)
        try:
            index = int(str(metadata["subsession_index"]))
            count = int(str(metadata["subsession_count"]))
            rounds = int(str(metadata["expected_round_trips"]))
        except ValueError as exc:
            raise DispatchContractError("subsession-number-invalid", str(exc)) from exc
        if not 1 <= index <= count <= 16 or not 1 <= rounds <= 20:
            raise DispatchContractError(
                "subsession-number-out-of-range", f"index={index},count={count},rounds={rounds}"
            )
        if purpose == "planned" and str(metadata.get("capacity_retry", "0")) == "1":
            raise DispatchContractError(
                "planned-subsession-retry-conflation", subsession_id
            )
        for key in (
            "phase_brief_sha256", "fixed_files_sha256", "narrow_verify_sha256"
        ):
            value = str(metadata[key])
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise DispatchContractError("subsession-digest-invalid", f"{key}={value}")
        for key in ("phase_brief", "state_ledger"):
            if not Path(str(metadata[key])).is_absolute():
                raise DispatchContractError(
                    "subsession-path-not-absolute", f"{key}={metadata[key]}"
                )
        if mode == "parallel" and not (
            metadata.get("parallel_group") or metadata.get("batch_group")
        ):
            raise DispatchContractError("parallel-subsession-batch-required", subsession_id)
    elif not stage_authority:
        raise DispatchContractError(
            "stage-authority-zero-without-subsession", str(metadata.get("route_node", ""))
        )


def headless_attempt_policy(
    *,
    route_file: str | None,
    route_node: str | None,
    intensity: str,
    harness: str,
    dispatch_depth: int,
    parent_slug: str | None,
    execution_surface: str,
    registered_worker: bool,
    fallback_hop: str | None,
    fallback_ordinal: int,
    parent_harness: str,
    parent_transport: str,
    parent_sandbox: str,
    launch_authority: str,
) -> dict[str, object]:
    """Bind one registered wrapper invocation to its immutable route axes."""

    effective_hop = fallback_hop or {
        1: "same-harness-headless",
        2: "cross-harness-headless",
    }.get(fallback_ordinal, "same-harness-headless")
    metadata: dict[str, object] = {
        "attempt_schema_version": ATTEMPT_SCHEMA_VERSION,
        "dispatch_depth": dispatch_depth,
        "transport": "headless",
        "execution_surface": execution_surface,
        "registered_worker": registered_worker,
        "fallback_hop": effective_hop,
    }
    validate_attempt_metadata(metadata, registered_headless_wrapper=True)
    policy: dict[str, object] = {
        "fallback_hop": effective_hop,
        "fallback_ordinal": fallback_ordinal,
        "quick": False,
        "terminal_attempt_limit": None,
        "replacement_attempt_limit": 0,
        "replacement_notes": frozenset(),
    }

    if not route_file:
        if intensity == "direct":
            raise DispatchContractError("direct-main-inline-only", "direct routes do not register workers")
        if intensity == "quick":
            raise DispatchContractError(
                "quick-headless-unavailable",
                "quick dispatch requires a current immutable route",
            )
        return policy
    try:
        # The sealed route with its parent's pin changes (`route_authority.route_in_force`).
        route = route_in_force(json.loads(Path(route_file).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise DispatchContractError("route-record-unreadable", str(exc)) from exc
    if route.get("schema_version") != 2:
        raise DispatchContractError(
            "legacy-route-read-only",
            f"route schema v{route.get('schema_version', 1)} cannot register or start workers",
        )
    node = next((row for row in route.get("nodes", []) if row.get("id") == route_node), None)
    if node is None:
        raise DispatchContractError("route-node-unknown", str(route_node))
    if route.get("effective_intensity") == "direct":
        raise DispatchContractError("direct-main-inline-only", "direct routes do not register workers")
    if int(node.get("dispatch_depth", -1)) != dispatch_depth:
        raise DispatchContractError("route-dispatch-depth-mismatch", str(node.get("dispatch_depth")))

    if route.get("effective_intensity") == "quick":
        # Quick is a THREE-node route now (`frame`, `frame-alternative`,
        # `one-shot`), so the shape check is a per-node-id table rather than a
        # pin to a single id. Depth 1 and "no parent slug" still hold for all
        # three; what differs is the tuple each id must carry.
        expected_tuple = {
            "one-shot": ("owner", "_kernel/owner"),
            "frame": ("frame", "plan/frame"),
            "frame-alternative": ("frame", "plan/frame"),
        }.get(str(route_node))
        if dispatch_depth != 1 or parent_slug or expected_tuple is None:
            raise DispatchContractError("quick-route-shape-invalid", str(route_node))
        if (node.get("worker_type"), node.get("unit")) != expected_tuple:
            raise DispatchContractError("quick-route-shape-invalid", str(route_node))
        if node.get("execution_surface") != "registered-headless" or node.get("registered_worker") is not True:
            raise DispatchContractError("quick-route-surface-invalid", str(node.get("execution_surface")))
        # The same-harness pin exists to keep quick's WORK pinned to one
        # harness, so its (nonexistent) fallback budget and artifact lineage do
        # not fragment. That reason applies to `one-shot` alone; the frame legs
        # are an advisory pair ahead of the work. SD-160 permits either
        # harness hop; their separate attempts and frame personas establish
        # independence, not the number of selected harnesses.
        if route_node == "one-shot" and effective_hop != "same-harness-headless":
            raise DispatchContractError("quick-fallback-forbidden", effective_hop)
        if route_node != "one-shot" and effective_hop not in (
            "same-harness-headless", "cross-harness-headless"
        ):
            raise DispatchContractError("quick-fallback-forbidden", effective_hop)
        # Unchanged below on purpose: candidate filtering and
        # `terminal_attempt_limit` are per-(route_id, route_node) serial-attempt
        # budgets, so three nodes each get their own budget from the same
        # candidate list -- exactly right without modification. The early
        # `return` keeps quick out of the `fallback_hops`-required check, which
        # no depth-1 node can ever satisfy.
        candidates = [
            row
            for row in route.get("registered_headless_candidates") or []
            if row.get("status") == "supported"
            and row.get("harness") == harness
            and row.get("transport") == "headless"
            and row.get("surface") == "registered-headless"
        ]
        if not candidates:
            raise DispatchContractError("quick-headless-unavailable", harness)
        policy.update(
            quick=True,
            terminal_attempt_limit=len(candidates),
            replacement_attempt_limit=1,
            replacement_notes=frozenset({"dead-protocol", "dead-permission-reject"}),
        )
        return policy

    # N1: a standard+ frame leg is depth 1 and carries NO `fallback_hops`, by
    # construction -- the compiler attaches a checked chain only to
    # dispatch_depth == 2 nodes. Without this early return every standard+
    # frame registration dies at `route-fallback-hops-missing` while compile and
    # route-verify both pass: precisely the late-failure class this cycle
    # exists to remove.
    #
    # Why an early return instead of teaching the compiler to attach a chain to
    # depth-1 nodes: `_fallback_chain` matching keys on `launch_authority`, and
    # LAUNCH_AUTHORITIES holds only {"conductor", "ancestor-broker"}. A depth-1
    # frame leg is launched under `launch_authority: "depth-0"`, a DIFFERENT
    # axis; making a chain match it would mean adding a new enum value that
    # batch, chain and manifest code all read. That is a far wider and riskier
    # change than one guarded return, and it was rejected deliberately.
    if (
        int(node.get("dispatch_depth", -1)) == 1
        and node.get("worker_type") == "frame"
        and node.get("unit") == "plan/frame"
    ):
        if execution_surface != "registered-headless" or registered_worker is not True:
            raise DispatchContractError("frame-route-surface-invalid", str(execution_surface))
        if effective_hop not in {"same-harness-headless", "cross-harness-headless"}:
            raise DispatchContractError("frame-route-surface-invalid", effective_hop)
        # Standard+ HAS checked dispatch evidence (quick does not), so the
        # requested harness is checked against it rather than taken on trust.
        if harness not in depth1_supported_harnesses(route, worker_type="frame"):
            raise DispatchContractError("frame-harness-unsupported", str(harness))
        policy.update(fallback_hop=effective_hop)
        return policy

    chain = node.get("fallback_hops")
    if not isinstance(chain, list):
        raise DispatchContractError("route-fallback-hops-missing", str(route_node))
    expected_candidate = {
        "parent_harness": parent_harness,
        "parent_transport": parent_transport,
        "parent_sandbox": parent_sandbox,
        "child_harness": harness,
        "launch_authority": launch_authority,
        "status": "supported",
    }

    def candidate_matches(candidate: object) -> bool:
        return isinstance(candidate, dict) and all(
            candidate.get(key) == value for key, value in expected_candidate.items()
        )

    selected = None
    if fallback_ordinal == 0:
        selected = next(
            (
                row
                for row in chain
                if any(candidate_matches(candidate) for candidate in row.get("candidates", []))
            ),
            None,
        )
        if selected is not None:
            fallback_ordinal = int(selected["ordinal"])
            effective_hop = str(selected["fallback_hop"])
            policy.update(fallback_ordinal=fallback_ordinal, fallback_hop=effective_hop)
    else:
        selected = next(
            (row for row in chain if int(row.get("ordinal", 0)) == fallback_ordinal),
            None,
        )
    if selected is None or selected.get("fallback_hop") != effective_hop:
        raise DispatchContractError("route-fallback-hop-mismatch", effective_hop)
    if not any(candidate_matches(candidate) for candidate in selected.get("candidates", [])):
        raise DispatchContractError(
            "route-fallback-candidate-mismatch",
            json.dumps(expected_candidate, sort_keys=True),
        )
    if effective_hop not in {"same-harness-headless", "cross-harness-headless"}:
        raise DispatchContractError("headless-wrapper-fallback-mismatch", effective_hop)
    return policy
