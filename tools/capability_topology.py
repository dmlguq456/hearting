#!/usr/bin/env python3
"""Validate and query the portable capability execution-topology registry."""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import hashlib
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import EXECUTION_SURFACES, FALLBACK_HOPS, WRAPPER_TRANSPORTS  # noqa: E402
from artifact_context import SEMANTIC_OUTPUTS, is_semantic_output  # noqa: E402

import model_profile as PROFILE
from owner_write_advisory import worktree_mutating_scope  # noqa: E402

REGISTRY = ROOT / "capabilities" / "topologies.json"
MANIFEST = ROOT / "harness-manifest.json"
UNITS = ROOT / "roles" / "units"
UNIT_REF_RE = re.compile(r"^[a-z-]+/[a-z-]+$")
PARALLEL_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# G6 / AC 21: recorded, non-silent exception to "no parallel group on a
# terminal node" -- keep in sync with capability-route.py's
# `_TERMINAL_PARALLEL_GROUP_GRANDFATHER`.
TERMINAL_PARALLEL_GROUP_GRANDFATHER = {("autopilot-research", "claim-verify")}
# SD-110 13.32.1-(0): sealed closed vocabulary for the model-required reason a
# node carries when advance_class is model-required. Do not infer defaults.
MODEL_REQUIRED_REASONS = {
    "authored-brief",
    "arbiter-or-merge",
    "conditional-extension",
    "terminal-report",
    "commit-boundary",
    "parallel-group-member",
    "operator-pinned",
}
# D9 / AC 5: an auxiliary leg's verdict enum may not carry any unconditionally
# blocking token. `findings` is deliberately absent because the five auxiliary
# units use it as their non-blocking finding carrier.
_BLOCKING_VERDICT_TOKENS = frozenset({
    "issues", "changes-required", "blocked", "BLOCKED", "fail", "FAIL",
    "failed", "FAILED", "needs_work", "killed", "conflicts-found",
})
_UNIT_CACHE: dict = {}
_UNIT_CONTRACT_VIEW: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "unit_contract_view", default=None)


class TopologyError(ValueError):
    pass


def load_registry(path: Path | str = REGISTRY):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def canonical_registry_bytes(registry):
    return json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def registry_digest(registry):
    return "sha256:" + hashlib.sha256(canonical_registry_bytes(registry)).hexdigest()


def _string_values(node, out):
    if isinstance(node, str):
        out.add(node)
    elif isinstance(node, dict):
        for value in node.values():
            _string_values(value, out)
    elif isinstance(node, list):
        for value in node:
            _string_values(value, out)


def capability_registry_digest(registry, capability, extra_recipes=()):
    """Digest of only the registry parts a route of `capability` derives from.

    Kept: every top-level section except the two per-name tables, so a change
    to any shared rule (intensities, profiles, workflow states, parallel
    policy, ...) still changes it; the `recipes` rows of this capability (and
    of the base capability a composed recipe names); and the
    `completion_gate_contracts` rows those recipes reference. Recipes and gate
    contracts of other capabilities are left out, so adding or editing an
    unrelated capability does not make this capability's routes stale.
    """
    recipes = list(extra_recipes)
    names = {capability}
    used_parts = set()
    for recipe in recipes:
        meta = recipe.get("compose") if isinstance(recipe, dict) else None
        base = meta.get("base_capability") if isinstance(meta, dict) else None
        if isinstance(base, str):
            names.add(base)
        if isinstance(meta, dict) and isinstance(meta.get("parts"), list):
            used_parts.update(part for part in meta["parts"] if isinstance(part, str))
    # SD-165: a borrowed part's origin recipe is part of what the route derives from.
    names.update(part.partition(":")[0] for part in used_parts)
    recipes = [r for r in registry.get("recipes", []) if r.get("capability") in names] + recipes
    referenced = set()
    _string_values(recipes, referenced)
    contracts = registry.get("completion_gate_contracts") or {}
    # The part catalog is never part of the shared projection: a route that uses
    # no catalog data keeps the digest it had before the catalog existed. Only
    # the rows a composed recipe actually used (`compose.parts`) are added.
    projection = {k: v for k, v in registry.items()
                  if k not in ("recipes", "completion_gate_contracts", "part_catalog")}
    projection["recipes"] = recipes
    projection["completion_gate_contracts"] = {k: v for k, v in contracts.items() if k in referenced}
    if used_parts:
        catalog = part_catalog(registry)
        rows = catalog.get("parts") or {}
        projection["part_catalog"] = {
            "parts": {part: rows.get(part) for part in sorted(used_parts)},
            "hosts": (catalog.get("hosts") or {}).get(capability),
        }
    return "sha256:" + hashlib.sha256(canonical_registry_bytes(projection)).hexdigest()


def expected_recipe_keys(manifest=None):
    if manifest is None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # W7C: every entry-router capability (the 12 user-facing entries, not only
    # the `entry` group) needs a recipe so a producer cycle can be issued for
    # it at direct/quick/standard+.
    return {(name, mode) for name, spec in manifest["capabilities"].items()
            if spec.get("invocation", {}).get("class") in ("entry-router", "compiler-internal")
            for mode in (spec["modes"] or ["default"])}


def recipe_keys(registry):
    return {(r["capability"], mode) for r in registry["recipes"] for mode in r["modes"]}


ROUTE_FRAME_CAPABILITY = "route-frame"
ROUTE_DECISION_KIND = "runtime-terminal"
ROUTE_FRAME_NODE_IDS = ["frame", "frame-alternative", "route-decision"]
# A framed route that runs one frame leg seals this projection of the same recipe.
ROUTE_FRAME_ONE_LEG_NODE_IDS = ["frame", "route-decision"]


def is_route_frame_terminal(recipe, node):
    """Whether ``node`` is the one model-less runtime terminal this registry accepts.

    The runtime terminal is not a worker kind and is not in `worker_kinds`: it is
    accepted only as the `route-decision` node of the `route-frame` recipe, and only
    when the whole recipe has the exact shape checked by `_validate_route_frame_shape`.
    Every other recipe that names such a node fails the ordinary worker-kind check.
    """
    return (isinstance(recipe, dict) and recipe.get("capability") == ROUTE_FRAME_CAPABILITY
            and isinstance(node, dict) and node.get("id") == "route-decision"
            and node.get("kind") == ROUTE_DECISION_KIND)


def _validate_route_frame_shape(recipe):
    """Exact shape of the compiler-internal framed route: two `top` frame legs and the terminal."""
    cap = ROUTE_FRAME_CAPABILITY
    graph = recipe["standard_plus"]
    nodes = graph.get("nodes", [])
    if [n.get("id") for n in nodes] != ROUTE_FRAME_NODE_IDS:
        raise TopologyError(f"{cap}: nodes must be exactly {ROUTE_FRAME_NODE_IDS}")
    frame, alternative, decision = nodes
    for leg in (frame, alternative):
        if (leg.get("kind") != "map-worker" or leg.get("unit") != "plan/frame"
                or leg.get("worker_type") != "frame" or leg.get("dispatch_depth") != 1
                or leg.get("depends_on") != [] or leg.get("launch_authority") != "depth-0"
                or leg.get("continuation") != {"kind": "human-gate", "gate": "frame-review"}):
            raise TopologyError(f"{cap}:{leg.get('id')}: frame leg shape mismatch")
    expected_decision = {
        "id": "route-decision", "kind": ROUTE_DECISION_KIND, "depends_on": ["frame", "frame-alternative"],
        "dispatch_depth": 0, "execution_surface": "inline", "registered_worker": False,
        "terminal": True, "terminal_gate": "route-decision", "completion_gate": "route-decision",
        "commit_expected": False, "advance_class": "runtime-eligible", "resource_class": "normal",
    }
    if any(decision.get(key) != value for key, value in expected_decision.items()):
        raise TopologyError(f"{cap}:route-decision: runtime terminal shape mismatch")
    if any(key in decision for key in ("unit", "model_profile", "worker_type", "fallback_hops",
                                       "continuation", "unit_choices", "role")):
        raise TopologyError(f"{cap}:route-decision: a runtime terminal carries no unit, role or model")
    if (recipe["human_gates"] != ["frame-review"]
            or recipe["human_gate_bindings"] != [{"gate": "frame-review", "node": "route-decision",
                                                  "position": "entry"}]):
        raise TopologyError(f"{cap}: exactly one frame-review binding at the terminal entry is required")
    if graph.get("parallel_groups") or recipe.get("conditional_extensions"):
        raise TopologyError(f"{cap}: no parallel groups or extensions")


def _scope_root(scope):
    if not isinstance(scope, str) or not scope or scope.startswith("/") or ".." in scope.split("/"):
        raise TopologyError(f"unsafe write scope: {scope!r}")
    return scope[:-3] if scope.endswith("/**") else scope


def _overlap(a, b):
    a, b = _scope_root(a), _scope_root(b)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _touches_artifact(scope, artifact):
    root = _scope_root(scope)
    return root == artifact or root.startswith(artifact + "/")


def _parallel_path(path, suffix):
    """Return a deterministic, disjoint artifact/scope path for one extra leg.

    Single shared home for this rule (U-4): `capability-route.py` imports it
    from here instead of keeping a second copy, so route expansion and D-1's
    bucket-anchor containment re-check can never drift apart.
    """
    if path.endswith("/**") and "*" not in path[:-3]:
        return path[:-3] + f"-{suffix}/**"
    head, sep, tail = path.rpartition("/")
    name, dot, ext = tail.rpartition(".")
    if not dot:
        return path + f"-{suffix}"
    if head:
        parent, parent_sep, directory = head.rpartition("/")
        return f"{parent}{parent_sep}{directory}-{suffix}/{tail}"
    return f"{head}{sep}{name}.{suffix}{dot}{ext}"


def _is_semantic_output(output):
    return is_semantic_output(output)


def _output_within_scope(output, scope):
    """Return whether one path output is writable under one declared scope."""
    output_root = _scope_root(output)
    scope_root = _scope_root(scope)
    if scope.endswith("/**"):
        return output_root == scope_root or output_root.startswith(scope_root + "/")
    return output == scope


def _uncovered_path_outputs(outputs, scopes):
    return [
        output for output in outputs if not _is_semantic_output(output)
        if not any(_output_within_scope(output, scope) for scope in scopes)
    ]


_ANCHOR_WILDCARD_RE = re.compile(r"^<[a-z]+>$")
# `<component>` is reserved-segment aware: a leading-underscore segment (e.g.
# `_internal`) is a bucket-internal name, never a component identifier, so it
# must not be absorbed by the wildcard (F10) -- otherwise the flat
# `spec/_internal/**` form and the component form `spec/<component>/_internal/`
# become indistinguishable and the more specific anchor can never win.
_RESERVED_SEGMENT_PREFIX = "_"

# D-1 (F2): scope roots whose first segment is one of these known top-level
# pollution names are rejected outright in `implicit` mode, unless that name is
# the recipe's own declared `map_anchor`/`review_anchor` (e.g. draft/lab/
# research legitimately write under a top-level `reviews/` bucket-relative
# segment). `implicit` mode has no literal cycle-anchor prefix to check against,
# so without this list any bare scope --including one that spells a design or
# spec top-level bucket name-- passes unexamined (the exact P1-1/P1-1b shape).
_IMPLICIT_FORBIDDEN_TOP_SEGMENTS = frozenset(
    {"design", "tokens", "components", "reviews", "handoff", "shards"}
)


def _anchor_prefix_match(root, anchor):
    """True when `root`'s leading segments equal `anchor`'s, `<token>` segments
    of `anchor` matching exactly one arbitrary segment of `root` (except that
    `<component>` never absorbs a reserved `_`-prefixed segment)."""
    root_segments, anchor_segments = root.split("/"), anchor.split("/")
    if len(root_segments) < len(anchor_segments):
        return False, None
    for root_seg, anchor_seg in zip(root_segments, anchor_segments):
        wildcard = _ANCHOR_WILDCARD_RE.match(anchor_seg)
        if wildcard:
            if anchor_seg == "<component>" and root_seg.startswith(_RESERVED_SEGMENT_PREFIX):
                return False, None
            continue
        if root_seg != anchor_seg:
            return False, None
    return True, "/".join(root_segments[len(anchor_segments):])


