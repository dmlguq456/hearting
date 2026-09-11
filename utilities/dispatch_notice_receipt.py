"""Typed notice semantics over one existing pending-delivery transport.

Gate release and recovery inspection remain different decisions. Only their
claim, send, acceptance, and crash/no-resend mechanics are shared.
"""
from __future__ import annotations


def codec(receipt: dict):
    if isinstance(receipt, dict) and receipt.get("kind") == "supervision":
        import dispatch_supervision
        return dispatch_supervision
    import human_gate_receipt
    return human_gate_receipt


def is_notice(receipt: object) -> bool:
    return isinstance(receipt, dict) and receipt.get("kind") in {"human-gate", "supervision"}


def validate_pending_record(record: dict, **kwargs) -> dict:
    return codec(record.get("receipt")).validate_pending_record(record, **kwargs)


def gateway_delivery_id(receipt: dict) -> str:
    return codec(receipt).gateway_delivery_id(receipt)


def context(receipt: dict, delivery_id: str) -> dict:
    return codec(receipt).context(receipt, delivery_id)
