"""User vocabulary shared by Fleet's group and process views; no state judgments."""
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from .model import project_of


def label(value):
    return str(value) if value else ""


def project(cwd):
    return project_of(cwd) if cwd else "project unknown"


def gpu_owner(label):
    """Execution IDs repeat the run identity; person/session labels add context."""
    return "" if str(label).startswith(("run:", "job:")) else label


def codex_credits(payload):
    """Optional account annotation; flags, rather than balance, decide usability."""
    if not isinstance(payload, Mapping) or "credits" not in payload:
        return None
    from .collectors.codex import credits_usable

    credits = payload["credits"]
    if payload.get("spend_control_reached") is True:
        return "credits spend limit"
    if not credits_usable(payload):
        return ("credits none" if isinstance(credits, Mapping)
                and credits.get("has_credits") is False
                and credits.get("unlimited") is not True else "credits ?")
    if credits.get("unlimited") is True:
        return "credits unlimited"
    balance = credits.get("balance")
    if isinstance(balance, (str, int, float)) and not isinstance(balance, bool):
        try:
            amount = Decimal(str(balance))
            if amount.is_finite() and 0 <= amount < 10**15:
                return f"credits {int(amount):,}"
        except InvalidOperation:
            pass
    return "credits available"
