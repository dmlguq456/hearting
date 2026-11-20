"""The route form: which routes one owner process runs alone, and which intensities each form may carry.

A route's *form* says who makes it -- `direct` (this session, inline), `unit` (one depth-0 leg of a
read-only unit, no owner), `solo` (one owner process, the `one-shot` node) or `staged`/`framed`
(stage nodes at depth 2). Its *intensity* says how many independent checks follow; a `unit` route has none
(one unit does one job and nothing checks it; its record keeps `quick` as `UNIT_RECORDED_INTENSITY`). The two are not the
same axis, so code that needs "does one owner run this route by itself" asks `one_shot_owner(route)`
here instead of reading the intensity.

Two questions are told apart, and both are read from the route's **nodes**, never from the intensity or
the recorded shape (a `solo` shape with a signal still seals a team route, so the shape cannot say):

- **A -- does one owner process make this route itself?** `one_shot_owner(route)`: a depth-1
  `_kernel/owner` node `one-shot` exists. Inline gates, wide write scope, the candidate-list start and
  the node-bound owner bundle hang on it.
- **B -- does the owner start depth-2 nodes?** `starts_depth2_nodes(route)`: a depth-2 node exists (or
  the framed route's runtime terminal). Checked dispatch evidence, the fallback chain,
  `max_dispatch_depth 2` and the owner's wait on its children hang on it.

In today's routes the two answers move together (quick: A only; team: B only). The middle cell
`solo·standard` -- one owner that makes the work and starts its kind's one declared review node -- is
the route where both are true.

`form_of` / `route_form` (the recorded capability, shape and intensity) say what to *build* at the
compile entrance and name a record for display; the middle cell is the one `solo` intensity a kind
must declare (`one_shot_review` in `capabilities/topologies.json`). `FORM_INTENSITIES` is the one table
of what a form may carry; compose, the frame proposal normalizer and the compiler all read it. Stdlib
only, so every module can import it.
"""
from __future__ import annotations

INTENSITY_ORDER = ("direct", "quick", "standard", "strong", "thorough", "adversarial")
INTENSITIES = frozenset(INTENSITY_ORDER)
UNIT_CAPABILITY = "unit"
DECISION_KIND = "runtime-terminal"  # the framed route's decision terminal (`capability_topology.ROUTE_DECISION_KIND`)
# A unit route has no intensity: one unit does one job and nothing checks it, so nobody picks a value for it.
# Its record still carries `quick` (`requested_intensity`, `effective_intensity`) as a compatibility value, because
# the review-round cap, the continuation budget, the worker's row and Fleet read that field and an old record has it.
# This is the name of that recorded value; it is what the route is recognized by, never something a person chooses.
UNIT_RECORDED_INTENSITY = "quick"
# The one table of "which intensities may a route form carry". The first value is the form's default
# intensity. `unit` is the kind-`unit` route (one depth-0 leg, no owner); its recorded shape is `solo` and it is
# listed here only for the value its record carries (`UNIT_RECORDED_INTENSITY`), not as an intensity to choose.
# `framed` is the internal frame route: compose checks no intensity for it, so it reads like `staged`.
# `solo` carries `standard` only where its kind declares a one-shot review (`allowed_intensities`).
_STAGED = ("standard", "strong", "thorough", "adversarial")
FORM_INTENSITIES = {
    "direct": ("direct",),
    "unit": (UNIT_RECORDED_INTENSITY,),
    "solo": ("quick", "standard"),
    "staged": _STAGED,
    "framed": _STAGED,
}
SHAPE_INTENSITY = {shape: values[0] for shape, values in FORM_INTENSITIES.items() if shape != "unit"}
# A route record with no usable `selection.shape` (an older record) reads its form from its intensity.
INTENSITY_SHAPE = {"direct": "direct", "quick": "solo"}


def shape_for_intensity(effective):
    return INTENSITY_SHAPE.get(effective, "staged")


def allowed_intensities(form, review_declared=False):
    """The intensities `form` may carry; `solo` carries `standard` only for a kind that declares a one-shot review."""
    values = FORM_INTENSITIES[form]
    if form == "solo" and not review_declared:
        return tuple(value for value in values if value != "standard")
    return values


def nearest_allowed(intensity, allowed):
    """The highest intensity `allowed` holds that is at or below `intensity`, or None when it holds none."""
    below = [value for value in allowed if INTENSITY_ORDER.index(value) <= INTENSITY_ORDER.index(intensity)]
    return max(below, key=INTENSITY_ORDER.index) if below else None


def intensity_mismatch(shape, requested, review_declared=False):
    """The compose refusal for a shape asked to carry an intensity it does not allow, or None."""
    if shape in ("direct", "solo", "staged"):
        allowed = allowed_intensities(shape, review_declared)
        if requested not in allowed:
            return f"compose-shape-intensity-mismatch:{shape} (allowed: {','.join(allowed)})"
    return None


