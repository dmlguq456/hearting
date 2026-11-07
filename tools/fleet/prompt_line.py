"""Shared presentation of an already-decided human prompt frontier."""
from collections.abc import Mapping
import math
from .config import ui_text


def last_input(display, now):
    display = display if isinstance(display, Mapping) else {}
    human = display.get("human")
    if not isinstance(human, Mapping):
        return ui_text("fleet.input.none" if display.get("human_state") == "none"
                       else "fleet.input.unknown")
    at = human.get("at")
    if now is None or type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at <= now:
        age = "?"
    else:
        minutes = int((now - at) // 60)
        age = f"{minutes // 1440}d" if minutes >= 1440 else f"{minutes // 60}h" if minutes >= 60 else f"{minutes}m"
    return ui_text("fleet.input.value", age=age, text=human.get("text") or "·")