def _validate_bucket_anchor(recipe, registry, scopes, node_kind, node_id, *, require_anchor_tail=False):
    """D-1: every write scope must classify into exactly one artifact domain.

    A scope is external (an exact-string allowlisted token outside the
    artifact root), root-anchor-relative (a fixed artifact-root-relative path
    that itself sits inside a declared `artifact_buckets` value), or
    cycle/target relative (the recipe's own bucket, or a runtime-resolved
    target). `_validate_recipe`'s 2026-08-05 predecessor rejected everything
    at the artifact-root top level implicitly by requiring a literal
    `shards/`/`reviews/`+`_internal/` prefix on map/review scopes; this
    generalizes that into a declared, per-recipe subtree instead of a
    hardcoded literal, and additionally rejects *any* scope (not just
    map/review ones) that fails to classify into a single unambiguous domain.

    `require_anchor_tail` (F4) is set only by the post-parallel-leg-expansion
    re-check: a realized leg must always land on a *sibling* of its base scope
    inside the same anchor, never overwrite the anchor itself. The base
    (non-leg) call sites never pass this, because a scope
    that terminates exactly at its anchor (tail == "") is a legitimate write
    to the cycle root itself.
    """
    capability = recipe["capability"]
    artifact_scope = recipe.get("artifact_scope")
    if not isinstance(artifact_scope, dict):
        raise TopologyError(f"{capability}: artifact_scope required")
    buckets = registry.get("artifact_buckets", {})
    if not isinstance(buckets, dict) or not buckets:
        raise TopologyError("artifact_buckets registry table required")
    external = set(artifact_scope.get("external_scopes", []))
    root_anchors = artifact_scope.get("root_anchors", [])
    cycle_anchors = artifact_scope.get("cycle_anchors", [])
    target_relative = artifact_scope.get("target_relative", False)
    map_anchor = artifact_scope.get("map_anchor")
    review_anchor = artifact_scope.get("review_anchor")
    # `literal`: the write_scope string itself carries the bucket prefix
    # (design's `designs/01_refs/**`, spec's `spec/_internal/research/**`)
    # -- required whenever a scope is genuinely ambiguous without it (a design
    # scope has to say which of two owning buckets it belongs to). `implicit`:
    # the bucket is applied externally by the caller and the scope string
    # stays bare (code's `plan/**`, research's `shards/retrieval/**`) -- the
    # pre-existing, still-correct convention for every other track.
    anchor_mode = artifact_scope.get("anchor_mode", "implicit")
    if anchor_mode not in ("implicit", "literal"):
        raise TopologyError(f"{capability}: artifact_scope.anchor_mode must be implicit or literal")
    if not cycle_anchors and not target_relative and not root_anchors and not external:
        raise TopologyError(f"{capability}: artifact_scope declares no domain")
    for anchor in root_anchors:
        if not any(anchor == bucket or anchor.startswith(bucket + "/") for bucket in buckets.values()):
            raise TopologyError(
                f"{capability}: root_anchor {anchor!r} is not inside a declared artifact bucket"
            )
    # F10: every declared cycle_anchor must itself be (or sit inside) a
    # declared `artifact_buckets` value, exactly like root_anchors above --
    # otherwise a recipe can declare an undeclared bucket name (`rogue/<cycle>`)
    # and anchor_mode=literal would happily validate scopes against it.
    for anchor in cycle_anchors:
        if not any(anchor == bucket or anchor.startswith(bucket + "/") for bucket in buckets.values()):
            raise TopologyError(
                f"{capability}: cycle_anchor {anchor!r} is not a declared artifact bucket"
            )
    for scope in scopes:
        root = _scope_root(scope)
        # Domains are mutually exclusive by priority (external > root_anchor >
        # cycle/target relative): a scope that is both an exact external token
        # and would also textually fall under a root_anchor or cycle_anchor is
        # still exactly one domain by construction. The one real ambiguity is
        # `literal` mode with more than one cycle_anchor matching the same
        # scope -- resolved by preferring the most specific (longest) anchor,
        # so a component anchor (`spec/<component>`) wins over its own flat
        # parent (`spec`) whenever both match the same scope.
        if scope in external:
            domain_kind, tail = "external", root
        elif any(root == anchor or root.startswith(anchor + "/") for anchor in root_anchors):
            domain_kind, tail = "root_anchor", root
        elif anchor_mode == "literal" and cycle_anchors:
            matches = []
            for anchor in cycle_anchors:
                matched, matched_tail = _anchor_prefix_match(root, anchor)
                if matched:
                    matches.append((len(anchor.split("/")), matched_tail))
            if not matches:
                raise TopologyError(
                    f"{capability}:{node_id}: write scope {scope!r} matches no declared cycle_anchor"
                )
            most_specific = max(length for length, _tail in matches)
            candidates = {tail for length, tail in matches if length == most_specific}
            if len(candidates) != 1:
                raise TopologyError(
                    f"{capability}:{node_id}: write scope {scope!r} matches {len(candidates)} "
                    "cycle_anchors ambiguously"
                )
            domain_kind, tail = "cycle_anchor", next(iter(candidates))
        elif cycle_anchors:
            # implicit mode: the write_scope string carries no bucket prefix at
            # all (it is resolved cycle-relative by the caller at dispatch
            # time), so there is no anchor text to check the scope against.
            # F2: without at least a negative check, a bare scope can still
            # spell a top-level pollution name that a *different* track's
            # literal anchor owns (`reviews/visual/**`, `handoff/**`) and pass
            # unexamined -- the exact P1-1/P1-1b shape this cycle closes. A
            # segment is allowed only when it is this recipe's own declared
            # map_anchor/review_anchor (draft/lab/research legitimately use a
            # top-level `reviews/` segment because `reviews` *is* their
            # review_anchor).
            top = root.split("/", 1)[0]
            own_anchors = {a.split("/", 1)[0] for a in (map_anchor, review_anchor) if a}
            if top in _IMPLICIT_FORBIDDEN_TOP_SEGMENTS and top not in own_anchors:
                raise TopologyError(
                    f"{capability}:{node_id}: write scope {scope!r} spells reserved top-level "
                    f"segment {top!r}, which this recipe does not own"
                )
            domain_kind, tail = "cycle_anchor", root
        elif target_relative:
            domain_kind, tail = "target_relative", root
        else:
            raise TopologyError(
                f"{capability}:{node_id}: write scope {scope!r} does not classify into any "
                "declared artifact domain"
            )
        if require_anchor_tail and domain_kind in ("cycle_anchor", "root_anchor") and tail == "":
            raise TopologyError(
                f"{capability}:{node_id}: write scope {scope!r} exhausts its anchor with no "
                "leaf tail; a realized parallel leg must stay a sibling inside the anchor, "
                "never suffix the anchor's own wildcard segment"
            )
        if node_kind == "map-worker":
            if not map_anchor:
                raise TopologyError(f"{capability}:{node_id}: map-worker requires a declared map_anchor")
            if domain_kind not in ("cycle_anchor", "target_relative") or not (
                tail == map_anchor or tail.startswith(map_anchor + "/")
            ):
                raise TopologyError(f"{capability}:{node_id}: map worker may write only inside {map_anchor}")
        if node_kind == "review-worker":
            if not review_anchor:
                raise TopologyError(f"{capability}:{node_id}: review-worker requires a declared review_anchor")
            if domain_kind not in ("cycle_anchor", "target_relative") or not (
                tail == review_anchor or tail.startswith(review_anchor + "/")
            ):
                raise TopologyError(f"{capability}:{node_id}: reviewer may write isolated verdicts only")


def _artifact_owner(registry, root):
    """Longest-prefix `artifact_owners` lookup (P4-8): `spec/design` can be owned
    by a different capability than plain `spec`, without a second bespoke check
    site — `artifact_owners` has exactly one consumer (this function), confirmed
    by grep before this rule changed from an exact-key lookup (§1.1-4 of the
    plan): `_scope_touches_spec`, `spec-transaction.py`, and
    `hooks/artifact-guard.sh` each run their own literal `spec`/`spec/` check
    and never read `artifact_owners`, so loosening the match here cannot loosen
    spec ownership anywhere else.
    """
    owners = registry.get("artifact_owners", {})
    segments = root.split("/")
    for depth in range(len(segments), 0, -1):
        candidate = "/".join(segments[:depth])
        if candidate in owners:
            return owners[candidate]
    return None


def _validate_guard_scope(recipe, scopes, preconditions, registry, node_id):
    declared = set(preconditions or [])
    unknown = declared - set(registry["guard_preconditions"])
    if unknown:
        raise TopologyError(f"{recipe['capability']}:{node_id}: unknown guard preconditions {sorted(unknown)}")
    for scope in scopes:
        if not _touches_artifact(scope, "spec"):
            continue
        owner = _artifact_owner(registry, _scope_root(scope))
        if recipe["capability"] != owner and "artifact-order-prechecked" not in declared:
            raise TopologyError(
                f"{recipe['capability']}:{node_id}: spec write scope requires sole owner "
                "or artifact-order-prechecked"
            )


def _validate_model_profile(registry, profile, context, *, registered=True):
    profiles = registry.get("model_profiles", {})
    row = profiles.get(profile)
    if not isinstance(row, dict):
        raise TopologyError(f"{context}: unknown model_profile {profile!r}")
    if registered and row.get("registered_topology") is not True:
        raise TopologyError(f"{context}: mini/unregistered model_profile is forbidden")
    return row


def _unit_frontmatter(unit):
    """Read the few scalars the validator needs from a unit file (stdlib regex only)."""
    view = _UNIT_CONTRACT_VIEW.get()
    if view is not None and unit in view:
        return dict(view[unit])
    key = (str(UNITS), unit)
    if key in _UNIT_CACHE:
        return _UNIT_CACHE[key]
    path = UNITS / f"{unit}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise TopologyError(f"unknown unit: {unit} (no roles/units/{unit}.md)")
    fields = _parse_unit_frontmatter(unit, text)
    _UNIT_CACHE[key] = fields
    return fields


def _parse_unit_frontmatter(unit, text):
    """Parse unit contract fields from the same sealed frontmatter text used on disk."""
    match = re.match(r"\A---\n(.*?\n)---\n", text, re.DOTALL)
    if not match:
        raise TopologyError(f"unit {unit}: missing frontmatter block")
    block = match.group(1)

    def scalar(name):
        found = re.search(rf"^{name}:\s*([^#\n]+?)\s*(?:#.*)?$", block, re.MULTILINE)
        return found.group(1).strip() if found else None

    fields = {name: scalar(name) for name in ("unit", "role", "worker_type", "read_only")}
    if fields["unit"] != unit:
        raise TopologyError(f"unit {unit}: frontmatter unit id mismatch ({fields['unit']!r})")
    if not fields["role"]:
        raise TopologyError(f"unit {unit}: frontmatter role required")
    if fields["worker_type"] not in ("owner", "stage", "review", "support", "frame"):
        raise TopologyError(f"unit {unit}: invalid worker_type {fields['worker_type']!r}")
    if fields["read_only"] not in ("true", "false"):
        raise TopologyError(f"unit {unit}: read_only must be true or false")
    verdict = re.search(r"^io:\s*\n\s+verdict:\s*\[([^\]]*)\]", block, re.MULTILINE)
    fields["verdict"] = (
        [token.strip() for token in verdict.group(1).split(",") if token.strip()]
        if verdict
        else []
    )
    return fields