def form_of(capability, effective, shape=None, review_declared=False):
    """The route form (`direct`, `unit`, `solo`, `staged`, `framed`) a capability, intensity and shape make.

    This names what the compile entrance builds. A declared shape counts when its intensity is one that
    form allows (`solo` + `standard` only where the kind declares the one-shot review); otherwise (an
    older record with no shape, or one whose shape and intensity disagree) the form follows the
    intensity as it always did. A *sealed* route is read by its nodes (`one_shot_owner`,
    `starts_depth2_nodes`), not by this."""
    if capability == UNIT_CAPABILITY and effective == UNIT_RECORDED_INTENSITY:
        return "unit"
    if shape in FORM_INTENSITIES and shape != "unit" and effective in allowed_intensities(shape, review_declared):
        return shape
    return shape_for_intensity(effective)


def _owner_node(node):
    """A depth-1 `_kernel/owner` node `one-shot`. The id `one-shot` is the owner's own, so a node that
    states no unit or depth (a fixture's shorthand) is that owner; one that states another is not."""
    return (isinstance(node, dict) and node.get("id") == "one-shot"
            and node.get("unit", "_kernel/owner") == "_kernel/owner" and node.get("dispatch_depth", 1) == 1)


def _node_list(route):
    nodes = route.get("nodes") if isinstance(route, dict) else None
    return nodes if isinstance(nodes, list) else None


def _record_form(route):
    """A record with no node list reads its form from its capability, shape and intensity as `form_of` does
    for a kind that declares no review (the answer every record of that kind has always had)."""
    selection = route.get("selection")
    return form_of(route.get("capability"), route.get("effective_intensity"),
                   selection.get("shape") if isinstance(selection, dict) else None)


def route_form(route):
    """A route record's form label, from its capability, `selection.shape` and nodes.

    A recorded `solo` at `standard` is the middle cell only when its nodes say one owner makes it; the
    team route that `compose --shape solo --signal ...` seals (shape `solo`, team nodes) reads `staged`."""
    if not isinstance(route, dict):
        return None
    selection = route.get("selection")
    form = form_of(route.get("capability"), route.get("effective_intensity"),
                   selection.get("shape") if isinstance(selection, dict) else None, review_declared=True)
    if form == "solo" and route.get("effective_intensity") != "quick" and not one_shot_owner(route):
        return shape_for_intensity(route.get("effective_intensity"))
    return form


def serial_attempt_form(form):
    """The forms whose attempts run serially from a registered-headless candidate list: a one-shot owner
    (alone, with a frame pair, or behind a resource run) and the unit route's one leg."""
    return form in ("solo", "unit")


def staged_form(form):
    """The forms run by stage nodes at depth 2 from checked dispatch evidence."""
    return form in ("staged", "framed")


def one_shot_owner(route):
    """A: whether one owner process (the `one-shot` node) makes this route's work itself.

    Read from the nodes: a depth-1 `_kernel/owner` node `one-shot`. True for solo·quick, a preset quick's
    frame pair, a resource resume and the middle cell `solo·standard`; false for a unit route, direct, and
    every team route (including the one `solo` + a signal seals). A record with no node list falls back to
    the form its capability, shape and intensity name."""
    if not isinstance(route, dict):
        return False
    nodes = _node_list(route)
    if nodes is None:
        return _record_form(route) == "solo"
    return any(_owner_node(node) for node in nodes)


def unit_route(route):
    """Whether this is the unit route: one depth-0 read-only leg and no owner."""
    return (isinstance(route, dict) and route.get("capability") == UNIT_CAPABILITY
            and route.get("effective_intensity") == UNIT_RECORDED_INTENSITY)


def serial_attempt_route(route):
    """Whether this record's attempts are serial candidate-list attempts: a one-shot owner route or a unit route."""
    return one_shot_owner(route) or unit_route(route)


def starts_depth2_nodes(route):
    """B: whether the owner starts depth-2 nodes (the route needs checked dispatch evidence and a
    fallback chain, and its owner waits on children): a depth-2 node, or the framed route's runtime
    terminal. True for every team route and for the middle cell; false for quick, direct, unit and
    the owner-only staged routes. A record with no node list falls back to its form."""
    if not isinstance(route, dict):
        return False
    nodes = _node_list(route)
    if nodes is None:
        return staged_form(_record_form(route))
    return any(isinstance(node, dict) and (node.get("dispatch_depth") == 2 or node.get("kind") == DECISION_KIND)
               for node in nodes)


def alone_without_stages(route):
    """Whether one process finishes this route with no depth-2 stage to wait for: a direct route, a unit
    route, or an owner that makes the work and starts nothing (`solo·quick`, a resource resume). The middle
    cell is not one of these -- it ends only on its review's marker."""
    return (isinstance(route, dict) and not starts_depth2_nodes(route)
            and (route_form(route) == "direct" or unit_route(route) or one_shot_owner(route)))


def one_shot_review_route(route):
    """The middle cell `solo·standard`: one owner makes the work (A) and starts its declared review (B)."""
    return one_shot_owner(route) and starts_depth2_nodes(route)


def one_shot_review_node(route):
    """The middle cell's review node (the depth-2 node the `one-shot` end node depends on), or None."""
    if not one_shot_review_route(route):
        return None
    return next((node for node in _node_list(route) if isinstance(node, dict) and node.get("dispatch_depth") == 2), None)
