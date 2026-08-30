#!/usr/bin/env python3
"""Resolve a finite owner continuation budget from its bound route."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import route_identity as ROUTE_IDENTITY


COMPATIBILITY_FLOOR = 12
TERMINAL_RESERVE_DEFAULT = 1


@dataclass(frozen=True)
class ContinuationBudget:
    """SD-116 §13.34.4-(2): `limit` is always `ordinary + reserved`.

    Every existing call site in `resolve_continuation_budget()` below still
    passes what used to be the whole `limit` -- `__post_init__` reinterprets
    that value as `ordinary` (the gross-ceiling amount the pre-SD-116 code
    already computed) and raises `limit` by `reserved` on top of it, so
    `ordinary` never shrinks below the prior `limit` (D47-9) without editing
    every call site individually."""

    limit: int
    source: str
    declared_nodes: int = 0
    retry_slots: int = 0
    reserved: int = TERMINAL_RESERVE_DEFAULT
    ordinary: int = 0
    stall: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "ordinary", self.limit)
        object.__setattr__(self, "limit", self.limit + self.reserved)
        # Finite and route-derived when a route is bound; otherwise generous
        # enough that the pre-existing `max_identical_redeliveries` /
        # `max_join_reparks` bounds -- not this counter -- remain the actual
        # enforcement point for those patterns (G47 no-regression).
        object.__setattr__(self, "stall", max(COMPATIBILITY_FLOOR, self.retry_slots + 1))


def positive_continuation_limit(raw: str) -> int:
    """Argparse converter for an explicit, finite owner-launch override."""

    value = int(raw)
    if value <= 0:
        raise ValueError("continuation limit must be positive")
    return value


def resolve_continuation_budget(
    *,
    explicit: int | None = None,
    route_file: str | Path | None = None,
    route_id: str = "",
    route_hash: str = "",
    expected_cwd: str | Path | None = None,
) -> ContinuationBudget:
    """Return an explicit limit or a conservative route-derived default.

    The route is usable only when the supervisor receives the complete owner
    binding. Unreadable, malformed, or mismatched input keeps the historical
    finite floor; it never turns a supervisor into an unbounded loop.
    """

    if explicit is not None:
        if explicit <= 0:
            raise ValueError("continuation limit must be positive")
        return ContinuationBudget(explicit, "explicit-owner-override")
    if not route_file or not route_id or not route_hash:
        return ContinuationBudget(COMPATIBILITY_FLOOR, "compatibility-floor")
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return ContinuationBudget(COMPATIBILITY_FLOOR, "compatibility-floor")
    if not isinstance(route, dict) or route.get("schema_version") != 2:
        return ContinuationBudget(COMPATIBILITY_FLOOR, "compatibility-floor")
    if route.get("route_id") != route_id or route.get("route_hash") != route_hash:
        return ContinuationBudget(COMPATIBILITY_FLOOR, "compatibility-floor")
    sealed_hash = ROUTE_IDENTITY.route_hash(route)
    if route_hash != sealed_hash or route_id != ROUTE_IDENTITY.route_id_from_hash(sealed_hash):
        return ContinuationBudget(COMPATIBILITY_FLOOR, "compatibility-floor")
    if expected_cwd is not None:
        raw_cwd = route.get("cwd")
        if (
            not isinstance(raw_cwd, str)
            or not Path(raw_cwd).is_absolute()
            or Path(raw_cwd).resolve() != Path(expected_cwd).resolve()
        ):
            return ContinuationBudget(COMPATIBILITY_FLOOR, "compatibility-floor")
    # SD-116 WP4 (D47-9): block first, current derivation second, floor last.
    # The block was sealed into `route_hash` at compile time (WP1/WP4), so
    # its integrity is already proven by the hash checks above -- no extra
    # cross-check against `nodes`/`resume_retry_boundaries` is needed here.
    sealed_block = route.get("continuation_budget")
    if isinstance(sealed_block, dict) and sealed_block.get("contract_version") == 1:
        block_ordinary = sealed_block.get("ordinary")
        block_reserved = sealed_block.get("reserved")
        block_limit = sealed_block.get("limit")
        block_declared_nodes = sealed_block.get("declared_nodes")
        block_review_round_cap = sealed_block.get("review_round_cap")
        block_gap = sealed_block.get("gap")
        block_retry = sealed_block.get("retry")
        if (
            isinstance(block_ordinary, int) and not isinstance(block_ordinary, bool)
            and isinstance(block_reserved, int) and not isinstance(block_reserved, bool)
            and isinstance(block_limit, int) and not isinstance(block_limit, bool)
            and isinstance(block_declared_nodes, int) and not isinstance(block_declared_nodes, bool)
            # impl-review round 1 finding 2: the compiler seals
            # `review_round_cap`/`gap`/`retry` too (capability-route.py's
            # `continuation_budget` literal); a malformed value here must
            # degrade to the bound-route derivation exactly like a malformed
            # `ordinary`/`reserved`/`limit`/`declared_nodes`, not be accepted
            # as `sealed-block` unchecked.
            and isinstance(block_review_round_cap, int) and not isinstance(block_review_round_cap, bool)
            and isinstance(block_gap, int) and not isinstance(block_gap, bool)
            and isinstance(block_retry, int) and not isinstance(block_retry, bool)
            and block_ordinary >= COMPATIBILITY_FLOOR
            and block_reserved >= 1
            and block_limit == block_ordinary + block_reserved
            and block_declared_nodes >= 0
            and block_review_round_cap >= 1
            and block_gap >= 0
            and block_retry >= 0
        ):
            return ContinuationBudget(
                block_ordinary,
                "sealed-block",
                declared_nodes=block_declared_nodes,
                retry_slots=(
                    sealed_block.get("retry_slots")
                    if isinstance(sealed_block.get("retry_slots"), int)
                    and not isinstance(sealed_block.get("retry_slots"), bool)
                    else 0
                ),
                reserved=block_reserved,
            )
    nodes = route.get("nodes")
    boundaries = route.get("resume_retry_boundaries")
    if not isinstance(nodes, list) or not isinstance(boundaries, list):
        return ContinuationBudget(COMPATIBILITY_FLOOR, "compatibility-floor")
    node_ids = [
        node.get("id") for node in nodes
        if isinstance(node, dict) and isinstance(node.get("id"), str) and node.get("id")
    ]
    if len(node_ids) != len(nodes) or len(set(node_ids)) != len(node_ids):
        return ContinuationBudget(COMPATIBILITY_FLOOR, "compatibility-floor")
    retry_ids = [item for item in boundaries if isinstance(item, str) and item]
    if len(retry_ids) != len(boundaries) or not set(retry_ids).issubset(node_ids):
        return ContinuationBudget(COMPATIBILITY_FLOOR, "compatibility-floor")
    retry_slots = len(set(retry_ids))
    return ContinuationBudget(
        max(COMPATIBILITY_FLOOR, len(node_ids) + retry_slots),
        "bound-route",
        declared_nodes=len(node_ids),
        retry_slots=retry_slots,
    )


@dataclass(frozen=True)
class AdmitVerdict:
    admitted: bool
    purpose: str
    charged: str
    refusal: str
    gross_remaining: int
    stall_remaining: int
    reserved_remaining: int


class ContinuationLedger:
    """Pure state machine (SD-116 §13.34.4-(2)): zero file access. The three
    remainders never go negative via clamping -- every admit that would push
    one below zero is refused instead (D47-4)."""

    def __init__(self, budget: ContinuationBudget) -> None:
        self._gross_remaining = budget.ordinary
        self._stall_remaining = budget.stall
        self._reserved_remaining = budget.reserved

    @property
    def gross_remaining(self) -> int:
        return self._gross_remaining

    @property
    def stall_remaining(self) -> int:
        return self._stall_remaining

    @property
    def reserved_remaining(self) -> int:
        return self._reserved_remaining

    def _verdict(self, *, admitted, purpose, charged, refusal) -> AdmitVerdict:
        return AdmitVerdict(
            admitted=admitted, purpose=purpose, charged=charged, refusal=refusal,
            gross_remaining=self._gross_remaining,
            stall_remaining=self._stall_remaining,
            reserved_remaining=self._reserved_remaining,
        )

    def admit(self, *, purpose: str, stalled: bool, reservation_ok: bool) -> AdmitVerdict:
        if purpose not in ("ordinary", "terminal-handoff"):
            # `purpose` names an axis distinct from `class` (SD-116
            # §13.34.4-(2)): a caller that names the reserved *class* on this
            # axis is asking to spend the reserve outside the one sealed
            # terminal-handoff site, which is a scope violation, not merely
            # an unrecognized purpose.
            refusal = (
                "continuation-reserved-scope-violation" if purpose == "reserved"
                else "continuation-budget-unavailable"
            )
            return self._verdict(admitted=False, purpose=purpose, charged="", refusal=refusal)
        # D47-7: an unknown/stale/negative/mismatched budget state -- a
        # negative remainder (corrupted ledger) or a reservation the caller
        # could not atomically secure -- refuses uniformly with
        # `continuation-budget-unavailable`, distinct from an ordinary
        # exhaustion refusal below.
        if (
            self._gross_remaining < 0
            or self._stall_remaining < 0
            or self._reserved_remaining < 0
            or not reservation_ok
        ):
            return self._verdict(
                admitted=False, purpose=purpose, charged="",
                refusal="continuation-budget-unavailable",
            )
        if purpose == "terminal-handoff":
            if self._reserved_remaining > 0:
                self._reserved_remaining -= 1
                return self._verdict(admitted=True, purpose=purpose, charged="reserved", refusal="")
            return self._verdict(
                admitted=False, purpose=purpose, charged="", refusal="continuation-admission-refused",
            )
        # SD-116 R2: only the sealed completion-receipt consumption site may
        # ever request purpose="terminal-handoff"; every other admit() call
        # in this ledger's lifetime passes purpose="ordinary".
        if stalled:
            if self._stall_remaining > 0:
                self._stall_remaining -= 1
                return self._verdict(admitted=True, purpose=purpose, charged="stall", refusal="")
            return self._verdict(
                admitted=False, purpose=purpose, charged="", refusal="continuation-admission-refused",
            )
        # `stall_remaining` gates only the two no-progress patterns (identical
        # redelivery, runtime-wait-without-started-child) routed through the
        # `stalled=True` branch above -- it never blocks a real-progress
        # gross spend, per §13.34.4-(2) "이 두 계열은 gross ceiling을 소비하지
        # 않는다" (the inverse holds too: gross spends don't borrow from stall).
        if self._gross_remaining > self._reserved_remaining:
            self._gross_remaining -= 1
            return self._verdict(admitted=True, purpose=purpose, charged="gross", refusal="")
        return self._verdict(
            admitted=False, purpose=purpose, charged="", refusal="continuation-admission-refused",
        )