@contextlib.contextmanager
def _unit_contract_view(fields_by_unit):
    """Temporarily expose authenticated historical fields without touching the cache."""
    copied={unit:dict(fields) for unit,fields in fields_by_unit.items()}
    token=_UNIT_CONTRACT_VIEW.set(copied)
    try:
        yield
    finally:
        _UNIT_CONTRACT_VIEW.reset(token)


def _validate_unit_ref(recipe, node, registry):
    capability, node_id, kind = recipe.get("capability"), node.get("id"), node.get("kind")
    unit = node.get("unit")
    compat = registry.get("unit_kind_compatibility", {}).get(kind)
    if compat is None:
        raise TopologyError(f"{capability}:{node_id}: no unit compatibility row for kind {kind}")
    if not isinstance(unit, str) or not unit:
        raise TopologyError(f"{capability}:{node_id}: node unit ref required")
    if kind in ("capability-owner", "resource-runner"):
        if unit not in compat or unit not in registry.get("reserved_units", {}):
            raise TopologyError(
                f"{capability}:{node_id}: kind {kind} requires reserved unit {sorted(compat)}"
            )
        # Reserved refs carry no catalog frontmatter, so their role must be pinned here
        # (2026-07-22 verify finding: _kernel/owner previously accepted any role).
        reserved_roles = {"_kernel/owner": {"deep orchestrator"},
                          "_kernel/resource": {"orchestrator"}}
        allowed = reserved_roles.get(unit)
        if allowed is not None and node.get("role") not in allowed:
            raise TopologyError(
                f"{capability}:{node_id}: reserved unit {unit} allows roles {sorted(allowed)}, got {node.get('role')}"
            )
        if "unit_choices" in node:
            raise TopologyError(f"{capability}:{node_id}: reserved-unit node cannot carry unit_choices")
        return
    if unit in registry.get("reserved_units", {}):
        raise TopologyError(f"{capability}:{node_id}: reserved unit {unit} not allowed on kind {kind}")
    choices = node.get("unit_choices")
    if choices is not None:
        if not isinstance(choices, list) or not choices:
            raise TopologyError(f"{capability}:{node_id}: unit_choices must be a non-empty list")
        if unit not in choices:
            raise TopologyError(f"{capability}:{node_id}: unit {unit} not in unit_choices")
    members = [unit] + [c for c in (choices or []) if c != unit]
    for member in members:
        if not UNIT_REF_RE.match(member or ""):
            raise TopologyError(f"{capability}:{node_id}: invalid unit ref {member!r}")
        fields = _unit_frontmatter(member)
        if fields["worker_type"] not in compat:
            raise TopologyError(
                f"{capability}:{node_id}: unit {member} worker_type {fields['worker_type']} "
                f"incompatible with kind {kind}"
            )
        if kind == "review-worker" and fields["read_only"] != "true":
            raise TopologyError(
                f"{capability}:{node_id}: review-worker unit {member} must declare read_only: true"
            )
    if node.get("role") != _unit_frontmatter(unit)["role"]:
        raise TopologyError(
            f"{capability}:{node_id}: node role {node.get('role')!r} differs from "
            f"unit role {_unit_frontmatter(unit)['role']!r}"
        )


def _validate_gate_contracts(recipe, registry):
    contracts = registry.get("completion_gate_contracts")
    if not isinstance(contracts, dict):
        raise TopologyError("completion_gate_contracts table required")
    node_by_gate = {
        node.get("completion_gate"): node
        for node in recipe["standard_plus"].get("nodes", [])
    }
    for gate in recipe["completion_gates"]:
        entry = contracts.get(gate)
        if not isinstance(entry, dict):
            raise TopologyError(f"{recipe['capability']}: gate {gate} lacks a completion_gate_contracts entry")
        kind = entry.get("kind")
        if kind == "unit-io":
            node = node_by_gate.get(gate)
            # SD-165: a node that declares `unit_choices` may run any of them
            # under its one unit-io gate; the gate names the default choice.
            if node is None or (entry.get("unit") != node.get("unit")
                                and entry.get("unit") not in (node.get("unit_choices") or [])):
                raise TopologyError(
                    f"{recipe['capability']}: unit-io gate {gate} must name the carrying node's unit"
                )
        elif kind == "capability-doc":
            contract = entry.get("contract")
            if not contract or not (ROOT / contract).is_file():
                raise TopologyError(
                    f"{recipe['capability']}: capability-doc contract missing for gate {gate}: {contract!r}"
                )
        elif kind == "custom":
            if not entry.get("doc"):
                raise TopologyError(f"{recipe['capability']}: custom gate {gate} requires a recorded reason")
        else:
            raise TopologyError(f"{recipe['capability']}: unknown gate contract kind {kind!r} for {gate}")
    # AC 5 (front half): every auxiliary-bearing group's ARBITER gate declares
    # `auxiliary_arbiter` — its verdict carries `auxiliary_findings_considered`
    # and the completion gate compares its length to the realized auxiliary count.
    #
    # This used to demand the declaration on the ANCHOR gate, which is the
    # proposition G1 disproved: PRD 13.30.4 names an arbiter for each anchor kind
    # and in none of the three is it the anchor. Leaving the old rule in place
    # kept the registry asserting, and this guard enforcing, a world the runtime
    # no longer lives in. `_resolve_auxiliary_arbiter` is still the one
    # implementation of the rule; it reads a compiled route, so this reads the
    # same three facts off the recipe, and
    # `capability_topology.test.py` pins the two to agree on every realized group.
    nodes_by_id = {node.get("id"): node for node in recipe["standard_plus"].get("nodes", [])}
    for group in recipe["standard_plus"].get("parallel_groups", []):
        if not any(leg.get("leg_class") == "auxiliary" for leg in group.get("legs", [])):
            continue
        anchor_node = nodes_by_id.get(group.get("node"))
        if anchor_node is None:
            continue
        consumers = [
            node for node in recipe["standard_plus"].get("nodes", [])
            if group.get("node") in (node.get("depends_on") or [])
        ]
        kind = anchor_node.get("kind")
        if kind == "review-worker":
            # the conductor merges; no route node is the arbiter, so no gate
            # declares it
            continue
        if kind == "pipeline-stage":
            consumers = [node for node in consumers if node.get("kind") == "review-worker"]
        if len(consumers) != 1:
            raise TopologyError(
                f"{recipe['capability']}: auxiliary group {group.get('id')} anchor "
                f"{group.get('node')} ({kind}) needs exactly one declared arbiter "
                f"consumer, found {len(consumers)}"
            )
        gate = consumers[0].get("completion_gate")
        entry = contracts.get(gate)
        if not isinstance(entry, dict) or entry.get("auxiliary_arbiter") is not True:
            raise TopologyError(
                f"{recipe['capability']}: auxiliary group {group.get('id')} arbiter gate "
                f"{gate} must declare auxiliary_arbiter"
            )
    # `auxiliary_arbiter` is a gate's ability to consume auxiliary findings.
    # Only a selected auxiliary group creates that obligation. Reusing the
    # review unit on a smaller graph does not require its preset's producers.


def _validate_activation_conditions(registry):
    conditions = registry.get("activation_conditions")
    if not isinstance(conditions, dict) or set(conditions) != {"artifact-sink-available"}:
        raise TopologyError("activation_conditions must declare artifact-sink-available")
    expected_keys = {
        "check", "probe_kind", "success_state", "unavailable_exit_code",
        "unavailable_reason",
    }
    row = conditions["artifact-sink-available"]
    if not isinstance(row, dict) or set(row) != expected_keys:
        raise TopologyError("artifact-sink-available activation contract shape mismatch")
    if row != {
        "check": "utilities/artifact-sink.sh --check",
        "probe_kind": "local-registration",
        "success_state": "available",
        "unavailable_exit_code": 69,
        "unavailable_reason": "extension-unavailable",
    }:
        raise TopologyError("artifact-sink-available activation contract mismatch")


def _validate_conditional_extensions(recipe, registry, by_id, deps):
    extensions = recipe.get("conditional_extensions", [])
    if not isinstance(extensions, list):
        raise TopologyError(f"{recipe['capability']}: conditional_extensions must be a list")
    required = {
        "id", "extension", "activation_condition", "after",
        "source_outputs", "on_unavailable",
    }
    ids = [row.get("id") for row in extensions if isinstance(row, dict)]
    if len(ids) != len(extensions) or len(ids) != len(set(ids)) or not all(ids):
        raise TopologyError(f"{recipe['capability']}: duplicate/empty conditional extension id")
    if any(str(i).startswith("_") for i in ids):
        raise TopologyError(f"{recipe['capability']}: route-node-id-reserved-prefix")
    node_ids = set(by_id)
    dependency_ids = {dep for node in by_id.values() for dep in node.get("depends_on", [])}
    terminal_ids = node_ids - dependency_ids
    known_conditions = set(registry["activation_conditions"])
    for row in extensions:
        if set(row) != required:
            raise TopologyError(
                f"{recipe['capability']}:{row.get('id')}: conditional extension requires exactly {sorted(required)}"
            )
        if row["extension"] != "artifact-sink":
            raise TopologyError(
                f"{recipe['capability']}:{row['id']}: conditional extension must target artifact-sink"
            )
        if row["activation_condition"] not in known_conditions:
            raise TopologyError(
                f"{recipe['capability']}:{row['id']}: unknown activation condition"
            )
        if row["on_unavailable"] != "skip":
            raise TopologyError(
                f"{recipe['capability']}:{row['id']}: unavailable action must be skip"
            )
        after = row["after"]
        if (not isinstance(after, list) or not after or len(after) != len(set(after))
                or not set(after) <= terminal_ids):
            raise TopologyError(
                f"{recipe['capability']}:{row['id']}: after must name unique terminal nodes"
            )
        sources = row["source_outputs"]
        if not isinstance(sources, list) or not sources:
            raise TopologyError(
                f"{recipe['capability']}:{row['id']}: source_outputs must be non-empty"
            )
        seen_sources = set()
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"node", "output"}:
                raise TopologyError(
                    f"{recipe['capability']}:{row['id']}: source output shape mismatch"
                )
            source_node, output = source["node"], source["output"]
            key = (source_node, output)
            if key in seen_sources:
                raise TopologyError(
                    f"{recipe['capability']}:{row['id']}: duplicate source output"
                )
            seen_sources.add(key)
            if source_node not in by_id or output not in by_id[source_node].get("outputs", []):
                raise TopologyError(
                    f"{recipe['capability']}:{row['id']}: source output is not declared by its node"
                )
            if any(source_node != terminal and source_node not in deps(terminal) for terminal in after):
                raise TopologyError(
                    f"{recipe['capability']}:{row['id']}: source output must precede every terminal anchor"
                )


