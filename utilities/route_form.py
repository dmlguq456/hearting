"""The route form: which routes one owner process runs alone, and which intensities each form may carry.

A route's *form* says who makes it -- `direct` (this session, inline), `unit` (one depth-0 leg of a
read-only unit, no owner), `solo` (one owner process, the `one-shot` node) or `staged`/`framed`
(stage nodes at depth 2). Its *intensity* says how many independent checks follow. The two are not the
same axis, so code that needs "does one owner run this route by itself" asks `one_shot_owner(route)`
here instead of reading the intensity.

The form is read from the record: the capability and `selection.shape`. A record with no usable shape
(an older one, or one whose shape and intensity disagree) falls back to the form its intensity has
always meant. `FORM_INTENSITIES` is the one table of what a form may carry; compose, the frame
proposal normalizer and the compiler all read it. Stdlib only, so every module can import it.
"""
from __future__ import annotations

INTENSITIES = frozenset(("direct", "quick", "standard", "strong", "thorough", "adversarial"))
UNIT_CAPABILITY = "unit"
# The one table of "which intensities may a route form carry". The first value is the form's default
# intensity. `unit` is the kind-`unit` route (one depth-0 leg, no owner); its recorded shape is `solo`.
# `framed` is the internal frame route: compose checks no intensity for it, so it reads like `staged`.
_STAGED = ("standard", "strong", "thorough", "adversarial")
FORM_INTENSITIES = {
    "direct": ("direct",),
    "unit": ("quick",),
    "solo": ("quick",),
    "staged": _STAGED,
    "framed": _STAGED,
}
SHAPE_INTENSITY = {shape: values[0] for shape, values in FORM_INTENSITIES.items() if shape != "unit"}
# A route record with no usable `selection.shape` (an older record) reads its form from its intensity.
INTENSITY_SHAPE = {"direct": "direct", "quick": "solo"}


def shape_for_intensity(effective):
    return INTENSITY_SHAPE.get(effective, "staged")


def intensity_mismatch(shape, requested):
    """The compose refusal for a shape asked to carry an intensity it does not allow, or None."""
    if shape in ("direct", "solo", "staged") and requested not in FORM_INTENSITIES[shape]:
        return f"compose-shape-intensity-mismatch:{shape} (allowed: {','.join(FORM_INTENSITIES[shape])})"
    return None


def form_of(capability, effective, shape=None):
    """The route form (`direct`, `unit`, `solo`, `staged`, `framed`) a capability, intensity and shape make.

    A declared shape counts when its intensity is one that form allows; otherwise (an older record with
    no shape, or one whose shape and intensity disagree) the form follows the intensity as it always did."""
    if capability == UNIT_CAPABILITY and effective in FORM_INTENSITIES["unit"]:
        return "unit"
    if shape in FORM_INTENSITIES and shape != "unit" and effective in FORM_INTENSITIES[shape]:
        return shape
    return shape_for_intensity(effective)


def route_form(route):
    """A route record's form, read from its capability and `selection.shape`."""
    if not isinstance(route, dict):
        return None
    selection = route.get("selection")
    return form_of(route.get("capability"), route.get("effective_intensity"),
                   selection.get("shape") if isinstance(selection, dict) else None)


def serial_attempt_form(form):
    """The forms whose attempts run serially from a registered-headless candidate list: a one-shot owner
    (alone, with a frame pair, or behind a resource run) and the unit route's one leg."""
    return form in ("solo", "unit")


def staged_form(form):
    """The forms run by stage nodes at depth 2 from checked dispatch evidence."""
    return form in ("staged", "framed")


def one_shot_owner(route):
    """Whether one owner process (the `one-shot` node) does this route's work by itself."""
    return route_form(route) == "solo"


def serial_attempt_route(route):
    """Whether this record's attempts are serial candidate-list attempts: a one-shot owner route or a unit route."""
    return serial_attempt_form(route_form(route))
