"""One receipt identity contract for terminal writers, storage, and carriers."""

from __future__ import annotations

import hashlib
import json
import base64

CANONICAL_RECEIPT_KEYS = frozenset({
    "schema_version", "state", "parent_attempt_id", "job_registry", "children",
    "delivery_classification",
})
CANONICAL_CHILD_KEYS = frozenset({
    "attempt_id", "status", "readiness", "reason", "required_action", "harness",
    "delivery_classification",
})
NOTICE_KINDS = frozenset({"human-gate", "supervision"})


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