def _validate_workflow_vocabulary(registry):
    """The portable tracked-workflow state machine (WORKFLOW §0.6) is registry-owned."""
    states = registry.get("workflow_states")
    failures = registry.get("workflow_failure_states")
    if states != ["CREATED", "READY", "RUNNING", "STAGE_SUCCEEDED", "NEXT_REGISTERED",
                  "NEXT_RUNNING", "TERMINAL_VERIFY", "COMPLETE"]:
        raise TopologyError("workflow_states must declare the portable ordered lifecycle")
    if failures != ["BLOCKED_HUMAN_GATE", "FAILED_RETRYABLE", "FAILED_TERMINAL", "CANCELLED"]:
        raise TopologyError("workflow_failure_states must declare the portable failure set")
    transitions = registry.get("workflow_transitions")
    known = set(states) | set(failures)
    if not isinstance(transitions, dict) or set(transitions) != known:
        raise TopologyError("workflow_transitions must cover every state exactly once")
    for state, targets in transitions.items():
        if not isinstance(targets, list) or len(targets) != len(set(targets)) \
                or not set(targets) <= known:
            raise TopologyError(f"workflow transition targets invalid for {state}")
    # COMPLETE reachable only through TERMINAL_VERIFY is the mechanical form of "a workflow
    # never completes before its terminal node" (2026-08-04 KWS_Net_tf).
    reaching_complete = {s for s, t in transitions.items() if "COMPLETE" in t}
    if reaching_complete != {"TERMINAL_VERIFY"}:
        raise TopologyError("COMPLETE must be reachable only from TERMINAL_VERIFY")
    if transitions["BLOCKED_HUMAN_GATE"] != ["RUNNING", "CANCELLED", "FAILED_TERMINAL"]:
        raise TopologyError("a human gate may only be released to RUNNING or abandoned")
    for terminal in ("COMPLETE", "FAILED_TERMINAL", "CANCELLED"):
        if transitions[terminal]:
            raise TopologyError(f"{terminal} must be absorbing")
    kinds = registry.get("continuation_kinds")
    if not isinstance(kinds, dict) or set(kinds) != {
        "inline-next", "supervised", "human-gate", "monitor"
    }:
        raise TopologyError("continuation_kinds must declare the four portable continuations")
    for name, row in kinds.items():
        if not isinstance(row, dict) or set(row) != {"doc", "requires_supervisor"} \
                or not row["doc"] or not isinstance(row["requires_supervisor"], bool):
            raise TopologyError(f"continuation kind {name} contract shape mismatch")
    if kinds["supervised"]["requires_supervisor"] is not True \
            or kinds["monitor"]["requires_supervisor"] is not True:
        raise TopologyError("supervised and monitor continuations require a supervisor")
    if registry.get("human_gate_positions") != ["entry", "terminal"]:
        raise TopologyError("human_gate_positions must be entry and terminal")


def _validate_continuations(recipe, registry, nodes, by_id):
    """Refuse a graph that can stall: every non-terminal node names its continuation.

    A stage graph whose last node is a detached process, or whose middle node has no
    declared way to reach the next one, is the exact 2026-08-04 KWS_Net_tf failure:
    training finished and nothing owned what came after it. The refusal happens at
    registry validation and therefore at route compile, because a graph repaired at
    runtime has already lost the run.
    """
    # A route reaching here without its capability used to raise a bare
    # KeyError. `build_continuation_route`'s caller only wraps TopologyError,
    # so that crash escaped as an untyped exception instead of the typed
    # refusal every other continuation problem produces (CI 2026-09-10).
    capability = recipe.get("capability")
    if not isinstance(capability, str) or not capability:
        raise TopologyError("continuation validation requires the route's capability")
    kinds = registry["continuation_kinds"]
    ids = [node["id"] for node in nodes]
    dependents = {node_id: [] for node_id in ids}
    for node in nodes:
        for dep in node.get("depends_on", []):
            dependents[dep].append(node["id"])

    bindings = recipe.get("human_gate_bindings")
    if not isinstance(bindings, list):
        raise TopologyError(f"{capability}: human_gate_bindings must be a list")
    declared = list(recipe["human_gates"])
    if len(declared) != len(set(declared)):
        raise TopologyError(f"{capability}: duplicate human gate")
    bound, entry_gate_of, terminal_gated = [], {}, set()
    for row in bindings:
        if not isinstance(row, dict) or set(row) != {"gate", "node", "position"}:
            raise TopologyError(f"{capability}: human gate binding shape mismatch")
        if row["gate"] not in declared:
            raise TopologyError(f"{capability}: bound gate {row['gate']} is not declared")
        if row["node"] not in by_id:
            raise TopologyError(f"{capability}: human gate {row['gate']} binds unknown node")
        if row["position"] not in registry["human_gate_positions"]:
            raise TopologyError(f"{capability}: human gate {row['gate']} has invalid position")
        bound.append(row["gate"])
        if row["position"] == "entry":
            if row["node"] in entry_gate_of:
                raise TopologyError(f"{capability}: node {row['node']} carries two entry gates")
            entry_gate_of[row["node"]] = row["gate"]
        else:
            terminal_gated.add(row["node"])
    if sorted(bound) != sorted(declared) or len(bound) != len(set(bound)):
        raise TopologyError(
            f"{capability}: every declared human gate must bind to exactly one node"
        )

    for node in nodes:
        node_id = node["id"]
        continuation = node.get("continuation")
        if not dependents[node_id]:
            if continuation is not None:
                raise TopologyError(f"{capability}:{node_id}: a terminal node has no continuation")
            if node.get("terminal") is not True:
                raise TopologyError(f"{capability}:{node_id}: sink must declare terminal: true")
            if node.get("terminal_gate") != node.get("completion_gate"):
                raise TopologyError(
                    f"{capability}:{node_id}: terminal_gate must equal the node completion gate"
                )
            if node.get("kind") == "resource-runner":
                raise TopologyError(
                    f"{capability}:{node_id}: a detached resource run can never be the workflow "
                    "terminal — declare the stage that verifies and hands it off"
                )
            continue
        if node.get("terminal") is not None or node.get("terminal_gate") is not None:
            raise TopologyError(f"{capability}:{node_id}: only a sink may declare terminal")
        if not isinstance(continuation, dict) or continuation.get("kind") not in kinds:
            raise TopologyError(
                f"{capability}:{node_id}: non-terminal node requires a continuation of "
                f"{sorted(kinds)}"
            )
        kind = continuation["kind"]
        expected = {"kind"} | ({"gate"} if kind == "human-gate" else set()) \
            | ({"monitor"} if kind == "monitor" else set())
        if set(continuation) != expected:
            raise TopologyError(f"{capability}:{node_id}: continuation shape mismatch")
        if node.get("kind") == "resource-runner" and kind != "supervised":
            raise TopologyError(
                f"{capability}:{node_id}: a detached resource run cannot continue itself; "
                "declare continuation kind supervised"
            )
        gated = {entry_gate_of[dep] for dep in dependents[node_id] if dep in entry_gate_of}
        if kind == "human-gate":
            if continuation["gate"] not in gated:
                raise TopologyError(
                    f"{capability}:{node_id}: human-gate continuation must name the entry gate "
                    "of a direct dependent"
                )
        elif gated:
            raise TopologyError(
                f"{capability}:{node_id}: a dependent carries entry gate {sorted(gated)[0]}, so "
                "this continuation must be human-gate"
            )
        if kind == "monitor" and not str(continuation.get("monitor") or "").strip():
            raise TopologyError(f"{capability}:{node_id}: monitor continuation requires a name")
    for node_id in terminal_gated:
        if dependents[node_id]:
            raise TopologyError(
                f"{capability}: a terminal-position human gate must bind the terminal node"
            )


