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


# Every terminal delivery classification. `cancelled` is a settled parent close:
# a terminal end like `success`, never a failure to inspect.
TERMINAL_CLASSIFICATIONS = frozenset({"success", "attention", "cancelled"})


def completion_classification(children) -> str:
    classes = {child.get("delivery_classification") for child in children}
    if classes == {"success"}:
        return "success"
    if "cancelled" in classes and classes <= {"cancelled", "success"}:
        return "cancelled"
    return "attention"


def parent_close_child(child: dict) -> bool:
    """A child its parent's close ended, as `cancelled` or, before that existed, `success`."""
    return child.get("reason") == "cancelled-by-parent" and child.get("required_action") == "advance-completed"


def settled_parent_close(children) -> bool:
    """True when a receipt tells of its parent's settled close: at least one child the close
    ended, every other one a success committed before it. The route is closed, so none advances.

    The same judgment as `completion_classification`'s `cancelled`, also for the older form."""
    return bool(children) and any(parent_close_child(child) for child in children) and all(
        parent_close_child(child) or (child.get("delivery_classification") == "success"
                                      and child.get("required_action") == "advance-completed")
        for child in children)


def legacy_parent_close_receipt(receipt: dict) -> dict | None:
    """The settled parent close of ``receipt`` as releases before `cancelled` wrote it, else None.

    Those releases carried such a close as `success` with reason `cancelled-by-parent`, and
    render it as settled only when every child is one. A success committed before the close
    stays out of that form, as it is: the older receiver is told the close alone, once.
    Only an ended receipt carrying `cancelled` has this form."""
    children = receipt.get("children")
    if (not isinstance(children, list) or not children
            or not all(isinstance(child, dict) and child.get("status") == "done" for child in children)
            or not any(child.get("delivery_classification") == "cancelled" for child in children)
            or not settled_parent_close(children)):
        return None
    closed = [dict(child, delivery_classification="success") for child in children if parent_close_child(child)]
    return dict(receipt, children=closed, delivery_classification="success")


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
