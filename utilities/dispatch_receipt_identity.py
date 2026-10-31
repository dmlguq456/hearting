"""One receipt identity contract for terminal writers, storage, and carriers."""

from __future__ import annotations

import hashlib
import json
import base64

CANONICAL_RECEIPT_KEYS = frozenset({
    "schema_version", "state", "parent_attempt_id", "job_registry", "children",
    "delivery_classification", "replacement_lineage", "replacement_attention",
})
CANONICAL_CHILD_KEYS = frozenset({
    "attempt_id", "status", "readiness", "reason", "required_action", "harness",
    "delivery_classification",
    # Optional: the route plan's next leg, present only on a completed owner of an approved plan.
    # A receipt without it has exactly the bytes and digest it had before.
    "next_leg",
})
NOTICE_KINDS = frozenset({"human-gate", "supervision", "notice"})
COMPLETION_ACTIONS = frozenset({"complete-open", "inspect-done-failure", "advance-completed",
                                "finish-workflow", "inspect-recovery"})
# The two closure-blocked:<gate reason> literals mirror
# dispatch_terminal_commit._PROVEN_BLOCKED_GATE_REASONS exactly (item 8,
# unfinishable-watch) -- a proven-permanent owner_completion_state("blocked")
# gate is the only source of these reasons. `producer-finalize-refused` is a
# separate held refusal: a deterministic producer-finalize outcome whose
# recorded lineage evidence has not changed since, so the same watch also
# stops retrying it.
# SD-154 I-5: `completion-evidence-hash-mismatch` is `completion-evidence-
# revised-unrecorded` now (an evidence sha mismatch usually means `revise`
# can record it, not that anything is broken).
COMPLETION_REASONS = frozenset({"registry-closed", "registry-closed-marker", "terminal-observed",
                                "cancelled-by-parent",
                                "row-advanced", "terminal-failure-or-unclosed",
                                "closure-blocked:completion-attempt-not-current",
                                "closure-blocked:completion-evidence-revised-unrecorded",
                                "closure-blocked:producer-finalize-refused"})
JOIN_REASONS = COMPLETION_REASONS | {"process-alive", "process-unverifiable",
                                     "termination-pending",
                                     "terminal-commit-pending", "workflow-completion-pending"}


def unseal_receipt(encoded: str) -> dict:
    """Restore the writer's exact receipt; decoding grants no authority."""
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("delivery-receipt-invalid")
    try:
        value = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("delivery-receipt-invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("delivery-receipt-invalid")
    return value


def canonical_receipt(receipt: dict) -> dict:
    if not isinstance(receipt, dict):
        raise ValueError("delivery-receipt-invalid")
    if receipt.get("kind") in NOTICE_KINDS:
        # A notice's binding and requested decision are its identity. It must
        # never collide with the terminal completion of the same attempt.
        return dict(receipt)
    value = {key: item for key, item in receipt.items() if key in CANONICAL_RECEIPT_KEYS}
    children = receipt.get("children")
    if isinstance(children, list):
        value["children"] = [
            {key: item for key, item in child.items() if key in CANONICAL_CHILD_KEYS}
            for child in children if isinstance(child, dict)
        ]
    return value


def receipt_digest(receipt: dict) -> str:
    value = canonical_receipt(receipt)
    notice = receipt.get("kind") in NOTICE_KINDS
    encoded = json.dumps(value, ensure_ascii=not notice,
                         separators=(",", ":"), sort_keys=True).encode("utf-8")
    return ("sha256:" if notice else "") + hashlib.sha256(encoded).hexdigest()
