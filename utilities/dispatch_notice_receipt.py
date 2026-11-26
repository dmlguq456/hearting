"""Typed notice semantics over one existing pending-delivery transport.

Gate release and recovery inspection remain different decisions. Only their
claim, send, acceptance, and crash/no-resend mechanics are shared.
"""
from __future__ import annotations


def codec(receipt: dict):
    if isinstance(receipt, dict) and receipt.get("kind") == "supervision":
        import dispatch_supervision
        return dispatch_supervision
    if isinstance(receipt, dict) and receipt.get("kind") == "notice":
        import session_notice
        return session_notice
    import human_gate_receipt
    return human_gate_receipt


def is_notice(receipt: object) -> bool:
    return isinstance(receipt, dict) and receipt.get("kind") in {"human-gate", "supervision", "notice"}


def validate_pending_record(record: dict, **kwargs) -> dict:
    return codec(record.get("receipt")).validate_pending_record(record, **kwargs)


def gateway_delivery_id(receipt: dict) -> str:
    return codec(receipt).gateway_delivery_id(receipt)


def context(receipt: dict, delivery_id: str) -> dict:
    return codec(receipt).context(receipt, delivery_id)


class NativeTextError(ValueError):
    """The notice codec's native context does not name exactly one text value."""


def native_text(receipt: dict) -> str:
    """The text a native queue carries for one notice, before its receipt marker.

    One rendering for the producer and for a reader that asks whether a prompt is
    exactly that notice; it depends only on the stored receipt.
    """
    if receipt.get("kind") == "notice":
        import session_notice
        return session_notice.render_text(receipt)
    additional = context(receipt, gateway_delivery_id(receipt)).get("additionalContext")
    values = [value.get("value") for value in additional.values()
              if isinstance(value, dict) and isinstance(value.get("value"), str)] \
        if isinstance(additional, dict) else []
    if len(values) != 1:
        raise NativeTextError("native-queue-notice-context-invalid")
    return values[0]