def _validate_recipe(recipe, registry, standard_plus_owner_profile):
    required = {"capability", "modes", "topology_class", "direct_predicates", "promotion_signals",
                "quick", "standard_plus", "completion_gates", "human_gates",
                "human_gate_bindings", "resume_retry_boundaries"}
    missing = required - recipe.keys()
    if missing:
        raise TopologyError(f"{recipe.get('capability')}: missing {sorted(missing)}")
    bare_depth_keys = {"depth", "owner_depth", "max_depth"}
    if any(key in recipe for key in bare_depth_keys):
        raise TopologyError(f"{recipe['capability']}: bare recipe dispatch-depth fields are forbidden")
    quick = recipe["quick"]
    if any(key in quick for key in bare_depth_keys):
        raise TopologyError(f"{recipe['capability']}: bare quick dispatch-depth fields are forbidden")
    if quick.get("owner_dispatch_depth") != 1 or quick.get("max_dispatch_depth") != 1:
        raise TopologyError(f"{recipe['capability']}: quick topology must be dispatch depth 1")
    _validate_model_profile(
        registry, quick.get("model_profile"), f"{recipe['capability']}:quick"
    )
    quick_owner_profile = registry["owner_profile_by_intensity"]["quick"]
    if quick.get("model_profile") != quick_owner_profile:
        raise TopologyError(
            f"{recipe['capability']}:quick: model_profile must match "
            f"owner_profile_by_intensity.quick ({quick_owner_profile})"
        )
    inline_gates = quick.get("inline_human_gates", [])
    expected_inline = ["preview-disposition"] if recipe["capability"] == "autopilot-refine" else []
    if inline_gates != expected_inline:
        raise TopologyError(f"{recipe['capability']}: quick inline human gates differ from the apply contract")
    for scope in quick.get("write_scope", []): _scope_root(scope)
    _validate_guard_scope(recipe, quick.get("write_scope", []), quick.get("guard_preconditions", []), registry, "quick")
    _validate_bucket_anchor(recipe, registry, quick.get("write_scope", []), None, "quick")
    graph = recipe["standard_plus"]
    if any(key in graph for key in bare_depth_keys):
        raise TopologyError(f"{recipe['capability']}: bare standard+ dispatch-depth fields are forbidden")
    if graph.get("owner_dispatch_depth") != 1:
        raise TopologyError(f"{recipe['capability']}: owner dispatch depth must be 1")
    nodes = graph.get("nodes", [])
    ids = [n.get("id") for n in nodes]
    if len(ids) != len(set(ids)) or not all(ids):
        raise TopologyError(f"{recipe['capability']}: duplicate/empty node id")
    if any(str(i).startswith("_") for i in ids):
        raise TopologyError(f"{recipe['capability']}: route-node-id-reserved-prefix")
    by_id = {n["id"]: n for n in nodes}
    if recipe["capability"] == ROUTE_FRAME_CAPABILITY:
        _validate_route_frame_shape(recipe)
    actual_max_dispatch_depth = max(
        (
            node.get("dispatch_depth", 0)
            for node in nodes
            if node.get("kind") != "resource-runner"
        ),
        default=0,
    )
    if graph.get("max_dispatch_depth") != actual_max_dispatch_depth:
        raise TopologyError(
            f"{recipe['capability']}: max_dispatch_depth must equal "
            f"{actual_max_dispatch_depth}"
        )
    gates = set(recipe["completion_gates"])
    for node in nodes:
        requirements = node.get("runtime_requirements", [])
        if (
            not isinstance(requirements, list)
            or any(not isinstance(item, str) for item in requirements)
            or len(requirements) != len(set(requirements))
            or not set(requirements) <= set(registry.get("runtime_requirements", []))
        ):
            raise TopologyError(
                f"{recipe['capability']}:{node['id']}: invalid runtime requirements"
            )
        runtime_terminal = is_route_frame_terminal(recipe, node)
        if node.get("kind") not in registry["worker_kinds"] and not runtime_terminal:
            raise TopologyError(f"{recipe['capability']}:{node['id']}: invalid worker kind")
        if not runtime_terminal:
            _validate_unit_ref(recipe, node, registry)
        if "profile_demand" in node:
            PROFILE.resolve_profile_demand(node["profile_demand"])
        if runtime_terminal:
            pass  # exact shape is checked once for the whole recipe below
        elif node["kind"] == "resource-runner":
            if "model_profile" in node:
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: resource runner cannot carry model_profile"
                )
            if any(
                key in node
                for key in (
                    "depth", "owner_depth", "max_depth", "dispatch_depth",
                    "transport", "fallback_hops",
                )
            ):
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: resource runner lifecycle is not an agent dispatch"
                )
            if node.get("resource_transport") != "detached-process":
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: detached resource transport required"
                )
        else:
            _validate_model_profile(
                registry, node.get("model_profile"), f"{recipe['capability']}:{node['id']}"
            )
            if any(key in node for key in bare_depth_keys) or node.get("dispatch_depth") not in (1, 2):
                raise TopologyError(f"{recipe['capability']}:{node['id']}: dispatch_depth must be 1 or 2")
            if (
                node.get("kind") == "capability-owner"
                and node.get("unit") == "_kernel/owner"
                and (
                    node.get("dispatch_depth") != graph["owner_dispatch_depth"]
                    or node.get("dispatch_depth") != 1
                    or node.get("model_profile") != standard_plus_owner_profile
                )
            ):
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: semantic capability owner "
                    f"must use dispatch_depth=1 and model_profile="
                    f"{standard_plus_owner_profile}"
                )
            unknown_hops = set(node.get("fallback_hops", [])) - FALLBACK_HOPS
            if unknown_hops:
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: unknown fallback hops {sorted(unknown_hops)}"
                )
        if not node.get("inputs") or not node.get("outputs") or not node.get("write_scope"):
            raise TopologyError(f"{recipe['capability']}:{node['id']}: inputs/outputs/write_scope required")
        advance_class = node.get("advance_class")
        if advance_class not in ("runtime-eligible", "model-required"):
            raise TopologyError(
                f"{recipe['capability']}:{node['id']}: advance_class must be "
                "runtime-eligible or model-required"
            )
        model_required_reason = node.get("model_required_reason")
        if advance_class == "model-required":
            if model_required_reason not in MODEL_REQUIRED_REASONS:
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: model_required_reason "
                    f"must be one of {sorted(MODEL_REQUIRED_REASONS)}"
                )
        elif model_required_reason is not None:
            raise TopologyError(
                f"{recipe['capability']}:{node['id']}: model_required_reason is only "
                "valid when advance_class is model-required"
            )
        if "commit_expected" not in node or isinstance(node["commit_expected"], bool) is False:
            raise TopologyError(f"{recipe['capability']}:{node['id']}: commit_expected must be a boolean")
        if node.get("parent_cross_preference") is not None and not isinstance(
            node.get("parent_cross_preference"), bool
        ):
            raise TopologyError(
                f"{recipe['capability']}:{node['id']}: parent_cross_preference must be a boolean"
            )
        if "subdivision" in node:
            subdivision = node["subdivision"]
            if not isinstance(subdivision, dict):
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: subdivision must be an object"
                )
            max_slices = subdivision.get("max_slices")
            if isinstance(max_slices, bool) or not isinstance(max_slices, int) or not 2 <= max_slices <= 4:
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: subdivision.max_slices must be in 2..4"
                )
            if subdivision.get("disjointness") != "exact-fixed-files":
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: subdivision.disjointness must be exact-fixed-files"
                )
            minimum = subdivision.get("min_intensity")
            if minimum not in registry.get("intensities", []) or (
                registry["intensities"].index(minimum) < registry["intensities"].index("standard")
            ):
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: subdivision.min_intensity must be a standard+ tier"
                )
            if node.get("kind") != "pipeline-stage":
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: subdivision requires a pipeline-stage node"
                )
        if node.get("completion_gate") not in gates:
            raise TopologyError(f"{recipe['capability']}:{node['id']}: missing completion gate")
        if not set(node.get("depends_on", [])) <= set(ids):
            raise TopologyError(f"{recipe['capability']}:{node['id']}: unknown dependency")
        scopes = node["write_scope"]
        for scope in scopes: _scope_root(scope)
        _validate_guard_scope(recipe, scopes, node.get("guard_preconditions", []), registry, node["id"])
        _validate_bucket_anchor(recipe, registry, scopes, node["kind"], node["id"])
        # F3: `outputs` is the same live contract surface as `write_scope` (it
        # feeds downstream `inputs` at compile time -- see
        # `capability-route.py`'s parallel-leg expansion), so a path-shaped
        # output (containing "/") must classify into the same bucket domain.
        # Bare semantic tokens are an explicit closed vocabulary. A filename such
        # as plan.md is still a path even though it contains no slash.
        path_outputs = [
            out for out in node.get("outputs", []) if not _is_semantic_output(out)
        ]
        if path_outputs:
            _validate_bucket_anchor(recipe, registry, path_outputs, node["kind"], node["id"])
            uncovered = _uncovered_path_outputs(path_outputs, scopes)
            if uncovered:
                raise TopologyError(
                    f"{recipe['capability']}:{node['id']}: outputs outside write_scope "
                    f"{sorted(uncovered)}"
                )
    if "replication" in graph or "replications" in graph:
        raise TopologyError(
            f"{recipe['capability']}: v5 uses parallel_groups; legacy replication keys are read-only"
        )
    parallel_groups = graph.get("parallel_groups")
    if parallel_groups is not None:
        if not isinstance(parallel_groups, list) or not parallel_groups:
            raise TopologyError(
                f"{recipe['capability']}: parallel_groups must be a non-empty list"
            )
        required_group = {
            "id", "node", "kind", "min_intensity", "width_by_intensity",
            "join_policy", "independence_axes", "legs",
        }
        anchored, group_ids = set(), set()
        tiers = registry["intensities"]
        max_allowed = registry["parallel_group_max_width"]
        leg_classes = registry["leg_classes"]
        auxiliary_checks = registry["auxiliary_checks"]
        for group in parallel_groups:
            if not isinstance(group, dict) or set(group) != required_group:
                raise TopologyError(
                    f"{recipe['capability']}: parallel groups require exactly {sorted(required_group)}"
                )
            group_id, target_id = group["id"], group["node"]
            if not PARALLEL_ID_RE.fullmatch(group_id or ""):
                raise TopologyError(f"{recipe['capability']}: invalid parallel group id {group_id!r}")
            if group_id in group_ids or target_id in anchored:
                raise TopologyError(
                    f"{recipe['capability']}: duplicate parallel group/anchor {group_id!r}/{target_id!r}"
                )
            group_ids.add(group_id); anchored.add(target_id)
            target = by_id.get(target_id)
            if target is None:
                raise TopologyError(
                    f"{recipe['capability']}: parallel group node {target_id!r} not in graph"
                )
            kind = target.get("kind")
            if kind not in ("review-worker", "map-worker", "pipeline-stage"):
                raise TopologyError(
                    f"{recipe['capability']}: parallel target {target_id} must be a review, map, or pipeline worker"
                )
            # G6 / AC 21: a parallel group on a `terminal: true` node is rejected
            # at declaration -- the realized-graph check further downstream
            # (`_workflow_contract`'s duplicate-terminal_gate rejection) can never
            # fire for a group, because the expansion step (D3) always strips
            # `terminal`/`terminal_gate` from every non-anchor leg first. Without
            # this declaration-level gate a terminal node's peer expansion is
            # silently permitted. `autopilot-research claim-verify` already ships
            # this pattern and is kept as a recorded, non-silent grandfather.
            if target.get("terminal") is True and (recipe["capability"], group_id) not in TERMINAL_PARALLEL_GROUP_GRANDFATHER:
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: parallel group on terminal node "
                    f"{target_id!r} is rejected unless explicitly grandfathered"
                )
            if group["kind"] not in registry["parallel_group_kinds"]:
                raise TopologyError(f"{recipe['capability']}:{group_id}: invalid parallel group kind")
            if group["join_policy"] not in registry["parallel_join_policies"]:
                raise TopologyError(f"{recipe['capability']}:{group_id}: invalid parallel join policy")
            axes = group["independence_axes"]
            if (not isinstance(axes, list) or len(axes) != len(set(axes))
                    or not set(axes) <= set(registry["parallel_independence_axes"])):
                raise TopologyError(f"{recipe['capability']}:{group_id}: invalid independence axes")
            if "perspective" not in axes:
                raise TopologyError(f"{recipe['capability']}:{group_id}: perspective axis required")
            minimum = group["min_intensity"]
            if minimum not in tiers or tiers.index(minimum) < tiers.index("standard"):
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: min_intensity must be a standard+ tier"
                )
            expected_width_tiers = tiers[tiers.index(minimum):]
            widths = group["width_by_intensity"]
            if not isinstance(widths, dict) or list(widths) != expected_width_tiers:
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: width_by_intensity must cover ordered tiers {expected_width_tiers}"
                )
            values = list(widths.values())
            if (any(isinstance(value, bool) or not isinstance(value, int)
                    or value < 2 for value in values)
                    or values != sorted(values)):
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: widths must be monotonic integers in 2..{max_allowed}"
                )
            legs = group["legs"]
            if not isinstance(legs, list):
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: legs must be a list"
                )
            # AC 4: single merged rejection when declared peers + auxiliaries exceed
            # the schema cap. Every leg is either a peer or an auxiliary, so the
            # total declared leg count is the merged count (plan.md W1a step 5).
            if len(legs) > max_allowed:
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: legs exceed parallel_group_max_width {max_allowed}"
                )
            if len(legs) != max(values):
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: legs must equal maximum declared width"
                )
            suffixes, perspectives, profiles = [], [], []
            peers_in_prefix, auxiliary_count = 0, 0
            auxiliary_kinds, saw_auxiliary = set(), False
            for leg in legs:
                if isinstance(leg, dict) and "profile_demand" in leg:
                    PROFILE.resolve_profile_demand(leg["profile_demand"])
                # 1: leg_class missing is a distinct single-assertion rejection.
                if not isinstance(leg, dict) or "leg_class" not in leg:
                    raise TopologyError(
                        f"{recipe['capability']}:{group_id}: leg requires leg_class"
                    )
                leg_class = leg["leg_class"]
                # 2: vocabulary-external leg_class is a distinct rejection.
                if leg_class not in leg_classes:
                    raise TopologyError(
                        f"{recipe['capability']}:{group_id}: invalid leg_class {leg_class!r}"
                    )
                if leg_class == "peer":
                    # 6: an auxiliary before a later peer violates peer-first ordering.
                    if saw_auxiliary:
                        raise TopologyError(
                            f"{recipe['capability']}:{group_id}: auxiliary leg must not precede a peer leg"
                        )
                    # 4: a peer leg must not carry an auxiliary_check.
                    if "auxiliary_check" in leg:
                        raise TopologyError(
                            f"{recipe['capability']}:{group_id}: peer leg must not carry auxiliary_check"
                        )
                    if set(leg) - {"profile_demand"} != {"suffix", "perspective", "model_profile", "leg_class"}:
                        raise TopologyError(
                            f"{recipe['capability']}:{group_id}: peer legs require exactly "
                            "suffix, perspective, model_profile, leg_class"
                        )
                    peers_in_prefix += 1
                else:
                    if set(leg) - {"profile_demand"} != {"suffix", "perspective", "model_profile",
                                    "leg_class", "auxiliary_check"}:
                        raise TopologyError(
                            f"{recipe['capability']}:{group_id}: auxiliary legs require exactly "
                            "suffix, perspective, model_profile, leg_class, auxiliary_check"
                        )
                    # 3: an auxiliary leg without an auxiliary_check is a distinct rejection.
                    check = leg["auxiliary_check"]
                    if check not in auxiliary_checks:
                        raise TopologyError(
                            f"{recipe['capability']}:{group_id}: invalid auxiliary_check {check!r}"
                        )
                    # 5: non-light auxiliary is a distinct rejection (AC 1 case).
                    if leg["model_profile"] != "light":
                        raise TopologyError(
                            f"{recipe['capability']}:{group_id}: auxiliary leg must use model_profile light"
                        )
                    saw_auxiliary = True
                    auxiliary_count += 1
                    auxiliary_kinds.add(check)
                    # D9 / AC 5 (back half): an auxiliary leg's verdict enum is
                    # structurally non-blocking — its unit may not carry any
                    # unconditionally blocking verdict token.
                    aux_units = registry.get("auxiliary_check_units") or {}
                    unit = aux_units.get(check)
                    if not isinstance(unit, str) or not unit:
                        raise TopologyError(
                            f"{recipe['capability']}:{group_id}: no auxiliary unit mapping for {check!r}"
                        )
                    verdict = _unit_frontmatter(unit).get("verdict") or []
                    if set(token.lower() for token in verdict) & _BLOCKING_VERDICT_TOKENS:
                        raise TopologyError(
                            f"{recipe['capability']}:{group_id}: auxiliary unit {unit} "
                            "carries a blocking verdict token"
                        )
                suffix, perspective, profile = (
                    leg["suffix"], leg["perspective"], leg["model_profile"]
                )
                if not PARALLEL_ID_RE.fullmatch(suffix or ""):
                    raise TopologyError(f"{recipe['capability']}:{group_id}: invalid leg suffix")
                if not isinstance(perspective, str) or not perspective.strip():
                    raise TopologyError(f"{recipe['capability']}:{group_id}: perspective required")
                _validate_model_profile(
                    registry, profile, f"{recipe['capability']}:{group_id}:{suffix}"
                )
                suffixes.append(suffix); perspectives.append(perspective); profiles.append(profile)
            # Every declared width prefix must carry at least two peer legs.
            for width in values:
                if sum(leg.get("leg_class") == "peer" for leg in legs[:width]) < 2:
                    raise TopologyError(
                        f"{recipe['capability']}:{group_id}: each declared width must hold at least two peer legs"
                    )
            # AC 3: auxiliary check kinds are unique within one group.
            if len(auxiliary_kinds) != auxiliary_count:
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: auxiliary_check kinds must be unique"
                )
            # AC 4: single merged rejection when peers + auxiliaries exceed the schema cap.
            if peers_in_prefix + auxiliary_count > max_allowed:
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: legs exceed parallel_group_max_width {max_allowed}"
                )
            # AC 23 / D6: an auxiliary-bearing group's declared maximum width is at most 3,
            # while parallel_group_max_width stays the schema-level cap.
            if auxiliary_count and max(values) > 3:
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: auxiliary groups must keep declared width at most 3"
                )
            if suffixes[0] != "anchor" or len(suffixes) != len(set(suffixes)):
                raise TopologyError(f"{recipe['capability']}:{group_id}: ordered unique suffixes must start with anchor")
            if "perspective" in axes and len(perspectives) != len(set(perspectives)):
                raise TopologyError(f"{recipe['capability']}:{group_id}: perspective axis requires unique legs")
            if "model-profile" in axes and len(set(profiles)) < 2:
                raise TopologyError(f"{recipe['capability']}:{group_id}: model-profile axis lacks diversity")
            dependents = [n for n in nodes if target_id in n.get("depends_on", [])]
            if kind != "review-worker" and not dependents:
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: non-review parallel anchor requires a downstream consumer"
                )
            if kind == "pipeline-stage" and not any(
                dependent.get("kind") == "review-worker" for dependent in dependents
            ):
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: pipeline anchor requires a direct review arbiter"
                )
            # AC 22 / D4: an auxiliary-bearing group's anchor needs an arbiter for
            # `auxiliary_findings_considered`. A `terminal: true` anchor has no
            # downstream verdict to carry the array, so it structurally has no
            # arbiter — declaring an auxiliary leg on such a group compiles-rejects.
            if auxiliary_count and target.get("terminal") is True:
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: terminal anchor {target_id} "
                    "has no arbiter for auxiliary findings"
                )
            for out in target.get("outputs", []):
                if "*" not in out:
                    continue
                if kind == "map-worker" and out.endswith("/**") and "*" not in out[:-3]:
                    continue
                raise TopologyError(
                    f"{recipe['capability']}:{group_id}: target outputs must be concrete files "
                    "or a map-worker '<dir>/**' shard tree"
                )
            # 4.3.5: a leg suffix (`-alternative`, ...) must stay inside the same bucket
            # anchor as its base scope. `_parallel_path` is the exact function that
            # expands a route's write_scope for a realized leg, so re-running D-1's
            # containment check against every non-anchor leg's expanded scopes is the
            # only way to catch an escape like `designs/**` -> a leg that
            # would, once expanded, resolve one level *above* the owning cycle.
            for leg in legs[1:]:
                expanded = [_parallel_path(scope, leg["suffix"]) for scope in target["write_scope"]]
                # Node-kind is intentionally omitted here: a realized leg is a
                # deterministic *sibling* of the base scope (`01_refs` ->
                # `01_refs-alternative`), not a child of it, so re-running the
                # map/review-anchor subtree check would reject the isolation
                # mechanism itself. Only bucket-anchor containment (does the
                # leg still resolve inside the same cycle/root/external
                # domain?) is re-checked.
                _validate_bucket_anchor(
                    recipe, registry, expanded, None, f"{target_id}-{leg['suffix']}",
                    require_anchor_tail=True,
                )
    visiting, done = set(), set()
    def visit(node_id):
        if node_id in visiting: raise TopologyError(f"{recipe['capability']}: cycle")
        if node_id in done: return
        visiting.add(node_id)
        for dep in by_id[node_id].get("depends_on", []): visit(dep)
        visiting.remove(node_id); done.add(node_id)
    for node_id in ids: visit(node_id)
    ancestors = {}
    def deps(node_id):
        if node_id not in ancestors:
            ancestors[node_id] = set(by_id[node_id].get("depends_on", []))
            for dep in list(ancestors[node_id]): ancestors[node_id] |= deps(dep)
        return ancestors[node_id]
    for i, left in enumerate(nodes):
        for right in nodes[i + 1:]:
            if left["id"] in deps(right["id"]) or right["id"] in deps(left["id"]): continue
            if any(_overlap(a, b) for a in left["write_scope"] for b in right["write_scope"]):
                raise TopologyError(f"{recipe['capability']}: concurrent scope overlap {left['id']}/{right['id']}")
    _validate_conditional_extensions(recipe, registry, by_id, deps)
    _validate_continuations(recipe, registry, nodes, by_id)
    if not recipe["resume_retry_boundaries"] or not set(recipe["resume_retry_boundaries"]) <= set(ids):
        raise TopologyError(f"{recipe['capability']}: invalid retry boundaries")


PRODUCER_LIFECYCLE_STEPS = ["begin", "write", "finalize", "admit-shared"]
PRODUCER_LIFECYCLE_ENV = [
    "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_PRODUCER_ID",
    "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR",
]
PRODUCER_SHARED_KINDS = {"spec": "canonical", "analysis": "canonical", "research": "explicit-promotion"}


def _validate_producer_lifecycle(registry):
    """W7C: the producer begin/finalize vocabulary is a closed registry table."""
    table = registry.get("producer_lifecycle")
    if not isinstance(table, dict):
        raise TopologyError("producer_lifecycle registry table required")
    if table.get("contract") != "artifact-producer/v1":
        raise TopologyError("producer_lifecycle.contract must be artifact-producer/v1")
    if table.get("cutover_states") != ["inactive", "active"]:
        raise TopologyError("producer_lifecycle.cutover_states must be [inactive, active]")
    if table.get("steps") != PRODUCER_LIFECYCLE_STEPS:
        raise TopologyError("producer_lifecycle.steps vocabulary mismatch")
    if table.get("shared_kinds") != PRODUCER_SHARED_KINDS:
        raise TopologyError("producer_lifecycle.shared_kinds must be spec/analysis canonical, research explicit-promotion")
    if table.get("env") != PRODUCER_LIFECYCLE_ENV:
        raise TopologyError("producer_lifecycle.env must list the five producer-cycle variables")
    by_intensity = table.get("by_intensity")
    if not isinstance(by_intensity, dict) or set(by_intensity) != {"direct", "quick", "standard+"}:
        raise TopologyError("producer_lifecycle.by_intensity must cover direct, quick, standard+")
    for intensity, row in by_intensity.items():
        if not isinstance(row, dict) or set(row) != {"begin", "finalize", "stage_workers"}:
            raise TopologyError(f"producer_lifecycle.by_intensity[{intensity}] shape mismatch")
        expected_owner = "inline-owner" if intensity == "direct" else "dispatch-depth-1-owner"
        if row["begin"] != expected_owner or row["finalize"] != expected_owner:
            raise TopologyError(f"producer_lifecycle.by_intensity[{intensity}] owner mismatch")
        expected_workers = "join-open-cycle-by-node" if intensity == "standard+" else "none"
        if row["stage_workers"] != expected_workers:
            raise TopologyError(f"producer_lifecycle.by_intensity[{intensity}] stage_workers mismatch")
    if table.get("legacy_top_level_writes") != {"inactive": "allowed-compat-window", "active": "denied"}:
        raise TopologyError("producer_lifecycle.legacy_top_level_writes policy mismatch")
    buckets = registry.get("artifact_buckets", {})
    campaign_cycle = "campaigns/<campaign-locator>/<cycle-locator>/artifacts"
    if buckets.get("campaign-cycle") != campaign_cycle:
        raise TopologyError("artifact_buckets.campaign-cycle must declare the W7C cycle output layout")
    if table.get("cycle_layout") != f"{campaign_cycle}/<bucket>/...":
        raise TopologyError("producer_lifecycle.cycle_layout must match artifact_buckets.campaign-cycle")
    if buckets.get("shared-revision") != "shared/<kind>/<reference>/revisions/<revision>":
        raise TopologyError("artifact_buckets.shared-revision must declare the immutable shared layout")


# ---------------------------------------------------------------------------
# Part catalog (SD-165, stage-dispatch 13.64). A part is one stage addressed as
# `<capability>:<stage>`. The catalog carries what recipe nodes do not: a
# one-line summary, `shareable`, `start_approval`, widened `unit_choices`, input
# name mapping, and optional parts that exist only in an explicit graph. Recipe
# nodes, presets and `completion_gates` stay untouched; compose merges a catalog
# value only when the graph actually calls for it.
# ---------------------------------------------------------------------------
PART_ID_RE = re.compile(r"^[a-z][a-z0-9-]*:[a-z][a-z0-9-]*$")
PART_ROW_KEYS = frozenset({
    "summary", "shareable", "hosts", "start_approval", "unit_choices",
    "input_names", "optional_inputs", "optional", "execution_needs",
})
PART_SHAREABLE_KINDS = ("map-worker", "pipeline-stage", "review-worker", "resource-runner")
PART_ANCHOR_BY_KIND = {"map-worker": "map_anchor", "review-worker": "review_anchor"}
FRAME_TEMPLATE_FIELDS = ("kind", "unit", "worker_type", "role", "dispatch_depth", "launch_authority")


def part_catalog(registry):
    catalog = registry.get("part_catalog")
    return catalog if isinstance(catalog, dict) else {}


def part_row(registry, part_id):
    row = (part_catalog(registry).get("parts") or {}).get(part_id)
    return row if isinstance(row, dict) else {}


def part_recipe(registry, part_id):
    """(origin recipe, node) of a part: a recipe node, else a catalog optional part."""
    capability, _, stage = part_id.partition(":")
    for recipe in registry.get("recipes", []):
        if recipe.get("capability") != capability:
            continue
        for node in recipe["standard_plus"]["nodes"]:
            if node.get("id") == stage:
                return recipe, node
    optional = part_row(registry, part_id).get("optional")
    if isinstance(optional, dict):
        modes = set(optional.get("modes") or [])
        for recipe in registry.get("recipes", []):
            if recipe.get("capability") == capability and modes & set(recipe.get("modes", [])):
                return recipe, optional["node"]
    return None


def recipe_optional_parts(registry, recipe):
    """Optional catalog parts of one recipe, as [(stage, row)] in catalog order."""
    found = []
    for part_id, row in (part_catalog(registry).get("parts") or {}).items():
        optional = row.get("optional") if isinstance(row, dict) else None
        capability, _, stage = part_id.partition(":")
        if (isinstance(optional, dict) and capability == recipe.get("capability")
                and set(optional.get("modes") or []) & set(recipe.get("modes", []))):
            found.append((stage, row))
    return found


def relocate_part_path(scope, kind, capability, stage, path):
    """Host paths of one borrowed part path: `<kind anchor>/parts/<capability>/<stage>/<path>`.

    The whole origin path stays under the part directory, so a realized parallel
    leg (`_parallel_path`) always lands inside `parts/<capability>/<stage>/`. A
    literal host keeps every cycle anchor; the first result is the output path.
    """
    anchor = scope.get(PART_ANCHOR_BY_KIND[kind]) if kind in PART_ANCHOR_BY_KIND else None
    relative = "/".join(segment for segment in (anchor, "parts", capability, stage, path) if segment)
    if scope.get("anchor_mode", "implicit") == "literal" and scope.get("cycle_anchors"):
        return [f"{cycle_anchor}/{relative}" for cycle_anchor in scope["cycle_anchors"]]
    return [relative]


def resolve_shared_part(registry, host_recipe, part_id):
    """The borrowable part `part_id` for this host recipe, or None.

    None covers every "cannot borrow" case alike -- unregistered, not
    shareable, host not listed, or a map/review part whose host declares no
    anchor -- so the caller keeps its one existing unknown-node answer.
    """
    row = part_row(registry, part_id)
    if row.get("shareable") is not True:
        return None
    hosts = row.get("hosts")
    if hosts is not None and host_recipe.get("capability") not in hosts:
        return None
    found = part_recipe(registry, part_id)
    if found is None or (found[0].get("capability") == host_recipe.get("capability")
                         and set(found[0].get("modes", [])) & set(host_recipe.get("modes", []))):
        return None  # unregistered, or the host's own stage (not a borrow)
    origin, node = found
    scope = json.loads(json.dumps(host_recipe["artifact_scope"]))
    needed = PART_ANCHOR_BY_KIND.get(node.get("kind"))
    merged = None
    if needed and not scope.get(needed):
        declared = ((part_catalog(registry).get("hosts") or {}).get(host_recipe.get("capability")) or {}).get(needed)
        if not declared:
            return None
        scope[needed] = declared
        merged = needed
    capability, _, stage = part_id.partition(":")
    return {"part": part_id, "capability": capability, "stage": stage, "recipe": origin,
            "node": node, "row": row, "scope": scope, "merged_anchor": merged}


# Catalog stages a plan's extra stage may not be shaped on: owners, resource runners and frames
# are not stages, and these ids carry input rules of their own.
PLAN_STAGE_TEMPLATE_EXCLUDED_IDS = frozenset({"plan-check", "review"})


def plan_stage_part(registry, host_recipe, stage):
    """A part-shaped row for a stage a plan adds (`extra_stages`), or None.

    The stage is shaped on the first catalog stage whose unit is `stage["unit"]` and that writes no
    source -- the host's own stages first, then every other recipe -- and keeps that stage's kind,
    gate and budget; its
    files move under `parts/plan/<id>/` inside the host's scope, exactly as a borrowed part's do."""
    unit, stage_id = stage.get("unit"), stage.get("id")
    if not isinstance(unit, str) or not isinstance(stage_id, str):
        return None
    hosts_first = [host_recipe] + [r for r in registry.get("recipes", []) if r is not host_recipe]
    for recipe in hosts_first:
        for node in recipe["standard_plus"]["nodes"]:
            if (node.get("unit") == unit and node.get("dispatch_depth") == 2
                    and node.get("kind") not in ("capability-owner", "resource-runner", "runtime-terminal")
                    and node.get("worker_type") != "frame" and (node.get("continuation") or {}).get("kind", "inline-next") == "inline-next"
                    and node.get("id") not in PLAN_STAGE_TEMPLATE_EXCLUDED_IDS
                    # A plan adds checks and measurements, never a stage that changes the source:
                    # one placed after the last check would land unreviewed.
                    and not node.get("commit_expected")
                    and not any(worktree_mutating_scope(scope) for scope in node.get("write_scope") or ())):
                scope = json.loads(json.dumps(host_recipe["artifact_scope"]))
                needed = PART_ANCHOR_BY_KIND.get(node.get("kind"))
                if needed and not scope.get(needed):
                    return None
                return {"part": f"{recipe['capability']}:{node['id']}", "capability": "plan", "stage": stage_id,
                        "recipe": recipe, "node": node, "row": {}, "scope": scope, "merged_anchor": None,
                        "plan_stage": {"id": stage_id, "template": node["id"],
                                       **({"verify": stage["verify"]} if stage.get("verify") else {})}}
    return None


def borrowable_parts(registry, host_recipe):
    """Part ids this host recipe can borrow, in catalog order."""
    return [part_id for part_id in (part_catalog(registry).get("parts") or {})
            if resolve_shared_part(registry, host_recipe, part_id) is not None]


def frame_brief_inputs(registry, capability):
    """Input names under which `capability`'s consumer node reads the frame briefs."""
    row = ((part_catalog(registry).get("frame") or {}).get("briefs") or {}).get(capability)
    return list(row["inputs"]) if isinstance(row, dict) else []


def _validate_optional_part(registry, recipe, part_id, optional, owner_profile):
    """An optional part must be a valid node of its recipe when selected alone."""
    if not isinstance(optional, dict) or set(optional) != {"modes", "after", "before", "node"}:
        raise TopologyError(f"part {part_id}: optional requires exactly modes, after, before, node")
    node = optional["node"]
    stage = part_id.partition(":")[2]
    if not isinstance(node, dict) or node.get("id") != stage:
        raise TopologyError(f"part {part_id}: optional node id must equal the stage id")
    # A detached, supervised resource is the one resource shape an optional part may take.
    supervised_resource = (node.get("kind") == "resource-runner"
                           and node.get("resource_transport") == "detached-process"
                           and node.get("continuation") == {"kind": "supervised"})
    if node.get("kind") not in ("pipeline-stage", "review-worker", "map-worker") and not supervised_resource:
        raise TopologyError(f"part {part_id}: optional part must be a pipeline, review, or map worker")
    known = {n["id"] for n in recipe["standard_plus"]["nodes"]} | {
        other for other, _row in recipe_optional_parts(registry, recipe)}
    for key in ("after", "before"):
        names = optional[key]
        if not isinstance(names, list) or not set(names) <= known - {stage}:
            raise TopologyError(f"part {part_id}: optional.{key} must name stages of its recipe")
    capability = part_id.partition(":")[0]
    modes = set(optional["modes"] or [])
    targets = [other for other in registry.get("recipes", [])
               if other.get("capability") == capability and modes & set(other.get("modes", []))
               ] if supervised_resource else [recipe]
    for target in targets:
        _validate_optional_probe(registry, target, stage, node, supervised_resource, owner_profile)


def _validate_optional_probe(registry, recipe, stage, node, supervised_resource, owner_profile):
    """Validate the optional node alone, followed by the recipe's own check when it is a resource."""
    alone = json.loads(json.dumps(node))
    alone.update(depends_on=[], terminal=True, terminal_gate=alone.get("completion_gate"),
                 advance_class="model-required", model_required_reason="terminal-report")
    alone.pop("continuation", None)
    probe_nodes = [alone]
    if supervised_resource:
        # A supervised resource is never terminal: its successor is the recipe's first
        # review worker after the recipe's own resource node.
        alone = json.loads(json.dumps(node))
        nodes = recipe["standard_plus"]["nodes"]
        start = next((i for i, n in enumerate(nodes) if n.get("kind") == "resource-runner"), None)
        successor = next((n for n in nodes[(start or 0) + 1:] if n.get("kind") == "review-worker"), None)
        if start is None or successor is None:
            raise TopologyError(f"part {recipe['capability']}:{stage}: a supervised resource needs "
                                f"a review worker after the recipe's own resource node")
        verification = json.loads(json.dumps(successor))
        verification.update(depends_on=[stage], terminal=True,
                            terminal_gate=verification["completion_gate"])
        verification.pop("continuation", None)
        probe_nodes = [alone, verification]
    probe = {key: json.loads(json.dumps(recipe[key])) for key in (
        "capability", "modes", "topology_class", "direct_predicates", "promotion_signals",
        "artifact_scope", "quick")}
    probe.update({
        "standard_plus": {"topology": recipe["standard_plus"].get("topology", recipe["topology_class"]),
                          "owner_dispatch_depth": recipe["standard_plus"]["owner_dispatch_depth"],
                          "max_dispatch_depth": max(n.get("dispatch_depth", 0) for n in probe_nodes),
                          "nodes": probe_nodes},
        "completion_gates": [n.get("completion_gate") for n in probe_nodes], "human_gates": [],
        "human_gate_bindings": [], "resume_retry_boundaries": [stage],
    })
    _validate_recipe(probe, registry, owner_profile)
    _validate_gate_contracts(probe, registry)


def _validate_part_catalog(registry, owner_profile):
    """SD-165: the catalog may only describe, widen, or add -- never redefine a recipe node."""
    if "part_catalog" not in registry:
        return
    catalog = registry["part_catalog"]
    if not isinstance(catalog, dict) or set(catalog) != {"schema_version", "frame", "hosts", "parts"}:
        raise TopologyError("part_catalog must declare exactly schema_version, frame, hosts, parts")
    if catalog["schema_version"] != 1:
        raise TopologyError("part_catalog.schema_version must be 1")
    rows = catalog["parts"]
    if not isinstance(rows, dict):
        raise TopologyError("part_catalog.parts must be an object")
    capabilities = {recipe["capability"] for recipe in registry["recipes"]}
    node_ids = {}
    for recipe in registry["recipes"]:
        if recipe["capability"] == ROUTE_FRAME_CAPABILITY:
            continue  # compiler-internal: never composable, so it has no catalog parts
        for node in recipe["standard_plus"]["nodes"]:
            key = f"{recipe['capability']}:{node['id']}"
            if key in node_ids:
                raise TopologyError(f"part id {key} is not unique across the capability's recipes")
            if node["id"] in capabilities:
                raise TopologyError(f"{key}: a stage id may not equal a capability id")
            node_ids[key] = (recipe, node)
    missing = sorted(set(node_ids) - set(rows))
    if missing:
        raise TopologyError(f"part_catalog.parts lacks a row for recipe stages {missing}")
    outputs_by_capability = {}
    for part_id, row in rows.items():
        if not isinstance(part_id, str) or not PART_ID_RE.fullmatch(part_id):
            raise TopologyError(f"invalid part id {part_id!r}")
        if not isinstance(row, dict) or set(row) - PART_ROW_KEYS:
            raise TopologyError(f"part {part_id}: unknown catalog fields {sorted(set(row) - PART_ROW_KEYS)}")
        execution_needs = row.get("execution_needs", [])
        if (not isinstance(execution_needs, list)
                or any(not isinstance(item, str) for item in execution_needs)
                or len(execution_needs) != len(set(execution_needs))
                or not set(execution_needs) <= {"compute-hosts", "host-process"}):
            raise TopologyError(f"part {part_id}: invalid execution_needs")
        summary = row.get("summary")
        if not isinstance(summary, str) or not summary.strip() or "\n" in summary:
            raise TopologyError(f"part {part_id}: a one-line summary is required")
        capability = part_id.partition(":")[0]
        if part_id in node_ids:
            if "optional" in row:
                raise TopologyError(f"part {part_id}: a recipe stage cannot be declared optional")
            recipe, node = node_ids[part_id]
        else:
            found = part_recipe(registry, part_id)
            if found is None or "optional" not in row:
                raise TopologyError(f"part {part_id}: not a recipe stage and no optional part declared")
            recipe, node = found
            _validate_optional_part(registry, recipe, part_id, row["optional"], owner_profile)
        outputs_by_capability.setdefault(capability, set()).update(
            out for out in node.get("outputs", []) if not _is_semantic_output(out))
        if "start_approval" in row and (not isinstance(row["start_approval"], str) or not row["start_approval"]):
            raise TopologyError(f"part {part_id}: start_approval must be a non-empty string")
        if "optional_inputs" in row and (
                not isinstance(row["optional_inputs"], list)
                or any(not isinstance(name, str) or not name for name in row["optional_inputs"])):
            raise TopologyError(f"part {part_id}: optional_inputs must be a list of names")
        if "unit_choices" in row:
            if "unit_choices" in node or node.get("kind") in ("capability-owner", "resource-runner"):
                raise TopologyError(f"part {part_id}: catalog unit_choices cannot redefine the node's own")
            widened = dict(node, unit_choices=row["unit_choices"])
            _validate_unit_ref(recipe, widened, registry)
        if "hosts" in row and row.get("shareable") is not True:
            raise TopologyError(f"part {part_id}: hosts is only valid on a shareable part")
        if "shareable" in row:
            if not isinstance(row["shareable"], bool):
                raise TopologyError(f"part {part_id}: shareable must be a boolean")
            hosts = row.get("hosts")
            if hosts is not None and (not isinstance(hosts, list) or not hosts or not set(hosts) <= capabilities):
                raise TopologyError(f"part {part_id}: hosts must list known capabilities")
            if row["shareable"]:
                scope = recipe["artifact_scope"]
                outside = set(scope.get("external_scopes", []))
                root_anchors = scope.get("root_anchors", [])
                paths = list(node["write_scope"]) + [
                    out for out in node.get("outputs", []) if not _is_semantic_output(out)]
                if node.get("kind") not in PART_SHAREABLE_KINDS:
                    raise TopologyError(f"part {part_id}: kind {node.get('kind')} cannot be shared")
                if scope.get("anchor_mode", "implicit") != "implicit":
                    raise TopologyError(f"part {part_id}: only an implicit-anchor recipe's part can be shared")
                for path in paths:
                    root = _scope_root(path)
                    if path in outside or any(root == a or root.startswith(a + "/") for a in root_anchors):
                        # Source-tree and root-anchored writes never travel with a part.
                        raise TopologyError(f"part {part_id}: write outside the cycle cannot be shared ({path})")
    for part_id, row in rows.items():
        names = row.get("input_names")
        if names is None:
            continue
        _recipe, node = part_recipe(registry, part_id)
        known_outputs = outputs_by_capability.get(part_id.partition(":")[0], set())
        if not isinstance(names, dict) or not names or not set(names) <= set(node.get("inputs", [])) \
                or not set(names.values()) <= known_outputs:
            raise TopologyError(
                f"part {part_id}: input_names must map the part's inputs to outputs of its capability")
    hosts = catalog["hosts"]
    if not isinstance(hosts, dict) or not set(hosts) <= capabilities:
        raise TopologyError("part_catalog.hosts must be keyed by known capabilities")
    for capability, anchors in hosts.items():
        if not isinstance(anchors, dict) or not anchors or not set(anchors) <= set(PART_ANCHOR_BY_KIND.values()) \
                or any(not isinstance(value, str) or not value for value in anchors.values()):
            raise TopologyError(f"part_catalog.hosts[{capability!r}] must declare map_anchor and/or review_anchor")
        for recipe in registry["recipes"]:
            if recipe["capability"] == capability and any(recipe["artifact_scope"].get(k) for k in anchors):
                raise TopologyError(f"part_catalog.hosts[{capability!r}] redeclares an anchor its recipe already has")
    # Every shareable part must land validly in every host that can take it.
    for part_id, row in rows.items():
        if row.get("shareable") is not True:
            continue
        for host in registry["recipes"]:
            part = resolve_shared_part(registry, host, part_id)
            if part is None:
                continue
            kind = part["node"]["kind"]
            probe = dict(host, artifact_scope=part["scope"])
            scopes = [moved for path in part["node"]["write_scope"]
                      for moved in relocate_part_path(part["scope"], kind, part["capability"], part["stage"], path)]
            label = f"{part['capability']}-{part['stage']}"
            _validate_bucket_anchor(probe, registry, scopes, kind, label)
            _validate_guard_scope(probe, scopes, [], registry, label)
    frame = catalog["frame"]
    if not isinstance(frame, dict) or set(frame) != {"template", "aliases", "brief_outputs", "briefs"}:
        raise TopologyError("part_catalog.frame must declare template, aliases, brief_outputs, briefs")
    template = frame["template"]
    if not isinstance(template, dict) or not set(FRAME_TEMPLATE_FIELDS) <= set(template):
        raise TopologyError("part_catalog.frame.template lacks a template field")
    aliases = frame["aliases"]
    framed = {recipe["capability"] for recipe in registry["recipes"]
              if recipe["capability"] != ROUTE_FRAME_CAPABILITY
              and any(node["id"] in aliases for node in recipe["standard_plus"]["nodes"])}
    if not isinstance(frame["briefs"], dict) or set(frame["briefs"]) != framed:
        raise TopologyError("part_catalog.frame.briefs must cover exactly the recipes that carry a frame")
    for recipe in registry["recipes"]:
        if recipe["capability"] not in framed:
            continue
        by_id = {node["id"]: node for node in recipe["standard_plus"]["nodes"]}
        for alias in aliases:
            node = by_id.get(alias)
            if node is None or any(node.get(field) != template[field] for field in FRAME_TEMPLATE_FIELDS):
                raise TopologyError(
                    f"{recipe['capability']}:{alias}: frame alias differs from the catalog template")
        brief = frame["briefs"][recipe["capability"]]
        consumer = by_id.get(brief.get("consumer")) if isinstance(brief, dict) else None
        if consumer is None or not isinstance(brief.get("inputs"), list) \
                or len(brief["inputs"]) != len(frame["brief_outputs"]) \
                or not set(brief["inputs"]) <= set(consumer.get("inputs", [])):
            raise TopologyError(
                f"{recipe['capability']}: frame brief mapping does not match the consumer node inputs")


def validate_registry(registry, manifest=None):
    if registry.get("schema_version") != 10:
        raise TopologyError("legacy topology registry is read-only")
    _validate_activation_conditions(registry)
    _validate_workflow_vocabulary(registry)
    expected_profiles = {
        "deep": {"tier": "deep", "effort": "high", "registered_topology": True},
        "balanced-deep": {"tier": "deep", "effort": "medium", "registered_topology": True},
        "balanced": {"tier": "balanced", "effort": "high", "registered_topology": True},
        "light": {"tier": "light", "effort": "medium", "registered_topology": True},
        "mini": {"tier": "mini", "effort": "low", "registered_topology": False},
    }
    if registry.get("model_profiles") != expected_profiles:
        raise TopologyError("model_profiles must declare the five portable execution profiles")
    owner_policy = registry.get("owner_profile_by_intensity", {})
    standard_plus_profiles = {
        owner_policy.get(intensity)
        for intensity in ("standard", "strong", "thorough", "adversarial")
    }
    if len(standard_plus_profiles) != 1 or None in standard_plus_profiles:
        raise TopologyError("standard+ owner profiles must be uniform across intensities")
    standard_plus_owner_profile = next(iter(standard_plus_profiles))
    if owner_policy != {
        "quick": "balanced-deep", "standard": "deep", "strong": "deep",
        "thorough": "deep", "adversarial": "deep",
    }:
        raise TopologyError("owner_profile_by_intensity does not match the portable policy")
    if registry.get("parallel_group_kinds") != ["replicate", "explore", "adversarial", "verify"]:
        raise TopologyError("parallel group kind vocabulary mismatch")
    if registry.get("parallel_join_policies") != ["all"]:
        raise TopologyError("parallel join policy must currently be all")
    if registry.get("parallel_independence_axes") != ["cross-harness", "model-profile", "perspective"]:
        raise TopologyError("parallel independence-axis vocabulary mismatch")
    if registry.get("parallel_group_max_width") != 4:
        raise TopologyError("parallel_group_max_width must be 4")
    if registry.get("leg_classes") != ["peer", "auxiliary"]:
        raise TopologyError("leg_classes must declare exactly peer and auxiliary")
    if registry.get("auxiliary_checks") != [
        "assumption-check", "edge-case-check", "failure-mode-check",
        "simplicity-check", "test-gap-check",
    ]:
        raise TopologyError("auxiliary_checks must declare the closed five checks")
    if registry.get("auxiliary_check_units") != {
        "assumption-check": "qa/assumption-check",
        "edge-case-check": "qa/edge-case-check",
        "failure-mode-check": "qa/failure-mode-check",
        "simplicity-check": "qa/simplicity-check",
        "test-gap-check": "qa/test-gap-check",
    }:
        raise TopologyError("auxiliary_check_units must map each check to its qa unit")
    if set(registry.get("transports", [])) != WRAPPER_TRANSPORTS:
        raise TopologyError("transport vocabulary differs from portable dispatch contract")
    if set(registry.get("execution_surfaces", [])) != EXECUTION_SURFACES:
        raise TopologyError("execution-surface vocabulary differs from portable dispatch contract")
    if set(registry.get("fallback_hops", [])) != FALLBACK_HOPS:
        raise TopologyError("fallback-hop vocabulary differs from portable dispatch contract")
    if registry.get("tracking_values") != ["tracked", "untracked"]:
        raise TopologyError("tracking_values must declare tracked and untracked independently")
    if set(registry.get("tracked_gate_evidence", [])) != {
        "spec_read", "drift_verdict", "workflow_mode", "artifact_guard"
    }:
        raise TopologyError("tracked_gate_evidence must contain the four SD-45 fields")
    if "artifact-order-prechecked" not in registry.get("guard_preconditions", []):
        raise TopologyError("artifact-order-prechecked guard precondition missing")
    if registry.get("artifact_owners", {}).get("spec") != "autopilot-spec":
        raise TopologyError("spec sole-update-path owner must be autopilot-spec")
    if registry.get("rollout", {}).get("route_compiler") != "enforced":
        raise TopologyError("route compiler rollout must be enforced")
    if "legacy_low_level_dispatch" in registry.get("rollout", {}):
        raise TopologyError("legacy_low_level_dispatch is retired")
    # D-1 (P4-10): every declared bucket must be a safe artifact-root-relative path, and
    # every recipe must fail closed with a declared artifact_scope -- a new recipe added
    # without one is exactly the P1-1/P1-1b regression this cycle is closing.
    buckets = registry.get("artifact_buckets")
    if not isinstance(buckets, dict) or not buckets:
        raise TopologyError("artifact_buckets registry table required")
    for name, value in buckets.items():
        if not isinstance(value, str) or not value or value.startswith("/") or ".." in value.split("/"):
            raise TopologyError(f"artifact_buckets[{name!r}] must be a safe relative path")
    for recipe in registry["recipes"]:
        if not isinstance(recipe.get("artifact_scope"), dict):
            raise TopologyError(f"{recipe['capability']}: artifact_scope required (fail-closed for new recipes)")
    _validate_producer_lifecycle(registry)
    actual, expected = recipe_keys(registry), expected_recipe_keys(manifest)
    if actual != expected:
        raise TopologyError(f"coverage mismatch missing={sorted(expected-actual)} extra={sorted(actual-expected)}")
    for recipe in registry["recipes"]:
        _validate_recipe(recipe, registry, standard_plus_owner_profile)
        _validate_gate_contracts(recipe, registry)
    _validate_part_catalog(registry, standard_plus_owner_profile)
    return {"capabilities": len({x[0] for x in actual}), "recipes": len(actual), "registry_digest": registry_digest(registry)}


def resolve_recipe(registry, capability, capability_mode="default"):
    known_modes = []
    for recipe in registry["recipes"]:
        if recipe["capability"] == capability:
            if capability_mode in recipe["modes"]:
                return recipe
            known_modes.extend(recipe["modes"])
    if known_modes:
        raise TopologyError(
            f"unknown capability/mode: {capability}/{capability_mode}"
            f" (valid modes: {', '.join(sorted(set(known_modes)))})"
        )
    known = ", ".join(sorted({r["capability"] for r in registry["recipes"]}))
    raise TopologyError(
        f"unknown capability/mode: {capability}/{capability_mode}"
        f" (unknown capability; known: {known})"
    )


def capability_summary(registry, capability):
    rows = [r for r in registry["recipes"] if r["capability"] == capability]
    if not rows: raise TopologyError(f"unknown capability: {capability}")
    return {"topology_registry": "capabilities/topologies.json", "registry_digest": registry_digest(registry),
            "capability_modes": sorted({m for r in rows for m in r["modes"]}),
            "topology_classes": sorted({r["topology_class"] for r in rows})}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=("validate", "summary", "digest")); parser.add_argument("--capability")
    args = parser.parse_args(); registry = load_registry()
    if args.command == "validate": output = validate_registry(registry)
    elif args.command == "digest": output = {"registry_digest": registry_digest(registry)}
    else: output = capability_summary(registry, args.capability or "")
    for key, value in output.items(): print(f"{key}={','.join(value) if isinstance(value, list) else value}")


if __name__ == "__main__": main()
